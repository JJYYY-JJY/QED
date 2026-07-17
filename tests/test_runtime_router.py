from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

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
    def __init__(self) -> None:
        self.requests: list[CapabilityRequest] = []

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        self.requests.append(request)
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
        self.stream_closed = False

    def supports(self, request: RunRequest) -> bool:
        return self.supported

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        try:
            yield ThreadStarted(thread_id=f"{self.backend}-thread", backend=self.backend)
        finally:
            self.stream_closed = True

    async def interrupt(self, turn: TurnRef) -> None:
        self.interruptions.append(turn)

    async def close(self) -> None:
        self.closed = True


def _request(
    runtime: RuntimePreference = RuntimePreference.AUTO,
    *,
    effort: str = "auto",
) -> RunRequest:
    return RunRequest(
        model="gpt-5.6-sol",
        effort=effort,
        proactive=True,
        prompt="Return a verdict.",
        output_schema={"type": "object", "additionalProperties": False},
        runtime=runtime,
    )


async def test_explicit_effort_streams_never_reprobe_live_capabilities() -> None:
    capabilities = FakeCapabilityRuntime()
    sdk = FakeTurnRuntime(RuntimeBackend.SDK)
    app = FakeTurnRuntime(RuntimeBackend.APP_SERVER)
    exec_runtime = FakeTurnRuntime(RuntimeBackend.EXEC)
    runtime = RoutedCodexRuntime(
        capabilities, sdk, app, exec_runtime, runtime_version="test-codex/1"
    )
    request = _request(effort="high")

    for _ in range(2):
        _ = [event async for event in runtime.stream(request)]

    assert capabilities.requests == []
    assert [sent.effort for sent in sdk.requests] == ["high", "high"]


async def test_auto_routes_resolved_controls_to_app_server_when_sdk_cannot() -> None:
    capabilities = FakeCapabilityRuntime()
    sdk = FakeTurnRuntime(RuntimeBackend.SDK, supported=False)
    app = FakeTurnRuntime(RuntimeBackend.APP_SERVER)
    exec_runtime = FakeTurnRuntime(RuntimeBackend.EXEC)
    runtime = RoutedCodexRuntime(
        capabilities, sdk, app, exec_runtime, runtime_version="test-codex/1"
    )

    events = [event async for event in runtime.stream(_request())]

    assert events == [
        ThreadStarted(thread_id="app_server-thread", backend=RuntimeBackend.APP_SERVER)
    ]
    assert app.requests[0].effort == "max"
    assert capabilities.requests == [
        CapabilityRequest(model="gpt-5.6-sol", effort="auto", proactive=True)
    ]
    assert sdk.requests == []
    assert exec_runtime.requests == []


async def test_exec_is_only_selected_explicitly() -> None:
    sdk = FakeTurnRuntime(RuntimeBackend.SDK, supported=True)
    app = FakeTurnRuntime(RuntimeBackend.APP_SERVER)
    exec_runtime = FakeTurnRuntime(RuntimeBackend.EXEC)
    runtime = RoutedCodexRuntime(
        FakeCapabilityRuntime(),
        sdk,
        app,
        exec_runtime,
        runtime_version="test-codex/1",
    )

    _ = [
        event
        async for event in runtime.stream(_request(runtime=RuntimePreference.EXEC))
    ]

    assert len(exec_runtime.requests) == 1
    assert sdk.requests == []
    assert app.requests == []


def test_preflight_rejects_unrepresentable_explicit_backend_before_stream() -> None:
    runtime = RoutedCodexRuntime(
        FakeCapabilityRuntime(),
        FakeTurnRuntime(RuntimeBackend.SDK),
        FakeTurnRuntime(RuntimeBackend.APP_SERVER),
        FakeTurnRuntime(RuntimeBackend.EXEC, supported=False),
        runtime_version="test-codex/1",
    )

    with pytest.raises(ValueError, match="exec fallback"):
        runtime.preflight(
            _request(runtime=RuntimePreference.EXEC, effort="high")
        )


async def test_closing_routed_stream_closes_selected_runtime_stream() -> None:
    sdk = FakeTurnRuntime(RuntimeBackend.SDK)
    app = FakeTurnRuntime(RuntimeBackend.APP_SERVER)
    exec_runtime = FakeTurnRuntime(RuntimeBackend.EXEC)
    runtime = RoutedCodexRuntime(
        FakeCapabilityRuntime(),
        sdk,
        app,
        exec_runtime,
        runtime_version="test-codex/1",
    )
    events = runtime.stream(
        _request(runtime=RuntimePreference.APP_SERVER, effort="high")
    )

    assert await anext(events) == ThreadStarted(
        thread_id="app_server-thread",
        backend=RuntimeBackend.APP_SERVER,
    )
    assert not app.stream_closed

    await events.aclose()

    assert app.stream_closed
