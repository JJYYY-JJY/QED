from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from qed.config import QEDConfig
from qed.export import (
    ExportCollisionError,
    ExportIntegrityError,
    ExportNotReadyError,
    build_export_bundle,
    write_export_bundle,
)
from qed.inputs import RunInput
from qed.schemas import (
    Adjudication,
    CheckStatus,
    CitationSupport,
    Evidence,
    Finding,
    Plan,
    PlanStep,
    ProofCandidate,
    Provenance,
    VerificationCheck,
    VerificationReport,
    canonical_sha256,
    evidence_sha256,
    sha256_text,
)
from qed.stable_contracts import (
    ClaimCoverage,
    ClaimType,
    ProofObligation,
    ProofObligationGraph,
    VerifierRole,
)
from qed.store import RunSnapshot, RunStage, RunStatus, RunStore, ThreadStatus

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
GENERATED_AT = datetime(2026, 7, 16, 13, 0, tzinfo=UTC)
PROOF = "For every integer $n > 1$, a prime divisor of $n! + 1$ exceeds $n$."
EVIDENCE_CONTENT = "Euclid's construction produces a prime outside any finite list."
VerificationKind = Literal[
    "structural",
    "detailed",
    "assumptions_quantifiers",
    "counterexample_edge_case",
    "reconstruction",
    "citation",
]
RUN_INPUT = RunInput(
    problem="Prove that there are infinitely many primes.",
    verification_rules=("Every divisibility inference must be justified.",),
)


def _provenance(source_id: str, prompt_version: str) -> Provenance:
    return Provenance(
        source="codex",
        source_id=source_id,
        model="gpt-5.6-sol",
        runtime_version="0.144.5",
        prompt_version=prompt_version,
        captured_at=NOW,
    )


def _report(
    kind: VerificationKind,
    thread_id: str,
    status: CheckStatus = CheckStatus.PASS,
) -> VerificationReport:
    findings = (
        (
            Finding(
                id=f"finding-{kind}",
                check_id=f"check-{kind}",
                severity="major",
                summary="The required argument is invalid.",
                detail="A stated inference does not follow from the frozen proof.",
                proof_span="prime divisor",
            ),
        )
        if status is CheckStatus.FAIL
        else ()
    )
    return VerificationReport(
        id=f"report-{kind}",
        candidate_id="candidate-1",
        candidate_sha256=sha256_text(PROOF),
        kind=kind,
        checks=(
            VerificationCheck(
                id=f"check-{kind}",
                category="mathematical correctness",
                status=status,
                summary=f"The {kind} review found no defect.",
                proof_spans=("prime divisor",),
                evidence_ids=("evidence-1",) if kind == "citation" else (),
                citation_support=(
                    (
                        CitationSupport(
                            evidence_id="evidence-1",
                            proof_span=PROOF,
                            evidence_excerpt=EVIDENCE_CONTENT,
                            source_locator="evidence:evidence-1",
                        ),
                    )
                    if kind == "citation"
                    else ()
                ),
                rule_ids=(
                    (RUN_INPUT.frozen_verification_rules[0].id,)
                    if kind == "detailed"
                    else ()
                ),
            ),
        ),
        findings=findings,
        verifier_thread_id=thread_id,
        verifier_external_thread_id=f"codex-{kind}",
        provenance=_provenance(thread_id, f"verify-{kind}-v1"),
        created_at=NOW,
    )


def _snapshot(
    tmp_path: Path,
    *,
    evidence_ids: tuple[str, ...] = ("evidence-1",),
    complete: bool = False,
) -> RunSnapshot:
    run_input = RUN_INPUT
    evidence = Evidence(
        id="evidence-1",
        kind="theorem",
        title="Euclid IX.20",
        content=EVIDENCE_CONTENT,
        content_sha256=sha256_text(EVIDENCE_CONTENT),
        provenance=_provenance("thread-literature", "literature-v1"),
        citation="Euclid, Elements IX.20",
    )
    plan = Plan(
        id="plan-1",
        problem_sha256=run_input.sha256,
        strategy="Construct a prime divisor outside an arbitrary finite list.",
        steps=(
            PlanStep(
                id="construction",
                statement="Take a prime divisor of one plus the finite product.",
                rationale="Every listed prime leaves remainder one.",
                success_criteria=("The divisor exceeds the finite list.",),
                evidence_ids=("evidence-1",),
                key_step=True,
            ),
        ),
        provenance=_provenance("thread-planner", "planning-v1"),
        created_at=NOW,
    )

    with RunStore(tmp_path / "qed.sqlite3", clock=lambda: NOW) as store:
        store.create_run(
            "run-1",
            config=QEDConfig(),
            run_input=run_input,
            provenance=_provenance("intake", "intake-v1"),
        )
        store.transition_run("run-1", RunStatus.RUNNING)
        store.transition_stage("run-1", RunStage.LITERATURE)
        store.add_evidence("run-1", evidence)
        store.transition_stage("run-1", RunStage.PLANNING)
        store.add_plan("run-1", plan)
        store.transition_stage("run-1", RunStage.PROVING)
        store.add_thread(
            "thread-prover",
            run_id="run-1",
            role="prover",
            model="gpt-5.6-sol",
            provenance=_provenance("thread-prover", "proof-v2"),
            external_thread_id="codex-prover",
        )
        store.transition_thread("thread-prover", ThreadStatus.COMPLETED)
        candidate = ProofCandidate(
                id="candidate-1",
                run_id="run-1",
                plan_id="plan-1",
                attempt=1,
                proof=PROOF,
                proof_sha256=sha256_text(PROOF),
                evidence_ids=evidence_ids,
                provenance=_provenance("thread-prover", "proof-v2"),
                created_at=NOW,
            )
        store.create_candidate(
            candidate,
            thread_id="thread-prover",
        )
        store.seal_candidate("candidate-1")
        store.transition_stage("run-1", RunStage.VERIFICATION)

        reports: list[VerificationReport] = []
        report_kinds: tuple[VerificationKind, ...] = (
            "structural",
            "detailed",
            "assumptions_quantifiers",
            "counterexample_edge_case",
            "reconstruction",
            "citation",
        )
        for kind in report_kinds:
            thread_id = f"thread-{kind}"
            store.add_thread(
                thread_id,
                run_id="run-1",
                role="verifier",
                model="gpt-5.6-sol",
                provenance=_provenance(thread_id, f"verify-{kind}-v1"),
                external_thread_id=f"codex-{kind}",
            )
            store.transition_thread(thread_id, ThreadStatus.COMPLETED)
            reports.append(store.add_verification("run-1", _report(kind, thread_id)).report)

        role_by_kind = {
            "structural": VerifierRole.STRUCTURAL,
            "detailed": VerifierRole.DETAILED_STEP,
            "assumptions_quantifiers": VerifierRole.ASSUMPTIONS_QUANTIFIERS,
            "counterexample_edge_case": VerifierRole.COUNTEREXAMPLE_EDGE_CASE,
            "reconstruction": VerifierRole.RECONSTRUCTION,
            "citation": VerifierRole.CITATION,
        }
        graph = ProofObligationGraph.from_proof(
            candidate_sha256=canonical_sha256(candidate),
            proof=PROOF,
            nodes=(
                ProofObligation(
                    claim_id="claim-candidate-1",
                    byte_start=0,
                    byte_end=len(PROOF.encode("utf-8")),
                    span_sha256=sha256_text(PROOF),
                    claim_text=PROOF,
                    claim_type=ClaimType.CONCLUSION,
                    evidence_ids=evidence_ids,
                    rule_ids=(RUN_INPUT.frozen_verification_rules[0].id,),
                    coverage=tuple(
                        ClaimCoverage(
                            role=role_by_kind[report.kind],
                            status=report.verdict.value,
                            report_id=report.id,
                            check_id=report.checks[0].id,
                        )
                        for report in reports
                    ),
                ),
            ),
        )
        store.add_stage_output(
            "output-claim-graph",
            run_id="run-1",
            stage=RunStage.VERIFICATION,
            kind="claim_graph:candidate-1",
            content=graph.model_dump(mode="json"),
            provenance=_provenance("candidate-1", "qed-claim-graph-v1"),
        )

        store.transition_stage("run-1", RunStage.ADJUDICATION)
        store.add_thread(
            "thread-adjudicator",
            run_id="run-1",
            role="adjudicator",
            model="gpt-5.6-sol",
            provenance=_provenance("thread-adjudicator", "adjudication-v1"),
            external_thread_id="codex-adjudicator",
        )
        store.transition_thread("thread-adjudicator", ThreadStatus.COMPLETED)
        store.add_adjudication(
            "run-1",
            Adjudication(
                id="adjudication-1",
                candidate_id="candidate-1",
                report_ids=tuple(report.id for report in reports),
                outcome="accept",
                rationale="Every required independent report passed.",
                provenance=_provenance("thread-adjudicator", "adjudication-v1"),
                created_at=NOW,
            ),
        )
        store.record_decision("run-1", "candidate-1")
        store.transition_stage("run-1", RunStage.EXPORT)
        if complete:
            for kind in ("proof", "report", "manifest"):
                store.add_artifact(
                    f"artifact-{kind}",
                    run_id="run-1",
                    kind=kind,
                    media_type="application/octet-stream",
                    sha256=sha256_text(kind),
                    size_bytes=len(kind),
                    relative_path=f"exports/{kind}",
                    provenance=_provenance("candidate-1", "qed-export-v1"),
                )
            store.transition_stage("run-1", RunStage.COMPLETE)
            store.transition_run("run-1", RunStatus.COMPLETED)
        return store.snapshot("run-1")


def test_builds_a_deterministic_verified_bundle(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)

    first = build_export_bundle(
        snapshot,
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    second = build_export_bundle(
        snapshot,
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )

    assert first == second
    assert first.proof_md == (
        "# Proof\n\n"
        "Run: `run-1`  \n"
        "Candidate: `candidate-1`  \n"
        f"Proof SHA-256: `{sha256_text(PROOF)}`\n\n"
        f"{PROOF}\n"
    )
    assert "Code-computed verdict: **QED policy PASS**" in first.report_md
    assert "not peer review, formal or Lean verification" in first.report_md
    assert RUN_INPUT.frozen_verification_rules[0].id in first.report_md
    assert f"Evidence excerpt: {EVIDENCE_CONTENT}" in first.report_md
    assert "Source locator: evidence:evidence-1" in first.report_md
    assert first.report_md.index("## Citation") < first.report_md.index("## Detailed")
    assert first.report_md.index("## Detailed") < first.report_md.index("## Structural")

    manifest = json.loads(first.manifest_json)
    assert snapshot.run_input is not None
    assert snapshot.run.stage is RunStage.EXPORT
    assert snapshot.run.status is RunStatus.RUNNING
    assert manifest["run_id"] == "run-1"
    assert manifest["run_status"] == "running"
    assert manifest["run_stage"] == "export"
    assert manifest["publication_phase"] == "snapshot"
    assert manifest["code_verdict"] == "PASS"
    assert manifest["verification_rules"] == [
        {
            "id": RUN_INPUT.frozen_verification_rules[0].id,
            "text": "Every divisibility inference must be justified.",
                "responsible_report_kinds": [
                    "structural",
                    "detailed",
                    "assumptions_quantifiers",
                    "counterexample_edge_case",
                    "reconstruction",
                    "citation",
                ],
            "coverage": [
                {
                    "report_id": "report-detailed",
                    "check_id": "check-detailed",
                    "status": "pass",
                }
            ],
        }
    ]
    assert manifest["input_sha256"] == snapshot.run_input.sha256
    assert manifest["config_sha256"] == QEDConfig().sha256
    assert manifest["runtime_version"] == "0.144.5"
    assert manifest["generated_at"] == "2026-07-16T13:00:00Z"
    assert manifest["first_event_seq"] == 1
    assert manifest["last_event_seq"] == 39
    assert manifest["prompt_versions"] == {
        "adjudication:adjudication-1": "adjudication-v1",
        "candidate:candidate-1": "proof-v2",
        "evidence:evidence-1": "literature-v1",
        "plan:plan-1": "planning-v1",
        "run:run-1": "intake-v1",
            "verification:report-citation": "verify-citation-v1",
            "verification:report-detailed": "verify-detailed-v1",
            "verification:report-structural": "verify-structural-v1",
            "verification:report-assumptions_quantifiers":
            "verify-assumptions_quantifiers-v1",
            "verification:report-counterexample_edge_case":
            "verify-counterexample_edge_case-v1",
            "verification:report-reconstruction": "verify-reconstruction-v1",
        }
    assert manifest["candidate_hashes"] == [snapshot.candidates[0].candidate_sha256]
    assert manifest["verification_hashes"] == [
        record.report_sha256
        for record in sorted(
            snapshot.verifications,
            key=lambda record: (record.kind, record.id),
        )
    ]
    assert manifest["evidence_records"] == [
        {"id": "evidence-1", "sha256": evidence_sha256(snapshot.evidence[0])}
    ]
    assert manifest["evidence_provenance"] == [
        {
            "content_trust": "legacy_untrusted",
            "evidence_id": "evidence-1",
            "observation_ids": [],
            "schema_version": 1,
            "source_trust": "legacy_untrusted",
            "source_uri_sha256": None,
        }
    ]
    assert manifest["web_search_observations"] == []
    assert manifest["citation_support"] == [
        {
            "report_id": "report-citation",
            "check_id": "check-citation",
            "evidence_id": "evidence-1",
            "proof_span": PROOF,
            "evidence_excerpt": EVIDENCE_CONTENT,
            "source_locator": "evidence:evidence-1",
            "sha256": canonical_sha256(
                next(
                    record
                    for record in snapshot.verifications
                    if record.kind == "citation"
                ).report.checks[0].citation_support[0]
            ),
        }
    ]
    assert manifest["plan_records"] == [
        {"id": "plan-1", "sha256": canonical_sha256(snapshot.plans[0])}
    ]
    assert manifest["candidate_records"] == [
        {
            "id": "candidate-1",
            "sha256": snapshot.candidates[0].candidate_sha256,
        }
    ]
    assert [item["id"] for item in manifest["verification_records"]] == [
        "report-assumptions_quantifiers",
        "report-citation",
        "report-counterexample_edge_case",
        "report-detailed",
        "report-reconstruction",
        "report-structural",
    ]
    assert manifest["adjudication_records"] == [
        {
            "id": "adjudication-1",
            "sha256": canonical_sha256(snapshot.adjudications[0]),
        }
    ]
    assert manifest["decision_records"] == [
        {
            "id": "candidate-1",
            "sha256": canonical_sha256(snapshot.decisions[0]),
        }
    ]
    assert manifest["runtime_resolutions"] == []
    assert manifest["execution_segments"] == []
    assert manifest["usage"] == {
        "cached_input_tokens": 0,
        "execution_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "search_queries": 0,
        "turns": 0,
    }
    assert len(manifest["event_chain_sha256"]) == 64
    assert [artifact["relative_path"] for artifact in manifest["artifacts"]] == [
        "proof.md",
        "report.md",
        "event-chain.json",
        "audit.json",
    ]
    assert manifest["artifacts"][0]["sha256"] == sha256_text(first.proof_md)
    assert manifest["artifacts"][1]["sha256"] == sha256_text(first.report_md)
    assert first.bundle_sha256 == sha256_text(first.manifest_json)
    assert set(first.files) == {
        "manifest.json",
        "proof.md",
        "report.md",
        "event-chain.json",
        "audit.json",
    }


def test_refuses_missing_or_failed_required_reports(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)
    missing_detailed = snapshot.model_copy(
        update={
            "verifications": tuple(
                record for record in snapshot.verifications if record.kind != "detailed"
            )
        }
    )
    missing_citation = snapshot.model_copy(
        update={
            "verifications": tuple(
                record for record in snapshot.verifications if record.kind != "citation"
            )
        }
    )
    failed_report = _report("structural", "thread-structural", CheckStatus.FAIL)
    failed_record = next(
        record for record in snapshot.verifications if record.kind == "structural"
    ).model_copy(
        update={
            "report": failed_report,
            "report_sha256": canonical_sha256(failed_report),
            "candidate_sha256": failed_report.candidate_sha256,
            "provenance": failed_report.provenance,
            "provenance_sha256": canonical_sha256(failed_report.provenance),
        }
    )
    failed = snapshot.model_copy(
        update={
            "verifications": tuple(
                failed_record if record.kind == "structural" else record
                for record in snapshot.verifications
            )
        }
    )

    with pytest.raises(ExportNotReadyError, match="missing:detailed"):
        build_export_bundle(
            missing_detailed,
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ExportNotReadyError, match="missing:citation"):
        build_export_bundle(
            missing_citation,
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ExportNotReadyError, match="non_pass:structural:fail"):
        build_export_bundle(
            failed,
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )


def test_requires_citation_from_frozen_ledger_when_candidate_omits_evidence_ids(
    tmp_path,
) -> None:
    omitted_ids = _snapshot(tmp_path, evidence_ids=())

    bundle = build_export_bundle(
        omitted_ids,
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )

    assert (
        "Required reports: `structural`, `detailed`, `assumptions_quantifiers`, "
        "`counterexample_edge_case`, `reconstruction`, `citation`"
    ) in bundle.report_md
    assert "## Citation" in bundle.report_md


def test_refuses_an_unsealed_candidate(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)
    unsealed = snapshot.candidates[0].model_copy(update={"sealed_at": None})
    snapshot = snapshot.model_copy(update={"candidates": (unsealed,)})

    with pytest.raises(ExportNotReadyError, match="not sealed"):
        build_export_bundle(
            snapshot,
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )


def test_refuses_missing_or_inconsistent_input_plan_and_evidence(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)
    variants = (
        ("typed run input", snapshot.model_copy(update={"run_input": None})),
        (
            "run input hash",
            snapshot.model_copy(
                update={"run_input": RunInput(problem="A different frozen problem.")}
            ),
        ),
        ("referenced plan", snapshot.model_copy(update={"plans": ()})),
        ("referenced evidence", snapshot.model_copy(update={"evidence": ()})),
    )

    for message, invalid in variants:
        with pytest.raises(ExportIntegrityError, match=message):
            build_export_bundle(
                invalid,
                candidate_id="candidate-1",
                generated_at=GENERATED_AT,
            )


def test_refuses_missing_or_disagreeing_decision_and_adjudication(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)
    changed_decision = snapshot.decisions[0].model_copy(
        update={"passed": False, "reasons": ("tampered",)}
    )
    changed_adjudication = snapshot.adjudications[-1].model_copy(update={"outcome": "revise_proof"})
    wrong_reports = snapshot.adjudications[-1].model_copy(
        update={"report_ids": (snapshot.verifications[0].id,)}
    )
    wrong_adjudicator = snapshot.adjudications[-1].model_copy(
        update={"provenance": _provenance("thread-prover", "adjudication-v1")}
    )
    variants = (
        ("persisted code decision", snapshot.model_copy(update={"decisions": ()})),
        (
            "persisted code decision",
            snapshot.model_copy(update={"decisions": (changed_decision,)}),
        ),
        ("typed adjudication", snapshot.model_copy(update={"adjudications": ()})),
        (
            "accepting adjudication",
            snapshot.model_copy(update={"adjudications": (changed_adjudication,)}),
        ),
        (
            "adjudication report ids",
            snapshot.model_copy(update={"adjudications": (wrong_reports,)}),
        ),
        (
            "adjudicator thread",
            snapshot.model_copy(update={"adjudications": (wrong_adjudicator,)}),
        ),
    )

    for message, invalid in variants:
        with pytest.raises(ExportIntegrityError, match=message):
            build_export_bundle(
                invalid,
                candidate_id="candidate-1",
                generated_at=GENERATED_AT,
            )


def test_refuses_run_stage_and_status_combinations_outside_export_contract(
    tmp_path,
) -> None:
    snapshot = _snapshot(tmp_path)
    variants = (
        snapshot.model_copy(
            update={"run": snapshot.run.model_copy(update={"stage": RunStage.PROVING})}
        ),
        snapshot.model_copy(
            update={"run": snapshot.run.model_copy(update={"status": RunStatus.COMPLETED})}
        ),
        snapshot.model_copy(
            update={
                "run": snapshot.run.model_copy(
                    update={"stage": RunStage.COMPLETE, "status": RunStatus.RUNNING}
                )
            }
        ),
    )

    for invalid in variants:
        with pytest.raises(ExportIntegrityError, match="export or completed stage"):
            build_export_bundle(
                invalid,
                candidate_id="candidate-1",
                generated_at=GENERATED_AT,
            )


def test_completed_run_can_rebuild_its_terminal_manifest(tmp_path) -> None:
    snapshot = _snapshot(tmp_path, complete=True)

    bundle = build_export_bundle(
        snapshot,
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )

    assert snapshot.run.stage is RunStage.COMPLETE
    assert snapshot.run.status is RunStatus.COMPLETED
    assert bundle.manifest.run_status == "completed"
    assert bundle.manifest.code_verdict == "PASS"
    assert bundle.manifest.last_event_seq == 44


def test_refuses_missing_reused_or_writer_external_verifier_identities(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)

    def change_external(kind: str, external_id: str | None) -> RunSnapshot:
        changed_records = []
        for record in snapshot.verifications:
            if record.kind != kind:
                changed_records.append(record)
                continue
            report = record.report.model_copy(update={"verifier_external_thread_id": external_id})
            changed_records.append(
                record.model_copy(
                    update={
                        "report": report,
                        "report_sha256": canonical_sha256(report),
                    }
                )
            )
        changed_threads = tuple(
            thread.model_copy(update={"external_thread_id": external_id})
            if thread.id == f"thread-{kind}"
            else thread
            for thread in snapshot.threads
        )
        return snapshot.model_copy(
            update={
                "verifications": tuple(changed_records),
                "threads": changed_threads,
            }
        )

    variants = (
        change_external("structural", None),
        change_external("detailed", "codex-structural"),
        change_external("structural", "codex-prover"),
    )
    for invalid in variants:
        with pytest.raises(ExportIntegrityError, match="external verifier"):
            build_export_bundle(
                invalid,
                candidate_id="candidate-1",
                generated_at=GENERATED_AT,
            )


def test_refuses_event_gaps_payload_tampering_and_terminal_stage_drift(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)
    gap = snapshot.model_copy(update={"events": snapshot.events[:8] + snapshot.events[9:]})
    payload_event = snapshot.events[4].model_copy(update={"payload": {"tampered": True}})
    bad_payload = snapshot.model_copy(
        update={
            "events": tuple(
                payload_event if event.seq == payload_event.seq else event
                for event in snapshot.events
            )
        }
    )
    terminal_event = snapshot.events[-1].model_copy(update={"stage": "adjudication"})
    terminal_drift = snapshot.model_copy(
        update={"events": snapshot.events[:-1] + (terminal_event,)}
    )
    variants = (
        ("event sequence", gap),
        ("event payload hash", bad_payload),
        ("event stage", terminal_drift),
    )

    for message, invalid in variants:
        with pytest.raises(ExportIntegrityError, match=message):
            build_export_bundle(
                invalid,
                candidate_id="candidate-1",
                generated_at=GENERATED_AT,
            )


def test_writes_a_content_addressed_bundle_idempotently(tmp_path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    managed_root = tmp_path / "exports"

    first = write_export_bundle(bundle, managed_root)
    second = write_export_bundle(bundle, managed_root)

    assert first == second
    assert first == managed_root.absolute() / "run-1" / bundle.bundle_sha256
    assert {path.name for path in first.iterdir()} == set(bundle.files)
    for name, expected in bundle.files.items():
        assert (first / name).read_bytes() == expected


def test_detects_snapshot_and_written_artifact_tampering(tmp_path) -> None:
    snapshot = _snapshot(tmp_path / "state")
    candidate = snapshot.candidates[0].model_copy(update={"candidate_sha256": "0" * 64})
    tampered_snapshot = snapshot.model_copy(update={"candidates": (candidate,)})

    with pytest.raises(ExportIntegrityError, match="candidate hash"):
        build_export_bundle(
            tampered_snapshot,
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )

    bundle = build_export_bundle(
        snapshot,
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    destination = write_export_bundle(bundle, tmp_path / "exports")
    (destination / "proof.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ExportCollisionError, match="does not match bundle"):
        write_export_bundle(bundle, tmp_path / "exports")


def test_writer_rejects_unsafe_ids_symlinks_and_collisions(tmp_path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )

    with pytest.raises(ExportCollisionError, match="unsafe run id"):
        write_export_bundle(replace(bundle, run_id="../escape"), tmp_path / "unsafe")

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ExportCollisionError, match="symlink"):
        write_export_bundle(bundle, linked_root)

    collision_root = tmp_path / "collision"
    run_address = collision_root / "run-1"
    run_address.parent.mkdir()
    run_address.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ExportCollisionError, match="not a directory"):
        write_export_bundle(bundle, collision_root)


def test_concurrent_writers_atomically_publish_the_same_bundle(tmp_path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    root = tmp_path / "exports"
    workers = 16
    barrier = threading.Barrier(workers)

    def publish() -> Path:
        barrier.wait()
        return write_export_bundle(bundle, root)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = tuple(pool.submit(publish) for _ in range(workers))
        destinations = tuple(future.result() for future in futures)

    assert len(set(destinations)) == 1
    destination = destinations[0]
    for name, expected in bundle.files.items():
        assert (destination / name).read_bytes() == expected


def test_writer_ignores_a_crash_left_staging_directory(tmp_path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    run_directory = tmp_path / "exports" / bundle.run_id
    run_directory.mkdir(parents=True)
    stale = run_directory / f".{bundle.bundle_sha256}.crashed"
    stale.mkdir()
    (stale / "proof.md").write_text("partial", encoding="utf-8")

    destination = write_export_bundle(bundle, tmp_path / "exports")

    assert destination.name == bundle.bundle_sha256
    assert (destination / "manifest.json").read_bytes() == bundle.files["manifest.json"]
