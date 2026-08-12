"""Backward-compatible imports for the authoritative domain state machine."""

from qed.domain.state import (
    COMMAND_SPECS,
    RUN_TRANSITIONS,
    STAGE_TRANSITIONS,
    THREAD_TRANSITIONS,
    CommandSpec,
    RunStage,
    RunStatus,
    ThreadRole,
    ThreadStatus,
    transition_table,
)

__all__ = [
    "COMMAND_SPECS",
    "RUN_TRANSITIONS",
    "STAGE_TRANSITIONS",
    "THREAD_TRANSITIONS",
    "CommandSpec",
    "RunStage",
    "RunStatus",
    "ThreadRole",
    "ThreadStatus",
    "transition_table",
]
