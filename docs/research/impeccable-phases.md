# Impeccable phase record — historical / non-runtime

> Archived alpha development record. Impeccable, its hook, detector, assets,
> and npm dependency are not part of QED v2 stable runtime or release gates.

Review date: 2026-07-16

Target: `frontend/src/App.tsx` and the QED research-console flow.

QED targets mathematical researchers working at a desk. The console favors
dense evidence, explicit provenance, and stable reading layouts. It does not use
marketing-page or general-chat patterns. The project asks for sound semantic and
keyboard defaults but sets no separate accessibility certification target.

Each named phase was a reference workflow in the former repo-local Impeccable bundle.
This record applies all eight workflows to the same target and records their
durable evidence.

## Phase outcomes

| Phase | Input and action | Durable evidence | Outcome |
| --- | --- | --- | --- |
| `init` | Recorded audience, purpose, tone, platform, exclusions, hook consent, and design direction from the maintainer's confirmed choices | `PRODUCT.md`, `DESIGN.md`, `.codex/hooks.json`, `AGENTS.md` | Product register and edit hook present |
| `shape` | Defined a run-centered information architecture with problem input, runtime policy, stages, metrics, candidates, evidence, activity, artifacts, and inspector details | `DESIGN.md`; `docs/research/frontend-audit.md`; component boundaries under `frontend/src/components/` | One auditable workspace replaces the legacy Streamlit flow |
| `craft` | Built the React/Vite console from the approved structure and token system | `frontend/src/App.tsx`, `frontend/src/styles.css`, unit tests, Playwright tests | Desktop three-panel shell with tablet and narrow drawers |
| `critique` | Ran an unanchored design review and detector/browser review in isolated agents, then synthesized and stored the result | This phase record, detector output, and frontend regression tests | Pre-fix health score 25/40; no P0; three P1 issues identified |
| `audit` | Reviewed accessibility defaults, performance, responsive behavior, theming, state coverage, browser errors, and secret exposure | Audit fixes and verification evidence below | Pre-fix audit 15/20; two P1 and four P2 issues reproduced |
| `harden` | Added explicit load recovery, cancellation confirmation, modal drawer focus behavior, safe numeric editing, and local-tool path protections | Frontend regressions; Impeccable security regressions; `docs/threat-model.md` | Reproduced failure and path-escape chains fail closed |
| `adapt` | Checked 1440px, 1024px, 390px, and 320px layouts; reflowed tables and controls; contained long proof text; kept the current stage in view | `frontend/src/styles.css`; `frontend/tests/console.spec.ts` | Desktop-first workflow remains usable at narrow widths |
| `polish` | Resolved design tokens, raised operational text sizes, aligned the advanced checkbox, cleared the stage connector, completed interaction states, and reran browser checks | `DESIGN.md`; detector, lint, typecheck, build, unit, and Playwright results | Detector clean; no page-level overflow or browser error in tested flows |

## Critique evidence

Method: two isolated review passes. The reviewers did not receive each other's
output. Assessment A finished before detector findings entered the synthesis.

Assessment A scored the ten Nielsen heuristics at 25/40. It found three P1
issues: modal drawers left focus behind the overlay, load failures looked like
empty data, and Cancel sent a high-consequence command on the first click. It
also flagged small audit metadata and a narrow stage rail that could start with
the current stage off-screen.

Assessment B ran:

```bash
node .agents/skills/impeccable/scripts/detect.mjs --json \
  frontend/index.html frontend/src
```

The CLI returned exit 0 and `[]`. A fresh headless Playwright page loaded the
real Vite client with mocked API responses. The empty state produced zero
findings. The new-run form produced two browser `tiny-text` findings: an 11px
context label and 10px explanatory text. The polish pass raised both to 12px.

Critique run notes:

- target slug: `frontend-src-app-tsx`;
- ignore file: absent;
- browser visibility: headless fallback because no native Browser MCP was
  exposed;
- overlay injection: succeeded inside the fallback browser, with two visible
  overlays on the form before fixes;
- user-visible Human tab: unavailable, so no claim was made;
- live server, Vite, browser, screenshots, and temporary files: stopped or
  removed;
- ignored working-session snapshot: written to
  `.impeccable/critique/2026-07-17T04-22-17Z__frontend-src-app-tsx.md`;
- trend: first run, score 25.

## Audit fixes

The audit reproduced each issue against the built interface. The implementation
now provides:

- separate initialization and snapshot failures with Retry actions;
- focus entry, Tab containment, Escape close, scroll lock, and trigger-focus
  restoration for modal navigation and inspector drawers;
- an inline confirmation before an active run sends Cancel;
- a complete tabs keyboard model with roving focus and linked tab panels;
- number fields that accept an empty editing state without writing `NaN`;
- wrapping for long proof, finding, evidence, event, and plan content;
- automatic narrow-screen scrolling to the current research stage;
- 10px or larger operational metadata and 12px form explanations;
- a token-aligned proactive-multi-agent checkbox and an opaque stage-label layer
  above the connector line.

The hardening pass also treats repo-local Impeccable tooling as a security
boundary. Regression tests cover contained regular-file access, symbolic-link
and parent-directory swaps, live-token bootstrap, screenshot isolation,
terminal sanitization, shell-free child-process arguments, manual-agent
sandboxing, and rollback.

## Verification and limits

The completion matrix records final command results. Browser tests cover
Desktop Chrome and a Pixel 7 viewport. They check drawer focus restoration,
Escape close, current-stage visibility, long unbroken proof text, proof/report
inspection, and evidence navigation.

No authenticated real-Codex call ran during the UI phases. No physical phone or
tablet was available, so Playwright device emulation supplies the narrow-screen
evidence. The console has no global shortcut system, and the advanced runtime
disclosure remains a dense expert form. Both choices fit the confirmed
desktop-first researcher scope and do not block this release candidate.

Descriptor-pinned Impeccable filesystem operations require Linux
`/proc/self/fd` and fail closed elsewhere. This restriction applies to the
repo-local design tooling, not the QED service or console runtime.
