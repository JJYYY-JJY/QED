"""Deterministic candidate acceptance rules."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import ConfigDict

from qed.schemas import (
    CheckStatus,
    Evidence,
    ProofCandidate,
    StrictModel,
    VerificationReport,
    VerificationVerdict,
    canonical_sha256,
    sha256_text,
)

RequiredReportKind = Literal[
    "structural",
    "detailed",
    "assumptions_quantifiers",
    "counterexample_edge_case",
    "reconstruction",
    "citation",
]

STABLE_REQUIRED_REPORT_KINDS: tuple[RequiredReportKind, ...] = (
    "structural",
    "detailed",
    "assumptions_quantifiers",
    "counterexample_edge_case",
    "reconstruction",
)


class CandidateIntegrityError(ValueError):
    """Raised when frozen candidate or report provenance no longer matches."""


class RuleCoverage(StrictModel):
    """One structured verifier check that explicitly addressed a frozen rule."""

    rule_id: str
    report_id: str
    check_id: str
    status: CheckStatus


class CandidateDecision(StrictModel):
    """Code-computed result; no model controls this value."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1, 2, 3] = 3
    candidate_id: str
    candidate_sha256: str
    passed: bool
    required_kinds: tuple[RequiredReportKind, ...]
    required_rule_ids: tuple[str, ...] = ()
    rule_coverage: tuple[RuleCoverage, ...] = ()
    report_ids: tuple[str, ...]
    reasons: tuple[str, ...]


def candidate_decision_sha256(decision: CandidateDecision) -> str:
    """Hash legacy decisions without retroactively adding v2 authority fields."""

    payload = decision.model_dump(mode="json")
    if decision.schema_version == 1:
        payload.pop("required_rule_ids", None)
        payload.pop("rule_coverage", None)
    return canonical_sha256(payload)


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
    prover_external_thread_id: str,
    require_citation: bool = False,
    required_evidence: tuple[Evidence, ...] = (),
    required_evidence_ids: tuple[str, ...] = (),
    required_rule_ids: tuple[str, ...] = (),
    required_report_kinds: tuple[RequiredReportKind, ...] | None = None,
) -> CandidateDecision:
    """Compute PASS from frozen input and independent structured reports."""

    _check_integrity(candidate, reports)
    if not prover_external_thread_id.strip():
        raise ValueError("prover_external_thread_id must be nonempty")
    if len(set(required_rule_ids)) != len(required_rule_ids):
        raise ValueError("required_rule_ids must be unique")
    evidence_ids = tuple(item.id for item in required_evidence)
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("required evidence ids must be unique")
    if (
        required_evidence_ids
        and required_evidence
        and set(required_evidence_ids) != set(evidence_ids)
    ):
        raise ValueError("required evidence records and ids disagree")
    effective_evidence_ids = evidence_ids or required_evidence_ids
    require_citation = require_citation or bool(effective_evidence_ids)
    required: tuple[RequiredReportKind, ...] = required_report_kinds or (
        ("structural", "detailed", "citation")
        if require_citation
        else ("structural", "detailed")
    )
    if len(set(required)) != len(required):
        raise ValueError("required report kinds must be unique")
    if require_citation and "citation" not in required:
        raise ValueError("citation is required when evidence is frozen")
    relevant = tuple(report for report in reports if report.kind in required)
    reasons: list[str] = []

    reasons.extend(
        f"legacy_report_schema:{report.id}:v{report.schema_version}"
        for report in relevant
        if report.schema_version != 3
    )

    for kind in required:
        matching = tuple(report for report in relevant if report.kind == kind)
        if not matching:
            reasons.append(f"missing:{kind}")
            continue
        for report in matching:
            if report.verdict is not VerificationVerdict.PASS:
                reasons.append(f"non_pass:{kind}:{report.verdict.value}")

    required_evidence_by_id = {item.id: item for item in required_evidence}
    required_evidence_id_set = set(effective_evidence_ids)
    citation_reports = tuple(report for report in relevant if report.kind == "citation")
    supported_evidence: set[str] = set()
    if effective_evidence_ids and not required_evidence:
        reasons.append("citation_evidence_records_missing")
    for report in relevant:
        if report.kind != "citation" and any(
            check.citation_support for check in report.checks
        ):
            reasons.append(f"citation_support_wrong_report_kind:{report.id}")
    for report in citation_reports:
        supports = tuple(
            (check, support)
            for check in report.checks
            for support in check.citation_support
        )
        referenced_evidence = {support.evidence_id for _, support in supports}
        for evidence_id in sorted(referenced_evidence - required_evidence_id_set):
            reasons.append(f"citation_unknown_evidence:{evidence_id}")
        for check, support in supports:
            evidence = required_evidence_by_id.get(support.evidence_id)
            if evidence is None:
                continue
            valid = True
            if support.proof_span not in candidate.proof:
                reasons.append(
                    "citation_proof_span_mismatch:"
                    f"{report.id}:{check.id}:{support.evidence_id}"
                )
                valid = False
            if support.evidence_excerpt not in evidence.content:
                reasons.append(
                    "citation_excerpt_mismatch:"
                    f"{report.id}:{check.id}:{support.evidence_id}"
                )
                valid = False
            expected_locator = evidence.source_uri or f"evidence:{evidence.id}"
            if support.source_locator != expected_locator:
                reasons.append(
                    "citation_source_locator_mismatch:"
                    f"{report.id}:{check.id}:{support.evidence_id}"
                )
                valid = False
            if valid and check.status is CheckStatus.PASS:
                supported_evidence.add(support.evidence_id)
    for evidence_id in sorted(required_evidence_id_set - supported_evidence):
        reasons.append(f"citation_missing_evidence:{evidence_id}")

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

    external_thread_counts = Counter(
        report.verifier_external_thread_id
        for report in relevant
        if report.verifier_external_thread_id is not None
    )
    reasons.extend(
        f"verifier_external_thread_reused:{thread_id}"
        for thread_id, count in sorted(external_thread_counts.items())
        if count > 1
    )
    reasons.extend(
        f"missing_verifier_external_thread:{report.id}"
        for report in relevant
        if report.verifier_external_thread_id is None
    )
    reasons.extend(
        f"writer_external_thread_reused:{report.kind}"
        for report in relevant
        if report.verifier_external_thread_id == prover_external_thread_id
    )

    required_rules = set(required_rule_ids)
    rule_coverage = tuple(
        sorted(
            (
                RuleCoverage(
                    rule_id=rule_id,
                    report_id=report.id,
                    check_id=check.id,
                    status=check.status,
                )
                for report in relevant
                for check in report.checks
                for rule_id in check.rule_ids
            ),
            key=lambda item: (item.rule_id, item.report_id, item.check_id),
        )
    )
    referenced_rules = {item.rule_id for item in rule_coverage}
    reasons.extend(
        f"unknown_rule:{rule_id}"
        for rule_id in sorted(referenced_rules - required_rules)
    )
    passed_rules = {
        item.rule_id
        for item in rule_coverage
        if item.rule_id in required_rules and item.status is CheckStatus.PASS
    }
    reasons.extend(
        f"rule_not_passed:{rule_id}"
        for rule_id in sorted(required_rules - passed_rules)
    )

    return CandidateDecision(
        candidate_id=candidate.id,
        candidate_sha256=candidate.proof_sha256,
        passed=not reasons,
        required_kinds=required,
        required_rule_ids=required_rule_ids,
        rule_coverage=rule_coverage,
        report_ids=tuple(sorted(report.id for report in relevant)),
        reasons=tuple(reasons),
    )


def decide_stable_candidate(
    candidate: ProofCandidate,
    reports: tuple[VerificationReport, ...],
    *,
    prover_external_thread_id: str,
    required_evidence: tuple[Evidence, ...] = (),
    required_rule_ids: tuple[str, ...] = (),
) -> CandidateDecision:
    """Compute the release policy with the frozen N-of-N verifier roles."""

    required = STABLE_REQUIRED_REPORT_KINDS + (("citation",) if required_evidence else ())
    return decide_candidate(
        candidate,
        reports,
        prover_external_thread_id=prover_external_thread_id,
        require_citation=bool(required_evidence),
        required_evidence=required_evidence,
        required_rule_ids=required_rule_ids,
        required_report_kinds=required,
    )
