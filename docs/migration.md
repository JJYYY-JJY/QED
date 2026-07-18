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

## v2 alpha authority boundary

Import and database migration preserve data; they do not upgrade trust. A
file-based run from v1, an upstream archive, or another provider remains
`legacy_untrusted` after import. QED does not synthesize a current candidate,
verifier identity, verification-rule coverage, adjudication, or code decision
from legacy Markdown.

A current QED policy PASS requires a new v2 run under the frozen v2 input and
policy. That run must create sealed candidates and fresh structured verifier
reports in SQLite. Copying a legacy proof into research guidance or citing its
import ID does not change this requirement.

SQLite schema upgrades also grant no new decision authority. An upgrade may add
tables, columns, indexes, or guards. It must not manufacture a missing external
thread identity, rule-coverage check, terminal event, lease, or content hash.
Startup stops on incompatible or duplicate data when a new uniqueness invariant
cannot be established. Operators should diagnose and retain the original
database rather than deleting or overwriting conflicting rows.

The v2 alpha release and old-version retention steps are listed in
[QED v2 alpha release boundary](release-v2-alpha.md).

## SQLite schema upgrades

The current SQLite database schema is version 4. QED opens database versions 1,
2, 3, and 4. It creates the missing version 4 structures and guards for a
supported older database, then updates the database metadata to version 4. The
operation is idempotent, so reopening after an interrupted startup repeats the
same checks. QED rejects every other database schema version.

Before applying the migration, QED searches each run for duplicate non-null
external thread IDs. A duplicate blocks startup with the run ID, external ID,
and conflicting local thread IDs. The migration never chooses one row or
overwrites the conflict.

Typed run, API, event, and legacy import records use schema versions separate
from the database layout. Evidence v1 remains hash-verifiable and readable but
lacks the current trust fields. Verification-report v1/v2 and code-decision
v1/v2 also remain readable with their original hashes; they predate the current
structured citation-support authority. Current reports and decisions use record
schema v3. Older record schemas cannot grant current QED policy PASS authority,
and resume adds new immutable attempts rather than rewriting them.

Back up the complete data root before starting a newer QED release against it.
There is no downgrade command. Test startup and resume against a copy before
allowing the new release to upgrade the production database.
