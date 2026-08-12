from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from pathlib import Path

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
    create_codex_runtime,
)
from qed.schemas import sha256_text


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
        cwd=Path("/var/lib/qed/turns/test"),
    )


_EXECUTABLE = Path("/opt/codex/bin/codex")


async def test_runtime_factory_creates_private_server_codex_home(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"

    runtime = create_codex_runtime(codex_home)
    try:
        assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700
        assert not (codex_home / "auth.json").exists()
    finally:
        await runtime.close()


async def test_explicit_effort_streams_never_reprobe_live_capabilities() -> None:
    capabilities = FakeCapabilityRuntime()
    sdk = FakeTurnRuntime(RuntimeBackend.SDK)
    app = FakeTurnRuntime(RuntimeBackend.APP_SERVER)
    exec_runtime = FakeTurnRuntime(RuntimeBackend.EXEC)
    runtime = RoutedCodexRuntime(
        capabilities,
        sdk,
        app,
        exec_runtime,
        executable=_EXECUTABLE,
        runtime_version="test-codex/1",
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
        capabilities,
        sdk,
        app,
        exec_runtime,
        executable=_EXECUTABLE,
        runtime_version="test-codex/1",
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
        executable=_EXECUTABLE,
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
        executable=_EXECUTABLE,
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
        executable=_EXECUTABLE,
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


def test_runtime_resolution_records_the_selected_backend(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codex"
    executable.write_bytes(b"official codex executable")
    capabilities = RuntimeCapabilities(
        model="gpt-5.6-sol",
        advertised_efforts=("low", "high"),
        default_effort="low",
        selected_effort="high",
        multi_agent=True,
        proactive_multi_agent=False,
        model_version="gpt-5.6-sol",
        backend=RuntimeBackend.APP_SERVER,
        model_catalog_sha256=sha256_text("catalog"),
        capability_response_sha256=sha256_text("capability"),
    )
    runtime = RoutedCodexRuntime(
        FakeCapabilityRuntime(),
        FakeTurnRuntime(RuntimeBackend.SDK),
        FakeTurnRuntime(RuntimeBackend.APP_SERVER),
        FakeTurnRuntime(RuntimeBackend.EXEC),
        executable=executable,
        runtime_version="test-codex/1",
    )

    resolution = runtime.runtime_resolution(
        capabilities,
        requested_effort="high",
        config_sha256=sha256_text("config"),
        prompt_sha256=sha256_text("prompt"),
        schema_sha256=sha256_text("schema"),
        requested_backend=RuntimePreference.APP_SERVER,
    )

    assert resolution["backend"] == "app_server"
    assert resolution["model"] == "gpt-5.6-sol"
