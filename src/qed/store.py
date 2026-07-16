"""Transactional SQLite state store for deterministic QED runs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from qed.config import QEDConfig
from qed.schemas import (
    Event,
    ProofCandidate,
    Provenance,
    Sha256,
    VerificationReport,
    canonical_json,
    canonical_sha256,
)

SCHEMA_VERSION = 1


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


class RunSnapshot(StoreModel):
    run: RunRecord
    events: tuple[Event, ...]
    stage_outputs: tuple[StageOutputRecord, ...]
    threads: tuple[ThreadRecord, ...]
    candidates: tuple[CandidateRecord, ...]
    verifications: tuple[VerificationRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _render_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RunStore:
    """Owns schema setup, state transitions, and ordered run events."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
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
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
                FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL
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

            CREATE INDEX IF NOT EXISTS events_run_seq_idx ON events(run_id, seq);
            CREATE INDEX IF NOT EXISTS stage_outputs_run_idx ON stage_outputs(run_id, stage);
            CREATE INDEX IF NOT EXISTS threads_run_idx ON threads(run_id, role);
            CREATE INDEX IF NOT EXISTS candidates_run_idx ON candidates(run_id, attempt);
            CREATE INDEX IF NOT EXISTS verifications_candidate_idx
                ON verifications(candidate_id, kind);
            CREATE INDEX IF NOT EXISTS artifacts_run_idx ON artifacts(run_id, kind);

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
        version = self._connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()["value"]
        journal_mode = str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise StoreError("SQLite WAL mode is required")
        foreign_keys = bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])
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
        input_sha256: Sha256,
        provenance: Provenance,
    ) -> RunRecord:
        now = _utc_now()
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

    def get_run(self, run_id: str) -> RunRecord:
        row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run not found: {run_id}")
        return self._run_from_row(row)

    def list_runs(self) -> tuple[RunRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM runs ORDER BY created_at, id"
        ).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def transition_run(self, run_id: str, target: RunStatus) -> RunRecord:
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
            if target not in RUN_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"invalid run transition: {current.value} -> {target.value}"
                )
            if target is RunStatus.COMPLETED and RunStage(row["stage"]) is not RunStage.COMPLETE:
                raise InvalidTransitionError("a run can complete only from the complete stage")
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
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
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
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
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
            self._append_event(
                connection,
                run_id,
                event_type="run.cancelled",
                stage=RunStage(row["stage"]),
                payload={"from": current.value, "to": RunStatus.CANCELLED.value},
                created_at=now,
            )
        return self.get_run(run_id)

    def resume_run(self, run_id: str) -> RunRecord:
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            current = RunStatus(row["status"])
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
                    resume_count = resume_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.RUNNING.value, _render_time(now), run_id),
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

    def transition_stage(self, run_id: str, target: RunStage) -> RunRecord:
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_run_row(connection, run_id)
            if RunStatus(row["status"]) is not RunStatus.RUNNING:
                raise InvalidTransitionError("stages may transition only while a run is running")
            current = RunStage(row["stage"])
            if target not in STAGE_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"invalid stage transition: {current.value} -> {target.value}"
                )
            connection.execute(
                "UPDATE runs SET stage = ?, updated_at = ? WHERE id = ?",
                (target.value, _render_time(now), run_id),
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

    def add_stage_output(
        self,
        output_id: str,
        *,
        run_id: str,
        stage: RunStage,
        kind: str,
        content: JsonValue,
        provenance: Provenance,
    ) -> StageOutputRecord:
        now = _utc_now()
        content_json = canonical_json(content)
        provenance_json = canonical_json(provenance)
        try:
            with self._transaction() as connection:
                self._require_run_row(connection, run_id)
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
        row = self._connection.execute(
            "SELECT * FROM stage_outputs WHERE id = ?", (output_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"stage output not found: {output_id}")
        return self._stage_output_from_row(row)

    def list_stage_outputs(self, run_id: str) -> tuple[StageOutputRecord, ...]:
        self.get_run(run_id)
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
    ) -> ThreadRecord:
        now = _utc_now()
        role_value = ThreadRole(role)
        provenance_json = canonical_json(provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
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
        row = self._connection.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"thread not found: {thread_id}")
        return self._thread_from_row(row)

    def list_threads(self, run_id: str) -> tuple[ThreadRecord, ...]:
        self.get_run(run_id)
        rows = self._connection.execute(
            "SELECT * FROM threads WHERE run_id = ? ORDER BY created_at, id", (run_id,)
        ).fetchall()
        return tuple(self._thread_from_row(row) for row in rows)

    def transition_thread(
        self, thread_id: str, target: ThreadStatus
    ) -> ThreadRecord:
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_thread_row(connection, thread_id)
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
        self, candidate: ProofCandidate, *, thread_id: str | None = None
    ) -> CandidateRecord:
        now = _utc_now()
        candidate_json = canonical_json(candidate)
        provenance_json = canonical_json(candidate.provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, candidate.run_id)
                if thread_id is not None:
                    thread = self._require_thread_row(connection, thread_id)
                    if thread["run_id"] != candidate.run_id:
                        raise ConflictError("candidate thread belongs to another run")
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

    def update_candidate(self, candidate: ProofCandidate) -> CandidateRecord:
        now = _utc_now()
        candidate_json = canonical_json(candidate)
        provenance_json = canonical_json(candidate.provenance)
        try:
            with self._transaction() as connection:
                row = self._require_candidate_row(connection, candidate.id)
                if row["run_id"] != candidate.run_id:
                    raise ConflictError("candidate cannot move to another run")
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

    def seal_candidate(self, candidate_id: str) -> CandidateRecord:
        now = _utc_now()
        with self._transaction() as connection:
            row = self._require_candidate_row(connection, candidate_id)
            if row["sealed_at"] is not None:
                raise ImmutableRecordError("sealed candidate is immutable")
            connection.execute(
                "UPDATE candidates SET sealed_at = ?, updated_at = ? WHERE id = ?",
                (_render_time(now), _render_time(now), candidate_id),
            )
            run = self._require_run_row(connection, row["run_id"])
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
        row = self._connection.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"candidate not found: {candidate_id}")
        return self._candidate_from_row(row)

    def list_candidates(self, run_id: str) -> tuple[CandidateRecord, ...]:
        self.get_run(run_id)
        rows = self._connection.execute(
            "SELECT * FROM candidates WHERE run_id = ? ORDER BY attempt, created_at, id",
            (run_id,),
        ).fetchall()
        return tuple(self._candidate_from_row(row) for row in rows)

    def add_verification(
        self, run_id: str, report: VerificationReport
    ) -> VerificationRecord:
        now = _utc_now()
        report_json = canonical_json(report)
        provenance_json = canonical_json(report.provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
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
                ):
                    raise ConflictError("verification requires a fresh verifier thread")
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

    def update_verification(self, report: VerificationReport) -> VerificationRecord:
        try:
            with self._transaction() as connection:
                self._require_verification_row(connection, report.id)
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

    def delete_verification(self, verification_id: str) -> None:
        try:
            with self._transaction() as connection:
                self._require_verification_row(connection, verification_id)
                connection.execute(
                    "DELETE FROM verifications WHERE id = ?", (verification_id,)
                )
        except sqlite3.IntegrityError as error:
            if "verification is immutable" in str(error):
                raise ImmutableRecordError("verification is immutable") from error
            raise ConflictError(f"verification deletion failed: {verification_id}") from error

    def get_verification(self, verification_id: str) -> VerificationRecord:
        row = self._connection.execute(
            "SELECT * FROM verifications WHERE id = ?", (verification_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"verification not found: {verification_id}")
        return self._verification_from_row(row)

    def list_verifications(self, run_id: str) -> tuple[VerificationRecord, ...]:
        self.get_run(run_id)
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
    ) -> ArtifactRecord:
        now = _utc_now()
        provenance_json = canonical_json(provenance)
        try:
            with self._transaction() as connection:
                run = self._require_run_row(connection, run_id)
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
        row = self._connection.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"artifact not found: {artifact_id}")
        return self._artifact_from_row(row)

    def list_artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        self.get_run(run_id)
        rows = self._connection.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
        return tuple(self._artifact_from_row(row) for row in rows)

    def snapshot(self, run_id: str) -> RunSnapshot:
        return RunSnapshot(
            run=self.get_run(run_id),
            events=self.list_events(run_id),
            stage_outputs=self.list_stage_outputs(run_id),
            threads=self.list_threads(run_id),
            candidates=self.list_candidates(run_id),
            verifications=self.list_verifications(run_id),
            artifacts=self.list_artifacts(run_id),
        )

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        stage: RunStage,
        payload: dict[str, JsonValue],
    ) -> Event:
        with self._transaction() as connection:
            self._require_run_row(connection, run_id)
            return self._append_event(
                connection,
                run_id,
                event_type=event_type,
                stage=stage,
                payload=payload,
                created_at=_utc_now(),
            )

    def list_events(self, run_id: str, *, after_seq: int = 0) -> tuple[Event, ...]:
        self.get_run(run_id)
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
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            schema_version=row["schema_version"],
            status=RunStatus(row["status"]),
            stage=RunStage(row["stage"]),
            config=QEDConfig.model_validate_json(row["config_json"]),
            config_sha256=row["config_sha256"],
            input_sha256=row["input_sha256"],
            provenance=Provenance.model_validate_json(row["provenance_json"]),
            provenance_sha256=row["provenance_sha256"],
            runtime_version=row["runtime_version"],
            cancellation_requested=bool(row["cancellation_requested"]),
            resumable=bool(row["resumable"]),
            resume_count=row["resume_count"],
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
        return ThreadRecord(
            id=row["id"],
            run_id=row["run_id"],
            role=ThreadRole(row["role"]),
            parent_thread_id=row["parent_thread_id"],
            external_thread_id=row["external_thread_id"],
            model=row["model"],
            status=ThreadStatus(row["status"]),
            schema_version=row["schema_version"],
            provenance=Provenance.model_validate_json(row["provenance_json"]),
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> CandidateRecord:
        return CandidateRecord(
            id=row["id"],
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            plan_id=row["plan_id"],
            attempt=row["attempt"],
            schema_version=row["schema_version"],
            candidate=ProofCandidate.model_validate_json(row["candidate_json"]),
            candidate_sha256=row["candidate_sha256"],
            proof_sha256=row["proof_sha256"],
            provenance=Provenance.model_validate_json(row["provenance_json"]),
            provenance_sha256=row["provenance_sha256"],
            sealed_at=_parse_time(row["sealed_at"]) if row["sealed_at"] else None,
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _verification_from_row(row: sqlite3.Row) -> VerificationRecord:
        return VerificationRecord(
            id=row["id"],
            run_id=row["run_id"],
            candidate_id=row["candidate_id"],
            thread_id=row["thread_id"],
            kind=row["kind"],
            schema_version=row["schema_version"],
            report=VerificationReport.model_validate_json(row["report_json"]),
            report_sha256=row["report_sha256"],
            candidate_sha256=row["candidate_sha256"],
            provenance=Provenance.model_validate_json(row["provenance_json"]),
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _stage_output_from_row(row: sqlite3.Row) -> StageOutputRecord:
        return StageOutputRecord(
            id=row["id"],
            run_id=row["run_id"],
            stage=RunStage(row["stage"]),
            kind=row["kind"],
            schema_version=row["schema_version"],
            content=json.loads(row["content_json"]),
            content_sha256=row["content_sha256"],
            provenance=Provenance.model_validate_json(row["provenance_json"]),
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            relative_path=row["relative_path"],
            media_type=row["media_type"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            schema_version=row["schema_version"],
            provenance=Provenance.model_validate_json(row["provenance_json"]),
            provenance_sha256=row["provenance_sha256"],
            created_at=_parse_time(row["created_at"]),
        )
