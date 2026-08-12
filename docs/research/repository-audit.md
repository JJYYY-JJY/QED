# QED pre-rewrite repository audit

> Historical / non-runtime research record. Commands and recommendations in
> this note are not current stable release gates.

Audit date: 2026-07-16 (America/Los_Angeles)
Audited revision: `7dccd9be9f900aeae31ab0a0473ce7bd3e4ec35d`
Scope: the complete tracked tree at that revision plus the current untracked/ignored Impeccable setup. This is a read-only assessment of the legacy implementation; this note is the only file created by the audit.

## Executive conclusion

The repository is a small, sequential, file-driven prototype, not a production state machine. Its valuable assets are the upstream identity and MIT license, the full prompt corpus and mathematical methodology, the proved-statement/expert-evidence corpus, and the concepts behind staged literature/planning/proving/verification/regulation. The runtime, provider abstraction, Streamlit UI, configuration, shell launchers, smoke test, standalone verifier implementation, and all archived UI code should be replaced rather than incrementally adapted.

The most important blocker is not merely provider compatibility: the current runtime cannot establish that a proof passed. An LLM emits mutable Markdown reports, another LLM returns free-form `DONE`/`CONTINUE`, and the code accepts any response containing `DONE` (`code/decomposition_prover.py:1249-1322`). On the outer path, a non-empty top-level `proof.md` is treated as success even though every failed candidate overwrites that same file (`code/decomposition_prover.py:380-386`, `code/pipeline.py:831-855`). The Easy path bypasses independent verification altogether (`prompts/literature_survey.md:41-50`, `code/pipeline.py:785-803`). The standalone verifier also never computes a final verdict from the detailed report (`verify/verify.py:526-547`).

The second blocker is isolation. Claude, Codex, and Gemini are launched with their unsafe approval/sandbox bypasses (`code/model_runner.py:104-130`, `code/model_runner.py:287-306`, `code/model_runner.py:440-497`), and proof writers, verifiers, verdict agents, and regulators all operate in the same writable run directory (`code/decomposition_prover.py:840-849`, `code/decomposition_prover.py:950-958`, `code/decomposition_prover.py:1159-1167`, `code/decomposition_prover.py:1229-1237`, `code/decomposition_prover.py:1302-1310`). A verifier can therefore modify the candidate it is supposed to assess. Codex is invoked with `--search` for every role, including non-literature roles (`code/model_runner.py:287-297`); there is no role-specific network policy.

The rewrite should therefore begin from a new typed domain/state model and import the useful content, not wrap the current orchestration.

## Non-negotiable preservation and removal boundary

### Preserve

1. Keep `LICENSE` verbatim. It is MIT, copyright 2026 proofQED (`LICENSE:1-13`).
2. Keep upstream attribution: the README author list and paper link (`README.md:1-5`) and BibTeX citation (`README.md:7-20`). The spelling differs between `Jiayun Zhang` at `README.md:3` and `Jiayaun Zhang` at `README.md:13`; verify the authoritative spelling before changing attribution rather than silently choosing one.
3. Keep the complete prompt corpus. Preserve its historical text even when runtime versions are rewritten around typed input/output models. The mathematical content to carry forward is listed under **Prompt-content migration** below.
4. Keep every file under `proved_statements/` byte-for-byte as an evidence/artifact corpus. Do not normalize formatting, rename expert material, or “clean up” mathematical prose during the runtime rewrite.
5. Keep `skill/super_math_skill.md` as source material for a proper Codex skill. Its useful themes include problem orientation (`skill/super_math_skill.md:25-54`), counterexample search (`skill/super_math_skill.md:97-129`), skeptical verification (`skill/super_math_skill.md:134-160`), explicit proof writing (`skill/super_math_skill.md:231-260`), and computational checking (`skill/super_math_skill.md:262-286`).
6. Keep the current sample problem `problem/problem.tex` and the standalone problem/proof pair as fixtures. The standalone proof asserts the wrong-direction H-minus-one inequality (`standalone_verifier/proof.txt:6-9`), so it is especially useful as a negative/mutation-verifier regression fixture rather than as a valid proof.
7. Preserve the concepts of optional prover guidance and verification rules represented by the two currently empty files in `human_help/`, but store/snapshot them as typed run inputs rather than mutable global Markdown.
8. Preserve the current untracked repo-local Impeccable bundle and approved hook as rewrite tooling: the installed skill identifies itself as Impeccable 3.9.1 (`.agents/skills/impeccable/SKILL.md:1-7`), the detector entry point is `.agents/skills/impeccable/scripts/detect.mjs:1-21`, and `.codex/hooks.json:1-17` invokes its hook after edits. The local consent file `.impeccable/config.local.json:1-5` is machine-local state and should not be committed.

### Remove or replace

1. Replace all of `code/model_runner.py`. Its Claude and Gemini runners and unified provider dispatcher are out of scope; its Codex path is an ad-hoc buffered CLI/JSONL parser and unsafe CLI invocation (`code/model_runner.py:265-407`) rather than the required SDK/App Server adapter.
2. Replace `code/pipeline.py`, `code/decomposition_prover.py`, and `verify/verify.py` as orchestration. Reuse prompt ideas only. Their state, control, output, isolation, and failure semantics are incompatible with the requested end state.
3. Delete provider dispatch and Claude/Gemini configuration everywhere: `config.yaml:9-48`, `config.yaml:67-75`, `code/model_runner.py:594-751`, `code/decomposition_prover.py:689-750`, `verify/verify.py:54-143`, and the provider-specific smoke tests in `code/smoke_test.py:250-566`.
4. Delete the current and archived Streamlit implementations (`ui/`) after the React replacement covers the required workflows. The current UI is entirely Streamlit (`ui/app.py:1-14`) and its only dependencies are Streamlit, PyYAML, and streamlit-autorefresh (`ui/requirements.txt:1-3`).
5. Delete all of `ui/archived/`. It is a second, obsolete round-based multi-provider UI. Its checked-in `.run_config.yaml` explicitly configures Claude/Codex/Gemini and the removed multi-model pipeline (`ui/archived/.run_config.yaml:1-42`). There is no runtime reason to retain duplicate application code.
6. Replace `run.sh` and `run_verifier.sh`; both hard-code the conda environment named `agent` (`run.sh:4-6`, `run_verifier.sh:4-6`). Replace `clean.sh` with an explicit, scoped CLI operation if cleanup remains useful (`clean.sh:1-3`).
7. Replace `config.yaml` with one validated typed Codex-only config. The existing file mixes credentials, provider selection, retry policy, and agent models, and exposes unsafe settings (`config.yaml:12-48`).
8. Replace `code/smoke_test.py` with mocked automated tests plus a separately marked opt-in real-model smoke test. The current smoke test is an executable script, not a unit/integration suite, and calls real provider CLIs with the unsafe Codex bypass (`code/smoke_test.py:448-479`).
9. Rewrite the provider/UI/setup/security/output portions of `README.md`; examples include multi-provider prerequisites (`README.md:170-215`), Streamlit instructions (`README.md:217-241`), provider configuration (`README.md:344-430`), subprocess architecture (`README.md:485-493`), and the warning that all CLIs bypass permissions (`README.md:606-614`). Retain attribution, result history, expert statements, and links.

## Git, remotes, licensing, and attribution

Command evidence at audit time:

- `HEAD`, local `main`, `origin/main`, and `upstream/main` all resolved to `7dccd9b`; `main...codex-native-rewrite` was `0 0` commits apart.
- Current branch: `codex-native-rewrite`. It has no configured upstream/tracking branch. Local `main` tracks `origin/main`.
- `origin`: `git@github.com:JJYYY-JJY/QED.git` (fork).
- `upstream`: `git@github.com:proofQED/QED.git` (source project).
- No tags were present.
- There were 69 tracked files, no tracked working-tree diff, and no staged diff before this note was created.
- The license requires preserving its copyright/permission notice in copies or substantial portions (`LICENSE:3-13`).
- The README names Chenyang An, Qihao Ye, Minghao Pan, and Jiayun Zhang and links arXiv:2604.24021 (`README.md:3-5`). Those identities and the original proofQED provenance must remain prominent after the fork is renamed/reframed.

Current non-tracked state before this note:

- 106 visible untracked files: 105 files under `.agents/skills/impeccable/` and `.codex/hooks.json`.
- One ignored file: `.impeccable/config.local.json`, ignored through `.git/info/exclude`, not through the repository `.gitignore`.
- The Impeccable bundle is about 2.4 MiB and contains 105 files. Treat it as an installed tool bundle, not application source.
- `.gitignore:310` ignores the entire `docs/` tree, including this required research note. The rewrite must remove that rule (and then add/force-add the research notes) or the required documentation will silently remain untracked.
- `.gitignore:307` uses the overbroad pattern `*api*`. That can also hide legitimate new FastAPI/API source. Replace it with explicit secret/env patterns.
- `.gitignore:313-317` ignores current Streamlit/run outputs, including `ui/proof_runs/`, `.config_run_active.yaml`, and `proof_output/`; these paths identify legacy-run locations the migration command should scan when explicitly requested.

## Complete tracked-source inventory

The following table accounts for every tracked implementation/configuration source file. Mathematical artifacts are itemized separately.

| Path | Current responsibility | Rewrite disposition |
|---|---|---|
| `code/model_runner.py` | Buffered subprocess wrappers for Claude, Codex, Gemini; token parsing; provider merge/dispatch | Replace completely with one typed Codex SDK/App Server adapter plus a separately tested CLI fallback |
| `code/pipeline.py` | Stage 0/1/2 orchestration, Markdown logs, token files, CLI | Replace with application service + deterministic persisted state machine; salvage stage concepts only |
| `code/decomposition_prover.py` | File-system resume, retry loops, planner/prover/verifier/regulator calls | Replace; salvage role separation and retry taxonomy only |
| `code/smoke_test.py` | Prompt existence/render checks, config checks, live provider connectivity | Remove from runtime; split into unit tests and opt-in real Codex smoke |
| `verify/verify.py` | Separate synchronous multi-provider verifier CLI | Replace with the same core verification service/adapter used by the main runtime |
| `run.sh` | Hard-coded conda launcher and human-help copy | Replace with packaged CLI/web entry points |
| `run_verifier.sh` | Hard-coded conda verifier wrapper | Replace with packaged CLI command |
| `clean.sh` | Recursively deletes `proof_output/` | Remove or replace with explicit scoped run deletion |
| `config.yaml` | Provider credentials/models, role selection, retry limits | Replace with typed Codex-only config and environment-based secrets/auth |
| `ui/app.py` | Streamlit page/session controls | Delete after React replacement |
| `ui/config_panel.py` | Claude/Codex/Gemini settings and retry inputs | Delete; reimplement only valid typed Codex settings |
| `ui/process_manager.py` | Background shell process, stop, destructive resume cleanup | Delete; use backend job/cancel/resume APIs and state transitions |
| `ui/progress_monitor.py` | Polls/regex-parses legacy files and renders Markdown | Delete; consume typed API/event stream |
| `ui/utils.py` | YAML/file helpers and Markdown/file-state inference | Delete; backend owns validated state |
| `ui/requirements.txt` | Three Streamlit dependencies | Delete |
| `ui/archived/app.py` | Obsolete round-based Streamlit UI | Delete |
| `ui/archived/config_panel.py` | Obsolete provider/multi-model controls | Delete |
| `ui/archived/process_manager.py` | Obsolete round cleanup/resume | Delete |
| `ui/archived/progress_monitor.py` | Obsolete multi-provider round renderer | Delete |
| `ui/archived/utils.py` | Obsolete round/provider file parsing | Delete |
| `ui/archived/requirements.txt` | Duplicate Streamlit requirements | Delete |
| `ui/archived/.run_config.yaml` | Obsolete checked-in multi-provider run config | Delete |
| `ui/archived/.gitignore` | Archived Python cache ignore | Delete with directory |
| `README.md` | Attribution/results plus stale setup/runtime/UI/security docs | Rewrite selectively; retain attribution/results/history |
| `.gitignore` | Mostly LaTeX ignores plus harmful `*api*` and `docs/` rules | Simplify and update for Python/Node/build/state/secrets |
| `LICENSE` | MIT license and proofQED copyright | Preserve verbatim |
| `problem/problem.tex` | Default Euclid sample problem | Preserve as example/fixture |
| `standalone_verifier/problem.txt` | Advection-diffusion verifier fixture | Preserve |
| `standalone_verifier/proof.txt` | Incorrect proof fixture | Preserve explicitly as negative fixture |
| `human_help/additional_prove_human_help_global.md` | Empty mutable prover guidance placeholder | Preserve concept; migrate to snapshotted typed run guidance |
| `human_help/additional_verify_rule_global.md` | Empty mutable verifier-rule placeholder | Preserve concept; migrate to snapshotted typed run rules |
| `skill/super_math_skill.md` | Legacy system-prompt-like math methodology | Preserve content; package as a focused Codex skill without duplicating orchestration |

### Prompt corpus inventory

All eleven tracked prompt files should remain available as historical/source artifacts. New runtime prompts should use frozen typed inputs and schema outputs rather than telling agents to inspect and mutate files.

| Prompt | Useful content to preserve | Runtime behavior to change |
|---|---|---|
| `prompts/literature_survey.md` | Difficulty analysis, relevant-results collection, counterexamples/pitfalls, source self-verification (`:11-137`) | Remove Easy direct-proof bypass (`:41-54`), broad web/tool/file authority (`:3`, `:66`, `:151-184`), and Markdown classification control |
| `prompts/proof_effort_summary.md` | Evidence-based attempt-by-attempt summary (`:11-43`) | Return typed summary data; renderer exports Markdown. Remove shell append instructions (`:78-90`) |
| `prompts/decomposition-prover/decomposition.md` | Quantitative intermediate claims, DAG inputs, key-step flags, self-critique, plan history (`:31-73`, `:164-235`) | Replace YAML/file writes with a validated Plan schema; do not assert every input problem is open (`:5-7`) |
| `prompts/decomposition-prover/single_prover.md` | Exact-target discipline, dependencies, citations, key steps, deviations (`:50-80`, `:159-243`, `:246-325`) | Return a typed candidate; remove shame/“never give up” pressure (`:20`, `:24-47`) and mutable scratch/output file access (`:329-347`) |
| `prompts/decomposition-prover/proof_verify_structural.md` | Problem alignment, coverage, citation validation, plan adherence, refuted-plan-step analysis (`:29-205`) | Supply frozen candidate/plan/evidence directly; return a structured report. Correct stale claim that individual steps were separately proved/aggregated (`:19-25`) |
| `prompts/decomposition-prover/proof_verify_detailed.md` | Fine-grained logic, key-step, dependency, coverage, and coherence checks (`:46-119`) | Fresh read-only verifier; structured findings/report, no file writes or uncontrolled shell |
| `prompts/decomposition-prover/regulator.md` | Execution-vs-plan-vs-strategy diagnosis and `REVISE_PROOF`/`REVISE_PLAN`/`REWRITE` taxonomy (`:35-72`) | Return typed advisory analysis; code enforces budgets/transitions and appends immutable history. Remove Markdown decision parsing/file append (`:135-227`) |
| `prompts/decomposition-prover/verdict_proof.md` | The boolean rule that both structural and detailed reports must pass (`:34-45`, `:71-86`) | Remove this model call entirely; compute the rule in code over validated report objects |
| `prompts/decomposition-prover/archive/step_prover.md` | Single-claim proving, declared inputs, verification aids/confidence (`:5-12`, `:86-169`) | Preserve as historical source; do not restore “never give up” behavior (`:16-31`, `:191-197`) |
| `prompts/decomposition-prover/archive/step_verifier.md` | Independent single-claim verification and actionable findings (`:13-93`, `:115-234`) | Preserve as historical source/possible role design; schema-bind it and isolate it if revived |
| `prompts/decomposition-prover/archive/proof_aggregator.md` | Coherent assembly and subgoal resolution concepts (`:73-140`) | Preserve historically; aggregation must not mutate sealed candidates or become a new source of proof content |

The verifier prompts currently encode results as prose placeholders such as `[PASS/FAIL]` (`prompts/decomposition-prover/proof_verify_structural.md:391-409`, `prompts/decomposition-prover/proof_verify_detailed.md:247-268`, `verify/prompt_verify_structural.md:153-164`, `verify/prompt_verify_detailed.md:127-142`). Those report taxonomies are useful, but their transport must become JSON-Schema/Pydantic models.

### Standalone-verifier prompt inventory

The four `verify/` prompts duplicate the main prompt ideas:

- `verify/prompt_check_problem.md` defines well-definedness, consistency, clarity, completeness, and soundness checks (`:1-38`). Preserve the rubric as a typed ProblemReview schema.
- `verify/prompt_judge.md` combines difficulty routing with an Easy verifier (`:1-69`). Split these responsibilities and never let classification reduce verification independence.
- `verify/prompt_verify_structural.md` defines statement/alignment/completeness/architecture checks (`:13-79`). Merge the useful rubric with the canonical structural verifier.
- `verify/prompt_verify_detailed.md` defines step-level logic, correctness, rigor, and coverage (`:11-54`). Merge it with the canonical detailed verifier.

## Proved statements and evidence corpus

There are 22 tracked files under `proved_statements/`. Preserve all of them. This corpus contains successful proofs, failed-problem records, human evaluations, citations, and original expert source material; it is not runtime/provider code even where it records historical provider names.

| Path(s) | Evidence/content |
|---|---|
| `proved_statements/analysis-Apr-24-2026/README.md` | Four contributed analysis problems, including two rejected and two human-verified results (`:1-32`) plus Xiaoqian Xu's detailed expert assessment (`:34-58`) |
| `proved_statements/analysis-Apr-24-2026/problem-1.md` | Bessel-function problem (failed); exact tasks at `:35-44` |
| `proved_statements/analysis-Apr-24-2026/problem-2.md` | Allen-Cahn/Navier-Stokes problem (failed); target at `:18-25` |
| `proved_statements/analysis-Apr-24-2026/problem-3.md` | Exponential-mixing prove/disprove problem (`:1-21`) |
| `proved_statements/analysis-Apr-24-2026/problem-3-correct-proof.md` | Human-verified seven-step negative proof; proof sections at `:25-373` |
| `proved_statements/analysis-Apr-24-2026/problem-4.md` | Batchelor-scale liminf problem (`:1-18`) |
| `proved_statements/analysis-Apr-24-2026/problem-4-correct-proof.md` | Human-verified proof; proof starts at `:24-41`, claims at `:85-338` |
| `proved_statements/analysis-May-19-2026/README.md` | Three advection-diffusion results and paper/expert assessment (`:1-63`) |
| `proved_statements/analysis-May-19-2026/cited-theorems/cited_theorem_1.md` | Byte-identical duplicate of `analysis-Apr-24-2026/problem-4-correct-proof.md` (SHA-256 `6ca93d55…9de`); preserve both paths because the second has citation-set meaning |
| `proved_statements/analysis-May-19-2026/cited-theorems/cited_theorem_2.md` | Long finite-window/resolvent proof; steps at `:275-1051` |
| `proved_statements/analysis-May-19-2026/cited-theorems/cited_theorem_3.md` | Fast-periodic/Floquet-style proof with citations; steps at `:22-709` |
| `proved_statements/prob-May-15-2026/README.md` | Two human-verified probability results, exact problem statements, workflow, and expert comments (`:1-58`) |
| `proved_statements/prob-May-15-2026/problem-1.md` | Lamplighter return-probability prompt and citation restriction (`:1-8`) |
| `proved_statements/prob-May-15-2026/problem-1-correct-proof.md` | Human-verified nine-step proof; proof at `:14-1073` |
| `proved_statements/prob-May-15-2026/problem-2.md` | Total-variation prompt (`:1-4`) |
| `proved_statements/prob-May-15-2026/problem-2-correct-proof.md` | Human-verified thirteen-step proof; proof at `:10-754` |
| `proved_statements/algebraicgeometry-May-17-2026/README.md` | LICT-Z/H1 history, proof summary, references, and Yilong Zhang evaluation (`:1-60`) |
| `proved_statements/algebraicgeometry-May-17-2026/problem-1.md` | Exact LICT-Z/H1 problem (`:1-9`) |
| `proved_statements/algebraicgeometry-May-17-2026/problem-1-correct-proof.md` | Human-verified six-step proof with inline source evidence; proof at `:13-334` |
| `proved_statements/algebraicgeometry-May-17-2026/original-expert-comment/comments to AI's proof.tex` | Original dated expert document, authored by Yilong Zhang (`:108-110`), with proof digest and evaluation (`:117-152`) |
| `proved_statements/algebraicgeometry-May-17-2026/original-expert-comment/bibfile.bib` | Original bibliography for the expert document (`:1-21`) |
| `proved_statements/pde-Mar-23-2026/README.md` | Carleman-weight result/workflow/expert statement; underlying proof intentionally not yet released (`:1-26`) |

Provider/model names inside these historical records (for example `proved_statements/algebraicgeometry-May-17-2026/README.md:5-10`) are provenance and must not be erased merely because the new runtime is Codex-only.

## Current runtime behavior

### Actual stage flow

1. `run.sh` resolves Python from conda env `agent`, runs the live smoke test, copies global human-help files into the output directory without overwriting existing copies, and execs `code/pipeline.py` (`run.sh:4-36`).
2. `pipeline.py` loads YAML, requires decomposition mode, copies the input problem only if `output/problem.tex` does not yet exist, overwrites `config_used.yaml` on every invocation, and creates a fresh in-memory `TokenTracker` (`code/pipeline.py:663-717`).
3. Stage 0 invokes a literature agent. A model-authored Markdown classification controls whether it writes a direct final proof or related work (`code/pipeline.py:179-214`, `code/pipeline.py:552-629`).
4. Medium/Hard runs enter the sequential decomposition loop: decomposer, single prover, structural verifier, verdict agent, detailed verifier, verdict agent, then regulator on failure (`code/decomposition_prover.py:1634-1882`). There is no parallel execution, explicit thread lifecycle, fork, or subagent API.
5. Retry counters are represented by directories `attempt_N/revision_N/proof_N`. Resume scans for the highest numeric directory and infers the next action from file existence and report text (`code/decomposition_prover.py:502-682`).
6. Stage 2 asks another model to read the entire output tree and write a Markdown proof-effort summary (`code/pipeline.py:839-898`).
7. The UI starts this shell pipeline in a process group and polls files every four seconds (`ui/process_manager.py:28-56`, `ui/app.py:75-90`). Stop sends SIGTERM and then SIGKILL (`ui/process_manager.py:59-74`); cancellation is not a persisted state transition.

### Current output contract

The implementation can create the following legacy artifacts:

- Run root: `problem.tex`, `config_used.yaml`, `proof.md`, `proof_effort_summary.md`, `error_proof_effort_summary.md`, `TOKEN_USAGE.md`, `token_usage.json`, and, in UI runs, `pipeline_stdout.log` (`code/pipeline.py:291-398`, `code/pipeline.py:703-715`, `ui/process_manager.py:44-53`).
- Guidance: `human_help/additional_prove_human_help_global.md` and `human_help/additional_verify_rule_global.md` (`run.sh:17-23`).
- Literature: `related_info/difficulty_evaluation.md`, optional `related_work.md`, `error_literature_survey.md`, plus `literature_survey_log/AUTO_RUN_STATUS.md`, `.history`, and `AUTO_RUN_LOG.txt` (`code/pipeline.py:201-214`, `code/pipeline.py:221-267`, `code/pipeline.py:552-629`).
- Decomposition: `decomposition/STATUS.md`, `log.txt`, `plan_history.md`, optional `failure_analysis.md`, and `attempt_N/revision_N/decomposition.yaml`, `decomposer_response.md`, then `proof_K/proof.md`, `prover_response.md`, optional `scratchpad.md`, structural/detailed reports, regulator decision, and error files (`code/decomposition_prover.py:127-258`, `code/decomposition_prover.py:284-423`, `code/decomposition_prover.py:757-1246`).
- Summary logs: `summary_log/AUTO_RUN_STATUS.md`, `.history`, and `AUTO_RUN_LOG.txt` (`code/pipeline.py:862-898`).
- Standalone verifier: combined `report.md`, and on the Hard path `structural_report.md` plus (only after structural pass) `detailed_report.md` (`verify/verify.py:474-547`, `run_verifier.sh:7-20`).

README's documented layout (`README.md:529-561`) is incomplete relative to the actual implementation: it omits `config_used.yaml`, error files, `plan_history.md`, `decomposer_response.md`, and UI stdout. The new export contract must be generated from canonical state rather than defined by side effects.

## Correctness, safety, and operability findings

### Critical: PASS can be fabricated or misclassified

- `run_verdict` treats any response containing `DONE` as success, so text such as `NOT DONE`, an echoed prompt, or explanatory prose passes (`code/decomposition_prover.py:1312-1317`).
- Resume bypasses the verdict agent and trusts the phrase `OVERALL VERDICT: PASS` found in mutable report Markdown (`code/decomposition_prover.py:638-673`). The final verdict itself is not persisted as authoritative structured state.
- Every proof attempt writes to both its candidate directory and the top-level `proof.md` (`code/decomposition_prover.py:380-386`). After retry exhaustion the last failed proof remains non-empty, and `pipeline.py` therefore sets `ok=True` and labels the summary outcome PASS (`code/pipeline.py:831-855`).
- The Easy path lets the literature agent author the final proof and exits before either verifier (`prompts/literature_survey.md:41-54`, `code/pipeline.py:785-803`).
- The standalone Hard path parses only the structural verdict. It appends the detailed verifier's raw output but never parses it into a final code-level result (`verify/verify.py:516-547`); both mathematical PASS and FAIL normally exit zero.
- The UI considers any non-empty summary “pipeline complete,” even if that summary describes failure (`ui/utils.py:247-264`, `ui/progress_monitor.py:112-118`).

Required rewrite invariant: only application code may derive final PASS, and only as a pure conjunction over validated, frozen, independently produced structured reports. A rendered string must never control a transition.

### Critical: verifiers are neither fresh nor read-only

- The single prover and both verifiers share `state.output_dir` as working directory (`code/decomposition_prover.py:950-958`, `code/decomposition_prover.py:1159-1167`, `code/decomposition_prover.py:1229-1237`).
- All model runners grant dangerous permissions/sandbox bypasses (`code/model_runner.py:104-130`, `:287-306`, `:440-497`). The README explicitly confirms this (`README.md:606-614`).
- Verification prompts tell the verifier to write report/error/temp files in the same run tree (`prompts/decomposition-prover/proof_verify_structural.md:209-219`, `:253-424`; `prompts/decomposition-prover/proof_verify_detailed.md:102-119`, `:136-280`).
- Codex search/network is always on, not restricted to literature/citation work (`code/model_runner.py:287-297`).

Required rewrite invariant: seal each candidate with SHA-256 before verification; give a fresh verifier only frozen content/evidence; mount or expose candidate inputs read-only; provide a distinct writable report workspace; compare candidate hash after verification; and reject any attempted mutation.

### Critical: credentials can be persisted and printed

- The UI accepts Anthropic and Gemini keys (`ui/config_panel.py:36-99`, `ui/config_panel.py:128-162`) and writes the assembled configuration to `.config_run_active.yaml` (`ui/process_manager.py:28-40`).
- `pipeline.py` copies that configuration, including keys, to `output/config_used.yaml` (`code/pipeline.py:709-715`).
- It then prints the entire configuration to stdout (`code/pipeline.py:737-751`), which UI mode redirects into `pipeline_stdout.log` (`ui/process_manager.py:44-53`).
- The current checked-in keys are empty, and a tracked-tree secret-pattern scan found no populated common token/private-key signatures. The design is nevertheless unsafe for any user who follows the UI/README instructions.

Required rewrite invariant: no secret fields in persisted config models, snapshots, logs, events, exports, browser state, or API responses. Rely on Codex authentication/environment and explicit redaction.

### High: resume is file inference, not deterministic state

- Resume selects the numerically highest directory and branches on file presence/text (`code/decomposition_prover.py:521-682`). There are no transactions, transition sequence numbers, leases, or crash-safe checkpoints.
- When a regulator decision already exists, resume increments only the proof number without replaying whether the decision was REVISE_PLAN or REWRITE (`code/decomposition_prover.py:644-673`).
- The status logger does not actually parse existing values; every partial update starts with default attempt/revision/proof values and can regress displayed state (`code/decomposition_prover.py:181-206`).
- `config_used.yaml` is overwritten on resume (`code/pipeline.py:709-715`) while old outputs remain, mixing artifacts produced under different configurations.
- The input copy is written only if absent (`code/pipeline.py:703-707`), but agents receive the original `args.input` path (`code/pipeline.py:774-776`, `code/pipeline.py:820-828`). A changed external input can therefore diverge from the recorded run problem.
- `TokenTracker` always starts empty and overwrites token files (`code/pipeline.py:274-398`, `code/pipeline.py:714-717`), losing prior usage on resume.
- Writes are direct, non-atomic file overwrites (`code/decomposition_prover.py:76-80`, `code/pipeline.py:382-398`). SIGKILL can leave partial files that later become state signals.

### High: candidates, evidence, and provenance are mutable/unversioned

- No SQLite, manifest, SHA-256, immutable-candidate record, runtime/schema/prompt version, or source provenance exists in tracked application code.
- Decomposition YAML is accepted via `yaml.safe_load` with no structural validation (`code/decomposition_prover.py:83-98`, `:857-877`); a syntactically valid scalar/list can fail later at `.get()` (`code/decomposition_prover.py:1699-1701`).
- Citations are semicolon-delimited `<cite>` text interpreted by other models, not evidence entities (`prompts/decomposition-prover/single_prover.md:159-170`).
- Candidate findings and decisions are Markdown documents. There is no stable finding ID, proof span, evidence hash, or adjudication record.

### High: arbitrary model text controls retries

- Regulator decisions are substring searches with a default to `REVISE_PROOF` (`code/decomposition_prover.py:261-277`).
- Difficulty is parsed from any line containing “classification” and then from keyword presence (`code/pipeline.py:179-198`); standalone difficulty and structural verdict are regex-based prose parsing (`verify/verify.py:345-374`).
- Pipeline fallbacks can save the same unstructured response into multiple expected files and only check existence, not schema/content (`code/pipeline.py:107-176`, `code/pipeline.py:595-626`).
- Retry bounds exist, but the model recommends transitions and file/counter updates are not committed atomically (`code/decomposition_prover.py:1634-1931`).

### High: the Streamlit control plane is unsafe and unreliable

- The user supplies an arbitrary output path (`ui/app.py:107-149`), which the app writes into (`ui/app.py:269-282`) and resume cleanup recursively deletes beneath (`ui/process_manager.py:157-251`). This is especially dangerous if Streamlit is reachable by untrusted users; there is no authentication or workspace-root constraint.
- The warning says inputs are locked once a pipeline starts (`ui/app.py:151-155`), but a stopped run can rewrite `problem.tex` and guidance before reuse (`ui/app.py:269-282`), mixing new inputs with old artifacts.
- The process handle lives only in Streamlit session state (`ui/app.py:61-89`); refresh/session loss can orphan a live pipeline. The README warns users not to refresh (`README.md:232-241`).
- Stop is best-effort process-group signaling, not durable cancellation (`ui/process_manager.py:59-74`).
- UI state is reconstructed from mutable Markdown and file existence (`ui/utils.py:103-134`, `ui/utils.py:192-285`), so it can disagree with actual runtime outcome.

### Medium: defaults and documentation have drifted

- Model default: checked-in config says `gpt-5.6-sol` (`config.yaml:36-40`), while code fallbacks use GPT-5.5 variants (`code/model_runner.py:283-285`, `code/decomposition_prover.py:40-47`) and UI defaults to `gpt-5.5` (`ui/config_panel.py:102-125`).
- Retry default: config uses 8 proof attempts (`config.yaml:95-100`), `DecompositionState` defaults to 3/2/3 (`code/decomposition_prover.py:34-38`), UI fallbacks use 4 (`ui/config_panel.py:264-281`), and pipeline display fallbacks use 2 (`code/pipeline.py:721-729`). README claims 4 in multiple places (`README.md:334-342`, `README.md:441-443`).
- `check_prerequisites` always requires `claude` even for the all-Codex checked-in config (`code/pipeline.py:80-96`). In the audited environment Codex and conda env `agent` existed, but Claude and Gemini were absent; therefore a direct `pipeline.py` run would fail before using Codex.
- The top-level project has no dependency metadata; PyYAML is only described in README, while the only requirements file is UI-specific (`README.md:172-201`, `ui/requirements.txt:1-3`).

### Positive implementation details worth retaining conceptually

- Shell entry arguments are quoted (`run.sh:7-36`, `run_verifier.sh:7-20`) and Python subprocesses use argument arrays rather than `shell=True` (`code/model_runner.py:104-130`, `:287-306`, `:440-497`).
- YAML loads use `safe_load` (`code/pipeline.py:672-674`, `code/decomposition_prover.py:98`, `verify/verify.py:49-51`).
- The retry taxonomy distinguishes proof execution, plan revision, and strategy rewrite (`prompts/decomposition-prover/regulator.md:35-72`).
- Structural and detailed verification have meaningfully different rubrics, and citation checking is separated conceptually (`prompts/decomposition-prover/proof_verify_structural.md:29-205`, `prompts/decomposition-prover/proof_verify_detailed.md:46-119`).
- The output tree retains raw responses, plans, reports, scratch work, and token accounting, which are good artifact categories even though the current storage/authority model is wrong.

## Configuration gaps against the requested runtime

The checked-in config exposes only provider/model knobs and three retry limits (`config.yaml:67-140`). It lacks:

- one validated Codex-only type shared by CLI/backend/UI;
- capability-detected effort values and multi-agent support;
- parallelism and per-stage/per-run time/token/cost budgets;
- role-specific search/network policy;
- role-specific sandbox and approval policy;
- SDK/App Server/fallback selection and capability/version metadata;
- database path, retention/export policy, and migration settings;
- cancellation/grace/retry backoff policy;
- schema/prompt version pins.

Do not carry forward the current hard-coded `xhigh` lists (`config.yaml:39-40`, `ui/utils.py:41-42`) as an assumption about supported values; the separate official-doc research must determine capability probing and public values.

## Tests, packaging, and CI audit

### What exists

- `code/smoke_test.py` checks prompt/skill presence, renders selected templates, validates some provider/retry fields, and performs real CLI connectivity calls (`code/smoke_test.py:56-248`, `:250-566`).
- `run.sh` runs that live smoke test on every invocation (`run.sh:11-15`).
- Static audit checks passed for shell syntax in the three shell scripts and in-memory Python syntax compilation for all 15 tracked Python files. This proves syntax only.
- The audited machine happened to have conda env `agent` with Python 3.11.15, PyYAML 6.0.3, and Streamlit 1.59.2. Those versions are not declared/pinned by the repository and are not reproducibility evidence.

### What does not exist

- No `tests/`, pytest configuration, fixtures, coverage setup, or mocked Codex adapter tests.
- No `pyproject.toml`, lockfile, package metadata, console entry points, type-checker/linter configuration, or one-command setup.
- No FastAPI backend or backend tests.
- No React/Vite/TypeScript source, `package.json`, lockfile, unit tests, accessibility tests, build, or Playwright tests.
- No `.github/workflows/` or other CI configuration.
- No state-machine, schema, security, resume, cancellation, retry, immutable-candidate, mutation-verifier, provenance, export, or legacy-migration tests.
- No opt-in marker separating real-model tests from default tests. The only smoke path is real and can consume network/model resources.
- No test covers the decisive bugs above: `NOT DONE` substring, stale/failed `proof.md`, Easy bypass, detailed-verifier FAIL, report/candidate mutation, secret redaction, crash mid-write, config/input drift on resume, or idempotent migration.

Minimum rewrite test matrix:

1. State transition/table-driven tests: legal/illegal transitions, bounded retries, monotonic sequence IDs, resume after every committed state, cancel at every stage, and crash recovery.
2. Schema/property tests: reject malformed/extra/ambiguous plan, proof, evidence, report, finding, and decision payloads; round-trip versions.
3. Adapter contract tests with a fake SDK/App Server event stream, disconnects, retries, cancellation, capability variations, usage accounting, and CLI fallback parity.
4. Security tests: no bypass flags; verifier read-only workspace; candidate hash unchanged; network denied except approved literature/citation roles; path confinement; secret redaction; malicious Markdown/control strings cannot transition state.
5. Verification tests: fresh independent verifier inputs, mutation attempts, contradictory reports, UNCERTAIN semantics, code-derived final PASS, and adjudication.
6. Persistence/provenance tests: immutable candidates, SHA-256 for every frozen input/output, manifest determinism, version metadata, and atomic export.
7. Backend tests: validation, run lifecycle, SSE/WebSocket replay/order, disconnect/reconnect, cancel/resume, artifact download, error mapping, and concurrent-run isolation.
8. Frontend tests: typed API client, editor/config states, stage graph, event timeline, candidate comparison, evidence ledger, findings links, metrics, artifacts, loading/empty/error/offline/reduced-motion/accessibility states.
9. Playwright lifecycle: create → stream → stop → resume → seal → verify → export, plus failed/retried run and browser refresh/reconnect.
10. Migration fixtures for both legacy dialects below; real-model smoke remains opt-in.
11. CI gates: Python format/lint/typecheck/unit/integration, frontend format/lint/typecheck/unit/build/Playwright, security checks, and the Impeccable detector.

## Legacy-run migration requirements

There are two distinct historical output dialects to support:

1. **Current decomposition dialect**: `related_info/`, `decomposition/attempt_N/revision_N/proof_K/`, top-level proof/summary/token files, described in `README.md:529-561` and implemented in `code/decomposition_prover.py:284-423`.
2. **Older round/multi-provider dialect**: `verification/round_N/`, optional provider subdirectories, `proof_status.md`, `selection.md`, structural/detailed folders, and per-round guidance. The obsolete parser documents this layout (`ui/archived/utils.py:75-269`, `ui/archived/progress_monitor.py:313-516`).

The importer should be explicit and non-destructive:

- Accept a user-selected legacy run directory; never scan/delete arbitrary paths automatically.
- Detect dialect and inventory every file before import. Store source relative path, size, mtime (advisory only), SHA-256, importer version, and warnings.
- Freeze `problem.tex`, human guidance/rules, config snapshot, literature outputs, plans, raw model responses, candidates, reports, decisions, summaries, token data, logs, scratch/temp artifacts, and errors as imported artifacts.
- Create immutable candidate records for every legacy `proof_*/proof.md`, round proof, and top-level proof; never assume the top-level proof is the winner.
- Parse legacy Markdown/YAML only as best-effort metadata. Store the original bytes. Any inferred difficulty/verdict/state must be marked `legacy_untrusted` with parser warnings.
- Never import a legacy PASS as a new trusted PASS. Re-run verification with frozen input under the new isolated verifier before promotion.
- Reconcile `token_usage.json` when valid; otherwise preserve it as raw data. Do not fabricate missing usage.
- Import old provider/model names as provenance strings even though new execution is Codex-only.
- Make import idempotent by source manifest hash; a rerun should return the same imported run or a clear duplicate result.
- Do not mutate/remove the source directory. Provide a post-import comparison report and explicit optional archival instructions.

Canonical new exports should include at minimum a human proof document, a human verification/report document, and a machine-readable manifest. The manifest should enumerate run/config/schema/prompt/runtime versions; frozen input and guidance hashes; evidence; candidate hashes; report/finding/adjudication IDs and hashes; transition/event range; usage/time metrics; final code-derived status; and every exported artifact hash.

## Required architecture consequences

The local evidence implies these implementation seams:

- **Codex adapter**: the only module aware of SDK, App Server JSON-RPC, streamed event variants, capability detection, or CLI fallback. No orchestration code should parse CLI JSONL.
- **Typed domain models**: RunConfig, Problem, FrozenInput, Evidence, Citation, Plan, Candidate, VerificationReport, Finding, Decision/Adjudication, Usage, Event, Artifact, and Manifest.
- **SQLite repository/state machine**: transactions own transition validity, retries, leases, cancel flags, immutable candidates, report attachment, final-status computation, and event sequencing.
- **Role executor**: creates explicit literature/planning/proof/verification/adjudication threads with per-role sandbox/network/tool budgets. Verification always starts fresh unless a deliberately documented frozen-history use case exists.
- **Application service**: start/stop/resume/seal/verify/export/migrate APIs; CLI and FastAPI call the same service.
- **Event stream**: persist first, then publish/replay typed events to SSE/WebSocket clients. Files and Markdown are projections/artifacts, never state authority.
- **Renderer/exporter**: generates proof/report/manifest from canonical records.
- **React console**: consumes typed APIs/events and never reads the filesystem or starts/kills processes itself.

## Completion checklist for the rewrite audit findings

Before declaring the rewrite complete, searches and tests must prove all of the following:

- No runtime references to Claude, Anthropic, Bedrock, Gemini, provider dispatch, Streamlit, the conda `agent` environment, or archived UI remain. Historical proved-statement provenance is exempt.
- No `--dangerously-*`, `bypassPermissions`, `--approval-mode yolo`, or equivalent approval/sandbox bypass remains.
- No regex/substring/Markdown/YAML-text control decision remains for difficulty, PASS, retry action, resume, or completion.
- No verifier has write access to candidate bytes; mutation tests prove this and candidate hashes are rechecked.
- No raw config/secret is persisted, printed, emitted, exported, or sent to the frontend.
- No state transition depends on file existence. SQLite state plus immutable artifact hashes is authoritative.
- Start, stream, persisted cancel, resume, candidate seal, fresh independent verification, adjudication, and proof/report/manifest export are covered end-to-end.
- Both legacy dialects import non-destructively and do not confer trusted PASS.
- Python/backend and frontend lint/typecheck/tests/build, Playwright, security tests, and Impeccable detection pass in CI.
- `LICENSE`, attribution/citation, all eleven prompts (or lossless archived copies plus derived runtime prompts), all 22 proved-statement files, the math methodology, and selected sample fixtures remain present.

## Audit verification record

Commands used included `git status --porcelain=v2 --branch -uall`, `git ls-files -s`, `git remote -v`, `git branch -vv`, `git rev-parse`, `git rev-list --left-right --count`, `git diff --quiet`, `rg --files`, line-numbered reads of every tracked implementation/prompt/config/doc file, secret/control/unsafe-flag searches, `sha256sum` on the proved-statement corpus, `bash -n`, and in-memory `compile()` over all tracked Python sources with bytecode disabled.

Results:

- All 69 tracked files were inventoried.
- All 15 tracked Python sources parsed successfully; all three shell scripts passed `bash -n`.
- No default test suite exists, so no functional pass claim is possible.
- No real model call was made during this audit: the only existing smoke test invokes live models with unsafe flags, so running it would be inappropriate evidence for a read-only pre-rewrite audit.
- The tracked tree and index were clean before creation of this note. The only visible untracked state was the 105-file Impeccable bundle and `.codex/hooks.json`; `.impeccable/config.local.json` was ignored locally.
- Because `.gitignore:310` currently ignores `docs/`, this note will not appear in ordinary `git status` until that rule is fixed or the note is force-added.
