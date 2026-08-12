"""Strict domain schemas and deterministic hashing for QED artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum, StrEnum
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

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


class EvidenceTrust(StrEnum):
    """Reader-facing trust level for one evidence field."""

    LEGACY_UNTRUSTED = "legacy_untrusted"
    MODEL_REPORTED = "model_reported"
    RUNTIME_OBSERVED = "runtime_observed"
    SERVER_CAPTURED = "server_captured"


class WebSearchObservation(StrictModel):
    """One URL-bearing native web-search action observed by QED."""

    schema_version: Literal[1] = 1
    id: NonEmptyStr
    run_id: NonEmptyStr
    backend: Literal["mock", "sdk", "app_server", "exec"]
    local_thread_id: NonEmptyStr
    external_thread_id: NonEmptyStr
    turn_id: NonEmptyStr
    item_id: NonEmptyStr
    action_type: Literal["open_page", "find_in_page"]
    uri: NonEmptyStr
    uri_sha256: Sha256
    payload: dict[str, JsonValue]
    payload_sha256: Sha256
    captured_at: datetime

    @model_validator(mode="after")
    def validate_observation_hashes(self) -> Self:
        parsed = urlsplit(self.uri)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("observed web-search URI must be an HTTP(S) URL")
        if self.uri_sha256 != sha256_text(self.uri):
            raise ValueError("uri_sha256 does not match observed URI")
        if self.payload_sha256 != canonical_sha256(self.payload):
            raise ValueError("payload_sha256 does not match observed payload")
        if (
            self.captured_at.tzinfo is None
            or self.captured_at.utcoffset() is None
        ):
            raise ValueError("web-search observation captured_at must be timezone-aware")
        return self


class Evidence(StrictModel):
    schema_version: Literal[1, 2] = 1
    id: NonEmptyStr
    kind: Literal["paper", "theorem", "computation", "human_guidance", "source", "note"]
    title: NonEmptyStr
    content: NonEmptyStr
    content_sha256: Sha256
    provenance: Provenance
    source_uri: NonEmptyStr | None = None
    citation: NonEmptyStr | None = None
    source_trust: EvidenceTrust = EvidenceTrust.LEGACY_UNTRUSTED
    content_trust: EvidenceTrust = EvidenceTrust.LEGACY_UNTRUSTED
    observation_ids: tuple[NonEmptyStr, ...] = ()
    source_uri_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_evidence_integrity(self) -> Self:
        if self.content_sha256 != sha256_text(self.content):
            raise ValueError("content_sha256 does not match content")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("evidence observation_ids must be unique")
        if self.source_uri is None:
            if self.source_uri_sha256 is not None:
                raise ValueError("source_uri_sha256 requires source_uri")
        elif (
            self.schema_version == 2
            and self.source_uri_sha256 != sha256_text(self.source_uri)
        ):
            raise ValueError("source_uri_sha256 does not match source_uri")
        if self.schema_version == 1:
            if (
                self.source_trust is not EvidenceTrust.LEGACY_UNTRUSTED
                or self.content_trust is not EvidenceTrust.LEGACY_UNTRUSTED
                or self.observation_ids
                or self.source_uri_sha256 is not None
            ):
                raise ValueError("schema v1 evidence must remain legacy_untrusted")
            return self
        if self.content_trust is not EvidenceTrust.MODEL_REPORTED:
            raise ValueError("evidence content is model_reported by the current runtime")
        if self.source_trust is EvidenceTrust.RUNTIME_OBSERVED:
            if self.source_uri is None or not self.observation_ids:
                raise ValueError(
                    "runtime_observed evidence requires a URI and observations"
                )
        elif self.source_trust is EvidenceTrust.MODEL_REPORTED:
            if self.observation_ids:
                raise ValueError(
                    "model_reported evidence cannot claim runtime observations"
                )
        elif self.source_trust is EvidenceTrust.SERVER_CAPTURED:
            raise ValueError(
                "the current Codex interfaces cannot produce server_captured evidence"
            )
        else:
            raise ValueError("schema v2 evidence cannot use legacy trust")
        return self


def evidence_sha256(evidence: Evidence) -> str:
    """Hash evidence using the immutable field set for its schema version."""

    payload = evidence.model_dump(mode="json")
    if evidence.schema_version == 1:
        for field in (
            "source_trust",
            "content_trust",
            "observation_ids",
            "source_uri_sha256",
        ):
            payload.pop(field, None)
    return canonical_sha256(payload)


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


class CitationSupport(StrictModel):
    """A structured, byte-checkable link from a proof span to frozen evidence."""

    evidence_id: NonEmptyStr
    proof_span: NonEmptyStr
    evidence_excerpt: NonEmptyStr
    source_locator: NonEmptyStr


class VerificationCheck(StrictModel):
    id: NonEmptyStr
    category: NonEmptyStr
    status: CheckStatus
    summary: NonEmptyStr
    proof_spans: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    rule_ids: tuple[NonEmptyStr, ...] = ()
    citation_support: tuple[CitationSupport, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("verification check evidence_ids must be unique")
        if len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("verification check rule_ids must be unique")
        support_keys = [
            (
                support.evidence_id,
                support.proof_span,
                support.evidence_excerpt,
                support.source_locator,
            )
            for support in self.citation_support
        ]
        if len(set(support_keys)) != len(support_keys):
            raise ValueError("verification check citation_support must be unique")
        return self


class Finding(StrictModel):
    id: NonEmptyStr
    check_id: NonEmptyStr
    severity: Literal["info", "minor", "major", "critical"]
    summary: NonEmptyStr
    detail: NonEmptyStr
    proof_span: NonEmptyStr | None = None
    evidence_ids: tuple[NonEmptyStr, ...] = ()


class VerificationReport(StrictModel):
    schema_version: Literal[1, 2, 3] = 3
    id: NonEmptyStr
    candidate_id: NonEmptyStr
    candidate_sha256: Sha256
    kind: Literal[
        "structural",
        "detailed",
        "assumptions_quantifiers",
        "counterexample_edge_case",
        "reconstruction",
        "citation",
        "mutation",
    ]
    checks: tuple[VerificationCheck, ...] = Field(min_length=1)
    findings: tuple[Finding, ...] = ()
    verifier_thread_id: NonEmptyStr
    verifier_external_thread_id: NonEmptyStr | None = None
    provenance: Provenance
    created_at: datetime

    @model_validator(mode="after")
    def validate_findings(self) -> Self:
        if self.schema_version == 1 and any(check.rule_ids for check in self.checks):
            raise ValueError(
                "verification report schema v1 cannot record rule coverage"
            )
        if self.schema_version in {1, 2} and any(
            check.citation_support for check in self.checks
        ):
            raise ValueError(
                "verification report schemas v1 and v2 cannot record citation support"
            )
        check_ids = [check.id for check in self.checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("verification check ids must be unique")
        finding_ids = [finding.id for finding in self.findings]
        if len(set(finding_ids)) != len(finding_ids):
            raise ValueError("verification finding ids must be unique")
        checks_by_id = {check.id: check for check in self.checks}
        unknown = {finding.check_id for finding in self.findings} - set(check_ids)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"findings reference unknown checks: {names}")
        for finding in self.findings:
            if (
                finding.severity in {"major", "critical"}
                and checks_by_id[finding.check_id].status is not CheckStatus.FAIL
            ):
                raise ValueError(
                    f"{finding.severity} findings must reference a failed check"
                )
        findings_by_check = {finding.check_id for finding in self.findings}
        missing = {
            check.id
            for check in self.checks
            if check.status is CheckStatus.FAIL and check.id not in findings_by_check
        }
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"failed checks require findings: {names}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> VerificationVerdict:
        if any(finding.severity in {"major", "critical"} for finding in self.findings):
            return VerificationVerdict.FAIL
        statuses = {check.status for check in self.checks}
        if CheckStatus.FAIL in statuses:
            return VerificationVerdict.FAIL
        if CheckStatus.UNCERTAIN in statuses:
            return VerificationVerdict.UNCERTAIN
        return VerificationVerdict.PASS


def verification_report_sha256(report: VerificationReport) -> str:
    """Hash reports using the immutable field set defined by their schema version."""

    payload = report.model_dump(mode="json", exclude_computed_fields=True)
    if report.schema_version == 1:
        checks = payload.get("checks")
        assert isinstance(checks, list)
        for check in checks:
            assert isinstance(check, dict)
            check.pop("rule_ids", None)
    if report.schema_version in {1, 2}:
        checks = payload.get("checks")
        assert isinstance(checks, list)
        for check in checks:
            assert isinstance(check, dict)
            check.pop("citation_support", None)
    return canonical_sha256(payload)


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


def event_chain_sha256(events: Sequence[Event]) -> str:
    """Hash one ordered event chain using its canonical persisted representation."""

    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event).encode())
        digest.update(b"\n")
    return digest.hexdigest()


class ManifestArtifact(StrictModel):
    id: NonEmptyStr
    kind: NonEmptyStr
    sha256: Sha256
    media_type: NonEmptyStr
    relative_path: NonEmptyStr | None = None


class ManifestRecord(StrictModel):
    id: NonEmptyStr
    sha256: Sha256


class ManifestEvidenceProvenance(StrictModel):
    evidence_id: NonEmptyStr
    schema_version: Literal[1, 2]
    source_trust: EvidenceTrust
    content_trust: EvidenceTrust
    observation_ids: tuple[NonEmptyStr, ...] = ()
    source_uri_sha256: Sha256 | None = None


class ManifestWebSearchObservation(StrictModel):
    id: NonEmptyStr
    backend: Literal["mock", "sdk", "app_server", "exec"]
    local_thread_id: NonEmptyStr
    external_thread_id: NonEmptyStr
    turn_id: NonEmptyStr
    item_id: NonEmptyStr
    action_type: Literal["open_page", "find_in_page"]
    uri: NonEmptyStr
    uri_sha256: Sha256
    payload_sha256: Sha256
    captured_at: datetime


class ManifestRuntimeResolution(StrictModel):
    segment_id: NonEmptyStr
    sha256: Sha256
    resolution: JsonValue

    @model_validator(mode="after")
    def validate_resolution_hash(self) -> Self:
        if canonical_sha256(self.resolution) != self.sha256:
            raise ValueError("runtime resolution hash does not match resolution")
        return self


class ManifestExecutionSegment(StrictModel):
    id: NonEmptyStr
    version: Annotated[int, Field(ge=1)]
    runtime_version: NonEmptyStr
    runtime_resolution_sha256: Sha256 | None = None
    started_at: datetime
    observed_until: datetime
    duration_seconds: Annotated[float, Field(ge=0)]


class ManifestUsage(StrictModel):
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    cached_input_tokens: Annotated[int, Field(ge=0)] = 0
    reasoning_output_tokens: Annotated[int, Field(ge=0)] = 0
    turns: Annotated[int, Field(ge=0)] = 0
    search_queries: Annotated[int, Field(ge=0)] = 0
    execution_seconds: Annotated[float, Field(ge=0)] = 0


class ManifestTurnInput(StrictModel):
    id: NonEmptyStr
    role: NonEmptyStr
    prompt_version: NonEmptyStr
    output_schema_sha256: Sha256
    payload_sha256: Sha256
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_payload_hash(self) -> Self:
        if canonical_sha256(self.payload) != self.payload_sha256:
            raise ValueError("turn input payload hash does not match payload")
        return self


class ManifestTurn(StrictModel):
    thread_id: NonEmptyStr
    turn_id: NonEmptyStr
    backend: NonEmptyStr
    turn_input_id: NonEmptyStr


class ManifestThread(StrictModel):
    id: NonEmptyStr
    role: NonEmptyStr
    parent_thread_id: NonEmptyStr | None = None
    external_thread_id: NonEmptyStr | None = None
    status: NonEmptyStr
    provenance: Provenance
    provenance_sha256: Sha256

    @model_validator(mode="after")
    def validate_provenance_hash(self) -> Self:
        if canonical_sha256(self.provenance) != self.provenance_sha256:
            raise ValueError("thread provenance hash does not match provenance")
        return self


class ManifestFinding(StrictModel):
    id: NonEmptyStr
    report_id: NonEmptyStr
    finding_id: NonEmptyStr
    sha256: Sha256
    severity: NonEmptyStr
    proof_span: NonEmptyStr | None = None
    evidence_ids: tuple[NonEmptyStr, ...] = ()


class ManifestRuleCoverage(StrictModel):
    report_id: NonEmptyStr
    check_id: NonEmptyStr
    status: CheckStatus


class ManifestCitationSupport(StrictModel):
    report_id: NonEmptyStr
    check_id: NonEmptyStr
    evidence_id: NonEmptyStr
    proof_span: NonEmptyStr
    evidence_excerpt: NonEmptyStr
    source_locator: NonEmptyStr
    sha256: Sha256


class ManifestOperatorDecision(StrictModel):
    action: Literal["abandon"]
    idempotency_key: NonEmptyStr
    reason: NonEmptyStr
    status_before: RunStatus
    status_after: RunStatus
    event_seq: Annotated[int, Field(ge=1)]
    event_sha256: Sha256
    created_at: datetime


class ManifestVerificationRule(StrictModel):
    id: NonEmptyStr
    text: NonEmptyStr
    responsible_report_kinds: tuple[
        Literal[
            "structural",
            "detailed",
            "assumptions_quantifiers",
            "counterexample_edge_case",
            "reconstruction",
            "citation",
            "mutation",
        ], ...
    ] = Field(min_length=2)
    coverage: tuple[ManifestRuleCoverage, ...] = ()


class Manifest(StrictModel):
    schema_version: Literal[1, 2] = 2
    run_id: NonEmptyStr
    run_status: RunStatus
    run_stage: RunStage = "export"
    publication_phase: Literal["snapshot", "export_intent"] = "snapshot"
    code_verdict: Literal["PASS"] | None = None
    input_sha256: Sha256
    config_sha256: Sha256
    runtime_version: NonEmptyStr
    prompt_versions: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    evidence_records: tuple[ManifestRecord, ...] = ()
    evidence_provenance: tuple[ManifestEvidenceProvenance, ...] = ()
    web_search_observations: tuple[ManifestWebSearchObservation, ...] = ()
    plan_records: tuple[ManifestRecord, ...] = ()
    candidate_records: tuple[ManifestRecord, ...] = ()
    verification_records: tuple[ManifestRecord, ...] = ()
    adjudication_records: tuple[ManifestRecord, ...] = ()
    decision_records: tuple[ManifestRecord, ...] = ()
    runtime_resolutions: tuple[ManifestRuntimeResolution, ...] = ()
    execution_segments: tuple[ManifestExecutionSegment, ...] = ()
    turn_inputs: tuple[ManifestTurnInput, ...] = ()
    turns: tuple[ManifestTurn, ...] = ()
    threads: tuple[ManifestThread, ...] = ()
    findings: tuple[ManifestFinding, ...] = ()
    citation_support: tuple[ManifestCitationSupport, ...] = ()
    operator_decisions: tuple[ManifestOperatorDecision, ...] = ()
    verification_rules: tuple[ManifestVerificationRule, ...] = ()
    usage: ManifestUsage = Field(default_factory=ManifestUsage)
    candidate_hashes: tuple[Sha256, ...] = ()
    verification_hashes: tuple[Sha256, ...] = ()
    artifacts: tuple[ManifestArtifact, ...] = ()
    first_event_seq: Annotated[int, Field(ge=1)] | None = None
    last_event_seq: Annotated[int, Field(ge=1)] | None = None
    event_chain_sha256: Sha256 | None = None
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
