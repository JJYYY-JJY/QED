from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    CapabilityRequest,
    ForkThread,
    FreshThread,
    ItemCompleted,
    ModelCapability,
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
    resolve_capability,
)


class RuntimeProtocolError(RuntimeError):
    pass


class AppServerTransport(Protocol):
    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def notify(self, method: str, params: dict[str, Any]) -> None: ...

    def notifications(self) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _EffortOption(_WireModel):
    reasoning_effort: str = Field(alias="reasoningEffort", min_length=1)


class _CatalogModel(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    model: str = Field(min_length=1)
    supported_reasoning_efforts: tuple[_EffortOption, ...] = Field(
        alias="supportedReasoningEfforts", min_length=1
    )
    default_reasoning_effort: str = Field(alias="defaultReasoningEffort", min_length=1)


class _ModelPage(_WireModel):
    data: tuple[_CatalogModel, ...]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class _Feature(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1)
    enabled: bool
    default_enabled: bool = Field(alias="defaultEnabled")
    stage: str = Field(min_length=1)


class _FeaturePage(_WireModel):
    data: tuple[_Feature, ...]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class _Identifier(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = Field(min_length=1)


class _ThreadResponse(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    thread: _Identifier


class _TurnResponse(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    turn: _Identifier


class _UsageBreakdown(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    input_tokens: int = Field(alias="inputTokens", ge=0)
    output_tokens: int = Field(alias="outputTokens", ge=0)
    cached_input_tokens: int = Field(default=0, alias="cachedInputTokens", ge=0)
    reasoning_output_tokens: int = Field(default=0, alias="reasoningOutputTokens", ge=0)


class _TokenUsage(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    last: _UsageBreakdown


class _UsageParams(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    thread_id: str = Field(alias="threadId", min_length=1)
    turn_id: str = Field(alias="turnId", min_length=1)
    token_usage: _TokenUsage = Field(alias="tokenUsage")


class _ItemParams(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    thread_id: str = Field(alias="threadId", min_length=1)
    turn_id: str = Field(alias="turnId", min_length=1)
    item: dict[str, Any]


class _TurnParams(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    thread_id: str = Field(alias="threadId", min_length=1)
    turn: dict[str, Any]


class _ErrorDetail(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message: str = Field(min_length=1)


class _ErrorParams(_WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    error: _ErrorDetail
    will_retry: bool = Field(default=False, alias="willRetry")


class AppServerRuntime:
    """Version-shaped adapter over the stable App Server JSON-RPC subset QED uses."""

    def __init__(self, transport: AppServerTransport) -> None:
        self._transport = transport

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        models = await self._model_catalog()
        features = await self._features()
        matching = [entry for entry in models if entry.model == request.model]
        if len(matching) != 1:
            raise ValueError(
                f"expected one exact catalog match for model {request.model!r}, got {len(matching)}"
            )
        selected = matching[0]
        model = ModelCapability(
            model=selected.model,
            advertised_efforts=tuple(
                option.reasoning_effort for option in selected.supported_reasoning_efforts
            ),
            default_effort=selected.default_reasoning_effort,
        )
        multi_agent = any(feature.name == "multi_agent" and feature.enabled for feature in features)
        return resolve_capability(request, model=model, multi_agent=multi_agent)

    async def _model_catalog(self) -> list[_CatalogModel]:
        return await self._page("model/list", _ModelPage)

    async def _features(self) -> list[_Feature]:
        return await self._page("experimentalFeature/list", _FeaturePage)

    async def _page(
        self,
        method: str,
        page_type: type[_ModelPage] | type[_FeaturePage],
    ) -> list[Any]:
        cursor: str | None = None
        seen: set[str] = set()
        values: list[Any] = []
        while True:
            raw = await self._transport.request(method, {"cursor": cursor, "limit": 100})
            page = page_type.model_validate(raw)
            values.extend(page.data)
            cursor = page.next_cursor
            if cursor is None:
                return values
            if cursor in seen:
                raise RuntimeProtocolError(f"{method} repeated pagination cursor {cursor!r}")
            seen.add(cursor)

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        if request.effort == "auto":
            raise ValueError("AppServerRuntime requires a capability-resolved effort")

        notifications = self._transport.notifications()
        thread_method, thread_params = self._thread_request(request)
        thread_raw = await self._transport.request(thread_method, thread_params)
        thread_id = _ThreadResponse.model_validate(thread_raw).thread.id
        yield ThreadStarted(thread_id=thread_id, backend=RuntimeBackend.APP_SERVER)

        turn_params = self._turn_request(request, thread_id)
        turn_raw = await self._transport.request("turn/start", turn_params)
        turn_id = _TurnResponse.model_validate(turn_raw).turn.id
        turn_ref = TurnRef(
            thread_id=thread_id,
            turn_id=turn_id,
            backend=RuntimeBackend.APP_SERVER,
        )
        yield TurnStarted(turn=turn_ref)

        final_output: str | None = None
        fallback_output: str | None = None
        async for notification in notifications:
            method, params = self._notification_shape(notification)
            if method == "turn/started":
                started = _TurnParams.model_validate(params)
                self._require_turn(started.thread_id, started.turn, turn_ref)
                continue
            if method == "item/completed":
                completed = _ItemParams.model_validate(params)
                if completed.thread_id != thread_id or completed.turn_id != turn_id:
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
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    item_type=item_type,
                    payload=completed.item,
                )
                continue
            if method == "thread/tokenUsage/updated":
                usage = _UsageParams.model_validate(params)
                if usage.thread_id != thread_id or usage.turn_id != turn_id:
                    continue
                last = usage.token_usage.last
                yield TokenUsageUpdated(
                    thread_id=thread_id,
                    turn_id=turn_id,
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
                yield RuntimeErrorEvent(
                    message=error.error.message,
                    retryable=error.will_retry,
                )
                continue
            if method == "turn/completed":
                completed_turn = _TurnParams.model_validate(params)
                self._require_turn(completed_turn.thread_id, completed_turn.turn, turn_ref)
                status = completed_turn.turn.get("status")
                if status not in {"completed", "failed", "interrupted"}:
                    raise RuntimeProtocolError(f"unsupported terminal turn status {status!r}")
                yield TurnCompleted(
                    turn=turn_ref,
                    status=status,
                    output=final_output if final_output is not None else fallback_output,
                )
                return
            yield UnknownNotification(method=method, payload=params)

        raise RuntimeProtocolError("App Server notification stream ended before turn/completed")

    def _thread_request(self, request: RunRequest) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "sandbox": request.sandbox.value,
            "model": request.model,
            "config": self._thread_config(request),
        }
        if request.cwd is not None:
            params["cwd"] = str(request.cwd)
        if isinstance(request.thread, FreshThread):
            return "thread/start", params
        if isinstance(request.thread, ResumeThread):
            params["threadId"] = request.thread.thread_id
            return "thread/resume", params
        if isinstance(request.thread, ForkThread):
            params["threadId"] = request.thread.thread_id
            return "thread/fork", params
        raise AssertionError("unreachable thread target")

    @staticmethod
    def _thread_config(request: RunRequest) -> dict[str, Any]:
        config: dict[str, Any] = {"web_search": request.web_search.value}
        if request.command_network is not None:
            config["features"] = {
                "network_proxy": {
                    "enabled": True,
                    "mode": "limited",
                    "domains": {domain: "allow" for domain in request.command_network.domains},
                    "allow_local_binding": False,
                    "allow_upstream_proxy": False,
                }
            }
        return config

    @staticmethod
    def _turn_request(request: RunRequest, thread_id: str) -> dict[str, Any]:
        network_access = request.command_network is not None
        if request.sandbox is SandboxMode.READ_ONLY:
            sandbox_policy: dict[str, Any] = {
                "type": "readOnly",
                "networkAccess": network_access,
            }
        else:
            sandbox_policy = {
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": network_access,
            }
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": request.prompt}],
            "approvalPolicy": "never",
            "sandboxPolicy": sandbox_policy,
            "model": request.model,
            "effort": request.effort,
            "outputSchema": request.output_schema,
        }
        if request.cwd is not None:
            params["cwd"] = str(request.cwd)
        return params

    @staticmethod
    def _notification_shape(notification: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        method = notification.get("method")
        params = notification.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise RuntimeProtocolError("notification must contain string method and object params")
        return method, params

    @staticmethod
    def _require_turn(thread_id: str, turn: dict[str, Any], expected: TurnRef) -> None:
        if thread_id != expected.thread_id or turn.get("id") != expected.turn_id:
            raise RuntimeProtocolError("received lifecycle event for an unexpected turn")

    async def interrupt(self, turn: TurnRef) -> None:
        if turn.backend is not RuntimeBackend.APP_SERVER:
            raise ValueError("cannot interrupt a non-App-Server turn through AppServerRuntime")
        await self._transport.request(
            "turn/interrupt",
            {"threadId": turn.thread_id, "turnId": turn.turn_id},
        )

    async def close(self) -> None:
        await self._transport.close()
