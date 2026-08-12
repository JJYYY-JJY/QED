"""Atomic SQLite backup, restore, and schema-upgrade operations."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from qed.schemas import StrictModel
from qed.security.paths import PathSecurityError, reject_symlink_components
from qed.store import RunStore
from qed.store_schema import (
    DATABASE_SCHEMA_VERSION,
    SUPPORTED_DATABASE_SCHEMA_VERSIONS,
    prepare_schema_migration,
)


class MigrationPreflight(StrictModel):
    schema_version: Literal[1] = 1
    database: str
    valid: bool
    integrity: str
    current_version: int | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class MigrationError(RuntimeError):
    """A backup, restore, or upgrade could not be completed safely."""


def _safe_path(path: Path, *, allow_missing: bool = False) -> Path:
    target = path.absolute()
    try:
        reject_symlink_components(target)
    except PathSecurityError as error:
        raise MigrationError(str(error)) from error
    if target.exists() and target.is_symlink():
        raise MigrationError(f"database path cannot be a symbolic link: {target}")
    if not allow_missing and not target.exists():
        raise MigrationError(f"database does not exist: {target}")
    return target


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_database(source: Path, destination: Path) -> None:
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            f"file:{source}?mode=ro",
            uri=True,
            timeout=5,
        )
        target_connection = sqlite3.connect(destination, timeout=5)
        source_connection.backup(target_connection)
        integrity = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError(f"backup integrity check failed: {integrity}")
        target_connection.commit()
    except sqlite3.Error as error:
        raise MigrationError(f"SQLite backup failed: {error}") from error
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def backup_database(database: str | Path, output: str | Path) -> Path:
    """Create a verified immutable backup without changing the source database."""

    source = _safe_path(Path(database))
    destination = _safe_path(Path(output), allow_missing=True)
    if destination.exists():
        raise MigrationError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        temporary_path.chmod(0o600)
        _copy_database(source, temporary_path)
        _fsync_file(temporary_path)
        temporary_path.replace(destination)
        return destination
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def restore_database(backup: str | Path, database: str | Path) -> Path:
    """Restore a verified backup through an atomic replacement."""

    source = _safe_path(Path(backup))
    destination = _safe_path(Path(database), allow_missing=True)
    if source == destination:
        raise MigrationError("backup and database paths must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.restore-",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        temporary_path.chmod(0o600)
        _copy_database(source, temporary_path)
        _fsync_file(temporary_path)
        temporary_path.replace(destination)
        return destination
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def preflight_database(database: str | Path) -> MigrationPreflight:
    """Inspect migration blockers without applying DDL."""

    target = _safe_path(Path(database))
    errors: list[str] = []
    warnings: list[str] = []
    integrity = "unknown"
    version: int | None = None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(target, timeout=5)
        connection.row_factory = sqlite3.Row
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            errors.append(f"sqlite integrity check: {integrity}")
        try:
            version_row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            version = int(version_row["value"]) if version_row is not None else None
        except (sqlite3.Error, TypeError, ValueError) as error:
            errors.append(f"schema version is unreadable: {error}")
        if version not in SUPPORTED_DATABASE_SCHEMA_VERSIONS:
            errors.append(f"unsupported schema version: {version}")
        try:
            prepare_schema_migration(connection)
        except ValueError as error:
            errors.append(str(error))
        duplicate_events = connection.execute(
            "SELECT run_id, seq, COUNT(*) AS count FROM events "
            "GROUP BY run_id, seq HAVING COUNT(*) > 1"
        ).fetchall()
        if duplicate_events:
            errors.append("duplicate event sequence exists")
        if _table_exists(connection, "execution_segments"):
            stale = connection.execute(
                "SELECT COUNT(*) AS count FROM execution_segments "
                "WHERE released_at IS NULL AND lease_expires_at <= ?",
                (datetime.now(UTC).isoformat(),),
            ).fetchone()["count"]
            if stale:
                warnings.append(
                    f"{stale} execution lease(s) are expired and require reconciliation"
                )
        if _table_exists(connection, "candidates"):
            invalid_immutable = connection.execute(
                "SELECT COUNT(*) AS count FROM candidates "
                "WHERE sealed_at IS NOT NULL AND length(candidate_sha256) != 64"
            ).fetchone()["count"]
            if invalid_immutable:
                errors.append("sealed candidate hash is invalid")
        if target.name.lower() in {"auth.json", "credentials.json", "token.json"}:
            errors.append("database path is credential-shaped and is refused")
    except sqlite3.Error as error:
        errors.append(f"SQLite preflight failed: {error}")
    finally:
        if connection is not None:
            connection.close()
    return MigrationPreflight(
        database=str(target),
        valid=not errors,
        integrity=integrity,
        current_version=version,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def upgrade_database(database: str | Path) -> Path:
    """Upgrade a copy, then atomically replace the database only after validation."""

    source = _safe_path(Path(database))
    preflight = preflight_database(source)
    if not preflight.valid:
        raise MigrationError("database preflight failed: " + "; ".join(preflight.errors))
    if preflight.current_version == DATABASE_SCHEMA_VERSION:
        return source
    with tempfile.NamedTemporaryFile(
        prefix=f".{source.name}.upgrade-",
        dir=source.parent,
        delete=False,
    ) as temporary:
        staging = Path(temporary.name)
    try:
        staging.chmod(0o600)
        _copy_database(source, staging)
        with RunStore(staging) as upgraded:
            if upgraded.info().schema_version != DATABASE_SCHEMA_VERSION:
                raise MigrationError("staged upgrade did not reach the current schema")
        _fsync_file(staging)
        staging.replace(source)
        return source
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
