---
name: QED Research Console
description: An auditable proof desk for evidence, candidates, and independent review.
---

# Design System: QED Research Console

## Overview

**Creative North Star: "The Red Pencil Proof Desk"**

QED should feel like a white proof desk under clear working light. Researchers
spend hours reading dense arguments here, so the interface stays quiet and lets
proof structure carry the page. A warm crimson mark appears only when an action,
selection, or decision needs attention.

Linear informs state clarity, GitHub code review informs proof-linked findings,
and Zotero informs the evidence ledger. The interface uses familiar product
controls and compact information density. Motion lasts 150–250ms and communicates
state changes; it does not stage entrances.

**Key Characteristics:**

- Dense without crowding
- Restrained color with precise semantic states
- Provenance visible beside claims and decisions
- Stable layouts that support long reading sessions

## Colors

Use a restrained palette on a pure white ground. The implementation defines the
palette as CSS custom properties in `frontend/src/styles.css`.

### Primary

- **Red Pencil Crimson** (`oklch(0.544 0.169 31.3)`): focus and decisive state
  markers.
- **Action Crimson** (`oklch(0.47 0.175 31.3)`): primary actions and links.
- **Crimson Hover** (`oklch(0.42 0.16 31.3)`): active pointer states.
- **Crimson Wash** (`oklch(0.945 0.035 31.3)`): selected backgrounds.

### Neutral

- **Proof Desk White** (`oklch(1 0 0)`): the page ground.
- **Working Surface** (`oklch(0.975 0.006 258)`): toolbars, sidebars, and
  inspectors.
- **Proof Ink** (`oklch(0.22 0.015 258)`): body text and mathematics.
- **Graphite Note** (`oklch(0.47 0.016 258)`): secondary metadata.
- **Rule** (`oklch(0.885 0.01 258)`): panel and table divisions.

**The Red Pencil Rule.** Crimson occupies no more than 10% of a screen and never
acts as decoration.

## Typography

Use the operating system's interface sans stack for controls and proof text. Use
`SFMono-Regular`, Consolas, or Liberation Mono for hashes, identifiers, code,
and event logs.

### Hierarchy

- **Display:** compact page titles, never oversized marketing headlines.
- **Headline:** stage and candidate headings with clear nesting.
- **Title:** panel and finding titles.
- **Body:** proof prose capped near 70 characters per line when it is not in a
  comparison view.
- **Label:** controls, metadata, and statuses; sentence case by default.

**The Proof First Rule.** Typography clarifies mathematical hierarchy and never
turns operational labels into display copy.

## Elevation

Use tonal layering and dividers at rest. Shadows appear only when an overlay or
temporary floating control must separate from dense research content.

**The Flat Desk Rule.** Persistent panels remain flat. Depth communicates an
interaction state, not visual decoration.

## Layout and motion

The desktop shell uses a 244px run rail, a fluid research workspace, and a 340px
inspector. The inspector becomes a drawer below 1180px. The run rail becomes a
drawer below 860px, and dense tables reflow into labeled records below 620px.
Coarse-pointer controls use a 44px minimum target. Transitions use the shared
`cubic-bezier(0.22, 1, 0.36, 1)` curve and last 180 to 220ms. Reduced-motion
preferences collapse animation and transition duration.

## Component contracts

- `RunNavigation` owns run selection and creation entry points.
- `StageRail` and `ActivityTimeline` show durable workflow state and events.
- `CandidateWorkspace`, `EvidenceLedger`, and `ArtifactList` keep proof output,
  provenance, and exports in one workspace.
- `Inspector` shows run, candidate, evidence, and verifier details without
  replacing the current workspace.
- `NewRunForm` owns frozen problem input, policies, budgets, and runtime options.

## Do's and Don'ts

### Do:

- **Do** keep evidence, source identity, and content hash near each cited claim.
- **Do** show stage, thread, and verification state with text as well as color.
- **Do** label Running, Paused, Failed, Uncertain, Export intent, Complete, and
  QED policy PASS separately.
- **Do** borrow Linear's state clarity, GitHub review's linked findings, and
  Zotero's evidence organization.
- **Do** keep motion within 150–250ms and provide a reduced-motion path.

### Don't:

- **Don't** turn the console into a ChatGPT-style single-column conversation.
- **Don't** use SaaS marketing-page structures, promotional heroes, or vanity metrics.
- **Don't** tint the white ground toward cream, clay, or parchment to echo the
  warm primary color.
- **Don't** use crimson for inactive controls or decoration.
- **Don't** describe QED policy PASS as mathematical truth, formal verification,
  or a certificate.
