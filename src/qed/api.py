"""Versioned FastAPI surface for the QED research service."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from qed.config import QEDConfig
from qed.inputs import RunInput
from qed.logging import get_logger
from qed.runtime import create_codex_runtime
from qed.schemas import Event, canonical_json
from qed.service import (
    ApplicationService,
    CommandReceipt,
    RunAlreadyActiveError,
    RuntimeFactory,
    StreamHeartbeat,
    build_service,
)
from qed.service_settings import ServiceSettings
from qed.store import (
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
    RunRecord,
    RunSnapshot,
    RunStatus,
    StoreError,
)
from qed.workflow import WorkflowExecutionError

RunId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
CommandKey = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]
_SSE_EVENT_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_REQUEST_BODY_BYTES = 1024 * 1024
_MAX_SSE_CONNECTIONS = 32
_MAX_SSE_CONNECTIONS_PER_CLIENT = 4
_MAX_SSE_REPLAY_EVENTS = 1000
_MAX_SSE_LIFETIME_SECONDS = 2 * 60 * 60
_LOGGER = get_logger(__name__)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateRunRequest(ApiModel):
    schema_version: Literal[1] = 1
    run_id: RunId | None = None
    run_input: RunInput
    config: QEDConfig = Field(default_factory=QEDConfig)

    @field_validator("run_input", mode="before")
    @classmethod
    def normalize_run_input_arrays(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        rules = normalized.get("verification_rules")
        if isinstance(rules, list):
            normalized["verification_rules"] = tuple(rules)
        return normalized

    @field_validator("config", mode="before")
    @classmethod
    def normalize_config_arrays(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        search = normalized.get("search")
        if isinstance(search, dict):
            normalized_search = dict(search)
            roles = normalized_search.get("allowed_roles")
            if isinstance(roles, list):
                normalized_search["allowed_roles"] = tuple(roles)
            normalized["search"] = normalized_search
        return normalized


class RunListResponse(ApiModel):
    schema_version: Literal[1] = 1
    items: tuple[RunRecord, ...]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)


class CapabilitiesResponse(ApiModel):
    schema_version: Literal[1] = 1
    api_version: Literal["v1"] = "v1"
    default_model: str
    commands: tuple[Literal["start", "cancel", "resume"], ...] = (
        "start",
        "cancel",
        "resume",
    )
    event_transport: Literal["sse"] = "sse"
    authentication_required: bool


class CommandRequest(ApiModel):
    schema_version: Literal[1] = 1
    idempotency_key: CommandKey


class RunEventEnvelope(ApiModel):
    schema_version: Literal[1] = 1
    run_id: str
    sequence: int = Field(ge=1)
    occurred_at: datetime
    kind: str
    stage_id: str
    payload: JsonValue


class ApiError(ApiModel):
    code: str
    message: str
    diagnostic_id: str


class ErrorEnvelope(ApiModel):
    schema_version: Literal[1] = 1
    error: ApiError


class AuthenticationRequired(RuntimeError):
    """Raised when a configured bearer token is missing or invalid."""


def _application_service(request: Request) -> ApplicationService:
    service: ApplicationService = request.app.state.application_service
    return service


ServiceDependency = Annotated[ApplicationService, Depends(_application_service)]

_bearer = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def _authenticate(request: Request, credentials: BearerCredentials) -> None:
    settings: ServiceSettings = request.app.state.service_settings
    if not settings.auth_required:
        return
    expected = settings.auth_token
    if (
        expected is None
        or credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(
            credentials.credentials,
            expected.get_secret_value(),
        )
    ):
        raise AuthenticationRequired


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    diagnostic_id: str | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ApiError(
            code=code,
            message=message,
            diagnostic_id=diagnostic_id or f"diag-{uuid4().hex}",
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _declared_content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name.lower() != b"content-length":
            continue
        try:
            length = int(value)
        except ValueError:
            return None
        return length if length >= 0 else None
    return None


class _RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared_length = _declared_content_length(scope)
        if declared_length is not None and declared_length > self._max_body_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                first_message = message
                break
            chunk = message.get("body", b"")
            if len(chunk) > self._max_body_bytes - len(body):
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                first_message = {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
                break

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return first_message
            return await receive()

        await self._app(scope, replay_body, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = _error_response(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="request_too_large",
            message="The request body is too large.",
        )
        await response(scope, receive, send)


def _event_envelope(event: Event) -> RunEventEnvelope:
    return RunEventEnvelope(
        run_id=event.run_id,
        sequence=event.seq,
        occurred_at=event.created_at,
        kind=event.event_type,
        stage_id=event.stage,
        payload=event.payload,
    )


def _render_sse_event(event: Event) -> str:
    event_name = event.event_type if _SSE_EVENT_NAME.fullmatch(event.event_type) else "qed.event"
    return (
        f"id: {event.seq}\n"
        f"event: {event_name}\n"
        f"data: {canonical_json(_event_envelope(event))}\n\n"
    )


def create_app(
    *,
    settings: ServiceSettings,
    service: ApplicationService | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> FastAPI:
    """Build an app with an injected service or the production Codex runtime."""

    if service is not None and runtime_factory is not None:
        raise ValueError("service and runtime_factory are mutually exclusive")
    selected_runtime_factory = (
        create_codex_runtime if runtime_factory is None else runtime_factory
    )
    selected_service = (
        service
        if service is not None
        else build_service(
            settings,
            runtime_factory=selected_runtime_factory,
        )
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await selected_service.close()

    app = FastAPI(
        title="QED API",
        version="1.0.0",
        lifespan=lifespan,
        dependencies=[Depends(_authenticate)],
    )
    app.state.application_service = selected_service
    app.state.service_settings = settings
    sse_lock = asyncio.Lock()
    sse_clients: dict[str, int] = {}
    sse_total = 0
    app.add_middleware(
        _RequestBodyLimitMiddleware,
        max_body_bytes=_MAX_REQUEST_BODY_BYTES,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Authorization", "Content-Type", "Last-Event-ID"],
    )

    @app.exception_handler(AuthenticationRequired)
    async def authentication_error(
        _request: Request,
        _error: AuthenticationRequired,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="authentication_required",
            message="A valid bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_request",
            message="Request validation failed.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        if error.status_code == status.HTTP_404_NOT_FOUND:
            code = "route_not_found"
            message = "The requested API route was not found."
        elif error.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
            code = "method_not_allowed"
            message = "The HTTP method is not allowed for this route."
        else:
            code = "http_error"
            message = "The HTTP request could not be completed."
        return _error_response(
            status_code=error.status_code,
            code=code,
            message=message,
            headers=dict(error.headers) if error.headers is not None else None,
        )

    @app.exception_handler(NotFoundError)
    async def not_found_error(_request: Request, _error: NotFoundError) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="run_not_found",
            message="The requested run was not found.",
        )

    @app.exception_handler(ConflictError)
    async def conflict_error(_request: Request, _error: ConflictError) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="resource_conflict",
            message="The request conflicts with durable run state.",
        )

    @app.exception_handler(InvalidTransitionError)
    async def invalid_transition_error(
        _request: Request,
        _error: InvalidTransitionError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="invalid_transition",
            message="The command is not valid for the run's current state.",
        )

    @app.exception_handler(RunAlreadyActiveError)
    async def active_worker_error(
        _request: Request,
        _error: RunAlreadyActiveError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="run_already_active",
            message="The run already has an active worker.",
        )

    @app.exception_handler(StoreError)
    async def store_error(_request: Request, _error: StoreError) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="The request could not be completed.",
        )

    @app.exception_handler(WorkflowExecutionError)
    async def workflow_error(
        _request: Request,
        _error: WorkflowExecutionError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="execution_failed",
            message="The research command could not be completed.",
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        diagnostic_id = f"diag-{uuid4().hex}"
        _LOGGER.error(
            "api.request_failed",
            diagnostic_id=diagnostic_id,
            error_type=type(error).__name__,
        )
        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="The request could not be completed.",
            diagnostic_id=diagnostic_id,
        )

    @app.post(
        "/api/v1/runs",
        response_model=RunRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_run(request: CreateRunRequest, service: ServiceDependency) -> RunRecord:
        run_id = request.run_id or f"run-{uuid4().hex}"
        return service.create_run(request.run_input, request.config, run_id=run_id)

    @app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
    async def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse(
            default_model=QEDConfig().model,
            authentication_required=settings.auth_required,
        )

    @app.get("/api/v1/runs", response_model=RunListResponse)
    async def list_runs(
        service: ServiceDependency,
        status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> RunListResponse:
        runs = service.list_runs()
        if status_filter is not None:
            runs = tuple(run for run in runs if run.status is status_filter)
        return RunListResponse(
            items=runs[offset : offset + limit],
            total=len(runs),
            offset=offset,
            limit=limit,
        )

    @app.get("/api/v1/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: RunId, service: ServiceDependency) -> RunRecord:
        return service.get_run(run_id)

    @app.get("/api/v1/runs/{run_id}/snapshot", response_model=RunSnapshot)
    async def snapshot(run_id: RunId, service: ServiceDependency) -> RunSnapshot:
        return service.snapshot(run_id)

    @app.post(
        "/api/v1/runs/{run_id}/commands/start",
        response_model=CommandReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(
        run_id: RunId,
        command: CommandRequest,
        service: ServiceDependency,
    ) -> CommandReceipt:
        return await service.start_run(run_id, idempotency_key=command.idempotency_key)

    @app.post(
        "/api/v1/runs/{run_id}/commands/cancel",
        response_model=CommandReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_run(
        run_id: RunId,
        command: CommandRequest,
        service: ServiceDependency,
    ) -> CommandReceipt:
        return await service.cancel_run(run_id, idempotency_key=command.idempotency_key)

    @app.post(
        "/api/v1/runs/{run_id}/commands/resume",
        response_model=CommandReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def resume_run(
        run_id: RunId,
        command: CommandRequest,
        service: ServiceDependency,
    ) -> CommandReceipt:
        return await service.resume_run(run_id, idempotency_key=command.idempotency_key)

    @app.get("/api/v1/runs/{run_id}/events", response_model=None)
    async def events(
        request: Request,
        run_id: RunId,
        service: ServiceDependency,
        last_event_id: Annotated[
            int | None,
            Header(alias="Last-Event-ID", ge=0),
        ] = None,
    ) -> StreamingResponse | JSONResponse:
        nonlocal sse_total
        service.get_run(run_id)
        client_id = request.client.host if request.client is not None else "unknown"
        async with sse_lock:
            if (
                sse_total >= _MAX_SSE_CONNECTIONS
                or sse_clients.get(client_id, 0) >= _MAX_SSE_CONNECTIONS_PER_CLIENT
            ):
                return _error_response(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code="sse_quota_exceeded",
                    message="The event stream quota is temporarily exhausted.",
                )
            sse_total += 1
            sse_clients[client_id] = sse_clients.get(client_id, 0) + 1

        async def body() -> AsyncIterator[str]:
            nonlocal sse_total
            delivered = 0
            try:
                async with asyncio.timeout(_MAX_SSE_LIFETIME_SECONDS):
                    async for item in service.stream_events(
                        run_id,
                        after_seq=last_event_id or 0,
                    ):
                        if await request.is_disconnected():
                            return
                        if isinstance(item, StreamHeartbeat):
                            yield ": heartbeat\n\n"
                        else:
                            delivered += 1
                            if delivered > _MAX_SSE_REPLAY_EVENTS:
                                yield "event: qed.stream_limit\ndata: {\"schema_version\":1}\n\n"
                                return
                            yield _render_sse_event(item)
            except TimeoutError:
                yield "event: qed.stream_timeout\ndata: {\"schema_version\":1}\n\n"
            finally:
                async with sse_lock:
                    sse_total -= 1
                    remaining = sse_clients.get(client_id, 1) - 1
                    if remaining:
                        sse_clients[client_id] = remaining
                    else:
                        sse_clients.pop(client_id, None)

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-QED-SSE-Schema-Version": "1",
            },
        )

    return app
