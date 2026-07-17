# Configuration

QED stores one strict configuration snapshot with each run. The snapshot uses
Pydantic validation, rejects extra fields, and contributes its canonical
SHA-256 hash to the run record and export manifest.

The API accepts the complete configuration object inside `POST /api/v1/runs`.
The CLI `run` command exposes model, effort, backend, and input fields. The web
console submits the same complete object and exposes every mutable field in a
compact advanced section. The client fixes `schema_version`; fixed security
invariants appear as disabled controls, while the prover sandbox remains
selectable. QED has no free-form runtime override field and no YAML provider
file.

See [`examples/run-request.json`](../examples/run-request.json) for a complete
API request.

## Run configuration

### Runtime

| Field | Type and default | Behavior |
| --- | --- | --- |
| `schema_version` | integer, `1` | Selects the current configuration schema. |
| `model` | non-empty string, `gpt-5.6-sol` | Requires an exact match in the active Codex model catalog. |
| `effort` | non-empty string, `auto` | Accepts `auto` or an exact effort value from the model catalog. |
| `backend` | `auto`, `sdk`, `app-server`, or `exec`; default `auto` | Selects the typed runtime route. `auto` prefers the SDK and uses App Server for controls the SDK cannot represent. |

QED probes capabilities before stage work. It records the selected effort,
runtime version, multi-agent capability, and canonical resolution hash. Resume
probes again and rejects capability drift that would change the frozen
execution policy.

### Parallelism

| Field | Range | Default | Meaning |
| --- | ---: | ---: | --- |
| `parallelism.runs` | 1 to 32 | 1 | Maximum concurrent run workers allowed by this run and the active service set. |
| `parallelism.proof_candidates` | 1 to 64 | 4 | Maximum proof candidates in flight for one proving stage. |
| `parallelism.verifiers` | 1 to 64 | 2 | Maximum verification turns in flight. |
| `parallelism.proactive_multi_agent` | Boolean | `true` | Request proactive Codex delegation when the runtime advertises multi-agent support and the selected effort is `ultra`. The App Server's global thread cap remains outside per-run QED config. |

Application-level candidate and verifier tasks use bounded semaphores. Codex
subagents remain inside those application-owned stage and budget boundaries.

### Budgets

| Field | Range | Default | Meaning |
| --- | ---: | ---: | --- |
| `budgets.run_seconds` | at least 1 | 7200 | Total execution time across run segments. |
| `budgets.stage_seconds` | 1 through `run_seconds` | 1800 | Time ceiling for one durable stage, including retries. |
| `budgets.max_tokens` | at least 1 | 250000 | Cumulative input and output token ceiling. |
| `budgets.proof_attempts` | at least 1 | 8 | Durable proof-attempt reservations. A failed or interrupted model call consumes its reservation. |
| `budgets.plan_revisions` | at least 0 | 2 | Adjudication-directed plan revisions. |
| `budgets.strategy_rewrites` | at least 0 | 2 | Adjudication-directed literature and strategy rewrites. |
| `budgets.turn_retries` | 0 to 10 | 2 | Retry allowance for one frozen turn input. QED records each attempt before the runtime call. |

QED checks proof-attempt, plan-revision, strategy-rewrite, and turn-attempt
counters plus token, search, and execution-time usage against persisted state.
Restarting a process does not reset those values. `max_tokens` counts input plus
output tokens. The event log and export manifest also retain cached-input and
reasoning-output token values as separate runtime-reported breakdowns; those
breakdowns are not added again to the budget total.

### Search and network

| Field | Type and default | Behavior |
| --- | --- | --- |
| `search.enabled` | boolean, `true` | Enables approved search roles. |
| `search.allowed_roles` | unique list of `literature` and `citation`; both by default | Limits live web search to the named roles. |
| `search.max_queries_per_stage` | integer at least 1, default 20 | Caps completed web-search items for each stage. |

Literature and citation turns may use live web search and remain read-only. The
current workflow grants no command network. Runtime validation restricts any
future command-network policy to those two roles, rejects wildcard host
patterns, and keeps the sandbox read-only. Structural and detailed verification
stays offline. Planning, proof generation, and adjudication receive no search
or command network access.

### Sandbox and approval

| Field | Accepted value | Default |
| --- | --- | --- |
| `sandbox.literature` | `read-only` | `read-only` |
| `sandbox.planner` | `read-only` | `read-only` |
| `sandbox.prover` | `read-only` or `workspace-write` | `read-only` |
| `sandbox.verifier` | `read-only` | `read-only` |
| `sandbox.adjudicator` | `read-only` | `read-only` |
| `sandbox.approval` | `never` | `never` |

The API cannot select full access, an approval escape path, executable paths,
or raw Codex configuration overrides.

## Service settings

Service settings stay outside run snapshots because they control one process
and can contain a bearer secret.

| Setting | CLI or environment | Default | Constraint |
| --- | --- | --- | --- |
| Data root | `--data-root` | `.qed` | Holds SQLite, exports, and legacy imports. |
| Database filename | `QED_DATABASE_NAME` | `qed.sqlite3` | Plain filename without a path. |
| Listen host | `--host` | `127.0.0.1` | `localhost` or an IP address. |
| Listen port | `--port` | `8000` | 1 to 65535. |
| Bearer token | `QED_AUTH_TOKEN` or `--auth-token` | unset | At least 32 characters. A non-loopback bind requires it. |
| CORS origins | `QED_ALLOWED_ORIGINS` | local Vite origins | JSON array of full `http` or `https` origins. Wildcards, credentials, paths, queries, and fragments fail validation. |

Prefer `QED_AUTH_TOKEN` over `--auth-token` so process listings do not expose the
secret. QED redacts known secret fields and bearer values from structured logs.
Do not put credentials in run input, run configuration, guidance, or
verification rules.

The React console does not accept, store, or send this bearer token. Use it for
non-browser API clients or for the server-side hop from a
backend-for-frontend (BFF) to QED. A remote browser receives an HttpOnly,
Secure, SameSite session cookie from that external layer. JavaScript must not
receive the QED bearer value.

The repository includes [`.env.example`](../.env.example). QED does not load
that file. Export its values through your process supervisor or shell:

```bash
cp .env.example .env
chmod 600 .env
# Keep QED_AUTH_TOKEN commented for tokenless loopback browser use.
# For a protected API or BFF hop, generate and uncomment it first.
set -a
. ./.env
set +a
uv run qed serve --data-root .qed --host 127.0.0.1 --port 8000
```

Generate a token without an external package:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## API example

Start a loopback development server without authentication:

```bash
uv run qed serve --runtime codex
```

Create and start the sample run from another terminal:

```bash
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data @examples/run-request.json \
  http://127.0.0.1:8000/api/v1/runs

curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"schema_version":1,"idempotency_key":"sample-start-1"}' \
  http://127.0.0.1:8000/api/v1/runs/sample-odd-square/commands/start
```

Add `Authorization: Bearer <token>` to each request when the service has an auth
token. Reuse an idempotency key when retrying the same command. Use a new key for
a new command attempt.
