from __future__ import annotations

import json
import os
from pathlib import Path

from qed.bundle_verifier import verify_bundle
from qed.export import build_export_bundle, write_export_bundle

from .test_export import GENERATED_AT, _snapshot


def test_offline_verifier_accepts_a_stable_fixture_bundle(tmp_path: Path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    destination = write_export_bundle(bundle, tmp_path / "exports")

    result = verify_bundle(destination)

    assert result.valid is True
    assert result.decision == "PASS"
    assert result.warnings


def test_offline_verifier_rejects_extra_file_and_manifest_tampering(tmp_path: Path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    destination = write_export_bundle(bundle, tmp_path / "exports")
    (destination / "unexpected.txt").write_text("no", encoding="utf-8")

    result = verify_bundle(destination)

    assert result.valid is False
    assert "unexpected bundle file: unexpected.txt" in result.errors

    (destination / "unexpected.txt").unlink()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["run_id"] = "tampered"
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = verify_bundle(destination)
    assert result.valid is False
    assert any("manifest" in error for error in result.errors)


def test_offline_verifier_recomputes_artifact_hashes_and_rejects_links(
    tmp_path: Path,
) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    destination = write_export_bundle(bundle, tmp_path / "exports")
    (destination / "report.md").write_text("tampered", encoding="utf-8")
    result = verify_bundle(destination)
    assert result.valid is False
    assert "artifact hash does not match manifest: report.md" in result.errors

    destination = write_export_bundle(bundle, tmp_path / "other-exports")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (destination / "proof.md").unlink()
    (destination / "proof.md").symlink_to(outside)
    result = verify_bundle(destination)
    assert result.valid is False
    assert any("regular file" in error for error in result.errors)

    destination = write_export_bundle(bundle, tmp_path / "hardlink-exports")
    outside = tmp_path / "hardlink-source.txt"
    outside.write_bytes((destination / "proof.md").read_bytes())
    (destination / "proof.md").unlink()
    os.link(outside, destination / "proof.md")
    result = verify_bundle(destination)
    assert result.valid is False
    assert any("private regular file" in error for error in result.errors)


def test_offline_verifier_rejects_noncanonical_and_incomplete_json(tmp_path: Path) -> None:
    bundle = build_export_bundle(
        _snapshot(tmp_path / "state"),
        candidate_id="candidate-1",
        generated_at=GENERATED_AT,
    )
    destination = write_export_bundle(bundle, tmp_path / "exports")
    (destination / "event-chain.json").write_text("[]\n", encoding="utf-8")
    result = verify_bundle(destination)
    assert result.valid is False
    assert "event-chain.json must contain a non-empty event list" in result.errors

    destination = write_export_bundle(bundle, tmp_path / "noncanonical")
    (destination / "event-chain.json").write_text(
        json.dumps(json.loads((destination / "event-chain.json").read_text()))
        + "\n",
        encoding="utf-8",
    )
    result = verify_bundle(destination)
    assert result.valid is False
    assert any("not canonical JSON" in error for error in result.errors)

    destination = write_export_bundle(bundle, tmp_path / "duplicate-key")
    (destination / "audit.json").write_text('{"candidate":{},"candidate":{}}\n', encoding="utf-8")
    result = verify_bundle(destination)
    assert result.valid is False
    assert any("valid UTF-8 JSON" in error for error in result.errors)
