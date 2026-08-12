from __future__ import annotations

from pathlib import Path

from qed.bundle_verifier import verify_bundle


def test_checked_in_golden_bundle_is_offline_valid() -> None:
    bundle = Path(__file__).parents[1] / "artifacts" / "golden-bundle"
    result = verify_bundle(bundle)
    assert result.valid, result.model_dump()
    assert result.decision == "PASS"
    assert result.signature_status == "unsigned"
