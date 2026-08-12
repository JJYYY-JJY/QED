from __future__ import annotations

import gzip
import io
from pathlib import Path

import pytest

import qed.security.network as network
from qed.runtime.isolation import prepare_codex_home
from qed.security.network import (
    NetworkPolicyError,
    RestrictedCitationFetcher,
    decompress_bounded_gzip,
)
from qed.security.paths import PathSecurityError, ensure_private_directory


def test_codex_home_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises((PathSecurityError, ValueError), match="link"):
        prepare_codex_home(linked / "codex-home")


def test_private_directory_rejects_group_or_world_access(tmp_path: Path) -> None:
    directory = tmp_path / "managed"
    directory.mkdir(mode=0o755)

    with pytest.raises(PathSecurityError, match="0700"):
        ensure_private_directory(directory)


@pytest.mark.parametrize(
    "url",
    (
        "file:///tmp/proof.pdf",
        "http://127.0.0.1/proof.pdf",
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:127.0.0.1]/proof.pdf",
        "https://literature.example/a/../secret",
    ),
)
def test_citation_url_policy_rejects_unsafe_targets(url: str) -> None:
    fetcher = RestrictedCitationFetcher(("literature.example",))

    with pytest.raises(NetworkPolicyError):
        fetcher.normalize_url(url)


def test_citation_dns_policy_rejects_private_and_metadata_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = RestrictedCitationFetcher(("literature.example",))
    monkeypatch.setattr(
        "qed.security.network.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("192.168.1.2", 443)),
        ],
    )

    with pytest.raises(NetworkPolicyError, match="not public"):
        fetcher.resolve_and_validate("literature.example", 443)


def test_compressed_citation_bytes_are_bounded() -> None:
    payload = gzip.compress(b"x" * 100)

    with pytest.raises(NetworkPolicyError, match="exceeds"):
        decompress_bounded_gzip(payload, max_bytes=10)

    stream = io.BytesIO(payload)
    assert stream.getvalue().startswith(b"\x1f\x8b")


class _Response:
    def __init__(self, status: int, headers: dict[str, str], content: bytes) -> None:
        self.status = status
        self.headers = headers
        self.content = content

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, _limit: int) -> bytes:
        return self.content


class _Connection:
    responses: list[_Response] = []
    fail_request = False

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.response = self.responses.pop(0)
        self.closed = False
        self.requested_path: str | None = None

    def request(self, _method: str, path: str, **_kwargs: object) -> None:
        if self.fail_request:
            raise OSError("injected connection failure")
        self.requested_path = path

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


def _fake_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_Response],
    *,
    max_redirects: int = 3,
    max_bytes: int = 100,
) -> RestrictedCitationFetcher:
    _Connection.responses = list(responses)
    _Connection.fail_request = False
    monkeypatch.setattr(network, "_PinnedHTTPConnection", _Connection)
    monkeypatch.setattr(network, "_PinnedHTTPSConnection", _Connection)
    fetcher = RestrictedCitationFetcher(
        ("literature.example",),
        max_redirects=max_redirects,
        max_bytes=max_bytes,
    )
    monkeypatch.setattr(fetcher, "resolve_and_validate", lambda *_args: ("203.0.113.10",))
    return fetcher


def test_restricted_fetcher_success_redirect_and_pdf_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(200, {"Content-Type": "text/plain", "Content-Length": "5"}, b"hello")
    fetcher = _fake_fetcher(monkeypatch, [response])
    result = fetcher.fetch("http://literature.example/paper?q=1")
    assert result.url == "http://literature.example/paper?q=1"
    assert result.content == b"hello"

    redirect = _Response(302, {"Location": "/final"}, b"")
    final = _Response(200, {"Content-Type": "application/pdf"}, b"%PDF-1.7")
    fetcher = _fake_fetcher(monkeypatch, [redirect, final], max_redirects=1)
    assert fetcher.fetch("https://literature.example/start").media_type == "application/pdf"


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (_Response(302, {}, b""), "redirect omitted"),
        (_Response(302, {"Location": "/next"}, b""), "redirect limit"),
        (_Response(500, {}, b""), "HTTP 500"),
        (
            _Response(200, {"Content-Encoding": "gzip", "Content-Type": "text/plain"}, b"x"),
            "compressed",
        ),
        (_Response(200, {"Content-Type": "application/octet-stream"}, b"x"), "MIME"),
        (_Response(200, {"Content-Type": "text/plain", "Content-Length": "bad"}, b"x"), "invalid"),
        (_Response(200, {"Content-Type": "text/plain", "Content-Length": "101"}, b"x"), "exceeds"),
        (_Response(200, {"Content-Type": "application/pdf"}, b"not pdf"), "magic"),
        (_Response(200, {"Content-Type": "text/plain"}, b"\xff"), "UTF-8"),
        (_Response(200, {"Content-Type": "text/html"}, b"  %PDF-1.7"), "conflicts"),
    ),
)
def test_restricted_fetcher_rejects_response_policy_violations(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response,
    message: str,
) -> None:
    max_redirects = 0 if response.status == 302 and response.getheader("Location") else 3
    fetcher = _fake_fetcher(monkeypatch, [response], max_redirects=max_redirects)
    if response.status == 302 and response.getheader("Location"):
        fetcher = _fake_fetcher(
            monkeypatch,
            [response, response],
            max_redirects=1,
        )
    with pytest.raises(NetworkPolicyError, match=message):
        fetcher.fetch("http://literature.example/paper")


def test_restricted_fetcher_rejects_dns_rebinding_and_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(200, {"Content-Type": "text/plain"}, b"ok")
    fetcher = _fake_fetcher(monkeypatch, [response])
    answers = iter((("203.0.113.10",), ("203.0.113.11",)))
    monkeypatch.setattr(fetcher, "resolve_and_validate", lambda *_args: next(answers))
    with pytest.raises(NetworkPolicyError, match="DNS answer changed"):
        fetcher.fetch("http://literature.example/paper")

    _Connection.responses = [response]
    _Connection.fail_request = True
    monkeypatch.setattr(network, "_PinnedHTTPConnection", _Connection)
    monkeypatch.setattr(network, "_PinnedHTTPSConnection", _Connection)
    fetcher = RestrictedCitationFetcher(("literature.example",))
    monkeypatch.setattr(fetcher, "resolve_and_validate", lambda *_args: ("203.0.113.10",))
    with pytest.raises(NetworkPolicyError, match="connection failed"):
        fetcher.fetch("http://literature.example/paper")


def test_restricted_fetcher_rejects_dns_and_redirect_policy_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = RestrictedCitationFetcher(("literature.example",))
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [],
    )
    with pytest.raises(NetworkPolicyError, match="no addresses"):
        fetcher.resolve_and_validate("literature.example", 80)
    monkeypatch.setattr(
        network.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("dns")),
    )
    with pytest.raises(NetworkPolicyError, match="DNS resolution failed"):
        fetcher.resolve_and_validate("literature.example", 80)
    with pytest.raises(NetworkPolicyError, match="allowlisted"):
        fetcher.validate_redirect("http://literature.example/a", "http://other.example/b")


def test_pinned_connections_use_resolved_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        network.socket,
        "create_connection",
        lambda address, timeout: (address, timeout),
    )
    http_connection = network._PinnedHTTPConnection(
        "literature.example", 80, "203.0.113.10", timeout=1
    )
    http_connection.connect()
    assert http_connection.sock == (("203.0.113.10", 80), 1)

    class _Context:
        def wrap_socket(self, raw: object, *, server_hostname: str) -> tuple[object, str]:
            return raw, server_hostname

    monkeypatch.setattr(network.ssl, "create_default_context", lambda: _Context())
    https_connection = network._PinnedHTTPSConnection(
        "literature.example", 443, "203.0.113.10", timeout=1
    )
    https_connection.connect()
    assert https_connection.sock == ((("203.0.113.10", 443), 1), "literature.example")
