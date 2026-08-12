"""Offline verification of a QED export directory.

This module deliberately imports no runtime, network, workflow, or service code.
It treats the bundle as untrusted bytes and recomputes the application decision
from the structured audit record.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from qed.decision import (
    CandidateDecision,
    candidate_decision_sha256,
    decide_stable_candidate,
)
from qed.schemas import (
    Event,
    Evidence,
    Manifest,
    ProofCandidate,
    VerificationReport,
    canonical_json,
    canonical_sha256,
    event_chain_sha256,
    evidence_sha256,
    sha256_text,
    verification_report_sha256,
)
from qed.stable_contracts import (
    BundleVerificationResult,
    ProofObligationGraph,
    VerifierRole,
)


class BundleVerificationError(ValueError):
    """Raised only for invalid verifier usage, not for an invalid bundle."""


_REQUIRED_FILES = frozenset(
    {"proof.md", "report.md", "event-chain.json", "audit.json", "manifest.json"}
)
_ROLE_BY_REPORT_KIND = {
    "structural": VerifierRole.STRUCTURAL,
    "detailed": VerifierRole.DETAILED_STEP,
    "assumptions_quantifiers": VerifierRole.ASSUMPTIONS_QUANTIFIERS,
    "counterexample_edge_case": VerifierRole.COUNTEREXAMPLE_EDGE_CASE,
    "reconstruction": VerifierRole.RECONSTRUCTION,
    "citation": VerifierRole.CITATION,
}
_REQUIRED_ROLES = frozenset(
    {
        VerifierRole.STRUCTURAL,
        VerifierRole.DETAILED_STEP,
        VerifierRole.ASSUMPTIONS_QUANTIFIERS,
        VerifierRole.COUNTEREXAMPLE_EDGE_CASE,
        VerifierRole.RECONSTRUCTION,
    }
)


def _duplicate_key_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise BundleVerificationError(f"bundle path contains a symlink: {current}")


def _read_regular(root: Path, name: str) -> bytes:
    path = root / name
    if path.parent != root or path.name != name:
        raise BundleVerificationError(f"bundle path escapes root: {name!r}")
    _reject_symlink_components(path)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise BundleVerificationError(f"cannot safely open bundle artifact: {name}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BundleVerificationError(f"bundle artifact is not a private regular file: {name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_json(data: bytes, *, name: str) -> Any:
    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_duplicate_key_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BundleVerificationError(f"{name} is not valid UTF-8 JSON") from error
    if text != f"{canonical_json(value)}\n":
        raise BundleVerificationError(f"{name} is not canonical JSON")
    return value


def _record_error(errors: list[str], message: str) -> None:
    if message not in errors:
        errors.append(message)


def verify_bundle(bundle: str | Path) -> BundleVerificationResult:
    """Verify one export directory without starting Codex or touching the network."""

    errors: list[str] = []
    warnings: list[str] = []
    checked: list[str] = []
    signature_status = "unsigned"
    decision = "UNKNOWN"
    try:
        root = Path(bundle)
        _reject_symlink_components(root)
        if root.is_symlink() or not root.is_dir():
            raise BundleVerificationError("bundle path is not a regular directory")
        entries = tuple(root.iterdir())
        names = {entry.name for entry in entries}
        for entry in entries:
            if entry.name not in _REQUIRED_FILES:
                _record_error(errors, f"unexpected bundle file: {entry.name}")
            elif entry.is_symlink() or not entry.is_file():
                _record_error(errors, f"bundle artifact is not a regular file: {entry.name}")
        for name in sorted(_REQUIRED_FILES - names):
            _record_error(errors, f"missing bundle artifact: {name}")
        if errors:
            return BundleVerificationResult(
                valid=False,
                errors=tuple(errors),
                warnings=tuple(warnings),
                decision=decision,  # type: ignore[arg-type]
                signature_status=signature_status,  # type: ignore[arg-type]
                checked_artifacts=tuple(sorted(names & _REQUIRED_FILES)),
            )

        raw = {name: _read_regular(root, name) for name in sorted(_REQUIRED_FILES)}
        checked.extend(sorted(_REQUIRED_FILES))
        _load_json(raw["manifest.json"], name="manifest.json")
        event_value = _load_json(raw["event-chain.json"], name="event-chain.json")
        audit_value = _load_json(raw["audit.json"], name="audit.json")
        manifest = Manifest.model_validate_json(raw["manifest.json"])
        if raw["manifest.json"].decode("utf-8") != f"{canonical_json(manifest)}\n":
            _record_error(errors, "manifest does not round-trip through the strict schema")
        artifact_hashes = {
            artifact.relative_path: artifact.sha256
            for artifact in manifest.artifacts
            if artifact.relative_path is not None
        }
        if set(artifact_hashes) != _REQUIRED_FILES - {"manifest.json"}:
            _record_error(errors, "manifest artifact set does not match bundle files")
        for name, expected in artifact_hashes.items():
            if name in raw and hashlib.sha256(raw[name]).hexdigest() != expected:
                _record_error(errors, f"artifact hash does not match manifest: {name}")
        if not isinstance(event_value, list) or not event_value:
            _record_error(errors, "event-chain.json must contain a non-empty event list")
            events: tuple[Event, ...] = ()
        else:
            try:
                events = tuple(
                    Event.model_validate_json(canonical_json(item)) for item in event_value
                )
            except (TypeError, ValueError) as error:
                _record_error(errors, f"event chain schema failure: {error}")
                events = ()
        if events:
            sequences = [event.seq for event in events]
            if sequences != list(range(1, len(events) + 1)):
                _record_error(errors, "event sequence is not strictly contiguous from one")
            if any(event.run_id != manifest.run_id for event in events):
                _record_error(errors, "event chain contains another run identity")
            if events[-1].stage != manifest.run_stage:
                _record_error(errors, "event-chain terminal stage disagrees with manifest")
            if manifest.event_chain_sha256 != event_chain_sha256(events):
                _record_error(errors, "event-chain root hash does not match manifest")
            if (
                manifest.first_event_seq != events[0].seq
                or manifest.last_event_seq != events[-1].seq
            ):
                _record_error(errors, "manifest event range does not match event chain")

        if not isinstance(audit_value, dict):
            _record_error(errors, "audit.json must contain an object")
            audit_value = {}
        try:
            candidate = ProofCandidate.model_validate_json(canonical_json(audit_value["candidate"]))
            reports = tuple(
                VerificationReport.model_validate_json(canonical_json(item))
                for item in audit_value["reports"]
            )
            evidence = tuple(
                Evidence.model_validate_json(canonical_json(item))
                for item in audit_value["evidence"]
            )
            declared_decision = audit_value["decision"]
            prover_external_id = audit_value["prover_external_thread_id"]
            graph = ProofObligationGraph.model_validate_json(
                canonical_json(audit_value["claim_graph"])
            )
            if not isinstance(declared_decision, dict) or not isinstance(prover_external_id, str):
                raise ValueError("audit decision or prover identity is malformed")
        except (KeyError, TypeError, ValueError) as error:
            _record_error(errors, f"audit schema failure: {error}")
            candidate = None
            reports = ()
            evidence = ()
            declared_decision = None
            prover_external_id = ""
            graph = None

        if candidate is not None and graph is not None and isinstance(declared_decision, dict):
            if candidate.run_id != manifest.run_id:
                _record_error(errors, "candidate run identity disagrees with manifest")
            if candidate.proof_sha256 != sha256_text(candidate.proof):
                _record_error(errors, "candidate proof hash does not match candidate bytes")
            if graph.proof_sha256 != candidate.proof_sha256:
                _record_error(errors, "claim graph proof hash does not match candidate")
            if graph.candidate_sha256 != canonical_sha256(candidate):
                _record_error(errors, "claim graph candidate hash does not match candidate")
            if raw["proof.md"].decode("utf-8").endswith(candidate.proof.rstrip() + "\n") is False:
                _record_error(errors, "proof.md does not contain the frozen candidate proof")
            try:
                graph.validate_against_proof(
                    candidate.proof,
                    evidence_ids=frozenset(item.id for item in evidence),
                    rule_ids=frozenset(rule.id for rule in manifest.verification_rules),
                )
            except ValueError as error:
                _record_error(errors, f"claim graph validation failed: {error}")
            declared = CandidateDecision.model_validate_json(canonical_json(declared_decision))
            # The strict decision is recomputed from the frozen structured records.
            try:
                recomputed = decide_stable_candidate(
                    candidate,
                    reports,
                    prover_external_thread_id=prover_external_id,
                    required_evidence=evidence,
                    required_rule_ids=declared.required_rule_ids,
                )
            except (TypeError, ValueError) as error:
                _record_error(errors, f"decision recomputation failed: {error}")
                recomputed = None
            if recomputed is not None:
                if canonical_sha256(recomputed) != candidate_decision_sha256(declared):
                    _record_error(errors, "declared decision differs from code-derived decision")
                decision = "PASS" if recomputed.passed else "NON_PASS"
            else:
                decision = "NON_PASS"

            report_ids = {report.id for report in reports}
            if len(report_ids) != len(reports):
                _record_error(errors, "audit reports contain duplicate IDs")
            external_ids = [report.verifier_external_thread_id for report in reports]
            if any(not item for item in external_ids) or len(set(external_ids)) != len(
                external_ids
            ):
                _record_error(errors, "verifier external thread identities are missing or reused")
            if prover_external_id in external_ids:
                _record_error(errors, "verifier reuses prover external thread identity")
            manifest_threads = {
                thread.external_thread_id: thread
                for thread in manifest.threads
                if thread.external_thread_id is not None
            }
            prover_manifest_thread = manifest_threads.get(prover_external_id)
            if prover_manifest_thread is None or prover_manifest_thread.role != "prover":
                _record_error(errors, "manifest does not contain the prover external identity")
            for report in reports:
                external_id = report.verifier_external_thread_id
                if not isinstance(external_id, str):
                    continue
                thread = manifest_threads.get(external_id)
                if thread is None or thread.role != "verifier" or thread.status != "completed":
                    _record_error(errors, f"manifest verifier lineage is missing: {report.id}")
                if report.verifier_thread_id != report.provenance.source_id:
                    _record_error(errors, f"report local thread provenance mismatch: {report.id}")
            covered_roles = {
                coverage.role
                for node in graph.nodes
                for coverage in node.coverage
                if coverage.status == "pass"
            }
            for role in sorted(_REQUIRED_ROLES, key=lambda item: item.value):
                if role not in covered_roles:
                    _record_error(errors, f"missing required claim coverage: {role.value}")
            if evidence and VerifierRole.CITATION not in covered_roles:
                _record_error(errors, "missing required claim coverage: citation")
            if any(node.byte_end > len(candidate.proof.encode("utf-8")) for node in graph.nodes):
                _record_error(errors, "claim graph byte span exceeds candidate UTF-8 bytes")

            manifest_candidate = {item.id: item.sha256 for item in manifest.candidate_records}
            if manifest_candidate.get(candidate.id) != canonical_sha256(candidate):
                _record_error(errors, "manifest candidate record does not match audit candidate")
            manifest_reports = {item.id: item.sha256 for item in manifest.verification_records}
            for report in reports:
                if manifest_reports.get(report.id) != verification_report_sha256(report):
                    _record_error(errors, f"manifest verification record mismatch: {report.id}")
            manifest_evidence = {item.id: item.sha256 for item in manifest.evidence_records}
            for item in evidence:
                if manifest_evidence.get(item.id) != evidence_sha256(item):
                    _record_error(errors, f"manifest evidence record mismatch: {item.id}")
            if manifest.decision_records:
                decision_record = next(
                    (item for item in manifest.decision_records if item.id == candidate.id),
                    None,
                )
                if decision_record is None or decision_record.sha256 != candidate_decision_sha256(
                    declared
                ):
                    _record_error(errors, "manifest decision record does not match audit decision")

            for rule in manifest.verification_rules:
                if not rule.coverage or any(
                    item.status.value != "pass" for item in rule.coverage
                ):
                    _record_error(
                        errors,
                        f"frozen verification rule is not fully covered: {rule.id}",
                    )
                if any(item.report_id not in report_ids for item in rule.coverage):
                    _record_error(
                        errors,
                        f"verification rule references an unknown report: {rule.id}",
                    )
        else:
            decision = "NON_PASS"

        if manifest.publication_phase == "export_intent":
            if not (manifest.run_status == "running" and manifest.run_stage == "export"):
                _record_error(errors, "export_intent must describe running/export state")
        elif (
            manifest.publication_phase == "snapshot"
            and (manifest.run_status, manifest.run_stage)
            not in {
                ("running", "export"),
                ("completed", "complete"),
            }
        ):
            _record_error(errors, "snapshot publication phase has invalid run state")
        if manifest.code_verdict == "PASS" and decision != "PASS":
            _record_error(errors, "manifest claims PASS while offline recomputation is NON_PASS")
    except (BundleVerificationError, OSError, ValueError, TypeError) as error:
        _record_error(errors, str(error))
        decision = "NON_PASS"
    if not errors:
        warnings.append("unsigned bundle: SHA-256 integrity is not authenticity or trusted time")
    else:
        decision = "NON_PASS"
    return BundleVerificationResult(
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        decision=decision,  # type: ignore[arg-type]
        signature_status=signature_status,  # type: ignore[arg-type]
        checked_artifacts=tuple(checked),
    )
