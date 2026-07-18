from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from qed.config import QEDConfig
from qed.inputs import RunInput
from qed.schemas import Provenance
from qed.state_machine import (
    RUN_TRANSITIONS,
    STAGE_TRANSITIONS,
    THREAD_TRANSITIONS,
    RunStage,
    RunStatus,
    ThreadStatus,
)
from qed.store import InvalidTransitionError, RunStore

NOW = datetime(2026, 7, 17, tzinfo=UTC)


def _provenance() -> Provenance:
    return Provenance(
        source="application",
        model="gpt-5.6-sol",
        runtime_version="test-runtime",
        captured_at=NOW,
    )


@pytest.mark.parametrize(
    ("states", "transitions"),
    [
        (tuple(RunStatus), RUN_TRANSITIONS),
        (tuple(RunStage), STAGE_TRANSITIONS),
        (tuple(ThreadStatus), THREAD_TRANSITIONS),
    ],
)
def test_transition_tables_exhaustively_classify_distinct_state_pairs(
    states: tuple[RunStatus, ...] | tuple[RunStage, ...] | tuple[ThreadStatus, ...],
    transitions: object,
) -> None:
    declared = transitions
    assert isinstance(declared, dict)
    assert set(declared) == set(states)
    all_pairs = set(itertools.permutations(states, 2))
    legal_pairs = {
        (source, target)
        for source, targets in declared.items()
        for target in targets
    }
    illegal_pairs = all_pairs - legal_pairs
    assert legal_pairs.isdisjoint(illegal_pairs)
    assert legal_pairs | illegal_pairs == all_pairs
    assert all(target in states for _, target in legal_pairs)
    assert all(source not in targets for source, targets in declared.items())


def test_store_accepts_a_legal_stage_edge_and_rejects_a_skipped_edge(tmp_path) -> None:
    with RunStore(tmp_path / "qed.sqlite3") as store:
        store.create_run(
            "run-state-machine",
            config=QEDConfig(),
            run_input=RunInput(problem="Prove P."),
            provenance=_provenance(),
        )
        store.transition_run("run-state-machine", RunStatus.RUNNING)

        with pytest.raises(InvalidTransitionError, match="invalid stage transition"):
            store.transition_stage("run-state-machine", RunStage.PROVING)
        assert (
            store.transition_stage("run-state-machine", RunStage.LITERATURE).stage
            is RunStage.LITERATURE
        )


@pytest.mark.parametrize(
    "terminal",
    [ThreadStatus.COMPLETED, ThreadStatus.FAILED, ThreadStatus.CANCELLED],
)
def test_terminal_thread_states_reject_every_distinct_successor(
    tmp_path,
    terminal: ThreadStatus,
) -> None:
    with RunStore(tmp_path / f"{terminal.value}.sqlite3") as store:
        store.create_run(
            "run-thread-state",
            config=QEDConfig(),
            run_input=RunInput(problem="Prove P."),
            provenance=_provenance(),
        )
        store.add_thread(
            "thread-1",
            run_id="run-thread-state",
            role="prover",
            model="gpt-5.6-sol",
            provenance=_provenance(),
        )
        store.transition_thread("thread-1", terminal)
        for target in set(ThreadStatus) - {terminal}:
            with pytest.raises(InvalidTransitionError):
                store.transition_thread("thread-1", target)
