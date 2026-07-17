# Codex runtime research and decisions

Research date: 2026-07-16 (America/Los_Angeles)
Official manual snapshot: fetched current on 2026-07-16 from [OpenAI's Codex manual](https://developers.openai.com/codex/codex-manual.md)
Runtime inspected: `codex-cli 0.144.5` on Linux x86-64
Official source inspected: `openai/codex` commit [`315195492c80fdade38e917c18f9584efd599304`](https://github.com/openai/codex/tree/315195492c80fdade38e917c18f9584efd599304)

This note uses three evidence labels:

- **Documented** means a current OpenAI manual, product-doc, or `openai/codex` source page states the claim.
- **Verified** means a package artifact or the installed runtime produced the observed result on the date above.
- **Bounded uncertainty** marks behavior that the official sources do not guarantee across accounts, versions, or future releases.

## Decisions for QED

1. Use the official `openai-codex` Python package and import it as `openai_codex`. Pin an exact release in the lockfile. Do not create a local package with either name.
2. Put all Codex operations behind one application interface. Implement its covered path with `AsyncCodex`. Put controls missing from the published SDK behind one typed App Server client generated from the pinned CLI's JSON Schema. QED needs that App Server path today for dynamic `max`/`ultra` effort values and read-only turns with network enabled.
3. Treat `gpt-5.6-sol` as the requested default model. At startup, require an exact `model/list` match for the active account and provider. Refuse the run if it is absent; do not substitute another model.
4. Treat reasoning effort as a non-empty, model-advertised string. Read the selected model's ordered `supportedReasoningEfforts` list. Accept a configured effort only when the list contains it. Do not maintain an application enum or infer ordering from names.
5. Detect model-driven subagents separately from application parallelism. Require an enabled `multi_agent` feature for internal subagent work, and require `ultra` in the selected model's effort list before requesting proactive delegation. Do not send the removed `multiAgentMode` field.
6. Give literature, planning, candidate generation, verification, and adjudication separate application-owned Codex threads. Start verifiers on new threads with frozen input and a read-only sandbox. Use `thread/fork` only when a stage must inherit conversation history.
7. Pass a JSON Schema on every control-producing turn and validate the final assistant message with the matching Pydantic model. Application code, not rendered text, computes transitions and final PASS.
8. Keep `codex exec` as a tested fallback. The fallback must emit JSONL, use the same output schema and explicit safety settings, and map into the same internal event types. No code path may use sandbox, approval, or hook-trust bypass flags.
9. Require an application-created absolute `cwd` on every turn. Build a distinct empty Git working directory for each attempt, including retries, and perform request construction and local preflight inside the attempt loop.
10. Keep command network out of the public turn contract. Apply one server-owned configuration through SDK, App Server, and exec that disables local shell, file-editing, browser, code-mode, plugin, and hook capabilities. Native web search remains available only to literature and citation roles.
11. Preserve Codex-native multi-agent capability when advertised. Subagents inherit the turn's empty Git working directory, read-only sandbox, and server-owned local-tool restrictions; QED does not force-enable an experimental multi-agent version.

## Official Python SDK

### Package identity and status

**Documented.** OpenAI documents a Python SDK installed with `pip install openai-codex`, imported as `openai_codex`, and requiring Python 3.10 or newer. The SDK starts a local Codex App Server over JSON-RPC and installs a pinned CLI runtime dependency. The current product manual calls the Python SDK beta. See [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) and the official [Python SDK source and docs](https://github.com/openai/codex/tree/main/sdk/python).

**Verified.** The public package index returned `openai-codex 0.1.0b3`. Its wheel metadata says:

| Field | Value |
|---|---|
| Distribution | `openai-codex` |
| Import package | `openai_codex` |
| Version | `0.1.0b3` |
| Python | `>=3.10` |
| Runtime dependency | `openai-codex-cli-bin==0.137.0a4` |
| Schema dependency | `pydantic>=2.12` |
| Release classifier | Beta |
| Wheel upload | 2026-06-03 |

Plain `pip download openai-codex` selected `0.1.0b3`; `--pre` was not required while no stable release existed. Pin the resolved beta explicitly so the first stable release cannot change behavior during an unrelated install.

The current `openai/codex` main branch labels its next Python package as stable and pins CLI `0.144.4`, but no stable `openai-codex` distribution appeared in the package index during this audit. Treat main-branch SDK changes as unreleased until the package index contains the matching release.

### Published `0.1.0b3` interface

**Verified against the wheel; also described by the official [API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md).** The useful public surface is:

```python
from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    Codex,
    CodexConfig,
    Sandbox,
    SkillInput,
    TextInput,
)
```

| Object | Relevant public operations |
|---|---|
| `Codex` / `AsyncCodex` | `thread_start`, `thread_resume`, `thread_fork`, `thread_list`, `thread_archive`, `thread_unarchive`, `models`, login/account methods, `close` |
| `Thread` / `AsyncThread` | `run`, `turn`, `read`, `set_name`, `compact`; property `id` |
| `TurnHandle` / `AsyncTurnHandle` | `stream`, `run`, `steer`, `interrupt`; property `id` |
| `TurnResult` | `id`, `status`, `error`, timestamps/duration, `final_response`, collected `items`, `usage` |
| `CodexConfig` | `codex_bin`, `launch_args_override`, `config_overrides`, `cwd`, `env`, client identity fields, `experimental_api` |

`thread_start` accepts `model`, `cwd`, `config`, `developer_instructions`, `ephemeral`, `sandbox`, and `approval_mode`, among other fields. `Thread.run` and `Thread.turn` accept `model`, `effort`, `output_schema`, `sandbox`, `approval_mode`, `cwd`, `service_tier`, `summary`, and input. The async API mirrors the sync API.

The SDK defines three sandbox presets:

- `Sandbox.read_only`
- `Sandbox.workspace_write`
- `Sandbox.full_access`, which maps to App Server `dangerFullAccess` and is forbidden by QED's runtime config

The SDK defines two high-level approval modes:

- `ApprovalMode.deny_all` maps to `approvalPolicy = "never"`. Codex receives no interactive escape path, so an action outside the active sandbox fails.
- `ApprovalMode.auto_review` maps to `approvalPolicy = "on-request"` plus `approvalsReviewer = "auto_review"`. A reviewer subagent decides eligible boundary requests without widening the sandbox.

The SDK defaults new threads to `auto_review`. QED must set the mode per role instead of inheriting that default. Fresh verification uses `deny_all` plus `read_only`.

### Streaming and lifecycle

**Documented and verified from the wheel.** `Thread.run(...)` starts one turn, consumes its notifications, and returns `TurnResult`. `Thread.turn(...)` returns a handle before completion. `TurnHandle.stream()` yields typed `Notification` values until the matching `turn/completed`; `steer()` adds input to the active turn, and `interrupt()` requests cancellation. The message router isolates streams by turn ID and supports more than one active turn on one client.

QED should use `AsyncCodex` and `AsyncTurnHandle.stream()` so the FastAPI service can forward events without blocking. Store the Codex thread ID in QED's SQLite state. Resume that exact ID after a process restart.

## App Server and JSON-RPC

### Protocol and transport

**Documented.** App Server powers OpenAI's rich Codex clients and exposes authentication, thread history, approvals, and streamed events. See [Codex App Server](https://learn.chatgpt.com/docs/app-server) and its official [protocol README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md).

App Server uses JSON-RPC 2.0 shapes while omitting the `"jsonrpc":"2.0"` member on the wire. It supports:

- JSONL over stdio, the default and supported integration choice for QED
- WebSocket, marked experimental and unsupported for production
- a local Unix socket carrying WebSocket frames

QED must spawn a local stdio server. It must not expose App Server on a public listener. Each connection sends one `initialize` request and one `initialized` notification before other methods. Use `experimentalApi: false` unless a reviewed feature requires the experimental surface.

The server returns JSON-RPC error `-32001`, `Server overloaded; retry later.`, when request ingress is full. Retry only that transient condition with bounded exponential backoff and jitter.

### Version-matched types

App Server generates a stable-only TypeScript or JSON Schema bundle for the installed CLI version:

```bash
codex app-server generate-json-schema --out ./schemas
codex app-server generate-ts --out ./schemas
```

Add `--experimental` only for an intentional experimental dependency. QED should pin the CLI used by the adapter, generate Pydantic request, response, notification, and server-request models from that version's stable JSON Schema, and run a CI drift check. The adapter must reject unknown required shapes and preserve unknown notification methods as diagnostics rather than crash the run.

### Required methods

The stable adapter needs a small surface:

| Purpose | App Server method |
|---|---|
| Handshake | `initialize`, then `initialized` |
| Capability probe | `model/list`, `experimentalFeature/list` |
| Fresh or continued context | `thread/start`, `thread/resume`, `thread/fork` |
| Stored context inspection | `thread/read`, `thread/list` |
| Execute and control | `turn/start`, `turn/steer`, `turn/interrupt` |
| Lifecycle cleanup | `thread/archive`, `thread/unarchive` |

`thread/start` creates a fresh conversation. `thread/resume` continues the same stored history. `thread/fork` creates a new thread ID with copied history. A verifier that receives a fork is not independent, so QED must call `thread/start` for verifier roles.

### Events

After `turn/start`, consume notifications until terminal `turn/completed`:

- `turn/started`, `turn/completed`, `turn/plan/updated`, `turn/diff/updated`
- `item/started`, item-specific deltas, `item/completed`
- `item/agentMessage/delta`, command output, reasoning summaries, file changes, MCP calls, web searches, and `collabToolCall` subagent activity
- `thread/tokenUsage/updated`
- `error`

OpenAI documents the item lifecycle as `item/started`, zero or more deltas, then `item/completed`. Treat `item/completed` as the authoritative item result. The current App Server README warns that `turn/started` and `turn/completed` can contain an empty `items` array after item events streamed; QED must build the turn item list from `item/*` notifications. The Python SDK's `TurnResult` performs that collection for its own path.

`turn/interrupt` returns after App Server accepts the request. Wait for `turn/completed` with status `interrupted` before committing QED's cancellation transition.

## Structured outputs

**Documented.** Python `Thread.run(..., output_schema=<dict>)`, App Server `turn/start.outputSchema`, and `codex exec --output-schema <file>` constrain the final assistant message to a JSON Schema. See the [App Server turn example](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#example-start-a-turn-send-user-input) and [non-interactive structured outputs](https://learn.chatgpt.com/docs/non-interactive-mode#create-structured-outputs-with-a-schema).

The Python SDK accepts a JSON object, not a Pydantic class, and returns the final value as `TurnResult.final_response`, a string. Use this boundary:

```python
schema = Report.model_json_schema()
result = await thread.run(prompt, output_schema=schema)
report = Report.model_validate_json(result.final_response)
```

All control schemas should set `additionalProperties: false` through Pydantic's extra-field policy and carry an application schema version. Reject a missing final response, malformed JSON, schema violation, and unsupported output-schema error. Do not fall back to Markdown parsing.

`outputSchema` applies to one turn. A later turn needs its own schema. The model catalog does not advertise an output-schema capability bit, so the opt-in real-model smoke test must verify one schema-constrained turn for the chosen model/runtime/auth combination. Startup should fail closed if that contract test fails.

## Models and reasoning effort

### `gpt-5.6-sol`

**Documented.** OpenAI's current [Codex model guide](https://learn.chatgpt.com/docs/models) says the default Power setting uses `gpt-5.6-sol` and describes Sol as the GPT-5.6 choice for complex, open-ended work. The exact slug is public documentation, not a private alias.

The same guide also documents `gpt-5.6-terra` and `gpt-5.6-luna`. Generic `gpt-5.6` appears in CLI and subagent guidance, so code must preserve the requested exact slug instead of normalizing it to the generic name.

**Verified for this account and runtime.** Raw `model/list` from `codex-cli 0.144.5` returned:

| Model | Available efforts, in advertised order | Catalog default | Default model |
|---|---|---|---|
| `gpt-5.6-sol` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` | `low` | yes |
| `gpt-5.6-terra` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` | `medium` | no |
| `gpt-5.6-luna` | `low`, `medium`, `high`, `xhigh`, `max` | `medium` | no |

The model guide's Power preset uses medium, while this runtime's catalog entry reports low as Sol's catalog default. These values refer to different defaults. QED should send its chosen effort explicitly after capability validation.

**Bounded uncertainty.** `model/list` reflects the current provider, account, entitlement, auth method, runtime, and model catalog. Public documentation does not guarantee that every account can use `gpt-5.6-sol`, nor that Platform API-key auth and ChatGPT-managed auth expose identical catalogs. Probe the session that will execute the run.

### Capability detection

App Server documents `model/list` as the source for `supportedReasoningEfforts`, and tells clients to preserve the returned array order. The current generated JSON Schema defines `ReasoningEffort` as a non-empty string rather than a closed enum.

Use this algorithm:

1. Page `model/list` to completion and find `model == configured_model`.
2. Preserve each `supportedReasoningEfforts[].reasoningEffort` string in returned order.
3. If the user omitted effort, use QED's recorded default policy or the catalog's `defaultReasoningEffort`.
4. If the user supplied effort, require exact membership. Return a configuration error containing the advertised list when it is absent.
5. Record model ID, selected effort, catalog response hash, CLI/SDK versions, auth mode, and probe time in the run manifest.

Do not hardcode `ultra`, `max`, or a ranking table. In particular, do not insert `model_reasoning_effort = "ultra"` into repo configuration and assume older runtimes understand it.

### Published SDK compatibility finding

**Verified.** `openai-codex 0.1.0b3` defines a closed Python `ReasoningEffort` enum containing `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`. When configured to use the installed CLI `0.144.5`, initialization succeeded but `Codex.models()` raised a Pydantic validation error on the runtime's `max` and `ultra` values.

The unreleased SDK source at the inspected commit accepts unknown non-empty effort strings, which fixes this forward-compatibility shape. Until a package with that code ships:

- let the published SDK use its pinned runtime;
- do not pass `CodexConfig(codex_bin=...)` without a contract test;
- use the version-matched App Server adapter for dynamic effort discovery and any turn that requests an effort the published SDK cannot represent.

## Multi-agent and subagents

**Documented.** Current Codex releases enable subagent workflows by default. Codex can delegate after an explicit prompt or applicable `AGENTS.md`/skill instruction. Ultra enables proactive delegation. Subagents inherit the active sandbox unless a custom agent overrides it. See [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).

The stable configuration keys include:

```toml
[features]
multi_agent = true

[agents]
max_threads = 6
max_depth = 1
# job_max_runtime_seconds = 1800
```

OpenAI documents `max_threads = 6` and `max_depth = 1` as defaults. QED must set lower application-level limits when stage parallelism plus internal subagents could exceed its budget.

App Server now ignores the deprecated `multiAgentMode` thread/turn field. Its protocol README says Ultra reasoning effort selects proactive behavior. Other effort levels can still delegate after an explicit request.

**Verified for this runtime.** Stable `experimentalFeature/list` returned `multi_agent` with `stage: "stable"`, `defaultEnabled: true`, and `enabled: true`. The method name is historical; its response includes stable features. The same call works with `experimentalApi: false`.

Capability gate:

- page `experimentalFeature/list` and require an entry named `multi_agent` with `enabled: true` before asking the model to use subagents;
- require `ultra` in the selected model's effort list before proactive delegation;
- use explicit application-owned threads when either capability is absent;
- record `collabToolCall` items so the UI can display spawned agent activity.

Application-owned stage threads remain the durable state-machine boundary. Internal subagents can help with bounded, read-heavy audits and reviews; they must not own candidate sealing, retry counters, PASS computation, or SQLite transitions.

## Configuration and customization controls

### `.codex/config.toml`

**Documented.** Codex loads project `.codex/config.toml` files from repo root toward the working directory. The closest value wins, and project layers load only for trusted repositories. CLI flags and `--config` overrides have higher precedence than project, profile, user, system, and built-in values. See [Config basics](https://learn.chatgpt.com/docs/config-file/config-basic), [advanced config](https://learn.chatgpt.com/docs/config-file/config-advanced), and the [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Use the checked-in project config for Codex developer defaults, feature enablement, agent limits, and approved hooks. Keep QED run budgets, stage transitions, retry rules, and export behavior in QED's typed application config. Duplicating those values in Codex config would create two authorities.

Launch the App Server adapter and CLI fallback with `--strict-config`. Pass run-specific model, effort, sandbox, search, and network settings as typed request fields or explicit overrides. Never accept raw `-c` strings from the web client.

### `AGENTS.md`

**Documented.** Codex reads one global guidance file and then one file per directory from project root to the working directory. Files closer to the working directory override earlier guidance. The default combined project limit is 32 KiB. See [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

QED's root `AGENTS.md` should contain repository commands, invariants, generated-file rules, verification expectations, and paths to focused references. It must not encode the runtime stage machine or mutable run state. Subdirectory files can narrow backend, frontend, and test instructions.

### Skills

**Documented.** Repo skills live at `.agents/skills/<name>/SKILL.md`. `SKILL.md` requires `name` and `description`; Codex first loads metadata and reads the body when it selects the skill. See [Build skills](https://learn.chatgpt.com/docs/build-skills).

Use skills for reusable mathematical procedures such as literature evidence collection, proof planning, candidate critique, and independent verification. QED can invoke a skill explicitly with SDK `SkillInput(name, path)` or App Server's `skill` input item, paired with a `$skill-name` text mention. Skills may explain a stage's method; SQLite and application code still control stage order and authority.

### Hooks

**Documented.** Hooks can live in `.codex/hooks.json` or inline config next to an active config layer. Project hooks require a trusted repository and hash-based user approval. Matching handlers from multiple sources all run, and matching command handlers launch concurrently. Only command handlers execute today. See [Hooks](https://learn.chatgpt.com/docs/hooks).

Useful stable events include `SessionStart`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, and `Stop`.

Use hooks for developer-time policy checks such as the approved Impeccable detector. Do not use hooks for candidate sealing, transition commits, retry accounting, or final verdicts. QED must not pass `--dangerously-bypass-hook-trust`; a setup command should tell the user to inspect and approve the checked-in hook.

## Sandbox, network, and approvals

**Documented.** Codex separates the OS-enforced sandbox boundary from the approval policy. Local `workspace-write` blocks network by default and limits writes to the workspace. `.git`, `.agents`, and `.codex` remain protected inside writable roots. See [Agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security).

QED permits only these role policies:

| Role | Filesystem | Command network | Search | Approval behavior |
|---|---|---|---|---|
| Literature/citation | `readOnly` in an empty per-attempt Git working directory | off | configured live/indexed mode | deny all |
| Planning/adjudication | `readOnly` in an empty per-attempt Git working directory | off | disabled | deny all |
| Candidate generation | `readOnly` in an empty per-attempt Git working directory | off | disabled | deny all |
| Structural/detailed verifier | `readOnly` over sealed candidate and evidence | off | disabled | deny all |

The published SDK's presets do not expose `networkAccess`. QED therefore keeps
command network disabled in every adapter and uses only native web search for
literature and citation work.

Enabling command network access alone grants unrestricted outbound access. QED
does not expose that control and explicitly disables the network proxy feature.

`web_search` is separate from command network. Supported documented modes are `disabled`, `cached`, `indexed`, and `live`. Only the literature/citation policy may select a non-disabled mode. Treat retrieved content as untrusted input and preserve source URLs plus hashes in the evidence ledger.

Do not expose `workspace-write`, `danger-full-access`, `Sandbox.full_access`,
`approval_policy = "never"` without a sandbox,
`--dangerously-bypass-approvals-and-sandbox`, `--yolo`, or
`--dangerously-bypass-hook-trust` in QED configuration.
`ApprovalMode.deny_all` is safe only because QED pairs it with `readOnly`; it
removes prompts, not the sandbox.

## `codex exec` fallback

**Documented.** `codex exec` is the stable non-interactive CLI. `--json` emits JSONL events such as `thread.started`, `turn.started`, `item.*`, `turn.completed`, `turn.failed`, and `error`. `--output-schema` constrains the final response, and `codex exec resume <SESSION_ID>` continues a stored session. See [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and [`codex exec` command reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-exec).

QED's fallback command builder must supply:

- exact model and validated effort through `--model` and explicit config override;
- `--sandbox read-only`;
- `--json`, `--output-schema`, `--strict-config`, and an explicit working directory;
- `--ignore-user-config` for deterministic application runs while retaining auth in `CODEX_HOME`;
- no bypass, `--full-auto`, or `--skip-git-repo-check` flags.

Parse JSONL into the same internal event union used by the SDK/App Server path. Capture the `thread.started` ID for resume. A stop request terminates the child process, records cancellation intent in SQLite, and waits for process exit before marking the attempt stopped. App Server remains the preferred path because `turn/interrupt` gives a protocol-level terminal event.

Run mocked parity tests for start, stream, structured result, failure, cancel, and resume. Keep a separate opt-in real CLI smoke test. The fallback must never activate silently after a schema or capability error; those errors require operator action.

## Bounded uncertainty and release gates

1. `openai-codex` is beta at `0.1.0b3`; OpenAI's main branch already contains post-beta API changes. An upgrade requires contract tests and a regenerated capability snapshot.
2. The package's pinned CLI and a separately installed `codex` can differ. Never combine them through `CodexConfig(codex_bin=...)` without proving schema compatibility.
3. `gpt-5.6-sol`, `max`, `ultra`, and multi-agent availability can vary by account and auth mode. Probe the executor session and fail closed.
4. Model discovery has no advertised structured-output bit. Require a schema-constrained real-model smoke test before enabling production execution for a new model/runtime pair.
5. Command networking stays disabled; literature and citation use native web search instead.
6. App Server WebSocket transport remains experimental and unsupported. Use local stdio for this rewrite.

## Direct official sources

- [Codex manual](https://developers.openai.com/codex/codex-manual.md)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Official Python SDK](https://github.com/openai/codex/tree/main/sdk/python)
- [Python SDK API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md)
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [App Server protocol source](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Codex models](https://learn.chatgpt.com/docs/models)
- [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Config basics](https://learn.chatgpt.com/docs/config-file/config-basic)
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Skills](https://learn.chatgpt.com/docs/build-skills)
- [Hooks](https://learn.chatgpt.com/docs/hooks)
- [Approvals, sandboxing, and network](https://learn.chatgpt.com/docs/agent-approvals-security)
- [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
