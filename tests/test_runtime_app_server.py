from __future__ import annotations

import asyncio
from typing import Any

import pytest

from qed.runtime import (
    AppServerRuntime,
    CapabilityRequest,
    ForkThread,
    FreshThread,
    ItemCompleted,
    RestrictedNetworkPolicy,
    ResumeThread,
    RunRequest,
    RuntimeBackend,
    RuntimeEvent,
    ThreadStarted,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
    UnknownNotification,
    WorkRole,
)


class FakeNotificationStream:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self._values = iter(values)
        self.closed = False

    def __aiter__(self) -> FakeNotificationStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self.closed:
            raise StopAsyncIteration
        try:
            return next(self._values)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any] | BaseException]],
        notifications: list[dict[str, Any]] | None = None,
    ) -> None:
        self.responses = {method: list(pages) for method, pages in responses.items()}
        self.notification_values = notifications or []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications_sent: list[tuple[str, dict[str, Any]]] = []
        self.notification_streams: list[FakeNotificationStream] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        response = self.responses[method].pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifications_sent.append((method, params))

    def notifications(self) -> FakeNotificationStream:
        stream = FakeNotificationStream(self.notification_values)
        self.notification_streams.append(stream)
        return stream

    async def close(self) -> None:
        return None


async def test_probe_pages_models_and_features_without_reordering_efforts() -> None:
    transport = FakeTransport(
        {
            "model/list": [
                {
                    "data": [
                        {
                            "model": "gpt-other",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "medium"}
                            ],
                            "defaultReasoningEffort": "medium",
                        }
                    ],
                    "nextCursor": "models-2",
                },
                {
                    "data": [
                        {
                            "model": "gpt-5.6-sol",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": effort}
                                for effort in ("low", "high", "max", "ultra")
                            ],
                            "defaultReasoningEffort": "low",
                        }
                    ],
                    "nextCursor": None,
                },
            ],
            "experimentalFeature/list": [
                {
                    "data": [
                        {
                            "name": "unrelated",
                            "enabled": True,
                            "defaultEnabled": True,
                            "stage": "stable",
                        }
                    ],
                    "nextCursor": "features-2",
                },
                {
                    "data": [
                        {
                            "name": "multi_agent",
                            "enabled": True,
                            "defaultEnabled": True,
                            "stage": "stable",
                        }
                    ],
                    "nextCursor": None,
                },
            ],
        }
    )
    runtime = AppServerRuntime(transport)

    capability = await runtime.probe(
        CapabilityRequest(model="gpt-5.6-sol", proactive=True)
    )

    assert capability.advertised_efforts == ("low", "high", "max", "ultra")
    assert capability.selected_effort == "ultra"
    assert capability.multi_agent is True
    assert transport.requests == [
        ("model/list", {"cursor": None, "limit": 100}),
        ("model/list", {"cursor": "models-2", "limit": 100}),
        ("experimentalFeature/list", {"cursor": None, "limit": 100}),
        ("experimentalFeature/list", {"cursor": "features-2", "limit": 100}),
    ]


async def test_stream_closes_notifications_when_thread_start_fails() -> None:
    transport = FakeTransport({"thread/start": [RuntimeError("thread failed")]})
    runtime = AppServerRuntime(transport)
    request = RunRequest(
        model="gpt-5.6-sol",
        effort="high",
        prompt="Return the verdict.",
        output_schema={"type": "object", "additionalProperties": False},
    )

    with pytest.raises(RuntimeError, match="thread failed"):
        await anext(runtime.stream(request))

    assert transport.notification_streams[0].closed


async def test_stream_closes_notifications_when_turn_start_fails() -> None:
    transport = FakeTransport(
        {
            "thread/start": [{"thread": {"id": "thread-1"}}],
            "turn/start": [RuntimeError("turn failed")],
        }
    )
    runtime = AppServerRuntime(transport)
    request = RunRequest(
        model="gpt-5.6-sol",
        effort="high",
        prompt="Return the verdict.",
        output_schema={"type": "object", "additionalProperties": False},
    )
    events = runtime.stream(request)

    assert await anext(events) == ThreadStarted(
        thread_id="thread-1", backend=RuntimeBackend.APP_SERVER
    )
    with pytest.raises(RuntimeError, match="turn failed"):
        await anext(events)

    assert transport.notification_streams[0].closed


@pytest.mark.parametrize(
    ("target", "method", "thread_param"),
    [
        (FreshThread(), "thread/start", None),
        (ResumeThread(thread_id="source-thread"), "thread/resume", "source-thread"),
        (ForkThread(thread_id="source-thread"), "thread/fork", "source-thread"),
    ],
)
async def test_stream_enforces_controls_and_maps_terminal_output(
    target: FreshThread | ResumeThread | ForkThread,
    method: str,
    thread_param: str | None,
) -> None:
    transport = FakeTransport(
        {
            method: [{"thread": {"id": "thread-1"}}],
            "turn/start": [{"turn": {"id": "turn-1"}}],
        },
        notifications=[
            {
                "method": "turn/started",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
            },
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
                            "inputTokens": 13,
                            "cachedInputTokens": 2,
                            "outputTokens": 5,
                            "reasoningOutputTokens": 3,
                            "totalTokens": 18,
                        }
                    },
                },
            },
            {"method": "future/event", "params": {"value": 1}},
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            },
        ],
    )
    runtime = AppServerRuntime(transport)
    request = RunRequest(
        model="gpt-5.6-sol",
        effort="max",
        prompt="Return the verdict.",
        output_schema={"type": "object", "additionalProperties": False},
        thread=target,
    )

    events = [event async for event in runtime.stream(request)]

    assert events[0] == ThreadStarted(
        thread_id="thread-1", backend=RuntimeBackend.APP_SERVER
    )
    assert TurnStarted(
        turn=TurnRef(
            thread_id="thread-1", turn_id="turn-1", backend=RuntimeBackend.APP_SERVER
        )
    ) in events
    assert ItemCompleted(
        thread_id="thread-1",
        turn_id="turn-1",
        item_id="item-1",
        item_type="agentMessage",
        payload={
            "id": "item-1",
            "type": "agentMessage",
            "text": '{"verdict":"PASS"}',
            "phase": "final_answer",
        },
    ) in events
    usage = next(event for event in events if isinstance(event, TokenUsageUpdated))
    assert usage.usage.cached_input_tokens == 2
    assert usage.usage.reasoning_output_tokens == 3
    assert UnknownNotification(method="future/event", payload={"value": 1}) in events
    assert events[-1] == TurnCompleted(
        turn=TurnRef(
            thread_id="thread-1", turn_id="turn-1", backend=RuntimeBackend.APP_SERVER
        ),
        status="completed",
        output='{"verdict":"PASS"}',
    )

    lifecycle_method, lifecycle_params = transport.requests[0]
    assert lifecycle_method == method
    assert lifecycle_params["approvalPolicy"] == "never"
    assert lifecycle_params["sandbox"] == "read-only"
    assert lifecycle_params["model"] == "gpt-5.6-sol"
    if thread_param is not None:
        assert lifecycle_params["threadId"] == thread_param
    turn_method, turn_params = transport.requests[1]
    assert turn_method == "turn/start"
    assert turn_params["approvalPolicy"] == "never"
    assert turn_params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn_params["outputSchema"] == request.output_schema
    assert turn_params["effort"] == "max"
    assert "multiAgentMode" not in repr(transport.requests)


async def test_interrupt_addresses_the_exact_app_server_turn() -> None:
    transport = FakeTransport({"turn/interrupt": [{}]})
    runtime = AppServerRuntime(transport)
    turn = TurnRef(
        thread_id="thread-1", turn_id="turn-1", backend=RuntimeBackend.APP_SERVER
    )

    await runtime.interrupt(turn)

    assert transport.requests == [
        ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"})
    ]


async def test_concurrent_streams_ignore_other_turn_lifecycle_events() -> None:
    notifications = [
        {
            "method": "turn/started",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        },
        {
            "method": "turn/started",
            "params": {"threadId": "thread-2", "turn": {"id": "turn-2"}},
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-2",
                "turnId": "turn-2",
                "item": {
                    "id": "item-2",
                    "type": "agentMessage",
                    "text": '{"stream":2}',
                    "phase": "final_answer",
                },
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "item-1",
                    "type": "agentMessage",
                    "text": '{"stream":1}',
                    "phase": "final_answer",
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-2",
                "turn": {"id": "turn-2", "status": "completed"},
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
    transport = FakeTransport(
        {
            "thread/start": [
                {"thread": {"id": "thread-1"}},
                {"thread": {"id": "thread-2"}},
            ],
            "turn/start": [
                {"turn": {"id": "turn-1"}},
                {"turn": {"id": "turn-2"}},
            ],
        },
        notifications=notifications,
    )
    runtime = AppServerRuntime(transport)

    async def collect(label: int) -> list[RuntimeEvent]:
        request = RunRequest(
            model="gpt-5.6-sol",
            effort="high",
            prompt=f"Return stream {label}.",
            output_schema={"type": "object", "additionalProperties": False},
        )
        return [event async for event in runtime.stream(request)]

    first, second = await asyncio.gather(collect(1), collect(2))

    assert first[-1] == TurnCompleted(
        turn=TurnRef(
            thread_id="thread-1",
            turn_id="turn-1",
            backend=RuntimeBackend.APP_SERVER,
        ),
        status="completed",
        output='{"stream":1}',
    )
    assert second[-1] == TurnCompleted(
        turn=TurnRef(
            thread_id="thread-2",
            turn_id="turn-2",
            backend=RuntimeBackend.APP_SERVER,
        ),
        status="completed",
        output='{"stream":2}',
    )


async def test_explicit_network_policy_builds_allowlist_first_config() -> None:
    transport = FakeTransport(
        {
            "thread/start": [{"thread": {"id": "thread-1"}}],
            "turn/start": [{"turn": {"id": "turn-1"}}],
        },
        notifications=[
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "item-1",
                        "type": "agentMessage",
                        "text": "{}",
                        "phase": "final_answer",
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        ],
    )
    runtime = AppServerRuntime(transport)
    request = RunRequest(
        model="gpt-5.6-sol",
        effort="high",
        prompt="Fetch the citation metadata.",
        output_schema={"type": "object", "additionalProperties": False},
        role=WorkRole.CITATION,
        command_network=RestrictedNetworkPolicy(domains=("api.crossref.org",)),
    )

    _ = [event async for event in runtime.stream(request)]

    thread_config = transport.requests[0][1]["config"]
    assert thread_config["features"]["network_proxy"] == {
        "enabled": True,
        "mode": "limited",
        "domains": {"api.crossref.org": "allow"},
        "allow_local_binding": False,
        "allow_upstream_proxy": False,
    }
    assert transport.requests[1][1]["sandboxPolicy"]["networkAccess"] is True
