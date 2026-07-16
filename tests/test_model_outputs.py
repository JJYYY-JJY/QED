from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from qed.model_outputs import (
    EvidenceBatch,
    EvidenceDraft,
    FindingDraft,
    PlanDraft,
    PlanStepDraft,
    ProofDraft,
    VerificationCheckDraft,
    VerificationDraft,
    materialize_candidate,
    materialize_evidence,
    materialize_plan,
    materialize_report,
)
from qed.schemas import CheckStatus, Provenance, sha256_text

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
PROBLEM_SHA = sha256_text("Prove that there are infinitely many primes.")


def provenance(thread_id: str) -> Provenance:
    return Provenance(
        source="codex",
        source_id=thread_id,
        model="gpt-5.6-sol",
        runtime_version="0.144.5",
        prompt_version="v1",
        captured_at=NOW,
    )


def test_model_output_schemas_forbid_authority_fields_and_unknown_data() -> None:
    with pytest.raises(ValidationError):
        ProofDraft(
            proof="A proof.",
            run_id="model-chosen-run",  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        VerificationDraft(
            checks=(
                VerificationCheckDraft(
                    id="check-1",
                    category="logic",
                    status=CheckStatus.PASS,
                    summary="The inference is valid.",
                    verdict="PASS",  # type: ignore[call-arg]
                ),
            ),
        )

    schema = VerificationDraft.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["VerificationCheckDraft"]["additionalProperties"] is False
    assert "verdict" not in schema["properties"]


def test_application_materializes_stable_evidence_plan_and_candidate_authority() -> None:
    evidence_draft = EvidenceBatch(
        items=(
            EvidenceDraft(
                kind="theorem",
                title="Euclid's theorem",
                content=(
                    "Every finite list of primes omits a prime divisor of its product plus one."
                ),
                source_uri="https://example.test/euclid",
                citation="Euclid, Elements IX.20",
            ),
        ),
    )
    evidence = materialize_evidence(evidence_draft, provenance("literature-thread"))
    repeated_evidence = materialize_evidence(evidence_draft, provenance("literature-thread"))
    plan_draft = PlanDraft(
        strategy="Assume a finite list and construct a number with a new prime divisor.",
        steps=(
            PlanStepDraft(
                id="finite-list",
                statement="Assume all primes are p_1 through p_n.",
                rationale="This negates the target.",
                success_criteria=("The assumption is stated without loss of primes.",),
            ),
            PlanStepDraft(
                id="new-divisor",
                statement="A prime divisor of p_1...p_n + 1 is absent from the list.",
                rationale="The remainder modulo each listed prime is one.",
                success_criteria=("A contradiction follows.",),
                dependencies=("finite-list",),
                evidence_ids=(evidence[0].id,),
                key_step=True,
            ),
        ),
    )
    plan = materialize_plan(
        plan_draft,
        problem_sha256=PROBLEM_SHA,
        provenance=provenance("planning-thread"),
        created_at=NOW,
    )
    proof_draft = ProofDraft(
        proof="Assume p_1,...,p_n are all primes. A prime dividing their product plus one is new.",
        evidence_ids=(evidence[0].id,),
    )
    candidate = materialize_candidate(
        proof_draft,
        run_id="run-1",
        plan_id=plan.id,
        attempt=1,
        provenance=provenance("proof-thread"),
        created_at=NOW,
    )

    assert evidence == repeated_evidence
    assert evidence[0].id.startswith("evidence-")
    assert evidence[0].content_sha256 == sha256_text(evidence[0].content)
    assert plan.id.startswith("plan-")
    assert candidate.id.startswith("candidate-1-")
    assert candidate.proof_sha256 == sha256_text(proof_draft.proof)


def test_application_materializes_report_and_code_derives_verdict() -> None:
    proof = ProofDraft(proof="A proposed proof.")
    candidate = materialize_candidate(
        proof,
        run_id="run-1",
        plan_id="plan-1",
        attempt=1,
        provenance=provenance("proof-thread"),
        created_at=NOW,
    )
    draft = VerificationDraft(
        checks=(
            VerificationCheckDraft(
                id="logic",
                category="logical-correctness",
                status=CheckStatus.FAIL,
                summary="The main implication is reversed.",
                proof_spans=("therefore",),
            ),
        ),
        findings=(
            FindingDraft(
                id="reversed-implication",
                check_id="logic",
                severity="critical",
                summary="Reversed implication",
                detail="The premise does not imply the stated conclusion.",
                proof_span="therefore",
            ),
        ),
    )

    report = materialize_report(
        draft,
        candidate=candidate,
        kind="detailed",
        verifier_thread_id="fresh-verifier-thread",
        provenance=provenance("fresh-verifier-thread"),
        created_at=NOW,
    )

    assert report.id.startswith("verification-detailed-")
    assert report.candidate_sha256 == candidate.proof_sha256
    assert report.verdict.value == "fail"
