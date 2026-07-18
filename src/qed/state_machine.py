"""Declared durable run, stage, and thread state machines."""

from __future__ import annotations

from enum import StrEnum


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
