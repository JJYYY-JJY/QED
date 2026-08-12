# Security Review: QED

## Scope

Working-tree diff against the alpha candidate HEAD, with changed-file security review and directly supporting runtime/security paths.

- Scan mode: working_tree
- Target kind: git_diff
- Target ID: target_sha256_192a804361b55a71e1da32163a5310fcf01116f9c4b58827fb9f273253013066
- Revision range: df40a9683004ab58fb38497d1aecfc8f1fffa288...df40a9683004ab58fb38497d1aecfc8f1fffa288
- Snapshot digest: codex-security-snapshot/v1:sha256:bcece120c01f5cf5740d08cf93f1f8fb9acd9521933288a51794205f86610bf5
- Inventory strategy: diff
- Included paths: .
- Excluded paths: none
- Runtime or test status: Parent-agent fallback; official Codex Security scan has no delegated worker capability in this session.
- Artifacts reviewed: artifacts/01_context/security_guidance.md, artifacts/01_context/threat_model.md, artifacts/02_discovery/deep_review_input.jsonl, artifacts/02_discovery/work_ledger.jsonl, artifacts/02_discovery/finding_discovery_report.md
- Scan context: Codex-only QED runtime; fail closed on provider, credential, filesystem, network, protocol, and bundle-integrity boundaries.

Limitations and exclusions:
- Delegated workers were unavailable; this scan does not claim independent worker/model diversity.
- The current parent review found no additional distinct candidate, but the prior partial baseline scan remains a separate release evidence blocker until closeout is accepted.
- Excluded .venv/\*\*: Dependency environment is not a shipped source surface.
- Excluded node_modules/\*\*: Installed dependency tree is checked through lockfile and npm audit, not diff-file review.

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 0 |
| Severity mix | none |
| Confidence mix | none |
| Coverage | partial |
| Validation mode | diff-scoped source review with regression-test and secret/export scan cross-checks |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

QED protects frozen mathematical inputs, immutable research records, Codex credentials and thread lineage, SQLite state transitions, managed workspaces, citation network boundaries, and content-addressed export integrity.

### Assets

- frozen problem and proof records
- Codex credentials and dedicated CODEX_HOME
- SQLite state, leases, fencing, and audit events
- server-owned workspaces and export bundles
- model/provider/runtime provenance

### Trust Boundaries

- operator/API to application service
- application service to SQLite
- application runtime to official Codex adapter
- citation fetcher to untrusted literature bytes
- export producer to offline bundle verifier
- frontend to loopback HTTP/SSE API

### Attacker Capabilities

- submit malformed API or protocol data
- control untrusted citation URLs/content
- attempt path/link/hard-link traversal
- replay stale workers or idempotency keys
- supply prompt-injection text in evidence or candidate content

### Security Objectives

- fail closed on unknown identity, terminal, schema, provider, or policy state
- prevent credential and workspace boundary escape
- preserve immutable audit lineage and event ordering
- keep model-reported data below server-captured authority

### Assumptions

- remote deployment is unsupported and non-loopback bind is rejected
- official Codex credentials are supplied only through a dedicated operator-controlled home
- offline verification must work without runtime or network access

## Findings

### No findings

No reportable findings survived the canonical discovery, validation, and reportability gates.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| 104 changed-file worklist rows | changed source, configuration, runtime, filesystem, network, export, and release surfaces | No issue found | Every row has a full-file receipt. 26 production/config rows were reviewed for security candidates; 78 deleted historical, documentation, or test rows were explicitly not applicable. Evidence: artifacts/02_discovery/work_ledger.jsonl |
| Prior eight source-backed baseline findings | release-level security closeout | Needs follow-up | Regression tests and code changes address the eight prior findings, but the prior scan was partial and its final accepted closeout is not replaced by this parent fallback scan. Evidence: artifacts/01_context/threat_model.md |

## Open Questions And Follow Up

- Can an operator run the same diff scan with delegated Codex Security workers and complete the prior baseline finding closeout in a release window?
  - Follow-up prompt: Resume scan cc2e7d79-c9f2-42e7-84ae-58c22896d567 with delegated worker capability and attach the accepted treatment for scan 61393cb5-6ff6-4961-b53f-91e24926455a.
- No delegated worker capability was available in the active Codex session; parent-agent review completed the worklist but independent worker receipts are unavailable.
  - Follow-up prompt: Review deferred unit delegated-worker-independent-review and close its stated proof gap. Surfaces: changed-file-worklist.
- The previous partial scan requires a separate accepted closeout artifact with owner/date treatment for every Medium finding.
  - Follow-up prompt: Review deferred unit baseline-security-acceptance and close its stated proof gap. Surfaces: prior-baseline-finding-closeout.
