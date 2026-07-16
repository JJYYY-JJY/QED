from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from .app_server import RuntimeProtocolError
from .base import CodexRuntime
from .models import (
    CapabilityRequest,
    ForkThread,
    ItemCompleted,
    RunRequest,
    RuntimeBackend,
    RuntimeCapabilities,
    RuntimeErrorEvent,
    RuntimeEvent,
    SandboxMode,
    ThreadStarted,
    TokenUsage,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
    UnknownNotification,
)


class _Reader(Protocol):
    async def readline(self) -> bytes: ...


class _ExecProcess(Protocol):
    stdout: _Reader
    stderr: _Reader
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...


ExecProcessFactory = Callable[[tuple[str, ...], Path], Awaitable[Any]]


async def _spawn(argv: tuple[str, ...], cwd: Path) -> _ExecProcess:
    process = await asyncio.create_subprocess_exec(  # noqa: S603 - absolute argv, no shell
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.terminate()
        await process.wait()
        raise RuntimeError("failed to create codex exec output pipes")
    return cast(_ExecProcess, process)


def build_exec_argv(
    executable: Path,
    request: RunRequest,
    schema_path: Path,
) -> tuple[str, ...]:
    if not executable.is_absolute() or not schema_path.is_absolute():
        raise ValueError("codex exec and schema paths must be absolute")
    if request.effort == "auto":
        raise ValueError("codex exec requires a capability-resolved effort")
    if isinstance(request.thread, ForkThread):
        raise ValueError("codex exec cannot fork a thread")
    if request.sandbox is not SandboxMode.READ_ONLY or request.command_network is not None:
        raise ValueError("codex exec fallback is restricted to offline read-only turns")

    controls = (
        "-c",
        'approval_policy="never"',
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        f"model_reasoning_effort={json.dumps(request.effort)}",
        "-c",
        f"web_search={json.dumps(request.web_search.value)}",
    )
    common = (
        "--strict-config",
        "--ignore-user-config",
        "--json",
        "--model",
        request.model,
        "--output-schema",
        str(schema_path),
        *controls,
    )
    if request.thread.kind == "resume":
        return (
            str(executable),
            "exec",
            "resume",
            *common,
            "--",
            request.thread.thread_id,
            request.prompt,
        )
    cwd = request.cwd if request.cwd is not None else Path.cwd()
    return (
        str(executable),
        "exec",
        *common,
        "--sandbox",
        "read-only",
        "--cd",
        str(cwd),
        "--",
        request.prompt,
    )


class ExecRuntime:
    """Explicit, fail-closed fallback over ``codex exec --json``."""

    def __init__(
        self,
        executable: Path,
        *,
        process_factory: ExecProcessFactory = _spawn,
        turn_id_factory: Callable[[], str] = lambda: str(uuid4()),
        capability_runtime: CodexRuntime | None = None,
    ) -> None:
        if not executable.is_absolute():
            raise ValueError("codex exec executable must be absolute")
        self._executable = executable
        self._process_factory = process_factory
        self._turn_id_factory = turn_id_factory
        self._capability_runtime = capability_runtime
        self._active: dict[tuple[str, str], _ExecProcess] = {}

    def supports(self, request: RunRequest) -> bool:
        return (
            request.effort != "auto"
            and not isinstance(request.thread, ForkThread)
            and request.sandbox is SandboxMode.READ_ONLY
            and request.command_network is None
        )

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        if self._capability_runtime is None:
            raise RuntimeError("codex exec does not expose the required paged capability probe")
        return await self._capability_runtime.probe(request)

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        if not self.supports(request):
            raise ValueError("requested controls are not representable by codex exec fallback")
        cwd = request.cwd if request.cwd is not None else Path.cwd()
        final_output: str | None = None
        turn: TurnRef | None = None
        terminal: Literal["completed", "failed"] | None = None
        terminal_usage: dict[str, Any] | None = None
        thread_id: str | None = None
        process: _ExecProcess | None = None
        active_key: tuple[str, str] | None = None
        with tempfile.TemporaryDirectory(prefix="qed-codex-") as directory:
            schema_path = Path(directory) / "output-schema.json"
            schema_path.write_text(json.dumps(request.output_schema), encoding="utf-8")
            argv = build_exec_argv(self._executable, request, schema_path)
            process = cast(_ExecProcess, await self._process_factory(argv, cwd))
            try:
                while line := await process.stdout.readline():
                    event = self._decode_event(line)
                    event_type = event.get("type")
                    if event_type == "thread.started":
                        thread_id = event.get("thread_id")
                        if not isinstance(thread_id, str):
                            raise RuntimeProtocolError("thread.started omitted thread_id")
                        yield ThreadStarted(thread_id=thread_id, backend=RuntimeBackend.EXEC)
                        continue
                    if event_type == "turn.started":
                        if thread_id is None:
                            raise RuntimeProtocolError("turn.started preceded thread.started")
                        turn = TurnRef(
                            thread_id=thread_id,
                            turn_id=self._turn_id_factory(),
                            backend=RuntimeBackend.EXEC,
                        )
                        active_key = (turn.thread_id, turn.turn_id)
                        self._active[active_key] = process
                        yield TurnStarted(turn=turn)
                        continue
                    if event_type == "item.completed":
                        if turn is None:
                            raise RuntimeProtocolError("item.completed preceded turn.started")
                        item = event.get("item")
                        if not isinstance(item, dict):
                            raise RuntimeProtocolError("item.completed omitted item object")
                        item_id = item.get("id")
                        item_type = item.get("type")
                        if not isinstance(item_id, str) or not isinstance(item_type, str):
                            raise RuntimeProtocolError("item.completed omitted string id or type")
                        text = item.get("text")
                        if item_type == "agent_message" and isinstance(text, str):
                            final_output = text
                        yield ItemCompleted(
                            thread_id=turn.thread_id,
                            turn_id=turn.turn_id,
                            item_id=item_id,
                            item_type=item_type,
                            payload=item,
                        )
                        continue
                    if event_type == "turn.completed":
                        terminal = "completed"
                        usage = event.get("usage")
                        terminal_usage = usage if isinstance(usage, dict) else None
                        continue
                    if event_type == "turn.failed":
                        terminal = "failed"
                        message = event.get("message") or event.get("error")
                        yield RuntimeErrorEvent(message=str(message or "codex exec turn failed"))
                        continue
                    if event_type == "error":
                        yield RuntimeErrorEvent(
                            message=str(event.get("message") or "codex exec error")
                        )
                        continue
                    if isinstance(event_type, str):
                        yield UnknownNotification(
                            method=event_type,
                            payload={key: value for key, value in event.items() if key != "type"},
                        )
                        continue
                    raise RuntimeProtocolError("codex exec event omitted string type")

                returncode = await process.wait()
                if returncode != 0:
                    raise RuntimeProtocolError(f"codex exec exited with status {returncode}")
                if turn is None or terminal is None:
                    raise RuntimeProtocolError("codex exec ended without a terminal turn event")
                if terminal_usage is not None:
                    yield TokenUsageUpdated(
                        thread_id=turn.thread_id,
                        turn_id=turn.turn_id,
                        usage=self._usage(terminal_usage),
                    )
                yield TurnCompleted(turn=turn, status=terminal, output=final_output)
            finally:
                if active_key is not None:
                    self._active.pop(active_key, None)
                if process.returncode is None:
                    process.terminate()
                    await process.wait()

    @staticmethod
    def _decode_event(line: bytes) -> dict[str, Any]:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeProtocolError("codex exec emitted invalid JSONL") from error
        if not isinstance(value, dict):
            raise RuntimeProtocolError("codex exec event must be an object")
        return value

    @staticmethod
    def _usage(value: dict[str, Any]) -> TokenUsage:
        def token(name: str) -> int:
            count = value.get(name, 0)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise RuntimeProtocolError(f"codex exec usage {name} must be non-negative")
            return count

        return TokenUsage(
            input_tokens=token("input_tokens"),
            output_tokens=token("output_tokens"),
            cached_input_tokens=token("cached_input_tokens"),
            reasoning_output_tokens=token("reasoning_output_tokens"),
        )

    async def interrupt(self, turn: TurnRef) -> None:
        if turn.backend is not RuntimeBackend.EXEC:
            raise ValueError("cannot interrupt a non-exec turn through ExecRuntime")
        process = self._active.get((turn.thread_id, turn.turn_id))
        if process is None:
            raise ValueError("codex exec turn is not active")
        process.terminate()
        await process.wait()

    async def close(self) -> None:
        processes = tuple(set(self._active.values()))
        for process in processes:
            if process.returncode is None:
                process.terminate()
        if processes:
            await asyncio.gather(*(process.wait() for process in processes))
