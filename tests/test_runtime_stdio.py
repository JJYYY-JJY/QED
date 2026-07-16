from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from qed.runtime import StdioAppServerTransport, build_app_server_argv


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

    def _reply(self, request_id: object, result: dict[str, Any]) -> None:
        response = {"id": request_id, "result": result}
        self.process.stdout.lines.put_nowait(json.dumps(response).encode() + b"\n")

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.finish()

    async def wait_closed(self) -> None:
        return None


def test_app_server_argv_is_absolute_strict_stdio_and_has_no_escape_flags() -> None:
    argv = build_app_server_argv(Path("/opt/codex/bin/codex"))

    assert argv == (
        "/opt/codex/bin/codex",
        "app-server",
        "--strict-config",
        "--listen",
        "stdio://",
    )
    rendered = " ".join(argv)
    assert "danger" not in rendered
    assert "bypass" not in rendered
    assert "full-auto" not in rendered


async def test_stdio_transport_initializes_once_and_broadcasts_notifications() -> None:
    process = FakeProcess()
    launched: list[tuple[str, ...]] = []

    async def spawn(argv: tuple[str, ...]) -> FakeProcess:
        launched.append(argv)
        return process

    transport = StdioAppServerTransport(
        Path("/opt/codex/bin/codex"), process_factory=cast(Any, spawn)
    )
    notifications = transport.notifications()

    result = await transport.request("model/list", {"cursor": None, "limit": 100})

    assert result == {"data": [], "nextCursor": None}
    assert launched == [build_app_server_argv(Path("/opt/codex/bin/codex"))]
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
