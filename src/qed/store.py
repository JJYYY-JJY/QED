"""Transactional SQLite state store for deterministic QED runs."""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from qed.config import QEDConfig
from qed.decision import (
    STABLE_REQUIRED_REPORT_KINDS,
    CandidateDecision,
    candidate_decision_sha256,
    decide_stable_candidate,
)
from qed.inputs import RunInput
from qed.schemas import (
    Adjudication,
    Event,
    Evidence,
    EvidenceTrust,
    Plan,
    ProofCandidate,
    Provenance,
    Sha256,
    VerificationReport,
    WebSearchObservation,
    canonical_json,
    canonical_sha256,
    evidence_sha256,
    sha256_text,
    verification_report_sha256,
)
from qed.stable_contracts import (
    ProofObligationGraph,
    RuntimeProvenance,
    VerifierRole,
)
from qed.state_machine import (
    RUN_TRANSITIONS,
    STAGE_TRANSITIONS,
    THREAD_TRANSITIONS,
)
from qed.state_machine import (
    RunStage as RunStage,
)
from qed.state_machine import (
    RunStatus as RunStatus,
)
from qed.state_machine import (
    ThreadRole as ThreadRole,
)
from qed.state_machine import (
    ThreadStatus as ThreadStatus,
)
from qed.store_schema import (
    DuplicateExternalThreadIdentityError,
    SchemaVersionError,
    finalize_schema_migration,
    prepare_schema_migration,
)

SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StoreError(RuntimeError):
    """Base class for state-store failures callers may handle."""


class NotFoundError(StoreError):
    """Raised when a requested persisted object does not exist."""


class ConflictError(StoreError):
    """Raised when a caller tries to create an existing identity."""


class InvalidTransitionError(StoreError):
    """Raised when a state transition is not in the declared transition table."""


class ImmutableRecordError(StoreError):
    """Raised when a sealed or immutable record would be changed."""


class StoreIntegrityError(StoreError):
    """Raised when persisted JSON disagrees with its denormalized integrity fields."""


RUNTIME_DRAIN_EVENTS = frozenset(
    {
        "runtime.turn_started",
        "runtime.token_usage",
        "runtime.item_completed",
        "runtime.unknown_notification",
        "runtime.error",
    }
)


class StoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StoreInfo(StoreModel):
    schema_version: int
    journal_mode: Literal["wal"]
    foreign_keys: bool
    tables: tuple[str, ...]


class RunRecord(StoreModel):
    id: str
    schema_version: int
    status: RunStatus
    stage: RunStage
    config: QEDConfig
    config_sha256: Sha256
    input_sha256: Sha256
    provenance: Provenance
    provenance_sha256: Sha256
    runtime_version: str
    cancellation_requested: bool
    resumable: bool
    resume_count: Annotated[int, Field(ge=0)]
    execution_version: Annotated[int, Field(ge=0)]
    proof_attempt_count: Annotated[int, Field(ge=0)]
    plan_revision_count: Annotated[int, Field(ge=0)]
    strategy_rewrite_count: Annotated[int, Field(ge=0)]
    created_at: datetime
    updated_at: datetime


class ResumeCommandResult(StoreModel):
    run: RunRecord
    replayed: bool
    accepted_status: RunStatus


class StartCommandResult(StoreModel):
    run: RunRecord
    replayed: bool
    accepted_status: RunStatus


class CancelCommandResult(StoreModel):
    run: RunRecord
    replayed: bool
    accepted_status: RunStatus
    accepted_execution_version: int


class OperatorDecisionRecord(StoreModel):
    """Immutable operator action reconstructed from the ordered event chain."""

    run_id: str
    action: Literal["abandon"]
    idempotency_key: str
    reason: str
    status_before: RunStatus
    status_after: RunStatus
    event_seq: Annotated[int, Field(ge=1)]
    created_at: datetime
    replayed: bool = False


class ThreadRecord(StoreModel):
    id: str
    run_id: str
    role: ThreadRole
    parent_thread_id: str | None
    external_thread_id: str | None
    model: str
    status: ThreadStatus
    schema_version: int
    provenance: Provenance
    provenance_sha256: Sha256
    created_at: datetime
    updated_at: datetime


class CandidateRecord(StoreModel):
    id: str
    run_id: str
    thread_id: str | None
    plan_id: str
    attempt: int
    schema_version: int
    candidate: ProofCandidate
    candidate_sha256: Sha256
    proof_sha256: Sha256
    provenance: Provenance
    provenance_sha256: Sha256
    sealed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class VerificationRecord(StoreModel):
    id: str
    run_id: str
    candidate_id: str
    thread_id: str
    kind: str
    schema_version: int
    report: VerificationReport
    report_sha256: Sha256
    candidate_sha256: Sha256
    provenance: Provenance
    provenance_sha256: Sha256
    created_at: datetime


class StageOutputRecord(StoreModel):
    id: str
    run_id: str
    stage: RunStage
    kind: str
    schema_version: int
    content: JsonValue
    content_sha256: Sha256
    provenance: Provenance
    provenance_sha256: Sha256
    created_at: datetime


class TurnInputRecord(StoreModel):
    id: str
    run_id: str
    role: str
    prompt_version: str
    output_schema_sha256: Sha256
    payload: dict[str, JsonValue]
    payload_sha256: Sha256
    created_at: datetime


class ArtifactRecord(StoreModel):
    id: str
    run_id: str
    kind: str
    relative_path: str | None
    media_type: str
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0)]
    schema_version: int
    provenance: Provenance
    provenance_sha256: Sha256
    created_at: datetime


class ExecutionToken(StoreModel):
    segment_id: str
    version: Annotated[int, Field(ge=1)]
    lease_token: str


class ExecutionLease(StoreModel):
    id: str
    run_id: str
    worker_id: str
    version: Annotated[int, Field(ge=1)]
    runtime_version: str | None
    runtime_resolution_sha256: Sha256 | None
    lease_expires_at: datetime
    released_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RuntimeResolutionRecord(StoreModel):
    segment_id: str
    run_id: str
    schema_version: int
    resolution: JsonValue
    resolution_sha256: Sha256
    created_at: datetime


class RunSnapshot(StoreModel):
    run: RunRecord
    run_input: RunInput | None = None
    events: tuple[Event, ...]
    stage_outputs: tuple[StageOutputRecord, ...]
    turn_inputs: tuple[TurnInputRecord, ...] = ()
    threads: tuple[ThreadRecord, ...]
    candidates: tuple[CandidateRecord, ...]
    verifications: tuple[VerificationRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]
    execution_segments: tuple[ExecutionLease, ...] = ()
    runtime_resolutions: tuple[RuntimeResolutionRecord, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    web_search_observations: tuple[WebSearchObservation, ...] = ()
    plans: tuple[Plan, ...] = ()
    adjudications: tuple[Adjudication, ...] = ()
    decisions: tuple[CandidateDecision, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _render_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_sha256(value: str, *, field: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_id(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field} must be 1-128 ASCII identifier characters without path separators"
        )
    return value


def _validate_run_id(value: str) -> str:
    if not isinstance(value, str) or _RUN_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "run_id must be 1-128 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return value


def _validate_nonempty(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def validate_relative_artifact_path(value: str) -> str:
    """Return a canonical contained POSIX path suitable for a managed artifact root."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("relative_path must be a nonempty canonical POSIX path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or path.as_posix() != value:
        raise ValueError("relative_path must be a canonical relative POSIX path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("relative_path must remain inside the managed artifact root")
    return value


def _require_integrity(condition: bool, message: str) -> None:
    if not condition:
        raise StoreIntegrityError(message)


class RunStore:
    """Owns schema setup, state transitions, and ordered run events."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5.0,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            journal_mode = self._connection.execute(
                "PRAGMA journal_mode = WAL"
            ).fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise StoreError("SQLite WAL mode is required")
            self._initialize_schema()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise StoreError("store clock must return a timezone-aware datetime")
        return now

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold one WAL snapshot while materializing a compound public read."""

        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize_schema(self) -> None:
        try:
            prior_version = prepare_schema_migration(self._connection)
        except DuplicateExternalThreadIdentityError as error:
            raise StoreIntegrityError(str(error)) from error
        except SchemaVersionError as error:
            raise StoreError(str(error)) from error
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS runtime_provenance (
                run_id TEXT PRIMARY KEY,
                provenance_json TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL CHECK (
                    length(provenance_sha256) = 64
                    AND provenance_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                config_json TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                runtime_version TEXT NOT NULL,
                cancellation_requested INTEGER NOT NULL DEFAULT 0 CHECK (
                    cancellation_requested IN (0, 1)
                ),
                resumable INTEGER NOT NULL DEFAULT 0 CHECK (resumable IN (0, 1)),
                resume_count INTEGER NOT NULL DEFAULT 0 CHECK (resume_count >= 0),
                last_resume_key TEXT,
                execution_version INTEGER NOT NULL DEFAULT 0 CHECK (execution_version >= 0),
                proof_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
                    proof_attempt_count >= 0
                ),
                plan_revision_count INTEGER NOT NULL DEFAULT 0 CHECK (
                    plan_revision_count >= 0
                ),
                strategy_rewrite_count INTEGER NOT NULL DEFAULT 0 CHECK (
                    strategy_rewrite_count >= 0
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS run_inputs (
                run_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                input_json TEXT NOT NULL,
                input_sha256 TEXT NOT NULL CHECK (
                    length(input_sha256) = 64 AND input_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL CHECK (seq >= 1),
                schema_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                stage TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, seq),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS stage_outputs (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                kind TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS turn_inputs (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                output_schema_sha256 TEXT NOT NULL CHECK (
                    length(output_schema_sha256) = 64
                    AND output_schema_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL CHECK (
                    length(payload_sha256) = 64
                    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS web_search_observations (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                backend TEXT NOT NULL,
                external_thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                uri_sha256 TEXT NOT NULL CHECK (
                    length(uri_sha256) = 64
                    AND uri_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                observation_json TEXT NOT NULL,
                observation_sha256 TEXT NOT NULL CHECK (
                    length(observation_sha256) = 64
                    AND observation_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                UNIQUE (
                    run_id, backend, external_thread_id, turn_id, item_id, uri_sha256
                )
            ) STRICT;

            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                plan_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                parent_thread_id TEXT,
                external_thread_id TEXT,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                provenance_json TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_thread_id) REFERENCES threads(id) ON DELETE SET NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                thread_id TEXT,
                plan_id TEXT NOT NULL,
                attempt INTEGER NOT NULL CHECK (attempt >= 1),
                schema_version INTEGER NOT NULL,
                candidate_json TEXT NOT NULL,
                candidate_sha256 TEXT NOT NULL,
                proof_sha256 TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                sealed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL,
                FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE RESTRICT,
                UNIQUE (run_id, attempt)
            ) STRICT;

            CREATE TABLE IF NOT EXISTS verifications (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                report_json TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                candidate_sha256 TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE RESTRICT,
                FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE RESTRICT
            ) STRICT;

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                relative_path TEXT,
                media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                schema_version INTEGER NOT NULL,
                provenance_json TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS execution_segments (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 1),
                lease_token_sha256 TEXT NOT NULL CHECK (
                    length(lease_token_sha256) = 64
                    AND lease_token_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                runtime_version TEXT,
                runtime_resolution_sha256 TEXT CHECK (
                    runtime_resolution_sha256 IS NULL OR (
                        length(runtime_resolution_sha256) = 64
                        AND runtime_resolution_sha256 NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                lease_expires_at TEXT NOT NULL,
                released_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (run_id, version),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS resume_commands (
                run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                accepted_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, idempotency_key),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS start_commands (
                run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                accepted_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, idempotency_key),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS cancel_commands (
                run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                accepted_status TEXT NOT NULL,
                accepted_execution_version INTEGER NOT NULL CHECK (
                    accepted_execution_version >= 0
                ),
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, idempotency_key),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS runtime_resolutions (
                segment_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                resolution_json TEXT NOT NULL,
                resolution_sha256 TEXT NOT NULL CHECK (
                    length(resolution_sha256) = 64
                    AND resolution_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (segment_id) REFERENCES execution_segments(id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            ) STRICT;

            CREATE TABLE IF NOT EXISTS adjudications (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                adjudication_json TEXT NOT NULL,
                adjudication_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE RESTRICT
            ) STRICT;

            CREATE TABLE IF NOT EXISTS candidate_decisions (
                candidate_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                decision_json TEXT NOT NULL,
                decision_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE RESTRICT
            ) STRICT;

            CREATE INDEX IF NOT EXISTS events_run_seq_idx ON events(run_id, seq);
            CREATE INDEX IF NOT EXISTS events_stage_entry_idx
                ON events(run_id, event_type, stage, seq DESC);
            CREATE INDEX IF NOT EXISTS stage_outputs_run_idx ON stage_outputs(run_id, stage);
            CREATE INDEX IF NOT EXISTS turn_inputs_run_idx
                ON turn_inputs(run_id, created_at, id);
            CREATE INDEX IF NOT EXISTS evidence_run_idx ON evidence(run_id, created_at, id);
            CREATE INDEX IF NOT EXISTS web_search_observations_run_idx
                ON web_search_observations(run_id, created_at, id);
            CREATE INDEX IF NOT EXISTS plans_run_idx ON plans(run_id, created_at, id);
            CREATE INDEX IF NOT EXISTS threads_run_idx ON threads(run_id, role);
            CREATE UNIQUE INDEX IF NOT EXISTS threads_external_unique_idx
                ON threads(run_id, external_thread_id)
                WHERE external_thread_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS candidates_run_idx ON candidates(run_id, attempt);
            CREATE INDEX IF NOT EXISTS verifications_candidate_idx
                ON verifications(candidate_id, kind);
            CREATE INDEX IF NOT EXISTS artifacts_run_idx ON artifacts(run_id, kind);
            CREATE INDEX IF NOT EXISTS execution_segments_run_idx
                ON execution_segments(run_id, version);
            CREATE INDEX IF NOT EXISTS resume_commands_run_idx
                ON resume_commands(run_id, created_at, idempotency_key);
            CREATE INDEX IF NOT EXISTS start_commands_run_idx
                ON start_commands(run_id, created_at, idempotency_key);
            CREATE INDEX IF NOT EXISTS cancel_commands_run_idx
                ON cancel_commands(run_id, created_at, idempotency_key);
            CREATE INDEX IF NOT EXISTS runtime_resolutions_run_idx
                ON runtime_resolutions(run_id, created_at, segment_id);
            CREATE INDEX IF NOT EXISTS adjudications_run_idx
                ON adjudications(run_id, created_at, id);
            CREATE INDEX IF NOT EXISTS candidate_decisions_run_idx
                ON candidate_decisions(run_id, created_at, candidate_id);

            CREATE TRIGGER IF NOT EXISTS candidates_sealed_update_guard
            BEFORE UPDATE ON candidates
            WHEN OLD.sealed_at IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'sealed candidate is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS candidates_sealed_delete_guard
            BEFORE DELETE ON candidates
            WHEN OLD.sealed_at IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'sealed candidate is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS verifications_update_guard
            BEFORE UPDATE ON verifications
            BEGIN
                SELECT RAISE(ABORT, 'verification is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS verifications_delete_guard
            BEFORE DELETE ON verifications
            BEGIN
                SELECT RAISE(ABORT, 'verification is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS threads_terminal_status_guard
            BEFORE UPDATE OF status ON threads
            WHEN OLD.status != 'active' AND NEW.status != OLD.status
            BEGIN
                SELECT RAISE(ABORT, 'terminal thread status is immutable');
            END;


            CREATE TRIGGER IF NOT EXISTS run_inputs_update_guard
            BEFORE UPDATE ON run_inputs
            BEGIN
                SELECT RAISE(ABORT, 'run input is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS run_inputs_delete_guard
            BEFORE DELETE ON run_inputs
            BEGIN
                SELECT RAISE(ABORT, 'run input is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS evidence_update_guard
            BEFORE UPDATE ON evidence BEGIN
                SELECT RAISE(ABORT, 'evidence is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_delete_guard
            BEFORE DELETE ON evidence BEGIN
                SELECT RAISE(ABORT, 'evidence is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS web_search_observations_update_guard
            BEFORE UPDATE ON web_search_observations BEGIN
                SELECT RAISE(ABORT, 'web search observation is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS web_search_observations_delete_guard
            BEFORE DELETE ON web_search_observations BEGIN
                SELECT RAISE(ABORT, 'web search observation is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS plans_update_guard
            BEFORE UPDATE ON plans BEGIN
                SELECT RAISE(ABORT, 'plan is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS plans_delete_guard
            BEFORE DELETE ON plans BEGIN
                SELECT RAISE(ABORT, 'plan is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS adjudications_update_guard
            BEFORE UPDATE ON adjudications BEGIN
                SELECT RAISE(ABORT, 'adjudication is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS adjudications_delete_guard
            BEFORE DELETE ON adjudications BEGIN
                SELECT RAISE(ABORT, 'adjudication is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS candidate_decisions_update_guard
            BEFORE UPDATE ON candidate_decisions BEGIN
                SELECT RAISE(ABORT, 'candidate decision is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS candidate_decisions_delete_guard
            BEFORE DELETE ON candidate_decisions BEGIN
                SELECT RAISE(ABORT, 'candidate decision is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS events_update_guard
            BEFORE UPDATE ON events BEGIN
                SELECT RAISE(ABORT, 'event is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS events_delete_guard
            BEFORE DELETE ON events BEGIN
                SELECT RAISE(ABORT, 'event is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS stage_outputs_update_guard
            BEFORE UPDATE ON stage_outputs BEGIN
                SELECT RAISE(ABORT, 'stage output is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS stage_outputs_delete_guard
            BEFORE DELETE ON stage_outputs BEGIN
                SELECT RAISE(ABORT, 'stage output is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS turn_inputs_update_guard
            BEFORE UPDATE ON turn_inputs BEGIN
                SELECT RAISE(ABORT, 'turn input is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS turn_inputs_delete_guard
            BEFORE DELETE ON turn_inputs BEGIN
                SELECT RAISE(ABORT, 'turn input is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS artifacts_update_guard
            BEFORE UPDATE ON artifacts BEGIN
                SELECT RAISE(ABORT, 'artifact is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS artifacts_delete_guard
            BEFORE DELETE ON artifacts BEGIN
                SELECT RAISE(ABORT, 'artifact is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS runtime_resolutions_update_guard
            BEFORE UPDATE ON runtime_resolutions BEGIN
                SELECT RAISE(ABORT, 'runtime resolution is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS runtime_resolutions_delete_guard
            BEFORE DELETE ON runtime_resolutions BEGIN
                SELECT RAISE(ABORT, 'runtime resolution is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS resume_commands_update_guard
            BEFORE UPDATE ON resume_commands BEGIN
                SELECT RAISE(ABORT, 'resume command is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS resume_commands_delete_guard
            BEFORE DELETE ON resume_commands BEGIN
                SELECT RAISE(ABORT, 'resume command is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS start_commands_update_guard
            BEFORE UPDATE ON start_commands BEGIN
                SELECT RAISE(ABORT, 'start command is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS start_commands_delete_guard
            BEFORE DELETE ON start_commands BEGIN
                SELECT RAISE(ABORT, 'start command is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS cancel_commands_update_guard
            BEFORE UPDATE ON cancel_commands BEGIN
                SELECT RAISE(ABORT, 'cancel command is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS cancel_commands_delete_guard
            BEFORE DELETE ON cancel_commands BEGIN
                SELECT RAISE(ABORT, 'cancel command is immutable');
            END;
            """
        )
        finalize_schema_migration(self._connection, prior_version)

    def info(self) -> StoreInfo:
        with self._lock:
            version = self._connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()["value"]
            journal_mode = str(
                self._connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            if journal_mode != "wal":
                raise StoreError("SQLite WAL mode is required")
            foreign_keys = bool(
                self._connection.execute("PRAGMA foreign_keys").fetchone()[0]
            )
            rows = self._connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
            return StoreInfo(
                schema_version=int(version),
                journal_mode=cast(Literal["wal"], journal_mode),
                foreign_keys=foreign_keys,
                tables=tuple(row["name"] for row in rows),
            )

    def create_run(
        self,
        run_id: str,
        *,
        config: QEDConfig,
        input_sha256: Sha256 | None = None,
        run_input: RunInput | None = None,
        provenance: Provenance,
    ) -> RunRecord:
        _validate_run_id(run_id)
        if run_input is None and input_sha256 is None:
            raise ValueError("create_run requires run_input or input_sha256")
        if run_input is not None:
            if input_sha256 is not None and input_sha256 != run_input.sha256:
                raise ValueError("input_sha256 does not match run_input")
            input_sha256 = run_input.sha256
        assert input_sha256 is not None
        _validate_sha256(input_sha256, field="input_sha256")
        now = self._now()
        config_json = canonical_json(config)
        provenance_json = canonical_json(provenance)
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, schema_version, status, stage, config_json, config_sha256,
                        input_sha256, provenance_json, provenance_sha256, runtime_version,
                        cancellation_requested, resumable, resume_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)
                    """,
                    (
                        run_id,
                        SCHEMA_VERSION,
                        RunStatus.CREATED.value,
                        RunStage.INTAKE.value,
                        config_json,
                        config.sha256,
                        input_sha256,
                        provenance_json,
                        canonical_sha256(provenance),
                        provenance.runtime_version,
                        _render_time(now),
                        _render_time(now),
                    ),
                )
                if run_input is not None:
                    connection.execute(
                        """
                        INSERT INTO run_inputs (
                            run_id, schema_version, input_json, input_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            run_input.schema_version,
                            canonical_json(run_input),
                            run_input.sha256,
                            _render_time(now),
                        ),
                    )
                self._append_event(
                    connection,
                    run_id,
                    event_type="run.created",
                    stage=RunStage.INTAKE,
                    payload={"status": RunStatus.CREATED.value},
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"run already exists: {run_id}") from error
        return self.get_run(run_id)

    def get_run_input(self, run_id: str) -> RunInput:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            row = self._connection.execute(
                "SELECT input_json, input_sha256 FROM run_inputs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"run input not found: {run_id}")
            run_input = RunInput.model_validate_json(row["input_json"])
            if run_input.sha256 != row["input_sha256"]:
                raise StoreIntegrityError(f"run input hash mismatch: {run_id}")
            return run_input

    def acquire_execution(
        self,
        run_id: str,
        *,
        segment_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 60,
        runtime_version: str | None = None,
        runtime_resolution_sha256: Sha256 | None = None,
    ) -> ExecutionLease:
        """Acquire one fenced worker segment, recovering only after the prior lease expires."""

        _validate_id(run_id, field="run_id")
        _validate_id(segment_id, field="segment_id")
        _validate_id(worker_id, field="worker_id")
        _validate_nonempty(lease_token, field="lease_token")
        if runtime_version is not None:
            _validate_nonempty(runtime_version, field="runtime_version")
        if runtime_resolution_sha256 is not None:
            _validate_sha256(
                runtime_resolution_sha256, field="runtime_resolution_sha256"
            )
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer from 1 through 3600")
        token_sha256 = sha256_text(lease_token)
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)

        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            if RunStatus(run["status"]) is not RunStatus.RUNNING:
                raise InvalidTransitionError("execution may be acquired only for a running run")
            if self._unconfirmed_runtime_turns(connection, run_id):
                raise ConflictError(
                    "cannot acquire execution without a confirmed terminal turn"
                )
            existing = connection.execute(
                "SELECT * FROM execution_segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["run_id"] != run_id
                    or existing["worker_id"] != worker_id
                    or existing["runtime_version"] != runtime_version
                    or existing["runtime_resolution_sha256"]
                    != runtime_resolution_sha256
                    or not hmac.compare_digest(existing["lease_token_sha256"], token_sha256)
                ):
                    raise ConflictError("execution segment idempotency key was reused")
                return self._execution_from_row(existing)

            current = connection.execute(
                """
                SELECT * FROM execution_segments
                WHERE run_id = ? AND released_at IS NULL
                ORDER BY version DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if current is not None:
                if _parse_time(current["lease_expires_at"]) > now:
                    raise ConflictError("run already has an active execution lease")
                connection.execute(
                    "UPDATE execution_segments SET released_at = ?, updated_at = ? WHERE id = ?",
                    (_render_time(now), _render_time(now), current["id"]),
                )

            version = int(run["execution_version"]) + 1
            connection.execute(
                "UPDATE runs SET execution_version = ?, updated_at = ? WHERE id = ?",
                (version, _render_time(now), run_id),
            )
            connection.execute(
                """
                INSERT INTO execution_segments (
                    id, run_id, worker_id, version, lease_token_sha256,
                    runtime_version, runtime_resolution_sha256, lease_expires_at,
                    released_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    segment_id,
                    run_id,
                    worker_id,
                    version,
                    token_sha256,
                    runtime_version,
                    runtime_resolution_sha256,
                    _render_time(expires_at),
                    _render_time(now),
                    _render_time(now),
                ),
            )
            self._append_event(
                connection,
                run_id,
                event_type="execution.acquired",
                stage=RunStage(run["stage"]),
                payload={"segment_id": segment_id, "version": version},
                created_at=now,
            )
        return self.get_execution(segment_id)

    def heartbeat_execution(
        self,
        execution: ExecutionToken,
        *,
        lease_seconds: int = 60,
    ) -> ExecutionLease:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer from 1 through 3600")
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction() as connection:
            row = self._validate_execution_token(connection, execution, now=now)
            connection.execute(
                """
                UPDATE execution_segments
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (_render_time(expires_at), _render_time(now), row["id"]),
            )
        return self.get_execution(execution.segment_id)

    def record_execution_resolution(
        self,
        execution: ExecutionToken,
        *,
        runtime_version: str,
        resolution: JsonValue,
    ) -> JsonValue:
        """Bind a full, content-addressed runtime resolution before model work."""

        _validate_nonempty(runtime_version, field="runtime_version")
        try:
            provenance = RuntimeProvenance.model_validate(resolution)
        except ValueError as error:
            raise StoreIntegrityError(f"runtime provenance schema failure: {error}") from error
        if provenance.codex_runtime_version != runtime_version:
            raise ConflictError("observed runtime version does not match provenance")
        rendered = canonical_json(resolution)
        resolution_sha256 = canonical_sha256(resolution)
        now = self._now()
        with self._transaction() as connection:
            segment = self._validate_execution_token(connection, execution, now=now)
            if segment["runtime_version"] not in {None, runtime_version}:
                raise ConflictError("execution runtime version is immutable")
            if segment["runtime_resolution_sha256"] not in {
                None,
                resolution_sha256,
            }:
                raise ConflictError("execution runtime resolution is immutable")
            existing = connection.execute(
                "SELECT * FROM runtime_resolutions WHERE segment_id = ?",
                (segment["id"],),
            ).fetchone()
            run_provenance = connection.execute(
                "SELECT * FROM runtime_provenance WHERE run_id = ?",
                (segment["run_id"],),
            ).fetchone()
            if existing is not None:
                if (
                    existing["run_id"] != segment["run_id"]
                    or existing["resolution_json"] != rendered
                    or existing["resolution_sha256"] != resolution_sha256
                ):
                    raise ConflictError("execution runtime resolution is immutable")
                return cast(JsonValue, json.loads(existing["resolution_json"]))
            connection.execute(
                """
                UPDATE execution_segments
                SET runtime_version = ?, runtime_resolution_sha256 = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    runtime_version,
                    resolution_sha256,
                    _render_time(now),
                    segment["id"],
                ),
            )
            if run_provenance is None:
                connection.execute(
                    """
                    INSERT INTO runtime_provenance (
                        run_id, provenance_json, provenance_sha256, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        segment["run_id"],
                        rendered,
                        canonical_sha256(provenance),
                        _render_time(now),
                    ),
                )
            connection.execute(
                """
                INSERT INTO runtime_resolutions (
                    segment_id, run_id, schema_version, resolution_json,
                    resolution_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    segment["id"],
                    segment["run_id"],
                    SCHEMA_VERSION,
                    rendered,
                    resolution_sha256,
                    _render_time(now),
                ),
            )
            run = self._require_run_row(connection, segment["run_id"])
            self._append_event(
                connection,
                segment["run_id"],
                event_type="execution.runtime_resolved",
                stage=RunStage(run["stage"]),
                payload={
                    "segment_id": segment["id"],
                    "runtime_version": runtime_version,
                    "resolution_sha256": resolution_sha256,
                },
                created_at=now,
            )
        return cast(JsonValue, json.loads(rendered))

    def get_execution_resolution(self, segment_id: str) -> JsonValue:
        _validate_id(segment_id, field="segment_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runtime_resolutions WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"execution runtime resolution not found: {segment_id}"
                )
            return self._runtime_resolution_from_row(row).resolution

    def release_execution(self, execution: ExecutionToken) -> ExecutionLease:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_execution_row(connection, execution.segment_id)
            self._validate_execution_identity(row, execution)
            if row["released_at"] is not None:
                return self._execution_from_row(row)
            if self._unconfirmed_runtime_turns(connection, row["run_id"]):
                raise ConflictError(
                    "cannot release execution without a confirmed terminal turn"
                )
            connection.execute(
                """
                UPDATE execution_segments SET released_at = ?, updated_at = ? WHERE id = ?
                """,
                (_render_time(now), _render_time(now), row["id"]),
            )
            run = self._require_run_row(connection, row["run_id"])
            self._append_event(
                connection,
                row["run_id"],
                event_type="execution.released",
                stage=RunStage(run["stage"]),
                payload={"segment_id": row["id"], "version": row["version"]},
                created_at=now,
            )
        return self.get_execution(execution.segment_id)

    def get_execution(self, segment_id: str) -> ExecutionLease:
        _validate_id(segment_id, field="segment_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM execution_segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"execution segment not found: {segment_id}")
            return self._execution_from_row(row)

    def list_execution_segments(self, run_id: str) -> tuple[ExecutionLease, ...]:
        _validate_id(run_id, field="run_id")
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                """
                SELECT * FROM execution_segments
                WHERE run_id = ? ORDER BY version
                """,
                (run_id,),
            ).fetchall()
            return tuple(self._execution_from_row(row) for row in rows)

    def latest_stage_entry(self, run_id: str, stage: RunStage) -> Event | None:
        _validate_id(run_id, field="run_id")
        with self._lock:
            self._require_run_row(self._connection, run_id)
            row = self._connection.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND event_type = 'run.stage_changed' AND stage = ?
                ORDER BY seq DESC LIMIT 1
                """,
                (run_id, stage.value),
            ).fetchone()
            return self._event_from_row(row) if row is not None else None

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"run not found: {run_id}")
            return self._run_from_row(row)

    def list_runs(self) -> tuple[RunRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs ORDER BY created_at, id"
            ).fetchall()
            return tuple(self._run_from_row(row) for row in rows)

    def abandon_run(
        self,
        run_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> OperatorDecisionRecord:
        """Record an immutable, non-PASS operator terminal decision.

        This does not invent a runtime terminal event. A late, authentic runtime
        terminal may still be appended by the fenced execution segment.
        """

        _validate_run_id(run_id)
        _validate_id(idempotency_key, field="idempotency_key")
        _validate_nonempty(reason, field="reason")
        if len(reason) > 2_000:
            raise ValueError("reason must not exceed 2000 characters")
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            prior_rows = connection.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND event_type = 'operator.run_abandoned'
                ORDER BY seq
                """,
                (run_id,),
            ).fetchall()
            for prior_row in prior_rows:
                payload = json.loads(prior_row["payload_json"])
                if not isinstance(payload, dict):
                    raise StoreIntegrityError(
                        "operator decision event has an invalid payload"
                    )
                if payload.get("idempotency_key") != idempotency_key:
                    continue
                if payload.get("reason") != reason:
                    raise ConflictError(
                        "operator idempotency key was reused with another reason"
                    )
                return OperatorDecisionRecord(
                    run_id=run_id,
                    action="abandon",
                    idempotency_key=idempotency_key,
                    reason=reason,
                    status_before=RunStatus(str(payload["from"])),
                    status_after=RunStatus(str(payload["to"])),
                    event_seq=int(prior_row["seq"]),
                    created_at=_parse_time(prior_row["created_at"]),
                    replayed=True,
                )
            if prior_rows:
                raise ConflictError(
                    "run already has an immutable operator abandon decision"
                )

            current = RunStatus(row["status"])
            if current in {RunStatus.CANCELLED, RunStatus.COMPLETED}:
                raise InvalidTransitionError(
                    f"cannot abandon a run while it is {current.value}"
                )
            live_execution = connection.execute(
                """
                SELECT id FROM execution_segments
                WHERE run_id = ? AND released_at IS NULL AND lease_expires_at > ?
                ORDER BY version DESC LIMIT 1
                """,
                (run_id, _render_time(now)),
            ).fetchone()
            if live_execution is not None:
                raise ConflictError(
                    "cannot abandon a run with a live execution lease: "
                    f"{live_execution['id']}"
                )

            target = RunStatus.FAILED
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = 0, resumable = 0, updated_at = ?
                WHERE id = ?
                """,
                (target.value, _render_time(now), run_id),
            )
            decision_event = self._append_event(
                connection,
                run_id,
                event_type="operator.run_abandoned",
                stage=RunStage(row["stage"]),
                payload={
                    "action": "abandon",
                    "idempotency_key": idempotency_key,
                    "reason": reason,
                    "from": current.value,
                    "to": target.value,
                },
                created_at=now,
            )
            if current is not target:
                self._append_event(
                    connection,
                    run_id,
                    event_type="run.status_changed",
                    stage=RunStage(row["stage"]),
                    payload={
                        "from": current.value,
                        "to": target.value,
                        "operator_decision_seq": decision_event.seq,
                    },
                    created_at=now,
                )
        return OperatorDecisionRecord(
            run_id=run_id,
            action="abandon",
            idempotency_key=idempotency_key,
            reason=reason,
            status_before=current,
            status_after=target,
            event_seq=decision_event.seq,
            created_at=decision_event.created_at,
        )

    def transition_run(
        self,
        run_id: str,
        target: RunStatus,
        *,
        execution: ExecutionToken | None = None,
    ) -> RunRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            if target is not RunStatus.RUNNING:
                unleased_failure = (
                    target is RunStatus.FAILED
                    and execution is None
                    and connection.execute(
                        """
                        SELECT 1 FROM execution_segments
                        WHERE run_id = ? AND released_at IS NULL
                        LIMIT 1
                        """,
                        (run_id,),
                    ).fetchone()
                    is None
                )
                if not unleased_failure:
                    self._authorize_execution(connection, run_id, execution)
            current = RunStatus(row["status"])
            if target not in RUN_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"invalid run transition: {current.value} -> {target.value}"
                )
            if target is RunStatus.COMPLETED and RunStage(row["stage"]) is not RunStage.COMPLETE:
                raise InvalidTransitionError("a run can complete only from the complete stage")
            if target is RunStatus.COMPLETED:
                self._validate_completion_gate(connection, run_id)
            cancellation_requested = target in {RunStatus.CANCELLING, RunStatus.CANCELLED}
            resumable = target in {RunStatus.PAUSED, RunStatus.CANCELLED, RunStatus.FAILED}
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = ?, resumable = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target.value,
                    int(cancellation_requested),
                    int(resumable),
                    _render_time(now),
                    run_id,
                ),
            )
            self._append_event(
                connection,
                run_id,
                event_type="run.status_changed",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": target.value},
                created_at=now,
            )
        return self.get_run(run_id)

    def pause_unleased_run(self, run_id: str) -> RunRecord:
        """Pause a claimed command that was cancelled before acquiring capacity."""

        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
            if current is RunStatus.PAUSED:
                return self._run_from_row(row)
            if current is not RunStatus.RUNNING:
                raise InvalidTransitionError(
                    f"cannot pause an unleased run while it is {current.value}"
                )
            live = connection.execute(
                """
                SELECT 1 FROM execution_segments
                WHERE run_id = ? AND released_at IS NULL AND lease_expires_at > ?
                LIMIT 1
                """,
                (run_id, _render_time(now)),
            ).fetchone()
            if live is not None:
                raise ConflictError("cannot pause a run with a live execution lease")
            connection.execute(
                """
                UPDATE runs
                SET status = ?, resumable = 1, updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.PAUSED.value, _render_time(now), run_id),
            )
            self._append_event(
                connection,
                run_id,
                event_type="run.status_changed",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": RunStatus.PAUSED.value},
                created_at=now,
            )
        return self.get_run(run_id)

    def request_cancel(self, run_id: str) -> RunRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
            if current in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                return self._run_from_row(row)
            if RunStatus.CANCELLING not in RUN_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"cannot request cancellation while run is {current.value}"
                )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = 1, resumable = 0, updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.CANCELLING.value, _render_time(now), run_id),
            )
            self._append_event(
                connection,
                run_id,
                event_type="run.cancel_requested",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": RunStatus.CANCELLING.value},
                created_at=now,
            )
        return self.get_run(run_id)

    def cancel_run_command(
        self,
        run_id: str,
        *,
        idempotency_key: str,
    ) -> CancelCommandResult:
        _validate_run_id(run_id)
        _validate_id(idempotency_key, field="idempotency_key")
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            prior_command = connection.execute(
                """
                SELECT accepted_status, accepted_execution_version
                FROM cancel_commands
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if prior_command is not None:
                return CancelCommandResult(
                    run=self._run_from_row(row),
                    replayed=True,
                    accepted_status=RunStatus(prior_command["accepted_status"]),
                    accepted_execution_version=int(
                        prior_command["accepted_execution_version"]
                    ),
                )
            current = RunStatus(row["status"])
            if RunStatus.CANCELLING not in RUN_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"cannot request cancellation while run is {current.value}"
                )
            execution_version = int(row["execution_version"])
            connection.execute(
                """
                INSERT INTO cancel_commands (
                    run_id, idempotency_key, accepted_status,
                    accepted_execution_version, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    idempotency_key,
                    current.value,
                    execution_version,
                    _render_time(now),
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = 1, resumable = 0, updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.CANCELLING.value, _render_time(now), run_id),
            )
            self._append_event(
                connection,
                run_id,
                event_type="run.cancel_requested",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": RunStatus.CANCELLING.value},
                created_at=now,
            )
        return CancelCommandResult(
            run=self.get_run(run_id),
            replayed=False,
            accepted_status=current,
            accepted_execution_version=execution_version,
        )

    def acknowledge_cancel(
        self,
        run_id: str,
        *,
        execution: ExecutionToken | None = None,
    ) -> RunRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
            if current is RunStatus.CANCELLED:
                return self._run_from_row(row)
            if current is not RunStatus.CANCELLING:
                raise InvalidTransitionError(
                    f"cannot acknowledge cancellation while run is {current.value}"
                )
            if self._unconfirmed_runtime_turns(connection, run_id):
                raise ConflictError(
                    "cannot acknowledge cancellation without a confirmed terminal turn"
                )
            active_segments = connection.execute(
                """
                SELECT * FROM execution_segments
                WHERE run_id = ? AND released_at IS NULL
                ORDER BY version DESC
                """,
                (run_id,),
            ).fetchall()
            live_segments = tuple(
                segment
                for segment in active_segments
                if _parse_time(segment["lease_expires_at"]) > now
            )
            if live_segments:
                if execution is None:
                    raise ConflictError(
                        "the active execution lease must acknowledge cancellation"
                    )
                owner = live_segments[0]
                self._validate_execution_identity(owner, execution)
                if (
                    owner["run_id"] != run_id
                    or int(owner["version"]) != int(row["execution_version"])
                ):
                    raise ConflictError("stale execution token")

            active_threads = connection.execute(
                """
                SELECT id, status FROM threads
                WHERE run_id = ? AND status = ?
                ORDER BY created_at, id
                """,
                (run_id, ThreadStatus.ACTIVE.value),
            ).fetchall()
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = 1, resumable = 1, updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.CANCELLED.value, _render_time(now), run_id),
            )
            for segment in active_segments:
                connection.execute(
                    """
                    UPDATE execution_segments
                    SET released_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_render_time(now), _render_time(now), segment["id"]),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="execution.released",
                    stage=RunStage(row["stage"]),
                    payload={
                        "segment_id": segment["id"],
                        "version": segment["version"],
                    },
                    created_at=now,
                )
            for thread in active_threads:
                connection.execute(
                    "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
                    (
                        ThreadStatus.CANCELLED.value,
                        _render_time(now),
                        thread["id"],
                    ),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="thread.status_changed",
                    stage=RunStage(row["stage"]),
                    payload={
                        "thread_id": thread["id"],
                        "from": ThreadStatus.ACTIVE.value,
                        "to": ThreadStatus.CANCELLED.value,
                    },
                    created_at=now,
                )
            self._append_event(
                connection,
                run_id,
                event_type="run.cancelled",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": RunStatus.CANCELLED.value},
                created_at=now,
            )
        return self.get_run(run_id)

    def resume_run(
        self, run_id: str, *, idempotency_key: str | None = None
    ) -> RunRecord:
        return self.resume_run_command(
            run_id,
            idempotency_key=idempotency_key,
        ).run

    def start_run_command(
        self,
        run_id: str,
        *,
        idempotency_key: str,
    ) -> StartCommandResult:
        _validate_run_id(run_id)
        _validate_id(idempotency_key, field="idempotency_key")
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            prior_command = connection.execute(
                """
                SELECT accepted_status FROM start_commands
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if prior_command is not None:
                return StartCommandResult(
                    run=self._run_from_row(row),
                    replayed=True,
                    accepted_status=RunStatus(prior_command["accepted_status"]),
                )
            current = RunStatus(row["status"])
            if current is not RunStatus.CREATED:
                raise InvalidTransitionError(
                    f"run cannot start while it is {current.value}"
                )
            connection.execute(
                """
                INSERT INTO start_commands (
                    run_id, idempotency_key, accepted_status, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, idempotency_key, current.value, _render_time(now)),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = 0, resumable = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.RUNNING.value, _render_time(now), run_id),
            )
            self._append_event(
                connection,
                run_id,
                event_type="run.status_changed",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": RunStatus.RUNNING.value},
                created_at=now,
            )
        return StartCommandResult(
            run=self.get_run(run_id),
            replayed=False,
            accepted_status=current,
        )

    def resume_run_command(
        self,
        run_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> ResumeCommandResult:
        if idempotency_key is not None:
            _validate_id(idempotency_key, field="idempotency_key")
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
            if idempotency_key is not None:
                prior_command = connection.execute(
                    """
                    SELECT accepted_status FROM resume_commands
                    WHERE run_id = ? AND idempotency_key = ?
                    """,
                    (run_id, idempotency_key),
                ).fetchone()
                if prior_command is not None:
                    return ResumeCommandResult(
                        run=self._run_from_row(row),
                        replayed=True,
                        accepted_status=RunStatus(prior_command["accepted_status"]),
                    )
            if not bool(row["resumable"]) or current not in {
                RunStatus.PAUSED,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
            }:
                raise InvalidTransitionError(f"run is not resumable while {current.value}")
            if self._unconfirmed_runtime_turns(connection, run_id):
                raise ConflictError(
                    "run has an unconfirmed terminal runtime turn"
                )
            live_execution = connection.execute(
                """
                SELECT 1 FROM execution_segments
                WHERE run_id = ? AND released_at IS NULL AND lease_expires_at > ?
                LIMIT 1
                """,
                (run_id, _render_time(now)),
            ).fetchone()
            if live_execution is not None:
                raise ConflictError("run still has a live execution lease")
            if idempotency_key is not None:
                connection.execute(
                    """
                    INSERT INTO resume_commands (
                        run_id, idempotency_key, accepted_status, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, idempotency_key, current.value, _render_time(now)),
                )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = 0, resumable = 0,
                    resume_count = resume_count + 1, last_resume_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    RunStatus.RUNNING.value,
                    idempotency_key,
                    _render_time(now),
                    run_id,
                ),
            )
            self._append_event(
                connection,
                run_id,
                event_type="run.resumed",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": RunStatus.RUNNING.value},
                created_at=now,
            )
        return ResumeCommandResult(
            run=self.get_run(run_id),
            replayed=False,
            accepted_status=current,
        )

    def get_resume_command_status(
        self,
        run_id: str,
        *,
        idempotency_key: str,
    ) -> RunStatus | None:
        _validate_run_id(run_id)
        _validate_id(idempotency_key, field="idempotency_key")
        with self._lock:
            self._require_run_row(self._connection, run_id)
            row = self._connection.execute(
                """
                SELECT accepted_status FROM resume_commands
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
        return None if row is None else RunStatus(row["accepted_status"])

    def transition_stage(
        self,
        run_id: str,
        target: RunStage,
        *,
        execution: ExecutionToken | None = None,
    ) -> RunRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            self._authorize_execution(connection, run_id, execution)
            if RunStatus(row["status"]) is not RunStatus.RUNNING:
                raise InvalidTransitionError("stages may transition only while a run is running")
            current = RunStage(row["stage"])
            if target not in STAGE_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"invalid stage transition: {current.value} -> {target.value}"
                )
            self._validate_stage_gate(connection, row, current=current, target=target)
            plan_revision_increment = int(
                current is RunStage.ADJUDICATION and target is RunStage.PLANNING
            )
            strategy_rewrite_increment = int(
                current is RunStage.ADJUDICATION and target is RunStage.LITERATURE
            )
            connection.execute(
                """
                UPDATE runs
                SET stage = ?, plan_revision_count = plan_revision_count + ?,
                    strategy_rewrite_count = strategy_rewrite_count + ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target.value,
                    plan_revision_increment,
                    strategy_rewrite_increment,
                    _render_time(now),
                    run_id,
                ),
            )
            self._append_event(
                connection,
                run_id,
                event_type="run.stage_changed",
                stage=target,
                payload={"from": current.value, "to": target.value},
                created_at=now,
            )
        return self.get_run(run_id)

    def add_evidence(
        self,
        run_id: str,
        evidence: Evidence,
        *,
        execution: ExecutionToken | None = None,
    ) -> Evidence:
        return self.add_evidence_batch(
            run_id,
            (evidence,),
            execution=execution,
        )[0]

    def add_web_search_observation(
        self,
        observation: WebSearchObservation,
        *,
        execution: ExecutionToken,
    ) -> WebSearchObservation:
        """Persist one URL-bearing native web-search action observed by QED."""

        _validate_id(observation.id, field="observation.id")
        _validate_id(observation.run_id, field="observation.run_id")
        rendered = canonical_json(observation)
        now = self._now()
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, observation.run_id)
                self._authorize_runtime_lifecycle(
                    connection,
                    observation.run_id,
                    execution,
                )
                stage = RunStage(run["stage"])
                if stage not in {RunStage.LITERATURE, RunStage.VERIFICATION}:
                    raise InvalidTransitionError(
                        "web-search observations require literature or verification"
                    )
                thread = self._require_thread_row(
                    connection,
                    observation.local_thread_id,
                )
                expected_role = (
                    ThreadRole.LITERATURE
                    if stage is RunStage.LITERATURE
                    else ThreadRole.VERIFIER
                )
                if (
                    thread["run_id"] != observation.run_id
                    or ThreadRole(thread["role"]) is not expected_role
                    or thread["external_thread_id"]
                    != observation.external_thread_id
                ):
                    raise ConflictError(
                        "web-search observation thread identity does not match"
                    )
                turn_rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM events
                    WHERE run_id = ? AND event_type = 'runtime.turn_started'
                    ORDER BY seq
                    """,
                    (observation.run_id,),
                ).fetchall()
                turn_observed = any(
                    (
                        (payload := json.loads(row["payload_json"])).get(
                            "thread_id"
                        )
                        == observation.external_thread_id
                        and payload.get("turn_id") == observation.turn_id
                        and payload.get("backend") == observation.backend
                    )
                    for row in turn_rows
                )
                if not turn_observed:
                    raise ConflictError(
                        "web-search observation requires its runtime turn identity"
                    )
                action = observation.payload.get("action")
                action_types = {
                    "openPage": "open_page",
                    "open_page": "open_page",
                    "findInPage": "find_in_page",
                    "find_in_page": "find_in_page",
                }
                runtime_action_type = (
                    action.get("type") if isinstance(action, dict) else None
                )
                if (
                    observation.payload.get("id") != observation.item_id
                    or observation.payload.get("type")
                    not in {"webSearch", "web_search", "web_search_call"}
                    or not isinstance(action, dict)
                    or not isinstance(runtime_action_type, str)
                    or action_types.get(runtime_action_type)
                    != observation.action_type
                    or action.get("url") != observation.uri
                ):
                    raise ConflictError(
                        "web-search observation does not match its runtime payload"
                    )
                existing = connection.execute(
                    "SELECT * FROM web_search_observations WHERE id = ?",
                    (observation.id,),
                ).fetchone()
                if existing is not None:
                    persisted = self._web_search_observation_from_row(existing)
                    if persisted != observation:
                        raise ImmutableRecordError(
                            "web-search observation is immutable"
                        )
                    return persisted
                connection.execute(
                    """
                    INSERT INTO web_search_observations (
                        id, run_id, schema_version, backend,
                        external_thread_id, turn_id, item_id, uri_sha256,
                        observation_json, observation_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.id,
                        observation.run_id,
                        observation.schema_version,
                        observation.backend,
                        observation.external_thread_id,
                        observation.turn_id,
                        observation.item_id,
                        observation.uri_sha256,
                        rendered,
                        canonical_sha256(observation),
                        _render_time(now),
                    ),
                )
                self._append_event(
                    connection,
                    observation.run_id,
                    event_type="runtime.web_search_observed",
                    stage=stage,
                    payload={
                        "observation_id": observation.id,
                        "backend": observation.backend,
                        "thread_id": observation.external_thread_id,
                        "turn_id": observation.turn_id,
                        "item_id": observation.item_id,
                        "uri_sha256": observation.uri_sha256,
                        "payload_sha256": observation.payload_sha256,
                    },
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                "web-search observation identity already exists"
            ) from error
        return self.get_web_search_observation(observation.id)

    def get_web_search_observation(
        self,
        observation_id: str,
    ) -> WebSearchObservation:
        _validate_id(observation_id, field="observation_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM web_search_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"web-search observation not found: {observation_id}"
                )
            return self._web_search_observation_from_row(row)

    def list_web_search_observations(
        self,
        run_id: str,
    ) -> tuple[WebSearchObservation, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                """
                SELECT * FROM web_search_observations
                WHERE run_id = ? ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
            return tuple(
                self._web_search_observation_from_row(row) for row in rows
            )

    def add_evidence_batch(
        self,
        run_id: str,
        evidence: tuple[Evidence, ...],
        *,
        execution: ExecutionToken | None = None,
    ) -> tuple[Evidence, ...]:
        """Persist one model evidence batch atomically."""

        _validate_id(run_id, field="run_id")
        if not evidence:
            raise ValueError("evidence batch cannot be empty")
        for item in evidence:
            _validate_id(item.id, field="evidence.id")
        if len({item.id for item in evidence}) != len(evidence):
            raise ValueError("evidence batch ids must be unique")
        now = self._now()
        rendered = tuple(canonical_json(item) for item in evidence)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
                self._authorize_execution(connection, run_id, execution)
                if RunStage(run["stage"]) is not RunStage.LITERATURE:
                    raise InvalidTransitionError(
                        "typed evidence may be added only in literature"
                    )
                for item, item_json in zip(evidence, rendered, strict=True):
                    if item.source_trust is EvidenceTrust.RUNTIME_OBSERVED:
                        self._require_evidence_observations(
                            connection,
                            run_id,
                            item,
                        )
                    connection.execute(
                        """
                        INSERT INTO evidence (
                            id, run_id, schema_version, evidence_json,
                            evidence_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.id,
                            run_id,
                            item.schema_version,
                            item_json,
                            evidence_sha256(item),
                            _render_time(now),
                        ),
                    )
                    self._append_event(
                        connection,
                        run_id,
                        event_type="evidence.created",
                        stage=RunStage.LITERATURE,
                        payload={"evidence_id": item.id},
                        created_at=now,
                    )
                self._append_event(
                    connection,
                    run_id,
                    event_type="evidence.batch_created",
                    stage=RunStage.LITERATURE,
                    payload={"evidence_ids": [item.id for item in evidence]},
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("evidence batch contains an existing identity") from error
        return tuple(self.get_evidence(item.id) for item in evidence)

    def get_evidence(self, evidence_id: str) -> Evidence:
        _validate_id(evidence_id, field="evidence_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"evidence not found: {evidence_id}")
            return self._evidence_from_row(row)

    def list_evidence(self, run_id: str) -> tuple[Evidence, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            return tuple(self._evidence_from_row(row) for row in rows)

    def add_plan(
        self,
        run_id: str,
        plan: Plan,
        *,
        execution: ExecutionToken | None = None,
    ) -> Plan:
        _validate_id(run_id, field="run_id")
        _validate_id(plan.id, field="plan.id")
        now = self._now()
        rendered = canonical_json(plan)
        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            self._authorize_execution(connection, run_id, execution)
            if RunStage(run["stage"]) is not RunStage.PLANNING:
                raise InvalidTransitionError("typed plans may be added only in planning")
            if plan.problem_sha256 != run["input_sha256"]:
                raise ConflictError("plan problem hash does not match the frozen run input")
            evidence_ids = {
                evidence_id for step in plan.steps for evidence_id in step.evidence_ids
            }
            self._require_evidence_ids(connection, run_id, evidence_ids)
            try:
                connection.execute(
                    """
                    INSERT INTO plans (
                        id, run_id, schema_version, plan_json, plan_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.id,
                        run_id,
                        plan.schema_version,
                        rendered,
                        canonical_sha256(plan),
                        _render_time(now),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError(f"plan already exists: {plan.id}") from error
            self._append_event(
                connection,
                run_id,
                event_type="plan.created",
                stage=RunStage.PLANNING,
                payload={"plan_id": plan.id},
                created_at=now,
            )
        return self.get_plan(plan.id)

    def get_plan(self, plan_id: str) -> Plan:
        _validate_id(plan_id, field="plan_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"plan not found: {plan_id}")
            return self._plan_from_row(row)

    def list_plans(self, run_id: str) -> tuple[Plan, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._plan_rows_in_event_order(self._connection, run_id)
            return tuple(self._plan_from_row(row) for row in rows)

    def add_adjudication(
        self,
        run_id: str,
        adjudication: Adjudication,
        *,
        execution: ExecutionToken | None = None,
    ) -> Adjudication:
        _validate_id(run_id, field="run_id")
        _validate_id(adjudication.id, field="adjudication.id")
        now = self._now()
        rendered = canonical_json(adjudication)
        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            self._authorize_execution(connection, run_id, execution)
            if RunStage(run["stage"]) is not RunStage.ADJUDICATION:
                raise InvalidTransitionError(
                    "typed adjudication may be added only in adjudication"
                )
            candidate = self._require_candidate_row(connection, adjudication.candidate_id)
            if candidate["run_id"] != run_id or candidate["sealed_at"] is None:
                raise ConflictError("adjudication requires a sealed candidate from this run")
            thread_id = adjudication.provenance.source_id
            if thread_id is None:
                raise ConflictError("adjudication provenance requires an adjudicator thread")
            thread = self._require_thread_row(connection, thread_id)
            if (
                thread["run_id"] != run_id
                or ThreadRole(thread["role"]) is not ThreadRole.ADJUDICATOR
            ):
                raise ConflictError("adjudication requires an adjudicator thread")
            report_rows = tuple(
                self._require_verification_row(connection, report_id)
                for report_id in adjudication.report_ids
            )
            if {row["id"] for row in report_rows} != set(adjudication.report_ids) or any(
                row["candidate_id"] != adjudication.candidate_id for row in report_rows
            ):
                raise ConflictError("adjudication report ids must belong to its candidate")
            try:
                connection.execute(
                    """
                    INSERT INTO adjudications (
                        id, run_id, candidate_id, schema_version, adjudication_json,
                        adjudication_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        adjudication.id,
                        run_id,
                        adjudication.candidate_id,
                        adjudication.schema_version,
                        rendered,
                        canonical_sha256(adjudication),
                        _render_time(now),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError(
                    f"adjudication already exists: {adjudication.id}"
                ) from error
            self._append_event(
                connection,
                run_id,
                event_type="adjudication.created",
                stage=RunStage.ADJUDICATION,
                payload={
                    "adjudication_id": adjudication.id,
                    "candidate_id": adjudication.candidate_id,
                    "outcome": adjudication.outcome,
                },
                created_at=now,
            )
        return self.get_adjudication(adjudication.id)

    def get_adjudication(self, adjudication_id: str) -> Adjudication:
        _validate_id(adjudication_id, field="adjudication_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM adjudications WHERE id = ?", (adjudication_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"adjudication not found: {adjudication_id}")
            return self._adjudication_from_row(row)

    def list_adjudications(self, run_id: str) -> tuple[Adjudication, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._adjudication_rows_in_event_order(
                self._connection,
                run_id,
            )
            return tuple(self._adjudication_from_row(row) for row in rows)

    def record_decision(
        self,
        run_id: str,
        candidate_id: str,
        *,
        require_citation: bool | None = None,
        execution: ExecutionToken | None = None,
    ) -> CandidateDecision:
        _validate_id(run_id, field="run_id")
        _validate_id(candidate_id, field="candidate_id")
        now = self._now()
        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            self._authorize_execution(connection, run_id, execution)
            if RunStage(run["stage"]) is not RunStage.ADJUDICATION:
                raise InvalidTransitionError(
                    "candidate decisions may be recorded only in adjudication"
                )
            candidate_row = self._require_candidate_row(connection, candidate_id)
            if candidate_row["run_id"] != run_id or candidate_row["sealed_at"] is None:
                raise ConflictError("decision requires a sealed candidate from this run")
            candidate_record = self._candidate_from_row(candidate_row)
            candidate = candidate_record.candidate
            if candidate_record.thread_id is None:
                raise ConflictError("decision requires a prover thread identity")
            prover_thread = self._require_thread_row(
                connection,
                candidate_record.thread_id,
            )
            prover_external_thread_id = prover_thread["external_thread_id"]
            if (
                ThreadRole(prover_thread["role"]) is not ThreadRole.PROVER
                or not isinstance(prover_external_thread_id, str)
                or not prover_external_thread_id
            ):
                raise ConflictError(
                    "decision requires a prover external thread identity"
                )
            report_rows = connection.execute(
                """
                SELECT * FROM verifications
                WHERE run_id = ? AND candidate_id = ? ORDER BY created_at, id
                """,
                (run_id, candidate_id),
            ).fetchall()
            reports = tuple(self._verification_from_row(row).report for row in report_rows)
            evidence = tuple(
                self._evidence_from_row(evidence_row)
                for evidence_row in connection.execute(
                    "SELECT * FROM evidence WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
            )
            citation_required = bool(evidence)
            if require_citation is not None and require_citation != citation_required:
                raise ConflictError(
                    "citation policy must be derived from the frozen evidence ledger"
                )
            input_row = connection.execute(
                "SELECT input_json FROM run_inputs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if input_row is None:
                raise ConflictError("decision requires the frozen run input")
            run_input = RunInput.model_validate_json(input_row["input_json"])
            required_rule_ids = tuple(
                rule.id for rule in run_input.frozen_verification_rules
            )
            graph_rows = connection.execute(
                "SELECT content_json FROM stage_outputs WHERE run_id = ? AND kind = ?",
                (run_id, f"claim_graph:{candidate_id}"),
            ).fetchall()
            if len(graph_rows) != 1:
                raise ConflictError("decision requires exactly one immutable claim graph")
            try:
                graph = ProofObligationGraph.model_validate_json(graph_rows[0]["content_json"])
                graph.validate_against_proof(
                    candidate.proof,
                    evidence_ids=frozenset(item.id for item in evidence),
                    rule_ids=frozenset(required_rule_ids),
                )
            except ValueError as error:
                raise ConflictError(f"claim graph is invalid: {error}") from error
            required_roles = {
                VerifierRole.STRUCTURAL,
                VerifierRole.DETAILED_STEP,
                VerifierRole.ASSUMPTIONS_QUANTIFIERS,
                VerifierRole.COUNTEREXAMPLE_EDGE_CASE,
                VerifierRole.RECONSTRUCTION,
            }
            if evidence:
                required_roles.add(VerifierRole.CITATION)
            covered_roles = {
                coverage.role
                for node in graph.nodes
                for coverage in node.coverage
            }
            if not required_roles.issubset(covered_roles):
                raise ConflictError("claim graph does not cover every required verifier role")
            decision = decide_stable_candidate(
                candidate,
                reports,
                prover_external_thread_id=prover_external_thread_id,
                required_evidence=evidence,
                required_rule_ids=required_rule_ids,
            )
            rendered = canonical_json(decision)
            existing = connection.execute(
                "SELECT * FROM candidate_decisions WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if existing is not None:
                persisted = self._decision_from_row(existing)
                if persisted != decision:
                    raise ImmutableRecordError("candidate decision is immutable")
                return persisted
            connection.execute(
                """
                INSERT INTO candidate_decisions (
                    candidate_id, run_id, schema_version, decision_json,
                    decision_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    run_id,
                    decision.schema_version,
                    rendered,
                    candidate_decision_sha256(decision),
                    _render_time(now),
                ),
            )
            self._append_event(
                connection,
                run_id,
                event_type="candidate.decision_recorded",
                stage=RunStage.ADJUDICATION,
                payload={"candidate_id": candidate_id, "passed": decision.passed},
                created_at=now,
            )
            return decision

    def get_decision(self, candidate_id: str) -> CandidateDecision:
        _validate_id(candidate_id, field="candidate_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM candidate_decisions WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"candidate decision not found: {candidate_id}")
            return self._decision_from_row(row)

    def list_decisions(self, run_id: str) -> tuple[CandidateDecision, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                """
                SELECT * FROM candidate_decisions
                WHERE run_id = ? ORDER BY created_at, candidate_id
                """,
                (run_id,),
            ).fetchall()
            return tuple(self._decision_from_row(row) for row in rows)

    def add_stage_output(
        self,
        output_id: str,
        *,
        run_id: str,
        stage: RunStage,
        kind: str,
        content: JsonValue,
        provenance: Provenance,
        execution: ExecutionToken | None = None,
    ) -> StageOutputRecord:
        _validate_id(output_id, field="output_id")
        _validate_id(run_id, field="run_id")
        _validate_nonempty(kind, field="kind")
        now = self._now()
        content_json = canonical_json(content)
        provenance_json = canonical_json(provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
                self._authorize_execution(connection, run_id, execution)
                if RunStage(run["stage"]) is not stage:
                    raise InvalidTransitionError(
                        "stage output stage must equal the run's current stage"
                    )
                connection.execute(
                    """
                    INSERT INTO stage_outputs (
                        id, run_id, stage, kind, schema_version, content_json,
                        content_sha256, provenance_json, provenance_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        output_id,
                        run_id,
                        stage.value,
                        kind,
                        SCHEMA_VERSION,
                        content_json,
                        canonical_sha256(content),
                        provenance_json,
                        canonical_sha256(provenance),
                        _render_time(now),
                    ),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="stage.output_created",
                    stage=stage,
                    payload={"output_id": output_id, "kind": kind},
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"stage output already exists: {output_id}") from error
        return self.get_stage_output(output_id)

    def get_stage_output(self, output_id: str) -> StageOutputRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM stage_outputs WHERE id = ?", (output_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"stage output not found: {output_id}")
            return self._stage_output_from_row(row)

    def list_stage_outputs(self, run_id: str) -> tuple[StageOutputRecord, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                "SELECT * FROM stage_outputs WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            return tuple(self._stage_output_from_row(row) for row in rows)

    def put_turn_input(
        self,
        input_id: str,
        *,
        run_id: str,
        role: str,
        prompt_version: str,
        output_schema_sha256: Sha256,
        payload: dict[str, JsonValue],
        payload_sha256: Sha256,
        execution: ExecutionToken | None = None,
    ) -> TurnInputRecord:
        """Persist one frozen model input and audit every selection of it."""

        _validate_id(input_id, field="input_id")
        _validate_id(run_id, field="run_id")
        _validate_nonempty(role, field="role")
        _validate_nonempty(prompt_version, field="prompt_version")
        _validate_sha256(output_schema_sha256, field="output_schema_sha256")
        _validate_sha256(payload_sha256, field="payload_sha256")
        if canonical_sha256(payload) != payload_sha256:
            raise ValueError("payload_sha256 does not match turn input payload")
        now = self._now()
        rendered = canonical_json(payload)
        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            self._authorize_execution(connection, run_id, execution)
            existing = connection.execute(
                "SELECT * FROM turn_inputs WHERE id = ?",
                (input_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO turn_inputs (
                        id, run_id, role, prompt_version, output_schema_sha256,
                        payload_json, payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        input_id,
                        run_id,
                        role,
                        prompt_version,
                        output_schema_sha256,
                        rendered,
                        payload_sha256,
                        _render_time(now),
                    ),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="runtime.turn_input_frozen",
                    stage=RunStage(run["stage"]),
                    payload={
                        "turn_input_id": input_id,
                        "role": role,
                        "payload_sha256": payload_sha256,
                        "output_schema_sha256": output_schema_sha256,
                    },
                    created_at=now,
                )
            elif (
                existing["run_id"] != run_id
                or existing["role"] != role
                or existing["prompt_version"] != prompt_version
                or existing["output_schema_sha256"] != output_schema_sha256
                or existing["payload_json"] != rendered
                or existing["payload_sha256"] != payload_sha256
            ):
                raise ConflictError("turn input identity is immutable")
            self._append_event(
                connection,
                run_id,
                event_type="runtime.turn_input_selected",
                stage=RunStage(run["stage"]),
                payload={"turn_input_id": input_id},
                created_at=now,
            )
        return self.get_turn_input(input_id)

    def get_turn_input(self, input_id: str) -> TurnInputRecord:
        _validate_id(input_id, field="input_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM turn_inputs WHERE id = ?",
                (input_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"turn input not found: {input_id}")
            return self._turn_input_from_row(row)

    def list_turn_inputs(self, run_id: str) -> tuple[TurnInputRecord, ...]:
        _validate_id(run_id, field="run_id")
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                "SELECT * FROM turn_inputs WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            return tuple(self._turn_input_from_row(row) for row in rows)

    def add_thread(
        self,
        thread_id: str,
        *,
        run_id: str,
        role: ThreadRole | str,
        model: str,
        provenance: Provenance,
        parent_thread_id: str | None = None,
        external_thread_id: str | None = None,
        execution: ExecutionToken | None = None,
        runtime_lifecycle: bool = False,
    ) -> ThreadRecord:
        _validate_id(thread_id, field="thread_id")
        _validate_id(run_id, field="run_id")
        if parent_thread_id is not None:
            _validate_id(parent_thread_id, field="parent_thread_id")
        if external_thread_id is not None:
            _validate_id(external_thread_id, field="external_thread_id")
        now = self._now()
        role_value = ThreadRole(role)
        if role_value is ThreadRole.VERIFIER and external_thread_id is None:
            raise ValueError("verifier threads require an external_thread_id")
        provenance_json = canonical_json(provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
                if runtime_lifecycle:
                    self._authorize_runtime_lifecycle(connection, run_id, execution)
                else:
                    self._authorize_execution(connection, run_id, execution)
                if parent_thread_id is not None:
                    parent = self._require_thread_row(connection, parent_thread_id)
                    if parent["run_id"] != run_id:
                        raise ConflictError("parent thread belongs to another run")
                connection.execute(
                    """
                    INSERT INTO threads (
                        id, run_id, role, parent_thread_id, external_thread_id, model,
                        status, schema_version, provenance_json, provenance_sha256,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        run_id,
                        role_value.value,
                        parent_thread_id,
                        external_thread_id,
                        model,
                        ThreadStatus.ACTIVE.value,
                        SCHEMA_VERSION,
                        provenance_json,
                        canonical_sha256(provenance),
                        _render_time(now),
                        _render_time(now),
                    ),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="thread.created",
                    stage=RunStage(run["stage"]),
                    payload={"thread_id": thread_id, "role": role_value.value},
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"thread already exists or has invalid references: {thread_id}"
            ) from error
        return self.get_thread(thread_id)

    def get_thread(self, thread_id: str) -> ThreadRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"thread not found: {thread_id}")
            return self._thread_from_row(row)

    def list_threads(self, run_id: str) -> tuple[ThreadRecord, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                "SELECT * FROM threads WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            return tuple(self._thread_from_row(row) for row in rows)

    def reserve_proof_attempt(
        self,
        run_id: str,
        *,
        execution: ExecutionToken | None = None,
    ) -> int:
        """Consume one durable proof-attempt slot before contacting a prover."""

        _validate_id(run_id, field="run_id")
        now = self._now()
        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            self._authorize_execution(connection, run_id, execution)
            if RunStage(run["stage"]) is not RunStage.PROVING:
                raise InvalidTransitionError(
                    "proof attempts may be reserved only in proving"
                )
            config = QEDConfig.model_validate_json(run["config_json"])
            attempt = int(run["proof_attempt_count"]) + 1
            if attempt > config.budgets.proof_attempts:
                raise ConflictError("proof attempt budget exhausted")
            connection.execute(
                """
                UPDATE runs
                SET proof_attempt_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempt, _render_time(now), run_id),
            )
            self._append_event(
                connection,
                run_id,
                event_type="proof.attempt_reserved",
                stage=RunStage.PROVING,
                payload={"attempt": attempt},
                created_at=now,
            )
        return attempt

    def transition_thread(
        self,
        thread_id: str,
        target: ThreadStatus,
        *,
        execution: ExecutionToken | None = None,
        runtime_lifecycle: bool = False,
    ) -> ThreadRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_thread_row(connection, thread_id)
            if runtime_lifecycle:
                self._authorize_runtime_lifecycle(
                    connection, row["run_id"], execution
                )
            else:
                self._authorize_execution(connection, row["run_id"], execution)
            current = ThreadStatus(row["status"])
            if target not in THREAD_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"invalid thread transition: {current.value} -> {target.value}"
                )
            connection.execute(
                "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, _render_time(now), thread_id),
            )
            run = self._require_run_row(connection, row["run_id"])
            self._append_event(
                connection,
                row["run_id"],
                event_type="thread.status_changed",
                stage=RunStage(run["stage"]),
                payload={
                    "thread_id": thread_id,
                    "from": current.value,
                    "to": target.value,
                },
                created_at=now,
            )
        return self.get_thread(thread_id)

    def create_candidate(
        self,
        candidate: ProofCandidate,
        *,
        thread_id: str | None = None,
        sealed: bool = False,
        execution: ExecutionToken | None = None,
    ) -> CandidateRecord:
        _validate_id(candidate.id, field="candidate.id")
        _validate_id(candidate.run_id, field="candidate.run_id")
        _validate_id(candidate.plan_id, field="candidate.plan_id")
        if thread_id is None:
            raise ConflictError("candidate requires a prover thread")
        _validate_id(thread_id, field="thread_id")
        now = self._now()
        candidate_json = canonical_json(candidate)
        provenance_json = canonical_json(candidate.provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, candidate.run_id)
                self._authorize_execution(connection, candidate.run_id, execution)
                if RunStage(run["stage"]) is not RunStage.PROVING:
                    raise InvalidTransitionError(
                        "proof candidates may be created only in proving"
                    )
                thread = self._require_thread_row(connection, thread_id)
                if (
                    thread["run_id"] != candidate.run_id
                    or ThreadRole(thread["role"]) is not ThreadRole.PROVER
                ):
                    raise ConflictError("candidate requires a prover thread from its run")
                if sealed and ThreadStatus(thread["status"]) is not ThreadStatus.COMPLETED:
                    raise ConflictError("sealed candidate requires a completed prover thread")
                if candidate.provenance.source_id != thread_id:
                    raise ConflictError("candidate provenance must bind its prover thread")
                plan = connection.execute(
                    "SELECT run_id FROM plans WHERE id = ?", (candidate.plan_id,)
                ).fetchone()
                if plan is None or plan["run_id"] != candidate.run_id:
                    raise ConflictError("candidate requires a typed plan from its run")
                self._require_evidence_ids(
                    connection, candidate.run_id, set(candidate.evidence_ids)
                )
                config = QEDConfig.model_validate_json(run["config_json"])
                attempt_count = int(run["proof_attempt_count"])
                auto_reserve = candidate.attempt == attempt_count + 1
                if auto_reserve and candidate.attempt > config.budgets.proof_attempts:
                    raise ConflictError("proof attempt budget exhausted")
                if not auto_reserve and not 1 <= candidate.attempt <= attempt_count:
                    raise ConflictError("candidate requires a reserved proof attempt")
                if auto_reserve:
                    connection.execute(
                        """
                        UPDATE runs SET proof_attempt_count = proof_attempt_count + 1,
                            updated_at = ? WHERE id = ?
                        """,
                        (_render_time(now), candidate.run_id),
                    )
                    self._append_event(
                        connection,
                        candidate.run_id,
                        event_type="proof.attempt_reserved",
                        stage=RunStage.PROVING,
                        payload={"attempt": candidate.attempt},
                        created_at=now,
                    )
                connection.execute(
                    """
                    INSERT INTO candidates (
                        id, run_id, thread_id, plan_id, attempt, schema_version,
                        candidate_json, candidate_sha256, proof_sha256, provenance_json,
                        provenance_sha256, sealed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.id,
                        candidate.run_id,
                        thread_id,
                        candidate.plan_id,
                        candidate.attempt,
                        candidate.schema_version,
                        candidate_json,
                        canonical_sha256(candidate),
                        candidate.proof_sha256,
                        provenance_json,
                        canonical_sha256(candidate.provenance),
                        _render_time(now) if sealed else None,
                        _render_time(now),
                        _render_time(now),
                    ),
                )
                self._append_event(
                    connection,
                    candidate.run_id,
                    event_type="candidate.created",
                    stage=RunStage(run["stage"]),
                    payload={"candidate_id": candidate.id, "sealed": sealed},
                    created_at=now,
                )
                if sealed:
                    self._append_event(
                        connection,
                        candidate.run_id,
                        event_type="candidate.sealed",
                        stage=RunStage(run["stage"]),
                        payload={
                            "candidate_id": candidate.id,
                            "candidate_sha256": canonical_sha256(candidate),
                            "proof_sha256": candidate.proof_sha256,
                        },
                        created_at=now,
                    )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"candidate already exists: {candidate.id}") from error
        return self.get_candidate(candidate.id)

    def update_candidate(
        self,
        candidate: ProofCandidate,
        *,
        execution: ExecutionToken | None = None,
    ) -> CandidateRecord:
        now = self._now()
        candidate_json = canonical_json(candidate)
        provenance_json = canonical_json(candidate.provenance)
        try:
            with self._transaction() as connection:
                row = self._require_candidate_row(connection, candidate.id)
                self._authorize_execution(connection, row["run_id"], execution)
                if row["run_id"] != candidate.run_id:
                    raise ConflictError("candidate cannot move to another run")
                run = self._require_run_row(connection, candidate.run_id)
                if RunStage(run["stage"]) is not RunStage.PROVING:
                    raise InvalidTransitionError(
                        "proof candidates may be updated only in proving"
                    )
                original = self._candidate_from_row(row).candidate
                if (
                    candidate.plan_id != original.plan_id
                    or candidate.attempt != original.attempt
                    or candidate.provenance.source_id != original.provenance.source_id
                ):
                    raise ConflictError(
                        "candidate plan, attempt, and prover identity are immutable"
                    )
                self._require_evidence_ids(
                    connection, candidate.run_id, set(candidate.evidence_ids)
                )
                connection.execute(
                    """
                    UPDATE candidates
                    SET plan_id = ?, attempt = ?, schema_version = ?, candidate_json = ?,
                        candidate_sha256 = ?, proof_sha256 = ?, provenance_json = ?,
                        provenance_sha256 = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        candidate.plan_id,
                        candidate.attempt,
                        candidate.schema_version,
                        candidate_json,
                        canonical_sha256(candidate),
                        candidate.proof_sha256,
                        provenance_json,
                        canonical_sha256(candidate.provenance),
                        _render_time(now),
                        candidate.id,
                    ),
                )
                self._append_event(
                    connection,
                    candidate.run_id,
                    event_type="candidate.updated",
                    stage=RunStage(
                        self._require_run_row(connection, candidate.run_id)["stage"]
                    ),
                    payload={"candidate_id": candidate.id},
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            if "sealed candidate is immutable" in str(error):
                raise ImmutableRecordError("sealed candidate is immutable") from error
            raise ConflictError(f"candidate update failed: {candidate.id}") from error
        return self.get_candidate(candidate.id)

    def seal_candidate(
        self,
        candidate_id: str,
        *,
        execution: ExecutionToken | None = None,
    ) -> CandidateRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_candidate_row(connection, candidate_id)
            self._authorize_execution(connection, row["run_id"], execution)
            run = self._require_run_row(connection, row["run_id"])
            if RunStage(run["stage"]) is not RunStage.PROVING:
                raise InvalidTransitionError("candidates may be sealed only in proving")
            if row["sealed_at"] is not None:
                raise ImmutableRecordError("sealed candidate is immutable")
            connection.execute(
                "UPDATE candidates SET sealed_at = ?, updated_at = ? WHERE id = ?",
                (_render_time(now), _render_time(now), candidate_id),
            )
            self._append_event(
                connection,
                row["run_id"],
                event_type="candidate.sealed",
                stage=RunStage(run["stage"]),
                payload={
                    "candidate_id": candidate_id,
                    "candidate_sha256": row["candidate_sha256"],
                    "proof_sha256": row["proof_sha256"],
                },
                created_at=now,
            )
        return self.get_candidate(candidate_id)

    def get_candidate(self, candidate_id: str) -> CandidateRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"candidate not found: {candidate_id}")
            return self._candidate_from_row(row)

    def list_candidates(self, run_id: str) -> tuple[CandidateRecord, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY attempt, created_at, id",
                (run_id,),
            ).fetchall()
            return tuple(self._candidate_from_row(row) for row in rows)

    def add_verification(
        self,
        run_id: str,
        report: VerificationReport,
        *,
        execution: ExecutionToken | None = None,
    ) -> VerificationRecord:
        _validate_id(run_id, field="run_id")
        _validate_id(report.id, field="report.id")
        now = self._now()
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
                self._authorize_execution(connection, run_id, execution)
                if RunStage(run["stage"]) is not RunStage.VERIFICATION:
                    raise InvalidTransitionError(
                        "verification reports may be added only in verification"
                    )
                candidate = self._require_candidate_row(connection, report.candidate_id)
                if candidate["run_id"] != run_id:
                    raise ConflictError("verification candidate belongs to another run")
                if candidate["sealed_at"] is None:
                    raise ConflictError("verification requires a sealed candidate")
                if candidate["proof_sha256"] != report.candidate_sha256:
                    raise ConflictError("verification candidate hash does not match sealed proof")
                prover_thread_id = candidate["thread_id"]
                if not isinstance(prover_thread_id, str):
                    raise ConflictError(
                        "verification candidate is missing its prover thread identity"
                    )
                prover_thread = self._require_thread_row(connection, prover_thread_id)
                prover_external_thread_id = prover_thread["external_thread_id"]
                if (
                    ThreadRole(prover_thread["role"]) is not ThreadRole.PROVER
                    or not isinstance(prover_external_thread_id, str)
                    or not prover_external_thread_id
                ):
                    raise ConflictError(
                        "verification candidate is missing its prover external thread identity"
                    )
                thread = self._require_thread_row(connection, report.verifier_thread_id)
                if thread["run_id"] != run_id:
                    raise ConflictError("verification thread belongs to another run")
                if (
                    ThreadRole(thread["role"]) is not ThreadRole.VERIFIER
                    or thread["parent_thread_id"] is not None
                    or thread["external_thread_id"] is None
                ):
                    raise ConflictError("verification requires a fresh verifier thread")
                if report.provenance.source_id != report.verifier_thread_id:
                    raise ConflictError("verification provenance must bind its verifier thread")
                external_thread_id = str(thread["external_thread_id"])
                if report.verifier_external_thread_id is None:
                    raise ConflictError(
                        "verification report requires an external thread identity"
                    )
                if report.verifier_external_thread_id != external_thread_id:
                    raise ConflictError(
                        "verification external thread id does not match the stored thread"
                    )
                if external_thread_id == prover_external_thread_id:
                    raise ConflictError(
                        "verification external thread identity reuses the candidate prover"
                    )
                required_kinds = {"structural", "detailed", "citation"}
                if report.kind in required_kinds:
                    prior_rows = connection.execute(
                        """
                        SELECT report_json
                        FROM verifications
                        WHERE run_id = ? AND candidate_id = ?
                        """,
                        (run_id, report.candidate_id),
                    ).fetchall()
                    for prior_row in prior_rows:
                        prior = VerificationReport.model_validate_json(
                            prior_row["report_json"]
                        )
                        if (
                            prior.kind in required_kinds
                            and prior.verifier_external_thread_id
                            == report.verifier_external_thread_id
                        ):
                            raise ConflictError(
                                "required verification reports must use distinct "
                                "external thread identities"
                            )
                input_row = connection.execute(
                    "SELECT input_json FROM run_inputs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if input_row is None:
                    raise ConflictError(
                        "verification requires the frozen run input"
                    )
                run_input = RunInput.model_validate_json(input_row["input_json"])
                known_rule_ids = {
                    rule.id for rule in run_input.frozen_verification_rules
                }
                referenced_rule_ids = {
                    rule_id
                    for check in report.checks
                    for rule_id in check.rule_ids
                }
                unknown_rule_ids = referenced_rule_ids - known_rule_ids
                if unknown_rule_ids:
                    names = ", ".join(sorted(unknown_rule_ids))
                    raise ConflictError(
                        f"verification references unknown verification rule: {names}"
                    )
                evidence_ids = {
                    str(evidence_row["id"])
                    for evidence_row in connection.execute(
                        "SELECT id FROM evidence WHERE run_id = ?",
                        (run_id,),
                    ).fetchall()
                }
                citation_support = tuple(
                    support
                    for check in report.checks
                    for support in check.citation_support
                )
                if report.kind != "citation" and citation_support:
                    raise ConflictError(
                        "structured citation support is allowed only in citation reports"
                    )
                referenced_evidence = {
                    evidence_id
                    for check in report.checks
                    for evidence_id in check.evidence_ids
                } | {
                    evidence_id
                    for finding in report.findings
                    for evidence_id in finding.evidence_ids
                } | {
                    support.evidence_id for support in citation_support
                }
                unknown_evidence = referenced_evidence - evidence_ids
                if unknown_evidence:
                    names = ", ".join(sorted(unknown_evidence))
                    raise ConflictError(
                        f"verification references unknown evidence: {names}"
                    )
                if report.kind == "citation":
                    supported_evidence = {
                        support.evidence_id for support in citation_support
                    }
                    missing_evidence = evidence_ids - supported_evidence
                    if missing_evidence:
                        names = ", ".join(sorted(missing_evidence))
                        raise ConflictError(
                            "citation report does not cover the frozen evidence ledger: "
                            f"{names}"
                        )
                    frozen_candidate = ProofCandidate.model_validate_json(
                        candidate["candidate_json"]
                    )
                    evidence_by_id = {
                        str(evidence_row["id"]): self._evidence_from_row(evidence_row)
                        for evidence_row in connection.execute(
                            "SELECT * FROM evidence WHERE run_id = ?",
                            (run_id,),
                        ).fetchall()
                    }
                    for support in citation_support:
                        frozen_evidence = evidence_by_id[support.evidence_id]
                        if support.proof_span not in frozen_candidate.proof:
                            raise ConflictError(
                                "citation support proof span is absent from the "
                                "frozen candidate"
                            )
                        if support.evidence_excerpt not in frozen_evidence.content:
                            raise ConflictError(
                                "citation support evidence excerpt is absent from "
                                "the frozen evidence"
                            )
                        expected_locator = (
                            frozen_evidence.source_uri
                            or f"evidence:{frozen_evidence.id}"
                        )
                        if support.source_locator != expected_locator:
                            raise ConflictError(
                                "citation support source locator does not match "
                                "the frozen evidence identity"
                            )
                report_json = canonical_json(report)
                provenance_json = canonical_json(report.provenance)
                connection.execute(
                    """
                    INSERT INTO verifications (
                        id, run_id, candidate_id, thread_id, kind, schema_version,
                        report_json, report_sha256, candidate_sha256, provenance_json,
                        provenance_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report.id,
                        run_id,
                        report.candidate_id,
                        report.verifier_thread_id,
                        report.kind,
                        report.schema_version,
                        report_json,
                        verification_report_sha256(report),
                        report.candidate_sha256,
                        provenance_json,
                        canonical_sha256(report.provenance),
                        _render_time(now),
                    ),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="verification.created",
                    stage=RunStage(run["stage"]),
                    payload={
                        "verification_id": report.id,
                        "candidate_id": report.candidate_id,
                        "verdict": report.verdict.value,
                    },
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"verification already exists: {report.id}") from error
        return self.get_verification(report.id)

    def update_verification(
        self,
        report: VerificationReport,
        *,
        execution: ExecutionToken | None = None,
    ) -> VerificationRecord:
        try:
            with self._transaction() as connection:
                row = self._require_verification_row(connection, report.id)
                self._authorize_execution(connection, row["run_id"], execution)
                connection.execute(
                    """
                    UPDATE verifications
                    SET report_json = ?, report_sha256 = ?, candidate_sha256 = ?
                    WHERE id = ?
                    """,
                    (
                        canonical_json(report),
                        verification_report_sha256(report),
                        report.candidate_sha256,
                        report.id,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "verification is immutable" in str(error):
                raise ImmutableRecordError("verification is immutable") from error
            raise ConflictError(f"verification update failed: {report.id}") from error
        return self.get_verification(report.id)

    def delete_verification(
        self,
        verification_id: str,
        *,
        execution: ExecutionToken | None = None,
    ) -> None:
        try:
            with self._transaction() as connection:
                row = self._require_verification_row(connection, verification_id)
                self._authorize_execution(connection, row["run_id"], execution)
                connection.execute(
                    "DELETE FROM verifications WHERE id = ?", (verification_id,)
                )
        except sqlite3.IntegrityError as error:
            if "verification is immutable" in str(error):
                raise ImmutableRecordError("verification is immutable") from error
            raise ConflictError(f"verification deletion failed: {verification_id}") from error

    def get_verification(self, verification_id: str) -> VerificationRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM verifications WHERE id = ?", (verification_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"verification not found: {verification_id}")
            return self._verification_from_row(row)

    def list_verifications(self, run_id: str) -> tuple[VerificationRecord, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                "SELECT * FROM verifications WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            return tuple(self._verification_from_row(row) for row in rows)

    def add_artifact(
        self,
        artifact_id: str,
        *,
        run_id: str,
        kind: str,
        media_type: str,
        sha256: Sha256,
        size_bytes: int,
        provenance: Provenance,
        relative_path: str | None = None,
        execution: ExecutionToken | None = None,
    ) -> ArtifactRecord:
        _validate_id(artifact_id, field="artifact_id")
        _validate_id(run_id, field="run_id")
        _validate_nonempty(kind, field="kind")
        _validate_nonempty(media_type, field="media_type")
        _validate_sha256(sha256, field="sha256")
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")
        if relative_path is not None:
            validate_relative_artifact_path(relative_path)
        now = self._now()
        provenance_json = canonical_json(provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
                self._authorize_execution(connection, run_id, execution)
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        id, run_id, kind, relative_path, media_type, sha256, size_bytes,
                        schema_version, provenance_json, provenance_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        run_id,
                        kind,
                        relative_path,
                        media_type,
                        sha256,
                        size_bytes,
                        SCHEMA_VERSION,
                        provenance_json,
                        canonical_sha256(provenance),
                        _render_time(now),
                    ),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="artifact.created",
                    stage=RunStage(run["stage"]),
                    payload={"artifact_id": artifact_id, "kind": kind, "sha256": sha256},
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"artifact already exists or is invalid: {artifact_id}") from error
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"artifact not found: {artifact_id}")
            return self._artifact_from_row(row)

    def list_artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            return tuple(self._artifact_from_row(row) for row in rows)

    def snapshot(self, run_id: str) -> RunSnapshot:
        with self._read_transaction() as connection:
            run_row = self._require_run_row(connection, run_id)
            run = self._run_from_row(run_row)
            input_row = connection.execute(
                "SELECT input_json, input_sha256 FROM run_inputs WHERE run_id = ?", (run_id,)
            ).fetchone()
            run_input = None
            if input_row is not None:
                run_input = RunInput.model_validate_json(input_row["input_json"])
                if run_input.sha256 != input_row["input_sha256"]:
                    raise StoreIntegrityError(f"run input hash mismatch: {run_id}")
            event_rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
            output_rows = connection.execute(
                "SELECT * FROM stage_outputs WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            turn_input_rows = connection.execute(
                "SELECT * FROM turn_inputs WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            thread_rows = connection.execute(
                "SELECT * FROM threads WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            candidate_rows = connection.execute(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY attempt, created_at, id",
                (run_id,),
            ).fetchall()
            verification_rows = connection.execute(
                "SELECT * FROM verifications WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            artifact_rows = connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            execution_rows = connection.execute(
                """
                SELECT * FROM execution_segments
                WHERE run_id = ? ORDER BY version
                """,
                (run_id,),
            ).fetchall()
            resolution_rows = connection.execute(
                """
                SELECT * FROM runtime_resolutions
                WHERE run_id = ? ORDER BY created_at, segment_id
                """,
                (run_id,),
            ).fetchall()
            evidence_rows = connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            observation_rows = connection.execute(
                """
                SELECT * FROM web_search_observations
                WHERE run_id = ? ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
            plan_rows = self._plan_rows_in_event_order(connection, run_id)
            adjudication_rows = self._adjudication_rows_in_event_order(
                connection,
                run_id,
            )
            decision_rows = connection.execute(
                """
                SELECT * FROM candidate_decisions
                WHERE run_id = ? ORDER BY created_at, candidate_id
                """,
                (run_id,),
            ).fetchall()
            events = tuple(self._event_from_row(row) for row in event_rows)
            _require_integrity(
                tuple(event.seq for event in events)
                == tuple(range(1, len(events) + 1)),
                f"event sequence is not contiguous: {run_id}",
            )
            return RunSnapshot(
                run=run,
                run_input=run_input,
                events=events,
                stage_outputs=tuple(
                    self._stage_output_from_row(row) for row in output_rows
                ),
                turn_inputs=tuple(
                    self._turn_input_from_row(row) for row in turn_input_rows
                ),
                threads=tuple(self._thread_from_row(row) for row in thread_rows),
                candidates=tuple(self._candidate_from_row(row) for row in candidate_rows),
                verifications=tuple(
                    self._verification_from_row(row) for row in verification_rows
                ),
                artifacts=tuple(self._artifact_from_row(row) for row in artifact_rows),
                execution_segments=tuple(
                    self._execution_from_row(row) for row in execution_rows
                ),
                runtime_resolutions=tuple(
                    self._runtime_resolution_from_row(row)
                    for row in resolution_rows
                ),
                evidence=tuple(self._evidence_from_row(row) for row in evidence_rows),
                web_search_observations=tuple(
                    self._web_search_observation_from_row(row)
                    for row in observation_rows
                ),
                plans=tuple(self._plan_from_row(row) for row in plan_rows),
                adjudications=tuple(
                    self._adjudication_from_row(row) for row in adjudication_rows
                ),
                decisions=tuple(self._decision_from_row(row) for row in decision_rows),
            )

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        stage: RunStage,
        payload: dict[str, JsonValue],
        execution: ExecutionToken | None = None,
    ) -> Event:
        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            if event_type in RUNTIME_DRAIN_EVENTS:
                self._authorize_runtime_lifecycle(connection, run_id, execution)
            else:
                self._authorize_execution(connection, run_id, execution)
            if RunStage(run["stage"]) is not stage:
                raise InvalidTransitionError(
                    "event stage must equal the run's current stage"
                )
            return self._append_event(
                connection,
                run_id,
                event_type=event_type,
                stage=stage,
                payload=payload,
                created_at=self._now(),
            )

    def record_turn_terminal_unconfirmed(
        self,
        run_id: str,
        *,
        payload: dict[str, JsonValue],
        execution: ExecutionToken,
    ) -> Event:
        """Record the one audit event a fenced worker may append while cancelling."""

        now = self._now()
        with self._transaction() as connection:
            self._authorize_runtime_lifecycle(connection, run_id, execution)
            run = self._require_run_row(connection, run_id)
            return self._append_event(
                connection,
                run_id,
                event_type="runtime.turn_terminal_unconfirmed",
                stage=RunStage(run["stage"]),
                payload=payload,
                created_at=now,
            )

    def record_turn_start_unconfirmed(
        self,
        run_id: str,
        *,
        payload: dict[str, JsonValue],
        execution: ExecutionToken,
    ) -> Event:
        """Persist an ambiguous turn-start request after its runtime call began."""

        now = self._now()
        with self._transaction() as connection:
            self._authorize_runtime_lifecycle(connection, run_id, execution)
            run = self._require_run_row(connection, run_id)
            return self._append_event(
                connection,
                run_id,
                event_type="runtime.turn_start_unconfirmed",
                stage=RunStage(run["stage"]),
                payload=payload,
                created_at=now,
            )

    def record_turn_completed(
        self,
        run_id: str,
        *,
        payload: dict[str, JsonValue],
        execution: ExecutionToken,
    ) -> Event:
        """Persist runtime terminal evidence even after cancellation was requested."""

        now = self._now()
        with self._transaction() as connection:
            try:
                self._authorize_runtime_lifecycle(connection, run_id, execution)
            except ConflictError:
                self._authorize_abandoned_late_terminal(
                    connection,
                    run_id,
                    payload,
                    execution,
                )
            run = self._require_run_row(connection, run_id)
            return self._append_event(
                connection,
                run_id,
                event_type="runtime.turn_completed",
                stage=RunStage(run["stage"]),
                payload=payload,
                created_at=now,
            )

    def _authorize_abandoned_late_terminal(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        payload: dict[str, JsonValue],
        execution: ExecutionToken,
    ) -> None:
        """Allow one authentic terminal observation after operator abandonment.

        The old segment may append audit evidence after its lease expires, but it
        cannot change durable lifecycle state, release a lease, or write any
        other event. This preserves reconciliation evidence without reopening a
        failed run.
        """

        run = self._require_run_row(connection, run_id)
        row = self._require_execution_row(connection, execution.segment_id)
        self._validate_execution_identity(row, execution)
        if (
            row["run_id"] != run_id
            or int(run["execution_version"]) != execution.version
            or row["released_at"] is not None
            or RunStatus(run["status"]) is not RunStatus.FAILED
        ):
            raise ConflictError("stale execution token")
        abandoned = connection.execute(
            "SELECT 1 FROM events WHERE run_id = ? "
            "AND event_type = 'operator.run_abandoned' LIMIT 1",
            (run_id,),
        ).fetchone()
        if abandoned is None:
            raise ConflictError("stale execution token")
        thread_id = payload.get("thread_id")
        turn_id = payload.get("turn_id")
        backend = payload.get("backend")
        if not all(isinstance(item, str) and item for item in (thread_id, turn_id, backend)):
            raise ConflictError("late terminal identity is incomplete")
        pending = self._unconfirmed_runtime_turns(connection, run_id)
        if ("turn", backend, thread_id, turn_id) not in pending:
            raise ConflictError("late terminal is not bound to an unconfirmed turn")

    def list_events(self, run_id: str, *, after_seq: int = 0) -> tuple[Event, ...]:
        with self._lock:
            self._require_run_row(self._connection, run_id)
            rows = self._connection.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq
                """,
                (run_id, after_seq),
            ).fetchall()
            return tuple(self._event_from_row(row) for row in rows)

    def has_unconfirmed_runtime_turns(self, run_id: str) -> bool:
        """Return whether a start attempt lacks a confirmed runtime terminal."""

        with self._lock:
            self._require_run_row(self._connection, run_id)
            return bool(self._unconfirmed_runtime_turns(self._connection, run_id))

    def pending_runtime_identities(
        self,
        run_id: str,
    ) -> tuple[tuple[str, ...], ...]:
        """Expose exact unresolved attempt/turn identities for read-only diagnosis."""

        with self._lock:
            self._require_run_row(self._connection, run_id)
            return tuple(
                sorted(self._unconfirmed_runtime_turns(self._connection, run_id))
            )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        event_type: str,
        stage: RunStage,
        payload: dict[str, JsonValue],
        created_at: datetime,
    ) -> Event:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        seq = int(row["next_seq"])
        payload_json = canonical_json(payload)
        payload_sha256 = canonical_sha256(payload)
        connection.execute(
            """
            INSERT INTO events (
                run_id, seq, schema_version, event_type, stage, payload_json,
                payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                SCHEMA_VERSION,
                event_type,
                stage.value,
                payload_json,
                payload_sha256,
                _render_time(created_at),
            ),
        )
        return Event(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            stage=stage.value,
            payload=json.loads(payload_json),
            payload_sha256=payload_sha256,
            created_at=created_at,
        )

    def _require_run_row(
        self, connection: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone(),
        )
        if row is None:
            raise NotFoundError(f"run not found: {run_id}")
        return row

    @staticmethod
    def _require_thread_row(
        connection: sqlite3.Connection, thread_id: str
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone(),
        )
        if row is None:
            raise NotFoundError(f"thread not found: {thread_id}")
        return row

    @staticmethod
    def _require_candidate_row(
        connection: sqlite3.Connection, candidate_id: str
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone(),
        )
        if row is None:
            raise NotFoundError(f"candidate not found: {candidate_id}")
        return row

    @staticmethod
    def _require_verification_row(
        connection: sqlite3.Connection, verification_id: str
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM verifications WHERE id = ?", (verification_id,)
            ).fetchone(),
        )
        if row is None:
            raise NotFoundError(f"verification not found: {verification_id}")
        return row

    @staticmethod
    def _require_evidence_ids(
        connection: sqlite3.Connection,
        run_id: str,
        evidence_ids: set[str],
    ) -> None:
        for evidence_id in sorted(evidence_ids):
            row = connection.execute(
                "SELECT run_id FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            if row is None or row["run_id"] != run_id:
                raise ConflictError(
                    f"unknown evidence id for run {run_id}: {evidence_id}"
                )

    @staticmethod
    def _require_evidence_observations(
        connection: sqlite3.Connection,
        run_id: str,
        evidence: Evidence,
    ) -> None:
        for observation_id in evidence.observation_ids:
            row = connection.execute(
                "SELECT * FROM web_search_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
            if row is None:
                raise ConflictError(
                    f"unknown web-search observation: {observation_id}"
                )
            observation = RunStore._web_search_observation_from_row(row)
            if (
                observation.run_id != run_id
                or observation.local_thread_id
                != evidence.provenance.source_id
                or observation.uri != evidence.source_uri
            ):
                raise ConflictError(
                    "runtime_observed evidence does not match its web-search "
                    f"observation: {observation_id}"
                )

    @staticmethod
    def _require_execution_row(
        connection: sqlite3.Connection, segment_id: str
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM execution_segments WHERE id = ?", (segment_id,)
            ).fetchone(),
        )
        if row is None:
            raise NotFoundError(f"execution segment not found: {segment_id}")
        return row

    @staticmethod
    def _validate_execution_identity(
        row: sqlite3.Row, execution: ExecutionToken
    ) -> None:
        if (
            int(row["version"]) != execution.version
            or not hmac.compare_digest(
                row["lease_token_sha256"], sha256_text(execution.lease_token)
            )
        ):
            raise ConflictError("stale execution token")

    def _validate_execution_token(
        self,
        connection: sqlite3.Connection,
        execution: ExecutionToken,
        *,
        now: datetime | None = None,
    ) -> sqlite3.Row:
        row = self._require_execution_row(connection, execution.segment_id)
        self._validate_execution_identity(row, execution)
        run = self._require_run_row(connection, row["run_id"])
        current_time = now or self._now()
        if (
            int(run["execution_version"]) != execution.version
            or row["released_at"] is not None
            or _parse_time(row["lease_expires_at"]) <= current_time
        ):
            raise ConflictError("stale execution token")
        return row

    def _authorize_execution(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        execution: ExecutionToken | None,
    ) -> None:
        run = self._require_run_row(connection, run_id)
        if int(run["execution_version"]) == 0:
            if execution is not None:
                raise ConflictError("stale execution token")
            return
        if RunStatus(run["status"]) is not RunStatus.RUNNING:
            raise ConflictError(
                f"run is not writable while {RunStatus(run['status']).value}"
            )
        if execution is None:
            raise ConflictError("an active execution token is required")
        row = self._validate_execution_token(connection, execution)
        if row["run_id"] != run_id:
            raise ConflictError("stale execution token")

    def _authorize_runtime_lifecycle(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        execution: ExecutionToken | None,
    ) -> sqlite3.Row:
        """Authorize only lifecycle drain writes from the current fenced owner."""

        run = self._require_run_row(connection, run_id)
        if execution is None:
            raise ConflictError("an active execution token is required")
        row = self._require_execution_row(connection, execution.segment_id)
        self._validate_execution_identity(row, execution)
        if (
            row["run_id"] != run_id
            or int(run["execution_version"]) != execution.version
            or row["released_at"] is not None
            or _parse_time(row["lease_expires_at"]) <= self._now()
        ):
            raise ConflictError("stale execution token")
        return row

    @staticmethod
    def _unconfirmed_runtime_turns(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> set[tuple[str, ...]]:
        pending: set[tuple[str, ...]] = set()
        rows = connection.execute(
            """
            SELECT event_type, payload_json FROM events
            WHERE run_id = ? AND event_type IN (
                'runtime.turn_attempt_started',
                'runtime.turn_not_started',
                'runtime.turn_start_unconfirmed',
                'runtime.turn_started',
                'runtime.turn_terminal_unconfirmed',
                'runtime.turn_completed'
            )
            ORDER BY seq
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                raise StoreIntegrityError("runtime ownership event has invalid payload")
            event_type = row["event_type"]
            if event_type in {
                "runtime.turn_attempt_started",
                "runtime.turn_not_started",
                "runtime.turn_start_unconfirmed",
            }:
                turn_input_id = payload.get("turn_input_id")
                attempt = payload.get("attempt")
                if (
                    not isinstance(turn_input_id, str)
                    or not turn_input_id
                    or isinstance(attempt, bool)
                    or not isinstance(attempt, int)
                    or attempt < 1
                ):
                    raise StoreIntegrityError(
                        "runtime turn attempt event has invalid identity"
                    )
                attempt_identity = ("attempt", turn_input_id, str(attempt))
                if event_type == "runtime.turn_not_started":
                    pending.discard(attempt_identity)
                else:
                    pending.add(attempt_identity)
                continue

            thread_id = payload.get("thread_id")
            turn_id = payload.get("turn_id")
            backend = payload.get("backend")
            if (
                not isinstance(thread_id, str)
                or not thread_id
                or not isinstance(turn_id, str)
                or not turn_id
                or not isinstance(backend, str)
                or not backend
            ):
                raise StoreIntegrityError("runtime turn event has invalid identity")
            identity = ("turn", backend, thread_id, turn_id)
            if event_type == "runtime.turn_started":
                turn_input_id = payload.get("turn_input_id")
                attempt = payload.get("attempt")
                if not isinstance(turn_input_id, str) or not turn_input_id:
                    raise StoreIntegrityError(
                        "runtime turn start event has invalid attempt identity"
                    )
                if attempt is None:
                    matching_attempts = {
                        item
                        for item in pending
                        if len(item) == 3
                        and item[0] == "attempt"
                        and item[1] == turn_input_id
                    }
                    if len(matching_attempts) > 1:
                        raise StoreIntegrityError(
                            "runtime turn start matches multiple pending attempts"
                        )
                    pending.difference_update(matching_attempts)
                elif (
                    isinstance(attempt, bool)
                    or not isinstance(attempt, int)
                    or attempt < 1
                ):
                    raise StoreIntegrityError(
                        "runtime turn start event has invalid attempt identity"
                    )
                else:
                    pending.discard(("attempt", turn_input_id, str(attempt)))
                pending.add(identity)
            elif event_type == "runtime.turn_terminal_unconfirmed":
                pending.add(identity)
            elif payload.get("status") in {"completed", "failed", "interrupted"}:
                pending.discard(identity)
        return pending

    @staticmethod
    def _latest_stage_entry_seq(
        connection: sqlite3.Connection,
        run_id: str,
        stage: RunStage,
    ) -> int:
        row = connection.execute(
            """
            SELECT seq FROM events
            WHERE run_id = ? AND event_type = 'run.stage_changed' AND stage = ?
            ORDER BY seq DESC LIMIT 1
            """,
            (run_id, stage.value),
        ).fetchone()
        if row is None:
            raise StoreIntegrityError(
                f"run {run_id} has no entry event for stage {stage.value}"
            )
        return int(row["seq"])

    @staticmethod
    def _event_record_ids_after(
        connection: sqlite3.Connection,
        run_id: str,
        *,
        after_seq: int,
        stage: RunStage,
        event_type: str,
        payload_key: str,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT payload_json FROM events
            WHERE run_id = ? AND seq > ? AND stage = ? AND event_type = ?
            ORDER BY seq
            """,
            (run_id, after_seq, stage.value, event_type),
        ).fetchall()
        record_ids: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            record_id = payload.get(payload_key)
            if not isinstance(record_id, str):
                raise StoreIntegrityError(
                    f"{event_type} event has invalid {payload_key}"
                )
            record_ids.append(record_id)
        return tuple(record_ids)

    def _adjudication_rows_in_event_order(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> tuple[sqlite3.Row, ...]:
        adjudication_ids = self._event_record_ids_after(
            connection,
            run_id,
            after_seq=after_seq,
            stage=RunStage.ADJUDICATION,
            event_type="adjudication.created",
            payload_key="adjudication_id",
        )
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        for adjudication_id in adjudication_ids:
            if adjudication_id in seen:
                raise StoreIntegrityError(
                    f"duplicate adjudication.created event: {adjudication_id}"
                )
            seen.add(adjudication_id)
            row = connection.execute(
                "SELECT * FROM adjudications WHERE id = ? AND run_id = ?",
                (adjudication_id, run_id),
            ).fetchone()
            if row is None:
                raise StoreIntegrityError(
                    f"adjudication event references a missing row: {adjudication_id}"
                )
            rows.append(row)
        if after_seq == 0:
            count_row = connection.execute(
                "SELECT COUNT(*) AS count FROM adjudications WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert count_row is not None
            if int(count_row["count"]) != len(rows):
                raise StoreIntegrityError(
                    f"adjudication row is missing its creation event: {run_id}"
                )
        return tuple(rows)

    def _plan_rows_in_event_order(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> tuple[sqlite3.Row, ...]:
        plan_ids = self._event_record_ids_after(
            connection,
            run_id,
            after_seq=0,
            stage=RunStage.PLANNING,
            event_type="plan.created",
            payload_key="plan_id",
        )
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        for plan_id in plan_ids:
            if plan_id in seen:
                raise StoreIntegrityError(f"duplicate plan.created event: {plan_id}")
            seen.add(plan_id)
            row = connection.execute(
                "SELECT * FROM plans WHERE id = ? AND run_id = ?",
                (plan_id, run_id),
            ).fetchone()
            if row is None:
                raise StoreIntegrityError(
                    f"plan event references a missing row: {plan_id}"
                )
            rows.append(row)
        count_row = connection.execute(
            "SELECT COUNT(*) AS count FROM plans WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert count_row is not None
        if int(count_row["count"]) != len(rows):
            raise StoreIntegrityError(f"plan row is missing its creation event: {run_id}")
        return tuple(rows)

    def _latest_adjudication_row(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        after_seq: int,
    ) -> sqlite3.Row | None:
        rows = self._adjudication_rows_in_event_order(
            connection,
            run_id,
            after_seq=after_seq,
        )
        return rows[-1] if rows else None

    def _validate_stage_gate(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        *,
        current: RunStage,
        target: RunStage,
    ) -> None:
        run_id = str(run["id"])
        if current is RunStage.INTAKE:
            persisted_input = connection.execute(
                "SELECT 1 FROM run_inputs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if persisted_input is None:
                raise InvalidTransitionError(
                    "intake requires a content-addressed typed run input"
                )
            return

        if current is RunStage.LITERATURE and target is RunStage.PLANNING:
            entry_seq = self._latest_stage_entry_seq(connection, run_id, current)
            evidence_ids = self._event_record_ids_after(
                connection,
                run_id,
                after_seq=entry_seq,
                stage=current,
                event_type="evidence.created",
                payload_key="evidence_id",
            )
            if not any(
                connection.execute(
                    "SELECT 1 FROM evidence WHERE run_id = ? AND id = ?",
                    (run_id, evidence_id),
                ).fetchone()
                is not None
                for evidence_id in evidence_ids
            ):
                raise InvalidTransitionError("planning requires typed evidence")
            return

        if current is RunStage.PLANNING and target is RunStage.PROVING:
            entry_seq = self._latest_stage_entry_seq(connection, run_id, current)
            plan_ids = self._event_record_ids_after(
                connection,
                run_id,
                after_seq=entry_seq,
                stage=current,
                event_type="plan.created",
                payload_key="plan_id",
            )
            if not any(
                connection.execute(
                    "SELECT 1 FROM plans WHERE run_id = ? AND id = ?",
                    (run_id, plan_id),
                ).fetchone()
                is not None
                for plan_id in plan_ids
            ):
                raise InvalidTransitionError("proving requires a typed plan")
            return

        if current is RunStage.PROVING and target is RunStage.VERIFICATION:
            entry_seq = self._latest_stage_entry_seq(connection, run_id, current)
            candidate_ids = self._event_record_ids_after(
                connection,
                run_id,
                after_seq=entry_seq,
                stage=current,
                event_type="candidate.sealed",
                payload_key="candidate_id",
            )
            if not any(
                connection.execute(
                    """
                    SELECT 1 FROM candidates
                    WHERE run_id = ? AND id = ? AND sealed_at IS NOT NULL
                    """,
                    (run_id, candidate_id),
                ).fetchone()
                is not None
                for candidate_id in candidate_ids
            ):
                raise InvalidTransitionError("verification requires a sealed proof candidate")
            return

        if current is RunStage.VERIFICATION and target is RunStage.ADJUDICATION:
            entry_seq = self._latest_stage_entry_seq(connection, run_id, current)
            if not self._has_independently_verified_candidate(
                connection,
                run_id,
                after_seq=entry_seq,
            ):
                raise InvalidTransitionError(
                    "adjudication requires all stable verifier reports from independent "
                    "threads"
                )
            return

        if current is RunStage.ADJUDICATION:
            entry_seq = self._latest_stage_entry_seq(connection, run_id, current)
            latest_row = self._latest_adjudication_row(
                connection,
                run_id,
                after_seq=entry_seq,
            )
            if latest_row is None:
                raise InvalidTransitionError("adjudication requires a typed adjudication")
            adjudication = self._adjudication_from_row(latest_row)
            config = QEDConfig.model_validate_json(run["config_json"])
            if target is RunStage.EXPORT:
                decision_row = connection.execute(
                    """
                    SELECT * FROM candidate_decisions
                    WHERE run_id = ? AND candidate_id = ?
                    """,
                    (run_id, adjudication.candidate_id),
                ).fetchone()
                if (
                    adjudication.outcome != "accept"
                    or decision_row is None
                    or self._decision_from_row(decision_row).schema_version != 3
                    or not self._decision_from_row(decision_row).passed
                ):
                    raise InvalidTransitionError(
                        "export requires an accepting adjudication and code-passed decision"
                    )
                return
            expected_outcome = {
                RunStage.LITERATURE: "rewrite",
                RunStage.PLANNING: "revise_plan",
                RunStage.PROVING: "revise_proof",
            }[target]
            if adjudication.outcome != expected_outcome:
                raise InvalidTransitionError(
                    f"{target.value} requires adjudication outcome {expected_outcome}"
                )
            if (
                target is RunStage.PLANNING
                and int(run["plan_revision_count"]) >= config.budgets.plan_revisions
            ):
                raise InvalidTransitionError("plan revision budget exhausted")
            if (
                target is RunStage.LITERATURE
                and int(run["strategy_rewrite_count"])
                >= config.budgets.strategy_rewrites
            ):
                raise InvalidTransitionError("strategy rewrite budget exhausted")
            return

        if current is RunStage.EXPORT and target is RunStage.COMPLETE:
            kinds = {
                row["kind"]
                for row in connection.execute(
                    "SELECT kind FROM artifacts WHERE run_id = ?", (run_id,)
                ).fetchall()
            }
            if not {"proof", "report", "manifest"}.issubset(kinds):
                raise InvalidTransitionError(
                    "completion requires proof, report, manifest artifacts"
                )

    def _validate_completion_gate(
        self, connection: sqlite3.Connection, run_id: str
    ) -> None:
        decision_rows = connection.execute(
            "SELECT * FROM candidate_decisions WHERE run_id = ?", (run_id,)
        ).fetchall()
        if not any(
            self._decision_from_row(row).schema_version == 3
            and self._decision_from_row(row).passed
            for row in decision_rows
        ):
            raise InvalidTransitionError("completed run requires a code-passed decision")
        kinds = {
            row["kind"]
            for row in connection.execute(
                "SELECT kind FROM artifacts WHERE run_id = ?", (run_id,)
            ).fetchall()
        }
        if not {"proof", "report", "manifest"}.issubset(kinds):
            raise InvalidTransitionError(
                "completed run requires proof, report, manifest artifacts"
            )

    def _has_independently_verified_candidate(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        after_seq: int,
    ) -> bool:
        verification_ids = self._event_record_ids_after(
            connection,
            run_id,
            after_seq=after_seq,
            stage=RunStage.VERIFICATION,
            event_type="verification.created",
            payload_key="verification_id",
        )
        report_rows = tuple(
            self._require_verification_row(connection, verification_id)
            for verification_id in verification_ids
        )
        proving_entry_seq = self._latest_stage_entry_seq(
            connection,
            run_id,
            RunStage.PROVING,
        )
        current_candidate_ids = set(
            self._event_record_ids_after(
                connection,
                run_id,
                after_seq=proving_entry_seq,
                stage=RunStage.PROVING,
                event_type="candidate.sealed",
                payload_key="candidate_id",
            )
        )
        candidate_ids = {
            str(row["candidate_id"])
            for row in report_rows
            if row["run_id"] == run_id
            and str(row["candidate_id"]) in current_candidate_ids
        }
        citation_required = connection.execute(
            "SELECT 1 FROM evidence WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone() is not None
        required_kinds = set(STABLE_REQUIRED_REPORT_KINDS)
        if citation_required:
            required_kinds.add("citation")
        for candidate_id in candidate_ids:
            candidate_row = self._require_candidate_row(connection, candidate_id)
            if candidate_row["run_id"] != run_id or candidate_row["sealed_at"] is None:
                continue
            prover_thread_id = candidate_row["thread_id"]
            if not isinstance(prover_thread_id, str):
                continue
            prover_thread = self._require_thread_row(connection, prover_thread_id)
            prover_external_thread_id = prover_thread["external_thread_id"]
            if (
                ThreadRole(prover_thread["role"]) is not ThreadRole.PROVER
                or not isinstance(prover_external_thread_id, str)
                or not prover_external_thread_id
            ):
                continue
            reports = tuple(
                self._verification_from_row(row).report
                for row in report_rows
                if row["candidate_id"] == candidate_id
            )
            kinds = {report.kind for report in reports}
            external_ids = {
                report.verifier_external_thread_id
                for report in reports
                if report.kind in required_kinds
                and report.verifier_external_thread_id is not None
            }
            if (
                required_kinds.issubset(kinds)
                and len(external_ids) >= len(required_kinds)
                and prover_external_thread_id not in external_ids
            ):
                return True
        return False

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        config = QEDConfig.model_validate_json(row["config_json"])
        provenance = Provenance.model_validate_json(row["provenance_json"])
        _require_integrity(
            canonical_sha256(config) == row["config_sha256"],
            f"run config hash mismatch: {row['id']}",
        )
        _require_integrity(
            canonical_sha256(provenance) == row["provenance_sha256"],
            f"run provenance hash mismatch: {row['id']}",
        )
        _require_integrity(
            provenance.runtime_version == row["runtime_version"],
            f"run runtime version mismatch: {row['id']}",
        )
        _require_integrity(
            _SHA256_PATTERN.fullmatch(row["input_sha256"]) is not None,
            f"run input hash is invalid: {row['id']}",
        )
        return RunRecord(
            id=row["id"],
            schema_version=row["schema_version"],
            status=RunStatus(row["status"]),
            stage=RunStage(row["stage"]),
            config=config,
            config_sha256=row["config_sha256"],
            input_sha256=row["input_sha256"],
            provenance=provenance,
            provenance_sha256=row["provenance_sha256"],
            runtime_version=row["runtime_version"],
            cancellation_requested=bool(row["cancellation_requested"]),
            resumable=bool(row["resumable"]),
            resume_count=row["resume_count"],
            execution_version=row["execution_version"],
            proof_attempt_count=row["proof_attempt_count"],
            plan_revision_count=row["plan_revision_count"],
            strategy_rewrite_count=row["strategy_rewrite_count"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            run_id=row["run_id"],
            seq=row["seq"],
            event_type=row["event_type"],
            stage=row["stage"],
            payload=json.loads(row["payload_json"]),
            payload_sha256=row["payload_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _thread_from_row(row: sqlite3.Row) -> ThreadRecord:
        provenance = Provenance.model_validate_json(row["provenance_json"])
        _require_integrity(
            canonical_sha256(provenance) == row["provenance_sha256"],
            f"thread provenance hash mismatch: {row['id']}",
        )
        return ThreadRecord(
            id=row["id"],
            run_id=row["run_id"],
            role=ThreadRole(row["role"]),
            parent_thread_id=row["parent_thread_id"],
            external_thread_id=row["external_thread_id"],
            model=row["model"],
            status=ThreadStatus(row["status"]),
            schema_version=row["schema_version"],
            provenance=provenance,
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> CandidateRecord:
        candidate = ProofCandidate.model_validate_json(row["candidate_json"])
        provenance = Provenance.model_validate_json(row["provenance_json"])
        _require_integrity(
            canonical_sha256(candidate) == row["candidate_sha256"],
            f"candidate hash mismatch: {row['id']}",
        )
        _require_integrity(
            candidate.proof_sha256 == row["proof_sha256"],
            f"candidate proof hash mismatch: {row['id']}",
        )
        _require_integrity(
            candidate.provenance == provenance
            and canonical_sha256(provenance) == row["provenance_sha256"],
            f"candidate provenance hash mismatch: {row['id']}",
        )
        return CandidateRecord(
            id=row["id"],
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            plan_id=row["plan_id"],
            attempt=row["attempt"],
            schema_version=row["schema_version"],
            candidate=candidate,
            candidate_sha256=row["candidate_sha256"],
            proof_sha256=row["proof_sha256"],
            provenance=provenance,
            provenance_sha256=row["provenance_sha256"],
            sealed_at=_parse_time(row["sealed_at"]) if row["sealed_at"] else None,
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _verification_from_row(row: sqlite3.Row) -> VerificationRecord:
        report = VerificationReport.model_validate_json(row["report_json"])
        provenance = Provenance.model_validate_json(row["provenance_json"])
        _require_integrity(
            verification_report_sha256(report) == row["report_sha256"],
            f"verification hash mismatch: {row['id']}",
        )
        _require_integrity(
            report.candidate_sha256 == row["candidate_sha256"],
            f"verification candidate hash mismatch: {row['id']}",
        )
        _require_integrity(
            report.provenance == provenance
            and canonical_sha256(provenance) == row["provenance_sha256"],
            f"verification provenance hash mismatch: {row['id']}",
        )
        return VerificationRecord(
            id=row["id"],
            run_id=row["run_id"],
            candidate_id=row["candidate_id"],
            thread_id=row["thread_id"],
            kind=row["kind"],
            schema_version=row["schema_version"],
            report=report,
            report_sha256=row["report_sha256"],
            candidate_sha256=row["candidate_sha256"],
            provenance=provenance,
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _stage_output_from_row(row: sqlite3.Row) -> StageOutputRecord:
        content = json.loads(row["content_json"])
        provenance = Provenance.model_validate_json(row["provenance_json"])
        _require_integrity(
            canonical_sha256(content) == row["content_sha256"],
            f"stage output hash mismatch: {row['id']}",
        )
        _require_integrity(
            canonical_sha256(provenance) == row["provenance_sha256"],
            f"stage output provenance hash mismatch: {row['id']}",
        )
        return StageOutputRecord(
            id=row["id"],
            run_id=row["run_id"],
            stage=RunStage(row["stage"]),
            kind=row["kind"],
            schema_version=row["schema_version"],
            content=content,
            content_sha256=row["content_sha256"],
            provenance=provenance,
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _turn_input_from_row(row: sqlite3.Row) -> TurnInputRecord:
        payload = json.loads(row["payload_json"])
        _require_integrity(
            isinstance(payload, dict)
            and canonical_sha256(payload) == row["payload_sha256"],
            f"turn input payload hash mismatch: {row['id']}",
        )
        return TurnInputRecord(
            id=row["id"],
            run_id=row["run_id"],
            role=row["role"],
            prompt_version=row["prompt_version"],
            output_schema_sha256=row["output_schema_sha256"],
            payload=cast(dict[str, JsonValue], payload),
            payload_sha256=row["payload_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        provenance = Provenance.model_validate_json(row["provenance_json"])
        _require_integrity(
            _SHA256_PATTERN.fullmatch(row["sha256"]) is not None,
            f"artifact hash is invalid: {row['id']}",
        )
        if row["relative_path"] is not None:
            validate_relative_artifact_path(row["relative_path"])
        _require_integrity(
            canonical_sha256(provenance) == row["provenance_sha256"],
            f"artifact provenance hash mismatch: {row['id']}",
        )
        return ArtifactRecord(
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            relative_path=row["relative_path"],
            media_type=row["media_type"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            schema_version=row["schema_version"],
            provenance=provenance,
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ExecutionLease:
        return ExecutionLease(
            id=row["id"],
            run_id=row["run_id"],
            worker_id=row["worker_id"],
            version=row["version"],
            runtime_version=row["runtime_version"],
            runtime_resolution_sha256=row["runtime_resolution_sha256"],
            lease_expires_at=_parse_time(row["lease_expires_at"]),
            released_at=_parse_time(row["released_at"]) if row["released_at"] else None,
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _runtime_resolution_from_row(row: sqlite3.Row) -> RuntimeResolutionRecord:
        resolution = cast(JsonValue, json.loads(row["resolution_json"]))
        _require_integrity(
            canonical_sha256(resolution) == row["resolution_sha256"],
            f"execution runtime resolution hash mismatch: {row['segment_id']}",
        )
        return RuntimeResolutionRecord(
            segment_id=row["segment_id"],
            run_id=row["run_id"],
            schema_version=row["schema_version"],
            resolution=resolution,
            resolution_sha256=row["resolution_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> Evidence:
        evidence = Evidence.model_validate_json(row["evidence_json"])
        _require_integrity(
            evidence_sha256(evidence) == row["evidence_sha256"],
            f"evidence hash mismatch: {row['id']}",
        )
        return evidence

    @staticmethod
    def _web_search_observation_from_row(
        row: sqlite3.Row,
    ) -> WebSearchObservation:
        observation = WebSearchObservation.model_validate_json(
            row["observation_json"]
        )
        _require_integrity(
            observation.run_id == row["run_id"]
            and observation.backend == row["backend"]
            and observation.external_thread_id == row["external_thread_id"]
            and observation.turn_id == row["turn_id"]
            and observation.item_id == row["item_id"]
            and observation.uri_sha256 == row["uri_sha256"]
            and canonical_sha256(observation) == row["observation_sha256"],
            f"web-search observation hash mismatch: {row['id']}",
        )
        return observation

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> Plan:
        plan = Plan.model_validate_json(row["plan_json"])
        _require_integrity(
            canonical_sha256(plan) == row["plan_sha256"],
            f"plan hash mismatch: {row['id']}",
        )
        return plan

    @staticmethod
    def _adjudication_from_row(row: sqlite3.Row) -> Adjudication:
        adjudication = Adjudication.model_validate_json(row["adjudication_json"])
        _require_integrity(
            canonical_sha256(adjudication) == row["adjudication_sha256"],
            f"adjudication hash mismatch: {row['id']}",
        )
        return adjudication

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> CandidateDecision:
        decision = CandidateDecision.model_validate_json(row["decision_json"])
        _require_integrity(
            candidate_decision_sha256(decision) == row["decision_sha256"],
            f"candidate decision hash mismatch: {row['candidate_id']}",
        )
        return decision
