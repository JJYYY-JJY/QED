# Python and uv packaging decision

Research date: 2026-07-16 (America/Los_Angeles)

## Decision

QED uses uv for the complete Python lifecycle: selecting/installing CPython,
creating the project virtual environment, resolving and locking dependencies,
installing development groups, running tools, and building the package. Conda is
not part of setup or runtime.

The repository pins CPython `3.14.6` in `.python-version` for local development
and records the full dependency solution in `uv.lock`. The package contract is
`>=3.13,<3.15`, and CI exercises both 3.13 and 3.14. This lets contributors follow
the current stable Python line without making the newest minor version the only
supported deployment target.

The one Python setup command is:

```bash
uv sync --all-groups --frozen
```

uv will obtain the pinned interpreter automatically when it is missing. A local
`.venv` is generated state and is not committed.

## Evidence and caveats

- Python.org released Python 3.14.6 on 2026-06-10 and describes it as the sixth
  3.14 maintenance release: [Python 3.14.6 release](https://www.python.org/downloads/release/python-3146/).
- uv documents automatic Python downloads, explicit version installation, and
  `.python-version` discovery: [installing Python](https://docs.astral.sh/uv/guides/install-python/)
  and [Python versions](https://docs.astral.sh/uv/concepts/python-versions/).
- uv documents that an existing `uv.lock` is preferred during sync and lock
  operations: [locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/).
- uv-managed CPython builds come from Astral's `python-build-standalone`, because
  python.org does not publish portable binaries in the shape uv needs. They are
  CPython builds, but not binary distributions produced by the PSF.

uv does not replace the Node toolchain used by the React application or the
official Codex CLI distribution. The repository-level setup command composes uv
with the pinned npm workspace; it does not introduce Conda as a second Python
resolver.

## Upgrade policy

Patch upgrades require the full backend suite and the opt-in SDK/App Server
contract smoke test. Minor-version upgrades additionally require resolving the
lock for the supported range and running CI on both the oldest and newest
declared Python versions. Dependency upgrades are explicit lockfile changes, not
implicit environment drift.
