# QED contributor instructions

QED is a Codex-only mathematical research system. Preserve the upstream MIT
license, attribution, prompt archive, proved statements, and historical research
artifacts. Do not add Claude, Gemini, or generic provider dispatch.

## Before changing code

- State assumptions and success criteria before non-trivial work.
- Prefer the smallest change that satisfies the requested behavior.
- Keep unrelated formatting, refactors, and dead-code cleanup out of the diff.
- Use tests to reproduce behavior before implementing it.

## Runtime invariants

- All Codex access goes through `qed.runtime.CodexRuntime`.
- Control outputs are strict Pydantic models constrained by JSON Schema. Never
  infer stage transitions or verdicts from Markdown or substring matching.
- SQLite is the durable source of truth. Transitions are explicit and events use
  store-assigned monotonic sequence numbers.
- Sealed proof candidates and verifier reports are immutable. A resumed run adds
  new attempts; it never edits frozen inputs or earlier reports.
- Structural and detailed verifiers start on fresh threads with frozen inputs,
  a read-only sandbox, no command network, and no approval escape path.
- Literature and citation work are the only roles allowed search or restricted
  network access. Structural and detailed verification stays offline.
- Application code computes PASS from structured reports. Agents and skills do
  not write verdicts or mutate run state.
- Never add full-access sandboxes, raw Codex config overrides, unsafe approval or
  hook bypass flags, committed credentials, or browser-visible secrets.

## Architecture boundaries

- `qed.runtime` adapts the official Python SDK, version-matched App Server, and
  the explicitly selected `codex exec` fallback.
- `qed.store` owns persistence and transition enforcement.
- The orchestration layer depends on those public interfaces, not their
  implementations.
- FastAPI and the CLI call the same application service. The React client talks
  only to the typed HTTP/SSE API.
- Repo-local skills describe reusable research methods; they do not duplicate
  runtime orchestration.

## Verification

Use the repository toolchains; do not create a Conda environment.

```bash
uv sync --all-groups
uv run ruff check .
uv run mypy src
uv run pytest
npm ci
npm run lint
npm run typecheck
npm run test
npm run build
npm run test:e2e
```

Real Codex smoke tests are opt-in and must never run in the default test suite.
Frontend changes run the checked-in unit, accessibility, contract, and Playwright
tests. Update architecture, configuration, migration, and threat-model documents
when their contracts change.
