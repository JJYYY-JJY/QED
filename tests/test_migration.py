from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from qed.migration import LegacyImportError, import_legacy_run, inspect_legacy_run


def test_legacy_import_is_content_addressed_non_destructive_and_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old-run"
    (source / "decomposition").mkdir(parents=True)
    (source / "proof.md").write_text("candidate proof\n", encoding="utf-8")
    (source / "decomposition" / "STATUS.md").write_text("DONE\n", encoding="utf-8")
    before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    inspected = inspect_legacy_run(source)
    imported = import_legacy_run(source, tmp_path / "managed")
    repeated = import_legacy_run(source, tmp_path / "managed")

    assert imported.manifest == inspected
    assert repeated == imported
    assert imported.manifest.trust == "legacy_untrusted"
    assert imported.import_dir.name == imported.manifest.import_id
    assert [entry.path for entry in imported.manifest.files] == [
        "decomposition/STATUS.md",
        "proof.md",
    ]
    assert imported.manifest.files[1].sha256 == hashlib.sha256(
        b"candidate proof\n"
    ).hexdigest()
    assert (imported.import_dir / "artifacts" / "proof.md").read_bytes() == before[
        "proof.md"
    ]
    assert {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    } == before


def test_legacy_import_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "old-run"
    source.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (source / "proof.md").symlink_to(outside)

    with pytest.raises(LegacyImportError, match="symbolic link"):
        inspect_legacy_run(source)


@pytest.mark.parametrize("operation", ("inspect", "import"))
def test_legacy_import_rejects_symlinked_source_ancestors(
    tmp_path: Path,
    operation: str,
) -> None:
    actual_parent = tmp_path / "actual-parent"
    source = actual_parent / "old-run"
    source.mkdir(parents=True)
    (source / "proof.md").write_text("outside proof", encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    linked_source = linked_parent / "old-run"

    with pytest.raises(LegacyImportError, match="symbolic link"):
        if operation == "inspect":
            inspect_legacy_run(linked_source)
        else:
            import_legacy_run(linked_source, tmp_path / "managed")


def test_legacy_import_accepts_an_ordinary_relative_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "old-run"
    source.mkdir()
    (source / "proof.md").write_text("proof", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    inspected = inspect_legacy_run(Path("old-run"))
    imported = import_legacy_run(Path("old-run"), Path("managed"))

    assert imported.manifest == inspected
    assert inspected.source_root == str(source)


def test_legacy_import_rejects_a_parent_swapped_to_a_symlink_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "old-run"
    source_parent = source / "nested"
    source_parent.mkdir(parents=True)
    (source_parent / "proof.md").write_text("original proof", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "proof.md").write_text("outside secret", encoding="utf-8")
    displaced = tmp_path / "displaced"
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(os.fsdecode(path)).name == "proof.md":
            source_parent.rename(displaced)
            source_parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(LegacyImportError, match="symbolic link|changed during import"):
        import_legacy_run(source, tmp_path / "managed")

    assert swapped is True
    assert not list((tmp_path / "managed" / "legacy-imports").glob("legacy-*"))


def test_legacy_import_rejects_managed_root_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "old-run"
    source.mkdir()
    (source / "proof.md").write_text("proof", encoding="utf-8")

    with pytest.raises(LegacyImportError, match="outside the legacy source"):
        import_legacy_run(source, source / "managed")


def test_legacy_import_detects_tampered_existing_copy(tmp_path: Path) -> None:
    source = tmp_path / "old-run"
    source.mkdir()
    (source / "proof.md").write_text("proof", encoding="utf-8")
    imported = import_legacy_run(source, tmp_path / "managed")
    (imported.import_dir / "artifacts" / "proof.md").write_text(
        "changed", encoding="utf-8"
    )

    with pytest.raises(LegacyImportError, match="does not match"):
        import_legacy_run(source, tmp_path / "managed")
