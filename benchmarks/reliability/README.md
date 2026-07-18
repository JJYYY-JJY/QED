# QED reliability benchmark

This directory contains the frozen v2 alpha case set, schemas, lock, runner, and
credential-free demo results. Run:

```bash
uv run python benchmarks/reliability/run.py validate
uv run python benchmarks/reliability/run.py --help
```

Prepare repeated blinded requests, then run the opt-in QED verifier adapter with
one dedicated data root:

```bash
uv run python benchmarks/reliability/run.py prepare \
  --repetitions 3 --output /tmp/qed-benchmark-requests.jsonl
QED_RUN_REAL_RELIABILITY_BENCHMARK=1 \
  uv run python benchmarks/reliability/qed_adapter.py \
  --requests /tmp/qed-benchmark-requests.jsonl \
  --output /tmp/qed-benchmark-results.jsonl \
  --data-root /tmp/qed-benchmark-data \
  --backend sdk
uv run python benchmarks/reliability/run.py summarize \
  --results /tmp/qed-benchmark-results.jsonl \
  --expected-repetitions 3 --output-dir /tmp/qed-benchmark-summary
```

Repeat the adapter command with a different empty data root and
`--backend app-server` for that transport. The adapter seeds the exact benchmark
candidate as an immutable fixture with a visibly synthetic prover identity,
then uses QED's normal fresh structural, detailed, and citation verifier
lifecycle, decision code, event chain, budgets, and dedicated `CODEX_HOME`.
It does not pretend that Codex authored the seeded candidate.

Read [`docs/reliability-benchmark.md`](../../docs/reliability-benchmark.md) for
the execution-adapter contract, repeated-run workflow, metric definitions, and
reporting limits. The demo fixture tests aggregation; it is not model output.
