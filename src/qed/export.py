"""Deterministic exports from immutable run snapshots."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from qed.decision import CandidateDecision, CandidateIntegrityError, decide_candidate
from qed.schemas import (
    Event,
    Manifest,
    ManifestArtifact,
    ManifestExecutionSegment,
    ManifestFinding,
    ManifestRecord,
    ManifestRuntimeResolution,
    ManifestThread,
    ManifestTurn,
    ManifestTurnInput,
    ManifestUsage,
    canonical_json,
    canonical_sha256,
    sha256_text,
)
from qed.store import (
    CandidateRecord,
    ExecutionLease,
    RunSnapshot,
    RunStage,
    RunStatus,
    RuntimeResolutionRecord,
    ThreadRole,
    ThreadStatus,
    VerificationRecord,
)


class ExportError(RuntimeError):
    """Base class for deterministic export failures."""


class ExportIntegrityError(ExportError):
    """Raised when a snapshot no longer matches its persisted hashes."""


class ExportNotReadyError(ExportError):
    """Raised when a selected candidate is not sealed and verified."""


class ExportCollisionError(ExportError):
    """Raised when an export address already contains different content."""


@dataclass(frozen=True, slots=True)
class ExportBundle:
    """Three fixed export artifacts and their content address."""

    run_id: str
    proof_md: str
    report_md: str
    manifest: Manifest
    manifest_json: str
    bundle_sha256: str

    @property
    def files(self) -> Mapping[str, bytes]:
        return MappingProxyType(
            {
                "proof.md": self.proof_md.encode(),
                "report.md": self.report_md.encode(),
                "manifest.json": self.manifest_json.encode(),
            }
        )


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXPORTED_PATHS = frozenset({"proof.md", "report.md", "manifest.json"})


def _validated_files(bundle: ExportBundle) -> Mapping[str, bytes]:
    expected_manifest = f"{canonical_json(bundle.manifest)}\n"
    if bundle.manifest_json != expected_manifest:
        raise ExportIntegrityError("bundle manifest is not canonical")
    if bundle.bundle_sha256 != sha256_text(bundle.manifest_json):
        raise ExportIntegrityError("bundle content address does not match manifest")
    if bundle.manifest.run_id != bundle.run_id:
        raise ExportIntegrityError("bundle run identity does not match manifest")

    artifacts = {
        artifact.relative_path: artifact.sha256
        for artifact in bundle.manifest.artifacts
        if artifact.relative_path is not None
    }
    if set(artifacts) != {"proof.md", "report.md"}:
        raise ExportIntegrityError("manifest must contain proof and report artifacts")
    if artifacts["proof.md"] != sha256_text(bundle.proof_md):
        raise ExportIntegrityError("proof artifact hash does not match bundle")
    if artifacts["report.md"] != sha256_text(bundle.report_md):
        raise ExportIntegrityError("report artifact hash does not match bundle")
    return bundle.files


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ExportCollisionError(f"export path contains a symlink: {current}")


def _validate_existing(directory: Path, files: Mapping[str, bytes]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ExportCollisionError(f"export address is not a regular directory: {directory}")
    if {path.name for path in directory.iterdir()} != _EXPORTED_PATHS:
        raise ExportCollisionError(f"export address contains unexpected files: {directory}")
    for name, expected in files.items():
        path = directory / name
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise ExportCollisionError(f"export artifact is missing: {path}") from error
        if not stat.S_ISREG(metadata.st_mode) or path.read_bytes() != expected:
            raise ExportCollisionError(f"export artifact does not match bundle: {path}")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard_staging(directory: Path) -> None:
    if not directory.exists():
        return
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()


def write_export_bundle(bundle: ExportBundle, managed_root: str | Path) -> Path:
    """Write a bundle once below a managed root, or validate its exact prior write."""

    if _SAFE_RUN_ID.fullmatch(bundle.run_id) is None or bundle.run_id in {".", ".."}:
        raise ExportCollisionError(f"unsafe run id for export: {bundle.run_id}")
    files = _validated_files(bundle)
    root = Path(managed_root).absolute()
    _reject_symlink_components(root)
    if root.exists() and not root.is_dir():
        raise ExportCollisionError(f"managed export root is not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(root)
    if not root.is_dir():
        raise ExportCollisionError(f"managed export root is not a directory: {root}")

    run_directory = root / bundle.run_id
    if run_directory.is_symlink():
        raise ExportCollisionError(f"export run directory is a symlink: {run_directory}")
    if run_directory.exists() and not run_directory.is_dir():
        raise ExportCollisionError(f"export run address is not a directory: {run_directory}")
    try:
        run_directory.mkdir(exist_ok=True)
    except FileExistsError as error:
        raise ExportCollisionError(
            f"export run address is not a directory: {run_directory}"
        ) from error
    if not run_directory.is_dir():
        raise ExportCollisionError(f"export run address is not a directory: {run_directory}")

    destination = run_directory / bundle.bundle_sha256
    if destination.exists() or destination.is_symlink():
        _validate_existing(destination, files)
        return destination

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{bundle.bundle_sha256}.",
            dir=run_directory,
        )
    )
    published = False
    try:
        for name, content in files.items():
            path = staging / name
            with path.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        _fsync_directory(staging)
        try:
            staging.rename(destination)
            published = True
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise ExportError(f"could not publish export bundle: {error}") from error
            _validate_existing(destination, files)
        _fsync_directory(run_directory)
        return destination
    finally:
        if not published:
            _discard_staging(staging)


def _candidate_record(snapshot: RunSnapshot, candidate_id: str) -> CandidateRecord:
    matches = tuple(record for record in snapshot.candidates if record.id == candidate_id)
    if len(matches) != 1:
        raise ExportNotReadyError(f"selected candidate not found: {candidate_id}")
    record = matches[0]
    if record.sealed_at is None:
        raise ExportNotReadyError(f"selected candidate is not sealed: {candidate_id}")
    return record


def _verify_snapshot(
    snapshot: RunSnapshot,
    candidate: CandidateRecord,
    reports: tuple[VerificationRecord, ...],
) -> None:
    run = snapshot.run
    if not (
        (run.stage is RunStage.EXPORT and run.status is RunStatus.RUNNING)
        or (run.stage is RunStage.COMPLETE and run.status is RunStatus.COMPLETED)
    ):
        raise ExportIntegrityError("run does not satisfy the export or completed stage contract")
    if run.config_sha256 != run.config.sha256:
        raise ExportIntegrityError("run config hash does not match frozen config")
    if run.provenance_sha256 != canonical_sha256(run.provenance):
        raise ExportIntegrityError("run provenance hash does not match frozen provenance")
    if run.runtime_version != run.provenance.runtime_version:
        raise ExportIntegrityError("run runtime version does not match provenance")

    run_input = snapshot.run_input
    if run_input is None:
        raise ExportIntegrityError("snapshot is missing the typed run input")
    if run_input.sha256 != run.input_sha256:
        raise ExportIntegrityError("run input hash does not match frozen input")

    evidence_by_id = {item.id: item for item in snapshot.evidence}
    if len(evidence_by_id) != len(snapshot.evidence):
        raise ExportIntegrityError("snapshot contains duplicate evidence identities")
    for item in snapshot.evidence:
        if item.content_sha256 != sha256_text(item.content):
            raise ExportIntegrityError(f"referenced evidence hash does not match: {item.id}")

    plans_by_id = {plan.id: plan for plan in snapshot.plans}
    if len(plans_by_id) != len(snapshot.plans):
        raise ExportIntegrityError("snapshot contains duplicate plan identities")

    frozen = candidate.candidate
    if candidate.run_id != run.id or frozen.run_id != run.id:
        raise ExportIntegrityError("candidate belongs to another run")
    if candidate.id != frozen.id:
        raise ExportIntegrityError("candidate record identity does not match frozen candidate")
    if candidate.plan_id != frozen.plan_id or candidate.attempt != frozen.attempt:
        raise ExportIntegrityError("candidate metadata does not match frozen candidate")
    if candidate.candidate_sha256 != canonical_sha256(frozen):
        raise ExportIntegrityError("candidate hash does not match frozen candidate")
    if candidate.proof_sha256 != frozen.proof_sha256:
        raise ExportIntegrityError("candidate proof hash does not match record")
    if candidate.provenance != frozen.provenance:
        raise ExportIntegrityError("candidate provenance does not match frozen candidate")
    if candidate.provenance_sha256 != canonical_sha256(frozen.provenance):
        raise ExportIntegrityError("candidate provenance hash does not match")

    plan = plans_by_id.get(frozen.plan_id)
    if plan is None:
        raise ExportIntegrityError(f"referenced plan is missing: {frozen.plan_id}")
    if plan.problem_sha256 != run_input.sha256:
        raise ExportIntegrityError("referenced plan input hash does not match run input")
    referenced_evidence = {
        evidence_id for step in plan.steps for evidence_id in step.evidence_ids
    } | set(frozen.evidence_ids)
    missing_evidence = referenced_evidence - set(evidence_by_id)
    if missing_evidence:
        names = ", ".join(sorted(missing_evidence))
        raise ExportIntegrityError(f"referenced evidence is missing: {names}")

    threads_by_id = {thread.id: thread for thread in snapshot.threads}
    if len(threads_by_id) != len(snapshot.threads):
        raise ExportIntegrityError("snapshot contains duplicate thread identities")
    writer = threads_by_id.get(candidate.thread_id or "")
    if (
        writer is None
        or writer.run_id != run.id
        or writer.role is not ThreadRole.PROVER
        or writer.status is not ThreadStatus.COMPLETED
        or writer.external_thread_id is None
        or frozen.provenance.source_id != writer.id
    ):
        raise ExportIntegrityError("candidate writer thread lineage does not match")

    verifier_external_ids: list[str] = []

    for record in reports:
        report = record.report
        if record.run_id != run.id:
            raise ExportIntegrityError(f"report belongs to another run: {record.id}")
        if record.id != report.id or record.candidate_id != frozen.id:
            raise ExportIntegrityError(f"report identity does not match record: {record.id}")
        if record.thread_id != report.verifier_thread_id or record.kind != report.kind:
            raise ExportIntegrityError(f"report metadata does not match record: {record.id}")
        if record.report_sha256 != canonical_sha256(report):
            raise ExportIntegrityError(f"report hash does not match frozen report: {record.id}")
        if record.candidate_sha256 != frozen.proof_sha256:
            raise ExportIntegrityError(f"report candidate hash does not match: {record.id}")
        if record.provenance != report.provenance:
            raise ExportIntegrityError(f"report provenance does not match: {record.id}")
        if record.provenance_sha256 != canonical_sha256(report.provenance):
            raise ExportIntegrityError(f"report provenance hash does not match: {record.id}")
        thread = threads_by_id.get(record.thread_id)
        if (
            thread is None
            or thread.run_id != run.id
            or thread.role is not ThreadRole.VERIFIER
            or thread.parent_thread_id is not None
            or thread.status is not ThreadStatus.COMPLETED
            or report.provenance.source_id != thread.id
        ):
            raise ExportIntegrityError(
                f"external verifier thread lineage does not match: {record.id}"
            )
        external_id = report.verifier_external_thread_id
        if external_id is None or thread.external_thread_id != external_id:
            raise ExportIntegrityError(
                f"external verifier identity is missing or mismatched: {record.id}"
            )
        if writer.external_thread_id == external_id:
            raise ExportIntegrityError(f"external verifier identity reuses the writer: {record.id}")
        verifier_external_ids.append(external_id)

    if len(set(verifier_external_ids)) != len(verifier_external_ids):
        raise ExportIntegrityError("external verifier identities must be fresh and unique")

    sequences = [event.seq for event in snapshot.events]
    if any(event.run_id != run.id for event in snapshot.events):
        raise ExportIntegrityError("event belongs to another run")
    if not sequences or sequences != list(range(1, sequences[-1] + 1)):
        raise ExportIntegrityError("snapshot event sequence is not complete and monotonic")
    if any(event.payload_sha256 != canonical_sha256(event.payload) for event in snapshot.events):
        raise ExportIntegrityError("event payload hash does not match frozen payload")
    if snapshot.events[-1].stage != run.stage.value:
        raise ExportIntegrityError("terminal event stage does not match run stage")

    resolutions = {item.segment_id: item for item in snapshot.runtime_resolutions}
    if len(resolutions) != len(snapshot.runtime_resolutions):
        raise ExportIntegrityError("snapshot contains duplicate runtime resolutions")
    for segment in snapshot.execution_segments:
        if segment.runtime_version is None:
            raise ExportIntegrityError(
                f"execution segment has no runtime version: {segment.id}"
            )
        resolution = resolutions.get(segment.id)
        if segment.runtime_resolution_sha256 is None:
            if resolution is not None:
                raise ExportIntegrityError(
                    f"unresolved execution has a runtime resolution: {segment.id}"
                )
            continue
        if resolution is None or resolution.run_id != run.id or (
            resolution.resolution_sha256 != segment.runtime_resolution_sha256
        ):
            raise ExportIntegrityError(
                f"execution runtime resolution is missing or mismatched: {segment.id}"
            )
    resolved_segment_ids = {
        segment.id
        for segment in snapshot.execution_segments
        if segment.runtime_resolution_sha256 is not None
    }
    if set(resolutions) != resolved_segment_ids:
        raise ExportIntegrityError("runtime resolution references an unknown execution")


def _render_proof(candidate: CandidateRecord) -> str:
    frozen = candidate.candidate
    return (
        "# Proof\n\n"
        f"Run: `{frozen.run_id}`  \n"
        f"Candidate: `{frozen.id}`  \n"
        f"Proof SHA-256: `{frozen.proof_sha256}`\n\n"
        f"{frozen.proof.rstrip()}\n"
    )


def _render_report(
    snapshot: RunSnapshot,
    candidate: CandidateRecord,
    reports: tuple[VerificationRecord, ...],
) -> str:
    frozen = candidate.candidate
    required = (
        ("structural", "detailed", "citation")
        if snapshot.evidence
        else (
            "structural",
            "detailed",
        )
    )
    lines = [
        "# Verification report",
        "",
        f"Run: `{snapshot.run.id}`  ",
        f"Candidate: `{frozen.id}`  ",
        f"Candidate SHA-256: `{candidate.candidate_sha256}`  ",
        "Code-computed verdict: **PASS**  ",
        "Required reports: " + ", ".join(f"`{kind}`" for kind in required),
    ]
    for record in reports:
        report = record.report
        lines.extend(
            [
                "",
                f"## {report.kind.title()}",
                "",
                f"Report: `{report.id}`  ",
                f"Report SHA-256: `{record.report_sha256}`  ",
                f"Verifier thread: `{report.verifier_thread_id}`  ",
                f"Verdict: **{report.verdict.value.upper()}**",
                "",
                "### Checks",
                "",
            ]
        )
        for check in report.checks:
            lines.append(
                f"- **{check.status.value.upper()}** `{check.id}` "
                f"({check.category}): {check.summary}"
            )
            if check.proof_spans:
                lines.append("  - Proof spans: " + "; ".join(check.proof_spans))
            if check.evidence_ids:
                lines.append("  - Evidence: " + ", ".join(check.evidence_ids))
        if report.findings:
            lines.extend(["", "### Findings", ""])
            for finding in report.findings:
                lines.append(
                    f"- **{finding.severity.upper()}** `{finding.id}`: "
                    f"{finding.summary} — {finding.detail}"
                )
                if finding.proof_span is not None:
                    lines.append(f"  - Proof span: {finding.proof_span}")
                if finding.evidence_ids:
                    lines.append("  - Evidence: " + ", ".join(finding.evidence_ids))
    return "\n".join(lines) + "\n"


def _prompt_versions(
    snapshot: RunSnapshot,
    candidate: CandidateRecord,
    reports: tuple[VerificationRecord, ...],
) -> dict[str, str]:
    versions: dict[str, str] = {}
    run_version = snapshot.run.provenance.prompt_version
    if run_version is not None:
        versions[f"run:{snapshot.run.id}"] = run_version

    for plan in snapshot.plans:
        version = plan.provenance.prompt_version
        if version is not None:
            versions[f"plan:{plan.id}"] = version
    for evidence in snapshot.evidence:
        version = evidence.provenance.prompt_version
        if version is not None:
            versions[f"evidence:{evidence.id}"] = version

    for candidate_record in snapshot.candidates:
        version = candidate_record.candidate.provenance.prompt_version
        if version is not None:
            versions[f"candidate:{candidate_record.id}"] = version
    for verification_record in snapshot.verifications:
        version = verification_record.report.provenance.prompt_version
        if version is not None:
            versions[f"verification:{verification_record.id}"] = version
    for adjudication in snapshot.adjudications:
        version = adjudication.provenance.prompt_version
        if version is not None:
            versions[f"adjudication:{adjudication.id}"] = version
    return versions


def _manifest_window(
    snapshot: RunSnapshot,
    generated_at: datetime,
) -> tuple[
    tuple[Event, ...],
    tuple[ExecutionLease, ...],
    tuple[RuntimeResolutionRecord, ...],
    datetime,
]:
    intents = tuple(
        output
        for output in snapshot.stage_outputs
        if output.kind == "export_intent"
        and isinstance(output.content, dict)
        and output.content.get("generated_at") == generated_at.isoformat()
    )
    if not intents:
        return (
            snapshot.events,
            snapshot.execution_segments,
            snapshot.runtime_resolutions,
            generated_at,
        )
    intent = intents[-1]
    marker = next(
        (
            event
            for event in snapshot.events
            if event.event_type == "stage.output_created"
            and event.payload.get("output_id") == intent.id
        ),
        None,
    )
    if marker is None:
        raise ExportIntegrityError("export intent has no durable event marker")
    events = tuple(event for event in snapshot.events if event.seq <= marker.seq)
    segments = tuple(
        segment
        for segment in snapshot.execution_segments
        if segment.created_at <= intent.created_at
    )
    segment_ids = {segment.id for segment in segments}
    resolutions = tuple(
        item
        for item in snapshot.runtime_resolutions
        if item.segment_id in segment_ids
    )
    return events, segments, resolutions, intent.created_at


def _event_chain_sha256(events: tuple[Event, ...]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _manifest_usage(
    events: tuple[Event, ...],
    segments: tuple[ExecutionLease, ...],
    observed_at: datetime,
) -> ManifestUsage:
    usage_by_turn: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    search_queries = 0
    for event in events:
        if event.event_type == "runtime.token_usage":
            thread_id = event.payload.get("thread_id")
            turn_id = event.payload.get("turn_id")
            usage = event.payload.get("usage")
            input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            output_tokens = (
                usage.get("output_tokens") if isinstance(usage, dict) else None
            )
            cached_input_tokens = (
                usage.get("cached_input_tokens") if isinstance(usage, dict) else None
            )
            reasoning_output_tokens = (
                usage.get("reasoning_output_tokens")
                if isinstance(usage, dict)
                else None
            )
            if (
                not isinstance(thread_id, str)
                or not isinstance(turn_id, str)
                or not isinstance(usage, dict)
                or type(input_tokens) is not int
                or type(output_tokens) is not int
                or type(cached_input_tokens) is not int
                or type(reasoning_output_tokens) is not int
                or input_tokens < 0
                or output_tokens < 0
                or cached_input_tokens < 0
                or reasoning_output_tokens < 0
            ):
                raise ExportIntegrityError("runtime token usage event is malformed")
            usage_by_turn[(thread_id, turn_id)] = (
                input_tokens,
                output_tokens,
                cached_input_tokens,
                reasoning_output_tokens,
            )
        if (
            event.event_type == "runtime.item_completed"
            and event.payload.get("counts_as_search_query") is True
        ):
            search_queries += 1

    execution_seconds = 0.0
    for segment in segments:
        observed_until = min(
            segment.released_at or observed_at,
            segment.lease_expires_at,
            observed_at,
        )
        duration = (observed_until - segment.created_at).total_seconds()
        if duration < 0:
            raise ExportIntegrityError(
                f"execution segment has an invalid duration: {segment.id}"
            )
        execution_seconds += duration
    return ManifestUsage(
        input_tokens=sum(item[0] for item in usage_by_turn.values()),
        output_tokens=sum(item[1] for item in usage_by_turn.values()),
        cached_input_tokens=sum(item[2] for item in usage_by_turn.values()),
        reasoning_output_tokens=sum(item[3] for item in usage_by_turn.values()),
        turns=len(usage_by_turn),
        search_queries=search_queries,
        execution_seconds=execution_seconds,
    )


def _manifest_execution_segments(
    segments: tuple[ExecutionLease, ...],
    observed_at: datetime,
) -> tuple[ManifestExecutionSegment, ...]:
    records: list[ManifestExecutionSegment] = []
    for segment in segments:
        if segment.runtime_version is None:
            raise ExportIntegrityError(
                f"execution segment has no runtime version: {segment.id}"
            )
        observed_until = min(
            segment.released_at or observed_at,
            segment.lease_expires_at,
            observed_at,
        )
        duration = (observed_until - segment.created_at).total_seconds()
        if duration < 0:
            raise ExportIntegrityError(
                f"execution segment has an invalid duration: {segment.id}"
            )
        records.append(
            ManifestExecutionSegment(
                id=segment.id,
                version=segment.version,
                runtime_version=segment.runtime_version,
                runtime_resolution_sha256=segment.runtime_resolution_sha256,
                started_at=segment.created_at,
                observed_until=observed_until,
                duration_seconds=duration,
            )
        )
    return tuple(records)


def _manifest_turns(events: tuple[Event, ...]) -> tuple[ManifestTurn, ...]:
    turns: list[ManifestTurn] = []
    for event in events:
        if event.event_type != "runtime.turn_started":
            continue
        thread_id = event.payload.get("thread_id")
        turn_id = event.payload.get("turn_id")
        backend = event.payload.get("backend")
        turn_input_id = event.payload.get("turn_input_id")
        if (
            not isinstance(thread_id, str)
            or not isinstance(turn_id, str)
            or not isinstance(backend, str)
            or not isinstance(turn_input_id, str)
        ):
            raise ExportIntegrityError("runtime turn event has incomplete provenance")
        turns.append(
            ManifestTurn(
                thread_id=thread_id,
                turn_id=turn_id,
                backend=backend,
                turn_input_id=turn_input_id,
            )
        )
    return tuple(turns)


def _verify_acceptance(
    snapshot: RunSnapshot,
    candidate: CandidateRecord,
    decision: CandidateDecision,
) -> None:
    persisted = tuple(item for item in snapshot.decisions if item.candidate_id == candidate.id)
    if len(persisted) != 1:
        raise ExportIntegrityError("snapshot requires one persisted code decision")
    if persisted[0] != decision:
        raise ExportIntegrityError("persisted code decision disagrees with recomputation")

    if not snapshot.adjudications:
        raise ExportIntegrityError("snapshot is missing a typed adjudication")
    adjudication = snapshot.adjudications[-1]
    if adjudication.candidate_id != candidate.id or adjudication.outcome != "accept":
        raise ExportIntegrityError("latest record is not an accepting adjudication")
    if len(set(adjudication.report_ids)) != len(adjudication.report_ids) or set(
        adjudication.report_ids
    ) != set(decision.report_ids):
        raise ExportIntegrityError("adjudication report ids disagree with code decision")
    adjudicator = next(
        (thread for thread in snapshot.threads if thread.id == adjudication.provenance.source_id),
        None,
    )
    if (
        adjudicator is None
        or adjudicator.run_id != snapshot.run.id
        or adjudicator.role is not ThreadRole.ADJUDICATOR
        or adjudicator.parent_thread_id is not None
        or adjudicator.status is not ThreadStatus.COMPLETED
        or adjudicator.external_thread_id is None
    ):
        raise ExportIntegrityError("adjudicator thread lineage does not match")


def build_export_bundle(
    snapshot: RunSnapshot,
    *,
    candidate_id: str,
    generated_at: datetime,
) -> ExportBundle:
    """Build a verified proof/report/manifest bundle without filesystem effects."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ExportIntegrityError("generated_at must be timezone-aware")
    candidate = _candidate_record(snapshot, candidate_id)
    reports = tuple(
        sorted(
            (record for record in snapshot.verifications if record.candidate_id == candidate_id),
            key=lambda record: (record.kind, record.id),
        )
    )
    _verify_snapshot(snapshot, candidate, reports)
    try:
        decision = decide_candidate(
            candidate.candidate,
            tuple(record.report for record in reports),
            require_citation=bool(snapshot.evidence),
            required_evidence_ids=tuple(item.id for item in snapshot.evidence),
        )
    except CandidateIntegrityError as error:
        raise ExportIntegrityError(str(error)) from error
    if not decision.passed:
        reasons = ", ".join(decision.reasons)
        raise ExportNotReadyError(f"candidate did not pass verification: {reasons}")
    _verify_acceptance(snapshot, candidate, decision)

    proof_md = _render_proof(candidate)
    report_md = _render_report(snapshot, candidate, reports)
    manifest_events, manifest_segments, manifest_resolutions, observed_at = (
        _manifest_window(snapshot, generated_at)
    )
    manifest_turns = _manifest_turns(manifest_events)
    selected_turn_inputs = {turn.turn_input_id for turn in manifest_turns}
    manifest = Manifest(
        run_id=snapshot.run.id,
        run_status=RunStatus.COMPLETED.value,
        code_verdict="PASS",
        input_sha256=snapshot.run.input_sha256,
        config_sha256=snapshot.run.config_sha256,
        runtime_version=snapshot.run.runtime_version,
        prompt_versions=_prompt_versions(snapshot, candidate, reports),
        evidence_records=tuple(
            ManifestRecord(id=item.id, sha256=canonical_sha256(item))
            for item in sorted(snapshot.evidence, key=lambda item: item.id)
        ),
        plan_records=tuple(
            ManifestRecord(id=item.id, sha256=canonical_sha256(item))
            for item in sorted(snapshot.plans, key=lambda item: item.id)
        ),
        candidate_records=tuple(
            ManifestRecord(id=item.id, sha256=item.candidate_sha256)
            for item in sorted(snapshot.candidates, key=lambda item: (item.attempt, item.id))
        ),
        verification_records=tuple(
            ManifestRecord(id=item.id, sha256=item.report_sha256)
            for item in sorted(
                snapshot.verifications,
                key=lambda item: (item.candidate_id, item.kind, item.id),
            )
        ),
        adjudication_records=tuple(
            ManifestRecord(id=item.id, sha256=canonical_sha256(item))
            for item in sorted(snapshot.adjudications, key=lambda item: item.id)
        ),
        decision_records=tuple(
            ManifestRecord(id=item.candidate_id, sha256=canonical_sha256(item))
            for item in sorted(snapshot.decisions, key=lambda item: item.candidate_id)
        ),
        runtime_resolutions=tuple(
            ManifestRuntimeResolution(
                segment_id=item.segment_id,
                sha256=item.resolution_sha256,
                resolution=item.resolution,
            )
            for item in sorted(
                manifest_resolutions,
                key=lambda item: (item.created_at, item.segment_id),
            )
        ),
        execution_segments=_manifest_execution_segments(
            manifest_segments,
            observed_at,
        ),
        turn_inputs=tuple(
            ManifestTurnInput(
                id=item.id,
                role=item.role,
                prompt_version=item.prompt_version,
                output_schema_sha256=item.output_schema_sha256,
                payload_sha256=item.payload_sha256,
                payload=item.payload,
            )
            for item in sorted(snapshot.turn_inputs, key=lambda item: item.id)
            if item.id in selected_turn_inputs
        ),
        turns=manifest_turns,
        threads=tuple(
            ManifestThread(
                id=item.id,
                role=item.role.value,
                parent_thread_id=item.parent_thread_id,
                external_thread_id=item.external_thread_id,
                status=item.status.value,
                provenance=item.provenance,
                provenance_sha256=item.provenance_sha256,
            )
            for item in sorted(snapshot.threads, key=lambda item: item.id)
            if item.created_at <= observed_at
        ),
        findings=tuple(
            ManifestFinding(
                id=f"{record.id}:{finding.id}",
                report_id=record.id,
                finding_id=finding.id,
                sha256=canonical_sha256(finding),
                severity=finding.severity,
                proof_span=finding.proof_span,
                evidence_ids=finding.evidence_ids,
            )
            for record in sorted(snapshot.verifications, key=lambda item: item.id)
            for finding in sorted(record.report.findings, key=lambda item: item.id)
        ),
        usage=_manifest_usage(
            manifest_events,
            manifest_segments,
            observed_at,
        ),
        candidate_hashes=tuple(
            item.candidate_sha256
            for item in sorted(snapshot.candidates, key=lambda item: (item.attempt, item.id))
        ),
        verification_hashes=tuple(
            item.report_sha256
            for item in sorted(
                snapshot.verifications,
                key=lambda item: (item.candidate_id, item.kind, item.id),
            )
        ),
        artifacts=(
            ManifestArtifact(
                id="proof",
                kind="proof",
                sha256=sha256_text(proof_md),
                media_type="text/markdown",
                relative_path="proof.md",
            ),
            ManifestArtifact(
                id="report",
                kind="verification_report",
                sha256=sha256_text(report_md),
                media_type="text/markdown",
                relative_path="report.md",
            ),
        ),
        first_event_seq=manifest_events[0].seq,
        last_event_seq=manifest_events[-1].seq,
        event_chain_sha256=_event_chain_sha256(manifest_events),
        generated_at=generated_at,
    )
    manifest_json = f"{canonical_json(manifest)}\n"
    return ExportBundle(
        run_id=snapshot.run.id,
        proof_md=proof_md,
        report_md=report_md,
        manifest=manifest,
        manifest_json=manifest_json,
        bundle_sha256=sha256_text(manifest_json),
    )
