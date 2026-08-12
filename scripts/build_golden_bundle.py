"""Build the deterministic, non-reliability golden export fixture."""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path

from qed.export import build_export_bundle, write_export_bundle


def _fixture_snapshot(repository: Path, root: Path):
    fixture_path = repository / "tests" / "test_export.py"
    spec = importlib.util.spec_from_file_location("qed_golden_export_fixture", fixture_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fixture module: {fixture_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._snapshot(root, complete=True)


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    destination = repository / "artifacts" / "golden-bundle"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError(f"golden bundle path is not a directory: {destination}")
        shutil.rmtree(destination)

    with tempfile.TemporaryDirectory(prefix="qed-golden-") as temporary:
        snapshot = _fixture_snapshot(repository, Path(temporary) / "fixture")
        bundle = build_export_bundle(
            snapshot,
            candidate_id="candidate-1",
            generated_at=snapshot.run.updated_at,
        )
        publish_root = destination.parent / ".golden-publish"
        if publish_root.exists():
            shutil.rmtree(publish_root)
        published = write_export_bundle(bundle, publish_root)
        shutil.copytree(published, destination)
        shutil.rmtree(publish_root)

    print(destination)
    print(bundle.bundle_sha256)


if __name__ == "__main__":
    main()
