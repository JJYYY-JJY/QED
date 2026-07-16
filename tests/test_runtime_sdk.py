from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

import pytest

from qed.runtime import (
    ForkThread,
    FreshThread,
    RestrictedNetworkPolicy,
    ResumeThread,
    RunRequest,
    RuntimeBackend,
    SandboxMode,
    SdkRuntime,
    ThreadStarted,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
    WebSearchMode,
    WorkRole,
)


class FakeHandle:
    def __init__(self, notifications: list[dict[str, Any]]) -> None:
        self.thread_id = "thread-1"
        self.id = "turn-1"
        self._notifications = notifications
        self.interrupted = False

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        for notification in self._notifications:
            yield notification

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeThread:
    def __init__(self, handle: FakeHandle) -> None:
        self.id = "thread-1"
        self.handle = handle
        self.turn_calls: list[tuple[str, dict[str, Any]]] = []

    async def turn(self, prompt: str, **kwargs: Any) -> FakeHandle:
        self.turn_calls.append((prompt, kwargs))
        return self.handle


class FakeSdkClient:
    def __init__(self, thread: FakeThread) -> None:
        self.thread = thread
        self.calls: list[tuple[str, str | None, dict[str, Any]]] = []
        self.closed = False

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self.calls.append(("thread_start", None, kwargs))
        return self.thread

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.calls.append(("thread_resume", thread_id, kwargs))
        return self.thread

    async def thread_fork(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.calls.append(("thread_fork", thread_id, kwargs))
        return self.thread

    async def close(self) -> None:
        self.closed = True


def _request(**overrides: object) -> RunRequest:
    values: dict[str, object] = {
        "model": "gpt-5.6-sol",
        "effort": "high",
        "prompt": "Return a verdict.",
        "output_schema": {"type": "object", "additionalProperties": False},
    }
    values.update(overrides)
    return RunRequest.model_validate(values)


def test_sdk_support_is_limited_to_published_typed_controls() -> None:
    runtime = SdkRuntime(FakeSdkClient(FakeThread(FakeHandle([]))))

    assert runtime.supports(_request()) is True
    assert runtime.supports(_request(effort="max")) is False
    assert (
        runtime.supports(
            _request(web_search=WebSearchMode.INDEXED, role=WorkRole.LITERATURE)
        )
        is False
    )
    assert (
        runtime.supports(
            _request(
                role=WorkRole.LITERATURE,
                command_network=RestrictedNetworkPolicy(domains=("api.crossref.org",)),
            )
        )
        is False
    )


@pytest.mark.parametrize(
    ("target", "expected_method", "expected_source"),
    [
        (FreshThread(), "thread_start", None),
        (ResumeThread(thread_id="source"), "thread_resume", "source"),
        (ForkThread(thread_id="source"), "thread_fork", "source"),
    ],
)
async def test_sdk_stream_enforces_safety_and_maps_usage_and_output(
    target: FreshThread | ResumeThread | ForkThread,
    expected_method: str,
    expected_source: str | None,
) -> None:
    handle = FakeHandle(
        [
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "item-1",
                        "type": "agentMessage",
                        "text": '{"verdict":"PASS"}',
                        "phase": "final_answer",
                    },
                },
            },
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tokenUsage": {
                        "last": {
                            "inputTokens": 8,
                            "cachedInputTokens": 1,
                            "outputTokens": 3,
                            "reasoningOutputTokens": 2,
                        }
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            },
        ]
    )
    thread = FakeThread(handle)
    client = FakeSdkClient(thread)
    runtime = SdkRuntime(client)
    request = _request(thread=target, sandbox=SandboxMode.READ_ONLY)

    events = [event async for event in runtime.stream(request)]

    assert events[0] == ThreadStarted(thread_id="thread-1", backend=RuntimeBackend.SDK)
    turn = TurnRef(thread_id="thread-1", turn_id="turn-1", backend=RuntimeBackend.SDK)
    assert events[1] == TurnStarted(turn=turn)
    assert any(isinstance(event, TokenUsageUpdated) for event in events)
    assert events[-1] == TurnCompleted(
        turn=turn, status="completed", output='{"verdict":"PASS"}'
    )
    method, source, lifecycle = client.calls[0]
    assert (method, source) == (expected_method, expected_source)
    assert lifecycle["approval_mode"].value == "deny_all"
    assert lifecycle["sandbox"].value == "read-only"
    assert lifecycle["config"] == {"web_search": "disabled"}
    prompt, turn_kwargs = thread.turn_calls[0]
    assert prompt == request.prompt
    assert turn_kwargs["approval_mode"].value == "deny_all"
    assert turn_kwargs["sandbox"].value == "read-only"
    assert turn_kwargs["effort"].value == "high"
    assert turn_kwargs["output_schema"] == request.output_schema


async def test_sdk_interrupt_uses_the_live_handle() -> None:
    handle = FakeHandle(
        [
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        ]
    )
    runtime = SdkRuntime(FakeSdkClient(FakeThread(handle)))
    request = _request()
    stream = cast(AsyncGenerator[Any], runtime.stream(request))
    assert await anext(stream) == ThreadStarted(
        thread_id="thread-1", backend=RuntimeBackend.SDK
    )
    assert await anext(stream) == TurnStarted(
        turn=TurnRef(thread_id="thread-1", turn_id="turn-1", backend=RuntimeBackend.SDK)
    )

    await runtime.interrupt(
        TurnRef(thread_id="thread-1", turn_id="turn-1", backend=RuntimeBackend.SDK)
    )

    assert handle.interrupted is True
    await stream.aclose()
