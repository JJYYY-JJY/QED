from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qed.runtime import (
    ExecRuntime,
    ForkThread,
    RunRequest,
    RuntimeBackend,
    RuntimeProtocolError,
    ThreadStarted,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
    WebSearchMode,
    build_exec_argv,
)
from qed.runtime.isolation import server_config_overrides

_CODEX_HOME = Path("/var/lib/qed/codex-home")


def _request(**overrides: object) -> RunRequest:
    values: dict[str, object] = {
        "model": "gpt-5.6-sol",
        "effort": "high",
        "prompt": "Return a verdict.",
        "output_schema": {"type": "object", "additionalProperties": False},
        "cwd": Path("/workspace"),
    }
    values.update(overrides)
    return RunRequest.model_validate(values)


def test_exec_argv_is_strict_read_only_structured_and_has_no_escape_flags() -> None:
    argv = build_exec_argv(
        Path("/opt/codex/bin/codex"),
        _request(),
        Path("/var/lib/qed/output-schema.json"),
    )

    assert argv[:2] == ("/opt/codex/bin/codex", "exec")
    for required in (
        "--strict-config",
        "--ignore-user-config",
        "--json",
        "--sandbox",
        "read-only",
        "--output-schema",
        "/var/lib/qed/output-schema.json",
    ):
        assert required in argv
    for override in server_config_overrides(WebSearchMode.DISABLED):
        assert override in argv
    rendered = " ".join(argv)
    assert "danger" not in rendered
    assert "bypass" not in rendered
    assert "full-auto" not in rendered
    assert "skip-git" not in rendered


def test_exec_prompt_cannot_be_reinterpreted_as_a_cli_option() -> None:
    prompt = "--dangerously-bypass-approvals-and-sandbox"
    argv = build_exec_argv(
        Path("/opt/codex/bin/codex"),
        _request(prompt=prompt),
        Path("/var/lib/qed/output-schema.json"),
    )

    assert argv[-2:] == ("--", prompt)


def test_exec_fallback_rejects_controls_it_cannot_safely_represent() -> None:
    runtime = ExecRuntime(
        Path("/opt/codex/bin/codex"), codex_home=_CODEX_HOME
    )

    assert runtime.supports(_request()) is True
    assert runtime.supports(_request(thread=ForkThread(thread_id="source"))) is False


class FakeReader:
    def __init__(self, lines: list[dict[str, object]] | None = None) -> None:
        self.lines = [json.dumps(line).encode() + b"\n" for line in (lines or [])]

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class FakeProcess:
    def __init__(self, lines: list[dict[str, object]], returncode: int = 0) -> None:
        self.stdout = FakeReader(lines)
        self.stderr = FakeReader()
        self.returncode: int | None = returncode
        self.terminated = False

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class QueueReader:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()
        self.read_count = 0

    async def readline(self) -> bytes:
        self.read_count += 1
        return await self.lines.get()


class StubbornProcess:
    def __init__(self) -> None:
        self.stdout = QueueReader()
        self.stderr = QueueReader()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._finished = asyncio.Event()
        for event in (
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
        ):
            self.stdout.lines.put_nowait(json.dumps(event).encode() + b"\n")
        self.stderr.lines.put_nowait(b"diagnostic output\n")

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.lines.put_nowait(b"")
        self.stderr.lines.put_nowait(b"")
        self._finished.set()


async def test_exec_fallback_maps_jsonl_and_usage() -> None:
    process = FakeProcess(
        [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": '{"verdict":"PASS"}',
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 9,
                    "cached_input_tokens": 2,
                    "output_tokens": 4,
                    "reasoning_output_tokens": 3,
                },
            },
        ]
    )
    launches: list[tuple[tuple[str, ...], Path, Path]] = []
    captured_schema: list[dict[str, object]] = []

    async def spawn(
        argv: tuple[str, ...], cwd: Path, codex_home: Path
    ) -> FakeProcess:
        launches.append((argv, cwd, codex_home))
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        value = json.loads(await asyncio.to_thread(schema_path.read_text))
        assert isinstance(value, dict)
        captured_schema.append(value)
        return process

    runtime = ExecRuntime(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=spawn,
        turn_id_factory=lambda: "local-turn-1",
    )

    events = [event async for event in runtime.stream(_request())]

    turn = TurnRef(
        thread_id="thread-1", turn_id="local-turn-1", backend=RuntimeBackend.EXEC
    )
    assert events[0] == ThreadStarted(thread_id="thread-1", backend=RuntimeBackend.EXEC)
    assert events[1] == TurnStarted(turn=turn)
    assert any(isinstance(event, TokenUsageUpdated) for event in events)
    assert events[-1] == TurnCompleted(
        turn=turn, status="completed", output='{"verdict":"PASS"}'
    )
    assert launches[0][1] == Path("/workspace")
    assert launches[0][2] == _CODEX_HOME
    assert captured_schema == [_request().output_schema]


async def test_exec_fallback_fails_if_cli_exits_without_terminal_event() -> None:
    async def spawn(
        _argv: tuple[str, ...], _cwd: Path, _codex_home: Path
    ) -> FakeProcess:
        return FakeProcess([{"type": "thread.started", "thread_id": "thread-1"}])

    runtime = ExecRuntime(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=spawn,
    )

    with pytest.raises(RuntimeProtocolError, match="terminal"):
        _ = [event async for event in runtime.stream(_request())]


async def test_exec_interrupt_escalates_and_completes_stream_as_interrupted() -> None:
    process = StubbornProcess()

    async def spawn(
        _argv: tuple[str, ...], _cwd: Path, _codex_home: Path
    ) -> StubbornProcess:
        return process

    runtime = ExecRuntime(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=spawn,
        turn_id_factory=lambda: "local-turn-1",
        terminate_timeout_seconds=0.01,
    )
    stream = runtime.stream(_request())
    assert await anext(stream) == ThreadStarted(
        thread_id="thread-1", backend=RuntimeBackend.EXEC
    )
    started = await anext(stream)
    assert isinstance(started, TurnStarted)

    await runtime.interrupt(started.turn)
    remaining = [event async for event in stream]

    assert remaining == [
        TurnCompleted(turn=started.turn, status="interrupted", output=None)
    ]
    assert process.terminated is True
    assert process.killed is True
    assert process.stderr.read_count >= 1
