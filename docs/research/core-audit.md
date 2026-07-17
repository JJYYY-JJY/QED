# Codex runtime and state core production audit

Audit date: 2026-07-16
Audited commit: `27cb3c7960e2f2cd6704f7772454ebeb162f2b9c`
Disposition: release blockers found

## Scope and method

This audit is pinned to the committed snapshot above. Untracked design work and later
working-tree fixes are intentionally excluded.

The reviewed production code is:

- `src/qed/config.py`, `schemas.py`, `store.py`, `decision.py`,
  `model_outputs.py`, `inputs.py`, `prompting.py`, and `service_settings.py`;
- every module under `src/qed/runtime/`;
- the matching unit tests under `tests/`.

The review focused on the repository invariants in `AGENTS.md`: strict structured control
outputs, SQLite authority, explicit transitions, immutable frozen inputs, fresh read-only
verifiers, literature-only network access, code-computed PASS, and no approval or sandbox
escape path.

The following baseline checks passed at the audited commit:

```text
uv run pytest -q <scoped tests>    56 passed in 0.41s
uv run ruff check <scoped files>   All checks passed!
uv run mypy <scoped source>        Success: no issues found in 17 source files
```

Passing these checks does not clear the findings below. The executable counterexamples use
public interfaces unless a finding explicitly says it is a source-traced failure path.

## Severity model

- **Critical**: can confer a trusted completion/PASS or defeat verifier isolation.
- **High**: can corrupt or deadlock durable execution, break deterministic resume, or bypass a
  required typed authority boundary.
- **Medium**: creates a latent security or configuration ambiguity that should be closed before
  production exposure.

## Critical findings

### C-01 — A run can complete without a candidate, verification, or code-computed PASS

**Evidence.** `RunStore.transition_stage()` checks only the stage transition table, and
`transition_run(..., COMPLETED)` checks only that the current stage is `complete`
(`src/qed/store.py:553-588`, `src/qed/store.py:674-697`). No persisted candidate, required report,
adjudication, or `CandidateDecision` is consulted.

**Minimal counterexample.** The following public call sequence succeeds:

```text
create_run
transition_run(RUNNING)
transition_stage(LITERATURE -> PLANNING -> PROVING -> VERIFICATION
                 -> ADJUDICATION -> EXPORT -> COMPLETE)
transition_run(COMPLETED)
```

The reproduced result was `status=completed` with ten events and zero candidates and zero
verifications.

**Impact.** The durable source of truth can claim completion without any mathematical PASS. A
future API or orchestration bug would become a trusted false-positive rather than a rejected
transition.

**Minimal fix.** Remove generic completion authority from `transition_run`. Add one atomic
completion operation that, inside the same transaction, loads a sealed candidate and all required
immutable reports, rechecks their hashes and canonical verifier identities, recomputes
`CandidateDecision`, persists the decision and its hash, appends the completion event, and only then
marks the run completed.

**Acceptance tests.**

- `test_completion_rejects_run_without_candidate`
- `test_completion_rejects_missing_or_non_pass_required_report`
- `test_completion_recomputes_decision_in_same_transaction`
- `test_completed_run_references_immutable_decision_and_candidate_hashes`

### C-02 — The runtime request type permits resumed, forked, or writable verifier turns

**Evidence.** `RunRequest.validate_network_controls()` restricts network roles but imposes no
thread or sandbox invariant for `WorkRole.VERIFIER` (`src/qed/runtime/models.py:80-103`).
`SandboxMode.WORKSPACE_WRITE`, `ResumeThread`, and `ForkThread` remain valid values.

**Minimal counterexample.** This validates successfully:

```python
RunRequest(
    model="gpt-5.6-sol",
    effort="high",
    prompt="verify",
    output_schema={},
    role=WorkRole.VERIFIER,
    thread=ResumeThread(thread_id="prover-thread"),
    sandbox=SandboxMode.WORKSPACE_WRITE,
)
```

The reproduced value retained `thread.kind == "resume"` and
`sandbox == "workspace-write"`. Every adapter then faithfully sends those unsafe-for-verification
controls.

**Impact.** A verifier can inherit proof-author conversation history and can write in the active
workspace. The type boundary therefore does not enforce the project's principal independent
verification guarantee.

**Minimal fix.** Make role policy a validator on the request used by every backend. A verifier must
require `FreshThread`, `READ_ONLY`, disabled web search, and no command network. Represent planning,
proof, and adjudication as explicit roles and validate their allowed combinations too. Literature
and citation turns should remain read-only even when restricted network is enabled.

**Acceptance tests.**

- Matrix-test every role against fresh/resume/fork, both sandbox modes, all search modes, and
  command-network presence.
- Assert that each SDK, App Server, and exec wire request for a verifier is fresh, read-only,
  search-disabled, network-disabled, and approval-denied.
- Assert construction fails before any runtime is contacted.

### C-03 — Distinct local verifier IDs can alias the same Codex thread and still PASS

**Evidence.** `threads.external_thread_id` is nullable and not unique
(`src/qed/store.py:346-361`). `add_thread()` does not bind it to provenance or prevent reuse
(`src/qed/store.py:763-819`). `add_verification()` defines “fresh” only as a local verifier row with
`parent_thread_id IS NULL` (`src/qed/store.py:1018-1025`). `decide_candidate()` compares the local
`report.verifier_thread_id` strings, not canonical runtime thread identities or report provenance
(`src/qed/decision.py:82-93`).

**Minimal counterexample.** Two local rows named `structural-local` and `detailed-local` were created
with the same `external_thread_id="same-external"`. Their structural and detailed PASS reports were
accepted by the store, and `decide_candidate(...).passed` reproduced as `True`.

**Impact.** The same Codex conversation can impersonate two independent verifiers. A reused writer
thread can also evade the writer/verifier comparison if local and external IDs occupy different
namespaces.

**Minimal fix.** Define one canonical runtime-thread identity and bind local thread record,
`external_thread_id`, provenance `source_id`, candidate author, and report verifier to it. Prevent a
canonical verifier thread from being attached to more than one required report or from matching the
candidate author. If resumes are modeled, update one thread lineage instead of minting a new local
identity for the same external thread.

**Acceptance tests.**

- Reproduce two local verifier rows with one external ID and require the second insert or final
  decision to fail.
- Reject report provenance whose `source_id` does not match the persisted verifier identity.
- Reject writer-thread reuse across local/external/provenance namespace variations.
- Reject reuse of an otherwise top-level verifier thread on a later candidate when policy requires
  a fresh verifier.

## High findings

### H-01 — One `RunStore` instance is not safe for concurrent callers, and snapshots are not atomic

**Evidence.** The store shares one `sqlite3.Connection` with `check_same_thread=False` but has no
serialization around `BEGIN IMMEDIATE` or reads (`src/qed/store.py:253-289`). `snapshot()` performs
seven independent public reads rather than one SQLite read transaction
(`src/qed/store.py:1182-1191`).

**Minimal counterexample.** Sixteen threads called `append_event()` concurrently on one store. The
reproduced outcomes were:

```text
1  ok
14 OperationalError: cannot start a transaction within a transaction
1  DatabaseError: another row available
```

Only two events, including `run.created`, remained persisted.

**Impact.** Ordinary FastAPI worker/threadpool concurrency can fail requests, interleave cursor
state, or return a run snapshot assembled from different database moments. A per-method write lock
alone would not make the multi-query snapshot consistent.

**Minimal fix.** Serialize all use of a shared connection or use one connection per operation, while
retaining `BEGIN IMMEDIATE` for writers. Read `RunSnapshot` in one explicit read transaction. Add a
run version and compare-and-swap semantics for mutating commands so serialization does not turn
stale commands into valid commands.

**Acceptance tests.**

- Run at least 32 simultaneous event appends through one `RunStore`; require no exceptions and a
  contiguous, unique sequence.
- Exercise simultaneous stage transitions and require exactly one expected-version winner.
- Pause a snapshot between component reads while another connection commits; require the returned
  snapshot to reflect entirely the before or entirely the after state.

### H-02 — App Server notification broadcast corrupts concurrent turn streams

**Evidence.** `StdioAppServerTransport` broadcasts every notification to every unbounded subscriber
queue (`src/qed/runtime/stdio.py:98`, `src/qed/runtime/stdio.py:116-134`,
`src/qed/runtime/stdio.py:234-240`). `AppServerRuntime.stream()` ignores unrelated item/usage events,
but treats an unrelated `turn/started` or `turn/completed` as a protocol violation
(`src/qed/runtime/app_server.py:206-290`). Error notifications are also unscoped and broadcast.

**Minimal counterexample.** A valid `turn/started` for thread B was delivered while stream A awaited
its own events. Stream A reproduced:

```text
RuntimeProtocolError: received lifecycle event for an unexpected turn
```

**Impact.** The requested parallel candidates/verifiers cannot safely share the App Server
transport. One active turn can crash every other turn, and slow subscribers can grow without bound.

**Minimal fix.** Give the transport a single reader/router that dispatches notifications by
`(thread_id, turn_id)` into bounded per-turn queues, with an explicit channel for global events.
Subscribe before `turn/start` can emit an event, buffer the short pre-ID interval, and close the
subscription deterministically in `finally`. Unrelated lifecycle events must never fail a stream.

**Acceptance tests.**

- Start two turns concurrently and interleave all lifecycle, item, usage, error, and completion
  notifications; each consumer must receive only its own scoped events.
- Complete/cancel one stream while the other continues.
- Flood one slow consumer and verify bounded backpressure or a defined fail-closed overflow.

### H-03 — Resume cannot reconstruct frozen input or fence prior workers, and retries are unbounded

**Evidence.** `RunInput` is immutable and hashable in memory (`src/qed/inputs.py:15-29`), but the
`runs` table stores only `input_sha256`, not the canonical problem/guidance/rules bytes
(`src/qed/store.py:299-317`, `src/qed/store.py:493-539`). There is no execution-segment, checkpoint,
worker lease, fencing token, or per-retry counter. `resume_run()` only changes run status and
increments `resume_count` (`src/qed/store.py:644-672`). Configured proof attempts, plan revisions,
and strategy rewrites (`src/qed/config.py:32-44`) are not enforced by store transitions or candidate
creation. The adjudication stage can loop indefinitely (`src/qed/store.py:102-113`).

**Impact.** SQLite alone cannot reconstruct the exact input after restart. A stale worker can keep
writing after a resumed worker starts, and retry budgets can be exceeded while the persisted config
still claims they are bounded.

**Minimal fix.** Persist one immutable canonical `RunInput` row and verify its hash. Model every
start/resume as an immutable execution segment with parent, checkpoint, status, worker lease epoch,
and event range. Require a current fencing token on writes. Persist retry counters and compare them
with the frozen config in the same transaction as every adjudication loop transition.

**Acceptance tests.**

- Close and reopen the database, then reconstruct byte-identical problem, guidance, and rules from
  SQLite without consulting files.
- Crash and resume after every stage; require a new segment and retained prior history.
- Attempt a write with the old segment's fencing token after resume; require rejection.
- Exhaust each proof/plan/strategy budget and require a deterministic terminal transition.

### H-04 — Generic stage output erases strict schemas and referential authority

**Evidence.** Evidence, plans, and adjudications have strict Pydantic types, but the store persists
them only through the unrestricted `add_stage_output(kind: str, content: JsonValue)` seam
(`src/qed/store.py:699-745`) and returns type-erased JSON (`src/qed/store.py:1405-1418`). A candidate's
`plan_id` has no foreign key or existence check. Evidence IDs cannot be resolved against a typed
ledger. `create_candidate()` checks that an optional thread belongs to the run but does not require
a prover thread or the correct run stage (`src/qed/store.py:866-913`). Public event/stage-output
methods can also claim a stage unrelated to the run's current stage.

**Impact.** Callers can persist malformed control artifacts, candidates for nonexistent plans, and
proofs attributed to verifier or literature threads. Strict model schemas exist but are not the
durable authority on resume.

**Minimal fix.** Add typed immutable evidence, plan, adjudication, and decision records, or a closed
schema registry that validates both writes and reads. Add plan/evidence relationships and enforce
run status, current stage, and producer role transactionally. Reserve generic stage outputs for
non-authoritative telemetry.

**Acceptance tests.**

- Reject malformed plan/evidence/adjudication JSON before any row or event commits.
- Reject a candidate whose plan does not exist or belongs to another run.
- Reject candidate creation by a non-prover thread or outside the proving stage.
- Reject events and outputs whose supplied stage differs from authoritative current state.

### H-05 — Contradictory verifier reports can compute PASS

**Evidence.** `VerificationReport.validate_findings()` only checks that each finding references a
known check. It does not require unique check/finding IDs or consistency between finding severity
and check status (`src/qed/schemas.py:209-257`). `decide_candidate()` considers only each report's
computed check-status verdict (`src/qed/decision.py:73-80`).

**Minimal counterexample.** Two required reports were created with duplicate PASS check IDs and a
critical finding whose detail said the proof is invalid. Both report verdicts reproduced as `pass`,
and `decide_candidate(...).passed` reproduced as `True`.

**Impact.** Although the final Boolean is computed in code, contradictory model-owned fields can
still make that computation fail open.

**Minimal fix.** Require unique IDs and define schema-level consistency rules. At minimum, a major
or critical finding must make its referenced check non-PASS, and the final decision should
independently fail closed on any contradictory or unresolved finding rather than trusting a
computed report property alone.

**Acceptance tests.**

- Reject duplicate check IDs and duplicate finding IDs.
- Reject PASS checks with attached critical/major findings.
- Ensure any accepted report set is internally consistent before `passed=True` can be constructed.

### H-06 — A terminal “completed” event does not guarantee validated structured output

**Evidence.** `RunRequest.output_schema` accepts any dictionary, including `{}`
(`src/qed/runtime/models.py:80-103`). `TurnCompleted.output` is an unvalidated `str | None`, even for
`status="completed"` (`src/qed/runtime/models.py:216-220`). SDK, App Server, and exec adapters copy
the final agent-message text directly into that field (`src/qed/runtime/sdk.py:193-210`,
`src/qed/runtime/app_server.py:276-286`, `src/qed/runtime/exec.py:237-248`). The committed core has no
mandatory mapping from that terminal string to the matching `ModelDraft` type.

**Impact.** JSON Schema is sent as a generation constraint, but malformed, missing, or
wrong-schema output can still cross the runtime seam as a successful completion and be parsed
inconsistently by callers.

**Minimal fix.** Make the application-facing operation generic over a concrete Pydantic output
type. Derive its JSON Schema internally, validate exactly one terminal JSON value, reject unknown
fields, and expose a typed result. A transport-level `TurnCompleted` may remain for streaming, but
it must not authorize a stage transition.

**Acceptance tests.**

- Reject `completed` with no final agent message.
- Reject malformed JSON, multiple JSON values, wrong types, and extra fields.
- Verify every role uses the schema derived from the exact model used to validate its result.

### H-07 — Exec fallback can deadlock and cannot report a clean interrupted terminal state

**Evidence.** The child stderr is a pipe (`src/qed/runtime/exec.py:50-57`) but is never drained. The
stream treats every nonzero process exit as a protocol error and only emits completed/failed
terminals (`src/qed/runtime/exec.py:155-255`). `interrupt()` sends `terminate()` and waits forever if
the child ignores it (`src/qed/runtime/exec.py:281-288`); `close()` has the same unbounded wait
(`src/qed/runtime/exec.py:290-296`). There is no interrupted-event bookkeeping.

**Impact.** Enough stderr can block the CLI before stdout completes. A cancellation can hang
forever or surface as a runtime failure rather than the cancellation state expected by the durable
state machine.

**Minimal fix.** Drain stderr concurrently with a bounded tail for diagnostics. Track
application-requested interruption, wait for a short grace period, escalate to kill, and map the
result to `TurnCompleted(status="interrupted")`. Apply the same bounded shutdown to generator
cleanup and `close()`.

**Acceptance tests.**

- Emit stderr beyond pipe capacity while valid JSONL arrives on stdout; require completion.
- Interrupt an active fake process and require exactly one interrupted terminal event.
- Use a fake process that ignores terminate; require bounded kill and return.
- Repeat interruption before and after `turn.started`.

### H-08 — Cancelled or failed App Server requests can poison the shared transport

**Evidence.** `_raw_request()` removes a pending request in `finally`
(`src/qed/runtime/stdio.py:155-166`). A late response then has an unknown ID and raises a protocol
error (`src/qed/runtime/stdio.py:220-223`). The reader catches the error, fails current pending
futures, and ends subscribers, but leaves `_process` non-null
(`src/qed/runtime/stdio.py:180-208`). `_ensure_started()` subsequently treats that dead process as
healthy (`src/qed/runtime/stdio.py:136-145`). No request has a timeout, and overload error codes are
flattened into a string (`src/qed/runtime/stdio.py:224-231`).

**Impact.** Cancelling one timed-out request can kill all active turns and make later requests wait
forever. An unexpected App Server exit leaves the adapter unusable without a clear restart or
fail-fast state.

**Minimal fix.** Keep tombstones for cancelled request IDs so late responses are ignored. Record a
terminal transport error, clear and reap the process, and make all future calls fail immediately or
perform one serialized restart. Add bounded request timeouts and preserve typed JSON-RPC error
codes; retry only explicitly classified transient overloads with a bounded policy.

**Acceptance tests.**

- Cancel a request, deliver its response late, and prove other concurrent and subsequent requests
  still work.
- Close stdout unexpectedly and require every waiter plus the next request to fail promptly.
- Reproduce the documented overload error and verify bounded retry count/backoff.
- Close a process that ignores terminate and require bounded escalation.

### H-09 — Invalid hashes can commit even though the public operation reports failure

**Evidence.** Store method annotations such as `input_sha256: Sha256` do not perform runtime
validation. `create_run()` inserts and commits the raw value, then constructs a validated
`RunRecord` only after the transaction (`src/qed/store.py:493-539`,
`src/qed/store.py:1321-1338`). `add_artifact()` has the same post-commit validation shape
(`src/qed/store.py:1116-1164`, `src/qed/store.py:1420-1434`).

**Minimal counterexample.** Calling `create_run(..., input_sha256="not-a-sha")` reproduced a
Pydantic `ValidationError`, but direct inspection immediately afterward found the committed row:

```text
('poisoned', 'not-a-sha')
```

**Impact.** A public call can fail while leaving authoritative state that cannot be read through the
public model. Resume and export can then fail permanently or use unverified denormalized hashes.

**Minimal fix.** Validate every primitive boundary value before `BEGIN`, add SQLite checks where
practical, and construct/validate the returned record before commit. On every read, recompute and
compare config, provenance, candidate, report, event, and stage-output hashes rather than accepting
valid-looking denormalized digest strings.

**Acceptance tests.**

- Pass invalid IDs, hashes, paths, sizes, and JSON to each public store method; require no row and no
  event after failure.
- Tamper each JSON/hash pair through a diagnostic connection and require an explicit integrity
  error on read/snapshot/export.

### H-10 — Capability resolution is live on every turn and is absent from the durable snapshot

**Evidence.** `RoutedCodexRuntime.stream()` probes and resolves `effort="auto"` for every call
(`src/qed/runtime/router.py:57-68`). The App Server performs fresh paged model and feature reads
(`src/qed/runtime/app_server.py:158-181`). The run record persists only unresolved `QEDConfig`
values, such as `effort="auto"`, and a caller-supplied runtime version. It does not store the model
catalog, selected effort, feature set, chosen backend, or actual CLI/SDK version.

**Impact.** A catalog default/order or installed CLI change between start and resume can change the
model controls while preserving the same config hash. Parallel turns can even resolve against
different catalog moments.

**Minimal fix.** Resolve capabilities at a defined boundary and persist an immutable capability
snapshot and chosen controls per run or execution segment. Resume must reuse that snapshot or fail
explicitly if the recorded controls are no longer available.

**Acceptance tests.**

- Return different fake catalogs before and after reopen; require the resumed segment to retain the
  recorded choice or fail, never silently change it.
- Assert manifests include model, selected effort, backend, capabilities, and runtime versions.

### H-11 — The prompt delimiter can be closed by untrusted input at instruction precedence

**Evidence.** `render_turn_prompt()` places role policy and untrusted canonical JSON in one text
prompt. JSON serialization does not escape `<` or `>`, so a value can contain the literal
`</frozen-input>` delimiter (`src/qed/prompting.py:67-83`). The existing malicious-input test
explicitly asserts that this closing tag appears unchanged inside the rendered payload
(`tests/test_inputs_prompting.py:42-55`).

**Impact.** User guidance or retrieved literature can visually terminate the data block and inject
same-precedence instructions. Hashing proves bytes did not change; it does not enforce instruction
boundaries.

**Minimal fix.** Put stable role policy in the SDK/App Server developer-instruction channel and
send untrusted data through a structured input channel. Ensure the chosen data encoding cannot
contain the framing sentinel; at minimum escape U+003C/U+003E and length-prefix the canonical bytes.
Sandbox, role policy, and output validation remain required defense in depth.

**Acceptance tests.**

- Render payloads containing closing/opening tags, fake hashes, Markdown control words, and nested
  JSON strings; assert the framing delimiter occurs exactly once in each direction.
- Verify the runtime wire request keeps developer policy separate from user/retrieved data.

## Medium findings

### M-01 — Artifact paths are not constrained to the managed run root

**Evidence.** `ArtifactRecord.relative_path` is an arbitrary optional string, and
`add_artifact()` stores it without rejecting absolute paths, `..`, or platform separators
(`src/qed/store.py:214-225`, `src/qed/store.py:1116-1164`).

**Impact.** If the artifact-serving layer later joins this value to `data_root`, the store becomes a
path-traversal authority. Store-generated paths reduce likelihood but do not remove the unsafe
interface.

**Minimal fix.** Validate a normalized relative POSIX artifact path at insertion. The read/download
layer must resolve under the configured run root, reject symlinks and non-regular files, and open by
server-owned artifact ID rather than a browser-provided path.

**Acceptance tests.** Reject absolute paths, `..`, empty segments, backslash traversal, NUL, and a
symlink that resolves outside the managed root.

### M-02 — The approval setting is a hashed but ineffective configuration knob

**Evidence.** `SandboxPolicy.approval` accepts `untrusted`, `on-request`, or `never`
(`src/qed/config.py:62-69`). `RunRequest` has no approval field, while every adapter hardcodes
deny-all/never (`src/qed/runtime/sdk.py:105-130`, `src/qed/runtime/app_server.py:292-347`,
`src/qed/runtime/exec.py:79-88`).

**Impact.** Two differently hashed configs can execute with identical approval behavior, and a UI
can promise `on-request` although the runtime ignores it. This weakens reproducibility and operator
trust.

**Minimal fix.** If QED's production policy is deny-all, narrow the config field to the literal
`never` or remove it from user configuration. Otherwise define and test an explicit role mapping,
while keeping verifiers unconditionally deny-all.

**Acceptance tests.** For every accepted config value, assert the exact SDK/App Server/exec wire
control. Reject any value with no implemented mapping.

## Confirmed positive invariants

These positives are limited to what the audited snapshot proves; they do not offset the blockers.

- Runtime sandbox enums expose only read-only and workspace-write, not full access
  (`src/qed/runtime/models.py:29-32`).
- The router never selects exec implicitly; auto selection is SDK then App Server, while exec is an
  explicit preference (`src/qed/runtime/router.py:70-81`).
- The exec builder uses argument arrays, `--strict-config`, `--ignore-user-config`, JSONL, an output
  schema, read-only sandboxing, and no bypass/full-auto/skip-git flags
  (`src/qed/runtime/exec.py:65-120`). Prompt text is placed after `--`, preventing option injection.
- Non-literature/citation roles cannot request web search or command network in the current generic
  request validator (`src/qed/runtime/models.py:94-103`).
- Sealed candidate and verification update/delete triggers exist, and the scoped immutability tests
  pass (`src/qed/store.py:423-447`, `tests/test_store.py:180-234`).
- `VerificationReport.verdict` is computed from structured check statuses rather than parsed from
  Markdown, and model draft schemas forbid an explicit verdict field
  (`src/qed/schemas.py:249-257`, `src/qed/model_outputs.py:30-100`).

## Release recommendation

Do not integrate the current core as a production authority until C-01 through C-03 are closed and
their acceptance tests pass. H-01 through H-11 should also close before the sample lifecycle is used
as a completion gate: several of them can otherwise turn cancellation, concurrency, or restart into
state corruption or a false result. Re-run this audit against the final commit, including two-turn
App Server concurrency and crash/resume tests, rather than relying only on the current happy-path
unit suite.
