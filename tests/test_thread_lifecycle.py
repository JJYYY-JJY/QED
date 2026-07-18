from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from qed.config import QEDConfig
from qed.inputs import RunInput
from qed.schemas import (
    CheckStatus,
    Evidence,
    Plan,
    PlanStep,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    sha256_text,
)
from qed.store import (
    ConflictError,
    InvalidTransitionError,
    RunStage,
    RunStatus,
    RunStore,
    ThreadStatus,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
INPUT_SHA = sha256_text("problem")


def provenance(thread_id: str) -> Provenance:
    return Provenance(
        source="codex",
        source_id=thread_id,
        model="gpt-5.6-sol",
        runtime_version="0.144.5",
        prompt_version="v1",
        captured_at=NOW,
    )


def running_store(path: Path) -> RunStore:
    store = RunStore(path)
    store.create_run(
        "run-1",
        config=QEDConfig(),
        run_input=RunInput(problem="problem"),
        provenance=provenance("intake"),
    )
    store.transition_run("run-1", RunStatus.RUNNING)
    return store


def test_thread_has_one_way_terminal_lifecycle_and_events(tmp_path: Path) -> None:
    with running_store(tmp_path / "qed.sqlite3") as store:
        store.add_thread(
            "planner-thread",
            run_id="run-1",
            role="planner",
            model="gpt-5.6-sol",
            provenance=provenance("planner-thread"),
        )

        completed = store.transition_thread("planner-thread", ThreadStatus.COMPLETED)

        assert completed.status is ThreadStatus.COMPLETED
        assert store.list_events("run-1")[-1].event_type == "thread.status_changed"
        with pytest.raises(InvalidTransitionError, match="completed -> failed"):
            store.transition_thread("planner-thread", ThreadStatus.FAILED)


def test_verification_requires_a_fresh_verifier_thread(tmp_path: Path) -> None:
    proof_text = "A complete proof."
    proof = ProofCandidate(
        id="candidate-1",
        run_id="run-1",
        plan_id="plan-1",
        attempt=1,
        proof=proof_text,
        proof_sha256=sha256_text(proof_text),
        provenance=provenance("prover-thread"),
        created_at=NOW,
    )

    with running_store(tmp_path / "qed.sqlite3") as store:
        run_input = store.get_run_input("run-1")
        evidence = Evidence(
            id="evidence-1",
            kind="note",
            title="Problem note",
            content="A note.",
            content_sha256=sha256_text("A note."),
            provenance=provenance("literature-thread"),
        )
        plan = Plan(
            id="plan-1",
            problem_sha256=run_input.sha256,
            strategy="Direct proof.",
            steps=(
                PlanStep(
                    id="step-1",
                    statement="Prove the claim.",
                    rationale="It is the target.",
                    success_criteria=("Claim proved.",),
                ),
            ),
            provenance=provenance("planner-thread"),
            created_at=NOW,
        )
        store.transition_stage("run-1", RunStage.LITERATURE)
        store.add_evidence("run-1", evidence)
        store.transition_stage("run-1", RunStage.PLANNING)
        store.add_plan("run-1", plan)
        store.transition_stage("run-1", RunStage.PROVING)
        store.add_thread(
            "prover-thread",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=provenance("prover-thread"),
            external_thread_id="codex-prover-thread",
        )
        store.add_thread(
            "planner-thread",
            run_id="run-1",
            role="planner",
            model="gpt-5.6-sol",
            provenance=provenance("planner-thread"),
        )
        with pytest.raises(ConflictError, match="prover thread"):
            store.create_candidate(proof, thread_id="planner-thread")
        with pytest.raises(ConflictError, match="typed plan"):
            store.create_candidate(
                proof.model_copy(update={"plan_id": "missing-plan"}),
                thread_id="prover-thread",
            )
        store.create_candidate(proof, thread_id="prover-thread")
        store.seal_candidate(proof.id)
        store.transition_stage("run-1", RunStage.VERIFICATION)
        store.add_thread(
            "forked-verifier",
            run_id="run-1",
            role="verifier",
            model="gpt-5.6-sol",
            provenance=provenance("forked-verifier"),
            parent_thread_id="prover-thread",
            external_thread_id="codex-forked-verifier",
        )
        store.add_thread(
            "fresh-verifier",
            run_id="run-1",
            role="verifier",
            model="gpt-5.6-sol",
            provenance=provenance("fresh-verifier"),
            external_thread_id="codex-fresh-verifier",
        )
        with pytest.raises(ConflictError, match="thread already exists|invalid references"):
            store.add_thread(
                "alias-of-fresh-verifier",
                run_id="run-1",
                role="verifier",
                model="gpt-5.6-sol",
                provenance=provenance("alias-of-fresh-verifier"),
                external_thread_id="codex-fresh-verifier",
            )

        def report(thread_id: str, report_id: str) -> VerificationReport:
            external_thread_ids = {
                "prover-thread": "codex-prover-thread",
                "forked-verifier": "codex-forked-verifier",
                "fresh-verifier": "codex-fresh-verifier",
            }
            return VerificationReport(
                id=report_id,
                candidate_id=proof.id,
                candidate_sha256=proof.proof_sha256,
                kind="structural",
                checks=(
                    VerificationCheck(
                        id=f"check-{report_id}",
                        category="coverage",
                        status=CheckStatus.PASS,
                        summary="All claims are covered.",
                    ),
                ),
                verifier_thread_id=thread_id,
                verifier_external_thread_id=external_thread_ids[thread_id],
                provenance=provenance(thread_id),
                created_at=NOW,
            )

        with pytest.raises(ConflictError, match="fresh verifier"):
            store.add_verification("run-1", report("prover-thread", "wrong-role"))
        with pytest.raises(ConflictError, match="fresh verifier"):
            store.add_verification("run-1", report("forked-verifier", "forked"))

        saved = store.add_verification(
            "run-1", report("fresh-verifier", "independent")
        )
        assert saved.thread_id == "fresh-verifier"
