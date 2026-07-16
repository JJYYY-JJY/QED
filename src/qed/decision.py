"""Deterministic candidate acceptance rules."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import ConfigDict

from qed.schemas import (
    ProofCandidate,
    StrictModel,
    VerificationReport,
    VerificationVerdict,
    sha256_text,
)

RequiredReportKind = Literal["structural", "detailed", "citation"]


class CandidateIntegrityError(ValueError):
    """Raised when frozen candidate or report provenance no longer matches."""


class CandidateDecision(StrictModel):
    """Code-computed result; no model controls this value."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    candidate_id: str
    candidate_sha256: str
    passed: bool
    required_kinds: tuple[RequiredReportKind, ...]
    report_ids: tuple[str, ...]
    reasons: tuple[str, ...]


def _check_integrity(
    candidate: ProofCandidate,
    reports: tuple[VerificationReport, ...],
) -> None:
    if sha256_text(candidate.proof) != candidate.proof_sha256:
        raise CandidateIntegrityError("candidate proof hash does not match frozen content")
    for report in reports:
        if report.candidate_id != candidate.id:
            raise CandidateIntegrityError(
                f"report {report.id} references another candidate"
            )
        if report.candidate_sha256 != candidate.proof_sha256:
            raise CandidateIntegrityError(
                f"report {report.id} references another candidate hash"
            )


def decide_candidate(
    candidate: ProofCandidate,
    reports: tuple[VerificationReport, ...],
    *,
    require_citation: bool = False,
) -> CandidateDecision:
    """Compute PASS from frozen input and independent structured reports."""

    _check_integrity(candidate, reports)
    required: tuple[RequiredReportKind, ...] = (
        ("structural", "detailed", "citation")
        if require_citation
        else ("structural", "detailed")
    )
    relevant = tuple(report for report in reports if report.kind in required)
    reasons: list[str] = []

    for kind in required:
        matching = tuple(report for report in relevant if report.kind == kind)
        if not matching:
            reasons.append(f"missing:{kind}")
            continue
        for report in matching:
            if report.verdict is not VerificationVerdict.PASS:
                reasons.append(f"non_pass:{kind}:{report.verdict.value}")

    writer_thread = candidate.provenance.source_id
    if writer_thread is not None:
        for report in relevant:
            if report.verifier_thread_id == writer_thread:
                reasons.append(f"writer_thread_reused:{report.kind}")

    thread_counts = Counter(report.verifier_thread_id for report in relevant)
    reasons.extend(
        f"verifier_thread_reused:{thread_id}"
        for thread_id, count in sorted(thread_counts.items())
        if count > 1
    )

    return CandidateDecision(
        candidate_id=candidate.id,
        candidate_sha256=candidate.proof_sha256,
        passed=not reasons,
        required_kinds=required,
        report_ids=tuple(sorted(report.id for report in relevant)),
        reasons=tuple(reasons),
    )
