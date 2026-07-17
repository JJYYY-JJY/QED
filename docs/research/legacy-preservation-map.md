# Legacy runtime removal and preservation map

## Scope

This audit separates executable compatibility code from the repository's
mathematical record. The files removed below are orchestration, UI, provider
configuration, or launch wrappers. None contains a proved statement or a
prompt that is not already stored as a standalone preserved file.

## Removed files

| File | Audit finding and removal reason |
| --- | --- |
| `code/decomposition_prover.py` | Legacy file-driven proof loop. It dispatches per-role providers, parses Markdown/YAML control decisions, and mutates attempt directories. Its prompt inputs are separate files under `prompts/decomposition-prover/`, which remain unchanged. |
| `code/model_runner.py` | Ad-hoc subprocess wrappers and dispatch for Claude, Codex, and Gemini. It includes unsafe permission/sandbox bypass flags and duplicates the typed Codex runtime. |
| `code/pipeline.py` | Legacy staged orchestrator with provider-specific configuration, filesystem checkpoints, and free-form agent outputs. It loads the independently preserved literature and summary prompts rather than defining mathematical content itself. |
| `code/smoke_test.py` | Smoke harness for the deleted CLI runners, provider configuration, and Claude settings. Current runtime contracts are covered by the repository test suite. |
| `ui/app.py` | Streamlit entry point for the decomposition pipeline. It is replaced by the typed FastAPI/React application surface. |
| `ui/config_panel.py` | Streamlit controls for Claude/Codex/Gemini selection and provider credentials. A provider selector is incompatible with the Codex-only product. |
| `ui/process_manager.py` | Launches `run.sh` as an unmanaged subprocess and implements resume by deleting output files. This conflicts with durable SQLite transitions and immutable attempts. |
| `ui/progress_monitor.py` | Polls legacy output directories and infers progress and verdicts from files/Markdown. Typed API events and stored state replace it. |
| `ui/requirements.txt` | Installs Streamlit, its auto-refresh extension, and PyYAML solely for the deleted UI. |
| `ui/archived/.gitignore` | Ignore file scoped only to the deleted archived Streamlit tree. |
| `ui/archived/.run_config.yaml` | Archived multi-provider run configuration containing Claude/Gemini selection and bypass-oriented settings. |
| `ui/archived/app.py` | Earlier Streamlit entry point for the multi-provider pipeline. |
| `ui/archived/config_panel.py` | Earlier provider-selection and credential UI for Claude, Codex, and Gemini. |
| `ui/archived/process_manager.py` | Earlier subprocess launcher and deletion-based resume implementation. |
| `ui/archived/progress_monitor.py` | Earlier filesystem/Markdown progress and verdict renderer. |
| `ui/archived/requirements.txt` | Duplicate archived Streamlit dependency list. |
| `ui/archived/utils.py` | Helpers for the archived provider UI, YAML config, output layout, and Markdown verdict parsing. |
| `ui/utils.py` | Helpers and constants for the current legacy Streamlit UI, including the three-provider registry and file-derived state. |
| `verify/verify.py` | Standalone Python runner with Claude/Codex/Gemini subprocess dispatch, unsafe bypass flags, and string verdict parsing. Its four prompt Markdown files remain unchanged. |
| `run.sh` | Hard-coded `conda run -n agent` launcher for the deleted pipeline. |
| `run_verifier.sh` | Hard-coded Conda wrapper for the deleted standalone verifier runner. |
| `clean.sh` | Unscoped legacy cleanup wrapper that recursively deletes `proof_output`; durable run data is managed through the application instead. |
| `config.yaml` | Multi-provider Claude/Codex/Gemini configuration, credential placeholders, and legacy role dispatch. Typed Codex-only settings replace it. |

## Preserved mathematical and historical assets

The following tracked assets are deliberately outside the removal set. The
object identifiers record their state at the cleanup baseline so later audits
can distinguish preservation from accidental recreation.

| Asset | Preserved content | Baseline identifier |
| --- | --- | --- |
| `LICENSE` | Upstream MIT license and attribution | Git blob `4bc52834a93ec09b8123a6d10baa2d64b348abff` |
| `prompts/` | All 11 active and archived proof-research prompt files | Git tree `3e6b794b43a254271be102c114eb2a672f37d0aa` |
| `verify/*.md` | Problem review, difficulty judge, structural verification, and detailed verification prompts | Combined SHA-256 `37a528818fd906c29b8c44df9a417b7e46e2a3c6b4b4c036f2b4cfca964e2a38` |
| `proved_statements/` | All 22 statements, accepted proofs, cited theorems, bibliography, and expert commentary files | Git tree `3d47604399ce894d2d3b5c06a1d2c51c29dbc2b6` |
| `human_help/` | Original proving guidance and verification rules | Git tree `669d4017e6368d9692444ba1740ee315d3fa5fde` |
| `problem/` | Original sample problem | Git tree `712aed9477220e930a6f4cd0138d7b5d24e76b6d` |
| `skill/` | Original mathematical research skill artifact | Git tree `6f622a4ff9373b9537985cb72e17a15bffe84eeb` |
| `standalone_verifier/` | Historical verifier problem and proof inputs | Git tree `7647b6be157e65d6b4735dc161d4b2a759901c5b` |

Repository documentation, including upstream attribution, is also left in
place. The regression test intentionally excludes historical documentation,
preserved prompts, statements, and artifacts from runtime keyword checks;
their contents are evidence, not executable compatibility code.

## Verification

The cleanup is verified by:

1. asserting every legacy executable/UI path is absent;
2. scanning production Python, launch scripts, dependency manifests, and
   configuration for legacy providers, Streamlit, Conda launch commands,
   provider dispatch, and dangerous bypass modes;
3. checking `git diff --name-only` for every protected asset path; and
4. running the scoped regression test in `tests/test_no_legacy_runtime.py`.
