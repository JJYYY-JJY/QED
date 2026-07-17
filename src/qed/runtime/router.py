from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from .app_server import AppServerRuntime
from .exec import ExecRuntime
from .models import (
    CapabilityRequest,
    RunRequest,
    RuntimeBackend,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimePreference,
    TurnRef,
)
from .sdk import SdkRuntime
from .stdio import StdioAppServerTransport, resolve_codex_executable


class _CapabilityRuntime(Protocol):
    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities: ...


class _TurnRuntime(Protocol):
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
    ) -> None:
        self._capability_runtime = capability_runtime
        self._sdk = sdk
        self._app_server = app_server
        self._exec = exec_runtime

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        return await self._capability_runtime.probe(request)

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
        runtime = self._select(resolved)
        async for event in runtime.stream(resolved):
            yield event

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


def create_codex_runtime(executable: str | Path | None = None) -> RoutedCodexRuntime:
    resolved = resolve_codex_executable(executable)
    app_server = AppServerRuntime(StdioAppServerTransport(resolved))
    sdk = SdkRuntime(capability_runtime=app_server)
    exec_runtime = ExecRuntime(resolved, capability_runtime=app_server)
    return RoutedCodexRuntime(app_server, sdk, app_server, exec_runtime)
