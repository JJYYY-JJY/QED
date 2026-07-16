"""Safe, content-addressed import of legacy file-based QED runs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LegacyImportError(ValueError):
    """Raised when a legacy directory cannot be imported safely."""


class LegacyFile(BaseModel):
    """One immutable file in a legacy import."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class LegacyImportManifest(BaseModel):
    """Deterministic inventory for an untrusted legacy run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    import_id: str = Field(pattern=r"^legacy-[0-9a-f]{24}$")
    trust: Literal["legacy_untrusted"] = "legacy_untrusted"
    source_root: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[LegacyFile, ...]


@dataclass(frozen=True)
class ImportedLegacyRun:
    """Location and verified manifest of a managed legacy copy."""

    import_dir: Path
    manifest: LegacyImportManifest


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LegacyImportError(f"cannot safely read legacy file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LegacyImportError(f"legacy entry is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _inventory(root: Path) -> tuple[LegacyFile, ...]:
    entries: list[LegacyFile] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise LegacyImportError(f"legacy source contains a symbolic link: {path}")
        if path.is_dir():
            continue
        data = _read_regular_file(path)
        entries.append(
            LegacyFile(
                path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
        )
    return tuple(entries)


def _content_hash(files: tuple[LegacyFile, ...]) -> str:
    payload = [entry.model_dump(mode="json") for entry in files]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_source(source: Path) -> Path:
    if source.is_symlink():
        raise LegacyImportError("legacy source cannot be a symbolic link")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise LegacyImportError(f"legacy source does not exist: {source}") from exc
    if not resolved.is_dir():
        raise LegacyImportError(f"legacy source is not a directory: {source}")
    return resolved


def inspect_legacy_run(source: Path) -> LegacyImportManifest:
    """Read a legacy directory without modifying it and create its manifest."""

    source_root = _resolve_source(source)
    files = _inventory(source_root)
    content_sha256 = _content_hash(files)
    return LegacyImportManifest(
        import_id=f"legacy-{content_sha256[:24]}",
        source_root=str(source_root),
        content_sha256=content_sha256,
        files=files,
    )


def _validate_existing(target: Path, expected: LegacyImportManifest) -> ImportedLegacyRun:
    manifest_path = target / "manifest.json"
    if manifest_path.is_symlink():
        raise LegacyImportError("existing legacy import manifest does not match source")
    try:
        actual = LegacyImportManifest.model_validate_json(_read_regular_file(manifest_path))
    except (LegacyImportError, ValueError) as exc:
        raise LegacyImportError("existing legacy import manifest does not match source") from exc
    artifacts = target / "artifacts"
    if actual != expected or not artifacts.is_dir() or _inventory(artifacts) != expected.files:
        raise LegacyImportError("existing legacy import does not match source")
    return ImportedLegacyRun(import_dir=target, manifest=actual)


def import_legacy_run(source: Path, managed_root: Path) -> ImportedLegacyRun:
    """Copy a legacy run into a managed, content-addressed, untrusted import."""

    source_root = _resolve_source(source)
    root = managed_root.resolve(strict=False)
    if root == source_root or root.is_relative_to(source_root):
        raise LegacyImportError("managed root must be outside the legacy source")

    manifest = inspect_legacy_run(source_root)
    imports_root = root / "legacy-imports"
    imports_root.mkdir(parents=True, exist_ok=True)
    target = imports_root / manifest.import_id
    if target.exists():
        return _validate_existing(target, manifest)

    staging = Path(tempfile.mkdtemp(prefix=f".{manifest.import_id}-", dir=imports_root))
    try:
        artifacts = staging / "artifacts"
        for entry in manifest.files:
            source_file = source_root / entry.path
            data = _read_regular_file(source_file)
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise LegacyImportError(f"legacy source changed during import: {entry.path}")
            destination = artifacts / entry.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)

        manifest_json = json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        (staging / "manifest.json").write_text(f"{manifest_json}\n", encoding="utf-8")
        try:
            staging.rename(target)
        except FileExistsError:
            return _validate_existing(target, manifest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return ImportedLegacyRun(import_dir=target, manifest=manifest)
