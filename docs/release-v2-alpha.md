# QED v2 alpha release boundary

The `codex-native-rewrite` branch is a QED v2 alpha candidate. It can enter
formal pull-request review after the checklist below has evidence. It does not
replace `main` as a stable release by declaration.

## Alpha trust boundary

The alpha keeps these release-blocking invariants:

- application code derives QED policy PASS from frozen structured reports;
- SQLite owns state transitions, event sequence numbers, leases, and fencing;
- sealed candidates, verifier reports, adjudications, and operator decisions
  remain immutable;
- verifier threads start fresh and use external Codex thread identities distinct
  from the prover and other required verifiers;
- a dedicated `CODEX_HOME` separates QED runtime state from personal Codex state;
- missing identity, ambiguous start, missing terminal, stale ownership, exhausted
  budget, and unknown terminal state fail closed;
- exports bind research records and the ordered event audit chain by hash.

QED policy PASS means that a candidate passed the configured code gates and
thread-isolated LLM checks. It does not mean peer review, formal verification,
Lean verification, or guaranteed mathematical truth. A fresh thread isolates
conversation state; it does not supply independent model weights. SHA-256 binds
bytes and record identity; it is not a signature or trusted timestamp.

## Pull-request checklist

Record the command, date, commit, and output for each completed item. Leave an
item open when credentials, CI access, or another external dependency prevents
the check. The pre-closeout
`docs/research/completion-matrix.md` remains an unchanged historical record; its
test counts are not evidence for this candidate.

### Code and data invariants

- [ ] Prover/verifier and verifier/verifier external thread reuse fails closed,
      including migrated database conflicts.
- [ ] Every frozen user verification rule has an application-assigned stable ID,
      structured PASS coverage, and export-visible report/check lineage.
- [ ] Unknown rule IDs, missing rule coverage, `FAIL`, and `UNCERTAIN` prevent
      QED policy PASS.
- [ ] Production CLI and API construction require a Codex runtime selection;
      mock provenance cannot look like a production run.
- [ ] Candidate/report immutability, execution fencing, lease expiry, retry and
      budget durability, cancellation, late terminals, and unknown terminals
      retain regression coverage.
- [ ] Evidence records distinguish `runtime_observed` source actions from
      `model_reported` source metadata and content. The export states that the
      current runtime provides no `server_captured` source content; URI,
      observation payload, and submitted content hashes do not raise trust.
- [ ] Citation PASS requires structured, byte-checked proof-span-to-evidence-
      excerpt bindings; bare evidence IDs and free-text citation claims fail.
- [ ] An export-intent manifest records `running/export` until SQLite observes
      artifact registration and the terminal transitions; a precommit bundle
      never claims completion.
- [ ] Legacy file runs remain `legacy_untrusted` and cannot acquire current PASS
      authority through import or schema migration.

### Verification evidence

- [ ] `uv sync --all-groups --frozen`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy src`
- [ ] `uv run pytest`
- [ ] `uv build`
- [ ] `npm ci`
- [ ] `npm run lint`
- [ ] `npm run typecheck`
- [ ] `npm test`
- [ ] `npm run test:impeccable` while the alpha development tool remains
- [ ] `npm run build`
- [ ] `npm run impeccable` while the alpha development tool remains
- [ ] `npm run test:e2e`
- [ ] The opt-in real-Codex lifecycle passes through the SDK and App Server
      adapters using a dedicated `CODEX_HOME` and data root.
- [ ] Any exec-backend smoke runs only after an explicit opt-in.
- [ ] The real-Codex record includes recomputed proof, report, manifest,
      event-chain, and related-record hashes plus external thread identities.
- [ ] At least one non-fixture reliability benchmark run archives its model,
      configuration, locked sample set, run window, repetitions, raw usage, and
      limitations.
- [ ] The opt-in verifier benchmark adapter is run with separate dedicated data
      roots for SDK and App Server; seeded candidates retain their visible
      synthetic-author provenance.

### Review and release notes

- [ ] Review the migration against a backup copy of each supported SQLite schema.
- [ ] Review the complete diff for unrelated archive, attribution, prompt, or
      `proved_statements/` changes.
- [ ] Confirm production Python, frontend, build, and export paths contain no
      Impeccable dependency.
- [ ] Describe open alpha defects and unrun checks without converting them into
      passed claims.
- [ ] Obtain security, runtime, persistence, mathematical-policy, frontend, and
      operations review.

## Known alpha engineering debt

The closeout extracted the declared state machines, database migration
preflight, exceptional-run operations, and runtime-event helpers into dedicated
modules. `store.py` and `workflow.py` still combine several invariant domains;
the suggested lease/research-record/read-model and
orchestration/supervision/cancellation split remains P2 follow-up work. Do that
as behavior-preserving changes against the current transition and fault-
injection tests, not as part of another policy fix.

Role-specific models are also design-only in this alpha. The global model
remains the only implemented configuration contract; see
[Role-specific model design](role-specific-models.md).

## Legacy v1 retention playbook

Preserve the pre-v2 `main` commit before merging v2. A release manager should
perform these steps in a clean clone after approval. This task does not create
or push either reference.

```bash
git fetch origin --tags
legacy_sha="$(git rev-parse origin/main)"
git show --no-patch --format=fuller "$legacy_sha"
git branch archive/qed-v1 "$legacy_sha"
git tag -a qed-v1-legacy "$legacy_sha" \
  -m "Last pre-v2 QED revision; retained for historical access"
git push origin archive/qed-v1
git push origin qed-v1-legacy
```

Record `legacy_sha` in the v2 merge or release notes before pushing. Stop if
`origin/main` already contains v2 or either reference resolves to another
commit. Repository policy may choose the branch, tag, or both. Do not rewrite a
published reference.

The retained revision stays historical. Running or importing one of its
file-based outputs does not grant v2 PASS authority. Follow
[Migration](migration.md) to preserve bytes and start a new v2 research run.

## v2 stable exit work

Lean proof checking remains a future route. Alpha and stable do not require
Lean. Add Lean only with a reviewed proof format, runtime boundary, migration,
and end-to-end tests; do not add placeholder interfaces to satisfy this
checklist.

Impeccable serves as temporary development tooling during alpha. Remove it
before v2 stable:

- [ ] Delete `.agents/skills/impeccable/`, including agents, references, scripts,
      detector engines, live-session code, vendored assets, and tests.
- [ ] Remove the Impeccable `PostToolUse` entry from `.codex/hooks.json`; delete
      the file if no QED-owned hook remains.
- [ ] Delete `.impeccable/` local state and remove its entries from `.gitignore`
      and maintainer-local Git excludes.
- [ ] Remove `test:impeccable` and `impeccable` from `package.json`.
- [ ] Remove dependencies used only by that tooling, including `pngjs` if no
      product code has adopted it, then refresh `package-lock.json` with npm.
- [ ] Remove the Impeccable regression and detector steps from
      `.github/workflows/ci.yml`.
- [ ] Delete `frontend/tests/impeccable-live.spec.ts` and every test under the
      Impeccable skill bundle.
- [ ] Remove current-tool commands and release requirements from `README.md`,
      `CONTRIBUTING.md`, `AGENTS.md`, `docs/research/ci-toolchain.md`, and
      `docs/research/completion-matrix.md`.
- [ ] Delete `docs/research/impeccable-phases.md` after applying the repository's
      historical-artifact retention policy. Keep an archived copy only when that
      policy requires it, and label it as a non-runtime development record.
- [ ] Remove Impeccable assets, actors, threats, assumptions, exclusions, and
      release gates from `docs/threat-model.md`.
- [ ] Review historical audit documents for normative Impeccable instructions.
      Preserve required research history, but remove links that treat the tool
      as a current stable dependency.
- [ ] Run `rg -n -i 'impeccable|\.impeccable'` over tracked stable-release files
      and classify each remaining historical occurrence.
- [ ] Re-run Python, frontend, package, and Playwright gates after deletion.

The cleanup must preserve QED's Codex runtime and research controls:

- `.codex/config.toml` developer settings that do not invoke Impeccable;
- `.agents/skills/qed-literature/` and `.agents/skills/qed-proof-review/`;
- `qed.runtime.CodexRuntime`, dedicated `CODEX_HOME`, pinned OpenAI dependencies,
  sandbox policy, thread isolation, and evidence-network restrictions;
- QED security tests, threat-model controls, event audit chain, export hashing,
  and the historical mathematical research archive.

Stable release review should reject a cleanup patch that weakens those controls
or removes them because they share a directory with the temporary tool.
