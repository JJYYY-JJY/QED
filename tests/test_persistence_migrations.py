from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from qed.persistence import migrations
from qed.persistence.migrations import (
    MigrationError,
    backup_database,
    preflight_database,
    restore_database,
    upgrade_database,
)
from qed.store import RunStore

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "migrations"


def test_migration_fixture_manifest_binds_all_fixture_bytes() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["current_database_schema_version"] == 5
    assert [entry["version"] for entry in manifest["fixtures"]] == [1, 2, 3, 4, 5]
    for entry in manifest["fixtures"]:
        fixture = FIXTURE_DIR / entry["path"]
        content = fixture.read_bytes()
        assert len(content) == entry["size_bytes"]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]


def test_backup_and_restore_are_atomic_and_verified(tmp_path: Path) -> None:
    database = tmp_path / "qed.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    with RunStore(database):
        pass

    assert backup_database(database, backup) == backup
    assert preflight_database(backup).valid is True
    assert restore_database(backup, restored) == restored
    assert preflight_database(restored).valid is True

    with pytest.raises(MigrationError, match="already exists"):
        backup_database(database, backup)


def test_upgrade_does_not_replace_original_on_failed_preflight(tmp_path: Path) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE schema_metadata SET value = '999' WHERE key = 'schema_version'"
        )
        connection.commit()
    original = database.read_bytes()

    with pytest.raises(MigrationError, match="preflight"):
        upgrade_database(database)

    assert database.read_bytes() == original


def test_upgrade_from_schema_v4_uses_staging_copy(tmp_path: Path) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE runtime_provenance")
        connection.execute(
            "UPDATE schema_metadata SET value = '4' WHERE key = 'schema_version'"
        )
        connection.commit()

    assert preflight_database(database).valid is True
    assert upgrade_database(database) == database
    with RunStore(database) as store:
        assert store.info().schema_version == 5
        assert "runtime_provenance" in store.info().tables


def test_preflight_reports_missing_metadata_and_invalid_integrity(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE schema_metadata (key TEXT, value TEXT)")
        connection.execute("INSERT INTO schema_metadata VALUES ('schema_version', 'bad')")
        connection.commit()
    report = preflight_database(database)
    assert report.valid is False
    assert any("unreadable" in error for error in report.errors)
    assert any("unsupported" in error for error in report.errors)

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database")
    corrupt_report = preflight_database(corrupt)
    assert corrupt_report.valid is False
    assert corrupt_report.integrity == "unknown"


def test_migration_paths_reject_missing_symlink_and_aliases(tmp_path: Path) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database):
        pass
    with pytest.raises(MigrationError, match="does not exist"):
        backup_database(tmp_path / "missing.sqlite3", tmp_path / "backup.sqlite3")

    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(database)
    with pytest.raises(MigrationError, match="link"):
        backup_database(linked, tmp_path / "backup-linked.sqlite3")
    with pytest.raises(MigrationError, match="paths must differ"):
        restore_database(database, database)


def test_backup_and_restore_clean_staging_after_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database):
        pass

    def fail_copy(_source: Path, _destination: Path) -> None:
        raise MigrationError("injected copy failure")

    monkeypatch.setattr(migrations, "_copy_database", fail_copy)
    with pytest.raises(MigrationError, match="injected"):
        backup_database(database, tmp_path / "backup.sqlite3")
    assert not tuple(tmp_path.glob(".backup.sqlite3.*"))

    with pytest.raises(MigrationError, match="injected"):
        restore_database(database, tmp_path / "restored.sqlite3")
    assert not tuple(tmp_path.glob(".restored.sqlite3.restore-*"))


def test_upgrade_rejects_credential_shaped_database(tmp_path: Path) -> None:
    credential_path = tmp_path / "auth.json"
    with RunStore(credential_path):
        pass
    report = preflight_database(credential_path)
    assert report.valid is False
    assert any("credential-shaped" in error for error in report.errors)


@pytest.mark.parametrize("version", range(1, 6))
def test_supported_schema_fixture_preflights_and_upgrades_atomically(
    tmp_path: Path,
    version: int,
) -> None:
    source = FIXTURE_DIR / f"v{version}.sqlite3"
    database = tmp_path / source.name
    shutil.copy2(source, database)
    original = database.read_bytes()

    report = preflight_database(database)
    assert report.valid is True
    assert report.current_version == version

    if version < 5:
        assert upgrade_database(database) == database

    with RunStore(database) as store:
        assert store.info().schema_version == 5
    assert source.read_bytes() == original
