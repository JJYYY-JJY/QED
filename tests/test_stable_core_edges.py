from __future__ import annotations

import errno
import os
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import qed.export as export_module
from qed.decision import decide_candidate
from qed.export import (
    ExportBundle,
    ExportCollisionError,
    ExportError,
    ExportIntegrityError,
    ExportNotReadyError,
    _candidate_record,
    _discard_staging,
    _manifest_execution_segments,
    _manifest_turns,
    _manifest_usage,
    _manifest_window,
    _render_report,
    _validate_existing,
    _validated_files,
    build_export_bundle,
    write_export_bundle,
)
from qed.persistence.migrations import preflight_database
from qed.schemas import (
    CheckStatus,
    Event,
    Evidence,
    EvidenceTrust,
    Finding,
    WebSearchObservation,
    canonical_json,
    canonical_sha256,
    sha256_text,
)
from qed.store import (
    ExecutionLease,
    RunStage,
    RuntimeResolutionRecord,
    StageOutputRecord,
)
from qed.store_schema import DuplicateExternalThreadIdentityError, prepare_schema_migration

from .test_decision import candidate, citation_support, evidence, report
from .test_export import GENERATED_AT, NOW, _provenance, _snapshot


def _readdress(bundle: ExportBundle, **updates: object) -> ExportBundle:
    manifest = updates.pop("manifest", bundle.manifest)
    manifest_json = f"{canonical_json(manifest)}\n"
    return replace(
        bundle,
        manifest=manifest,
        manifest_json=manifest_json,
        bundle_sha256=sha256_text(manifest_json),
        **updates,
    )


def _manifest_with_artifact_hash(bundle: ExportBundle, name: str, digest: str) -> ExportBundle:
    artifacts = tuple(
        item.model_copy(update={"sha256": digest}) if item.relative_path == name else item
        for item in bundle.manifest.artifacts
    )
    return _readdress(bundle, manifest=bundle.manifest.model_copy(update={"artifacts": artifacts}))


def test_state_artifacts_and_legal_transition_helper_cover_all_tables() -> None:
    from qed.domain.state import (
        COMMAND_SPECS,
        RunStage,
        RunStatus,
        ThreadStatus,
        is_legal_transition,
        transition_table,
    )

    table = transition_table()
    assert set(table) == {"run", "stage", "thread", "commands"}
    assert len(table["commands"]) == len(COMMAND_SPECS)
    assert is_legal_transition(
        {RunStatus.CREATED: frozenset({RunStatus.RUNNING})},
        RunStatus.CREATED,
        RunStatus.RUNNING,
    )
    assert not is_legal_transition({}, RunStatus.CREATED, RunStatus.RUNNING)
    assert RunStage.COMPLETE.value in table["stage"]
    assert ThreadStatus.COMPLETED.value in table["thread"]


def test_decision_rejects_integrity_and_duplicate_policy_inputs() -> None:
    proof = candidate()
    good_reports = (
        report(proof, kind="structural", status=CheckStatus.PASS, thread_id="s"),
        report(proof, kind="detailed", status=CheckStatus.PASS, thread_id="d"),
    )

    with pytest.raises(ValueError, match="prover_external_thread_id"):
        decide_candidate(proof, good_reports, prover_external_thread_id=" ")
    with pytest.raises(ValueError, match="required_rule_ids"):
        decide_candidate(
            proof,
            good_reports,
            prover_external_thread_id="writer",
            required_rule_ids=("rule", "rule"),
        )
    source = evidence()
    with pytest.raises(ValueError, match="required evidence ids"):
        decide_candidate(
            proof,
            good_reports,
            prover_external_thread_id="writer",
            required_evidence=(source, source),
        )
    with pytest.raises(ValueError, match="records and ids disagree"):
        decide_candidate(
            proof,
            good_reports,
            prover_external_thread_id="writer",
            required_evidence=(source,),
            required_evidence_ids=("other",),
        )
    with pytest.raises(ValueError, match="required report kinds"):
        decide_candidate(
            proof,
            good_reports,
            prover_external_thread_id="writer",
            required_report_kinds=("structural", "structural"),
        )
    with pytest.raises(ValueError, match="citation is required"):
        decide_candidate(
            proof,
            good_reports,
            prover_external_thread_id="writer",
            require_citation=True,
            required_report_kinds=("structural", "detailed"),
        )

    wrong_id = good_reports[0].model_copy(update={"candidate_id": "other"})
    with pytest.raises(ValueError, match="another candidate"):
        decide_candidate(
            proof,
            (wrong_id, good_reports[1]),
            prover_external_thread_id="writer",
        )
    wrong_hash = good_reports[0].model_copy(update={"candidate_sha256": "0" * 64})
    with pytest.raises(ValueError, match="another candidate hash"):
        decide_candidate(
            proof,
            (wrong_hash, good_reports[1]),
            prover_external_thread_id="writer",
        )


def test_decision_rejects_unbound_and_mismatched_citation_support() -> None:
    proof = candidate()
    source = evidence()
    base = (
        report(proof, kind="structural", status=CheckStatus.PASS, thread_id="s"),
        report(proof, kind="detailed", status=CheckStatus.PASS, thread_id="d"),
    )
    citation = report(
        proof,
        kind="citation",
        status=CheckStatus.PASS,
        thread_id="c",
        evidence_ids=(source.id,),
        citation_support=(citation_support(proof, source),),
    )

    no_records = decide_candidate(
        proof,
        base + (citation,),
        prover_external_thread_id="writer",
        required_evidence_ids=(source.id,),
    )
    assert "citation_evidence_records_missing" in no_records.reasons

    bad_check = citation.checks[0].model_copy(
        update={
            "citation_support": (
                citation_support(
                    proof,
                    source,
                    proof_span="not in proof",
                    excerpt="not in evidence",
                ).model_copy(update={"source_locator": "wrong-locator"}),
            )
        }
    )
    bad_citation = citation.model_copy(update={"checks": (bad_check,)})
    bad = decide_candidate(
        proof,
        base + (bad_citation,),
        prover_external_thread_id="writer",
        required_evidence=(source,),
    )
    assert any("citation_proof_span_mismatch" in reason for reason in bad.reasons)
    assert any("citation_excerpt_mismatch" in reason for reason in bad.reasons)
    assert any("citation_source_locator_mismatch" in reason for reason in bad.reasons)

    failing_check = citation.checks[0].model_copy(update={"status": CheckStatus.FAIL})
    failing = decide_candidate(
        proof,
        base + (citation.model_copy(update={"checks": (failing_check,)}),),
        prover_external_thread_id="writer",
        required_evidence=(source,),
    )
    assert "non_pass:citation:fail" in failing.reasons
    assert "citation_missing_evidence:evidence-1" in failing.reasons

    wrong_kind_check = (
        base[0]
        .checks[0]
        .model_copy(update={"citation_support": (citation_support(proof, source),)})
    )
    wrong_kind = base[0].model_copy(update={"checks": (wrong_kind_check,)})
    result = decide_candidate(
        proof,
        (wrong_kind, base[1], citation),
        prover_external_thread_id="writer",
        required_evidence=(source,),
    )
    assert any("citation_support_wrong_report_kind" in reason for reason in result.reasons)


def test_validated_export_bundle_rejects_each_integrity_component(tmp_path: Path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )

    with pytest.raises(ExportIntegrityError, match="canonical"):
        _validated_files(replace(bundle, manifest_json=bundle.manifest_json + " "))
    with pytest.raises(ExportIntegrityError, match="content address"):
        _validated_files(replace(bundle, bundle_sha256="0" * 64))
    with pytest.raises(ExportIntegrityError, match="run identity"):
        _validated_files(replace(bundle, run_id="other"))
    with pytest.raises(ExportIntegrityError, match="manifest must contain"):
        _validated_files(
            _readdress(
                bundle,
                manifest=bundle.manifest.model_copy(
                    update={"artifacts": bundle.manifest.artifacts[:-1]}
                ),
            )
        )
    for name, message in (
        ("proof.md", "proof artifact"),
        ("report.md", "report artifact"),
        ("event-chain.json", "event-chain artifact"),
        ("audit.json", "audit artifact"),
    ):
        with pytest.raises(ExportIntegrityError, match=message):
            _validated_files(_manifest_with_artifact_hash(bundle, name, "0" * 64))

    invalid_json = _manifest_with_artifact_hash(bundle, "event-chain.json", sha256_text("{"))
    with pytest.raises(ExportIntegrityError, match="JSON event list"):
        _validated_files(replace(invalid_json, event_chain_json="{"))
    empty_events = _manifest_with_artifact_hash(bundle, "event-chain.json", sha256_text("[]\n"))
    with pytest.raises(ExportIntegrityError, match="event-chain hash"):
        _validated_files(replace(empty_events, event_chain_json="[]\n"))


def test_export_writer_rejects_existing_shape_and_publication_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    files = bundle.files

    non_directory = tmp_path / "not-a-directory"
    non_directory.write_text("x", encoding="utf-8")
    with pytest.raises(ExportCollisionError, match="regular directory"):
        _validate_existing(non_directory, files)

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "unexpected").write_text("x", encoding="utf-8")
    with pytest.raises(ExportCollisionError, match="unexpected files"):
        _validate_existing(existing, files)

    for name in files:
        target = tmp_path / f"missing-{name.replace('.', '-')}"
        target.mkdir()
        for other, content in files.items():
            if other != name:
                (target / other).write_bytes(content)
        with pytest.raises(ExportCollisionError, match="missing"):
            _validate_existing(target, files)

    _discard_staging(tmp_path / "does-not-exist")
    root_file = tmp_path / "root-file"
    root_file.write_text("x", encoding="utf-8")
    with pytest.raises(ExportCollisionError, match="managed export root"):
        write_export_bundle(bundle, root_file)

    root = tmp_path / "run-symlink-root"
    root.mkdir()
    real = tmp_path / "real-run"
    real.mkdir()
    (root / bundle.run_id).symlink_to(real, target_is_directory=True)
    with pytest.raises(ExportCollisionError, match="run directory is a symlink"):
        write_export_bundle(bundle, root)

    def reject_descendant(_root: Path, _child: Path) -> None:
        raise export_module.PathSecurityError("injected escape")

    monkeypatch.setattr(export_module, "require_descendant", reject_descendant)
    with pytest.raises(ExportCollisionError, match="injected escape"):
        write_export_bundle(bundle, tmp_path / "escape-root")

    complete = tmp_path / "complete"
    complete.mkdir()
    for name, content in files.items():
        (complete / name).write_bytes(content)
    missing_target = complete / "proof.md"
    real_lstat = Path.lstat

    def missing_lstat(path: Path) -> os.stat_result:
        if path == missing_target:
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", missing_lstat)
    with pytest.raises(ExportCollisionError, match="artifact is missing"):
        _validate_existing(complete, files)


def test_export_writer_rejects_post_mkdir_shape_and_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    original_is_dir = Path.is_dir
    root = (tmp_path / "post-mkdir-root").absolute()

    def root_is_not_dir(path: Path) -> bool:
        if path == root:
            return False
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", root_is_not_dir)
    with pytest.raises(ExportCollisionError, match="managed export root"):
        write_export_bundle(bundle, root)

    monkeypatch.undo()
    root = (tmp_path / "post-run-root").absolute()
    original_stat = Path.stat
    calls = 0

    def changed_root_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal calls
        result = original_stat(path, *args, **kwargs)
        if path == root:
            calls += 1
            if calls >= 2:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", changed_root_stat)
    with pytest.raises(ExportCollisionError, match="changed during publication"):
        write_export_bundle(bundle, root)

    monkeypatch.undo()
    root = (tmp_path / "post-run-shape").absolute()
    original_is_dir = Path.is_dir

    def run_is_not_dir(path: Path) -> bool:
        if path == root / bundle.run_id:
            return False
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", run_is_not_dir)
    with pytest.raises(ExportCollisionError, match="run address is not a directory"):
        write_export_bundle(bundle, root)


def test_export_writer_rejects_publish_os_error_and_mkdir_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    original_mkdir = Path.mkdir

    def race_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == bundle.run_id and kwargs.get("exist_ok") is True:
            raise FileExistsError(self)
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race_mkdir)
    with pytest.raises(ExportCollisionError, match="run address"):
        write_export_bundle(bundle, tmp_path / "mkdir-race")

    monkeypatch.undo()
    original_rename = Path.rename

    def failing_rename(self: Path, target: Path) -> Path:
        if self.name.startswith(".") and target.name == bundle.bundle_sha256:
            raise OSError(errno.EPERM, "injected publish failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(ExportError, match="could not publish"):
        write_export_bundle(bundle, tmp_path / "rename-failure")
    assert not list((tmp_path / "rename-failure" / bundle.run_id).glob(".*"))


def test_export_snapshot_rejects_hash_identity_and_lineage_variants(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "state")
    record = snapshot.candidates[0]

    variants: list[tuple[str, object]] = [
        ("selected candidate", snapshot.model_copy(update={"candidates": ()})),
        (
            "config hash",
            snapshot.model_copy(
                update={"run": snapshot.run.model_copy(update={"config_sha256": "0" * 64})}
            ),
        ),
        (
            "provenance hash",
            snapshot.model_copy(
                update={"run": snapshot.run.model_copy(update={"provenance_sha256": "0" * 64})}
            ),
        ),
        (
            "runtime version",
            snapshot.model_copy(
                update={"run": snapshot.run.model_copy(update={"runtime_version": "other"})}
            ),
        ),
        (
            "duplicate evidence",
            snapshot.model_copy(update={"evidence": (snapshot.evidence[0], snapshot.evidence[0])}),
        ),
        (
            "duplicate plan identities",
            snapshot.model_copy(update={"plans": (snapshot.plans[0], snapshot.plans[0])}),
        ),
        (
            "candidate belongs",
            snapshot.model_copy(
                update={"candidates": (record.model_copy(update={"run_id": "other"}),)}
            ),
        ),
        (
            "candidate record identity",
            snapshot.model_copy(
                update={
                    "candidates": (
                        record.model_copy(
                            update={
                                "candidate": record.candidate.model_copy(update={"id": "other"})
                            }
                        ),
                    )
                }
            ),
        ),
        (
            "candidate metadata",
            snapshot.model_copy(update={"candidates": (record.model_copy(update={"attempt": 2}),)}),
        ),
        (
            "candidate hash",
            snapshot.model_copy(
                update={"candidates": (record.model_copy(update={"candidate_sha256": "0" * 64}),)}
            ),
        ),
        (
            "candidate proof hash",
            snapshot.model_copy(
                update={"candidates": (record.model_copy(update={"proof_sha256": "0" * 64}),)}
            ),
        ),
        (
            "candidate provenance",
            snapshot.model_copy(
                update={
                    "candidates": (
                        record.model_copy(update={"provenance": _provenance("other", "proof-v2")}),
                    )
                }
            ),
        ),
        (
            "candidate provenance hash",
            snapshot.model_copy(
                update={"candidates": (record.model_copy(update={"provenance_sha256": "0" * 64}),)}
            ),
        ),
        (
            "duplicate thread identities",
            snapshot.model_copy(update={"threads": (snapshot.threads[0], *snapshot.threads)}),
        ),
    ]
    for message, invalid in variants:
        with pytest.raises((ExportNotReadyError, ExportIntegrityError), match=message):
            build_export_bundle(invalid, candidate_id="candidate-1", generated_at=GENERATED_AT)

    with pytest.raises(ExportIntegrityError, match="writer thread lineage"):
        threads = tuple(
            thread.model_copy(update={"role": "verifier"})
            if thread.id == "thread-prover"
            else thread
            for thread in snapshot.threads
        )
        build_export_bundle(
            snapshot.model_copy(update={"threads": threads}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )


def test_export_snapshot_rejects_report_event_and_execution_variants(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "state")
    first = snapshot.verifications[0]

    def with_report(
        update_record: dict[str, object], report_update: dict[str, object] | None = None
    ):
        record = first.model_copy(update=update_record)
        if report_update:
            changed = record.report.model_copy(update=report_update)
            record = record.model_copy(
                update={
                    "report": changed,
                    "report_sha256": canonical_sha256(changed),
                }
            )
        return snapshot.model_copy(
            update={
                "verifications": (
                    record,
                    *snapshot.verifications[1:],
                )
            }
        )

    variants = (
        ("report belongs", with_report({"run_id": "other"})),
        ("report identity", with_report({"id": "other"})),
        ("report metadata", with_report({"thread_id": "other"})),
        ("report hash", with_report({"report_sha256": "0" * 64})),
        ("report candidate hash", with_report({"candidate_sha256": "0" * 64})),
        ("report provenance", with_report({"provenance": _provenance("other", "v")})),
        ("report provenance hash", with_report({"provenance_sha256": "0" * 64})),
        ("report identity", with_report({}, {"id": "other"})),
    )
    for message, invalid in variants:
        with pytest.raises(ExportIntegrityError, match=message):
            build_export_bundle(invalid, candidate_id="candidate-1", generated_at=GENERATED_AT)

    bad_thread = tuple(
        thread.model_copy(update={"role": "prover"}) if thread.id == first.thread_id else thread
        for thread in snapshot.threads
    )
    with pytest.raises(ExportIntegrityError, match="external verifier thread lineage"):
        build_export_bundle(
            snapshot.model_copy(update={"threads": bad_thread}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )

    event = snapshot.events[0]
    with pytest.raises(ExportIntegrityError, match="event belongs"):
        build_export_bundle(
            snapshot.model_copy(
                update={
                    "events": (event.model_copy(update={"run_id": "other"}), *snapshot.events[1:])
                }
            ),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ExportIntegrityError, match="event sequence"):
        build_export_bundle(
            snapshot.model_copy(update={"events": ()}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )

    segment = ExecutionLease(
        id="segment-1",
        run_id="run-1",
        worker_id="worker",
        version=1,
        runtime_version=None,
        runtime_resolution_sha256=None,
        lease_expires_at=NOW + timedelta(seconds=30),
        released_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ExportIntegrityError, match="no runtime version"):
        build_export_bundle(
            snapshot.model_copy(update={"execution_segments": (segment,)}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )

    plan = snapshot.plans[0].model_copy(update={"problem_sha256": "0" * 64})
    with pytest.raises(ExportIntegrityError, match="plan input hash"):
        build_export_bundle(
            snapshot.model_copy(update={"plans": (plan,)}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )

    candidate = snapshot.candidates[0].candidate.model_copy(update={"proof_sha256": "0" * 64})
    changed_record = snapshot.candidates[0].model_copy(
        update={
            "candidate": candidate,
            "candidate_sha256": canonical_sha256(candidate),
            "proof_sha256": "0" * 64,
        }
    )
    changed_reports = tuple(
        item.model_copy(update={"candidate_sha256": "0" * 64}) for item in snapshot.verifications
    )
    with pytest.raises(ExportIntegrityError, match="candidate proof hash"):
        build_export_bundle(
            snapshot.model_copy(
                update={
                    "candidates": (changed_record,),
                    "verifications": changed_reports,
                }
            ),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )

    resolution = RuntimeResolutionRecord(
        segment_id="segment-1",
        run_id="run-1",
        schema_version=1,
        resolution={"backend": "fixture"},
        resolution_sha256=canonical_sha256({"backend": "fixture"}),
        created_at=NOW,
    )
    duplicate_resolutions = snapshot.model_copy(
        update={"runtime_resolutions": (resolution, resolution)}
    )
    with pytest.raises(ExportIntegrityError, match="duplicate runtime resolutions"):
        build_export_bundle(
            duplicate_resolutions,
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    unresolved_segment = segment.model_copy(update={"runtime_version": "runtime"})
    with pytest.raises(ExportIntegrityError, match="unresolved execution"):
        build_export_bundle(
            snapshot.model_copy(
                update={
                    "execution_segments": (unresolved_segment,),
                    "runtime_resolutions": (resolution,),
                }
            ),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    mismatched_segment = unresolved_segment.model_copy(
        update={"runtime_resolution_sha256": "a" * 64}
    )
    wrong_resolution = resolution.model_copy(update={"resolution_sha256": "b" * 64})
    with pytest.raises(ExportIntegrityError, match="runtime resolution"):
        build_export_bundle(
            snapshot.model_copy(
                update={
                    "execution_segments": (mismatched_segment,),
                    "runtime_resolutions": (wrong_resolution,),
                }
            ),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ExportIntegrityError, match="unknown execution"):
        build_export_bundle(
            snapshot.model_copy(update={"runtime_resolutions": (resolution,)}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )


def test_export_report_and_observation_integrity_edges(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "state")
    observation = WebSearchObservation(
        id="observation-1",
        run_id="run-1",
        backend="sdk",
        local_thread_id="thread-literature",
        external_thread_id="codex-literature",
        turn_id="turn-1",
        item_id="item-1",
        action_type="open_page",
        uri="https://literature.example/paper",
        uri_sha256=sha256_text("https://literature.example/paper"),
        payload={},
        payload_sha256=canonical_sha256({}),
        captured_at=NOW,
    )
    observed = Evidence(
        schema_version=2,
        id="evidence-1",
        kind="theorem",
        title="Observed theorem",
        content="Observed content",
        content_sha256=sha256_text("Observed content"),
        provenance=_provenance("thread-literature", "literature-v2"),
        source_uri=observation.uri,
        source_uri_sha256=observation.uri_sha256,
        source_trust=EvidenceTrust.RUNTIME_OBSERVED,
        content_trust=EvidenceTrust.MODEL_REPORTED,
        observation_ids=(observation.id,),
    )
    observed_snapshot = snapshot.model_copy(
        update={"evidence": (observed,), "web_search_observations": (observation,)}
    )
    variants = (
        ("another run", observation.model_copy(update={"run_id": "other"})),
        ("URI hash", observation.model_copy(update={"uri_sha256": "0" * 64})),
        ("payload hash", observation.model_copy(update={"payload_sha256": "0" * 64})),
    )
    for message, changed in variants:
        with pytest.raises(ExportIntegrityError, match=message):
            build_export_bundle(
                observed_snapshot.model_copy(update={"web_search_observations": (changed,)}),
                candidate_id="candidate-1",
                generated_at=GENERATED_AT,
            )
    with pytest.raises(ExportIntegrityError, match="evidence hash"):
        build_export_bundle(
            observed_snapshot.model_copy(
                update={"evidence": (observed.model_copy(update={"content_sha256": "0" * 64}),)}
            ),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    with pytest.raises(ExportIntegrityError, match="observation binding"):
        build_export_bundle(
            observed_snapshot.model_copy(
                update={
                    "evidence": (
                        observed.model_copy(update={"source_uri": "https://other.example"}),
                    )
                }
            ),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )

    record = snapshot.verifications[0]
    finding_report = record.report.model_copy(
        update={
            "findings": (
                Finding(
                    id="finding-render",
                    check_id=record.report.checks[0].id,
                    severity="major",
                    summary="A rendered finding.",
                    detail="The rendered finding has evidence.",
                    proof_span="prime divisor",
                    evidence_ids=("evidence-1",),
                ),
            )
        }
    )
    rendered = _render_report(
        snapshot,
        snapshot.candidates[0],
        (record.model_copy(update={"report": finding_report}),),
        snapshot.decisions[0],
    )
    assert "### Findings" in rendered
    assert "Evidence: evidence-1" in rendered


def test_export_intent_and_build_input_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path / "state")
    intent_content = {"generated_at": GENERATED_AT.isoformat()}
    intent = StageOutputRecord(
        id="intent-1",
        run_id="run-1",
        stage=RunStage.EXPORT,
        kind="export_intent",
        schema_version=1,
        content=intent_content,
        content_sha256=canonical_sha256(intent_content),
        provenance=_provenance("candidate-1", "export-v1"),
        provenance_sha256=canonical_sha256(_provenance("candidate-1", "export-v1")),
        created_at=NOW,
    )
    with pytest.raises(ExportIntegrityError, match="event marker"):
        _manifest_window(snapshot.model_copy(update={"stage_outputs": (intent,)}), GENERATED_AT)
    with pytest.raises(ExportIntegrityError, match="timezone-aware"):
        build_export_bundle(
            snapshot,
            candidate_id="candidate-1",
            generated_at=datetime(2026, 7, 16, 13, 0),
        )

    original_verify = export_module._verify_snapshot
    monkeypatch.setattr(export_module, "_verify_snapshot", lambda *_args: None)
    no_external = tuple(
        thread.model_copy(update={"external_thread_id": None})
        if thread.id == "thread-prover"
        else thread
        for thread in snapshot.threads
    )
    with pytest.raises(ExportIntegrityError, match="writer external identity"):
        build_export_bundle(
            snapshot.model_copy(update={"threads": no_external}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )
    monkeypatch.setattr(export_module, "_verify_snapshot", original_verify)
    monkeypatch.setattr(
        export_module,
        "_verify_snapshot",
        lambda *_args: None,
    )
    with pytest.raises(ExportIntegrityError, match="typed run input"):
        build_export_bundle(
            snapshot.model_copy(update={"run_input": None}),
            candidate_id="candidate-1",
            generated_at=GENERATED_AT,
        )


def test_export_helpers_cover_intent_usage_turn_and_duration_failures(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "state")
    usage_event = Event(
        run_id="run-1",
        seq=1,
        event_type="runtime.token_usage",
        stage=RunStage.PROVING,
        payload={
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cached_input_tokens": 3,
                "reasoning_output_tokens": 4,
            },
        },
        payload_sha256=canonical_sha256(
            {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cached_input_tokens": 3,
                    "reasoning_output_tokens": 4,
                },
            }
        ),
        created_at=NOW,
    )
    search_event = usage_event.model_copy(
        update={
            "seq": 2,
            "event_type": "runtime.item_completed",
            "payload": {"counts_as_search_query": True},
            "payload_sha256": canonical_sha256({"counts_as_search_query": True}),
        }
    )
    usage = _manifest_usage((usage_event, search_event), (), GENERATED_AT)
    assert usage.input_tokens == 1
    assert usage.search_queries == 1

    malformed = usage_event.model_copy(
        update={
            "seq": 3,
            "payload": {"thread_id": "thread-1", "turn_id": "turn-1", "usage": {}},
            "payload_sha256": canonical_sha256(
                {
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "usage": {},
                }
            ),
        }
    )
    with pytest.raises(ExportIntegrityError, match="token usage"):
        _manifest_usage((malformed,), (), GENERATED_AT)

    bad_segment = ExecutionLease(
        id="segment-bad",
        run_id="run-1",
        worker_id="worker",
        version=1,
        runtime_version="runtime",
        runtime_resolution_sha256=None,
        lease_expires_at=NOW - timedelta(seconds=1),
        released_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ExportIntegrityError, match="invalid duration"):
        _manifest_execution_segments((bad_segment,), GENERATED_AT)
    with pytest.raises(ExportIntegrityError, match="invalid duration"):
        _manifest_usage((), (bad_segment,), GENERATED_AT)

    incomplete_turn = usage_event.model_copy(
        update={
            "event_type": "runtime.turn_started",
            "payload": {"thread_id": "thread-1"},
            "payload_sha256": canonical_sha256({"thread_id": "thread-1"}),
        }
    )
    with pytest.raises(ExportIntegrityError, match="incomplete provenance"):
        _manifest_turns((incomplete_turn,))

    assert _candidate_record(snapshot, "candidate-1").id == "candidate-1"
    with pytest.raises(ExportNotReadyError, match="selected candidate"):
        _candidate_record(snapshot, "missing")


def test_migration_preflight_reports_duplicate_and_immutable_records(tmp_path: Path) -> None:
    database = tmp_path / "invalid.sqlite3"
    import sqlite3

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_metadata VALUES ('schema_version', '1')")
        connection.execute(
            "CREATE TABLE events(run_id TEXT, seq INTEGER, schema_version INTEGER, "
            "event_type TEXT, stage TEXT, payload_json TEXT, payload_sha256 TEXT, "
            "created_at TEXT)"
        )
        connection.execute("CREATE TABLE candidates(candidate_sha256 TEXT, sealed_at TEXT)")
        connection.execute(
            "INSERT INTO events(run_id, seq, schema_version, event_type, stage, "
            "payload_json, payload_sha256, created_at) "
            "VALUES ('run-duplicate', 1, 1, 'event', 'intake', '{}', ?, "
            "'2026-01-01T00:00:00+00:00')",
            (canonical_sha256({}),),
        )
        connection.execute(
            "INSERT INTO events(run_id, seq, schema_version, event_type, stage, "
            "payload_json, payload_sha256, created_at) "
            "VALUES ('run-duplicate', 1, 1, 'event', 'intake', '{}', ?, "
            "'2026-01-01T00:00:00+00:00')",
            (canonical_sha256({}),),
        )
        connection.execute(
            "INSERT INTO candidates(candidate_sha256, sealed_at) "
            "VALUES ('bad', '2026-01-01T00:00:00+00:00')",
        )
        connection.commit()

    result = preflight_database(database)
    assert result.valid is False
    assert "duplicate event sequence exists" in result.errors
    assert "sealed candidate hash is invalid" in result.errors


def test_migration_preflight_warns_about_stale_leases(tmp_path: Path) -> None:
    source = Path("tests/fixtures/migrations/v2.sqlite3")
    database = tmp_path / "stale.sqlite3"
    database.write_bytes(source.read_bytes())
    import sqlite3

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO execution_segments("
            "id, run_id, worker_id, version, lease_token_sha256, runtime_version, "
            "lease_expires_at, created_at, updated_at) "
            "VALUES ('segment-stale', 'run-1', 'worker', 1, ?, 'runtime', "
            "'2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00', "
            "'2000-01-01T00:00:00+00:00')",
            ("0" * 64,),
        )
        connection.commit()
    result = preflight_database(database)
    assert result.valid is True
    assert any("expired" in warning for warning in result.warnings)


def test_schema_preflight_rejects_duplicate_external_threads(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "threads.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE schema_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_metadata VALUES ('schema_version', '1')")
        connection.execute("CREATE TABLE threads(run_id TEXT, external_thread_id TEXT, id TEXT)")
        connection.executemany(
            "INSERT INTO threads VALUES ('run-1', 'external-1', ?)",
            (("thread-a",), ("thread-b",)),
        )
        connection.commit()
        with pytest.raises(DuplicateExternalThreadIdentityError, match="duplicate external"):
            prepare_schema_migration(connection)
