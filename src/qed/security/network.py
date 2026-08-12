"""Deterministic, bounded network policy for citation bytes."""

from __future__ import annotations

import gzip
import http.client
import io
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import monotonic
from urllib.parse import urljoin, urlsplit


class NetworkPolicyError(ValueError):
    """A citation request is outside the explicit literature boundary."""


@dataclass(frozen=True, slots=True)
class CitationResponse:
    url: str
    media_type: str
    content: bytes


class RestrictedCitationFetcher:
    """Fetch bounded untrusted literature bytes from an exact hostname allowlist."""

    def __init__(
        self,
        allowed_hosts: tuple[str, ...],
        *,
        max_redirects: int = 3,
        max_bytes: int = 5 * 1024 * 1024,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("citation allowlist cannot be empty")
        if max_redirects < 0 or max_bytes <= 0 or timeout_seconds <= 0:
            raise ValueError("citation network limits must be positive")
        self.allowed_hosts = tuple(self._normalize_host(host) for host in allowed_hosts)
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _normalize_host(host: str) -> str:
        value = host.strip().rstrip(".").lower()
        if not value or any(character.isspace() for character in value):
            raise NetworkPolicyError("citation hostname is empty or contains whitespace")
        try:
            return value.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise NetworkPolicyError("citation hostname is not valid IDNA") from error

    def normalize_url(self, url: str) -> tuple[str, str, int, str]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise NetworkPolicyError("citation URL scheme must be http or https")
        if parsed.username is not None or parsed.password is not None:
            raise NetworkPolicyError("citation URL cannot contain userinfo")
        if parsed.fragment:
            raise NetworkPolicyError("citation URL cannot contain a fragment")
        if not parsed.hostname:
            raise NetworkPolicyError("citation URL must contain a hostname")
        hostname = self._normalize_host(parsed.hostname)
        if hostname not in self.allowed_hosts:
            raise NetworkPolicyError(f"citation hostname is not allowlisted: {hostname}")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise NetworkPolicyError("citation URL has an invalid port") from error
        if not 1 <= port <= 65535:
            raise NetworkPolicyError("citation URL port is outside the valid range")
        path = parsed.path or "/"
        if (
            "\\" in path
            or "\x00" in path
            or any(part == ".." for part in PurePosixPath(path).parts)
        ):
            raise NetworkPolicyError("citation URL path contains traversal")
        normalized = parsed._replace(
            netloc=hostname if parsed.port is None else f"{hostname}:{port}",
            path=path,
            fragment="",
        ).geturl()
        return normalized, hostname, port, parsed.scheme

    @staticmethod
    def _validate_address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise NetworkPolicyError(f"DNS returned an invalid address: {address}") from error
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            raise NetworkPolicyError("IPv4-in-IPv6 citation address is forbidden")
        if (
            parsed.is_loopback
            or parsed.is_private
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_unspecified
            or parsed.is_reserved
            or str(parsed) in {"169.254.169.254", "100.100.100.200"}
        ):
            raise NetworkPolicyError(f"citation address is not public: {parsed}")
        return parsed

    def resolve_and_validate(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            records = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise NetworkPolicyError(f"citation DNS resolution failed for {hostname}") from error
        addresses = tuple(
            sorted({str(self._validate_address(str(record[4][0]))) for record in records})
        )
        if not addresses:
            raise NetworkPolicyError(f"citation DNS returned no addresses for {hostname}")
        return addresses

    def validate_redirect(self, current_url: str, location: str) -> str:
        target = urljoin(current_url, location)
        self.normalize_url(target)
        return target

    def fetch(self, url: str) -> CitationResponse:
        """Fetch bytes with manual redirects and deterministic size/content checks."""

        current = url
        started = monotonic()
        for redirect_count in range(self.max_redirects + 1):
            normalized, hostname, port, _scheme = self.normalize_url(current)
            before = self.resolve_and_validate(hostname, port)
            if monotonic() - started > self.timeout_seconds:
                raise NetworkPolicyError("citation total timeout exceeded")
            parsed = urlsplit(normalized)
            address = before[0]
            connection: http.client.HTTPConnection
            if parsed.scheme == "https":
                connection = _PinnedHTTPSConnection(
                    hostname,
                    port,
                    address,
                    timeout=self.timeout_seconds,
                )
            else:
                connection = _PinnedHTTPConnection(
                    hostname,
                    port,
                    address,
                    timeout=self.timeout_seconds,
                )
            request_path = parsed.path or "/"
            if parsed.query:
                request_path += f"?{parsed.query}"
            try:
                connection.request(
                    "GET",
                    request_path,
                    headers={
                        "Accept": "text/plain,text/html,application/pdf",
                        "Host": parsed.netloc,
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise NetworkPolicyError("citation redirect omitted Location")
                    if redirect_count >= self.max_redirects:
                        raise NetworkPolicyError("citation redirect limit exceeded")
                    current = self.validate_redirect(normalized, location)
                    continue
                if response.status < 200 or response.status >= 300:
                    raise NetworkPolicyError(
                        f"citation server returned HTTP {response.status}"
                    )
                after = self.resolve_and_validate(hostname, port)
                if before != after:
                    raise NetworkPolicyError("citation DNS answer changed during one request")
                encoding = response.getheader("Content-Encoding", "").lower()
                if encoding not in {"", "identity"}:
                    raise NetworkPolicyError("compressed citation responses are disabled")
                media_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                if media_type not in {"text/plain", "text/html", "application/pdf"}:
                    raise NetworkPolicyError(
                        f"citation MIME type is not allowlisted: {media_type or 'missing'}"
                    )
                content_length = response.getheader("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as error:
                        raise NetworkPolicyError("citation Content-Length is invalid") from error
                    if declared_length > self.max_bytes:
                        raise NetworkPolicyError("citation response exceeds the byte limit")
                content = response.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise NetworkPolicyError("citation response exceeds the byte limit")
            except (OSError, http.client.HTTPException) as error:
                raise NetworkPolicyError("citation connection failed") from error
            finally:
                connection.close()
            if media_type == "application/pdf" and not content.startswith(b"%PDF-"):
                raise NetworkPolicyError("citation PDF failed magic-byte sniffing")
            if media_type in {"text/plain", "text/html"}:
                try:
                    decoded = content.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise NetworkPolicyError("citation text is not valid UTF-8") from error
                if media_type == "text/html" and decoded.lstrip().startswith("%PDF-"):
                    raise NetworkPolicyError("citation MIME type conflicts with PDF bytes")
            return CitationResponse(url=normalized, media_type=media_type, content=content)
        raise NetworkPolicyError("citation redirect handling failed closed")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, address: str, *, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str, *, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._address = address
        self._server_hostname = hostname

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw, server_hostname=self._server_hostname)


def decompress_bounded_gzip(content: bytes, *, max_bytes: int) -> bytes:
    """Utility for tests and explicit callers; decompression is bounded."""

    with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
        result = stream.read(max_bytes + 1)
    if len(result) > max_bytes:
        raise NetworkPolicyError("decompressed citation content exceeds the byte limit")
    return result
