from __future__ import annotations

import json

import pytest

from qed.evidence import dimension_eligibility, load_stable_evidence, verify_evidence_artifacts
from qed.schemas import canonical_json


def test_stable_evidence_rejects_noncanonical_and_unknown_keys(tmp_path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit_sha": "a" * 40,
                "generated_at": "2026-08-11T00:00:00Z",
                "gates": [
                    {
                        "gate_id": "gate.one",
                        "dimension": "architecture",
                        "required": True,
                        "status": "passed",
                        "command": "true",
                        "utc_date": "2026-08-11",
                        "commit_sha": "a" * 40,
                        "environment": "test",
                        "result": "passed",
                        "artifact_path": "artifact.txt",
                        "artifact_sha256": "b" * 64,
                        "limitation": None,
                        "references": [],
                        "unexpected": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_stable_evidence(path)


def test_dimension_eligibility_is_fail_closed(tmp_path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(canonical_json({
        "schema_version": 1,
        "commit_sha": "a" * 40,
        "generated_at": "2026-08-11T00:00:00Z",
        "gates": [
            {
                "gate_id": "architecture.ok",
                "dimension": "architecture",
                "required": True,
                "status": "passed",
                "command": "true",
                "utc_date": "2026-08-11",
                "commit_sha": "a" * 40,
                "environment": "test",
                "result": "ok",
                "artifact_path": "artifact.txt",
                "artifact_sha256": "b" * 64,
                "limitation": None,
                "references": [],
            },
            {
                "gate_id": "architecture.blocked",
                "dimension": "architecture",
                "required": True,
                "status": "blocked",
                "command": "not-run",
                "utc_date": "2026-08-11",
                "commit_sha": "a" * 40,
                "environment": "test",
                "result": "blocked",
                "artifact_path": "blocker.txt",
                "artifact_sha256": None,
                "limitation": "missing operator input",
                "references": [],
            },
        ],
    }) + "\n", encoding="utf-8")
    evidence = load_stable_evidence(path)
    assert dimension_eligibility(evidence)["architecture"] is False
    assert dimension_eligibility(evidence)["security"] is False


def test_artifact_hashes_are_checked_against_repository(tmp_path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("evidence\n", encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"
    from hashlib import sha256

    value = {
        "schema_version": 1,
        "commit_sha": "a" * 40,
        "generated_at": "2026-08-11T00:00:00Z",
        "gates": [
            {
                "gate_id": "architecture.artifact",
                "dimension": "architecture",
                "status": "passed",
                "command": "test",
                "utc_date": "2026-08-11",
                "commit_sha": "a" * 40,
                "environment": "test",
                    "result": "passed",
                    "artifact_path": "artifact.txt",
                    "artifact_sha256": sha256(b"evidence\n").hexdigest(),
                    "required": True,
                    "limitation": None,
                    "references": [],
                }
        ],
    }
    evidence_path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    verify_evidence_artifacts(load_stable_evidence(evidence_path), tmp_path)
    artifact.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_evidence_artifacts(load_stable_evidence(evidence_path), tmp_path)
