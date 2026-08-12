from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, cast

from .app_server import AppServerRuntime
from .exec import ExecRuntime
from .isolation import prepare_codex_home
from .models import (
    CapabilityRequest,
    FreshThread,
    RunRequest,
    RuntimeBackend,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimePreference,
    TurnRef,
)
from .sdk import SdkRuntime
from .stdio import (
    StdioAppServerTransport,
    probe_codex_version,
    resolve_codex_executable,
)


class _CapabilityRuntime(Protocol):
    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities: ...


class _ClosableEventStream(Protocol):
    def __aiter__(self) -> AsyncIterator[RuntimeEvent]: ...

    async def __anext__(self) -> RuntimeEvent: ...

    async def aclose(self) -> None: ...


class _TurnRuntime(Protocol):
    def preflight(self, request: RunRequest) -> None: ...

    def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]: ...

    async def interrupt(self, turn: TurnRef) -> None: ...

    async def close(self) -> None: ...


class _SelectableRuntime(_TurnRuntime, Protocol):
    def supports(self, request: RunRequest) -> bool: ...


class RoutedCodexRuntime:
    """Capability-resolving runtime that keeps exec fallback explicit."""

    def __init__(
        self,
        capability_runtime: _CapabilityRuntime,
        sdk: _SelectableRuntime,
        app_server: _TurnRuntime,
        exec_runtime: _SelectableRuntime,
        *,
        executable: Path,
        runtime_version: str,
    ) -> None:
        self._capability_runtime = capability_runtime
        self._sdk = sdk
        self._app_server = app_server
        self._exec = exec_runtime
        self._executable = executable
        self.runtime_version = runtime_version

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        return await self._capability_runtime.probe(request)

    def runtime_resolution(
        self,
        capabilities: RuntimeCapabilities,
        *,
        requested_effort: str,
        config_sha256: str,
        prompt_sha256: str,
        schema_sha256: str,
        requested_backend: RuntimePreference | None = None,
    ) -> dict[str, object]:
        """Return the immutable, observed identity bound before model work."""

        if capabilities.model_version is None or capabilities.model_catalog_sha256 is None:
            raise RuntimeError("Codex capability probe did not return exact model provenance")
        if capabilities.capability_response_sha256 is None:
            raise RuntimeError("Codex capability probe did not return a response hash")
        try:
            sdk_version = importlib.metadata.version("openai-codex")
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError("official openai-codex package version is unavailable") from error
        selected_backend = capabilities.backend
        if requested_backend is not None:
            if requested_backend is RuntimePreference.SDK:
                selected_backend = RuntimeBackend.SDK
            elif requested_backend is RuntimePreference.APP_SERVER:
                selected_backend = RuntimeBackend.APP_SERVER
            elif requested_backend is RuntimePreference.EXEC:
                selected_backend = RuntimeBackend.EXEC
            else:
                probe_request = RunRequest(
                    model=capabilities.model,
                    effort=capabilities.selected_effort,
                    prompt="runtime backend capability probe",
                    output_schema={"type": "object"},
                    thread=FreshThread(),
                    cwd=Path("/"),
                )
                try:
                    sdk_available = self._sdk.supports(probe_request)
                except Exception as error:
                    raise RuntimeError("Codex SDK backend capability check failed") from error
                selected_backend = (
                    RuntimeBackend.SDK if sdk_available else RuntimeBackend.APP_SERVER
                )
        if selected_backend is None:
            raise RuntimeError("Codex runtime did not resolve a selected backend")
        return {
            "schema_version": 1,
            "model_provider": "OpenAI",
            "model": capabilities.model,
            "model_version": capabilities.model_version,
            "backend": selected_backend.value,
            "codex_runtime_version": self.runtime_version,
            "codex_cli_version": self.runtime_version,
            "sdk_version": sdk_version,
            "app_server_version": self.runtime_version,
            "requested_effort": requested_effort,
            "selected_effort": capabilities.selected_effort,
            "model_catalog_sha256": capabilities.model_catalog_sha256,
            "config_sha256": config_sha256,
            "prompt_sha256": prompt_sha256,
            "schema_sha256": schema_sha256,
            "executable_sha256": hashlib.sha256(self._executable.read_bytes()).hexdigest(),
            "capability_response_sha256": capabilities.capability_response_sha256,
            "protocol_version": "qed-codex-protocol-v1",
        }

    def preflight(self, request: RunRequest) -> None:
        if request.effort == "auto":
            raise ValueError("runtime preflight requires capability-resolved effort")
        runtime = self._select(request)
        selected_preflight = getattr(runtime, "preflight", None)
        if selected_preflight is not None:
            selected_preflight(request)

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        resolved = request
        if request.effort == "auto":
            capability = await self.probe(
                CapabilityRequest(
                    model=request.model,
                    effort=request.effort,
                    proactive=request.proactive,
                )
            )
            resolved = request.model_copy(update={"effort": capability.selected_effort})
        self.preflight(resolved)
        runtime = self._select(resolved)
        events = cast(_ClosableEventStream, runtime.stream(resolved))
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    def _select(self, request: RunRequest) -> _TurnRuntime:
        if request.runtime is RuntimePreference.AUTO:
            return self._sdk if self._sdk.supports(request) else self._app_server
        if request.runtime is RuntimePreference.SDK:
            if not self._sdk.supports(request):
                raise ValueError("requested controls are not representable by the published SDK")
            return self._sdk
        if request.runtime is RuntimePreference.APP_SERVER:
            return self._app_server
        if not self._exec.supports(request):
            raise ValueError("requested controls are not representable by codex exec fallback")
        return self._exec

    async def interrupt(self, turn: TurnRef) -> None:
        if turn.backend is RuntimeBackend.SDK:
            await self._sdk.interrupt(turn)
        elif turn.backend is RuntimeBackend.APP_SERVER:
            await self._app_server.interrupt(turn)
        elif turn.backend is RuntimeBackend.EXEC:
            await self._exec.interrupt(turn)
        else:
            raise ValueError(f"cannot route interruption for backend {turn.backend}")

    async def close(self) -> None:
        await asyncio.gather(
            self._sdk.close(),
            self._app_server.close(),
            self._exec.close(),
        )


def create_codex_runtime(
    codex_home: Path,
    executable: str | Path | None = None,
) -> RoutedCodexRuntime:
    server_codex_home = prepare_codex_home(codex_home)
    resolved = resolve_codex_executable(executable)
    runtime_version = probe_codex_version(resolved)
    app_server = AppServerRuntime(
        StdioAppServerTransport(resolved, codex_home=server_codex_home)
    )
    sdk = SdkRuntime(
        capability_runtime=app_server,
        executable=resolved,
        codex_home=server_codex_home,
    )
    exec_runtime = ExecRuntime(
        resolved,
        capability_runtime=app_server,
        codex_home=server_codex_home,
    )
    return RoutedCodexRuntime(
        app_server,
        sdk,
        app_server,
        exec_runtime,
        executable=resolved,
        runtime_version=runtime_version,
    )
