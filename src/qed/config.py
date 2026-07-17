"""One strict, Codex-only runtime configuration."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from qed.schemas import canonical_sha256

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ParallelismPolicy(ConfigModel):
    runs: Annotated[int, Field(ge=1, le=32)] = 1
    proof_candidates: Annotated[int, Field(ge=1, le=64)] = 4
    verifiers: Annotated[int, Field(ge=1, le=64)] = 2
    subagents: Annotated[int, Field(ge=1, le=64)] = 4


class BudgetPolicy(ConfigModel):
    run_seconds: Annotated[int, Field(ge=1)] = 7200
    stage_seconds: Annotated[int, Field(ge=1)] = 1800
    max_tokens: Annotated[int, Field(ge=1)] = 250_000
    proof_attempts: Annotated[int, Field(ge=1)] = 8
    plan_revisions: Annotated[int, Field(ge=0)] = 2
    strategy_rewrites: Annotated[int, Field(ge=0)] = 2
    turn_retries: Annotated[int, Field(ge=0, le=10)] = 2

    @model_validator(mode="after")
    def validate_stage_budget(self) -> Self:
        if self.stage_seconds > self.run_seconds:
            raise ValueError("stage_seconds cannot exceed run_seconds")
        return self


class SearchPolicy(ConfigModel):
    enabled: bool = True
    allowed_roles: tuple[Literal["literature", "citation"], ...] = (
        "literature",
        "citation",
    )
    max_queries_per_stage: Annotated[int, Field(ge=1)] = 20

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        if len(set(self.allowed_roles)) != len(self.allowed_roles):
            raise ValueError("allowed_roles must be unique")
        return self


class SandboxPolicy(ConfigModel):
    literature: Literal["read-only"] = "read-only"
    planner: Literal["read-only"] = "read-only"
    prover: Literal["read-only", "workspace-write"] = "read-only"
    verifier: Literal["read-only"] = "read-only"
    adjudicator: Literal["read-only"] = "read-only"
    approval: Literal["never"] = "never"


class QEDConfig(ConfigModel):
    schema_version: Literal[1] = 1
    model: NonEmptyStr = "gpt-5.6-sol"
    effort: NonEmptyStr = "auto"
    backend: Literal["auto", "sdk", "app-server", "exec"] = "auto"
    parallelism: ParallelismPolicy = Field(default_factory=ParallelismPolicy)
    budgets: BudgetPolicy = Field(default_factory=BudgetPolicy)
    search: SearchPolicy = Field(default_factory=SearchPolicy)
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)

    @field_validator("model", "effort")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a nonempty string")
        return value.strip()

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)
