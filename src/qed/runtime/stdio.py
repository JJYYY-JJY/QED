from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]

from .app_server import RuntimeProtocolError
from .isolation import (
    codex_home_environment,
    codex_subprocess_environment,
    server_config_overrides,
)
from .models import WebSearchMode
from .protocol import MAX_JSONL_FRAME_BYTES


class RuntimeRequestTimeout(RuntimeProtocolError):
    pass


def resolve_codex_executable(executable: str | Path | None = None) -> Path:
    bundled = Path(bundled_codex_path()).resolve(strict=True)
    value = bundled if executable is None else Path(executable)
    candidate = value if value.is_absolute() else shutil.which(str(value))
    if candidate is None:
        raise FileNotFoundError(f"could not resolve Codex executable {str(value)!r}")
    resolved = Path(candidate).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PermissionError(f"Codex executable is not executable: {resolved}")
    if resolved != bundled:
        raise PermissionError(
            "production Codex runtime must use the package-managed official executable"
        )
    return resolved


def probe_codex_version(executable: Path) -> str:
    """Return the exact version reported by the resolved executable."""

    if not executable.is_absolute():
        raise ValueError("Codex executable must be absolute before version probing")
    try:
        completed = subprocess.run(  # noqa: S603 - resolved absolute executable, no shell
            (str(executable), "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not probe Codex executable version: {executable}") from error
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0 or not output or "\n" in output or len(output) > 256:
        raise RuntimeError(f"Codex executable returned an invalid version: {executable}")
    return output


def build_app_server_argv(executable: Path) -> tuple[str, ...]:
    if not executable.is_absolute():
        raise ValueError("App Server executable must be an absolute path")
    controls = tuple(
        item
        for override in server_config_overrides(WebSearchMode.DISABLED)
        for item in ("-c", override)
    )
    return (
        str(executable),
        "app-server",
        "--strict-config",
        *controls,
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

    def kill(self) -> None: ...


ProcessFactory = Callable[[tuple[str, ...], Path], Awaitable[_Process]]


async def _spawn(argv: tuple[str, ...], codex_home: Path) -> _Process:
    process = await asyncio.create_subprocess_exec(  # noqa: S603 - absolute argv, no shell
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=MAX_JSONL_FRAME_BYTES + 1,
        env=codex_subprocess_environment(codex_home),
    )
    if process.stdin is None or process.stdout is None:
        process.terminate()
        await process.wait()
        raise RuntimeError("failed to create App Server stdio pipes")
    return cast(_Process, process)


_END = object()


class _NotificationStream:
    def __init__(
        self,
        queue: asyncio.Queue[object],
        unsubscribe: Callable[[asyncio.Queue[object]], None],
    ) -> None:
        self._queue = queue
        self._unsubscribe = unsubscribe
        self._closed = False

    def __aiter__(self) -> _NotificationStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._closed:
            raise StopAsyncIteration
        try:
            value = await self._queue.get()
            if value is _END:
                raise StopAsyncIteration
            if isinstance(value, BaseException):
                raise value
            if not isinstance(value, dict):
                raise RuntimeProtocolError(
                    "internal notification queue value is not an object"
                )
            return value
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe(self._queue)


class StdioAppServerTransport:
    """Small JSONL transport for the stable App Server stdio protocol."""

    def __init__(
        self,
        executable: Path,
        *,
        codex_home: Path,
        process_factory: ProcessFactory = _spawn,
        request_timeout_seconds: float = 30.0,
        notification_queue_size: int = 256,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("App Server request timeout must be positive")
        if notification_queue_size <= 0:
            raise ValueError("notification queue size must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("App Server shutdown timeout must be positive")
        codex_home_environment(codex_home)
        self._argv = build_app_server_argv(executable)
        self._codex_home = codex_home
        self._process_factory = process_factory
        self._request_timeout_seconds = request_timeout_seconds
        self._notification_queue_size = notification_queue_size
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._process: _Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._startup_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscribers: set[asyncio.Queue[object]] = set()
        self._initialized = False
        self._closed = False

    @classmethod
    def from_environment(
        cls,
        executable: str | Path | None = None,
        *,
        codex_home: Path,
    ) -> StdioAppServerTransport:
        return cls(resolve_codex_executable(executable), codex_home=codex_home)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_started()
        return await self._raw_request(method, params)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._ensure_started()
        await self._raw_notify(method, params)

    def notifications(self) -> _NotificationStream:
        queue: asyncio.Queue[object] = asyncio.Queue(
            maxsize=self._notification_queue_size
        )
        self._subscribers.add(queue)
        return _NotificationStream(queue, self._unsubscribe)

    def _unsubscribe(self, queue: asyncio.Queue[object]) -> None:
        self._subscribers.discard(queue)
        while not queue.empty():
            queue.get_nowait()

    async def _ensure_started(self) -> None:
        if self._closed:
            raise RuntimeError("App Server transport is closed")
        async with self._startup_lock:
            if self._initialized:
                return
            if self._process is None:
                self._process = await self._process_factory(
                    self._argv, self._codex_home
                )
                self._reader_task = asyncio.create_task(self._reader_loop())
            await self._raw_request(
                "initialize",
                {
                    "clientInfo": {"name": "qed", "title": "QED", "version": "1"},
                    "capabilities": {"experimentalApi": False},
                },
            )
            await self._raw_notify("initialized", {})
            self._initialized = True

    async def _raw_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                await self._write({"id": request_id, "method": method, "params": params})
                return await asyncio.shield(future)
        except TimeoutError as error:
            raise RuntimeRequestTimeout(f"App Server request {method!r} timed out") from error
        finally:
            if not future.done():
                future.cancel()
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
                if len(line) > MAX_JSONL_FRAME_BYTES:
                    raise RuntimeProtocolError(
                        "App Server JSONL frame exceeds the configured byte limit"
                    )
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
        if len(line) > MAX_JSONL_FRAME_BYTES:
            raise RuntimeProtocolError("App Server JSONL frame exceeds the configured byte limit")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeProtocolError("App Server emitted invalid JSONL") from error
        if not isinstance(value, dict):
            raise RuntimeProtocolError("App Server JSON-RPC message must be an object")
        return value

    def _resolve_response(self, request_id: object, message: dict[str, Any]) -> None:
        if not isinstance(request_id, int):
            raise RuntimeProtocolError(f"App Server returned unknown request id {request_id!r}")
        future = self._pending.get(request_id)
        if future is None:
            if 0 < request_id < self._next_id:
                return
            raise RuntimeProtocolError(f"App Server returned unknown request id {request_id!r}")
        if future.done():
            return
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
            try:
                subscriber.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(subscriber)
                self._replace_queue_value(
                    subscriber,
                    RuntimeProtocolError(
                        "App Server notification subscriber exceeded its bounded queue"
                    ),
                )

    def _broadcast_end(self) -> None:
        subscribers = tuple(self._subscribers)
        self._subscribers.clear()
        for subscriber in subscribers:
            self._replace_queue_value(subscriber, _END)

    @staticmethod
    def _replace_queue_value(queue: asyncio.Queue[object], value: object) -> None:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(value)

    def _fail_pending(self, error: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def _wait_for_exit(self, task: asyncio.Task[int]) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError:
            return False
        return True

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
        wait_task = asyncio.create_task(process.wait())
        if not await self._wait_for_exit(wait_task):
            if process.returncode is None:
                process.terminate()
            if not await self._wait_for_exit(wait_task):
                if process.returncode is None:
                    process.kill()
                if not await self._wait_for_exit(wait_task):
                    wait_task.cancel()
                    await asyncio.gather(wait_task, return_exceptions=True)
                    if self._reader_task is not None:
                        self._reader_task.cancel()
                        await asyncio.gather(self._reader_task, return_exceptions=True)
                    raise RuntimeError("App Server process did not exit after kill")
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)
