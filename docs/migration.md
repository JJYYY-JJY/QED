# SQLite migration and recovery

QED schema version 5 is the current stable-candidate schema. SQLite remains the
single durable authority; migrations never edit frozen research records in
place without a preflight and a verified staged copy.

## Supported versions

Schema versions 1 through 5 are accepted by the preflight. The checked-in
fixture matrix under `tests/fixtures/migrations/` exercises every supported
version, v1/v2 historical layouts, the v4-compatible v3/v4 layout, the v5
schema, malformed input, unsupported versions, backup, restore, and staged
upgrade. The v3 fixture is explicitly a compatibility fixture because the
repository history contains no separate v3 DDL snapshot; it is not presented
as historical schema evidence. Unsupported downgrades are rejected. A stable
rollback is a restore from a verified backup, not a guessed reverse migration.

Preflight checks integrity, readable schema version, duplicate `(run_id, seq)`
events, conflicting external thread identities, stale unreleased leases,
invalid immutable candidate hashes, and credential-shaped database paths. A
preflight warning about a stale lease is not permission to delete or release
it; reconcile the owner or record an explicit operator abandonment.

## Safe commands

Stop QED before changing a production data root. Use a dedicated backup path
outside the data root and protect it as sensitive:

```bash
uv run --frozen qed backup /srv/qed/qed.sqlite3 \
  --output /srv/backups/qed-2026-08-11.sqlite3
uv run --frozen qed upgrade /srv/qed/qed.sqlite3
uv run --frozen qed restore /srv/backups/qed-2026-08-11.sqlite3 \
  --database /srv/qed/qed.sqlite3
```

`backup` uses SQLite backup APIs, validates the copy, fsyncs it, and publishes
it atomically. `upgrade` runs preflight, copies to a private staging file,
opens the staged copy through `RunStore`, and replaces the original only after
the current schema and integrity checks pass. A failed upgrade deletes only its
staging file and leaves the original database byte-for-byte available for
restore. `restore` follows the same staged replacement path and rejects source
and destination aliasing, symlinked components, and credential-shaped paths.

Do not run two QED versions against one database. Preserve SQLite `-wal` and
`-shm` files when making a filesystem-level data-root backup, or stop the
service and use `qed backup` for a single-file backup. The dedicated Codex home
may contain authentication state; it must never be included in an export or
committed to Git. Rotate credentials if a backup leaves the operator boundary.

## Failed-upgrade recovery

1. Stop the service and retain the original database plus its WAL files.
2. Capture `qed doctor --json` and the migration preflight result without
   editing SQLite.
3. Run `qed restore <verified-backup> --database <staging-database>` and check
   `PRAGMA integrity_check`, schema version, event-chain continuity, lease
   ownership, and one read-only run snapshot.
4. Start QED against the restored staging data root on loopback only.
5. Resume only runs whose durable state has no unconfirmed runtime terminal and
   no active lease owned by another execution. Abandon an unrecoverable run
   through the command so the operator rationale is recorded.

Migration never restores credentials from model output, never imports a
personal `~/.codex`, and never treats legacy Markdown, old provider metadata,
or a fixture result as current PASS authority.

## Retention and compatibility

Frozen inputs, candidates, verifier reports, events, execution segments, and
export intent records are append-only audit history. Resume adds attempts and
segments; it does not replenish budgets or rewrite earlier records. Current
readers may display legacy records, but only schema-current structured reports
with valid Codex provenance and stable rule/claim coverage can participate in a
QED policy PASS.
