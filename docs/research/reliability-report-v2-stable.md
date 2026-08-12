# QED v2 stable reliability report

Status: **BLOCKED / UNRUN**

This report deliberately does not claim a stable mathematical-reliability score. The checked-in fixture adapter is excluded from every real-Codex denominator. Its alpha-only observations remain recorded as historical baseline: false PASS `2/17`, semantic mutation detection `3/4`, and citation precision `1/2`.

The versioned development pack contains 27 locked cases and validates with:

```text
uv run --frozen python benchmarks/reliability/run.py validate --cases benchmarks/reliability/v2-stable-cases.jsonl --lock benchmarks/reliability/v2-stable-cases.lock.json
```

Pack SHA-256: `52e175d7ca13be4a212afe4c73b64e087c387462daab12ebac9007a597eb3abc`  
Lock SHA-256: `99e62ecced5f66c2dd68993e2035e36830eeb23f5dd36371b438bdfd198944c8`

No real Codex execution was attempted. Dedicated credentials, quota, a server-owned `CODEX_HOME`, and an operator-supplied sealed holdout pack were not available. Therefore the required 300 expected-NON-PASS executions, 100 known-true executions, 100 citation judgments, 100% mutation detection, confidence bounds, real lifecycle conformance, and the infrastructure-healthy UNCERTAIN rate are all `unrun`, not PASS or FAIL.

Raw normalized blocker record: [reliability-v2-stable-raw.jsonl](reliability-v2-stable-raw.jsonl). The machine-readable source of truth is [reliability-report-v2-stable.json](reliability-report-v2-stable.json); its `unrun`/`blocked` statuses are release blockers.
