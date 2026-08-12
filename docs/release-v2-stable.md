# QED v2 stable-candidate release boundary

This document is a release checklist, not a score declaration. The current
working tree is a stable candidate under implementation. A dimension is 10/10
only when every required gate in
[`v2-stable-evidence.json`](research/v2-stable-evidence.json) is `passed` and
`qed validate-evidence` recomputes eligibility as `true`. The current evidence
contains blocked/unrun gates for real Codex credentials, sealed holdout, and
other release work; those blockers are intentional and remain visible.

## Release invariants

- Production AI access is official OpenAI Codex only. Fixture runtime output is
  test evidence and never a real reliability result.
- SQLite is the durable authority for transitions, event sequence, attempts,
  budgets, idempotency, leases, fencing, and terminal state.
- Every required verifier uses a distinct fresh external thread and the same
  exact model ID. Fresh threads isolate conversation state; they do not create
  independent model weights.
- Application code computes QED policy PASS from immutable structured reports,
  claim/rule coverage, lineage, and runtime provenance. Adjudicator prose has no
  authority to upgrade a failure.
- `qed verify-bundle` is offline and checks integrity, schema, lineage, event
  chain, claim graph, export intent, and the recomputed code decision.
- Non-loopback binds fail closed. No personal Codex home, auth file, ambient
  Codex/API key, full-access sandbox, or approval bypass is accepted.
- The public 19-case alpha pack remains locked. The v2 development pack and an
  operator-supplied sealed holdout are versioned by content hash.
- QED policy PASS is not formal verification, mathematical truth, peer review,
  a trusted timestamp, or a signature.

## Required release checklist

Each row must be recorded in the machine-readable evidence file with the exact
command, UTC date, commit, environment, result, artifact path and SHA-256, and
limitation. `blocked`, `unrun`, `unknown`, or `failed` is never promoted to
`passed`.

| Gate family | Required checks | Current evidence |
| --- | --- | --- |
| Architecture | typed state table, illegal-transition/property tests, bounded dependency scan, API/idempotency/SSE contract, core and repository coverage, mutation score | Local implementation and tests are present; coverage/mutation are not yet release-clean |
| Runtime | exact OpenAI model/provider/backend/version/effort, executable/catalog/config/prompt/schema hashes, SDK/App Server lifecycle, fresh-thread identity | Fixture tests pass; real lifecycle canary is opt-in and currently unrun |
| Security | eight baseline findings, path/link/TOCTOU tests, restricted citation policy, protocol frame limits, SSE quotas, secret/export scan, no Critical/High findings | Candidate closeout map records owner/date/evidence for all eight findings; official diff scan `cc2e7d79-c9f2-42e7-84ae-58c22896d567` found 0 reportable findings with 104/104 receipts, but coverage is partial and independent release-window acceptance remains blocked |
| Mathematics | five required verifier roles plus citation, immutable proof-obligation graph, N-of-N policy, rule/claim coverage, blinded public and sealed holdout harness | Code gate is fail-closed; real 300/100/100 reliability window is blocked |
| Persistence | schema v1-v5 preflight, staged upgrade, backup/restore, failed-upgrade recovery, lease and crash/concurrency matrix | Migration and focused tests exist; full fault/concurrency release window remains required |
| Product/operations | `qed doctor`, loopback boundary, cancel/resume/unknown terminal docs, backup/restore/upgrade, frontend status vocabulary | Doctor is fail-closed and recorded unknown checks for this uninitialized local data root; remote deployment remains unsupported by design |
| Packaging/CI | frozen uv/npm installs, lint/typecheck/tests/build, Playwright, offline golden bundle, benchmark lock, forbidden-provider and secret scan, platform matrix, 20-run stability | Local frontend gate and browser assertions passed; platform matrix and 20-run evidence are not yet recorded |

## Commands and evidence format

The canonical local commands are:

```bash
uv sync --all-groups --frozen
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen pytest --cov=qed --cov-branch
uv build
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npx playwright install --with-deps chromium
npm run test:e2e
npm audit --audit-level=high
uv run --frozen qed verify-bundle artifacts/golden-bundle --json
uv run --frozen qed validate-evidence docs/research/v2-stable-evidence.json
git diff --check
```

The checked-in deterministic golden bundle is a `benchmark_fixture`. Its
offline verification proves the verifier and export contract only; it is never
included in real model reliability denominators. The reliability command must
retain normalized raw rows, pack hashes, exact model/runtime/backend/prompt and
schema hashes, repetition, usage, duration, exclusions, and failure class.

## External release blockers

The current report must remain below stable release until an operator supplies
and validates a dedicated server-owned `CODEX_HOME`, authentication, quota,
exact backend/model, and a sealed holdout pack. The required real execution
window is at least 300 expected-NON_PASS runs with zero false PASS, 100 known-
true correct-proof runs with the stated confidence bound, 100 citation
judgments with 100% precision, complete semantic mutation detection, and an
infrastructure-healthy UNCERTAIN rate no higher than 5%. No fixture, mock, or
model-reported claim can fill those denominators.

Optional Ed25519 manifest signing is separate from hash verification and is not
enabled by this candidate. An unsigned bundle must continue to verify its hashes
and report `signature_status: unsigned`; it must not be described as signed or
timestamped.
