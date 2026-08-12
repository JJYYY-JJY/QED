"""Load and recompute the stable-release evidence contract.

Evidence is an audit input, not a score override.  The only score-like value
this module exposes is derived from required gate statuses at read time.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from qed.schemas import canonical_json, canonical_sha256
from qed.stable_contracts import StableEvidence


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate evidence JSON key: {key}")
        result[key] = value
    return result


def load_stable_evidence(path: str | Path) -> StableEvidence:
    """Read one canonical, strictly typed evidence document."""

    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"evidence path is not a regular file: {target}")
    try:
        text = target.read_text(encoding="utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"evidence is not valid JSON: {target}") from error
    evidence = StableEvidence.model_validate_json(canonical_json(value))
    if text != f"{canonical_json(evidence)}\n":
        raise ValueError("stable evidence must use canonical JSON formatting")
    return evidence


def dimension_eligibility(evidence: StableEvidence) -> dict[str, bool]:
    """Compute eligibility without trusting a stored or prose score."""

    return {
        dimension: evidence.eligible_for_10(dimension)
        for dimension in ("architecture", "security", "mathematics", "maturity")
    }


def evidence_digest(evidence: StableEvidence) -> str:
    return canonical_sha256(evidence)


def verify_evidence_artifacts(evidence: StableEvidence, repository: str | Path) -> None:
    """Check every supplied artifact hash without allowing path escape."""

    root = Path(repository).absolute()
    for gate in evidence.gates:
        if gate.artifact_sha256 is None:
            continue
        artifact = (root / gate.artifact_path).absolute()
        try:
            artifact.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"evidence artifact escapes repository: {gate.artifact_path}"
            ) from error
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"evidence artifact is not a regular file: {gate.artifact_path}")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if digest != gate.artifact_sha256:
            raise ValueError(f"evidence artifact hash mismatch: {gate.artifact_path}")
