# QED v2 stable candidate roadmap

## Baseline

This roadmap records the baseline before implementation on branch
`codex-native-rewrite`.

- HEAD: `df40a9683004ab58fb38497d1aecfc8f1fffa288`
- Commit: `Harden QED v2 alpha invariants`
- Worktree: clean; `git diff --check` passed.
- `uv.lock` SHA-256: `02bb3133e83e7cda792adde315cc9f25c8b39f878b314c903d9f9bb581db5af8`
- `package-lock.json` SHA-256: `99f2f7e3740526c6cc0357c09a2140ae836813a8488284fb19ca58f9f6dceee2`
- Python: `279 passed, 4 deselected`; Ruff, mypy, frontend lint, typecheck,
  unit tests, build, and `uv build` passed.
- Python branch coverage was 83%, below the stable thresholds.
- Playwright E2E did not reach assertions because the local Chromium binary was
  not installed.
- Impeccable tests/detector failed on macOS `/proc/self/fd` limitations; the
  alpha-only dependency is removed in this roadmap rather than repaired.
- The fixture-only reliability demonstration reported false-PASS `2/17`,
  semantic mutation detection `3/4`, and citation precision `1/2`. These are
  not real Codex release metrics.
- Four opt-in real-Codex tests were skipped because dedicated credentials,
  `CODEX_HOME`, and data-root variables were not supplied.

The standard Codex Security scan `61393cb5-6ff6-4961-b53f-91e24926455a`
found eight source-backed findings: five medium and three low. Its artifacts
were hashed as follows:

| Artifact | SHA-256 |
| --- | --- |
| scan-manifest.json | `8fb6d95823f07b7dba7af9014a74500c1fd5b27a1d1cd95f1f105deeee50a23d` |
| findings.json | `f74f7382671033ff366d7b61744d195449f6a78b990c958b49b98b319ef9287c` |
| coverage.json | `d9e12a3ee0f5a59226c83531af31f77b9c2b239f16c5e9656700d7e558b21bf1` |
| report.md | `b69a0ef983f2560db65ee93851096aa5fea7dea0b32f5633022bc2c92518fa5c` |

The scan coverage was partial and is evidence of baseline risk, not a stable
security approval.

## Release gaps and blockers

The implementation pass addressed the alpha gaps with bounded state/runtime,
provenance, security, verification, export, doctor, packaging, and evidence
contracts. The following blockers remain evidence-backed and are intentionally
not converted into a stable approval:

1. Core coverage now passes its fixed target at 98.10% line / 93.99% branch,
   but repository coverage remains below the fixed target at 88.19% line /
   72.30% branch (repository target: line >=90%, branch >=85%).
2. The current mutation run killed 1,232 of 2,138 executable mutants (57.62%),
   below the required 90% core gate.
3. The official working-tree Codex Security diff review found no reportable
   findings and produced 104/104 file receipts, but its coverage is partial
   because delegated workers were unavailable; the candidate closeout map now
   records owner/date/evidence for all eight findings, but independent release-
   window acceptance is still required.
4. Real Codex lifecycle, 300/100/100 reliability windows, sealed holdout,
   cross-platform CI, 20-run stability, and operator-managed `qed doctor`
   checks remain unrun or blocked without dedicated credentials, quota,
   `CODEX_HOME`, and holdout input.
5. Citation network access remains disabled until the restricted fetcher is
   attested by the runtime; optional Ed25519 signing is not enabled.

## Implementation order

### Stage 1: contracts and durable boundaries

- Add strict runtime provenance, verifier-role, claim-graph, bundle-result, and
  evidence-gate schemas.
- Make the state-machine command table authoritative and verify its generated
  transition artifact.
- Extract pure verification policy and decision helpers while retaining the
  existing public `RunStore` and `ResearchWorkflow` entry points.
- Add property tests for transition legality, event ordering, idempotency,
  fencing, cancellation, resume, and budget durability.

### Stage 2: Codex runtime and isolation

- Reject mock runtime in production CLI/API/service construction.
- Bind live executable selection to the package-managed Codex binary and record
  executable/package/catalog hashes.
- Enforce exact OpenAI provider/model/backend/version/effort provenance and
  fail closed on silent substitution.
- Route App Server notifications by thread/turn with bounded queues and reject
  oversized or unknown critical protocol messages.
- Harden dedicated `CODEX_HOME`, attempt workspaces, verifier read-only roots,
  environment scrubbing, and runtime role network policy.

### Stage 3: mathematical decision and evidence

- Add immutable proof-obligation graphs with byte-checked spans, dependency
  cycle checks, stable rule IDs, and verifier-role coverage.
- Require structural, detailed, assumptions/quantifiers,
  counterexample/edge-case, reconstruction, and conditional citation reports
  using N-of-N application-code policy.
- Add offline `qed verify-bundle`, hash/signature-state checks, canonical JSON
  validation, event-chain verification, and export-intent checks.

### Stage 4: security, migration, and reliability

- Implement deterministic restricted citation fetching or fail closed when the
  current runtime cannot attest the policy.
- Fix the eight baseline security findings and add regression/property/fuzz
  cases for URL, path, protocol, manifest, migration, and provenance boundaries.
- Validate the checked-in schema-v1-v5 fixture matrix, backup/restore/
  failed-upgrade recovery, deterministic fault injection, lease/concurrency
  stress, and SSE quotas.
- Implement environment and live capability `qed doctor`.

### Stage 5: stable packaging and evidence

- Remove Impeccable scripts, hooks, skill files, dependency, CI steps, and
  current normative references while preserving historical records.
- Extend the locked benchmark suite and sealed holdout harness without changing
  alpha expected labels or counting fixtures as real runs.
- Add strict stable evidence schema, generated scorecard, release checklist,
  CI coverage/mutation/platform gates, and an offline-verified golden bundle.
- Run all local gates, install Playwright browsers for E2E, then run opt-in real
  Codex lifecycle and reliability windows when dedicated credentials exist.

## Gate rule

Every gate records its command, UTC time, commit, environment, result,
artifact hash, and limitation in `v2-stable-evidence.json`. A dimension is
eligible for 10/10 only when every required gate is `passed`; `false`, missing,
unknown, blocked, or unrun gates can never be promoted to 10/10. Real Codex
results, costs, credentials, holdout outcomes, and confidence intervals are
never synthesized from fixtures.
