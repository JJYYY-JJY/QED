from __future__ import annotations

from datetime import UTC, datetime

from qed.decision import (
    CandidateDecision,
    candidate_decision_sha256,
    decide_candidate,
)
from qed.schemas import (
    CheckStatus,
    CitationSupport,
    Evidence,
    Finding,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    canonical_sha256,
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
    evidence_ids: tuple[str, ...] = (),
    rule_ids: tuple[str, ...] = (),
    citation_support: tuple[CitationSupport, ...] = (),
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
                evidence_ids=evidence_ids,
                rule_ids=rule_ids,
                citation_support=citation_support,
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


def evidence() -> Evidence:
    content = "Every integer greater than one has a prime divisor."
    return Evidence(
        id="evidence-1",
        kind="theorem",
        title="Prime divisor theorem",
        content=content,
        content_sha256=sha256_text(content),
        provenance=Provenance(
            source="codex",
            source_id="literature-thread",
            model="gpt-5.6-sol",
            runtime_version="0.1.0b3",
            prompt_version="literature-v1",
            captured_at=NOW,
        ),
    )


def citation_support(
    proof: ProofCandidate,
    source: Evidence,
    *,
    evidence_id: str | None = None,
    proof_span: str | None = None,
    excerpt: str | None = None,
) -> CitationSupport:
    return CitationSupport(
        evidence_id=evidence_id or source.id,
        proof_span=proof_span or proof.proof,
        evidence_excerpt=excerpt or source.content,
        source_locator=f"evidence:{source.id}",
    )


def test_schema_v1_decision_hash_omits_v2_rule_authority_fields() -> None:
    decision = CandidateDecision(
        schema_version=1,
        candidate_id="candidate-1",
        candidate_sha256="a" * 64,
        passed=True,
        required_kinds=("structural", "detailed"),
        report_ids=("structural-report", "detailed-report"),
        reasons=(),
    )
    legacy_payload = decision.model_dump(mode="json")
    legacy_payload.pop("required_rule_ids")
    legacy_payload.pop("rule_coverage")

    assert candidate_decision_sha256(decision) == canonical_sha256(legacy_payload)


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
        prover_external_thread_id="codex-writer-thread",
    )

    assert decision.passed is True
    assert decision.reasons == ()
    assert decision.required_kinds == ("structural", "detailed")


def test_legacy_report_schemas_cannot_gain_current_pass_authority() -> None:
    proof = candidate()
    reports = tuple(
        report(
            proof,
            kind=kind,
            status=CheckStatus.PASS,
            thread_id=f"{kind}-thread",
        ).model_copy(update={"schema_version": 2})
        for kind in ("structural", "detailed")
    )

    decision = decide_candidate(
        proof,
        reports,
        prover_external_thread_id="codex-writer-thread",
    )

    assert decision.schema_version == 3
    assert decision.passed is False
    assert decision.reasons == (
        "legacy_report_schema:structural-structural-thread:v2",
        "legacy_report_schema:detailed-detailed-thread:v2",
    )


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
        prover_external_thread_id="codex-writer-thread",
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
        prover_external_thread_id="codex-writer-thread",
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
        prover_external_thread_id="codex-writer-thread",
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
        prover_external_thread_id="codex-writer-thread",
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
        prover_external_thread_id="codex-writer-thread",
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
        prover_external_thread_id="codex-writer-thread",
    )

    assert decision.passed is False
    assert "verifier_external_thread_reused:codex-thread-1" in decision.reasons


def test_distinct_local_roles_cannot_hide_writer_external_thread_reuse() -> None:
    proof = candidate()
    decision = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="local-structural",
                external_thread_id="codex-writer-thread",
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.PASS,
                thread_id="local-detailed",
                external_thread_id="codex-detailed-thread",
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
    )

    assert decision.passed is False
    assert "writer_external_thread_reused:structural" in decision.reasons


def test_verification_rules_require_passing_structured_check_coverage() -> None:
    proof = candidate()
    reports = (
        report(
            proof,
            kind="structural",
            status=CheckStatus.PASS,
            thread_id="structural-thread",
            rule_ids=("rule-1",),
        ),
        report(
            proof,
            kind="detailed",
            status=CheckStatus.PASS,
            thread_id="detailed-thread",
        ),
    )

    missing = decide_candidate(
        proof,
        reports,
        prover_external_thread_id="codex-writer-thread",
        required_rule_ids=("rule-1", "rule-2"),
    )
    uncertain = decide_candidate(
        proof,
        (
            reports[0],
            report(
                proof,
                kind="detailed",
                status=CheckStatus.UNCERTAIN,
                thread_id="uncertain-thread",
                rule_ids=("rule-2",),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_rule_ids=("rule-1", "rule-2"),
    )
    failed = decide_candidate(
        proof,
        (
            reports[0],
            report(
                proof,
                kind="detailed",
                status=CheckStatus.FAIL,
                thread_id="failed-thread",
                rule_ids=("rule-2",),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_rule_ids=("rule-1", "rule-2"),
    )

    assert "rule_not_passed:rule-2" in missing.reasons
    assert "rule_not_passed:rule-2" in uncertain.reasons
    assert "rule_not_passed:rule-2" in failed.reasons


def test_multiple_reports_can_jointly_cover_all_verification_rules() -> None:
    proof = candidate()
    decision = decide_candidate(
        proof,
        (
            report(
                proof,
                kind="structural",
                status=CheckStatus.PASS,
                thread_id="structural-thread",
                rule_ids=("rule-1",),
            ),
            report(
                proof,
                kind="detailed",
                status=CheckStatus.PASS,
                thread_id="detailed-thread",
                rule_ids=("rule-2",),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_rule_ids=("rule-1", "rule-2"),
    )

    assert decision.passed is True
    assert tuple(item.rule_id for item in decision.rule_coverage) == (
        "rule-1",
        "rule-2",
    )


def test_empty_verification_rules_remain_compatible() -> None:
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
        prover_external_thread_id="codex-writer-thread",
        required_rule_ids=(),
    )

    assert decision.passed is True
    assert decision.rule_coverage == ()


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

    decision = decide_candidate(
        proof,
        reports,
        prover_external_thread_id="codex-writer-thread",
        require_citation=True,
    )

    assert decision.passed is False
    assert decision.reasons == ("missing:citation",)


def test_citation_pass_requires_exact_frozen_evidence_coverage() -> None:
    proof = candidate()
    source = evidence()
    base = (
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
    missing = decide_candidate(
        proof,
        base
        + (
            report(
                proof,
                kind="citation",
                status=CheckStatus.PASS,
                thread_id="citation-thread-missing",
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_evidence=(source,),
    )
    unknown = decide_candidate(
        proof,
        base
        + (
            report(
                proof,
                kind="citation",
                status=CheckStatus.PASS,
                thread_id="citation-thread-unknown",
                evidence_ids=("evidence-1", "evidence-unknown"),
                citation_support=(
                    citation_support(
                        proof,
                        source,
                        evidence_id="evidence-unknown",
                    ),
                ),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_evidence=(source,),
    )
    passed = decide_candidate(
        proof,
        base
        + (
            report(
                proof,
                kind="citation",
                status=CheckStatus.PASS,
                thread_id="citation-thread-pass",
                evidence_ids=("evidence-1",),
                citation_support=(citation_support(proof, source),),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_evidence=(source,),
    )

    assert "citation_missing_evidence:evidence-1" in missing.reasons
    assert "citation_unknown_evidence:evidence-unknown" in unknown.reasons
    assert passed.passed is True


def test_citation_free_text_or_evidence_ids_cannot_replace_structured_support() -> None:
    proof = candidate()
    source = evidence()
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
            report(
                proof,
                kind="citation",
                status=CheckStatus.PASS,
                thread_id="citation-thread",
                evidence_ids=(source.id,),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_evidence=(source,),
    )

    assert decision.passed is False
    assert decision.reasons == ("citation_missing_evidence:evidence-1",)


def test_citation_support_quotes_frozen_proof_and_evidence_exactly() -> None:
    proof = candidate()
    source = evidence()
    base = (
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

    wrong_proof_span = decide_candidate(
        proof,
        base
        + (
            report(
                proof,
                kind="citation",
                status=CheckStatus.PASS,
                thread_id="citation-proof-span",
                citation_support=(
                    citation_support(
                        proof,
                        source,
                        proof_span="This claim is absent from the frozen proof.",
                    ),
                ),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_evidence=(source,),
    )
    wrong_excerpt = decide_candidate(
        proof,
        base
        + (
            report(
                proof,
                kind="citation",
                status=CheckStatus.PASS,
                thread_id="citation-excerpt",
                citation_support=(
                    citation_support(
                        proof,
                        source,
                        excerpt="This excerpt is absent from the frozen evidence.",
                    ),
                ),
            ),
        ),
        prover_external_thread_id="codex-writer-thread",
        required_evidence=(source,),
    )

    assert any(
        reason.startswith("citation_proof_span_mismatch:")
        for reason in wrong_proof_span.reasons
    )
    assert any(
        reason.startswith("citation_excerpt_mismatch:")
        for reason in wrong_excerpt.reasons
    )
