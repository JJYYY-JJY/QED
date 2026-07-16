from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from .models import CapabilityRequest, RunRequest, RuntimeCapabilities, RuntimeEvent, TurnRef


class MockRuntime:
    def __init__(
        self,
        events: Iterable[RuntimeEvent] = (),
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> None:
        self._events = tuple(events)
        self._capabilities = capabilities
        self.requests: list[RunRequest] = []
        self.probes: list[CapabilityRequest] = []
        self.interruptions: list[TurnRef] = []

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        self.probes.append(request)
        if self._capabilities is None:
            raise RuntimeError("MockRuntime has no configured capabilities")
        return self._capabilities

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        for event in self._events:
            yield event

    async def interrupt(self, turn: TurnRef) -> None:
        self.interruptions.append(turn)

    async def close(self) -> None:
        return None
