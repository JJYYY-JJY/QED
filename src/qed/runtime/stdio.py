from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

from .app_server import RuntimeProtocolError


def resolve_codex_executable(executable: str | Path | None = None) -> Path:
    value = "codex" if executable is None else str(executable)
    candidate = value if Path(value).is_absolute() else shutil.which(value)
    if candidate is None:
        raise FileNotFoundError(f"could not resolve Codex executable {value!r}")
    resolved = Path(candidate).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PermissionError(f"Codex executable is not executable: {resolved}")
    return resolved


def build_app_server_argv(executable: Path) -> tuple[str, ...]:
    if not executable.is_absolute():
        raise ValueError("App Server executable must be an absolute path")
    return (
        str(executable),
        "app-server",
        "--strict-config",
        "--listen",
        "stdio://",
    )


class _Stdin(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class _Stdout(Protocol):
    async def readline(self) -> bytes: ...


class _Process(Protocol):
    stdin: _Stdin
    stdout: _Stdout
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...


ProcessFactory = Callable[[tuple[str, ...]], Awaitable[_Process]]


async def _spawn(argv: tuple[str, ...]) -> _Process:
    process = await asyncio.create_subprocess_exec(  # noqa: S603 - absolute argv, no shell
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:
        process.terminate()
        await process.wait()
        raise RuntimeError("failed to create App Server stdio pipes")
    return cast(_Process, process)


_END = object()


class StdioAppServerTransport:
    """Small JSONL transport for the stable App Server stdio protocol."""

    def __init__(
        self,
        executable: Path,
        *,
        process_factory: ProcessFactory = _spawn,
    ) -> None:
        self._argv = build_app_server_argv(executable)
        self._process_factory = process_factory
        self._process: _Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._startup_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscribers: set[asyncio.Queue[object]] = set()
        self._closed = False

    @classmethod
    def from_environment(
        cls,
        executable: str | Path | None = None,
    ) -> StdioAppServerTransport:
        return cls(resolve_codex_executable(executable))

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_started()
        return await self._raw_request(method, params)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._ensure_started()
        await self._raw_notify(method, params)

    def notifications(self) -> AsyncGenerator[dict[str, Any]]:
        queue: asyncio.Queue[object] = asyncio.Queue()
        self._subscribers.add(queue)

        async def iterate() -> AsyncGenerator[dict[str, Any]]:
            try:
                while True:
                    value = await queue.get()
                    if value is _END:
                        return
                    if not isinstance(value, dict):
                        raise RuntimeProtocolError(
                            "internal notification queue value is not an object"
                        )
                    yield value
            finally:
                self._subscribers.discard(queue)

        return iterate()

    async def _ensure_started(self) -> None:
        if self._closed:
            raise RuntimeError("App Server transport is closed")
        if self._process is not None:
            return
        async with self._startup_lock:
            if self._process is not None:
                return
            self._process = await self._process_factory(self._argv)
            self._reader_task = asyncio.create_task(self._reader_loop())
            await self._raw_request(
                "initialize",
                {
                    "clientInfo": {"name": "qed", "title": "QED", "version": "1"},
                    "capabilities": {"experimentalApi": False},
                },
            )
            await self._raw_notify("initialized", {})

    async def _raw_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _raw_notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None:
            raise RuntimeError("App Server process is not running")
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _reader_loop(self) -> None:
        process = self._process
        assert process is not None
        try:
            while line := await process.stdout.readline():
                message = self._decode_message(line)
                request_id = message.get("id")
                method = message.get("method")
                if request_id is not None and method is None:
                    self._resolve_response(request_id, message)
                elif isinstance(method, str) and request_id is None:
                    self._broadcast(message)
                elif isinstance(method, str) and request_id is not None:
                    await self._write(
                        {
                            "id": request_id,
                            "error": {"code": -32601, "message": "method not supported"},
                        }
                    )
                else:
                    raise RuntimeProtocolError("invalid App Server JSON-RPC message shape")
            if not self._closed:
                raise RuntimeProtocolError(
                    f"App Server stdout closed unexpectedly (returncode={process.returncode})"
                )
        except BaseException as error:
            if not self._closed:
                self._fail_pending(error)
                self._broadcast_end()

    @staticmethod
    def _decode_message(line: bytes) -> dict[str, Any]:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeProtocolError("App Server emitted invalid JSONL") from error
        if not isinstance(value, dict):
            raise RuntimeProtocolError("App Server JSON-RPC message must be an object")
        return value

    def _resolve_response(self, request_id: object, message: dict[str, Any]) -> None:
        if not isinstance(request_id, int) or request_id not in self._pending:
            raise RuntimeProtocolError(f"App Server returned unknown request id {request_id!r}")
        future = self._pending[request_id]
        error = message.get("error")
        if error is not None:
            future.set_exception(RuntimeProtocolError(f"App Server request failed: {error!r}"))
            return
        result = message.get("result")
        if not isinstance(result, dict):
            future.set_exception(RuntimeProtocolError("App Server result must be an object"))
            return
        future.set_result(result)

    def _broadcast(self, message: dict[str, Any]) -> None:
        for subscriber in tuple(self._subscribers):
            subscriber.put_nowait(message)

    def _broadcast_end(self) -> None:
        for subscriber in tuple(self._subscribers):
            subscriber.put_nowait(_END)

    def _fail_pending(self, error: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._broadcast_end()
        process = self._process
        if process is None:
            return
        process.stdin.close()
        await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.terminate()
            await process.wait()
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)
