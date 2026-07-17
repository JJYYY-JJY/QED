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
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from qed.config import QEDConfig
from qed.decision import CandidateDecision, decide_candidate
from qed.inputs import RunInput
from qed.schemas import (
    Adjudication,
    Event,
    Evidence,
    Plan,
    ProofCandidate,
    Provenance,
    Sha256,
    VerificationReport,
    canonical_json,
    canonical_sha256,
    sha256_text,
)

SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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
    RunStatus.PAUSED: frozenset(
        {RunStatus.CANCELLING, RunStatus.FAILED}
    ),
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


class RunSnapshot(StoreModel):
    run: RunRecord
    run_input: RunInput | None = None
    events: tuple[Event, ...]
    stage_outputs: tuple[StageOutputRecord, ...]
    threads: tuple[ThreadRecord, ...]
    candidates: tuple[CandidateRecord, ...]
    verifications: tuple[VerificationRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]
    execution_segments: tuple[ExecutionLease, ...] = ()
    evidence: tuple[Evidence, ...] = ()
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
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            self._connection.close()
            raise StoreError("SQLite WAL mode is required")
        self._initialize_schema()

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
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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

            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
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
            CREATE INDEX IF NOT EXISTS stage_outputs_run_idx ON stage_outputs(run_id, stage);
            CREATE INDEX IF NOT EXISTS evidence_run_idx ON evidence(run_id, created_at, id);
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
            CREATE TRIGGER IF NOT EXISTS artifacts_update_guard
            BEFORE UPDATE ON artifacts BEGIN
                SELECT RAISE(ABORT, 'artifact is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS artifacts_delete_guard
            BEFORE DELETE ON artifacts BEGIN
                SELECT RAISE(ABORT, 'artifact is immutable');
            END;
            """
        )
        existing = self._connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if existing is None:
            self._connection.execute(
                "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(existing["value"]) != SCHEMA_VERSION:
            raise StoreError(
                f"unsupported schema version {existing['value']}; expected {SCHEMA_VERSION}"
            )

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
        _validate_id(run_id, field="run_id")
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

    def release_execution(self, execution: ExecutionToken) -> ExecutionLease:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_execution_row(connection, execution.segment_id)
            self._validate_execution_identity(row, execution)
            if row["released_at"] is not None:
                return self._execution_from_row(row)
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

    def acknowledge_cancel(self, run_id: str) -> RunRecord:
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
            connection.execute(
                """
                UPDATE runs
                SET status = ?, cancellation_requested = 1, resumable = 1, updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.CANCELLED.value, _render_time(now), run_id),
            )
            connection.execute(
                """
                UPDATE execution_segments
                SET released_at = COALESCE(released_at, ?), updated_at = ?
                WHERE run_id = ? AND released_at IS NULL
                """,
                (_render_time(now), _render_time(now), run_id),
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
        if idempotency_key is not None:
            _validate_id(idempotency_key, field="idempotency_key")
        now = self._now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
            if (
                idempotency_key is not None
                and row["last_resume_key"] == idempotency_key
                and current is RunStatus.RUNNING
            ):
                return self._run_from_row(row)
            if not bool(row["resumable"]) or current not in {
                RunStatus.PAUSED,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
            }:
                raise InvalidTransitionError(f"run is not resumable while {current.value}")
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
        return self.get_run(run_id)

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
        _validate_id(run_id, field="run_id")
        _validate_id(evidence.id, field="evidence.id")
        now = self._now()
        rendered = canonical_json(evidence)
        with self._transaction() as connection:
            run = self._require_run_row(connection, run_id)
            self._authorize_execution(connection, run_id, execution)
            if RunStage(run["stage"]) is not RunStage.LITERATURE:
                raise InvalidTransitionError("typed evidence may be added only in literature")
            try:
                connection.execute(
                    """
                    INSERT INTO evidence (
                        id, run_id, schema_version, evidence_json, evidence_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.id,
                        run_id,
                        evidence.schema_version,
                        rendered,
                        canonical_sha256(evidence),
                        _render_time(now),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError(f"evidence already exists: {evidence.id}") from error
            self._append_event(
                connection,
                run_id,
                event_type="evidence.created",
                stage=RunStage.LITERATURE,
                payload={"evidence_id": evidence.id},
                created_at=now,
            )
        return self.get_evidence(evidence.id)

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
            rows = self._connection.execute(
                "SELECT * FROM plans WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
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
            rows = self._connection.execute(
                """
                SELECT * FROM adjudications WHERE run_id = ? ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
            return tuple(self._adjudication_from_row(row) for row in rows)

    def record_decision(
        self,
        run_id: str,
        candidate_id: str,
        *,
        require_citation: bool = False,
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
            candidate = self._candidate_from_row(candidate_row).candidate
            report_rows = connection.execute(
                """
                SELECT * FROM verifications
                WHERE run_id = ? AND candidate_id = ? ORDER BY created_at, id
                """,
                (run_id, candidate_id),
            ).fetchall()
            reports = tuple(self._verification_from_row(row).report for row in report_rows)
            decision = decide_candidate(
                candidate, reports, require_citation=require_citation
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
                    canonical_sha256(decision),
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

    def transition_thread(
        self,
        thread_id: str,
        target: ThreadStatus,
        *,
        execution: ExecutionToken | None = None,
    ) -> ThreadRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_thread_row(connection, thread_id)
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
                if attempt_count >= config.budgets.proof_attempts:
                    raise ConflictError("proof attempt budget exhausted")
                if candidate.attempt != attempt_count + 1:
                    raise ConflictError("candidate attempt must be the next durable attempt")
                connection.execute(
                    """
                    INSERT INTO candidates (
                        id, run_id, thread_id, plan_id, attempt, schema_version,
                        candidate_json, candidate_sha256, proof_sha256, provenance_json,
                        provenance_sha256, sealed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
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
                        _render_time(now),
                        _render_time(now),
                    ),
                )
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
                    event_type="candidate.created",
                    stage=RunStage(run["stage"]),
                    payload={"candidate_id": candidate.id, "sealed": False},
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
                if (
                    report.verifier_external_thread_id is not None
                    and report.verifier_external_thread_id != external_thread_id
                ):
                    raise ConflictError(
                        "verification external thread id does not match the stored thread"
                    )
                if report.verifier_external_thread_id is None:
                    report = report.model_copy(
                        update={"verifier_external_thread_id": external_thread_id}
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
                        canonical_sha256(report),
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
                        canonical_sha256(report),
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
            evidence_rows = connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            plan_rows = connection.execute(
                "SELECT * FROM plans WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            ).fetchall()
            adjudication_rows = connection.execute(
                """
                SELECT * FROM adjudications WHERE run_id = ? ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
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
                threads=tuple(self._thread_from_row(row) for row in thread_rows),
                candidates=tuple(self._candidate_from_row(row) for row in candidate_rows),
                verifications=tuple(
                    self._verification_from_row(row) for row in verification_rows
                ),
                artifacts=tuple(self._artifact_from_row(row) for row in artifact_rows),
                execution_segments=tuple(
                    self._execution_from_row(row) for row in execution_rows
                ),
                evidence=tuple(self._evidence_from_row(row) for row in evidence_rows),
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

    @staticmethod
    def _latest_adjudication_row(
        connection: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT * FROM adjudications
                WHERE run_id = ? ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone(),
        )

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
            if connection.execute(
                "SELECT 1 FROM evidence WHERE run_id = ? LIMIT 1", (run_id,)
            ).fetchone() is None:
                raise InvalidTransitionError("planning requires typed evidence")
            return

        if current is RunStage.PLANNING and target is RunStage.PROVING:
            if connection.execute(
                "SELECT 1 FROM plans WHERE run_id = ? LIMIT 1", (run_id,)
            ).fetchone() is None:
                raise InvalidTransitionError("proving requires a typed plan")
            return

        if current is RunStage.PROVING and target is RunStage.VERIFICATION:
            if connection.execute(
                """
                SELECT 1 FROM candidates
                WHERE run_id = ? AND sealed_at IS NOT NULL LIMIT 1
                """,
                (run_id,),
            ).fetchone() is None:
                raise InvalidTransitionError("verification requires a sealed proof candidate")
            return

        if current is RunStage.VERIFICATION and target is RunStage.ADJUDICATION:
            if not self._has_independently_verified_candidate(connection, run_id):
                raise InvalidTransitionError(
                    "adjudication requires structural and detailed independent reports"
                )
            return

        if current is RunStage.ADJUDICATION:
            latest_row = self._latest_adjudication_row(connection, run_id)
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
        if not any(self._decision_from_row(row).passed for row in decision_rows):
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
        self, connection: sqlite3.Connection, run_id: str
    ) -> bool:
        candidate_rows = connection.execute(
            """
            SELECT * FROM candidates WHERE run_id = ? AND sealed_at IS NOT NULL
            """,
            (run_id,),
        ).fetchall()
        for candidate_row in candidate_rows:
            report_rows = connection.execute(
                """
                SELECT * FROM verifications WHERE candidate_id = ?
                ORDER BY created_at, id
                """,
                (candidate_row["id"],),
            ).fetchall()
            reports = tuple(self._verification_from_row(row).report for row in report_rows)
            kinds = {report.kind for report in reports}
            external_ids = {
                report.verifier_external_thread_id
                for report in reports
                if report.kind in {"structural", "detailed"}
                and report.verifier_external_thread_id is not None
            }
            if {"structural", "detailed"}.issubset(kinds) and len(external_ids) >= 2:
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
            canonical_sha256(report) == row["report_sha256"],
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
    def _evidence_from_row(row: sqlite3.Row) -> Evidence:
        evidence = Evidence.model_validate_json(row["evidence_json"])
        _require_integrity(
            canonical_sha256(evidence) == row["evidence_sha256"],
            f"evidence hash mismatch: {row['id']}",
        )
        return evidence

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
            canonical_sha256(decision) == row["decision_sha256"],
            f"candidate decision hash mismatch: {row['candidate_id']}",
        )
        return decision
