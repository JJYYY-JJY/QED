# Operations

QED runs as one FastAPI process with a local Codex runtime and a managed SQLite
data root. Use one durable data root per deployment. Place it on local storage
that supports file locks and `fsync`.

## Start the service

For a local research workstation:

```bash
uv sync --all-groups --frozen
uv run qed init --data-root .qed
mkdir -p .qed/codex-home
chmod 700 .qed/codex-home
QED_CODEX="$(uv run python -c \
  'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')"
CODEX_HOME="$PWD/.qed/codex-home" "$QED_CODEX" login
uv run qed --log-level info --log-format json serve \
  --data-root .qed \
  --host 127.0.0.1 \
  --port 8000 \
  --runtime codex
```

The live runtime uses the uv-pinned Codex binary and the dedicated
`<data-root>/codex-home` authentication context. It does not read or copy the
personal `~/.codex/config.toml` or `auth.json`. Authenticate this dedicated
context once per data root and confirm that the configured model appears in
that account's model catalog before starting research. The service removes
inherited `CODEX_ACCESS_TOKEN`, `CODEX_API_KEY`, and `OPENAI_API_KEY` values
from managed Codex launches.

`GET /api/v1/capabilities` provides a process-readiness check:

```bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/capabilities
```

QED has no separate liveness endpoint. A successful capabilities response
proves that FastAPI can serve requests; the first run also probes Codex model
and feature capabilities.

## Opt-in real Codex tests

The `real_codex` test makes an authenticated remote model call. It consumes
network access, model quota, time, and potentially billable usage. Run it only
after accepting those costs and creating a dedicated persistent credential
directory; it intentionally refuses the personal `~/.codex` tree.

```bash
export QED_REAL_CODEX_DATA_ROOT=/absolute/path/to/qed-real-codex
export QED_REAL_CODEX_HOME="$QED_REAL_CODEX_DATA_ROOT/codex-home"
mkdir -p "$QED_REAL_CODEX_HOME"
chmod 700 "$QED_REAL_CODEX_DATA_ROOT" "$QED_REAL_CODEX_HOME"

QED_CODEX="$(uv run python -c \
  'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')"
CODEX_HOME="$QED_REAL_CODEX_HOME" "$QED_CODEX" login

export QED_RUN_REAL_CODEX=1
uv run --frozen pytest -m real_codex \
  tests/test_real_codex.py \
  -k schema_constrained_offline_turn
```

The minimal cases probe the exact `gpt-5.6-sol` catalog entry with the supported
`low` effort, then run one fresh, read-only, offline,
JSON-Schema-constrained turn through the SDK and App Server routes. The exec
fallback requires another switch:

```bash
export QED_RUN_REAL_CODEX_EXEC=1
uv run --frozen pytest -m real_codex \
  tests/test_real_codex.py \
  -k authenticated_exec
```

The full lifecycle case runs literature, planning, two proof candidates,
structural, detailed, and citation verification, adjudication, and export. It
recomputes proof, report, research-record, manifest, and event-chain hashes and
checks the persisted external thread identities:

```bash
export QED_RUN_REAL_CODEX_LIFECYCLE=1
export QED_REAL_CODEX_LIFECYCLE_BACKEND=sdk  # or app-server
uv run --frozen pytest -m real_codex \
  tests/test_real_codex.py \
  -k full_research_lifecycle
```

These cases spend quota and may incur cost. Without the base opt-in switch and
both existing absolute directories, they skip before runtime construction.
The lifecycle and exec cases require their own switches in addition to
`QED_RUN_REAL_CODEX=1`.

The reliability benchmark has a separate spend guard because it runs the
frozen case grid, often with several repetitions:

```bash
QED_RUN_REAL_RELIABILITY_BENCHMARK=1 \
  uv run python benchmarks/reliability/qed_adapter.py \
  --requests /tmp/qed-reliability-requests.jsonl \
  --output /tmp/qed-reliability-sdk-results.jsonl \
  --data-root /tmp/qed-reliability-sdk \
  --backend sdk
```

Use a different dedicated data root for the App Server adapter. No benchmark
command reads personal `~/.codex`.

## Remote bind

Non-loopback binds are rejected in this release, even when a bearer token is
provided. The remote BFF, HttpOnly/Secure/SameSite session, CSRF protection,
Origin validation, exact CORS allowlist, and TLS deployment contract are not yet
implemented as one QED-controlled boundary. Do not work around this check by
passing a raw server override or exposing the App Server, SQLite files, or
export directory.

The browser console never receives the QED bearer token. For remote browser
access, use a same-origin backend-for-frontend (BFF) that authenticates users
with an HttpOnly, Secure, SameSite session cookie and adds the QED bearer header
on the server-side proxy hop. Bind QED to a private interface that only the BFF
can reach.

## Run the web console

For local development, start the tokenless loopback API on port 8000 and Vite on
port 5173:

```bash
npm ci
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000`. Open
<http://127.0.0.1:5173>.

Build static production assets with:

```bash
npm run build
```

The build writes `dist/`. Serve that directory from the same origin as the BFF
and route `/api` through the BFF to QED. Do not put a bearer token in a Vite
environment variable, HTML, JavaScript bundle, browser storage, or request made
by browser JavaScript.

## Run commands

Start one blocking CLI run:

```bash
uv run qed run \
  'Prove that the square of every odd integer is congruent to 1 modulo 8.' \
  --run-id odd-square-001 \
  --guidance 'Parameterize the odd integer.' \
  --verification-rule 'Check every divisibility claim.' \
  --data-root .qed
```

Inspect, cancel, or resume the durable run from another terminal:

```bash
uv run qed status odd-square-001 --data-root .qed
uv run qed cancel odd-square-001 \
  --idempotency-key odd-square-cancel-1 \
  --data-root .qed
uv run qed resume odd-square-001 \
  --idempotency-key odd-square-resume-1 \
  --data-root .qed
```

Reuse a command's idempotency key when a transport failure leaves its response
unknown. Choose a new key when you intend a new resume attempt. The service
queues accepted run workers when active run configurations leave no capacity.

Inspect a run that cannot resume:

```bash
uv run qed doctor odd-square-001 --data-root .qed
uv run qed reconcile odd-square-001 --data-root .qed
```

`doctor` is read-only. It prints a diagnostic manifest with execution leases,
thread and turn identities, unconfirmed runtime events, budget consumption, the
event-chain hash, and concrete resume blockers. `reconcile` exits with a
conflict because the current runtime interface has no authoritative
cross-backend exact-turn lookup. It never invents `turn.completed`.

After the execution lease expires and you decide to stop recovery, record the
operator decision:

```bash
uv run qed abandon odd-square-001 \
  --reason 'Runtime terminal status cannot be recovered from backend records.' \
  --idempotency-key odd-square-abandon-1 \
  --data-root .qed
```

The store appends the reason to the ordered event chain and sets a
non-resumable `failed` status. Reusing the key and reason returns the same
decision. Reusing the key with different text fails. A late runtime terminal
can still enter the audit chain under the original fencing identity.

## Event streaming

The event route replays stored events and then follows the run until a terminal
status:

```bash
curl --no-buffer \
  http://127.0.0.1:8000/api/v1/runs/odd-square-001/events
```

Each SSE `id` equals the store-assigned event sequence. Reconnect with the last
processed value:

```bash
curl --no-buffer \
  --header 'Last-Event-ID: 42' \
  http://127.0.0.1:8000/api/v1/runs/odd-square-001/events
```

The service sends comment heartbeats during quiet periods. Proxies must disable
response buffering and keep streaming connections open. Add the bearer header
when authentication is active.

## Shutdown and recovery

Send the service process `SIGINT` or `SIGTERM` through your process supervisor.
Service shutdown cancels in-process workers. The workflow asks the runtime to
interrupt active turns, retains frozen records, and normally marks the run
`paused` and releases its lease after each turn reports a terminal event. Start
the service against the same data root and resume each paused run with a stable
idempotency key.

A browser or SSE disconnect does not stop a run. Use the cancel command when you
want the workflow to stop. Cancellation first records `cancelling`, interrupts
active turns, and records `cancelled` only after every started turn has a
confirmed terminal event. Fenced lifecycle events that arrive during
`cancelling` continue draining, and a late terminal remains attached to an
in-process reconciliation pump. The execution owner then releases its lease.

QED records `runtime.turn_attempt_started` before invoking the runtime. A normal
EOF before any remote thread starts is closed by `runtime.turn_not_started`.
Local runtime preflight rejections happen before the attempt is recorded. If a
start may have been accepted but its turn identity was not recovered, QED
records `runtime.turn_start_unconfirmed`. If the identity is known but the
stream or protocol ends without a terminal event, QED records
`runtime.turn_terminal_unconfirmed`. Either ambiguous state fails closed: QED
will not acknowledge cancellation, release the execution, resume, or allow a
replacement execution even after lease expiry. Preserve the database and
runtime logs and run `qed doctor`. Do not edit SQLite to force a resume.
`qed abandon` records a terminal operator disposition when the backend cannot
supply the missing evidence.

QED marks schema, runtime, output, integrity, and exhausted-budget failures as
`failed`. A failed run keeps its events and artifacts for audit. Paused,
cancelled, and non-abandoned failed runs may resume from their current durable
stage. An operator-abandoned run is terminal and cannot resume. Resume requires
no unconfirmed runtime turn or live execution lease. It does not restore
exhausted budgets or discard earlier attempts, so correct the underlying runtime
or policy problem before retrying a failed run.

## Backups

Stop the service before a filesystem backup. Copy the complete data root so the
backup contains the SQLite database, WAL files, exports, legacy imports, and the
dedicated Codex state needed to resume recorded threads. For a verified
database-only backup and restore use:

```bash
uv run --frozen qed backup /srv/qed/qed.sqlite3 \
  --output /srv/backups/qed-2026-08-11.sqlite3
uv run --frozen qed restore /srv/backups/qed-2026-08-11.sqlite3 \
  --database /srv/qed/qed.sqlite3
```

`codex-home` can contain authentication credentials and local session state.
Restrict the data root and backup to the service account, encrypt backup media,
and never publish or commit either path. Restoring the complete root also
restores that credential material; use the Codex login flow to rotate or revoke
it when a backup may have been exposed.

Restore into a new empty path, start QED against that path, and inspect runs with
`qed status` or the API before opening it to users. The current release opens
SQLite schema versions 1 through 5 and upgrades supported older versions to 5.
The migration rejects duplicate external thread identities and reports the
conflicting run, external ID, and local thread IDs. Typed research records use
their own schema versions; legacy v1 reports and decisions remain readable but
cannot grant current QED policy PASS authority.
Run `uv run --frozen qed upgrade /srv/qed/qed.sqlite3` only after a verified
backup. Upgrade occurs on a staged copy and atomically replaces the database
only after integrity and schema checks pass; failed upgrades preserve the
original. The operator owns backup rotation, retention, credential rotation,
and restore drills.

## Upgrade procedure

1. Stop the service and back up the complete data root.
2. Check out the reviewed release commit.
3. Run `uv sync --all-groups --frozen` and the repository verification commands.
4. Start QED on loopback against a copy of production data, allowing any
   supported database schema upgrade to run there first.
5. Check the resulting database version, capabilities, snapshots, export paths,
   and one paused-run resume.
6. Start the production service and retain the prior binary plus backup for
   rollback.

Do not run two QED versions against one data root during an upgrade.

## Logs and diagnostics

The CLI writes command responses to stdout and structured logs to stderr. JSON
is the default log format. Set `--log-level info` for lifecycle events and use
`--log-format console` for an interactive workstation.

Log records include event names, run IDs, and error types. API errors return a
diagnostic ID without returning internal exception text. QED redacts known
secret keys and bearer values. Keep input problem text and credentials out of
external log context added by a proxy or supervisor.

The durable event log provides the audit timeline. Use
`GET /api/v1/runs/{run_id}/snapshot` for the complete typed state and inspect
`manifest.json` for research-record and turn-input hashes, thread/turn lineage,
proof-linked findings, runtime resolutions, execution segments, input/output
and cached-input/reasoning-output token totals, turn and search-query counts,
execution timing, the status and stage observed at the export boundary, QED
policy PASS,
and the canonical event-chain hash.
