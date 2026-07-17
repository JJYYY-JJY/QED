"""Model-owned drafts and application-owned materialization helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from qed.schemas import (
    Adjudication,
    CheckStatus,
    Evidence,
    Finding,
    Plan,
    PlanStep,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    canonical_sha256,
    sha256_text,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EvidenceKind = Literal["paper", "theorem", "computation", "human_guidance", "source", "note"]
VerificationKind = Literal["structural", "detailed", "citation", "mutation"]


class ModelDraft(BaseModel):
    """A strict schema for model output with no application authority fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvidenceDraft(ModelDraft):
    kind: EvidenceKind
    title: NonEmptyStr
    content: NonEmptyStr
    source_uri: NonEmptyStr | None = None
    citation: NonEmptyStr | None = None


class EvidenceBatch(ModelDraft):
    schema_version: Literal[1] = 1
    items: tuple[EvidenceDraft, ...] = Field(min_length=1)


class PlanStepDraft(ModelDraft):
    id: NonEmptyStr
    statement: NonEmptyStr
    rationale: NonEmptyStr
    success_criteria: tuple[NonEmptyStr, ...] = Field(min_length=1)
    dependencies: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    key_step: bool = False


class PlanDraft(ModelDraft):
    schema_version: Literal[1] = 1
    strategy: NonEmptyStr
    steps: tuple[PlanStepDraft, ...] = Field(min_length=1)


class ProofDraft(ModelDraft):
    schema_version: Literal[1] = 1
    proof: NonEmptyStr
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    deviations: tuple[NonEmptyStr, ...] = ()


class VerificationCheckDraft(ModelDraft):
    id: NonEmptyStr
    category: NonEmptyStr
    status: CheckStatus
    summary: NonEmptyStr
    proof_spans: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()


class FindingDraft(ModelDraft):
    id: NonEmptyStr
    check_id: NonEmptyStr
    severity: Literal["info", "minor", "major", "critical"]
    summary: NonEmptyStr
    detail: NonEmptyStr
    proof_span: NonEmptyStr | None = None
    evidence_ids: tuple[NonEmptyStr, ...] = ()


class VerificationDraft(ModelDraft):
    schema_version: Literal[1] = 1
    checks: tuple[VerificationCheckDraft, ...] = Field(min_length=1)
    findings: tuple[FindingDraft, ...] = ()


class AdjudicationDraft(ModelDraft):
    schema_version: Literal[1] = 1
    outcome: Literal["accept", "revise_proof", "revise_plan", "rewrite", "abandon"]
    rationale: NonEmptyStr


def _stable_id(prefix: str, value: BaseModel | JsonValue) -> str:
    return f"{prefix}-{canonical_sha256(value)[:20]}"


def materialize_evidence(
    draft: EvidenceBatch,
    provenance: Provenance,
) -> tuple[Evidence, ...]:
    """Assign evidence IDs and content hashes outside the model boundary."""

    return tuple(
        Evidence(
            id=_stable_id(
                "evidence",
                {
                    "draft": item.model_dump(mode="json"),
                    "provenance_source_id": provenance.source_id,
                },
            ),
            kind=item.kind,
            title=item.title,
            content=item.content,
            content_sha256=sha256_text(item.content),
            provenance=provenance,
            source_uri=item.source_uri,
            citation=item.citation,
        )
        for item in draft.items
    )


def materialize_plan(
    draft: PlanDraft,
    *,
    problem_sha256: str,
    provenance: Provenance,
    created_at: datetime,
) -> Plan:
    """Bind a plan draft to one frozen problem and execution provenance."""

    identity: dict[str, JsonValue] = {
        "problem_sha256": problem_sha256,
        "draft": draft.model_dump(mode="json"),
        "provenance_source_id": provenance.source_id,
    }
    return Plan(
        id=_stable_id("plan", identity),
        problem_sha256=problem_sha256,
        strategy=draft.strategy,
        steps=tuple(
            PlanStep(
                id=step.id,
                statement=step.statement,
                rationale=step.rationale,
                success_criteria=step.success_criteria,
                dependencies=step.dependencies,
                evidence_ids=step.evidence_ids,
                key_step=step.key_step,
            )
            for step in draft.steps
        ),
        provenance=provenance,
        created_at=created_at,
    )


def materialize_candidate(
    draft: ProofDraft,
    *,
    run_id: str,
    plan_id: str,
    attempt: int,
    provenance: Provenance,
    created_at: datetime,
) -> ProofCandidate:
    """Bind proof text to a run, plan, attempt, and trusted content hash."""

    proof_sha256 = sha256_text(draft.proof)
    identity: dict[str, JsonValue] = {
        "run_id": run_id,
        "plan_id": plan_id,
        "attempt": attempt,
        "proof_sha256": proof_sha256,
    }
    return ProofCandidate(
        id=f"candidate-{attempt}-{canonical_sha256(identity)[:20]}",
        run_id=run_id,
        plan_id=plan_id,
        attempt=attempt,
        proof=draft.proof,
        proof_sha256=proof_sha256,
        evidence_ids=draft.evidence_ids,
        deviations=draft.deviations,
        provenance=provenance,
        created_at=created_at,
    )


def materialize_report(
    draft: VerificationDraft,
    *,
    candidate: ProofCandidate,
    kind: VerificationKind,
    verifier_thread_id: str,
    verifier_external_thread_id: str | None = None,
    provenance: Provenance,
    created_at: datetime,
) -> VerificationReport:
    """Attach a verifier draft to the exact candidate bytes it reviewed."""

    identity: dict[str, JsonValue] = {
        "candidate_id": candidate.id,
        "candidate_sha256": candidate.proof_sha256,
        "kind": kind,
        "draft": draft.model_dump(mode="json"),
    }
    return VerificationReport(
        id=_stable_id(f"verification-{kind}", identity),
        candidate_id=candidate.id,
        candidate_sha256=candidate.proof_sha256,
        kind=kind,
        checks=tuple(
            VerificationCheck(
                id=check.id,
                category=check.category,
                status=check.status,
                summary=check.summary,
                proof_spans=check.proof_spans,
                evidence_ids=check.evidence_ids,
            )
            for check in draft.checks
        ),
        findings=tuple(
            Finding(
                id=finding.id,
                check_id=finding.check_id,
                severity=finding.severity,
                summary=finding.summary,
                detail=finding.detail,
                proof_span=finding.proof_span,
                evidence_ids=finding.evidence_ids,
            )
            for finding in draft.findings
        ),
        verifier_thread_id=verifier_thread_id,
        verifier_external_thread_id=verifier_external_thread_id,
        provenance=provenance,
        created_at=created_at,
    )


def materialize_adjudication(
    draft: AdjudicationDraft,
    *,
    candidate_id: str,
    report_ids: tuple[str, ...],
    provenance: Provenance,
    created_at: datetime,
) -> Adjudication:
    """Record advisory model output without granting it final PASS authority."""

    identity: dict[str, JsonValue] = {
        "candidate_id": candidate_id,
        "report_ids": list(report_ids),
        "draft": draft.model_dump(mode="json"),
    }
    return Adjudication(
        id=_stable_id("adjudication", identity),
        candidate_id=candidate_id,
        report_ids=report_ids,
        outcome=draft.outcome,
        rationale=draft.rationale,
        provenance=provenance,
        created_at=created_at,
    )
