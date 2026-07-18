from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from qed.config import QEDConfig
from qed.operations import diagnose_run
from qed.schemas import Provenance, canonical_json
from qed.store import (
    ConflictError,
    ExecutionToken,
    InvalidTransitionError,
    RunStage,
    RunStatus,
    RunStore,
)
from tests.test_store import ABC_SHA256, NOW


def _provenance() -> Provenance:
    return Provenance(
        source="application",
        source_id="operator-test",
        runtime_version="test-runtime",
        captured_at=NOW,
    )


def _leased_run(store: RunStore) -> ExecutionToken:
    store.create_run(
        "run-1",
        config=QEDConfig(),
        input_sha256=ABC_SHA256,
        provenance=_provenance(),
    )
    store.transition_run("run-1", RunStatus.RUNNING)
    lease = store.acquire_execution(
        "run-1",
        segment_id="segment-1",
        worker_id="worker-1",
        lease_token="secret-1",
        lease_seconds=1,
        runtime_version="test-runtime",
    )
    return ExecutionToken(
        segment_id=lease.id,
        version=lease.version,
        lease_token="secret-1",
    )


def test_doctor_explains_ambiguous_start_and_budget_state(tmp_path) -> None:
    now = [NOW]
    with RunStore(tmp_path / "qed.sqlite3", clock=lambda: now[0]) as store:
        token = _leased_run(store)
        store.append_event(
            "run-1",
            event_type="runtime.turn_attempt_started",
            stage=RunStage.INTAKE,
            payload={
                "turn_input_id": "turn-input-1",
                "output_schema": "EvidenceBatch",
                "attempt": 1,
            },
            execution=token,
        )
        store.record_turn_start_unconfirmed(
            "run-1",
            payload={
                "turn_input_id": "turn-input-1",
                "attempt": 1,
                "reason": "turn/start response lost",
            },
            execution=token,
        )
        store.append_event(
            "run-1",
            event_type="runtime.token_usage",
            stage=RunStage.INTAKE,
            payload={
                "thread_id": "external-thread-1",
                "turn_id": "external-turn-1",
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 5,
                    "cached_input_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            },
            execution=token,
        )

        diagnosis = diagnose_run(store, "run-1", observed_at=NOW)

        assert diagnosis.pending_runtime[0].kind == "attempt"
        assert diagnosis.pending_runtime[0].turn_input_id == "turn-input-1"
        assert diagnosis.budget.token_usage == 12
        assert len(diagnosis.execution_segments) == 1
        assert any("start result is ambiguous" in item for item in diagnosis.blockers)
        assert any("live execution lease" in item for item in diagnosis.blockers)
        assert diagnosis.reconciliation.available is False
        assert len(diagnosis.unconfirmed_events) == 1

        digest = hashlib.sha256()
        for event in store.list_events("run-1"):
            digest.update(canonical_json(event).encode())
            digest.update(b"\n")
        assert diagnosis.event_chain_sha256 == digest.hexdigest()


def test_abandon_is_immutable_idempotent_and_never_fabricates_terminal(tmp_path) -> None:
    now = [NOW]
    with RunStore(tmp_path / "qed.sqlite3", clock=lambda: now[0]) as store:
        token = _leased_run(store)
        store.append_event(
            "run-1",
            event_type="runtime.turn_attempt_started",
            stage=RunStage.INTAKE,
            payload={
                "turn_input_id": "turn-input-1",
                "output_schema": "EvidenceBatch",
                "attempt": 1,
            },
            execution=token,
        )
        store.append_event(
            "run-1",
            event_type="runtime.turn_started",
            stage=RunStage.INTAKE,
            payload={
                "thread_id": "external-thread-1",
                "turn_id": "external-turn-1",
                "backend": "app_server",
                "turn_input_id": "turn-input-1",
                "attempt": 1,
            },
            execution=token,
        )
        store.record_turn_terminal_unconfirmed(
            "run-1",
            payload={
                "thread_id": "external-thread-1",
                "turn_id": "external-turn-1",
                "backend": "app_server",
                "reason": "stream ended",
            },
            execution=token,
        )

        with pytest.raises(ConflictError, match="live execution lease"):
            store.abandon_run(
                "run-1",
                reason="Runtime status cannot be reconciled.",
                idempotency_key="operator-1",
            )

        now[0] += timedelta(seconds=2)
        decision = store.abandon_run(
            "run-1",
            reason="Runtime status cannot be reconciled.",
            idempotency_key="operator-1",
        )
        run = store.get_run("run-1")
        assert decision.replayed is False
        assert run.status is RunStatus.FAILED
        assert run.resumable is False
        assert not any(
            event.event_type == "runtime.turn_completed"
            for event in store.list_events("run-1")
        )

        replay = store.abandon_run(
            "run-1",
            reason="Runtime status cannot be reconciled.",
            idempotency_key="operator-1",
        )
        assert replay.replayed is True
        assert replay.event_seq == decision.event_seq
        with pytest.raises(ConflictError, match="another reason"):
            store.abandon_run(
                "run-1",
                reason="A different rationale.",
                idempotency_key="operator-1",
            )
        with pytest.raises(ConflictError, match="already has"):
            store.abandon_run(
                "run-1",
                reason="Runtime status cannot be reconciled.",
                idempotency_key="operator-2",
            )

        store.record_turn_completed(
            "run-1",
            payload={
                "thread_id": "external-thread-1",
                "turn_id": "external-turn-1",
                "backend": "app_server",
                "status": "interrupted",
            },
            execution=token,
        )
        assert store.pending_runtime_identities("run-1") == ()
        diagnosis = diagnose_run(store, "run-1", observed_at=now[0])
        assert diagnosis.operator_decisions[0].event_seq == decision.event_seq
        assert any("operator abandon" in item for item in diagnosis.blockers)
        assert store.get_run("run-1").status is RunStatus.FAILED


def test_abandon_rejects_success_and_cancelled_terminal_runs(tmp_path) -> None:
    with RunStore(tmp_path / "qed.sqlite3") as store:
        store.create_run(
            "run-cancelled",
            config=QEDConfig(),
            input_sha256=ABC_SHA256,
            provenance=_provenance(),
        )
        store.request_cancel("run-cancelled")
        store.acknowledge_cancel("run-cancelled")

        with pytest.raises(InvalidTransitionError, match="cancelled"):
            store.abandon_run(
                "run-cancelled",
                reason="No longer needed.",
                idempotency_key="operator-cancelled",
            )
