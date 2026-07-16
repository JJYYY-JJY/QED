from datetime import UTC, datetime

import pytest

from qed.config import QEDConfig
from qed.schemas import (
    CheckStatus,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    canonical_sha256,
    sha256_text,
)
from qed.store import (
    ImmutableRecordError,
    InvalidTransitionError,
    RunStage,
    RunStatus,
    RunStore,
)

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
REQUIRED_TABLES = {
    "artifacts",
    "candidates",
    "events",
    "runs",
    "schema_metadata",
    "stage_outputs",
    "threads",
    "verifications",
}


def provenance(source_id: str = "thread-1") -> Provenance:
    return Provenance(
        source="codex",
        source_id=source_id,
        model="gpt-5.6-sol",
        runtime_version="0.1.0",
        prompt_version="proof-v1",
        captured_at=NOW,
    )


def candidate(proof: str) -> ProofCandidate:
    return ProofCandidate(
        id="candidate-1",
        run_id="run-1",
        plan_id="plan-1",
        attempt=1,
        proof=proof,
        proof_sha256=sha256_text(proof),
        evidence_ids=("evidence-1",),
        provenance=provenance("thread-prover-1"),
        created_at=NOW,
    )


def verification(summary: str) -> VerificationReport:
    return VerificationReport(
        id="verification-1",
        candidate_id="candidate-1",
        candidate_sha256=sha256_text("A complete proof."),
        kind="structural",
        checks=(
            VerificationCheck(
                id="check-1",
                category="coverage",
                status=CheckStatus.PASS,
                summary=summary,
            ),
        ),
        verifier_thread_id="thread-verifier-1",
        provenance=provenance("thread-verifier-1"),
        created_at=NOW,
    )


def test_store_persists_schema_lifecycle_and_monotonic_events(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"
    config = QEDConfig()

    with RunStore(database) as store:
        info = store.info()
        assert info.schema_version == 1
        assert info.journal_mode == "wal"
        assert info.foreign_keys is True
        assert set(info.tables) == REQUIRED_TABLES

        created = store.create_run(
            "run-1",
            config=config,
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        assert created.status is RunStatus.CREATED
        assert created.stage is RunStage.INTAKE
        assert created.config_sha256 == config.sha256
        assert created.input_sha256 == ABC_SHA256
        assert created.provenance == provenance()

        running = store.transition_run("run-1", RunStatus.RUNNING)
        assert running.status is RunStatus.RUNNING
        literature = store.transition_stage("run-1", RunStage.LITERATURE)
        assert literature.stage is RunStage.LITERATURE

        custom = store.append_event(
            "run-1",
            event_type="literature.source_found",
            stage=RunStage.LITERATURE,
            payload={"evidence_id": "evidence-1"},
        )
        assert custom.seq == 4
        assert [event.seq for event in store.list_events("run-1")] == [1, 2, 3, 4]

    with RunStore(database) as reopened:
        restored = reopened.get_run("run-1")
        assert restored.status is RunStatus.RUNNING
        assert restored.stage is RunStage.LITERATURE
        assert restored.config == config
        assert reopened.list_runs() == (restored,)

        next_event = reopened.append_event(
            "run-1",
            event_type="literature.completed",
            stage=RunStage.LITERATURE,
            payload={},
        )
        assert next_event.seq == 5
        assert [event.seq for event in reopened.list_events("run-1", after_seq=3)] == [4, 5]


def test_invalid_transitions_are_rejected_and_cancelled_run_resumes_after_reopen(
    tmp_path,
) -> None:
    database = tmp_path / "qed.sqlite3"

    with RunStore(database) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )

        with pytest.raises(InvalidTransitionError, match="created -> completed"):
            store.transition_run("run-1", RunStatus.COMPLETED)

        store.transition_run("run-1", RunStatus.RUNNING)
        with pytest.raises(InvalidTransitionError, match="intake -> proving"):
            store.transition_stage("run-1", RunStage.PROVING)

        cancelling = store.request_cancel("run-1")
        assert cancelling.status is RunStatus.CANCELLING
        assert cancelling.cancellation_requested is True
        assert cancelling.resumable is False

        cancelled = store.acknowledge_cancel("run-1")
        assert cancelled.status is RunStatus.CANCELLED
        assert cancelled.cancellation_requested is True
        assert cancelled.resumable is True

    with RunStore(database) as reopened:
        resumed = reopened.resume_run("run-1")
        assert resumed.status is RunStatus.RUNNING
        assert resumed.stage is RunStage.INTAKE
        assert resumed.cancellation_requested is False
        assert resumed.resumable is False
        assert resumed.resume_count == 1

        with pytest.raises(InvalidTransitionError, match="not resumable"):
            reopened.resume_run("run-1")

        assert [event.seq for event in reopened.list_events("run-1")] == [1, 2, 3, 4, 5]


def test_sealed_candidates_and_verifications_remain_immutable_across_resume(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"

    with RunStore(database) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        store.add_thread(
            "thread-prover-1",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=provenance("thread-prover-1"),
        )
        store.add_thread(
            "thread-verifier-1",
            run_id="run-1",
            role="verifier",
            model="gpt-5.6-sol",
            provenance=provenance("thread-verifier-1"),
        )

        draft = store.create_candidate(
            candidate("An incomplete draft."),
            thread_id="thread-prover-1",
        )
        assert draft.sealed_at is None
        revised = store.update_candidate(candidate("A complete proof."))
        assert revised.candidate.proof == "A complete proof."

        sealed = store.seal_candidate("candidate-1")
        assert sealed.sealed_at is not None
        with pytest.raises(ImmutableRecordError, match="sealed candidate"):
            store.update_candidate(candidate("A mutation after sealing."))

        report = store.add_verification("run-1", verification("Every claim is covered."))
        with pytest.raises(ImmutableRecordError, match="verification is immutable"):
            store.update_verification(verification("Mutated verifier report."))
        with pytest.raises(ImmutableRecordError, match="verification is immutable"):
            store.delete_verification("verification-1")

        store.request_cancel("run-1")
        store.acknowledge_cancel("run-1")

    with RunStore(database) as reopened:
        reopened.resume_run("run-1")

        assert reopened.get_candidate("candidate-1") == sealed
        assert reopened.list_candidates("run-1") == (sealed,)
        assert reopened.get_verification("verification-1") == report
        assert reopened.list_verifications("run-1") == (report,)


def test_snapshot_exposes_versioned_hashed_provenance_for_export(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"
    output = {"classification": "hard", "evidence_ids": ["evidence-1"]}

    with RunStore(database) as store:
        run = store.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        stage_output = store.add_stage_output(
            "output-1",
            run_id="run-1",
            stage=RunStage.LITERATURE,
            kind="literature_result",
            content=output,
            provenance=provenance("thread-literature-1"),
        )
        thread = store.add_thread(
            "thread-literature-1",
            run_id="run-1",
            role="literature",
            model="gpt-5.6-sol",
            provenance=provenance("thread-literature-1"),
        )
        artifact = store.add_artifact(
            "artifact-1",
            run_id="run-1",
            kind="problem",
            relative_path="inputs/problem.tex",
            media_type="text/x-tex",
            sha256=ABC_SHA256,
            size_bytes=3,
            provenance=provenance("input-upload"),
        )

        assert stage_output.content_sha256 == canonical_sha256(output)
        assert stage_output.provenance_sha256 == canonical_sha256(stage_output.provenance)
        assert artifact.sha256 == ABC_SHA256
        assert artifact.schema_version == 1

    with RunStore(database) as reopened:
        snapshot = reopened.snapshot("run-1")

        assert snapshot.run == run
        assert snapshot.stage_outputs == (stage_output,)
        assert snapshot.threads == (thread,)
        assert snapshot.candidates == ()
        assert snapshot.verifications == ()
        assert snapshot.artifacts == (artifact,)
        assert snapshot.events == reopened.list_events("run-1")
        assert reopened.list_stage_outputs("run-1") == (stage_output,)
        assert reopened.list_artifacts("run-1") == (artifact,)
