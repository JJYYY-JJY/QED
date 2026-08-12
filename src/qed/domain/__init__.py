"""Pure QED domain contracts.

The domain package contains state and policy values only.  It does not import
SQLite, runtime adapters, FastAPI, or the orchestration layer.
"""

from .state import (
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
