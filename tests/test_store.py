import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from qed.config import BudgetPolicy, QEDConfig
from qed.inputs import RunInput
from qed.schemas import (
    Adjudication,
    CheckStatus,
    Evidence,
    Plan,
    PlanStep,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    canonical_sha256,
    sha256_text,
)
from qed.store import (
    ConflictError,
    ExecutionToken,
    ImmutableRecordError,
    InvalidTransitionError,
    NotFoundError,
    RunStage,
    RunStatus,
    RunStore,
    StoreIntegrityError,
)

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
REQUIRED_TABLES = {
    "artifacts",
    "adjudications",
    "candidate_decisions",
    "cancel_commands",
    "candidates",
    "evidence",
    "events",
    "execution_segments",
    "plans",
    "resume_commands",
    "runtime_resolutions",
    "runs",
    "run_inputs",
    "schema_metadata",
    "start_commands",
    "stage_outputs",
    "threads",
    "turn_inputs",
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
    run_input = RunInput(problem="abc")

    with RunStore(database) as store:
        info = store.info()
        assert info.schema_version == 2
        assert info.journal_mode == "wal"
        assert info.foreign_keys is True
        assert set(info.tables) == REQUIRED_TABLES

        created = store.create_run(
            "run-1",
            config=config,
            run_input=run_input,
            provenance=provenance(),
        )
        assert created.status is RunStatus.CREATED
        assert created.stage is RunStage.INTAKE
        assert created.config_sha256 == config.sha256
        assert created.input_sha256 == run_input.sha256
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


def test_store_migrates_additive_database_schema_v1_to_v2(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database):
        pass

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE turn_inputs")
        connection.execute("DROP TABLE runtime_resolutions")
        connection.execute("DROP TABLE resume_commands")
        connection.execute("DROP TABLE start_commands")
        connection.execute("DROP TABLE cancel_commands")
        connection.execute(
            "UPDATE schema_metadata SET value = '1' WHERE key = 'schema_version'"
        )

    with RunStore(database) as migrated:
        assert migrated.info().schema_version == 2
        assert set(migrated.info().tables) == REQUIRED_TABLES


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


def test_start_command_claim_is_durable_and_exclusive_across_connections(
    tmp_path,
) -> None:
    database = tmp_path / "qed.sqlite3"
    first = RunStore(database)
    second = RunStore(database)
    try:
        first.create_run(
            "run-claim",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )

        claimed = first.start_run_command(
            "run-claim",
            idempotency_key="start-claim-1",
        )
        replayed = second.start_run_command(
            "run-claim",
            idempotency_key="start-claim-1",
        )

        assert claimed.replayed is False
        assert replayed.replayed is True
        assert claimed.accepted_status is RunStatus.CREATED
        assert replayed.accepted_status is RunStatus.CREATED
        with pytest.raises(InvalidTransitionError, match="cannot start"):
            second.start_run_command(
                "run-claim",
                idempotency_key="start-claim-2",
            )
    finally:
        second.close()
        first.close()


def test_sealed_candidates_and_verifications_remain_immutable_across_resume(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"
    run_input = RunInput(problem="abc")
    evidence_text = "Evidence."
    evidence = Evidence(
        id="evidence-1",
        kind="note",
        title="Evidence",
        content=evidence_text,
        content_sha256=sha256_text(evidence_text),
        provenance=provenance("thread-literature-1"),
    )
    plan = Plan(
        id="plan-1",
        problem_sha256=run_input.sha256,
        strategy="Direct proof.",
        steps=(
            PlanStep(
                id="step-1",
                statement="Prove it.",
                rationale="It is the target.",
                success_criteria=("The target is proved.",),
                evidence_ids=(evidence.id,),
            ),
        ),
        provenance=provenance("thread-planner-1"),
        created_at=NOW,
    )

    with RunStore(database) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=run_input,
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        store.transition_stage("run-1", RunStage.LITERATURE)
        store.add_evidence("run-1", evidence)
        store.transition_stage("run-1", RunStage.PLANNING)
        store.add_plan("run-1", plan)
        store.transition_stage("run-1", RunStage.PROVING)
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
            external_thread_id="codex-verifier-1",
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

        store.transition_stage("run-1", RunStage.VERIFICATION)
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
            stage=RunStage.INTAKE,
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


def test_one_store_serializes_concurrent_writes(tmp_path) -> None:
    with RunStore(tmp_path / "qed.sqlite3") as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )

        def append(index: int) -> int:
            return store.append_event(
                "run-1",
                event_type="test.concurrent",
                stage=RunStage.INTAKE,
                payload={"index": index},
            ).seq

        with ThreadPoolExecutor(max_workers=16) as pool:
            sequences = tuple(pool.map(append, range(32)))

        assert sorted(sequences) == list(range(2, 34))
        assert [event.seq for event in store.list_events("run-1")] == list(range(1, 34))


def test_snapshot_uses_one_sqlite_read_transaction(tmp_path, monkeypatch) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database) as reader, RunStore(database) as writer:
        reader.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        original = RunStore._run_from_row
        inserted = False

        def insert_after_snapshot_starts(row):
            nonlocal inserted
            run = original(row)
            if not inserted:
                inserted = True
                writer.append_event(
                    "run-1",
                    event_type="test.after-snapshot-start",
                    stage=RunStage.INTAKE,
                    payload={},
                )
            return run

        monkeypatch.setattr(RunStore, "_run_from_row", staticmethod(insert_after_snapshot_starts))
        snapshot = reader.snapshot("run-1")

        assert [event.seq for event in snapshot.events] == [1]
        assert [event.seq for event in writer.list_events("run-1")] == [1, 2]


def test_run_input_body_is_persisted_by_content_address_and_recovers_independently(
    tmp_path,
) -> None:
    database = tmp_path / "qed.sqlite3"
    run_input = RunInput(
        problem="Prove that there are infinitely many primes.",
        prove_guidance="Use contradiction.",
        verification_rules=("Check the quantified conclusion.",),
    )

    with RunStore(database) as store:
        run = store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=run_input,
            provenance=provenance(),
        )
        assert run.input_sha256 == run_input.sha256
        assert store.get_run_input("run-1") == run_input

    with RunStore(database) as reopened:
        assert reopened.get_run_input("run-1") == run_input
        assert reopened.snapshot("run-1").run_input == run_input


def test_invalid_or_mismatched_input_hash_is_rejected_before_a_transaction(tmp_path) -> None:
    run_input = RunInput(problem="A frozen problem")
    with RunStore(tmp_path / "qed.sqlite3") as store:
        with pytest.raises(ValueError, match="input_sha256"):
            store.create_run(
                "run-mismatch",
                config=QEDConfig(),
                run_input=run_input,
                input_sha256="0" * 64,
                provenance=provenance(),
            )
        with pytest.raises(ValueError, match="SHA-256"):
            store.create_run(
                "run-invalid",
                config=QEDConfig(),
                input_sha256="not-a-sha",  # type: ignore[arg-type]
                provenance=provenance(),
            )

        with pytest.raises(NotFoundError):
            store.get_run("run-mismatch")
        with pytest.raises(NotFoundError):
            store.get_run("run-invalid")


@pytest.mark.parametrize(
    "relative_path",
    [
        "../escape",
        "/absolute",
        "nested\\windows",
        "nested//file",
        "nested/./file",
        ".",
        "",
    ],
)
def test_artifact_metadata_is_validated_before_writing(
    tmp_path, relative_path: str
) -> None:
    with RunStore(tmp_path / "qed.sqlite3") as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        before = store.list_events("run-1")
        with pytest.raises(ValueError):
            store.add_artifact(
                "artifact-1",
                run_id="run-1",
                kind="proof",
                media_type="text/plain",
                sha256=ABC_SHA256,
                size_bytes=3,
                relative_path=relative_path,
                provenance=provenance(),
            )
        assert store.list_events("run-1") == before


def test_invalid_ids_hashes_and_sizes_do_not_leave_poisoned_rows(tmp_path) -> None:
    with RunStore(tmp_path / "qed.sqlite3") as store:
        with pytest.raises(ValueError, match="run_id"):
            store.create_run(
                "../run",
                config=QEDConfig(),
                input_sha256=ABC_SHA256,
                provenance=provenance(),
            )
        with pytest.raises(ValueError, match="run_id"):
            store.create_run(
                "run:colon",
                config=QEDConfig(),
                input_sha256=ABC_SHA256,
                provenance=provenance(),
            )
        store.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        for sha256, size in (("not-a-sha", 3), (ABC_SHA256, -1), (ABC_SHA256, True)):
            with pytest.raises(ValueError):
                store.add_artifact(
                    "artifact-1",
                    run_id="run-1",
                    kind="proof",
                    media_type="text/plain",
                    sha256=sha256,  # type: ignore[arg-type]
                    size_bytes=size,  # type: ignore[arg-type]
                    provenance=provenance(),
                )
        assert store.list_artifacts("run-1") == ()


def test_denormalized_hashes_are_rechecked_when_rows_are_read(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )

    connection = sqlite3.connect(database)
    connection.execute("UPDATE runs SET config_sha256 = ? WHERE id = ?", ("0" * 64, "run-1"))
    connection.commit()
    connection.close()

    with (
        RunStore(database) as reopened,
        pytest.raises(StoreIntegrityError, match="config hash"),
    ):
        reopened.get_run("run-1")


class FrozenClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def test_execution_lease_fences_stale_workers_and_is_idempotent(tmp_path) -> None:
    clock = FrozenClock()
    with RunStore(tmp_path / "qed.sqlite3", clock=clock) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=RunInput(problem="A problem"),
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        first = store.acquire_execution(
            "run-1",
            segment_id="segment-1",
            worker_id="worker-1",
            lease_token="secret-token-1",
            lease_seconds=30,
            runtime_version="0.144.5",
            runtime_resolution_sha256=ABC_SHA256,
        )
        assert first.version == 1
        assert first.runtime_version == "0.144.5"
        assert first.runtime_resolution_sha256 == ABC_SHA256
        assert store.acquire_execution(
            "run-1",
            segment_id="segment-1",
            worker_id="worker-1",
            lease_token="secret-token-1",
            lease_seconds=30,
            runtime_version="0.144.5",
            runtime_resolution_sha256=ABC_SHA256,
        ) == first
        first_token = ExecutionToken(
            segment_id=first.id,
            version=first.version,
            lease_token="secret-token-1",
        )
        with pytest.raises(ConflictError, match="execution token"):
            store.add_stage_output(
                "output-without-token",
                run_id="run-1",
                stage=RunStage.INTAKE,
                kind="runtime_capabilities",
                content={},
                provenance=provenance(),
            )
        with pytest.raises(ConflictError, match="execution token"):
            store.add_artifact(
                "artifact-without-token",
                run_id="run-1",
                kind="checkpoint",
                media_type="application/json",
                sha256=ABC_SHA256,
                size_bytes=3,
                provenance=provenance(),
            )
        store.append_event(
            "run-1",
            event_type="worker.progress",
            stage=RunStage.INTAKE,
            payload={},
            execution=first_token,
        )

        clock.advance(31)
        second = store.acquire_execution(
            "run-1",
            segment_id="segment-2",
            worker_id="worker-2",
            lease_token="secret-token-2",
            lease_seconds=30,
        )
        assert second.version == 2
        with pytest.raises(ConflictError, match="stale execution"):
            store.append_event(
                "run-1",
                event_type="worker.stale",
                stage=RunStage.INTAKE,
                payload={},
                execution=first_token,
            )
        with pytest.raises(ConflictError, match="stale execution"):
            store.transition_stage(
                "run-1", RunStage.LITERATURE, execution=first_token
            )

        second_token = ExecutionToken(
            segment_id=second.id,
            version=second.version,
            lease_token="secret-token-2",
        )
        heartbeat = store.heartbeat_execution(second_token, lease_seconds=60)
        assert heartbeat.lease_expires_at == clock.now + timedelta(seconds=60)
        released = store.release_execution(second_token)
        assert released.released_at == clock.now
        assert store.release_execution(second_token) == released


def test_cancel_and_resume_commands_are_idempotent_and_rotate_execution_version(
    tmp_path,
) -> None:
    clock = FrozenClock()
    with RunStore(tmp_path / "qed.sqlite3", clock=clock) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=RunInput(problem="A problem"),
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        first = store.acquire_execution(
            "run-1",
            segment_id="segment-1",
            worker_id="worker-1",
            lease_token="secret-token-1",
        )
        token = ExecutionToken(
            segment_id=first.id,
            version=first.version,
            lease_token="secret-token-1",
        )
        store.add_thread(
            "active-prover",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=provenance("active-prover"),
            execution=token,
        )

        cancelling = store.request_cancel("run-1")
        assert store.request_cancel("run-1") == cancelling
        with pytest.raises(ConflictError, match="not writable while cancelling"):
            store.append_event(
                "run-1",
                event_type="worker.after-cancel",
                stage=RunStage.INTAKE,
                payload={},
                execution=token,
            )
        with pytest.raises(ConflictError, match="active execution lease"):
            store.acknowledge_cancel("run-1")
        cancelled = store.acknowledge_cancel("run-1", execution=token)
        assert store.acknowledge_cancel("run-1") == cancelled
        assert store.get_thread("active-prover").status.value == "cancelled"
        cancellation_events = store.list_events("run-1")
        assert any(
            event.event_type == "thread.status_changed"
            and event.payload["thread_id"] == "active-prover"
            and event.payload["to"] == "cancelled"
            for event in cancellation_events
        )
        assert any(
            event.event_type == "execution.released"
            and event.payload["segment_id"] == first.id
            for event in cancellation_events
        )
        resumed = store.resume_run("run-1", idempotency_key="resume-command-1")
        assert store.resume_run("run-1", idempotency_key="resume-command-1") == resumed
        assert resumed.resume_count == 1

        second = store.acquire_execution(
            "run-1",
            segment_id="segment-2",
            worker_id="worker-2",
            lease_token="secret-token-2",
        )
        assert second.version == first.version + 1


def test_execution_runtime_resolution_is_full_hashed_and_immutable(tmp_path) -> None:
    resolution = {
        "model": "gpt-5.6-sol",
        "selected_effort": "ultra",
        "proactive_multi_agent": True,
    }
    with RunStore(tmp_path / "qed.sqlite3") as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=RunInput(problem="A problem"),
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        lease = store.acquire_execution(
            "run-1",
            segment_id="segment-1",
            worker_id="worker-1",
            lease_token="secret-token-1",
            runtime_version="0.144.5",
        )
        token = ExecutionToken(
            segment_id=lease.id,
            version=lease.version,
            lease_token="secret-token-1",
        )

        assert store.record_execution_resolution(
            token,
            runtime_version="0.144.5",
            resolution=resolution,
        ) == resolution
        assert store.record_execution_resolution(
            token,
            runtime_version="0.144.5",
            resolution=resolution,
        ) == resolution
        assert store.get_execution_resolution(lease.id) == resolution
        assert (
            store.get_execution(lease.id).runtime_resolution_sha256
            == canonical_sha256(resolution)
        )

        with pytest.raises(ConflictError, match="immutable"):
            store.record_execution_resolution(
                token,
                runtime_version="0.144.5",
                resolution={**resolution, "selected_effort": "high"},
            )


def test_expired_execution_lease_cannot_block_external_cancellation(tmp_path) -> None:
    clock = FrozenClock()
    with RunStore(tmp_path / "qed.sqlite3", clock=clock) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=RunInput(problem="A problem"),
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        lease = store.acquire_execution(
            "run-1",
            segment_id="segment-1",
            worker_id="crashed-worker",
            lease_token="secret-token-1",
            lease_seconds=1,
        )
        token = ExecutionToken(
            segment_id=lease.id,
            version=lease.version,
            lease_token="secret-token-1",
        )
        store.add_thread(
            "orphaned-prover",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=provenance("orphaned-prover"),
            execution=token,
        )
        store.request_cancel("run-1")

        clock.advance(2)
        cancelled = store.acknowledge_cancel("run-1")

        assert cancelled.status is RunStatus.CANCELLED
        assert store.get_execution(lease.id).released_at == clock.now
        assert store.get_thread("orphaned-prover").status.value == "cancelled"


def test_persisted_turn_start_blocks_crash_recovery_until_terminal(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"
    now = [NOW]
    with RunStore(database, clock=lambda: now[0]) as store:
        store.create_run(
            "run-started",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        store.transition_run("run-started", RunStatus.RUNNING)
        lease = store.acquire_execution(
            "run-started",
            segment_id="segment-started",
            worker_id="worker-started",
            lease_token="secret-started",
            lease_seconds=1,
            runtime_version="test-runtime",
        )
        token = ExecutionToken(
            segment_id=lease.id,
            version=lease.version,
            lease_token="secret-started",
        )
        store.append_event(
            "run-started",
            event_type="runtime.turn_started",
            stage=RunStage.INTAKE,
            payload={
                "thread_id": "remote-thread",
                "turn_id": "remote-turn",
                "backend": "app_server",
                "turn_input_id": "turn-input-1",
                "attempt": 1,
            },
            execution=token,
        )

    now[0] += timedelta(seconds=2)
    with RunStore(database, clock=lambda: now[0]) as reopened:
        with pytest.raises(ConflictError, match="confirmed terminal"):
            reopened.acquire_execution(
                "run-started",
                segment_id="replacement-segment",
                worker_id="replacement-worker",
                lease_token="replacement-secret",
            )
        reopened.request_cancel("run-started")
        with pytest.raises(ConflictError, match="confirmed terminal"):
            reopened.acknowledge_cancel("run-started")

        reopened.record_turn_completed(
            "run-started",
            payload={
                "thread_id": "remote-thread",
                "turn_id": "remote-turn",
                "backend": "app_server",
                "status": "interrupted",
            },
            execution=token,
        )
        assert reopened.acknowledge_cancel("run-started").status is RunStatus.CANCELLED


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "interrupted"])
def test_expired_lease_cannot_override_unconfirmed_runtime_turn(
    tmp_path, terminal_status: str
) -> None:
    now = [NOW]
    with RunStore(tmp_path / "qed.sqlite3", clock=lambda: now[0]) as store:
        store.create_run(
            "run-unconfirmed",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=provenance(),
        )
        store.transition_run("run-unconfirmed", RunStatus.RUNNING)
        lease = store.acquire_execution(
            "run-unconfirmed",
            segment_id="segment-unconfirmed",
            worker_id="worker-unconfirmed",
            lease_token="secret-unconfirmed",
            lease_seconds=1,
            runtime_version="test-runtime",
        )
        token = ExecutionToken(
            segment_id=lease.id,
            version=lease.version,
            lease_token="secret-unconfirmed",
        )
        payload = {
            "thread_id": "remote-thread",
            "turn_id": "remote-turn",
            "backend": "app_server",
            "reason": "stream_ended",
        }
        store.record_turn_terminal_unconfirmed(
            "run-unconfirmed",
            payload=payload,
            execution=token,
        )
        with pytest.raises(ConflictError, match="confirmed terminal"):
            store.release_execution(token)

        now[0] += timedelta(seconds=2)

        with pytest.raises(ConflictError, match="confirmed terminal"):
            store.acquire_execution(
                "run-unconfirmed",
                segment_id="replacement-segment",
                worker_id="replacement-worker",
                lease_token="replacement-secret",
            )

        store.record_turn_completed(
            "run-unconfirmed",
            payload={
                "thread_id": "remote-thread",
                "turn_id": "remote-turn",
                "backend": "app_server",
                "status": terminal_status,
            },
            execution=token,
        )
        assert store.release_execution(token).released_at == now[0]
        store.request_cancel("run-unconfirmed")
        assert store.acknowledge_cancel("run-unconfirmed").status is RunStatus.CANCELLED


def test_resume_idempotency_receipt_survives_failure_and_reopen(tmp_path) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=RunInput(problem="A problem"),
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        store.request_cancel("run-1")
        store.acknowledge_cancel("run-1")
        first = store.resume_run("run-1", idempotency_key="resume-command-1")
        assert first.resume_count == 1
        failed = store.transition_run("run-1", RunStatus.FAILED)
        event_count = len(store.list_events("run-1"))

    with RunStore(database) as reopened:
        replayed = reopened.resume_run(
            "run-1", idempotency_key="resume-command-1"
        )
        assert replayed == failed
        assert len(reopened.list_events("run-1")) == event_count

        retried = reopened.resume_run(
            "run-1", idempotency_key="resume-command-2"
        )
        assert retried.status is RunStatus.RUNNING
        assert retried.resume_count == 2


def test_typed_artifacts_are_required_for_semantic_stage_progress_and_completion(
    tmp_path,
) -> None:
    run_input = RunInput(problem="Prove the target theorem.")
    evidence_text = "A primary theorem supporting the reduction."
    evidence = Evidence(
        id="evidence-1",
        kind="theorem",
        title="Supporting theorem",
        content=evidence_text,
        content_sha256=sha256_text(evidence_text),
        provenance=provenance("literature-thread"),
    )
    plan = Plan(
        id="plan-1",
        problem_sha256=run_input.sha256,
        strategy="Apply the supporting theorem.",
        steps=(
            PlanStep(
                id="step-1",
                statement="Reduce the target to the supporting theorem.",
                rationale="The hypotheses coincide.",
                success_criteria=("The target follows.",),
                evidence_ids=(evidence.id,),
            ),
        ),
        provenance=provenance("planner-thread"),
        created_at=NOW,
    )
    proof = candidate("A complete proof.").model_copy(
        update={
            "evidence_ids": (evidence.id,),
            "provenance": provenance("prover-thread"),
        }
    )

    with RunStore(tmp_path / "qed.sqlite3") as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=run_input,
            provenance=provenance(),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        store.transition_stage("run-1", RunStage.LITERATURE)
        store.add_stage_output(
            "fake-plan",
            run_id="run-1",
            stage=RunStage.LITERATURE,
            kind="plan",
            content={"id": plan.id},
            provenance=provenance(),
        )
        with pytest.raises(InvalidTransitionError, match="typed evidence"):
            store.transition_stage("run-1", RunStage.PLANNING)

        assert store.add_evidence("run-1", evidence) == evidence
        store.transition_stage("run-1", RunStage.PLANNING)
        with pytest.raises(InvalidTransitionError, match="typed plan"):
            store.transition_stage("run-1", RunStage.PROVING)
        assert store.add_plan("run-1", plan) == plan
        store.transition_stage("run-1", RunStage.PROVING)
        with pytest.raises(InvalidTransitionError, match="sealed proof candidate"):
            store.transition_stage("run-1", RunStage.VERIFICATION)

        store.add_thread(
            "prover-thread",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=provenance("prover-thread"),
            external_thread_id="codex-prover-1",
        )
        store.create_candidate(proof, thread_id="prover-thread")
        store.seal_candidate(proof.id)
        store.transition_stage("run-1", RunStage.VERIFICATION)

        reports = []
        for kind, local_id, external_id in (
            ("structural", "structural-thread", "codex-verifier-1"),
            ("detailed", "detailed-thread", "codex-verifier-2"),
            ("citation", "citation-thread", "codex-verifier-3"),
        ):
            store.add_thread(
                local_id,
                run_id="run-1",
                role="verifier",
                model="gpt-5.6-sol",
                provenance=provenance(local_id),
                external_thread_id=external_id,
            )
            report = VerificationReport(
                id=f"{kind}-report",
                candidate_id=proof.id,
                candidate_sha256=proof.proof_sha256,
                kind=kind,  # type: ignore[arg-type]
                checks=(
                    VerificationCheck(
                        id=f"{kind}-check",
                        category="correctness",
                        status=CheckStatus.PASS,
                        summary="The proof passes this independent check.",
                        evidence_ids=(evidence.id,) if kind == "citation" else (),
                    ),
                ),
                verifier_thread_id=local_id,
                verifier_external_thread_id=external_id,
                provenance=provenance(local_id),
                created_at=NOW,
            )
            reports.append(store.add_verification("run-1", report).report)
        store.transition_stage("run-1", RunStage.ADJUDICATION)
        with pytest.raises(InvalidTransitionError, match="typed adjudication"):
            store.transition_stage("run-1", RunStage.EXPORT)

        store.add_thread(
            "adjudicator-thread",
            run_id="run-1",
            role="adjudicator",
            model="gpt-5.6-sol",
            provenance=provenance("adjudicator-thread"),
            external_thread_id="codex-adjudicator-1",
        )
        adjudication = Adjudication(
            id="adjudication-1",
            candidate_id=proof.id,
            report_ids=tuple(report.id for report in reports),
            outcome="accept",
            rationale="Both independent reports pass.",
            provenance=provenance("adjudicator-thread"),
            created_at=NOW,
        )
        assert store.add_adjudication("run-1", adjudication) == adjudication
        decision = store.record_decision("run-1", proof.id)
        assert decision.passed is True
        store.transition_stage("run-1", RunStage.EXPORT)
        with pytest.raises(InvalidTransitionError, match="proof, report, manifest"):
            store.transition_stage("run-1", RunStage.COMPLETE)

        for kind in ("proof", "report", "manifest"):
            store.add_artifact(
                f"artifact-{kind}",
                run_id="run-1",
                kind=kind,
                media_type="application/json",
                sha256=ABC_SHA256,
                size_bytes=3,
                relative_path=f"artifacts/{kind}.json",
                provenance=provenance("export"),
            )
        store.transition_stage("run-1", RunStage.COMPLETE)
        completed = store.transition_run("run-1", RunStatus.COMPLETED)
        snapshot = store.snapshot("run-1")

        assert completed.status is RunStatus.COMPLETED
        assert snapshot.run_input == run_input
        assert snapshot.evidence == (evidence,)
        assert snapshot.plans == (plan,)
        assert snapshot.adjudications == (adjudication,)
        assert snapshot.decisions == (decision,)


def test_bounded_attempt_and_revision_counters_are_enforced_transactionally(
    tmp_path,
) -> None:
    config = QEDConfig(
        budgets={
            "run_seconds": 100,
            "stage_seconds": 100,
            "max_tokens": 1000,
            "proof_attempts": 1,
            "plan_revisions": 0,
            "strategy_rewrites": 0,
        }
    )
    run_input = RunInput(problem="A problem")
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
                rationale="This is the target.",
                success_criteria=("Claim proved.",),
            ),
        ),
        provenance=provenance("planner-thread"),
        created_at=NOW,
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        store.create_run(
            "run-1", config=config, run_input=run_input, provenance=provenance()
        )
        store.transition_run("run-1", RunStatus.RUNNING)
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
        )
        first = candidate("First proof.").model_copy(
            update={
                "evidence_ids": (evidence.id,),
                "provenance": provenance("prover-thread"),
            }
        )
        store.create_candidate(first, thread_id="prover-thread")
        second = first.model_copy(
            update={
                "id": "candidate-2",
                "attempt": 2,
                "proof": "Second proof.",
                "proof_sha256": sha256_text("Second proof."),
            }
        )
        with pytest.raises(ConflictError, match="proof attempt budget"):
            store.create_candidate(second, thread_id="prover-thread")
        assert store.get_run("run-1").proof_attempt_count == 1


def prepare_adjudication_for_revision(
    store: RunStore,
    *,
    config: QEDConfig,
    outcome: str,
) -> None:
    run_input = RunInput(problem="A revision problem")
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
    proof = candidate("A revisable proof.").model_copy(
        update={
            "evidence_ids": (evidence.id,),
            "provenance": provenance("prover-thread"),
        }
    )
    store.create_run(
        "run-1", config=config, run_input=run_input, provenance=provenance()
    )
    store.transition_run("run-1", RunStatus.RUNNING)
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
    )
    store.create_candidate(proof, thread_id="prover-thread")
    store.seal_candidate(proof.id)
    store.transition_stage("run-1", RunStage.VERIFICATION)
    report_ids = []
    for kind, local_id, external_id in (
        ("structural", "structural-thread", "codex-verifier-1"),
        ("detailed", "detailed-thread", "codex-verifier-2"),
        ("citation", "citation-thread", "codex-verifier-3"),
    ):
        store.add_thread(
            local_id,
            run_id="run-1",
            role="verifier",
            model="gpt-5.6-sol",
            provenance=provenance(local_id),
            external_thread_id=external_id,
        )
        report = VerificationReport(
            id=f"{kind}-report",
            candidate_id=proof.id,
            candidate_sha256=proof.proof_sha256,
            kind=kind,  # type: ignore[arg-type]
            checks=(
                VerificationCheck(
                    id=f"{kind}-check",
                    category="correctness",
                    status=CheckStatus.PASS,
                    summary="Independent report.",
                    evidence_ids=(evidence.id,) if kind == "citation" else (),
                ),
            ),
            verifier_thread_id=local_id,
            verifier_external_thread_id=external_id,
            provenance=provenance(local_id),
            created_at=NOW,
        )
        report_ids.append(store.add_verification("run-1", report).id)
    store.transition_stage("run-1", RunStage.ADJUDICATION)
    store.add_thread(
        "adjudicator-thread",
        run_id="run-1",
        role="adjudicator",
        model="gpt-5.6-sol",
        provenance=provenance("adjudicator-thread"),
    )
    store.add_adjudication(
        "run-1",
        Adjudication(
            id="adjudication-1",
            candidate_id=proof.id,
            report_ids=tuple(report_ids),
            outcome=outcome,  # type: ignore[arg-type]
            rationale="Revise according to the selected strategy.",
            provenance=provenance("adjudicator-thread"),
            created_at=NOW,
        ),
    )


@pytest.mark.parametrize(
    ("outcome", "target", "counter"),
    [
        ("revise_plan", RunStage.PLANNING, "plan_revision_count"),
        ("rewrite", RunStage.LITERATURE, "strategy_rewrite_count"),
    ],
)
def test_revision_counters_increment_once_within_budget(
    tmp_path, outcome: str, target: RunStage, counter: str
) -> None:
    config = QEDConfig(
        budgets=BudgetPolicy(plan_revisions=1, strategy_rewrites=1)
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        prepare_adjudication_for_revision(store, config=config, outcome=outcome)
        transitioned = store.transition_stage("run-1", target)
        assert getattr(transitioned, counter) == 1


def test_revise_plan_requires_a_plan_from_the_new_planning_cycle(tmp_path) -> None:
    config = QEDConfig(budgets=BudgetPolicy(plan_revisions=2))
    with RunStore(tmp_path / "qed.sqlite3") as store:
        prepare_adjudication_for_revision(
            store,
            config=config,
            outcome="revise_plan",
        )
        store.transition_stage("run-1", RunStage.PLANNING)

        with pytest.raises(InvalidTransitionError, match="typed plan"):
            store.transition_stage("run-1", RunStage.PROVING)

        prior_plan = store.list_plans("run-1")[0]
        store.add_plan(
            "run-1",
            prior_plan.model_copy(
                update={"id": "plan-2", "strategy": "Use the revised strategy."}
            ),
        )
        assert store.transition_stage("run-1", RunStage.PROVING).stage is RunStage.PROVING


def test_plans_follow_creation_event_order_when_timestamps_tie(tmp_path) -> None:
    config = QEDConfig(budgets=BudgetPolicy(plan_revisions=2))
    with RunStore(tmp_path / "qed.sqlite3", clock=lambda: NOW) as store:
        prepare_adjudication_for_revision(
            store,
            config=config,
            outcome="revise_plan",
        )
        store.transition_stage("run-1", RunStage.PLANNING)
        original = store.list_plans("run-1")[0]
        revised = original.model_copy(
            update={"id": "a-revised-plan", "strategy": "Use the revised strategy."}
        )
        store.add_plan("run-1", revised)

        assert [plan.id for plan in store.list_plans("run-1")] == [
            original.id,
            revised.id,
        ]
        assert [plan.id for plan in store.snapshot("run-1").plans] == [
            original.id,
            revised.id,
        ]


def test_verification_requires_reports_for_a_candidate_from_the_latest_proving_cycle(
    tmp_path,
) -> None:
    with RunStore(tmp_path / "qed.sqlite3") as store:
        prepare_adjudication_for_revision(
            store,
            config=QEDConfig(),
            outcome="revise_proof",
        )
        prior_candidate = store.get_candidate("candidate-1").candidate
        store.transition_stage("run-1", RunStage.PROVING)
        store.add_thread(
            "prover-thread-2",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=provenance("prover-thread-2"),
        )
        revised_proof = "A proof from the revised proving cycle."
        current_candidate = prior_candidate.model_copy(
            update={
                "id": "candidate-2",
                "attempt": 2,
                "proof": revised_proof,
                "proof_sha256": sha256_text(revised_proof),
                "provenance": provenance("prover-thread-2"),
            }
        )
        store.create_candidate(current_candidate, thread_id="prover-thread-2")
        store.seal_candidate(current_candidate.id)
        store.transition_stage("run-1", RunStage.VERIFICATION)

        def add_reports(target: ProofCandidate, prefix: str) -> None:
            for index, kind in enumerate(
                ("structural", "detailed", "citation"),
                start=1,
            ):
                thread_id = f"{prefix}-{kind}-thread"
                external_id = f"codex-{prefix}-verifier-{index}"
                store.add_thread(
                    thread_id,
                    run_id="run-1",
                    role="verifier",
                    model="gpt-5.6-sol",
                    provenance=provenance(thread_id),
                    external_thread_id=external_id,
                )
                store.add_verification(
                    "run-1",
                    VerificationReport(
                        id=f"{prefix}-{kind}-report",
                        candidate_id=target.id,
                        candidate_sha256=target.proof_sha256,
                        kind=kind,  # type: ignore[arg-type]
                        checks=(
                            VerificationCheck(
                                id=f"{prefix}-{kind}-check",
                                category="correctness",
                                status=CheckStatus.PASS,
                                summary="Independent report for this cycle.",
                                evidence_ids=("evidence-1",)
                                if kind == "citation"
                                else (),
                            ),
                        ),
                        verifier_thread_id=thread_id,
                        verifier_external_thread_id=external_id,
                        provenance=provenance(thread_id),
                        created_at=NOW,
                    ),
                )

        add_reports(prior_candidate, "historical-candidate")
        with pytest.raises(InvalidTransitionError, match="independent threads"):
            store.transition_stage("run-1", RunStage.ADJUDICATION)

        add_reports(current_candidate, "current-candidate")
        assert (
            store.transition_stage("run-1", RunStage.ADJUDICATION).stage
            is RunStage.ADJUDICATION
        )


def test_adjudications_follow_event_sequence_when_timestamps_and_ids_disagree(
    tmp_path,
) -> None:
    config = QEDConfig(budgets=BudgetPolicy(plan_revisions=1))
    with RunStore(tmp_path / "qed.sqlite3", clock=FrozenClock()) as store:
        prepare_adjudication_for_revision(
            store,
            config=config,
            outcome="accept",
        )
        first = store.list_adjudications("run-1")[0]
        second = first.model_copy(
            update={
                "id": "adjudication-0",
                "outcome": "revise_plan",
                "rationale": "The later event requests a plan revision.",
            }
        )
        store.add_adjudication("run-1", second)

        assert store.list_adjudications("run-1") == (first, second)
        assert store.snapshot("run-1").adjudications == (first, second)
        assert store.transition_stage("run-1", RunStage.PLANNING).stage is RunStage.PLANNING


@pytest.mark.parametrize(
    ("outcome", "target", "error"),
    [
        ("revise_plan", RunStage.PLANNING, "plan revision budget"),
        ("rewrite", RunStage.LITERATURE, "strategy rewrite budget"),
    ],
)
def test_revision_counters_fail_closed_when_budget_is_zero(
    tmp_path, outcome: str, target: RunStage, error: str
) -> None:
    config = QEDConfig(
        budgets=BudgetPolicy(plan_revisions=0, strategy_rewrites=0)
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        prepare_adjudication_for_revision(store, config=config, outcome=outcome)
        with pytest.raises(InvalidTransitionError, match=error):
            store.transition_stage("run-1", target)
        run = store.get_run("run-1")
        assert run.plan_revision_count == 0
        assert run.strategy_rewrite_count == 0
