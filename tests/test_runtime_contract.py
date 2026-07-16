from __future__ import annotations

from qed.runtime import (
    MockRuntime,
    RunRequest,
    RuntimeBackend,
    RuntimeEvent,
    ThreadStarted,
    TokenUsage,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
)


async def test_mock_runtime_streams_the_public_event_contract() -> None:
    turn = TurnRef(thread_id="thread-1", turn_id="turn-1", backend=RuntimeBackend.MOCK)
    expected: list[RuntimeEvent] = [
        ThreadStarted(thread_id="thread-1", backend=RuntimeBackend.MOCK),
        TurnStarted(turn=turn),
        TokenUsageUpdated(
            thread_id="thread-1",
            turn_id="turn-1",
            usage=TokenUsage(input_tokens=13, output_tokens=5),
        ),
        TurnCompleted(turn=turn, status="completed", output='{"verdict":"PASS"}'),
    ]
    runtime = MockRuntime(events=expected)
    request = RunRequest(
        model="gpt-5.6-sol",
        effort="high",
        prompt="Verify the frozen candidate.",
        output_schema={
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
            "additionalProperties": False,
        },
    )

    observed = [event async for event in runtime.stream(request)]

    assert observed == expected
