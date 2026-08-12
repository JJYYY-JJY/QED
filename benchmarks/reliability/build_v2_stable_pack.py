#!/usr/bin/env python3
"""Build the versioned development pack without touching the frozen alpha pack."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ALPHA_CASES = ROOT / "cases.jsonl"
CASE_SCHEMA = ROOT / "case.schema.json"
STABLE_CASES = ROOT / "v2-stable-cases.jsonl"
STABLE_LOCK = ROOT / "v2-stable-cases.lock.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _case(
    case_id: str,
    title: str,
    category: str,
    statement: str,
    candidate_proof: str,
    tags: list[str],
    expected: dict[str, str],
    *,
    citation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "title": title,
        "category": category,
        "statement": statement,
        "candidate_proof": candidate_proof,
        "verification_rules": ["Check every stated hypothesis and inference."],
        "expected": expected,
        "tags": tags,
        "citation": citation,
        "mutation": None,
    }


def _extensions() -> list[dict[str, Any]]:
    citation_excerpt = "The cited theorem concerns continuity, not differentiability."
    citation = {
        "claim": "The cited theorem proves differentiability at every point.",
        "cited_evidence_ids": ["fixture-continuity-theorem"],
        "evidence": [
            {
                "evidence_id": "fixture-continuity-theorem",
                "title": "Continuity theorem fixture",
                "source_uri": "https://example.invalid/continuity",
                "final_uri": "https://example.invalid/continuity",
                "retrieved_at": "2026-01-01T00:00:00Z",
                "excerpt": citation_excerpt,
                "provenance": "benchmark_fixture",
                "content_sha256": _sha256(citation_excerpt.encode()),
            }
        ],
    }
    return [
        _case(
            "stable-algebra-factorization",
            "Algebra factorization",
            "known_true_correct_proof",
            "For every real x, x^2 - 1 = (x - 1)(x + 1).",
            "Expand the right side: (x - 1)(x + 1) = x^2 + x - x - 1 = x^2 - 1.",
            ["algebra"],
            {"policy_verdict": "PASS", "citation_support": "NOT_APPLICABLE"},
        ),
        _case(
            "stable-analysis-uniform-limit",
            "Pointwise versus uniform convergence",
            "missing_assumption",
            "Every pointwise limit of continuous functions is continuous.",
            "Each function is continuous, so taking the pointwise limit preserves continuity.",
            ["analysis", "pointwise-versus-uniform"],
            {"policy_verdict": "NON_PASS", "citation_support": "NOT_APPLICABLE"},
        ),
        _case(
            "stable-probability-almost-sure",
            "Almost sure versus everywhere",
            "domain_error",
            "If X_n converges almost surely to X, then it converges at every outcome.",
            (
                "Almost-sure convergence means the exceptional set is empty, hence "
                "convergence holds everywhere."
            ),
            ["probability", "almost-sure-versus-everywhere"],
            {"policy_verdict": "NON_PASS", "citation_support": "NOT_APPLICABLE"},
        ),
        _case(
            "stable-combinatorics-pigeonhole",
            "Pigeonhole principle",
            "known_true_correct_proof",
            "Placing n+1 objects into n boxes puts two objects in one box.",
            (
                "If each box contained at most one object, there would be at most n "
                "objects, a contradiction."
            ),
            ["combinatorics"],
            {"policy_verdict": "PASS", "citation_support": "NOT_APPLICABLE"},
        ),
        _case(
            "stable-geometry-topology-continuity",
            "Continuous image of a compact interval",
            "known_true_correct_proof",
            "A continuous function on [0,1] is bounded.",
            (
                "The interval [0,1] is compact, and the continuous image of a compact "
                "set is compact and therefore bounded."
            ),
            ["geometry", "topology"],
            {"policy_verdict": "PASS", "citation_support": "NOT_APPLICABLE"},
        ),
        _case(
            "stable-differential-equation-uniqueness",
            "ODE uniqueness hypotheses",
            "missing_assumption",
            "Every differential equation has a unique solution through every initial point.",
            (
                "Existence of one solution automatically excludes a second solution "
                "through the same point."
            ),
            ["differential-equations", "uniqueness"],
            {"policy_verdict": "NON_PASS", "citation_support": "NOT_APPLICABLE"},
        ),
        _case(
            "stable-number-theory-boundary",
            "Boundary case in a divisibility argument",
            "missing_boundary_case",
            "For every integer n, n divides n+1.",
            "Since n+1 is one more than n, n divides n+1.",
            ["number-theory", "boundary-case"],
            {"policy_verdict": "NON_PASS", "citation_support": "NOT_APPLICABLE"},
        ),
        _case(
            "stable-citation-nearby-claim",
            "Citation supports a nearby claim only",
            "citation_unsupported",
            "A continuity result cited for a differentiability claim supports the exact claim.",
            "The source mentions continuity, so it proves differentiability as well.",
            ["citation", "nearby-but-different-claim"],
            {"policy_verdict": "NON_PASS", "citation_support": "UNSUPPORTED"},
            citation=citation,
        ),
    ]


def main() -> None:
    alpha_lines = [line for line in ALPHA_CASES.read_bytes().splitlines() if line]
    alpha_ids = {json.loads(line)["case_id"] for line in alpha_lines}
    extensions = _extensions()
    if alpha_ids & {case["case_id"] for case in extensions}:
        raise SystemExit("stable extension case ID collides with the frozen alpha pack")
    cases = [json.loads(line) for line in alpha_lines] + extensions
    payload = b"".join(_canonical(case) + b"\n" for case in cases)
    STABLE_CASES.write_bytes(payload)
    lock = {
        "schema_version": 1,
        "case_schema_sha256": _sha256(CASE_SCHEMA.read_bytes()),
        "cases_file_sha256": _sha256(payload),
        "case_count": len(cases),
        "case_hashes": {
            case["case_id"]: _sha256(_canonical(case))
            for case in cases
        },
    }
    STABLE_LOCK.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "case_count": len(cases),
                "cases": str(STABLE_CASES),
                "lock": str(STABLE_LOCK),
                "cases_file_sha256": lock["cases_file_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
