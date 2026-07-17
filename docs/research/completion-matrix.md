# Production rewrite completion matrix

Last updated: 2026-07-17

This matrix records the evidence available in the final working tree. `Cleared`
means the implementation and its scoped check are present. All forty-two rows
are cleared on the local `codex-native-rewrite` candidate; nothing was pushed.

## Repository and preservation

| ID | Requirement | Evidence | Status |
| --- | --- | --- | --- |
| R-01 | Audit repository, remotes, license, behavior, tests, prompts, and outputs before deletion | Repository, frontend, core, preservation, packaging, and runtime research notes | Cleared |
| R-02 | Preserve MIT license and upstream attribution | Unchanged `LICENSE`; maintainer and upstream attribution in README | Cleared |
| R-03 | Preserve prompts, proved statements, and useful artifacts | Preservation map, README archive index, and protected-asset hash check | Cleared |
| R-04 | Remove Claude, Gemini, provider dispatch, and Streamlit runtime paths | Production-path scan and two no-legacy regression tests | Cleared |

## Codex runtime

| ID | Requirement | Evidence | Status |
| --- | --- | --- | --- |
| C-01 | Use the official `openai-codex` SDK | Locked package, SDK adapter tests, and router tests | Cleared |
| C-02 | Use one typed App Server adapter for controls absent from the SDK | Typed JSON-RPC transport, paging, event, network-policy, and interrupt tests | Cleared |
| C-03 | Keep `codex exec` behind explicit selection | Router selection and strict argv tests | Cleared |
| C-04 | Default to `gpt-5.6-sol` | Config schema, API capabilities, CLI, sample request, and frontend defaults | Cleared |
| C-05 | Resolve effort and multi-agent support from capabilities | Exact-model effort tests, `proactive_multi_agent` gating, persisted resolution, and drift checks | Cleared |
| C-06 | Expose model, effort, parallelism, budgets, search, and sandbox in one strict config | Pydantic schema, typed API, complete browser request test, sample request, and CLI core controls | Cleared |
| C-07 | Give literature, planning, proof, verification, and adjudication explicit threads | Complete mocked workflow snapshot and role-specific thread assertions | Cleared |
| C-08 | Start each verifier fresh, read-only, and offline | Runtime request policy, external identity, workflow, and decision tests | Cleared |
| C-09 | Restrict literature and citation network access | Runtime-model and adapter wire-policy tests | Cleared |
| C-10 | Stream typed lifecycle, usage, item, error, and terminal events | SDK, App Server, exec, public runtime contract, and SSE replay tests | Cleared |
| C-11 | Parse model control outputs through Pydantic and JSON Schema | Strict output-schema tests and malformed-output fail-closed workflow tests | Cleared |
| C-12 | Avoid approval and sandbox bypass flags | Config validation, argv checks, no-legacy scans, and threat model | Cleared |

## State and orchestration

| ID | Requirement | Evidence | Status |
| --- | --- | --- | --- |
| S-01 | Use SQLite as the deterministic source of run state | Reopen, transaction, monotonic event, snapshot, and database schema v1-to-v2 tests | Cleared |
| S-02 | Persist frozen input, config, runtime resolution, versions, and SHA-256 provenance | Store integrity, runtime resolution, export manifest, and tamper tests | Cleared |
| S-03 | Fence workers across crash and resume | Lease expiry, stale token, durable command, dual-worker, and terminal-ownership tests | Cleared |
| S-04 | Make start, cancel, and resume idempotent | Durable receipt replay, duplicate command, queued cancel, and active interruption tests | Cleared |
| S-05 | Enforce proof, plan, strategy, retry, token, search, stage-time, and run-time budgets | Transactional counter and persistent workflow exhaustion tests | Cleared |
| S-06 | Keep sealed candidates and verification reports immutable | SQLite triggers and public mutation tests across resume | Cleared |
| S-07 | Compute PASS from immutable structured reports in code | Decision mutation, evidence coverage, thread identity, workflow, and export gates | Cleared |
| S-08 | Bind plans, evidence, candidates, reports, adjudications, findings, and decisions with typed references | Store guards, schema tests, reopen reads, and manifest assertions | Cleared |
| S-09 | Run configured candidates and verifiers with bounded concurrency | Overlap, configured-limit, sibling cancellation, service-capacity, and failure tests | Cleared |
| S-10 | Export deterministic proof, report, and manifest files | Determinism, hash, tamper, concurrent writer, staging crash, and terminal rebuild tests | Cleared |
| S-11 | Complete mocked runs through start, stream, stop, resume, seal, verify, decide, and export | Workflow and CLI end-to-end tests with artifact inspection | Cleared |

## Product interface

| ID | Requirement | Evidence | Status |
| --- | --- | --- | --- |
| U-01 | Serve a typed FastAPI backend and React/Vite/TypeScript client | API tests, strict typecheck, unit tests, and production build | Cleared |
| U-02 | Replay live events through SSE | Service and API `Last-Event-ID`, reconnect, terminal-close, and disconnect tests | Cleared |
| U-03 | Edit problem, guidance, verification rules, complete config, and budgets | Browser create/start test with full request snapshot | Cleared |
| U-04 | Show stage and agent state, timeline, token/time metrics, candidates, evidence, findings, artifacts, resume, and cancel | Component inspection, completed-run tests, and desktop/narrow Playwright checks | Cleared |
| U-05 | Provide loading, empty, failed, disconnected, cancelled, and completed states | Loading and snapshot shells, empty views, safe error banner, reconnect state, status-derived actions, and completed-run tests | Cleared |
| U-06 | Apply Impeccable init, shape, craft, critique, audit, harden, adapt, and polish | PRODUCT/DESIGN contracts, checked-in hook and detector, responsive tests, clean detector, and the [phase record](impeccable-phases.md) | Cleared |
| U-07 | Run the approved Impeccable detector in CI | Pinned local detector command, workflow step, and clean output | Cleared |

## Packaging, operations, and documentation

| ID | Requirement | Evidence | Status |
| --- | --- | --- | --- |
| E-01 | Use uv with Python 3.13 and 3.14 support; remove Conda | Package range, uv lock/sync, `.python-version` 3.14.6, CI matrix, and production scan | Cleared |
| E-02 | Provide one-command setup plus CLI and web entry points | Frozen uv sync, `npm ci`, CLI help and mocked run tests, API serve docs, and Vite build | Cleared |
| E-03 | Emit structured logs without secrets | Secret-field, bearer-value, exception, and API error-envelope tests | Cleared |
| E-04 | Import legacy runs without mutating source data | Idempotency, symlink, contained-root, and tamper tests | Cleared |
| E-05 | Test state, schemas, security, resume, mutation verification, backend, frontend, and Playwright | CI jobs and the working-tree results below | Cleared |
| E-06 | Keep real-model smoke tests opt-in | Default marker exclusion plus one collected, guarded exact-model/schema-turn smoke test; the authenticated call was not run | Cleared; smoke not run |
| E-07 | Document README, architecture, migration, threat model, config, operations, CI, and contribution flow | Relative-link, command, schema, and style scans | Cleared |
| E-08 | Work on a branch with logical commits and no push | `codex-native-rewrite`, the local logical commit sequence, and clean-candidate replay; no push was made | Cleared |

## Verification evidence

These commands ran against the current candidate on 2026-07-17:

| Command | Result |
| --- | --- |
| `uv run ruff check .` | Passed |
| `uv run mypy src` | Passed |
| `uv run pytest` | 229 passed, 1 opt-in real Codex test deselected |
| `uv build` | Source distribution and wheel built |
| `npm run lint` | Passed |
| `npm run typecheck` | Passed |
| `npm test` | 3 files and 10 tests passed |
| `npm run build` | Passed |
| `npm run impeccable` | Passed with no anti-pattern findings |
| `npm run test:impeccable` | 103 passed |
| `npm run test:e2e` | 5 passed, 3 expected skips |

The `real_codex` smoke test is present and collectable, but no real Codex call
was made. Structured-output compatibility for the current account, runtime,
`gpt-5.6-sol`, and `low` effort therefore remains a release-time operational
risk. The test remains opt-in because it consumes credentials, network access,
model quota, time, and potentially billable usage. GitHub Actions has not run
for this local, unpushed candidate; the SHA-pinned workflow will exercise
Python 3.13 and 3.14 after a maintainer pushes it.

## Release checks outside this candidate

Before release, the maintainer can:

1. inspect the first GitHub Actions run, including both Python matrix jobs and
   the tokenless-browser source check;
2. decide whether to run an authenticated `real_codex` smoke test under the
   documented cost and credential boundary.
