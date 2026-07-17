from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


OutputModel = TypeVar("OutputModel", bound=BaseModel)


class RuntimeBackend(StrEnum):
    MOCK = "mock"
    SDK = "sdk"
    APP_SERVER = "app_server"
    EXEC = "exec"


class RuntimePreference(StrEnum):
    AUTO = "auto"
    SDK = "sdk"
    APP_SERVER = "app_server"
    EXEC = "exec"


class SandboxMode(StrEnum):
    READ_ONLY = "read-only"


class WebSearchMode(StrEnum):
    DISABLED = "disabled"
    CACHED = "cached"
    INDEXED = "indexed"
    LIVE = "live"


class WorkRole(StrEnum):
    GENERAL = "general"
    LITERATURE = "literature"
    CITATION = "citation"
    VERIFIER = "verifier"


class FreshThread(_FrozenModel):
    kind: Literal["fresh"] = "fresh"


class ResumeThread(_FrozenModel):
    kind: Literal["resume"] = "resume"
    thread_id: str = Field(min_length=1)


class ForkThread(_FrozenModel):
    kind: Literal["fork"] = "fork"
    thread_id: str = Field(min_length=1)


ThreadTarget = Annotated[FreshThread | ResumeThread | ForkThread, Field(discriminator="kind")]


class RunRequest(_FrozenModel):
    model: str = Field(min_length=1)
    effort: str = Field(default="auto", min_length=1)
    proactive: bool = False
    prompt: str = Field(min_length=1)
    output_schema: dict[str, Any] = Field(min_length=1)
    thread: ThreadTarget = Field(default_factory=FreshThread)
    role: WorkRole = WorkRole.GENERAL
    sandbox: SandboxMode = SandboxMode.READ_ONLY
    web_search: WebSearchMode = WebSearchMode.DISABLED
    runtime: RuntimePreference = RuntimePreference.AUTO
    cwd: Path

    @field_validator("cwd")
    @classmethod
    def validate_absolute_cwd(cls, cwd: Path) -> Path:
        if not cwd.is_absolute():
            raise ValueError("cwd must be absolute")
        return cwd

    @model_validator(mode="after")
    def validate_network_controls(self) -> Self:
        if self.role in {WorkRole.VERIFIER, WorkRole.CITATION} and (
            not isinstance(self.thread, FreshThread)
            or self.sandbox is not SandboxMode.READ_ONLY
        ):
            raise ValueError("verification turns must be fresh and read-only")
        if (
            self.role is WorkRole.VERIFIER
            and self.web_search is not WebSearchMode.DISABLED
        ):
            raise ValueError("structural and detailed verifiers must be offline")
        network_roles = {WorkRole.LITERATURE, WorkRole.CITATION}
        if (
            self.web_search is not WebSearchMode.DISABLED
            and self.role not in network_roles
        ):
            raise ValueError("web search is limited to literature and citation turns")
        return self


class CapabilityRequest(_FrozenModel):
    model: str = Field(min_length=1)
    effort: str = Field(default="auto", min_length=1)
    proactive: bool = False


class ModelCapability(_FrozenModel):
    model: str = Field(min_length=1)
    advertised_efforts: tuple[str, ...] = Field(min_length=1)
    default_effort: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_default_effort(self) -> Self:
        if self.default_effort not in self.advertised_efforts:
            raise ValueError("default effort must be advertised by the model")
        if len(set(self.advertised_efforts)) != len(self.advertised_efforts):
            raise ValueError("advertised efforts must be unique")
        return self


class RuntimeCapabilities(_FrozenModel):
    model: str
    advertised_efforts: tuple[str, ...]
    default_effort: str
    selected_effort: str
    multi_agent: bool
    proactive_multi_agent: bool


def resolve_capability(
    request: CapabilityRequest,
    *,
    model: ModelCapability,
    multi_agent: bool,
) -> RuntimeCapabilities:
    if request.model != model.model:
        raise ValueError(f"model catalog returned {model.model!r}, expected {request.model!r}")

    proactive_available = (
        request.proactive and multi_agent and "ultra" in model.advertised_efforts
    )
    if request.effort == "auto":
        selected = "ultra" if proactive_available else model.default_effort
    elif request.effort in model.advertised_efforts:
        selected = request.effort
    else:
        raise ValueError(
            f"effort {request.effort!r} is not advertised for model {request.model!r}"
        )
    proactive = proactive_available and selected == "ultra"

    return RuntimeCapabilities(
        model=model.model,
        advertised_efforts=model.advertised_efforts,
        default_effort=model.default_effort,
        selected_effort=selected,
        multi_agent=multi_agent,
        proactive_multi_agent=proactive,
    )


class TurnRef(_FrozenModel):
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    backend: RuntimeBackend


class TokenUsage(_FrozenModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)


class ThreadStarted(_FrozenModel):
    kind: Literal["thread.started"] = "thread.started"
    thread_id: str = Field(min_length=1)
    backend: RuntimeBackend


class TurnStarted(_FrozenModel):
    kind: Literal["turn.started"] = "turn.started"
    turn: TurnRef


class TokenUsageUpdated(_FrozenModel):
    kind: Literal["token_usage.updated"] = "token_usage.updated"
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    usage: TokenUsage


class ItemCompleted(_FrozenModel):
    kind: Literal["item.completed"] = "item.completed"
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    item_type: str = Field(min_length=1)
    payload: dict[str, Any]


class RuntimeErrorEvent(_FrozenModel):
    kind: Literal["runtime.error"] = "runtime.error"
    message: str = Field(min_length=1)
    retryable: bool = False


class UnknownNotification(_FrozenModel):
    kind: Literal["notification.unknown"] = "notification.unknown"
    method: str = Field(min_length=1)
    payload: dict[str, Any]


class TurnCompleted(_FrozenModel):
    kind: Literal["turn.completed"] = "turn.completed"
    turn: TurnRef
    status: Literal["completed", "failed", "interrupted"]
    output: str | None = None

    @model_validator(mode="after")
    def validate_completed_output(self) -> Self:
        if self.status == "completed" and (
            self.output is None or not self.output.strip()
        ):
            raise ValueError("completed turn must include structured output")
        return self

    def parse_output_as(self, output_type: type[OutputModel]) -> OutputModel:
        if self.status != "completed" or self.output is None:
            raise ValueError("only completed turns have structured output")
        return output_type.model_validate_json(self.output, strict=True)


RuntimeEvent = (
    ThreadStarted
    | TurnStarted
    | TokenUsageUpdated
    | ItemCompleted
    | RuntimeErrorEvent
    | UnknownNotification
    | TurnCompleted
)
