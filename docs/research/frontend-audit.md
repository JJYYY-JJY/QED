# Frontend audit and research-console design requirements

> Historical / non-runtime research record. The alpha-only visual tooling
> mentioned below is not a dependency or current release gate.

Status: audit complete; implementation not started
Audit date: 2026-07-16
Branch observed: `codex-native-rewrite`

## Executive conclusion

FastAPI plus React, Vite, and TypeScript is feasible and is the right replacement for the current Streamlit UI. There is no frontend-specific blocker. The important sequencing constraint is that the React application must be built over the new typed, durable run API and event log, not over the current filesystem scanner or `subprocess.Popen` objects.

The present UI is useful as a behavioral inventory, but it is not a safe production control plane. It launches a shell pipeline directly, treats arbitrary directories and Markdown files as application state, stores the live process only in browser session state, and polls the filesystem every four seconds. The production console should preserve the useful intent—problem editing, guidance, configuration, progress, artifacts, stop, and resume—while replacing every underlying lifecycle mechanism.

The recommended transport is **SSE for ordered server-to-client run events plus authenticated REST commands for mutations**. A WebSocket is not justified by the current product requirements. The event stream must replay from a durable sequence number, while the run snapshot remains the source of truth after reload or reconnect.

Three release-blocking conditions must be resolved before any web UI is exposed beyond a trusted local machine:

1. Eliminate arbitrary filesystem selection and destructive path-based resume.
2. Keep credentials out of browser state, run configuration snapshots, logs, and artifacts.
3. Put authenticated, authorized API boundaries in front of execution; never let a UI field select an executable path or unsafe approval bypass.

## Scope, evidence, and limitations

This is a code-level frontend audit following the repo-local Impeccable `audit` and product-register guidance. It covers the tracked Streamlit UI, the process and artifact contracts it depends on, the documented UX, and the absence of a modern frontend/test stack.

Evidence inspected:

- `ui/app.py`, `ui/config_panel.py`, `ui/process_manager.py`, `ui/progress_monitor.py`, and `ui/utils.py`
- all files under `ui/archived/`
- `run.sh`, `config.yaml`, relevant sections of `code/pipeline.py`, `code/model_runner.py`, and `code/decomposition_prover.py`
- `README.md`, `.gitignore`, and the current repository file inventory
- the repo-local Impeccable skill, its `audit` reference, and its product register

Validation performed without editing the product:

- All current `ui/*.py` files parsed successfully as Python ASTs.
- The Streamlit application started successfully on loopback using the documented `agent` conda environment and Streamlit 1.59.2, then was stopped without starting a proof run.
- The ordinary shell Python environment does not contain Streamlit, confirming that the current UI still depends on the hard-coded conda setup.
- No React/Vite/TypeScript package, FastAPI app, frontend tests, Playwright tests, or CI workflow exists in the tracked tree.

This audit did not execute a proof pipeline, mutate a run directory, or perform an assistive-technology/browser-DOM pass. Streamlit owns its generated HTML and CSS, so contrast, focus order, and exact ARIA behavior cannot be fully established from Python source alone. Those points are marked as unverified rather than asserted as failures.

`PRODUCT.md` and `DESIGN.md` do not exist. Per Impeccable's scoped-audit flow, their absence did not block this audit. `$impeccable init` remains a required first implementation step. The local Impeccable consent file records accepted hook consent, but it is excluded locally and there is no tracked detector CI integration.

## Current frontend inventory

### Repository state

The tracked frontend is Streamlit-only:

```text
ui/
├── app.py
├── config_panel.py
├── process_manager.py
├── progress_monitor.py
├── requirements.txt
├── utils.py
└── archived/
    ├── app.py
    ├── config_panel.py
    ├── process_manager.py
    ├── progress_monitor.py
    ├── requirements.txt
    ├── utils.py
    └── .run_config.yaml
```

The current dependency surface is only `streamlit`, `streamlit-autorefresh`, and `pyyaml` (`ui/requirements.txt:1-3`). There are no custom styles, tokens, components, theme files, or frontend assets. `.gitignore:310` ignores the entire `docs/` directory, which conflicts with the requested tracked research records and must be corrected during the rewrite.

### Current user experience

The application is one wide Streamlit page with a configuration sidebar.

1. **Global provider configuration.** The sidebar exposes Claude, Codex, and Gemini defaults, authentication choices, model strings, API keys, CLI paths, reasoning/thinking controls, and unsafe permission/approval strings (`ui/config_panel.py:36-162`).
2. **Per-agent configuration.** Eight agent roles appear as repeated bordered containers with provider, optional model override, and provider-specific effort controls (`ui/config_panel.py:172-242`, `ui/config_panel.py:264-294`).
3. **Retry limits.** The user can change maximum proof attempts, plan revisions, and decompositions (`ui/config_panel.py:264-282`). There are no token, time, cost, call, parallelism, search, network, or sandbox budgets.
4. **Run input.** The main pane accepts an arbitrary output-directory string, can load `problem.tex` and human-help files from that directory, and provides editors for the LaTeX problem, prove guidance, and verification rules (`ui/app.py:103-183`).
5. **Run controls.** Run, Stop, and Resume From controls sit beside a textual status summary (`ui/app.py:190-262`). Input fields are disabled while the in-session process is active.
6. **Run start.** Starting creates the chosen directory, writes the input and guidance files, writes a root-level active YAML configuration, and launches `run.sh` in a background process group (`ui/app.py:269-286`, `ui/process_manager.py:28-56`).
7. **Stop.** Stop sends `SIGTERM` to the process group, waits five seconds, then sends `SIGKILL` if necessary (`ui/process_manager.py:59-74`). There is no durable cancelling/cancelled transition.
8. **Resume.** The UI enumerates filesystem-derived rewind points. Selecting one and pressing Resume deletes artifacts at and after that point, then starts the shell pipeline again (`ui/app.py:305-336`, `ui/process_manager.py:95-250`). There is no second confirmation, transaction, retained branch, or immutable candidate.
9. **Progress.** While the session thinks a process is active, `streamlit-autorefresh` reruns the page every four seconds (`ui/app.py:79-89`). Progress is inferred from file and directory presence, parsed Markdown, and token JSON (`ui/progress_monitor.py:42-154`).
10. **Run detail.** The page displays smoke-test status, a configuration snapshot, a three-stage indicator, attempts/difficulty/token totals, status/history, literature outputs, nested attempt/revision/proof expanders, verification reports, logs, final proof, failure analysis, summary, and token usage (`ui/progress_monitor.py:197-589`).

The README does not exactly match the implementation. It describes tabs, editing `config.yaml` in place, streaming logs, a Start button, and automatic continuation from the last checkpoint (`README.md:217-241`). The code uses a sidebar and single page, writes a separate active config, polls rather than streams, labels the button Run, and asks the user to choose a destructive rewind point.

### Current data sources

| Data | Current source | Current consumer | Production disposition |
|---|---|---|---|
| Problem draft | Streamlit session state; `<output>/problem.tex` | editor and pipeline | Store as a versioned typed draft/run input; import legacy file once. |
| Prove guidance | Streamlit session state; `<output>/human_help/additional_prove_human_help_global.md` | editor and agents | Version with the run input; retain exact text and hash. |
| Verification rules | Streamlit session state; `<output>/human_help/additional_verify_rule_global.md` | editor and verifier | Version with the run input; retain exact text and hash. |
| Configuration defaults | root `config.yaml`, cached once per Streamlit session | sidebar | Replace with typed server configuration and capability response. |
| Active configuration | root `.config_run_active.yaml` | shell process | Delete this singleton mechanism. Persist a redacted immutable run-config snapshot in SQLite. |
| Process identity | `subprocess.Popen` in `st.session_state` | Run/Stop status | Replace with durable run/execution records and a supervised worker handle. |
| Smoke-test progress | parsed `<output>/pipeline_stdout.log` | blocking progress gate | Replace with structured preflight events and findings. |
| Literature | `related_info/*.md`, survey logs | expanders | Store structured evidence and artifacts; preserve imported legacy files. |
| Current stage | inferred files plus `decomposition/STATUS.md` | status header/stage boxes | Store explicit state-machine status and append-only events. |
| Attempt/revision/proof tree | numbered directories | nested expanders and resume picker | Model attempts, plans, candidates, and execution lineage in SQLite. |
| Verification | Markdown files and string verdict parsing | proof expanders/resume | Store schema-validated reports and proof-step links; compute PASS in code. |
| Timeline | `*.history`, `log.txt`, raw stdout logs | code blocks/expanders | Store sequenced structured events; retain logs as artifacts. |
| Token/time metrics | `token_usage.json`, `TOKEN_USAGE.md` | four metrics and artifact | Store per-call typed metrics and aggregate server-side. |
| Final proof/summary | `proof.md`, `proof_effort_summary.md` | final-output expanders | Store selected sealed candidate plus export artifacts and manifest. |

### Current process lifecycle

```text
Streamlit session
  -> writes arbitrary filesystem paths and a shared active YAML file
  -> starts bash run.sh via Popen
  -> run.sh runs smoke_test.py in conda env `agent`
  -> pipeline.py and decomposition_prover.py write Markdown/YAML/JSON files
  -> Streamlit reruns every 4s and rescans those files
  -> Stop sends signals to the in-memory process group
  -> Resume deletes later files and relaunches the shell pipeline
```

Important lifecycle consequences:

- A browser refresh or new Streamlit session loses the only `Popen` reference and `run_active` flag. The child may continue, but the new session cannot reliably identify or cancel it. The README explicitly warns users not to refresh (`README.md:239`).
- `.config_run_active.yaml` is a shared singleton across sessions and runs (`ui/utils.py:18`, `ui/process_manager.py:39-49`). Concurrent starts can overwrite each other's inputs.
- Stop can interrupt non-atomic writes and has no persisted acknowledgement. The next state is inferred from whatever files remain.
- Resume is really destructive rewind. `_rm` can recursively delete directories, and the base path is the arbitrary string entered in the browser (`ui/process_manager.py:157-250`).
- Completion is inferred from a summary file, an Easy-path special case, status strings, and file presence rather than one authoritative state transition (`ui/utils.py:247-292`, `ui/progress_monitor.py:140-152`).
- Candidate proof content is copied to the top-level `proof.md` before independent verification is complete (`code/decomposition_prover.py:380-386`), so that filename cannot itself prove finality.

## Impeccable technical audit

The score reflects implementation evidence, not a rendered visual-design critique.

| # | Dimension | Score | Key finding |
|---|---|---:|---|
| 1 | Accessibility | 2/4 | Standard Streamlit controls have visible labels, but dynamic progress, dense nested disclosure, mathematical semantics, focus behavior, and WCAG AA conformance are unverified. |
| 2 | Performance | 1/4 | A four-second full-page rerun repeatedly scans and reads an ever-growing artifact tree and log files. |
| 3 | Responsive design | 1/4 | The app opts into wide layout and dense sidebar/multi-column structures with no explicit small-screen strategy or tests. |
| 4 | Theming | 0/4 | There is no product theme, token system, semantic state palette, or documented visual system. |
| 5 | Anti-patterns | 3/4 | It avoids decorative AI slop, but remains stock Streamlit with repeated bordered agent containers, metric boxes, and deeply nested expanders. |
| **Total** |  | **7/20** | **Poor — major overhaul required** |

### Anti-pattern verdict

**Does it look AI-generated? Pass, narrowly.** The source does not contain gradient text, glassmorphism, decorative grid backgrounds, side-stripe callouts, sketch SVGs, excessive radii, ornamental motion, or arbitrary z-index values. It looks like an unstyled Streamlit operations page rather than an AI-generated visual concept.

It nevertheless fails the product-register trust test. A researcher fluent in serious technical tools would encounter generic configuration repetition, raw filenames as navigation, nested expanders as the principal information architecture, and lifecycle controls whose meaning is not durable. The failure is not visual excess; it is insufficient product structure.

### Findings by severity

Issue count: **3 P0 / 8 P1 / 6 P2 / 0 P3**.

#### P0 — blocking

##### F-01: Arbitrary path control reaches recursive deletion

- **Location:** `ui/app.py:107-149`, `ui/app.py:274-280`, `ui/process_manager.py:157-250`
- **Category:** Security / hardening
- **Impact:** A user who can reach the UI can make the process read and write named files under any accessible directory and can trigger recursive deletion of matching `decomposition`, `related_info`, `human_help`, `tmp`, attempt, revision, or proof directories. A typo can also destroy a valid run branch.
- **Standard:** Production least privilege; CWE-22/path traversal and CWE-73/external control of path concerns.
- **Recommendation:** Remove filesystem paths from the browser contract. The server allocates opaque run IDs under one configured data root, resolves and containment-checks every artifact by ID, rejects symlinks, and never implements resume by deletion.
- **Suggested command:** `$impeccable harden`

##### F-02: Credentials are persisted, copied, printed, and displayed

- **Location:** `ui/config_panel.py:53-99`, `ui/config_panel.py:128-162`, `ui/process_manager.py:35-40`, `code/pipeline.py:709-712`, `code/pipeline.py:737-751`, `ui/progress_monitor.py:280-297`
- **Category:** Security / privacy
- **Impact:** API keys entered as password widgets are still placed in the assembled dict, written to `.config_run_active.yaml`, copied to `config_used.yaml`, printed into redirected pipeline stdout, and exposed by the Configuration used expander. Output directories need not be ignored or private.
- **Standard:** Secret minimization and log redaction; CWE-312/cleartext storage of sensitive information.
- **Recommendation:** Credentials stay in server-side environment/credential storage and are referenced by non-secret profile ID. Redact typed run snapshots before persistence, structurally redact logs, and regression-test every artifact/export for secret values.
- **Suggested command:** `$impeccable harden`

##### F-03: The UI is an unauthenticated execution control plane for unsafe agents

- **Location:** no authentication or authorization layer exists in `ui/`; `ui/process_manager.py:48-54`; `code/model_runner.py:287-306`; `README.md:606-614`
- **Category:** Security
- **Impact:** If the Streamlit server is bound beyond trusted loopback, anyone who reaches it can start an agent process whose current Codex path bypasses approval and sandbox controls and has the selected output directory as its working directory.
- **Standard:** Authentication, authorization, least privilege, secure defaults.
- **Recommendation:** FastAPI must authenticate every request, authorize run access, enforce capability-based sandbox policies server-side, and ship in local-only mode unless deployment authentication is configured. Never expose executable paths or free-form approval modes.
- **Suggested command:** `$impeccable harden`

#### P1 — major

##### F-04: Browser session state owns run control

- **Location:** `ui/app.py:61-89`, `ui/app.py:282-298`, `README.md:239`
- **Category:** Reliability / hardening
- **Impact:** Refreshing or reconnecting can orphan control of a still-running process, misreport it as idle, permit a conflicting second launch, and make cancel unavailable.
- **Standard:** Durable lifecycle and reconnect-safe control.
- **Recommendation:** Persist run and execution state in SQLite, supervise workers outside request/session memory, and reconstruct the UI entirely from `GET /runs/{id}` plus durable events.
- **Suggested command:** `$impeccable harden`

##### F-05: Runs are not isolated or concurrency-safe

- **Location:** `ui/utils.py:18`, `ui/process_manager.py:35-49`, `ui/app.py:107-127`
- **Category:** Reliability / security
- **Impact:** A single root active-config file and user-chosen output directory allow sessions to race or collide. There is no idempotency key, lock, owner, version check, or duplicate-start guard.
- **Standard:** Multi-user isolation and optimistic concurrency control.
- **Recommendation:** Give every run and execution a server-issued ID, owner/scope, state version, managed directory, transaction boundary, and idempotent command records.
- **Suggested command:** `$impeccable harden`

##### F-06: Resume destroys provenance instead of creating lineage

- **Location:** `ui/process_manager.py:95-150`, `ui/process_manager.py:167-250`, `ui/app.py:308-336`
- **Category:** Interaction / data integrity
- **Impact:** Resuming can erase later attempts, verification, summaries, token history, logs, and the top-level proof. The UI cannot compare the abandoned branch or audit why it was replaced.
- **Standard:** Recoverability, provenance, and user confirmation for destructive actions.
- **Recommendation:** Resume from the last durable checkpoint by appending a new execution segment. Forking from an earlier checkpoint creates a new run lineage and preserves the original. Sealed candidates are immutable.
- **Suggested command:** `$impeccable harden`

##### F-07: Files and free-form strings act as the state machine

- **Location:** `ui/utils.py:103-134`, `ui/utils.py:192-285`, `ui/progress_monitor.py:84-154`, `code/decomposition_prover.py:101-120`, `code/decomposition_prover.py:261-277`, `code/decomposition_prover.py:638-681`
- **Category:** Reliability / information architecture
- **Impact:** Partial writes, stale files, formatting variation, or ambiguous model text can change status, verdict, and resume behavior. The UI cannot distinguish authoritative state from a legacy artifact.
- **Standard:** Typed state, schema validation, and deterministic decisions.
- **Recommendation:** Expose Pydantic models for state, evidence, candidates, reports, and events. Compute transitions and final PASS in backend code; render Markdown only as an artifact.
- **Suggested command:** `$impeccable harden`

##### F-08: No API or event contract separates UI from orchestration

- **Location:** direct imports in `ui/app.py:16-29`; filesystem calls throughout `ui/`; direct shell launch in `ui/process_manager.py:28-56`
- **Category:** Architecture / performance
- **Impact:** The UI cannot be independently tested, secured, deployed, reconnected, or evolved. A React port that preserves these calls would merely relocate the coupling.
- **Standard:** Typed boundary and least-authority client.
- **Recommendation:** Establish the FastAPI/OpenAPI and event-envelope contracts before building feature screens. Generate or verify TypeScript types from the backend schema in CI.
- **Suggested command:** `$impeccable shape`

##### F-09: Dynamic and mathematical accessibility is not designed or verified

- **Location:** status updates in `ui/app.py:239-262`; nested dynamic content in `ui/progress_monitor.py:304-379` and `ui/progress_monitor.py:409-557`; Markdown proof rendering in `ui/progress_monitor.py:534-551`
- **Category:** Accessibility
- **Impact:** Screen-reader users may not learn about stage changes, may face a long disclosure hierarchy, and have no guaranteed semantic MathML, proof-step anchors, skip navigation, focus restoration, or paused auto-follow behavior.
- **WCAG/standard:** WCAG 2.2 AA, especially 1.3.1, 2.1.1, 2.4.3, 2.4.7, 4.1.2, and 4.1.3.
- **Recommendation:** Define semantics and keyboard behavior per target surface, use polite live summaries rather than announcing every event, render accessible mathematics, and run automated plus manual assistive-technology tests.
- **Suggested command:** `$impeccable audit`

##### F-10: The layout has no explicit responsive model

- **Location:** `ui/app.py:50-54`, `ui/app.py:111-125`, `ui/app.py:192-214`; `ui/progress_monitor.py:304-343` and `ui/progress_monitor.py:421-428`
- **Category:** Responsive design
- **Impact:** A wide page, dense sidebar, four-control row, metric row, and nested three-column statuses can become cramped or reorder poorly on tablets, narrow windows, zoomed text, and mobile devices.
- **WCAG/standard:** WCAG 1.4.10 Reflow and 1.4.4 Resize Text; 44×44 CSS-pixel target policy for touch controls.
- **Recommendation:** Use structural breakpoints, one-column narrow layouts, a drawer for inspectors, mobile candidate switching instead of side-by-side comparison, and a list fallback for the graph.
- **Suggested command:** `$impeccable adapt`

##### F-11: Required research decisions have no first-class surface

- **Location:** the only progress surfaces are metrics, status Markdown, artifact expanders, and the attempt tree (`ui/progress_monitor.py:304-557`)
- **Category:** Information architecture
- **Impact:** Users cannot compare candidates, trace evidence to claims, inspect proof-linked findings, understand agent concurrency, seal a candidate, or verify/export a reproducible result. Raw files force users to reconstruct those relationships mentally.
- **Standard:** Product task completion and traceability.
- **Recommendation:** Build dedicated stage graph, event timeline, candidate comparison, evidence ledger, findings, and artifact/export views around stable IDs and typed relationships.
- **Suggested command:** `$impeccable shape`

#### P2 — minor but material

##### F-12: Polling repeatedly performs unbounded synchronous scans

- **Location:** `ui/app.py:79-89`, `ui/app.py:239-241`, `ui/progress_monitor.py:84-154`, `ui/progress_monitor.py:161-193`, `ui/progress_monitor.py:564-589`
- **Category:** Performance
- **Impact:** Active pages can scan twice per rerun, read the whole stdout log for smoke status, traverse every attempt/revision/proof, and load large Markdown artifacts. Cost grows with the research history and delays the page that is meant to monitor it.
- **Recommendation:** Stream compact events, page/virtualize long collections, lazy-load artifact bodies, server-filter tables, and keep run snapshots bounded.
- **Suggested command:** `$impeccable optimize`

##### F-13: Configuration hierarchy is repetitive and exposes implementation jargon

- **Location:** `ui/config_panel.py:172-242`, `ui/config_panel.py:249-321`
- **Category:** Clarity / information architecture
- **Impact:** Users must parse repeated provider cards, raw role names, snake-case retry labels, CLI paths, and provider-specific controls before they can start a run. Important safety/budget choices are not distinguished from advanced overrides.
- **Recommendation:** Lead with a concise run preset, model/effort, parallelism, budgets, search, and sandbox policy. Reveal per-role overrides progressively and explain their effect in research terms.
- **Suggested command:** `$impeccable clarify`

##### F-14: There is no design system or semantic state vocabulary

- **Location:** no CSS/theme/token files in the tracked UI; framework defaults are used throughout.
- **Category:** Theming / consistency
- **Impact:** Loading, selected, warning, verified, rejected, sealed, and disabled states cannot be made consistent across a future multi-view console without first defining tokens and component contracts.
- **Recommendation:** Create `PRODUCT.md`, `DESIGN.md`, OKLCH tokens, type/spacing scales, semantic statuses, z-index layers, and complete component states before screen polish.
- **Suggested command:** `$impeccable init`

##### F-15: Loading, empty, error, and reconnect states are incomplete

- **Location:** `ui/progress_monitor.py:197-222`, `ui/progress_monitor.py:568-579`; exception swallowing in `ui/config_panel.py:22-29` and `ui/utils.py:68-96`
- **Category:** Hardening / onboarding
- **Impact:** Users receive generic messages or missing sections, cannot distinguish no data from a read failure, and have no recovery instructions for stale streams, schema mismatches, rejected commands, or partial exports.
- **Recommendation:** Specify every asynchronous state, preserve the last good snapshot, expose actionable retry/details, teach empty views, and never erase content just because a refresh failed.
- **Suggested command:** `$impeccable harden`

##### F-16: Documentation describes behavior the UI does not provide

- **Location:** `README.md:217-241`, `README.md:598-603`
- **Category:** Clarity
- **Impact:** Operators form incorrect expectations about tabs, config persistence, streaming, and resume safety.
- **Recommendation:** Rewrite run commands and workflows at cutover, and test documentation commands in CI.
- **Suggested command:** `$impeccable clarify`

##### F-17: There is no frontend or browser test safety net

- **Location:** no tracked frontend package/test files or CI workflows; `code/smoke_test.py` is a runtime preflight rather than a UI test suite.
- **Category:** Quality
- **Impact:** Keyboard behavior, responsive layouts, event reconnection, lifecycle controls, visual regressions, and malicious artifact rendering can regress without detection.
- **Recommendation:** Add unit, component, contract, accessibility, Playwright, security, and performance tests described below; use mocked Codex by default.
- **Suggested command:** `$impeccable audit`

### Patterns and systemic issues

- **Filesystem paths are both identifiers and authority.** They identify a run, grant read/write scope, determine resume behavior, and feed UI labels.
- **Rendered artifacts substitute for domain data.** Markdown filenames encode stage, candidate, verification, and verdict relationships that should be explicit records.
- **Session state substitutes for process supervision.** Reload and multi-user behavior are consequently undefined.
- **Provider implementation details dominate configuration.** Research intent and safety budgets are secondary.
- **Expanders substitute for information architecture.** They work for occasional detail, not for comparing and tracing many related research objects.
- **There is no stable design vocabulary.** The future state-rich product needs semantic tokens and complete component states before visual polish.

### Positive findings to preserve

- Standard Streamlit inputs have explicit visible labels, and controls use disabled states during the in-session run (`ui/app.py:113-124`, `ui/app.py:157-181`, `ui/app.py:195-213`).
- Status messages include text such as Running, Pending, Done, Failed, and Complete rather than relying on color alone (`ui/app.py:239-262`, `ui/progress_monitor.py:304-324`).
- The UI makes the problem, prove guidance, and verification rules separate concepts; that separation belongs in the target domain.
- The attempt → revision → proof hierarchy, stage indicator, and proof-level verification artifacts provide a useful migration vocabulary.
- Some displayed logs are intentionally tailed to 20,000 bytes/80 lines (`ui/progress_monitor.py:229-244`).
- The intention to snapshot the exact run configuration and track per-call tokens/time is correct; only the secret handling and storage model need replacement.
- No `unsafe_allow_html`, custom HTML injection, decorative animation, or bespoke widget reinvention appears in the tracked Streamlit UI.

## Target product definition

### Product scene and visual register

Physical scene: **A mathematician works for hours at a bright desk, moving between a dense proof, citations, and independent verification while needing every status and provenance claim to remain calm, legible, and exact.**

This calls for a light-first, restrained product interface—not a dark terminal aesthetic and not an editorial landing page. The UI should feel like a blue-pencil proof desk: a true white working surface, quiet cool neutrals, and deep cobalt used only for primary actions, selection, and active state.

The repo-local Impeccable palette seed for the future design system is `oklch(0.340 0.159 262.4)`. A suitable starting palette for `DESIGN.md` is:

```css
:root {
  --color-bg: oklch(1 0 0);
  --color-surface: oklch(0.975 0.006 262);
  --color-surface-raised: oklch(0.99 0.003 262);
  --color-ink: oklch(0.20 0.025 262);
  --color-muted: oklch(0.43 0.025 262);
  --color-primary: oklch(0.34 0.159 262.4);
  --color-primary-ink: oklch(0.98 0.005 262);
  --color-accent: oklch(0.90 0.075 170);
  --color-accent-ink: oklch(0.22 0.04 170);
}
```

These are starting roles, not approved implementation tokens. The actual token pairs, including every semantic status and disabled state, must be measured for WCAG AA contrast in both browser tests and the Impeccable audit. Do not add a dark theme until product context explicitly requires one and the full semantic mapping can be tested; token architecture should not be mistaken for a promise to ship two incomplete themes.

Use one familiar sans/system family for product UI and tabular numerals for metrics. Mathematical content may use the math renderer's glyphs, but controls, labels, tables, and navigation stay in the product type family. Body prose should remain within 65–75 characters; proof and data views may be denser where structure requires it.

### Core product principles

1. **The run is durable; the browser is a view.** Reloading never changes execution ownership or truth.
2. **Structured data first; artifacts second.** Proofs, findings, evidence, verdicts, metrics, and state are typed records. Markdown is an export/preview.
3. **Immutability earns trust.** Sealed candidates and verifier inputs never change. A retry or fork creates lineage.
4. **Every claim is traceable.** Evidence and findings link to stable proof-step IDs and provenance hashes.
5. **Controls reflect server capability.** Unsupported model efforts, sandbox modes, or parallelism values never appear as writable strings.
6. **Density without decoration.** Prefer tables, split panes, ordered timelines, and clear dividers over repeated cards and nested containers.
7. **Live updates do not steal attention.** Users can pause auto-follow, navigate with a keyboard, and inspect a stable snapshot while events continue.

## Information architecture

Do not invent a project/team abstraction until the domain needs one. The smallest serious architecture is run-centered:

```text
/runs
  Run library: search, status, created/updated, model, elapsed, tokens, outcome

/runs/new
  Problem + prove guidance + verification rules
  Run configuration + budgets + capability validation
  Review immutable input snapshot, then Start

/runs/:runId/overview
  Persistent run header and controls
  Live stage/agent graph
  Budget/time/token summary
  Current decisions, failures, and next required action

/runs/:runId/timeline
  Sequenced, filterable, virtualized events with auto-follow control

/runs/:runId/candidates
  Candidate list, comparison, stable proof-step anchors, verification matrix

/runs/:runId/evidence
  Evidence ledger, citations, hashes, claim/proof-step usage, verification state

/runs/:runId/findings
  Proof-linked verifier findings, severity, status, agent, candidate, adjudication

/runs/:runId/artifacts
  Typed artifact inventory, lineage, preview/download, manifest/export
```

### Application shell

- A persistent top bar contains product identity, run title/ID, run status, connection status, and context-appropriate commands.
- A compact left navigation switches the run views and shows stage progress. It collapses to a drawer at medium widths and a simple route switcher on narrow screens.
- The center is the task surface. It should not be a grid of decorative cards.
- A right inspector is allowed only when an object is selected and detail materially helps. It becomes a drawer at medium/narrow widths.
- Run status and connection status are distinct. A disconnected browser does not imply a stopped run.

### New-run editor

The creation view should keep four inputs visible and conceptually separate:

1. **Problem statement.** LaTeX-aware plain-text editor, validation summary, character/size limit, and preview. Preserve exact source; preview is never the stored authority.
2. **Prove guidance.** Optional strategies and constraints for planning/proving.
3. **Verification rules.** Explicit hard requirements passed only to verifiers as defined by the backend contract.
4. **Run configuration.** Model, supported effort, parallelism, retry/call/token/time budgets, search/network policy, and sandbox policy. Per-role overrides are an Advanced disclosure, not eight always-visible cards.

Starting a run shows the immutable snapshot and validation result inline. It must not ask for an output path, CLI path, API key, approval string, or undocumented capability. A draft can be saved without starting, but autosave should remain local/server-draft behavior and must not silently mutate a running input.

### Run overview and live graph

The overview should answer, in order:

1. Is the run executing, stopped, retryable, failed, or complete?
2. What stage and agents are active now?
3. Are budgets healthy, near limit, or exceeded?
4. What candidates exist, which are sealed, and which are under independent verification?
5. Is user action needed?

Represent the pipeline as a real DAG only where dependency edges carry information: literature → planning → candidate generation → sealing → independent verification → adjudication → export. Nodes contain a text status and concise metrics. Provide the same information as an ordered accessible list/table; the graphical canvas is never the only route to status. Agent concurrency appears within stage nodes or a focused inspector rather than an ornamental swarm animation.

### Event timeline

- Events are ordered by durable sequence number, with timestamp as secondary display data.
- Filters: stage, agent/thread, candidate, severity, event kind, and text.
- Each row has a concise summary and expandable structured detail; raw payloads are an expert/debug affordance.
- Auto-follow is explicit and pauses when the user scrolls away or selects content.
- New events update a polite summary such as “3 new events” instead of announcing every token/tool event to assistive technology.
- Long timelines are virtualized and server-paginated. Reload resumes from the last acknowledged event while separately fetching the current snapshot.

### Candidate comparison

- Candidate identity and version are stable; sealed candidates cannot be edited.
- Wide screens compare two candidates side by side, with optional additional candidates summarized in a matrix. Narrow screens switch one candidate at a time with a difference/findings lens; do not squeeze columns.
- Proofs are decomposed into stable step IDs. Findings, citations, evidence, and verifier decisions link to those IDs.
- Comparison supports synchronized step navigation, structural outline, changed claims/assumptions, evidence coverage, verifier outcome, and token/time cost.
- Raw Markdown diff is secondary; mathematical meaning and verification are not inferred from text color alone.

### Evidence ledger

Use a dense table with a detail inspector. Required fields include evidence ID, source type, title/citation, exact supported statement, locator/URL where allowed, retrieval time, SHA-256 provenance, literature thread, validation status, and proof-step usages. Filters should expose unverified, unused, contradicted, or missing evidence. Links opened externally must be clearly labeled and safely isolated.

### Proof-linked findings

Each finding needs a typed severity/status, verifier/thread, candidate ID, proof-step range, claim, evidence, recommendation, and adjudication. Selecting a finding scrolls/focuses the proof anchor without losing the list position. Selecting a proof step shows its findings and evidence. “No findings” must distinguish not yet verified, verifier running, verifier failed, and verified with zero findings.

### Artifacts and export

Artifacts are addressed by ID, never a browser-provided filesystem path. Show type, immutable/version state, MIME type, size, created time, producer/thread, SHA-256, and lineage. Preview only supported safe formats; download others with safe content disposition. Export produces proof, report, evidence/finding data, redacted configuration, version metadata, and manifest as one reproducible bundle whose hashes are visible before download.

## State model exposed to the UI

### Run states

```text
DRAFT
  -> QUEUED
  -> STARTING
  -> RUNNING
  -> CANCELLING -> CANCELLED
  -> FAILED_RETRYABLE -> QUEUED (resume creates a new execution segment)
  -> FAILED_FINAL
  -> COMPLETED
```

Every mutation is idempotent and checked against an expected state/version. `CANCELLING` remains visible until the worker acknowledges or a timeout escalates. Resume never erases the previous execution. Forking an older checkpoint creates a new run with explicit parent lineage.

### Stage, agent, and candidate states

- Stages: `pending`, `running`, `succeeded`, `failed`, `skipped`, `cancelled`.
- Agent/thread activity: `queued`, `running`, `waiting`, `retrying`, `succeeded`, `failed`, `cancelled`.
- Candidates: `generating`, `draft`, `sealed`, `verifying`, `verified`, `rejected`, `selected`.
- Exports: `not_requested`, `building`, `ready`, `failed`.

State labels shown to users should be sentence case and explained in context. Color is redundant with text/icon/shape. The frontend never manufactures terminal state from an absent event or filename.

### Required async UI states

Every data surface and interactive component must define:

- initial loading skeleton
- loaded with data
- loaded empty with a teaching next step
- stale snapshot while reconnecting
- recoverable fetch/command failure with Retry
- terminal failure with diagnostic reference
- unauthorized/forbidden/not found
- unsupported capability or schema version
- default, hover, focus-visible, active, selected, disabled, and in-progress interaction states

Do not blank the last good snapshot during reconnect. Do not use a centered spinner as the entire page.

## FastAPI and React boundary

Define backend Pydantic models first and expose them through versioned OpenAPI. At minimum:

- `Capabilities`
- `ProblemDraft` / `RunInputSnapshot`
- `RunConfig` / `RedactedRunConfig`
- `RunSummary` / `RunSnapshot`
- `Stage`, `AgentThread`, and `ExecutionSegment`
- `RunEvent`
- `Candidate` and `ProofStep`
- `EvidenceItem`
- `Finding` and `VerificationReport`
- `Artifact` and `ExportManifest`
- command request/acknowledgement/error envelopes

Minimum endpoint shape:

```text
GET    /api/v1/capabilities
GET    /api/v1/runs
POST   /api/v1/runs
GET    /api/v1/runs/{run_id}
POST   /api/v1/runs/{run_id}/commands/start
POST   /api/v1/runs/{run_id}/commands/cancel
POST   /api/v1/runs/{run_id}/commands/resume
GET    /api/v1/runs/{run_id}/events
GET    /api/v1/runs/{run_id}/candidates
GET    /api/v1/runs/{run_id}/evidence
GET    /api/v1/runs/{run_id}/findings
GET    /api/v1/runs/{run_id}/artifacts
POST   /api/v1/runs/{run_id}/exports
```

Collections must be paginated/filterable. Artifact content/download is a separate authorized endpoint. Command requests carry an idempotency key and expected state version. Errors use stable machine codes plus safe human messages and a diagnostic ID.

The TypeScript client types should be generated from or mechanically checked against OpenAPI/JSON Schema. React event reducers must be exhaustive over event kinds; unknown future events are retained for diagnostics but do not crash or invent state.

In production, FastAPI should serve the built SPA or share one origin through a reverse proxy. Vite's development proxy is development-only. Same-origin deployment simplifies credential handling, CSP, CSRF, and SSE authorization.

## SSE versus WebSocket

### Recommendation: SSE

The required live traffic is overwhelmingly one-way: stage changes, agent/thread activity, timeline entries, token/time updates, candidates, findings, and artifact readiness flow from server to browser. Start, cancel, resume, seal, and export are discrete, auditable commands and belong in ordinary HTTP requests.

Use one run-scoped SSE stream:

```text
GET /api/v1/runs/{run_id}/events
Accept: text/event-stream
Last-Event-ID: <durable sequence>
```

Each event envelope should contain:

```json
{
  "schema_version": 1,
  "run_id": "run_...",
  "sequence": 1842,
  "occurred_at": "...",
  "kind": "candidate.sealed",
  "stage_id": "verification",
  "thread_id": "...",
  "candidate_id": "...",
  "payload": {}
}
```

Requirements:

- Persist the event before publishing it.
- Replay events after `Last-Event-ID`; deduplicate by sequence in the client.
- Send heartbeat comments, detect stale connections, and reconnect with bounded jitter.
- Authorize the run before opening the stream and stop it when authorization expires.
- Keep a single stream per active run tab, not one per widget/agent.
- Bound event and payload size; large logs/artifacts are referenced by ID and fetched separately.
- Coalesce high-frequency metric deltas when necessary; token-by-token prose does not belong in the research timeline.
- After reconnect, fetch a fresh run snapshot even if event replay succeeds. Events provide continuity; the snapshot proves current state.

Native `EventSource` cannot attach arbitrary authorization headers. Prefer secure same-origin HTTP-only cookies with CSRF protection on mutation requests. If deployment requires bearer headers, use a reviewed fetch-streaming SSE client rather than placing tokens in query strings.

### When to reconsider WebSocket

Choose WebSocket only if a validated product requirement adds sustained bidirectional interaction such as collaborative editing, interactive terminal input, or server-driven backpressure negotiation. None of the requested console behaviors needs that today. Introducing it now would add connection state, custom reconnection, message correlation, proxy configuration, and security surface without user value.

## Accessibility strategy

Target WCAG 2.2 AA and test it as a release gate.

### Structure and navigation

- Use `header`, `nav`, `main`, `aside`, and named regions with one page `h1`.
- Provide a skip link to the run work area and route-change focus management.
- Preserve logical DOM order when visual panels rearrange.
- Use real links for navigation and buttons for commands; no clickable `div` controls.
- Keep focus visible and never use color alone for stage, severity, verification, or connection state.

### Live behavior

- Announce coarse run/status changes through one polite live region.
- Do not announce every streamed event. Present an accumulated “new events” control.
- Never move focus on incoming data. Preserve selection and scroll while tables update.
- Make auto-follow opt-in after the user navigates away from the timeline tail.
- On accepted cancel/resume commands, focus a stable status message, not a disappearing button.

### Mathematics and proof links

- Render mathematics with semantic MathML where the selected renderer supports it and preserve a copyable LaTeX source alternative.
- Give equations and proof steps stable visible identifiers and keyboard-addressable anchors.
- Findings and evidence links must state the target step/claim, not “view”.
- Do not communicate insertions, deletions, or invalid steps by red/green color alone; include labels and accessible descriptions.

### Components and data density

- All form controls have persistent labels, help/error association, and required indicators.
- Tables use real headers, captions or accessible names, sortable-state announcements, and server pagination.
- The stage graph has a complete list/table equivalent and keyboard navigation if nodes are interactive.
- Dialogs are reserved for truly blocking decisions; prefer inline disclosure/confirmation. Any dialog must trap and restore focus correctly.
- Touch targets are at least 44×44 CSS pixels, while dense rows can use larger row hit areas without visually oversized controls.

### Verification

- Automated axe checks on every primary route/state.
- Keyboard-only Playwright journeys at desktop and narrow widths.
- Manual VoiceOver and NVDA passes for new-run creation, live status, graph alternative, candidate comparison, finding-to-proof navigation, cancel/resume, and export.
- Contrast checks for every token pairing, including placeholder, disabled, selected, chart/graph, and focus states.
- Tests at 200% zoom, increased text size, forced colors, and `prefers-reduced-motion`.

## Responsive strategy

Responsive behavior is structural, not fluid headline typography.

| Surface | Wide desktop | Medium/tablet/narrow desktop | Narrow/mobile |
|---|---|---|---|
| App shell | left nav + main + optional inspector | collapsed nav + main + inspector drawer | single main column + route/menu control |
| New run | editor and validation/config split | stacked sections with sticky review summary | single column; full-width editor; no side preview |
| Stage graph | full DAG plus summary | horizontally scrollable only if necessary, with list adjacent | ordered stage/agent list is primary |
| Timeline | multi-column virtualized rows | hide low-priority columns into detail | timestamp + summary; filters in drawer |
| Candidate compare | two synchronized proof panes + matrix | two panes if space allows, otherwise candidate switcher | one candidate plus diff/findings mode |
| Evidence/findings | table + inspector | priority columns + drawer | stacked semantic rows/definition lists, server pagination |
| Artifacts | table/tree + preview | table + preview drawer | list + dedicated preview route |

Test at content-driven breakpoints rather than naming devices. Long model names, filenames, theorem titles, URLs, unbroken LaTeX, translated labels, and 200% zoom must not cause horizontal page scroll. Allow intentionally scrollable code/equation regions with labels and keyboard access.

## Motion strategy

Motion communicates state and never delays work.

- Use 150–250 ms ease-out transitions for drawers, disclosure, selection, and status emphasis.
- A stage-node state change may briefly change background/border emphasis; do not pulse continuously.
- New timeline rows may use a restrained highlight that does not gate visibility.
- Do not animate layout dimensions for the main workbench, orchestrate page-load sequences, bounce, or animate every incoming agent event.
- Do not rely on animation for causal order; sequence numbers and labels carry meaning.
- Under `prefers-reduced-motion: reduce`, remove movement and use immediate state changes or a brief crossfade.
- Virtualized content and streamed updates must remain correct with all animation disabled.

## Performance strategy

- Render bounded snapshots; page evidence, findings, artifacts, and events on the server.
- Virtualize the timeline and any table proven to exceed the practical DOM budget.
- Lazy-load artifact bodies, syntax highlighting, proof diffs, and expensive math below the active view.
- Batch event reducer updates to a frame or modest interval under high-frequency streams without delaying critical status/cancel acknowledgement.
- Memoize only measured hot paths; stable IDs and normalized stores matter more than blanket `memo` use.
- Keep one mathematical renderer and one icon vocabulary; avoid overlapping component libraries.
- Establish bundle, initial-render, interaction, and 10,000-event timeline budgets in `DESIGN.md`; enforce them in CI with representative fixtures.
- Ensure stopping or navigating away closes the SSE connection and cancels obsolete fetches.

## Security requirements for the target UI/API

1. Browser requests refer only to opaque IDs. The server owns and containment-checks storage paths under one configured root.
2. Credentials never enter run input/config schemas returned to the client. Persist and display only redacted credential-profile references.
3. Authenticate all API and SSE access; authorize each run, candidate, artifact, and export.
4. Protect cookie-authenticated mutations against CSRF. Never put credentials in URLs, logs, SSE query strings, or artifact manifests.
5. Capability/sandbox fields are enums returned by the backend. Reject unsupported or unsafe values server-side; never accept CLI paths or shell fragments.
6. Sanitize rendered Markdown and links. Configure the math renderer in an untrusted mode, disable raw HTML by default, and enforce a restrictive CSP.
7. Download artifacts by database ID with safe MIME/type handling and `Content-Disposition`; do not reflect stored paths.
8. Redact secrets structurally before logging. Test adversarial values that are split, encoded, nested, or repeated in model output.
9. Commands are idempotent, state-versioned, rate-limited where appropriate, and fully audited with actor/time/result.
10. Cancellation, resume, sealing, adjudication, and export are backend decisions. UI optimistic state may show “requesting” but never claims a transition before acknowledgement.
11. Treat generated proof, citations, filenames, event text, and legacy artifacts as untrusted content.
12. Define retention/export/delete policy separately. “Delete run” is not resume and must require an explicit recoverability design.

## Test plan

### Backend and schema tests

- Pydantic validation for every request, event, state, candidate, evidence, finding, artifact, and manifest.
- Exhaustive legal/illegal state-transition tests, including duplicate/out-of-order commands.
- SQLite transaction tests for start, cancel acknowledgement/escalation, retry, resume, sealing, independent verification, adjudication, completion, and export.
- SSE persistence/replay tests for disconnect, `Last-Event-ID`, duplicate delivery, gap detection, heartbeat, authorization expiry, and bounded payloads.
- Path containment, symlink, traversal, authorization, CSRF, redaction, malicious Markdown/LaTeX, and artifact-download tests.
- Legacy-run importer tests using checked-in fixtures for Easy, active, cancelled, failed, complete, corrupt, and partially written directory trees.

### Frontend unit/component tests

- Capability-driven config: unsupported effort/sandbox fields never render and cannot be submitted.
- New-run validation for empty/oversized inputs, numeric budgets, server validation errors, and immutable review snapshot.
- Event reducer idempotency, unknown-event tolerance, out-of-order/gap handling, reconnect state, and snapshot reconciliation.
- Complete visual/interaction states for buttons, inputs, tables, navigation, status, graph nodes, drawers, and error boundaries.
- Candidate comparison, synchronized proof anchors, evidence usage, finding navigation, sealing state, and export readiness.
- Sanitization and safe external-link behavior.
- Component-level axe checks and keyboard contracts.

### FastAPI + React integration tests

Use a temporary SQLite database and a deterministic mocked Codex runtime by default. One fixture must prove the full requested lifecycle:

```text
create draft
  -> start
  -> stream literature/planning/candidate events
  -> cancel
  -> reload browser and observe CANCELLED
  -> resume from durable checkpoint without deleting history
  -> seal candidates
  -> run fresh read-only verifiers over frozen inputs
  -> compute adjudication/PASS in backend code
  -> export proof + report + manifest
  -> verify hashes and lineage in UI
```

Also test parallel candidates, verifier disagreement, retry-budget exhaustion, network disconnect during each command, process crash, API restart, malformed legacy artifact, and concurrent tabs issuing the same command.

### Playwright coverage

- Desktop, medium, and narrow viewport journeys for every primary route.
- Start/cancel/resume after full page reload.
- SSE interruption/replay and stale-snapshot banner.
- Keyboard-only navigation and focus restoration.
- 200% zoom, long content, reduced motion, forced colors, and high event volume.
- Candidate comparison and finding ↔ proof ↔ evidence deep links.
- Artifact preview/download and export manifest.
- Visual regression snapshots for default, loading, empty, running, cancelling, cancelled, failed, completed, disconnected, and unauthorized states.

### CI gates

- Python lint, typecheck, unit/integration/security tests.
- Frontend lint, TypeScript typecheck, unit/component tests, and production Vite build.
- OpenAPI/generated-TypeScript drift check.
- Playwright with mocked runtime.
- Repo-local Impeccable detector over actual frontend markup/styles, followed by `$impeccable audit` before release.
- Real-model smoke tests remain opt-in and never gate ordinary pull requests.

## Migration and deletion map

The React console is not a compatibility skin. Preserve user-visible behavior and legacy artifacts through typed import, then delete the Streamlit mechanisms.

| Existing artifact | Action | Cutover gate |
|---|---|---|
| `ui/app.py` | Delete. Replace page/session lifecycle with React routes and API state. | Full mocked lifecycle passes in Playwright. |
| `ui/config_panel.py` | Delete. Do not retain Claude/Gemini/provider dispatch or executable/approval fields. | Typed capability-driven Codex config is live. |
| `ui/process_manager.py` | Delete. Do not call it from FastAPI. | Durable worker supervision and idempotent cancel/resume tests pass. |
| `ui/progress_monitor.py` | Delete after extracting only legacy format knowledge into a one-shot importer. | Current and legacy fixtures import into typed records. |
| `ui/utils.py` | Delete after the importer/backend owns any necessary legacy parsing. | No runtime imports remain. |
| `ui/requirements.txt` | Delete with Streamlit dependencies. | Vite production build and FastAPI packaging replace it. |
| `ui/archived/` including `.run_config.yaml` | Delete in full; it is provider/Streamlit legacy, not migration code. | No gate beyond confirming no runtime import/reference. |
| `.config_run_active.yaml` and its singleton write path | Delete. | Per-run typed redacted config snapshots exist. |
| `config_used.yaml` as a raw config copy | Replace with redacted structured config plus manifest hash. Legacy importer treats old copies as sensitive. | Secret-redaction tests pass. |
| `pipeline_stdout.log` as UI state | Retain only as a legacy/debug artifact; never parse it for state. | Structured preflight/run events exist. |
| `STATUS.md`, `*.history`, `token_usage.json`, numbered directories | Preserve for legacy import/export; stop using them as runtime truth. | SQLite state/event/metric coverage is complete. |
| `ui/proof_runs/` ignore rule | Remove or replace with the new configured runtime-data policy. | Managed data root is documented and excluded intentionally. |
| `.gitignore:310` (`docs/`) | Remove or narrow so `docs/research/`, architecture, migration, and threat-model documents are tracked. | Required docs appear in `git status`/index. |
| README Streamlit instructions and UI file tree (`README.md:217-241`, `README.md:598-603`) | Rewrite for one-command backend/frontend setup and new run flows. | Documentation commands run successfully in CI. |
| `run.sh` as the web execution entry | Refactor away from web use; keep a tested CLI entry only if it calls the same application service. | FastAPI and CLI share the durable orchestration service. |

### Behavioral preservation map

| Preserve this intent | Replace this mechanism |
|---|---|
| Edit exact problem/guidance/rules | Streamlit session textareas → typed draft and immutable run input snapshot |
| Configure agents and retry limits | provider cards/free strings → capability-driven typed Codex config and budgets |
| Start a run | direct `Popen(run.sh)` → idempotent service command and supervised execution |
| See stage progress | file existence/Markdown status → durable state and SSE events |
| See attempts/revisions/proofs | numbered directory nesting → execution lineage, plans, immutable candidates |
| Stop | process signal held by page session → durable cancel command and acknowledgement |
| Resume | delete later files → append execution segment or lineage fork |
| Inspect proof/verifications/logs | nested raw-file expanders → candidate, findings, timeline, and artifact views |
| Track tokens/time | Markdown/JSON polling → typed per-call metrics and aggregates |
| Export proof | loose `proof.md` → proof/report/evidence/manifest bundle with hashes |

## Recommended implementation sequence

1. **Product and design contract.** Run `$impeccable init`; write `PRODUCT.md`, `DESIGN.md`, tokens, state vocabulary, responsive rules, and component contracts. Verify: review every requested surface and state against this audit.
2. **Typed backend seam.** Implement schemas, SQLite state machine, managed storage, mock runtime, REST commands, and SSE replay. Verify: backend transition/security/restart tests.
3. **Read-only React run explorer.** Build shell, run library, overview, timeline, and artifact views against fixtures/API. Verify: reload/reconnect/accessibility tests.
4. **Creation and lifecycle controls.** Add editors, capability-driven config, budgets, start/cancel/resume. Verify: full mocked start/cancel/reload/resume flow.
5. **Research decision surfaces.** Add stage/agent graph, candidate comparison, evidence ledger, proof-linked findings, sealing, adjudication, and export. Verify: stable links, frozen verifier inputs, computed PASS, manifest hashes.
6. **Production hardening.** Complete auth, secret redaction, CSP/sanitization, responsive behavior, reduced motion, performance budgets, manual accessibility, and error/reconnect states. Verify: security, Playwright, Impeccable, and performance gates.
7. **Cutover and deletion.** Import legacy fixtures, update docs, delete all Streamlit/provider UI and archived files, and remove obsolete dependencies/ignores. Verify: searches show no Streamlit or Claude/Gemini runtime UI, all CI gates pass, and the diff contains no compatibility adapter.

## Feasibility risks and decisions still needed

These are implementation dependencies, not blockers to the selected stack:

- The frontend cannot finalize event reducers or graph relationships until backend schemas and state transitions are fixed.
- Legacy directories may be partially written or internally contradictory. The importer must report uncertainty and preserve raw artifacts; it must not fabricate completed states.
- Authentication/deployment mode must be specified before production exposure. Safe default is loopback/local-only when no deployment auth is configured.
- Accessible mathematical rendering needs an explicit library/configuration decision and manual screen-reader validation.
- Large proof diffs and timelines require representative worst-case fixtures before budgets can be set.

No current requirement justifies WebSocket, collaborative editing, a project/team hierarchy, a dark theme, custom canvas interactions, or a general-purpose plugin system. Those should not be added speculatively.

## Recommended Impeccable actions

1. **P0 `$impeccable init`**: capture product scene, routes, tokens, semantics, and the approved detector hook before React implementation.
2. **P1 `$impeccable shape runs/new`**: validate the problem/guidance/rules/config hierarchy and capability-driven budgets.
3. **P1 `$impeccable shape runs/:runId`**: validate graph, timeline, comparison, evidence, findings, and artifact relationships before component work.
4. **P1 `$impeccable harden`**: cover lifecycle, reconnect, errors, authorization, redaction, empty/loading states, and destructive-action boundaries.
5. **P1 `$impeccable adapt`**: verify structural breakpoints, 200% zoom, long mathematics, and narrow comparison behavior.
6. **P1 `$impeccable audit`**: run code/browser accessibility, performance, responsive, theming, and anti-pattern checks against the built UI.
7. **P2 `$impeccable polish`**: final consistency and craft pass after all functional/security gates are green.

Re-run `$impeccable audit` after fixes to measure improvement.
