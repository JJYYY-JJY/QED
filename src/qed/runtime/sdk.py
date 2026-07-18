from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
from openai_codex.types import ReasoningEffort

from .app_server import (
    RuntimeProtocolError,
    _ErrorParams,
    _ItemParams,
    _TurnParams,
    _UsageParams,
)
from .base import CodexRuntime
from .isolation import codex_home_environment, server_config
from .models import (
    CapabilityRequest,
    ForkThread,
    FreshThread,
    ItemCompleted,
    ResumeThread,
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
    WebSearchMode,
)
from .stdio import build_app_server_argv


class _SdkHandle(Protocol):
    thread_id: str
    id: str

    def stream(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> Any: ...


class _SdkThread(Protocol):
    id: str

    async def turn(self, prompt: str, **kwargs: Any) -> _SdkHandle: ...


class _SdkClient(Protocol):
    async def thread_start(self, **kwargs: Any) -> _SdkThread: ...

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> _SdkThread: ...

    async def thread_fork(self, thread_id: str, **kwargs: Any) -> _SdkThread: ...

    async def close(self) -> None: ...


class SdkRuntime:
    """Adapter for controls representable by the published typed Python SDK."""

    def __init__(
        self,
        client: _SdkClient | None = None,
        *,
        capability_runtime: CodexRuntime | None = None,
        executable: Path | None = None,
        codex_home: Path | None = None,
    ) -> None:
        if client is None:
            if (
                executable is None
                or not executable.is_absolute()
                or codex_home is None
            ):
                raise ValueError(
                    "SdkRuntime requires the resolved executable and server-owned Codex home"
                )
            client = cast(
                _SdkClient,
                AsyncCodex(
                    CodexConfig(
                        codex_bin=str(executable),
                        launch_args_override=build_app_server_argv(executable),
                        env=codex_home_environment(codex_home),
                        experimental_api=False,
                    )
                ),
            )
        self._client = client
        self._capability_runtime = capability_runtime
        self._handles: dict[tuple[str, str], _SdkHandle] = {}

    def supports(self, request: RunRequest) -> bool:
        if request.effort == "auto":
            return False
        if request.web_search is WebSearchMode.INDEXED:
            return False
        try:
            ReasoningEffort(request.effort)
        except ValueError:
            return False
        return True

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        if self._capability_runtime is None:
            raise RuntimeError(
                "the published SDK cannot perform the required paged capability probe"
            )
        return await self._capability_runtime.probe(request)

    def preflight(self, request: RunRequest) -> None:
        if not self.supports(request):
            raise ValueError(
                "requested controls are not representable by the published SDK"
            )

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.preflight(request)

        sandbox = self._sandbox(request.sandbox)
        lifecycle: dict[str, Any] = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(request.cwd),
            "model": request.model,
            "sandbox": sandbox,
            "config": server_config(request.web_search),
        }
        if isinstance(request.thread, FreshThread):
            thread = await self._client.thread_start(**lifecycle)
        elif isinstance(request.thread, ResumeThread):
            thread = await self._client.thread_resume(request.thread.thread_id, **lifecycle)
        elif isinstance(request.thread, ForkThread):
            thread = await self._client.thread_fork(request.thread.thread_id, **lifecycle)
        else:
            raise AssertionError("unreachable thread target")

        yield ThreadStarted(thread_id=thread.id, backend=RuntimeBackend.SDK)
        handle = await thread.turn(
            request.prompt,
            approval_mode=ApprovalMode.deny_all,
            cwd=str(request.cwd),
            effort=ReasoningEffort(request.effort),
            model=request.model,
            output_schema=request.output_schema,
            sandbox=sandbox,
        )
        turn = TurnRef(
            thread_id=handle.thread_id,
            turn_id=handle.id,
            backend=RuntimeBackend.SDK,
        )
        key = (turn.thread_id, turn.turn_id)
        self._handles[key] = handle
        final_output: str | None = None
        fallback_output: str | None = None
        terminal_observed = False
        try:
            yield TurnStarted(turn=turn)
            async for raw_notification in handle.stream():
                method, params = self._notification(raw_notification)
                if method == "turn/started":
                    continue
                if method == "item/completed":
                    completed = _ItemParams.model_validate(params)
                    if completed.thread_id != turn.thread_id or completed.turn_id != turn.turn_id:
                        continue
                    item_id = completed.item.get("id")
                    item_type = completed.item.get("type")
                    if not isinstance(item_id, str) or not isinstance(item_type, str):
                        raise RuntimeProtocolError("item/completed omitted string id or type")
                    text = completed.item.get("text")
                    if item_type == "agentMessage" and isinstance(text, str):
                        phase = completed.item.get("phase")
                        if phase == "final_answer":
                            final_output = text
                        elif phase is None:
                            fallback_output = text
                    yield ItemCompleted(
                        thread_id=turn.thread_id,
                        turn_id=turn.turn_id,
                        item_id=item_id,
                        item_type=item_type,
                        payload=completed.item,
                        completed_at=(
                            datetime.fromtimestamp(
                                completed.completed_at_ms / 1000,
                                tz=UTC,
                            )
                            if completed.completed_at_ms is not None
                            else None
                        ),
                    )
                    continue
                if method == "thread/tokenUsage/updated":
                    usage = _UsageParams.model_validate(params)
                    if usage.thread_id != turn.thread_id or usage.turn_id != turn.turn_id:
                        continue
                    last = usage.token_usage.last
                    yield TokenUsageUpdated(
                        thread_id=turn.thread_id,
                        turn_id=turn.turn_id,
                        usage=TokenUsage(
                            input_tokens=last.input_tokens,
                            output_tokens=last.output_tokens,
                            cached_input_tokens=last.cached_input_tokens,
                            reasoning_output_tokens=last.reasoning_output_tokens,
                        ),
                    )
                    continue
                if method == "error":
                    error = _ErrorParams.model_validate(params)
                    if (
                        error.thread_id != turn.thread_id
                        or error.turn_id != turn.turn_id
                    ):
                        raise RuntimeProtocolError(
                            "received error for an unexpected SDK turn"
                        )
                    yield RuntimeErrorEvent(
                        message=error.error.message,
                        retryable=error.will_retry,
                    )
                    continue
                if method == "turn/completed":
                    terminal = _TurnParams.model_validate(params)
                    status = terminal.turn.get("status")
                    if (
                        terminal.thread_id != turn.thread_id
                        or terminal.turn.get("id") != turn.turn_id
                    ):
                        raise RuntimeProtocolError("received completion for an unexpected SDK turn")
                    if status not in {"completed", "failed", "interrupted"}:
                        raise RuntimeProtocolError(f"unsupported terminal turn status {status!r}")
                    terminal_status = cast(
                        Literal["completed", "failed", "interrupted"], status
                    )
                    terminal_observed = True
                    yield TurnCompleted(
                        turn=turn,
                        status=terminal_status,
                        output=final_output if final_output is not None else fallback_output,
                    )
                    return
                yield UnknownNotification(method=method, payload=params)
            raise RuntimeProtocolError("SDK stream ended before turn/completed")
        finally:
            if terminal_observed:
                self._handles.pop(key, None)

    @staticmethod
    def _notification(notification: Any) -> tuple[str, dict[str, Any]]:
        if isinstance(notification, dict):
            method = notification.get("method")
            params = notification.get("params")
        else:
            method = getattr(notification, "method", None)
            payload = getattr(notification, "payload", None)
            model_dump = getattr(payload, "model_dump", None)
            params = (
                model_dump(mode="json", by_alias=True) if callable(model_dump) else payload
            )
        if not isinstance(method, str) or not isinstance(params, dict):
            raise RuntimeProtocolError("SDK notification has an invalid method or payload")
        return method, params

    @staticmethod
    def _sandbox(mode: SandboxMode) -> Sandbox:
        if mode is not SandboxMode.READ_ONLY:
            raise ValueError("QED runtime turns must be read-only")
        return Sandbox.read_only

    async def interrupt(self, turn: TurnRef) -> None:
        if turn.backend is not RuntimeBackend.SDK:
            raise ValueError("cannot interrupt a non-SDK turn through SdkRuntime")
        handle = self._handles.get((turn.thread_id, turn.turn_id))
        if handle is None:
            raise ValueError("SDK turn is not active")
        await handle.interrupt()

    async def close(self) -> None:
        try:
            await self._client.close()
        finally:
            self._handles.clear()
