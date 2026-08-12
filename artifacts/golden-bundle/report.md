# QED policy verification report

Run: `run-1`  
Candidate: `candidate-1`  
Candidate SHA-256: `a34c3d5d487ae072afc62edb6e71b5bb9918df9a64e6c47aa84793243d93d0b5`  
Code-computed verdict: **QED policy PASS**  
Required reports: `structural`, `detailed`, `assumptions_quantifiers`, `counterexample_edge_case`, `reconstruction`, `citation`

This status means the candidate satisfied the configured code gates and fresh-thread LLM checks. It is not peer review, formal or Lean verification, or a guarantee of mathematical truth.
The manifest records the run state observed at its event-chain boundary; QED policy PASS is not itself a completion receipt.
Fresh threads isolate conversation state, not model weights. SHA-256 values provide integrity addressing, not signatures or trusted timestamps.

## Evidence provenance

`runtime_observed` means QED observed the runtime open/find action for the exact URL. The locked Codex interfaces do not expose fetched page bodies or final redirect URLs, so evidence text remains `model_reported`; no source is labeled `server_captured`.
- `evidence-1`: source `legacy_untrusted`; content `legacy_untrusted`

## Frozen verification rules

- `rule-001-b704e3ecf20553f5`: Every divisibility inference must be justified.
  - `report-detailed` / `check-detailed`: **PASS**

## Assumptions_Quantifiers

Report: `report-assumptions_quantifiers`  
Report SHA-256: `e4ecc7d90b0efdd135130fb0f271f7f678410523b9c931a20faf1b5392924443`  
Verifier thread: `thread-assumptions_quantifiers`  
Verdict: **PASS**

### Checks

- **PASS** `check-assumptions_quantifiers` (mathematical correctness): The assumptions_quantifiers review found no defect.
  - Proof spans: prime divisor

## Citation

Report: `report-citation`  
Report SHA-256: `78318b0c9538785503a467989a3bfbd0c376e4b0680f3d9273b517b09f33f07e`  
Verifier thread: `thread-citation`  
Verdict: **PASS**

### Checks

- **PASS** `check-citation` (mathematical correctness): The citation review found no defect.
  - Proof spans: prime divisor
  - Evidence: evidence-1
  - Citation support `evidence-1`:
    - Proof span: For every integer $n > 1$, a prime divisor of $n! + 1$ exceeds $n$.
    - Evidence excerpt: Euclid's construction produces a prime outside any finite list.
    - Source locator: evidence:evidence-1

## Counterexample_Edge_Case

Report: `report-counterexample_edge_case`  
Report SHA-256: `f4a45350c7c27bc58a1d33b69ed2670ca17c223bc4a3d98d846b839d1912408c`  
Verifier thread: `thread-counterexample_edge_case`  
Verdict: **PASS**

### Checks

- **PASS** `check-counterexample_edge_case` (mathematical correctness): The counterexample_edge_case review found no defect.
  - Proof spans: prime divisor

## Detailed

Report: `report-detailed`  
Report SHA-256: `8dddda5491568e7d1a21fb876ea5304b9057adeec4af51f6a042a872bbcbdc69`  
Verifier thread: `thread-detailed`  
Verdict: **PASS**

### Checks

- **PASS** `check-detailed` (mathematical correctness): The detailed review found no defect.
  - Proof spans: prime divisor
  - Verification rules: rule-001-b704e3ecf20553f5

## Reconstruction

Report: `report-reconstruction`  
Report SHA-256: `7861d02a7d081c00fca54c548889cbe2607a4382652bd1222c83370fcd660087`  
Verifier thread: `thread-reconstruction`  
Verdict: **PASS**

### Checks

- **PASS** `check-reconstruction` (mathematical correctness): The reconstruction review found no defect.
  - Proof spans: prime divisor

## Structural

Report: `report-structural`  
Report SHA-256: `9d089ec175542b455ea9912c668b690eda172e3f73d19de8c3c8ea6deb0b8eda`  
Verifier thread: `thread-structural`  
Verdict: **PASS**

### Checks

- **PASS** `check-structural` (mathematical correctness): The structural review found no defect.
  - Proof spans: prime divisor
