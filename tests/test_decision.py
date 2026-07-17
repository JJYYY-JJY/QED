from __future__ import annotations

from datetime import UTC, datetime

from qed.decision import decide_candidate
from qed.schemas import (
    CheckStatus,
    Finding,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    sha256_text,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def candidate() -> ProofCandidate:
    proof = "Every integer greater than one has a prime divisor."
    return ProofCandidate(
        id="candidate-1",
        run_id="run-1",
        plan_id="plan-1",
        attempt=1,
        proof=proof,
        proof_sha256=sha256_text(proof),
        provenance=Provenance(
            source="codex",
            source_id="writer-thread",
            model="gpt-5.6-sol",
            runtime_version="0.1.0b3",
            prompt_version="proof-v1",
            captured_at=NOW,
        ),
        created_at=NOW,
    )


def report(
    proof: ProofCandidate,
    *,
    kind: str,
    status: CheckStatus,
    thread_id: str,
    external_thread_id: str | None = None,
) -> VerificationReport:
    check_id = f"check-{kind}-{thread_id}"
    findings = (
        (
            Finding(
                id=f"finding-{kind}-{thread_id}",
                check_id=check_id,
                severity="major",
                summary="The check failed.",
                detail="The verifier found a correctness defect.",
            ),
        )
        if status is CheckStatus.FAIL
        else ()
    )
    return VerificationReport(
        id=f"{kind}-{thread_id}",
        candidate_id=proof.id,
        candidate_sha256=proof.proof_sha256,
        kind=kind,  # type: ignore[arg-type]
        checks=(
            VerificationCheck(
                id=check_id,
                category="correctness",
                status=status,
                summary="Independent structured check.",
            ),
        ),
        findings=findings,
        verifier_thread_id=thread_id,
        verifier_external_thread_id=external_thread_id or thread_id,
        provenance=Provenance(
            source="codex",
            source_id=thread_id,
            model="gpt-5.6-sol",
            runtime_version="0.1.0b3",
            prompt_version=f"verify-{kind}-v1",
            captured_at=NOW,
        ),
        created_at=NOW,
    )


def test_code_computes_pass_from_independent_required_reports() -> None:
    proof = candidate()
    decision = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="structural-thread",
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.PASS,
                thread_id="detailed-thread",
            ),
        ),
    )

    assert decision.passed is True
    assert decision.reasons == ()
    assert decision.required_kinds == ("structural", "detailed")


def test_missing_uncertain_or_failed_report_cannot_pass() -> None:
    proof = candidate()
    missing = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="structural-thread",
            ),
        ),
    )
    uncertain = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="structural-thread",
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.UNCERTAIN,
                thread_id="detailed-thread",
            ),
        ),
    )
    failed = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.FAIL,
                thread_id="structural-thread",
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.PASS,
                thread_id="detailed-thread",
            ),
        ),
    )

    assert missing.passed is False
    assert missing.reasons == ("missing:detailed",)
    assert uncertain.passed is False
    assert uncertain.reasons == ("non_pass:detailed:uncertain",)
    assert failed.passed is False
    assert failed.reasons == ("non_pass:structural:fail",)


def test_reusing_a_writer_or_verifier_thread_cannot_pass() -> None:
    proof = candidate()
    writer_reused = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="writer-thread",
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.PASS,
                thread_id="detailed-thread",
            ),
        ),
    )
    verifier_reused = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="shared-thread",
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.PASS,
                thread_id="shared-thread",
            ),
        ),
    )

    assert writer_reused.passed is False
    assert "writer_thread_reused:structural" in writer_reused.reasons
    assert verifier_reused.passed is False
    assert "verifier_thread_reused:shared-thread" in verifier_reused.reasons


def test_distinct_local_ids_cannot_hide_one_external_verifier_thread() -> None:
    proof = candidate()
    decision = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="local-structural",
                external_thread_id="codex-thread-1",
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.PASS,
                thread_id="local-detailed",
                external_thread_id="codex-thread-1",
            ),
        ),
    )

    assert decision.passed is False
    assert "verifier_external_thread_reused:codex-thread-1" in decision.reasons


def test_citation_report_can_be_required_by_policy() -> None:
    proof = candidate()
    reports = (
        report(
            proof,
            kind="structural",
            status=CheckStatus.PASS,
            thread_id="structural-thread",
        ),
        report(
            proof,
            kind="detailed",
            status=CheckStatus.PASS,
            thread_id="detailed-thread",
        ),
    )

    decision = decide_candidate(proof, reports, require_citation=True)

    assert decision.passed is False
    assert decision.reasons == ("missing:citation",)
