from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .models import CapabilityRequest, RunRequest, RuntimeCapabilities, RuntimeEvent, TurnRef


class CodexRuntime(Protocol):
    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities: ...

    def preflight(self, request: RunRequest) -> None: ...

    def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]: ...

    async def interrupt(self, turn: TurnRef) -> None: ...

    async def close(self) -> None: ...
