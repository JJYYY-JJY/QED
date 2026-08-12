# Reliability benchmark

> The alpha pack and fixture examples below are preserved as historical,
> non-release evidence. The current stable-candidate contract is described in
> the v2 section at the end of this document.

QED v2 alpha includes a frozen false-PASS benchmark under
[`benchmarks/reliability/`](../benchmarks/reliability/). The benchmark measures
one configured QED runtime against known cases. It does not certify the runtime,
the model, or a mathematical result.

## Case set

The 19 alpha cases cover:

- a known true statement with a correct proof and a known false statement;
- a missing assumption, swapped quantifiers, and a domain error;
- a correct conclusion with an invalid proof, circular reasoning, and an
  unproved key lemma;
- an invalid limit exchange, a reversed inequality, and an omitted boundary
  case;
- four proof-content mutations: deleting a key step, swapping a quantifier,
  changing a symbol, and replacing an assumption;
- a supported citation, a contradicted citation, an unrelated source, and
  fabricated source metadata.

The mutation cases change mathematical content. A case-file or hash-tampering
test checks integrity and does not count as a semantic mutation.

[`case.schema.json`](../benchmarks/reliability/case.schema.json) fixes the case
shape and rejects extra fields.
[`cases.lock.json`](../benchmarks/reliability/cases.lock.json) binds the schema
bytes, JSONL bytes, case count, and canonical SHA-256 for each case. The runner
refuses a changed schema or case file until a reviewer updates the lock as part
of an intentional benchmark revision. SHA-256 provides content addressing. It
does not provide an author signature or trusted timestamp.

Citation excerpts in the case set use `benchmark_fixture` provenance. Their
excerpt hashes prove which fixture bytes the benchmark used. They do not prove
that QED fetched the URI or that the source supports a claim outside the frozen
excerpt.

## Commands

Validate the checked-in schema, lock, and cases without credentials:

```bash
uv run python benchmarks/reliability/run.py validate
```

Generate blinded execution requests for five repetitions:

```bash
uv run python benchmarks/reliability/run.py prepare \
  --repetitions 5 \
  --output /tmp/qed-reliability-requests.jsonl
```

`prepare` includes the problem, candidate proof, verification rules, registered
citation evidence, case hash, and repetition. It omits the expected verdict,
category, tags, and mutation label. Each row matches
[`request.schema.json`](../benchmarks/reliability/request.schema.json).

The opt-in
[`qed_adapter.py`](../benchmarks/reliability/qed_adapter.py) consumes those
requests and writes one result matching
[`result.schema.json`](../benchmarks/reliability/result.schema.json):

```bash
QED_RUN_REAL_RELIABILITY_BENCHMARK=1 \
  uv run python benchmarks/reliability/qed_adapter.py \
  --requests /tmp/qed-reliability-requests.jsonl \
  --output /tmp/qed-reliability-results.jsonl \
  --data-root /tmp/qed-reliability-sdk \
  --backend sdk
```

Use a separate empty data root and `--backend app-server` for that adapter. The
script never reads personal `~/.codex`: the chosen data root owns its SQLite
database, exports, and dedicated `CODEX_HOME`.

This is a verifier reliability harness, not a claim that Codex authored the
case proofs. It seeds each exact candidate as an immutable benchmark fixture
with a synthetic, export-visible prover identity. A typed fixture plan and
evidence ledger take the run to verification; QED then uses its normal fresh
structural, detailed, and citation verifier turns, application decision,
budgets, event chain, adjudication, and export behavior. The separate opt-in
real-Codex lifecycle test covers model-driven literature, planning, and
multiple proof generation. Keeping those questions separate prevents the
benchmark from silently replacing the candidate bytes it is meant to test.

Summarize a complete result grid:

```bash
uv run python benchmarks/reliability/run.py summarize \
  --results /tmp/qed-reliability-results.jsonl \
  --expected-repetitions 5 \
  --output-dir /tmp/qed-reliability-summary
```

The command writes:

- `raw-results.jsonl`, with a canonical hash on every normalized run result;
- `summary.json`, with metrics, verdict counts, usage totals, per-case variance,
  and hashes for the input results and normalized output.

The summarizer rejects an unknown case, a wrong case hash, duplicate run
identity, missing repetition, extra repetition, malformed usage, or ambiguous
runtime provenance. An unresolved or incomplete required verifier set yields
`UNCERTAIN`, never PASS. The dedicated data root retains the run diagnosis and
event chain; operators must inspect it and disclose infrastructure failures
rather than interpreting UNCERTAIN as a mathematical finding.

## Result contract

Every non-fixture result records:

- backend, model, model version, adapter, and configuration SHA-256;
- UTC start and finish timestamps;
- input, cached-input, output, and reasoning-output tokens;
- search-query count and execution seconds;
- cost amount, currency, and whether the provider or operator supplied it.

Use `cost_source: "unavailable"` with null amount and currency when the runtime
does not expose cost. Do not infer a price from a mutable public price table
inside the benchmark runner. Archive the pricing inputs beside a report if an
operator computes cost after the run.

Set `runtime.fixture` to `false` for a model run. Such a result must name a
backend and model. Fixture rows must use `backend: "none"`, `model: "none"`,
zero token/query/time usage, and unavailable cost.

## Metrics

| Metric | Numerator | Denominator |
| --- | --- | --- |
| false-PASS rate | Expected `NON_PASS` cases reported as `PASS` | All expected `NON_PASS` runs |
| true-proof acceptance rate | `known_true_correct_proof` runs reported as `PASS` | All `known_true_correct_proof` runs |
| semantic mutation detection rate | Mutation runs reported as `FAIL` or `UNCERTAIN` | All semantic mutation runs |
| UNCERTAIN rate | Runs reported as `UNCERTAIN` | All runs |
| citation support precision | Expected-supported citations among results reported as `SUPPORTED` | Citation results reported as `SUPPORTED` |

The mutation metric counts `UNCERTAIN` as detection because QED fails closed and
does not grant PASS. The false-PASS population also contains mutation and bad
citation cases, so the metrics overlap.

`summary.json` stores a numerator, denominator, and value for each metric. A
zero denominator produces `null`, not a fabricated zero. Per-case verdict
counts show whether repeated runs varied.

## Checked-in demo fixture

[`fixtures/demo-results.jsonl`](../benchmarks/reliability/fixtures/demo-results.jsonl)
tests the runner without credentials:

```bash
uv run python benchmarks/reliability/run.py summarize \
  --results benchmarks/reliability/fixtures/demo-results.jsonl \
  --expected-repetitions 1 \
  --output-dir /tmp/qed-reliability-demo
```

The fixture metadata records adapter `checked-in-demo-fixture`, backend and
model `none`, model version `not-applicable`, configuration hash
`c907dd2162f78f39422ebf135fe52f0dda8eb6c0a01f1bcd9badac5de5d19f84`,
one repetition of the 19 locked cases, and a zero-duration timestamp on
2026-07-17. Its selected verdicts exercise every metric calculation, including
false PASS and UNCERTAIN. They are test data, not observed model output.

No authenticated model benchmark result is checked in. A publishable result
must retain the raw and summary files and state:

- QED commit and case-set hashes;
- exact model version, backend, runtime adapter, and configuration;
- repetition count and run window;
- raw token, query, elapsed-time, and cost fields;
- failed or excluded runs and the reason for each exclusion.

## v2 stable-candidate harness

`benchmarks/reliability/v2-stable-pack.json` records the versioned development
pack, domain/error coverage, case-file hash, lock hash, and required real-run
sample sizes. `build_v2_stable_pack.py` derives it from the unchanged 19-case
alpha pack plus explicitly versioned development cases; it never rewrites the
alpha expected labels. Validate it with:

```bash
uv run --frozen python benchmarks/reliability/run.py validate \
  --cases benchmarks/reliability/v2-stable-cases.jsonl \
  --lock benchmarks/reliability/v2-stable-cases.lock.json
```

`benchmarks/reliability/statistics.py` computes reproducible one-sided 95%
Wilson confidence bounds. Its tests cover the release examples `0/300` and
`95/100`; those examples are statistical checks, not observed Codex runs.

The stable result file is
[`docs/research/reliability-report-v2-stable.json`](research/reliability-report-v2-stable.json)
with normalized raw rows in
[`reliability-v2-stable-raw.jsonl`](research/reliability-v2-stable-raw.jsonl).
The checked-in report is currently `blocked` with zero real result rows. It
records the exact missing credential/quota/CODEX_HOME/holdout conditions and
explicitly excludes the alpha fixture rates. A real run must preserve the
operator-supplied holdout hash before execution and must not expose expected
labels to any verifier context.

## Limits

The alpha sample is small and hand-authored. Most proofs are elementary, and
several defect categories overlap. Public cases can enter model training data.
The benchmark measures one model/runtime/configuration combination during one
run window. Provider updates, nondeterminism, prompt changes, and evidence
availability can change later results. Report observed rates with counts and
configuration metadata; do not present them as a product guarantee or a bound
on mathematical error.
