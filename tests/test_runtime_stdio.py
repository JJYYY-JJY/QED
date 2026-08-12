from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from codex_cli_bin import bundled_codex_path

from qed.runtime import (
    RuntimeProtocolError,
    RuntimeRequestTimeout,
    StdioAppServerTransport,
    build_app_server_argv,
    probe_codex_version,
    resolve_codex_executable,
)

_CODEX_HOME = Path("/var/lib/qed/codex-home")


class FakeStdout:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def finish(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout.lines.put_nowait(b"")
        self._finished.set()

    def terminate(self) -> None:
        self.finish(-15)


class FakeStdin:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.messages: list[dict[str, Any]] = []
        self.slow_written = asyncio.Event()

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        assert isinstance(message, dict)
        self.messages.append(message)
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            self._reply(request_id, {"userAgent": "fake"})
        elif method == "model/list":
            self._reply(request_id, {"data": [], "nextCursor": None})
            self.process.stdout.lines.put_nowait(
                json.dumps({"method": "server/ready", "params": {}}).encode() + b"\n"
            )
        elif method == "fast":
            self._reply(request_id, {"ok": True})
        elif method == "slow":
            self.slow_written.set()

    def _reply(self, request_id: object, result: dict[str, Any]) -> None:
        response = {"id": request_id, "result": result}
        self.process.stdout.lines.put_nowait(json.dumps(response).encode() + b"\n")

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.finish()

    async def wait_closed(self) -> None:
        return None


class DelayedInitializeStdin(FakeStdin):
    def __init__(self, process: FakeProcess) -> None:
        super().__init__(process)
        self.initialize_written = asyncio.Event()
        self._initialize_id: object = None

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        if message.get("method") != "initialize":
            super().write(data)
            return
        self.messages.append(message)
        self._initialize_id = message.get("id")
        self.initialize_written.set()

    def release(self) -> None:
        self._reply(self._initialize_id, {"userAgent": "fake"})


class StubbornStdin(FakeStdin):
    def close(self) -> None:
        return None


class StubbornProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.stdin = StubbornStdin(self)
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.finish(-9)


def test_app_server_argv_is_absolute_strict_stdio_and_has_no_escape_flags() -> None:
    argv = build_app_server_argv(Path("/opt/codex/bin/codex"))

    assert argv[:3] == (
        "/opt/codex/bin/codex",
        "app-server",
        "--strict-config",
    )
    assert argv[-2:] == ("--listen", "stdio://")
    assert "features.shell_tool=false" in argv
    assert "features.plugins=false" in argv
    assert 'web_search="disabled"' in argv
    rendered = " ".join(argv)
    assert "danger" not in rendered
    assert "bypass" not in rendered
    assert "full-auto" not in rendered


def test_runtime_version_comes_from_the_resolved_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/opt/codex/bin/codex")
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 1.2.3\n", stderr="")

    monkeypatch.setattr("qed.runtime.stdio.subprocess.run", run)

    assert probe_codex_version(executable) == "codex-cli 1.2.3"
    assert calls == [(str(executable), "--version")]


def test_default_executable_is_the_uv_bundled_codex() -> None:
    assert resolve_codex_executable() == bundled_codex_path().resolve(strict=True)
    with pytest.raises(PermissionError, match="package-managed official executable"):
        resolve_codex_executable(sys.executable)


async def test_stdio_transport_initializes_once_and_broadcasts_notifications() -> None:
    process = FakeProcess()
    launched: list[tuple[tuple[str, ...], Path]] = []

    async def spawn(argv: tuple[str, ...], codex_home: Path) -> FakeProcess:
        launched.append((argv, codex_home))
        return process

    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=cast(Any, spawn),
    )
    notifications = transport.notifications()

    result = await transport.request("model/list", {"cursor": None, "limit": 100})

    assert result == {"data": [], "nextCursor": None}
    assert launched == [
        (build_app_server_argv(Path("/opt/codex/bin/codex")), _CODEX_HOME)
    ]
    assert [message["method"] for message in process.stdin.messages] == [
        "initialize",
        "initialized",
        "model/list",
    ]
    assert process.stdin.messages[0]["params"] == {
        "clientInfo": {"name": "qed", "title": "QED", "version": "1"},
        "capabilities": {"experimentalApi": False},
    }
    assert await anext(notifications) == {"method": "server/ready", "params": {}}

    await notifications.aclose()
    await transport.close()


async def test_unstarted_notification_stream_aclose_unregisters_subscriber() -> None:
    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"), codex_home=_CODEX_HOME
    )
    notifications = transport.notifications()

    assert len(transport._subscribers) == 1

    await notifications.aclose()

    assert not transport._subscribers


async def test_concurrent_first_requests_wait_for_initialization() -> None:
    process = FakeProcess()
    delayed = DelayedInitializeStdin(process)
    process.stdin = delayed

    async def spawn(_argv: tuple[str, ...], _codex_home: Path) -> FakeProcess:
        return process

    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=cast(Any, spawn),
    )
    first = asyncio.create_task(transport.request("fast", {}))
    second = asyncio.create_task(
        transport.request("model/list", {"cursor": None, "limit": 100})
    )
    await delayed.initialize_written.wait()
    await asyncio.sleep(0)

    assert [message["method"] for message in delayed.messages] == ["initialize"]

    delayed.release()
    assert await asyncio.gather(first, second) == [
        {"ok": True},
        {"data": [], "nextCursor": None},
    ]
    assert [message["method"] for message in delayed.messages] == [
        "initialize",
        "initialized",
        "fast",
        "model/list",
    ]
    await transport.close()


async def test_notification_subscriber_fails_closed_on_queue_overflow() -> None:
    process = FakeProcess()

    async def spawn(_argv: tuple[str, ...], _codex_home: Path) -> FakeProcess:
        return process

    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=cast(Any, spawn),
        notification_queue_size=1,
    )
    notifications = transport.notifications()
    await transport.request("model/list", {"cursor": None, "limit": 100})
    process.stdout.lines.put_nowait(
        b'{"method":"server/second","params":{}}\n'
    )
    await asyncio.sleep(0)

    with pytest.raises(RuntimeProtocolError, match="bounded queue"):
        await anext(notifications)

    await notifications.aclose()
    await transport.close()


async def test_cancelled_request_late_response_does_not_poison_transport() -> None:
    process = FakeProcess()

    async def spawn(_argv: tuple[str, ...], _codex_home: Path) -> FakeProcess:
        return process

    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=cast(Any, spawn),
    )
    slow = asyncio.create_task(transport.request("slow", {}))
    await process.stdin.slow_written.wait()
    slow_request = next(
        message for message in process.stdin.messages if message.get("method") == "slow"
    )

    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
    process.stdin._reply(slow_request["id"], {"late": True})
    await asyncio.sleep(0)

    result = await asyncio.wait_for(transport.request("fast", {}), timeout=0.1)

    assert result == {"ok": True}
    await transport.close()


async def test_timed_out_request_late_response_does_not_poison_transport() -> None:
    process = FakeProcess()

    async def spawn(_argv: tuple[str, ...], _codex_home: Path) -> FakeProcess:
        return process

    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=cast(Any, spawn),
        request_timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeRequestTimeout, match="slow.*timed out"):
        await transport.request("slow", {})
    slow_request = next(
        message for message in process.stdin.messages if message.get("method") == "slow"
    )
    process.stdin._reply(slow_request["id"], {"late": True})
    await asyncio.sleep(0)

    assert await transport.request("fast", {}) == {"ok": True}
    await transport.close()


async def test_close_escalates_from_terminate_to_kill_with_bounded_waits() -> None:
    process = StubbornProcess()

    async def spawn(
        _argv: tuple[str, ...], _codex_home: Path
    ) -> StubbornProcess:
        return process

    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"),
        codex_home=_CODEX_HOME,
        process_factory=cast(Any, spawn),
        shutdown_timeout_seconds=0.01,
    )
    assert await transport.request("fast", {}) == {"ok": True}

    await asyncio.wait_for(transport.close(), timeout=0.2)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == -9
