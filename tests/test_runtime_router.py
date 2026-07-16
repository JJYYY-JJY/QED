from __future__ import annotations

from collections.abc import AsyncIterator

from qed.runtime import (
    CapabilityRequest,
    RoutedCodexRuntime,
    RunRequest,
    RuntimeBackend,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimePreference,
    ThreadStarted,
    TurnRef,
)


class FakeCapabilityRuntime:
    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            model=request.model,
            advertised_efforts=("low", "high", "max"),
            default_effort="low",
            selected_effort="max" if request.proactive else "low",
            multi_agent=True,
            proactive_multi_agent=request.proactive,
        )


class FakeTurnRuntime:
    def __init__(self, backend: RuntimeBackend, *, supported: bool = True) -> None:
        self.backend = backend
        self.supported = supported
        self.requests: list[RunRequest] = []
        self.interruptions: list[TurnRef] = []
        self.closed = False

    def supports(self, request: RunRequest) -> bool:
        return self.supported

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        yield ThreadStarted(thread_id=f"{self.backend}-thread", backend=self.backend)

    async def interrupt(self, turn: TurnRef) -> None:
        self.interruptions.append(turn)

    async def close(self) -> None:
        self.closed = True


def _request(runtime: RuntimePreference = RuntimePreference.AUTO) -> RunRequest:
    return RunRequest(
        model="gpt-5.6-sol",
        effort="auto",
        proactive=True,
        prompt="Return a verdict.",
        output_schema={"type": "object", "additionalProperties": False},
        runtime=runtime,
    )


async def test_auto_routes_resolved_controls_to_app_server_when_sdk_cannot() -> None:
    sdk = FakeTurnRuntime(RuntimeBackend.SDK, supported=False)
    app = FakeTurnRuntime(RuntimeBackend.APP_SERVER)
    exec_runtime = FakeTurnRuntime(RuntimeBackend.EXEC)
    runtime = RoutedCodexRuntime(FakeCapabilityRuntime(), sdk, app, exec_runtime)

    events = [event async for event in runtime.stream(_request())]

    assert events == [
        ThreadStarted(thread_id="app_server-thread", backend=RuntimeBackend.APP_SERVER)
    ]
    assert app.requests[0].effort == "max"
    assert sdk.requests == []
    assert exec_runtime.requests == []


async def test_exec_is_only_selected_explicitly() -> None:
    sdk = FakeTurnRuntime(RuntimeBackend.SDK, supported=True)
    app = FakeTurnRuntime(RuntimeBackend.APP_SERVER)
    exec_runtime = FakeTurnRuntime(RuntimeBackend.EXEC)
    runtime = RoutedCodexRuntime(FakeCapabilityRuntime(), sdk, app, exec_runtime)

    _ = [
        event
        async for event in runtime.stream(_request(runtime=RuntimePreference.EXEC))
    ]

    assert len(exec_runtime.requests) == 1
    assert sdk.requests == []
    assert app.requests == []
