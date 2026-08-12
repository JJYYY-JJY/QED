"""The single authoritative durable state machine.

This module is deliberately pure.  Persistence owns the transaction which
applies a command; this module owns which commands are legal and what their
durable contract is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class RunStage(StrEnum):
    INTAKE = "intake"
    LITERATURE = "literature"
    PLANNING = "planning"
    PROVING = "proving"
    VERIFICATION = "verification"
    ADJUDICATION = "adjudication"
    EXPORT = "export"
    COMPLETE = "complete"


class ThreadRole(StrEnum):
    LITERATURE = "literature"
    PLANNER = "planner"
    PROVER = "prover"
    VERIFIER = "verifier"
    ADJUDICATOR = "adjudicator"


class ThreadStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.FAILED}
    ),
    RunStatus.RUNNING: frozenset(
        {RunStatus.PAUSED, RunStatus.CANCELLING, RunStatus.FAILED, RunStatus.COMPLETED}
    ),
    RunStatus.PAUSED: frozenset({RunStatus.CANCELLING, RunStatus.FAILED}),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.COMPLETED: frozenset(),
}

STAGE_TRANSITIONS: dict[RunStage, frozenset[RunStage]] = {
    RunStage.INTAKE: frozenset({RunStage.LITERATURE}),
    RunStage.LITERATURE: frozenset({RunStage.PLANNING}),
    RunStage.PLANNING: frozenset({RunStage.PROVING}),
    RunStage.PROVING: frozenset({RunStage.VERIFICATION}),
    RunStage.VERIFICATION: frozenset({RunStage.ADJUDICATION}),
    RunStage.ADJUDICATION: frozenset(
        {RunStage.LITERATURE, RunStage.PLANNING, RunStage.PROVING, RunStage.EXPORT}
    ),
    RunStage.EXPORT: frozenset({RunStage.COMPLETE}),
    RunStage.COMPLETE: frozenset(),
}

THREAD_TRANSITIONS: dict[ThreadStatus, frozenset[ThreadStatus]] = {
    ThreadStatus.ACTIVE: frozenset(
        {ThreadStatus.COMPLETED, ThreadStatus.FAILED, ThreadStatus.CANCELLED}
    ),
    ThreadStatus.COMPLETED: frozenset(),
    ThreadStatus.FAILED: frozenset(),
    ThreadStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Durable contract for one state-changing command."""

    id: str
    source_states: tuple[str, ...]
    target_state: str
    transaction_boundary: str
    emitted_events: tuple[str, ...]
    retry_semantics: str
    stale_owner_behavior: str
    crash_recovery_behavior: str


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        id="run.start",
        source_states=(RunStatus.CREATED.value,),
        target_state=RunStatus.RUNNING.value,
        transaction_boundary="one BEGIN IMMEDIATE mutation",
        emitted_events=("run.started",),
        retry_semantics="idempotency receipt returns the original command result",
        stale_owner_behavior="no execution owner exists until a run is started",
        crash_recovery_behavior="created remains durable until a later explicit start",
    ),
    CommandSpec(
        id="run.cancel",
        source_states=(RunStatus.CREATED.value, RunStatus.RUNNING.value, RunStatus.PAUSED.value),
        target_state=RunStatus.CANCELLING.value,
        transaction_boundary="one BEGIN IMMEDIATE mutation",
        emitted_events=("run.cancelling",),
        retry_semantics="idempotency receipt returns the original command result",
        stale_owner_behavior="cancel is accepted only by the command owner",
        crash_recovery_behavior="cancelling is reconciled to cancelled or failed",
    ),
    CommandSpec(
        id="run.resume",
        source_states=(RunStatus.PAUSED.value,),
        target_state=RunStatus.RUNNING.value,
        transaction_boundary="one BEGIN IMMEDIATE mutation",
        emitted_events=("run.resumed",),
        retry_semantics="one durable receipt per idempotency key",
        stale_owner_behavior="expired execution segments are fenced",
        crash_recovery_behavior="paused remains paused until resume is durably accepted",
    ),
    CommandSpec(
        id="run.complete",
        source_states=(RunStatus.RUNNING.value,),
        target_state=RunStatus.COMPLETED.value,
        transaction_boundary="gate validation and final transition in one transaction",
        emitted_events=("run.completed",),
        retry_semantics="a completed run rejects a second terminal command",
        stale_owner_behavior="stale execution cannot complete a run",
        crash_recovery_behavior="missing completion gate leaves the run non-terminal",
    ),
)


def transition_table() -> dict[str, Any]:
    """Return a deterministic, JSON-serializable state-machine artifact."""

    return {
        "run": {
            source.value: sorted(target.value for target in targets)
            for source, targets in RUN_TRANSITIONS.items()
        },
        "stage": {
            source.value: sorted(target.value for target in targets)
            for source, targets in STAGE_TRANSITIONS.items()
        },
        "thread": {
            source.value: sorted(target.value for target in targets)
            for source, targets in THREAD_TRANSITIONS.items()
        },
        "commands": [asdict(spec) for spec in COMMAND_SPECS],
    }


def is_legal_transition(
    transitions: dict[StrEnum, frozenset[StrEnum]],
    source: StrEnum,
    target: StrEnum,
) -> bool:
    """Pure helper used by persistence and property tests."""

    return target in transitions.get(source, frozenset())
