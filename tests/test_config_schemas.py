from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from qed.config import BudgetPolicy, QEDConfig, SandboxPolicy
from qed.schemas import (
    Adjudication,
    CheckStatus,
    Event,
    Evidence,
    Finding,
    Manifest,
    ManifestArtifact,
    Plan,
    PlanStep,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    VerificationVerdict,
    canonical_json,
    canonical_sha256,
    sha256_text,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
MAPPING_SHA256 = "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"


def provenance() -> Provenance:
    return Provenance(
        source="codex",
        source_id="thread-1",
        model="gpt-5.6-sol",
        runtime_version="0.1.0",
        prompt_version="proof-v1",
        captured_at=NOW,
    )


def test_canonical_json_and_sha256_match_known_vectors() -> None:
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert canonical_sha256({"b": 2, "a": 1}) == MAPPING_SHA256
    assert sha256_text("abc") == ABC_SHA256


def test_config_is_codex_only_strict_and_safe_by_default() -> None:
    config = QEDConfig()

    assert config.model == "gpt-5.6-sol"
    assert config.effort == "auto"
    assert config.backend == "auto"
    assert config.sandbox.literature == "read-only"
    assert config.sandbox.planner == "read-only"
    assert config.sandbox.prover == "read-only"
    assert config.sandbox.verifier == "read-only"
    assert config.sandbox.adjudicator == "read-only"
    assert config.sandbox.approval == "never"
    assert config.search.allowed_roles == ("literature", "citation")
    assert config.budgets.turn_retries == 2
    assert BudgetPolicy(turn_retries=0).turn_retries == 0

    # Effort remains capability-driven rather than an enum frozen in this repository.
    assert QEDConfig(effort="future-supported-effort").effort == "future-supported-effort"
    assert QEDConfig(backend="exec").backend == "exec"
    with pytest.raises(ValidationError):
        SandboxPolicy(prover="workspace-write")

    with pytest.raises(ValidationError):
        QEDConfig(effort="")
    with pytest.raises(ValidationError):
        QEDConfig(effort=1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        QEDConfig(provider="claude")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        QEDConfig(backend="silent-fallback")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prover", "danger-full-access"),
        ("verifier", "workspace-write"),
        ("approval", "dangerously-bypass-approvals-and-sandbox"),
        ("approval", "yolo"),
    ],
)
def test_sandbox_policy_rejects_unsafe_modes(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy(**{field: value})


@pytest.mark.parametrize("approval", ["untrusted", "on-request"])
def test_sandbox_approval_is_not_a_configurable_no_op(approval: str) -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy(approval=approval)  # type: ignore[arg-type]


def test_strict_schemas_validate_a_complete_artifact_chain() -> None:
    source = provenance()
    evidence_text = "The cited theorem gives the required estimate."
    evidence = Evidence(
        id="evidence-1",
        kind="theorem",
        title="A useful theorem",
        content=evidence_text,
        content_sha256=sha256_text(evidence_text),
        provenance=source,
    )
    step = PlanStep(
        id="step-1",
        statement="Apply the theorem to the normalized solution.",
        rationale="Its hypotheses match the frozen problem.",
        success_criteria=("The target estimate follows with explicit constants.",),
        evidence_ids=(evidence.id,),
        key_step=True,
    )
    plan = Plan(
        id="plan-1",
        problem_sha256=ABC_SHA256,
        strategy="Normalize, apply the theorem, and restore scaling.",
        steps=(step,),
        provenance=source,
        created_at=NOW,
    )
    proof_text = "By the cited theorem, the normalized estimate holds; scaling proves the claim."
    candidate = ProofCandidate(
        id="candidate-1",
        run_id="run-1",
        plan_id=plan.id,
        attempt=1,
        proof=proof_text,
        proof_sha256=sha256_text(proof_text),
        evidence_ids=(evidence.id,),
        provenance=source,
        created_at=NOW,
    )
    check = VerificationCheck(
        id="check-1",
        category="logical-correctness",
        status=CheckStatus.FAIL,
        summary="The scaling exponent is not justified.",
    )
    finding = Finding(
        id="finding-1",
        check_id=check.id,
        severity="major",
        summary="Missing scaling argument",
        detail="The final rescaling changes the claimed exponent.",
        proof_span="scaling proves the claim",
    )
    report = VerificationReport(
        id="verification-1",
        candidate_id=candidate.id,
        candidate_sha256=candidate.proof_sha256,
        kind="detailed",
        checks=(check,),
        findings=(finding,),
        verifier_thread_id="thread-verifier-1",
        provenance=source,
        created_at=NOW,
    )
    adjudication = Adjudication(
        id="adjudication-1",
        candidate_id=candidate.id,
        report_ids=(report.id,),
        outcome="revise_proof",
        rationale="Resolve the major finding before another independent verification.",
        provenance=source,
        created_at=NOW,
    )
    payload = {"candidate_id": candidate.id, "verdict": report.verdict.value}
    event = Event(
        run_id=candidate.run_id,
        seq=1,
        event_type="verification.completed",
        stage="verification",
        payload=payload,
        payload_sha256=canonical_sha256(payload),
        created_at=NOW,
    )
    manifest = Manifest(
        run_id=candidate.run_id,
        run_status="running",
        input_sha256=ABC_SHA256,
        config_sha256=MAPPING_SHA256,
        runtime_version="0.1.0",
        prompt_versions={"proof": "proof-v1"},
        candidate_hashes=(candidate.proof_sha256,),
        verification_hashes=(canonical_sha256(report),),
        artifacts=(
            ManifestArtifact(
                id="proof",
                kind="proof",
                sha256=candidate.proof_sha256,
                media_type="text/markdown",
            ),
        ),
        first_event_seq=1,
        last_event_seq=event.seq,
        generated_at=NOW,
    )

    assert report.verdict is VerificationVerdict.FAIL
    assert adjudication.outcome == "revise_proof"
    assert event.payload["verdict"] == "fail"
    assert manifest.artifacts[0].sha256 == candidate.proof_sha256


def test_schema_hashes_and_plan_dependencies_cannot_lie() -> None:
    source = provenance()

    with pytest.raises(ValidationError, match="content_sha256"):
        Evidence(
            id="evidence-1",
            kind="theorem",
            title="Theorem",
            content="abc",
            content_sha256="0" * 64,
            provenance=source,
        )

    with pytest.raises(ValidationError, match="unknown dependency"):
        Plan(
            id="plan-1",
            problem_sha256=ABC_SHA256,
            strategy="A strategy",
            steps=(
                PlanStep(
                    id="step-1",
                    statement="A claim",
                    rationale="Needed for the target.",
                    success_criteria=("Claim is proved.",),
                    dependencies=("missing-step",),
                ),
            ),
            provenance=source,
            created_at=NOW,
        )

    with pytest.raises(ValidationError):
        PlanStep(
            id="step-1",
            statement="A claim",
            rationale="Needed for the target.",
            success_criteria=("Claim is proved.",),
            key_step=1,  # type: ignore[arg-type]
        )


def test_verification_findings_are_consistent_and_fail_closed() -> None:
    source = provenance()
    passed = VerificationCheck(
        id="check-1",
        category="correctness",
        status=CheckStatus.PASS,
        summary="The inference is valid.",
    )
    failed = passed.model_copy(update={"status": CheckStatus.FAIL})
    major = Finding(
        id="finding-1",
        check_id=passed.id,
        severity="major",
        summary="Invalid inference",
        detail="The implication does not follow.",
    )

    with pytest.raises(ValidationError, match="major.*failed check"):
        VerificationReport(
            id="verification-1",
            candidate_id="candidate-1",
            candidate_sha256=ABC_SHA256,
            kind="detailed",
            checks=(passed,),
            findings=(major,),
            verifier_thread_id="verifier-1",
            provenance=source,
            created_at=NOW,
        )

    with pytest.raises(ValidationError, match="failed checks require findings"):
        VerificationReport(
            id="verification-2",
            candidate_id="candidate-1",
            candidate_sha256=ABC_SHA256,
            kind="detailed",
            checks=(failed,),
            verifier_thread_id="verifier-1",
            provenance=source,
            created_at=NOW,
        )

    report = VerificationReport(
        id="verification-3",
        candidate_id="candidate-1",
        candidate_sha256=ABC_SHA256,
        kind="detailed",
        checks=(failed,),
        findings=(major,),
        verifier_thread_id="verifier-1",
        provenance=source,
        created_at=NOW,
    )
    assert report.verdict is VerificationVerdict.FAIL
