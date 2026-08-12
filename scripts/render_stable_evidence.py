"""Generate the machine-readable stable evidence and derived scorecard."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from qed.schemas import canonical_json
from qed.stable_contracts import EvidenceGate, StableEvidence

ROOT = Path(__file__).resolve().parents[1]
COMMIT = shutil.which("git")
if COMMIT is None:
    raise RuntimeError("git is required to render release evidence")
COMMIT = subprocess.run(  # noqa: S603 - executable is resolved from the trusted PATH
    (COMMIT, "rev-parse", "HEAD"),
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
NOW = datetime.now(UTC)
DATE = NOW.date().isoformat()


def _artifact(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def _gate(
    gate_id: str,
    dimension: str,
    status: str,
    command: str,
    result: str,
    artifact_path: str,
    *,
    limitation: str | None = None,
    references: tuple[str, ...] = (),
) -> EvidenceGate:
    return EvidenceGate(
        gate_id=gate_id,
        dimension=dimension,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        command=command,
        utc_date=DATE,
        commit_sha=COMMIT,
        environment=(
            "macOS Darwin; Python 3.14.6; Node 24.19.0; uv 0.12.3; "
            "uv frozen; working tree changes present"
        ),
        result=result,
        artifact_path=artifact_path,
        artifact_sha256=_artifact(artifact_path),
        limitation=limitation,
        references=references,
    )


def build_evidence() -> StableEvidence:
    reliability = "docs/research/reliability-report-v2-stable.json"
    golden = "artifacts/golden-bundle/manifest.json"
    specs = [
        {
            "gate_id": "architecture.roadmap",
            "dimension": "architecture",
            "status": "passed",
            "command": "git diff --check",
            "result": "roadmap recorded",
            "artifact_path": "docs/research/v2-stable-roadmap.md",
            "references": ("docs/research/v2-stable-roadmap.md",),
        },
        {
            "gate_id": "architecture.state-policy-tests",
            "dimension": "architecture",
            "status": "passed",
            "command": "uv run --frozen pytest tests/test_store.py tests/test_workflow.py",
            "result": "focused state/policy tests passed",
            "artifact_path": golden,
            "references": ("tests/test_store.py", "tests/test_workflow.py"),
        },
        {
            "gate_id": "architecture.dependency-boundaries",
            "dimension": "architecture",
            "status": "passed",
            "command": "uv run --frozen pytest tests/test_package_boundaries.py",
            "result": (
                "packaging boundary regression passed; logical contexts are "
                "documented against shipped modules"
            ),
            "artifact_path": "docs/architecture.md",
            "references": ("tests/test_package_boundaries.py", "docs/architecture.md"),
        },
        {
            "gate_id": "architecture.api-sse-contract",
            "dimension": "architecture",
            "status": "passed",
            "command": "uv run --frozen pytest tests/test_api.py tests/test_service.py",
            "result": (
                "typed API, idempotent command, replay, bounded SSE, and "
                "slow-client behavior tests passed"
            ),
            "artifact_path": "docs/research/frontend-v2-stable-results.json",
            "references": ("tests/test_api.py", "tests/test_service.py", "src/qed/api.py"),
        },
        {
            "gate_id": "architecture.offline-bundle-contract",
            "dimension": "architecture",
            "status": "passed",
            "command": (
                "uv run --frozen pytest tests/test_bundle_verifier.py "
                "tests/test_golden_bundle.py"
            ),
            "result": "offline verifier tests passed",
            "artifact_path": golden,
            "references": ("src/qed/bundle_verifier.py", "tests/test_bundle_verifier.py"),
        },
        {
            "gate_id": "architecture.coverage",
            "dimension": "architecture",
            "status": "failed",
            "command": "uv run --frozen pytest --cov=qed --cov-branch",
            "result": "repository coverage remains below the stable target; core coverage passed",
            "artifact_path": "docs/research/coverage-v2-stable.json",
            "limitation": (
                "Core measured 98.10% line / 93.99% branch and passes its target. "
                "Repository measured 88.19% line / 72.30% branch; repository "
                "target is line >=90% and branch >=85%."
            ),
            "references": ("scripts/check_coverage.py",),
        },
        {
            "gate_id": "architecture.mutation",
            "dimension": "architecture",
            "status": "failed",
            "command": "uv run --frozen mutmut run --max-children 2",
            "result": "1232/2138 executable mutants killed; mutation score 57.62%",
            "artifact_path": "docs/research/mutation-v2-stable.json",
            "limitation": (
                "The required core mutation score is >=90%; surviving mutations "
                "remain a code blocker."
            ),
            "references": ("docs/research/mutation-v2-stable.json",),
        },
        {
            "gate_id": "security.path-network-regressions",
            "dimension": "security",
            "status": "passed",
            "command": "uv run --frozen pytest tests/test_security_boundaries.py",
            "result": "path and restricted-network regression tests passed",
            "artifact_path": "docs/threat-model.md",
            "references": (
                "tests/test_security_boundaries.py",
                "src/qed/security/paths.py",
                "src/qed/security/network.py",
            ),
        },
        {
            "gate_id": "security.protocol-limits",
            "dimension": "security",
            "status": "passed",
            "command": "uv run --frozen pytest tests/test_runtime_stdio.py tests/test_api.py",
            "result": (
                "typed protocol frame, notification routing, bounded queue, "
                "SSE quota, and replay tests passed"
            ),
            "artifact_path": "docs/threat-model.md",
            "references": (
                "tests/test_runtime_stdio.py",
                "tests/test_api.py",
                "src/qed/runtime/stdio.py",
            ),
        },
        {
            "gate_id": "security.offline-bundle",
            "dimension": "security",
            "status": "passed",
            "command": "uv run --frozen qed verify-bundle artifacts/golden-bundle --json",
            "result": "valid=true; decision=PASS; signature_status=unsigned",
            "artifact_path": golden,
            "limitation": (
                "The fixture proves integrity and policy recomputation, not signature "
                "authenticity or real Codex reliability."
            ),
            "references": ("src/qed/bundle_verifier.py", "artifacts/golden-bundle"),
        },
        {
            "gate_id": "security.baseline-scan-closeout",
            "dimension": "security",
            "status": "blocked",
            "command": "Codex Security release review",
            "result": (
                "official diff review found 0 reportable findings with 104/104 "
                "file receipts, but coverage is partial"
            ),
            "artifact_path": "docs/research/security-findings-closeout-v2-stable.json",
            "limitation": (
                "The candidate remediation map has owner/date/evidence for all eight "
                "baseline findings, but delegated workers were unavailable and a "
                "release-window independent closeout is still required."
            ),
            "references": (
                "docs/research/security-findings-closeout-v2-stable.json",
                "artifacts/security-diff-scan-v2-stable/report.md",
                "docs/research/v2-stable-roadmap.md",
                "docs/threat-model.md",
            ),
        },
        {
            "gate_id": "security.secret-export-scan",
            "dimension": "security",
            "status": "passed",
            "command": "uv run --frozen python scripts/security_export_scan.py",
            "result": "production provider/unsafe scan and golden-bundle secret scan are clean",
            "artifact_path": "docs/research/security-export-scan-v2-stable.json",
            "references": ("scripts/security_export_scan.py",),
        },
        {
            "gate_id": "mathematics.policy-n-of-n",
            "dimension": "mathematics",
            "status": "passed",
            "command": (
                "uv run --frozen pytest tests/test_decision.py "
                "tests/test_bundle_verifier.py"
            ),
            "result": "application code requires all stable verifier roles and independent threads",
            "artifact_path": golden,
            "references": ("src/qed/decision.py", "src/qed/stable_contracts.py"),
        },
        {
            "gate_id": "mathematics.claim-graph",
            "dimension": "mathematics",
            "status": "passed",
            "command": (
                "uv run --frozen pytest tests/test_workflow.py "
                "tests/test_bundle_verifier.py"
            ),
            "result": (
                "UTF-8 byte spans, stable claim IDs, dependency/cycle checks, "
                "and coverage checks passed"
            ),
            "artifact_path": golden,
            "references": ("src/qed/stable_contracts.py", "src/qed/workflow.py"),
        },
        {
            "gate_id": "mathematics.fresh-thread-lineage",
            "dimension": "mathematics",
            "status": "passed",
            "command": (
                "uv run --frozen pytest tests/test_runtime_models.py "
                "tests/test_runtime_router.py tests/test_decision.py"
            ),
            "result": (
                "fresh verifier thread, exact model, and distinct external "
                "identity checks passed"
            ),
            "artifact_path": golden,
            "references": ("src/qed/runtime/models.py", "src/qed/runtime/router.py"),
        },
        {
            "gate_id": "mathematics.benchmark-lock",
            "dimension": "mathematics",
            "status": "passed",
            "command": (
                "uv run --frozen python benchmarks/reliability/run.py validate "
                "--cases benchmarks/reliability/v2-stable-cases.jsonl "
                "--lock benchmarks/reliability/v2-stable-cases.lock.json"
            ),
            "result": "27 cases validated",
            "artifact_path": "benchmarks/reliability/v2-stable-cases.lock.json",
            "references": ("benchmarks/reliability/v2-stable-pack.json",),
        },
        {
            "gate_id": "mathematics.statistics-tests",
            "dimension": "mathematics",
            "status": "passed",
            "command": "uv run --frozen pytest tests/test_reliability_statistics.py",
            "result": "confidence-bound tests passed",
            "artifact_path": "benchmarks/reliability/v2-stable-pack.json",
            "references": ("benchmarks/reliability/statistics.py",),
        },
        {
            "gate_id": "mathematics.real-reliability-window",
            "dimension": "mathematics",
            "status": "blocked",
            "command": "QED_RUN_REAL_RELIABILITY_BENCHMARK=1 ...",
            "result": "0 real result rows; required window unrun",
            "artifact_path": reliability,
            "limitation": (
                "Dedicated credentials, quota, server-owned CODEX_HOME, and a real "
                "backend were not supplied."
            ),
            "references": ("docs/research/reliability-report-v2-stable.json",),
        },
        {
            "gate_id": "mathematics.sealed-holdout",
            "dimension": "mathematics",
            "status": "blocked",
            "command": "operator-supplied sealed holdout execution",
            "result": "holdout pack not supplied",
            "artifact_path": reliability,
            "limitation": "Expected labels were not exposed; no holdout hash or result exists.",
            "references": ("docs/research/reliability-report-v2-stable.json",),
        },
        {
            "gate_id": "mathematics.mutation-and-citation-metrics",
            "dimension": "mathematics",
            "status": "unrun",
            "command": "stable reliability result summarizer",
            "result": "300/100/100 real populations unavailable",
            "artifact_path": reliability,
            "limitation": "Fixture rates are explicitly excluded from the stable denominator.",
            "references": ("docs/research/reliability-report-v2-stable.md",),
        },
        {
            "gate_id": "maturity.alpha-tool-removal",
            "dimension": "maturity",
            "status": "passed",
            "command": "git grep -n -i impeccable -- production paths",
            "result": "alpha-only Impeccable runtime/development files removed",
            "artifact_path": "package-lock.json",
            "limitation": "Historical research records retain clearly marked references.",
            "references": ("docs/research/impeccable-phases.md",),
        },
        {
            "gate_id": "maturity.migration-recovery",
            "dimension": "maturity",
            "status": "passed",
            "command": (
                "uv run --frozen pytest tests/test_migration.py "
                "tests/test_persistence_migrations.py"
            ),
            "result": (
                "migration, staged backup/restore, and v1-v5 fixture matrix tests "
                "passed"
            ),
            "artifact_path": "tests/fixtures/migrations/manifest.json",
            "limitation": (
                "v3 is a documented v4-compatible fixture because no separate "
                "v3 DDL snapshot exists in repository history."
            ),
            "references": (
                "src/qed/persistence/migrations.py",
                "tests/test_persistence_migrations.py",
                "tests/fixtures/migrations/README.md",
            ),
        },
        {
            "gate_id": "maturity.fault-injection-concurrency",
            "dimension": "maturity",
            "status": "unrun",
            "command": (
                "QED_FAULT_INJECTION=all uv run --frozen pytest "
                "tests/test_service.py tests/test_store.py"
            ),
            "result": "systematic crash-point and repeated concurrency release window not run",
            "artifact_path": reliability,
            "limitation": (
                "Local race and idempotency regression tests exist, but the required "
                "durable-write crash matrix and repeated multi-process window have "
                "not been executed."
            ),
            "references": (
                "docs/release-v2-stable.md",
                "tests/test_service.py",
                "tests/test_store.py",
            ),
        },
        {
            "gate_id": "maturity.golden-run",
            "dimension": "maturity",
            "status": "passed",
            "command": "uv run --frozen qed verify-bundle artifacts/golden-bundle --json",
            "result": "offline golden bundle verified",
            "artifact_path": golden,
            "references": ("scripts/build_golden_bundle.py", "tests/test_golden_bundle.py"),
        },
        {
            "gate_id": "maturity.doctor",
            "dimension": "maturity",
            "status": "failed",
            "command": "uv run --frozen qed doctor --json",
            "result": (
                "doctor ran; local environment has unknown managed roots, exact "
                "catalog, and live capability"
            ),
            "artifact_path": "docs/research/doctor-v2-stable.json",
            "limitation": (
                "This worktree has no initialized managed data root or operator "
                "credentials; unknown checks are not promoted to pass."
            ),
            "references": ("src/qed/doctor.py",),
        },
        {
            "gate_id": "maturity.frontend-and-e2e",
            "dimension": "maturity",
            "status": "passed",
            "command": (
                "npm ci && npm run lint && npm run typecheck && npm test && "
                "npm run build && npm run test:e2e"
            ),
            "result": (
                "npm gate passed; 10 unit tests and 4 desktop Playwright assertions "
                "passed; 2 mobile cases explicitly skipped"
            ),
            "artifact_path": "docs/research/frontend-v2-stable-results.json",
            "limitation": (
                "Mobile-only Playwright cases are intentionally skipped by the "
                "existing viewport-specific test contract."
            ),
            "references": ("scripts/run_frontend_gate.py", "frontend/tests/console.spec.ts"),
        },
        {
            "gate_id": "maturity.ci-platform-and-stability",
            "dimension": "maturity",
            "status": "unrun",
            "command": "CI matrix and 20 consecutive critical runs",
            "result": "not run",
            "artifact_path": reliability,
            "limitation": (
                "Linux/macOS/Windows matrix, Python 3.13/3.14, Node minimum, and "
                "20-run flake evidence are not available in this worktree."
            ),
            "references": ("docs/release-v2-stable.md",),
        },
        {
            "gate_id": "maturity.real-codex-canary",
            "dimension": "maturity",
            "status": "blocked",
            "command": "uv run --frozen pytest -m real_codex",
            "result": "four real-Codex tests remain skipped",
            "artifact_path": reliability,
            "limitation": (
                "No dedicated credentials, quota, or server-owned CODEX_HOME were supplied."
            ),
            "references": ("docs/research/reliability-report-v2-stable.json",),
        },
        {
            "gate_id": "maturity.real-codex-conformance",
            "dimension": "maturity",
            "status": "blocked",
            "command": (
                "QED_RUN_REAL_CODEX_LIFECYCLE=1 uv run --frozen pytest "
                "tests/test_real_codex.py"
            ),
            "result": (
                "SDK/App Server lifecycle conformance is blocked by "
                "unavailable real Codex execution"
            ),
            "artifact_path": reliability,
            "limitation": (
                "Mock protocol fixtures pass, but authenticated SDK/App Server lifecycle, "
                "cancellation, resume, usage, and late-terminal evidence is unavailable."
            ),
            "references": ("docs/research/codex-runtime.md", "tests/test_real_codex.py"),
        },
        {
            "gate_id": "maturity.release-documentation",
            "dimension": "maturity",
            "status": "passed",
            "command": "git diff --check",
            "result": (
                "stable architecture, threat, migration, operations, reliability, "
                "and release docs updated"
            ),
            "artifact_path": "docs/release-v2-stable.md",
            "references": (
                "docs/architecture.md",
                "docs/threat-model.md",
                "docs/migration.md",
                "docs/operations.md",
            ),
        },
    ]
    return StableEvidence(
        commit_sha=COMMIT,
        generated_at=NOW.isoformat().replace("+00:00", "Z"),
        gates=tuple(_gate(**spec) for spec in specs),  # type: ignore[arg-type]
    )


def render_scorecard(evidence: StableEvidence) -> str:
    status_by_dimension = {
        dimension: evidence.eligible_for_10(dimension)
        for dimension in ("architecture", "security", "mathematics", "maturity")
    }
    labels = {
        "architecture": "软件架构",
        "security": "安全与审计",
        "mathematics": "数学正确性保障",
        "maturity": "稳定版成熟度",
    }
    lines = [
        "# QED v2 stable candidate scorecard",
        "",
        "> Generated from `v2-stable-evidence.json`; scores are not hand-entered.",
        "> Any required non-passed gate keeps that dimension below 10/10.",
        "",
        "| Dimension | Eligible for 10/10 | Required gate summary |",
        "| --- | --- | --- |",
    ]
    for dimension, label in labels.items():
        gates = tuple(
            gate
            for gate in evidence.gates
            if gate.dimension == dimension and gate.required
        )
        summary = ", ".join(f"{gate.gate_id}={gate.status}" for gate in gates)
        eligible = "YES" if status_by_dimension[dimension] else "NO"
        lines.append(f"| {label} (`{dimension}`) | {eligible} | {summary} |")
    lines.extend(
        [
            "",
            (
                "Current result: no dimension is eligible for 10/10. The real Codex "
                "reliability window, sealed holdout, final security closeout, "
                "coverage/mutation gates, platform matrix, and stability run "
                "remain blockers where marked."
            ),
            "",
            (
                "QED policy PASS is a code-computed policy decision. It is not formal "
                "verification, a mathematical truth claim, a signature, or a trusted timestamp."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    evidence = build_evidence()
    evidence_path = ROOT / "docs/research/v2-stable-evidence.json"
    evidence_path.write_text(canonical_json(evidence) + "\n", encoding="utf-8")
    (ROOT / "docs/research/v2-stable-scorecard.md").write_text(
        render_scorecard(evidence), encoding="utf-8"
    )
    print(evidence_path)


if __name__ == "__main__":
    main()
