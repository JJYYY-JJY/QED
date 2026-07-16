from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from qed.config import QEDConfig
from qed.schemas import (
    CheckStatus,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    sha256_text,
)
from qed.store import (
    ConflictError,
    InvalidTransitionError,
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
        input_sha256=INPUT_SHA,
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
        store.add_thread(
            "prover-thread",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=provenance("prover-thread"),
        )
        store.create_candidate(proof, thread_id="prover-thread")
        store.seal_candidate(proof.id)
        store.add_thread(
            "forked-verifier",
            run_id="run-1",
            role="verifier",
            model="gpt-5.6-sol",
            provenance=provenance("forked-verifier"),
            parent_thread_id="prover-thread",
        )
        store.add_thread(
            "fresh-verifier",
            run_id="run-1",
            role="verifier",
            model="gpt-5.6-sol",
            provenance=provenance("fresh-verifier"),
        )

        def report(thread_id: str, report_id: str) -> VerificationReport:
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
