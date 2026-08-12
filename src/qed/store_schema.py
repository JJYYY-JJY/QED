"""SQLite schema-version and migration preflight for the QED store."""

from __future__ import annotations

import sqlite3

DATABASE_SCHEMA_VERSION = 5
SUPPORTED_DATABASE_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5})


class SchemaVersionError(ValueError):
    """Persisted schema metadata is missing, malformed, or unsupported."""


class DuplicateExternalThreadIdentityError(ValueError):
    """A uniqueness migration cannot preserve conflicting thread identities."""


def prepare_schema_migration(connection: sqlite3.Connection) -> int | None:
    """Read the prior version and fail before DDL on incompatible identity data."""

    metadata_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'schema_metadata'
        """
    ).fetchone()
    prior_version: int | None = None
    if metadata_exists is not None:
        version_row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if version_row is None:
            raise SchemaVersionError(
                "database schema metadata is missing its version"
            )
        try:
            prior_version = int(version_row["value"])
        except (TypeError, ValueError) as error:
            raise SchemaVersionError(
                "database schema version is not an integer"
            ) from error
        if prior_version not in SUPPORTED_DATABASE_SCHEMA_VERSIONS:
            raise SchemaVersionError(
                f"unsupported schema version {prior_version}; expected one of "
                f"{sorted(SUPPORTED_DATABASE_SCHEMA_VERSIONS)}"
            )

    threads_table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'threads'
        """
    ).fetchone()
    if threads_table_exists is None:
        return prior_version

    duplicate_rows = connection.execute(
        """
        SELECT
            run_id,
            external_thread_id,
            GROUP_CONCAT(id, ',') AS thread_ids
        FROM threads
        WHERE external_thread_id IS NOT NULL
        GROUP BY run_id, external_thread_id
        HAVING COUNT(*) > 1
        ORDER BY run_id, external_thread_id
        """
    ).fetchall()
    if duplicate_rows:
        details = "; ".join(
            (
                f"run={row['run_id']} external={row['external_thread_id']} "
                f"threads={row['thread_ids']}"
            )
            for row in duplicate_rows
        )
        raise DuplicateExternalThreadIdentityError(
            "duplicate external thread identities block schema migration: "
            f"{details}"
        )
    return prior_version


def finalize_schema_migration(
    connection: sqlite3.Connection,
    prior_version: int | None,
) -> None:
    """Persist the current version after idempotent DDL has succeeded."""

    if prior_version is None:
        connection.execute(
            "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?)",
            (str(DATABASE_SCHEMA_VERSION),),
        )
    elif prior_version != DATABASE_SCHEMA_VERSION:
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            (str(DATABASE_SCHEMA_VERSION),),
        )
