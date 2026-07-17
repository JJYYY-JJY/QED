# Legacy run migration

QED imports a legacy run directory as an untrusted, content-addressed archive.
The importer preserves bytes for audit and leaves the source directory intact.
It does not convert legacy Markdown verdicts into trusted QED decisions.

## Import a directory

Choose a managed data root outside the legacy source:

```bash
uv run qed migrate /absolute/path/to/legacy-run --data-root .qed
```

The command prints JSON with the import directory and manifest. QED writes the
archive under:

```text
<data-root>/legacy-imports/legacy-<content-prefix>/
├── artifacts/       # byte copies with source-relative paths
└── manifest.json
```

The manifest records:

- the `legacy_untrusted` trust label;
- the resolved source root and aggregate content SHA-256;
- each regular file's relative path, byte size, and SHA-256.

The aggregate hash determines the import ID. Importing the same unchanged tree
returns the same address and validates the existing copy. QED rejects an
existing address when its manifest or artifact bytes differ.

## Safety rules

The importer:

- opens regular files without following symbolic links;
- rejects a symbolic link at any point in the source tree;
- rejects a managed root inside the source tree;
- inventories the source before copying and checks each file again during copy;
- writes through a staging directory and renames the complete archive into
  place;
- leaves the source tree unchanged.

Use a source snapshot when another process may still write the legacy run. A
mid-import change causes the command to fail, and QED removes its staging
directory.

## Trust and re-verification

Legacy providers, model names, prompt text, reports, logs, and verdict strings
remain historical evidence inside the archive. They have no authority in the
current state machine. The importer does not create a runnable current run,
select a candidate, or mark a proof as passed.

To research a legacy problem under the current policy, create a new run with
the exact problem, guidance, and verification rules. Keep the legacy import ID
in your external research notes. QED will generate new evidence, candidates,
fresh verifier reports, and a new content-addressed export.

## Limits

Legacy import manifest schema version 1 preserves the legacy tree as files. It
does not parse either historical directory dialect into typed evidence, plans,
candidates, token usage, or event records. Do not delete the source archive
after import unless your retention policy treats the managed copy and its
backup as sufficient.

## SQLite schema upgrades

The current SQLite database schema is version 2. Typed run, API, event, and
legacy import records still use their independent `schema_version: 1`; those
numbers describe record formats rather than the database layout.

When QED opens a version 1 database, it creates the version 2 structures and
guards that are missing, then updates the database metadata to version 2. The
operation is idempotent, so reopening after an interrupted startup repeats the
same checks. QED opens version 2 directly and rejects every other database
schema version.

Back up the complete data root before starting a newer QED release against it.
There is no downgrade command. Test startup and resume against a copy before
allowing the new release to upgrade the production database.
