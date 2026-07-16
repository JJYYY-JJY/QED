"""Strict domain schemas and deterministic hashing for QED artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum, StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    computed_field,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RunStage = Literal[
    "intake",
    "literature",
    "planning",
    "proving",
    "verification",
    "adjudication",
    "export",
    "complete",
]
RunStatus = Literal[
    "created",
    "running",
    "paused",
    "cancelling",
    "cancelled",
    "failed",
    "completed",
]


class StrictModel(BaseModel):
    """Base class for immutable, non-coercing public domain objects."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_computed_fields=True)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        rendered = value.isoformat()
        return rendered.replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


def canonical_json(value: JsonValue | BaseModel) -> str:
    """Serialize JSON-compatible data using a stable, whitespace-free encoding."""

    return json.dumps(
        _jsonable(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_text(value: str) -> str:
    """Return the lowercase SHA-256 digest of UTF-8 text."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_sha256(value: JsonValue | BaseModel) -> str:
    """Hash the canonical JSON representation of a domain value."""

    return sha256_text(canonical_json(value))


class Provenance(StrictModel):
    source: NonEmptyStr
    source_id: NonEmptyStr | None = None
    model: NonEmptyStr | None = None
    runtime_version: NonEmptyStr
    prompt_version: NonEmptyStr | None = None
    captured_at: datetime


class Evidence(StrictModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    kind: Literal["paper", "theorem", "computation", "human_guidance", "source", "note"]
    title: NonEmptyStr
    content: NonEmptyStr
    content_sha256: Sha256
    provenance: Provenance
    source_uri: NonEmptyStr | None = None
    citation: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_content_hash(self) -> Self:
        if self.content_sha256 != sha256_text(self.content):
            raise ValueError("content_sha256 does not match content")
        return self


class PlanStep(StrictModel):
    id: NonEmptyStr
    statement: NonEmptyStr
    rationale: NonEmptyStr
    success_criteria: tuple[NonEmptyStr, ...] = Field(min_length=1)
    dependencies: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    key_step: bool = False

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if self.id in self.dependencies:
            raise ValueError("a plan step cannot depend on itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("plan step dependencies must be unique")
        return self


class Plan(StrictModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    problem_sha256: Sha256
    strategy: NonEmptyStr
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    provenance: Provenance
    created_at: datetime

    @model_validator(mode="after")
    def validate_step_graph(self) -> Self:
        step_ids = [step.id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("plan step ids must be unique")

        known_ids = set(step_ids)
        for step in self.steps:
            unknown = set(step.dependencies) - known_ids
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"unknown dependency for {step.id}: {names}")

        dependencies = {step.id: set(step.dependencies) for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("plan dependencies must form an acyclic graph")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        return self


class ProofCandidate(StrictModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    run_id: NonEmptyStr
    plan_id: NonEmptyStr
    attempt: Annotated[int, Field(ge=1)]
    proof: NonEmptyStr
    proof_sha256: Sha256
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    deviations: tuple[NonEmptyStr, ...] = ()
    provenance: Provenance
    created_at: datetime

    @model_validator(mode="after")
    def validate_proof_hash(self) -> Self:
        if self.proof_sha256 != sha256_text(self.proof):
            raise ValueError("proof_sha256 does not match proof")
        return self


class CheckStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - mathematical verification verdict
    FAIL = "fail"
    UNCERTAIN = "uncertain"


class VerificationVerdict(StrEnum):
    PASS = "pass"  # noqa: S105 - mathematical verification verdict
    FAIL = "fail"
    UNCERTAIN = "uncertain"


class VerificationCheck(StrictModel):
    id: NonEmptyStr
    category: NonEmptyStr
    status: CheckStatus
    summary: NonEmptyStr
    proof_spans: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()


class Finding(StrictModel):
    id: NonEmptyStr
    check_id: NonEmptyStr
    severity: Literal["info", "minor", "major", "critical"]
    summary: NonEmptyStr
    detail: NonEmptyStr
    proof_span: NonEmptyStr | None = None
    evidence_ids: tuple[NonEmptyStr, ...] = ()


class VerificationReport(StrictModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    candidate_id: NonEmptyStr
    candidate_sha256: Sha256
    kind: Literal["structural", "detailed", "citation", "mutation"]
    checks: tuple[VerificationCheck, ...] = Field(min_length=1)
    findings: tuple[Finding, ...] = ()
    verifier_thread_id: NonEmptyStr
    provenance: Provenance
    created_at: datetime

    @model_validator(mode="after")
    def validate_findings(self) -> Self:
        check_ids = {check.id for check in self.checks}
        unknown = {finding.check_id for finding in self.findings} - check_ids
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"findings reference unknown checks: {names}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> VerificationVerdict:
        statuses = {check.status for check in self.checks}
        if CheckStatus.FAIL in statuses:
            return VerificationVerdict.FAIL
        if CheckStatus.UNCERTAIN in statuses:
            return VerificationVerdict.UNCERTAIN
        return VerificationVerdict.PASS


class Adjudication(StrictModel):
    schema_version: Literal[1] = 1
    id: NonEmptyStr
    candidate_id: NonEmptyStr
    report_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    outcome: Literal["accept", "revise_proof", "revise_plan", "rewrite", "abandon"]
    rationale: NonEmptyStr
    provenance: Provenance
    created_at: datetime


class Event(StrictModel):
    schema_version: Literal[1] = 1
    run_id: NonEmptyStr
    seq: Annotated[int, Field(ge=1)]
    event_type: NonEmptyStr
    stage: RunStage
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    payload_sha256: Sha256
    created_at: datetime

    @model_validator(mode="after")
    def validate_payload_hash(self) -> Self:
        if self.payload_sha256 != canonical_sha256(self.payload):
            raise ValueError("payload_sha256 does not match payload")
        return self


class ManifestArtifact(StrictModel):
    id: NonEmptyStr
    kind: NonEmptyStr
    sha256: Sha256
    media_type: NonEmptyStr
    relative_path: NonEmptyStr | None = None


class Manifest(StrictModel):
    schema_version: Literal[1] = 1
    run_id: NonEmptyStr
    run_status: RunStatus
    input_sha256: Sha256
    config_sha256: Sha256
    runtime_version: NonEmptyStr
    prompt_versions: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    candidate_hashes: tuple[Sha256, ...] = ()
    verification_hashes: tuple[Sha256, ...] = ()
    artifacts: tuple[ManifestArtifact, ...] = ()
    first_event_seq: Annotated[int, Field(ge=1)] | None = None
    last_event_seq: Annotated[int, Field(ge=1)] | None = None
    generated_at: datetime

    @model_validator(mode="after")
    def validate_event_range(self) -> Self:
        if (self.first_event_seq is None) != (self.last_event_seq is None):
            raise ValueError("manifest event range requires both endpoints")
        if (
            self.first_event_seq is not None
            and self.last_event_seq is not None
            and self.first_event_seq > self.last_event_seq
        ):
            raise ValueError("manifest event range is reversed")
        return self
