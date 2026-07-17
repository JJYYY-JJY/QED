"""Safe, content-addressed import of legacy file-based QED runs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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


def _descriptor_flags(*, directory: bool) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise LegacyImportError(
            "legacy import requires descriptor-relative no-follow filesystem controls"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    display_path: str,
    expected: os.stat_result | None = None,
) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            _descriptor_flags(directory=True),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise LegacyImportError(f"legacy entry is not a directory: {display_path}")
        if expected is not None and not _same_file(metadata, expected):
            raise LegacyImportError(f"legacy source changed during import: {display_path}")
        return descriptor
    except LegacyImportError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise LegacyImportError(
            f"legacy source contains a symbolic link or changed during import: {display_path}"
        ) from exc


@contextmanager
def _pinned_directory(path: Path) -> Iterator[tuple[int, Path]]:
    descriptor: int | None = None
    try:
        absolute_path = path.absolute()
        descriptor = os.open(
            absolute_path.anchor,
            _descriptor_flags(directory=True),
        )
        for index, component in enumerate(absolute_path.parts[1:], start=1):
            child_descriptor = _open_directory_at(
                descriptor,
                component,
                display_path=str(Path(*absolute_path.parts[: index + 1])),
            )
            os.close(descriptor)
            descriptor = child_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise LegacyImportError(f"legacy source is not a directory: {path}")
        source_root = Path(os.path.normpath(absolute_path))
        yield descriptor, source_root
    except LegacyImportError:
        raise
    except OSError as exc:
        raise LegacyImportError(f"cannot safely open legacy directory: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    display_path: str,
    expected: os.stat_result | None = None,
) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            _descriptor_flags(directory=False),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LegacyImportError(f"legacy entry is not a regular file: {display_path}")
        if expected is not None and not _same_file(metadata, expected):
            raise LegacyImportError(f"legacy source changed during import: {display_path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read()
    except LegacyImportError:
        raise
    except OSError as exc:
        raise LegacyImportError(
            f"legacy source contains a symbolic link or changed during import: {display_path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _descriptor_flags(directory=False))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LegacyImportError(f"legacy entry is not a regular file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read()
    except LegacyImportError:
        raise
    except OSError as exc:
        raise LegacyImportError(f"cannot safely read legacy file: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_component(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise LegacyImportError(f"legacy source contains an invalid path component: {name!r}")


def _inventory(
    root_descriptor: int,
    *,
    relative_root: PurePosixPath | None = None,
) -> tuple[LegacyFile, ...]:
    if relative_root is None:
        relative_root = PurePosixPath()
    entries: list[LegacyFile] = []
    try:
        with os.scandir(root_descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise LegacyImportError("cannot safely enumerate legacy directory") from exc
    for name in names:
        _validate_component(name)
        relative_path = relative_root / name
        display_path = relative_path.as_posix()
        try:
            metadata = os.stat(
                name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise LegacyImportError(
                f"legacy source changed during import: {display_path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise LegacyImportError(
                f"legacy source contains a symbolic link: {display_path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            child_descriptor = _open_directory_at(
                root_descriptor,
                name,
                display_path=display_path,
                expected=metadata,
            )
            try:
                entries.extend(
                    _inventory(
                        child_descriptor,
                        relative_root=relative_path,
                    )
                )
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise LegacyImportError(
                f"legacy entry is not a regular file: {display_path}"
            )
        data = _read_regular_at(
            root_descriptor,
            name,
            display_path=display_path,
            expected=metadata,
        )
        entries.append(
            LegacyFile(
                path=display_path,
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
        )
    return tuple(entries)


def _content_hash(files: tuple[LegacyFile, ...]) -> str:
    payload = [entry.model_dump(mode="json") for entry in files]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manifest_for_descriptor(
    source_root: Path,
    source_descriptor: int,
) -> LegacyImportManifest:
    files = _inventory(source_descriptor)
    content_sha256 = _content_hash(files)
    return LegacyImportManifest(
        import_id=f"legacy-{content_sha256[:24]}",
        source_root=str(source_root),
        content_sha256=content_sha256,
        files=files,
    )


def inspect_legacy_run(source: Path) -> LegacyImportManifest:
    """Read a legacy directory without modifying it and create its manifest."""

    with _pinned_directory(source) as (source_descriptor, source_root):
        return _manifest_for_descriptor(source_root, source_descriptor)


def _read_relative_regular(root_descriptor: int, relative_path: str) -> bytes:
    parts = PurePosixPath(relative_path).parts
    if not parts or any(
        part in {"", ".", ".."} or "/" in part or "\x00" in part for part in parts
    ):
        raise LegacyImportError(f"invalid legacy file path: {relative_path}")
    parent_descriptor = os.dup(root_descriptor)
    try:
        for index, part in enumerate(parts[:-1], start=1):
            child_descriptor = _open_directory_at(
                parent_descriptor,
                part,
                display_path=PurePosixPath(*parts[:index]).as_posix(),
            )
            os.close(parent_descriptor)
            parent_descriptor = child_descriptor
        return _read_regular_at(
            parent_descriptor,
            parts[-1],
            display_path=relative_path,
        )
    finally:
        os.close(parent_descriptor)


def _inventory_path(root: Path) -> tuple[LegacyFile, ...]:
    with _pinned_directory(root) as (descriptor, _):
        return _inventory(descriptor)


def _validate_existing(target: Path, expected: LegacyImportManifest) -> ImportedLegacyRun:
    manifest_path = target / "manifest.json"
    if manifest_path.is_symlink():
        raise LegacyImportError("existing legacy import manifest does not match source")
    try:
        actual = LegacyImportManifest.model_validate_json(_read_regular_file(manifest_path))
    except (LegacyImportError, ValueError) as exc:
        raise LegacyImportError("existing legacy import manifest does not match source") from exc
    artifacts = target / "artifacts"
    if (
        actual != expected
        or not artifacts.is_dir()
        or _inventory_path(artifacts) != expected.files
    ):
        raise LegacyImportError("existing legacy import does not match source")
    return ImportedLegacyRun(import_dir=target, manifest=actual)


def import_legacy_run(source: Path, managed_root: Path) -> ImportedLegacyRun:
    """Copy a legacy run into a managed, content-addressed, untrusted import."""

    with _pinned_directory(source) as (source_descriptor, source_root):
        root = managed_root.resolve(strict=False)
        if root == source_root or root.is_relative_to(source_root):
            raise LegacyImportError("managed root must be outside the legacy source")

        manifest = _manifest_for_descriptor(source_root, source_descriptor)
        imports_root = root / "legacy-imports"
        imports_root.mkdir(parents=True, exist_ok=True)
        target = imports_root / manifest.import_id
        if target.exists():
            return _validate_existing(target, manifest)

        staging = Path(tempfile.mkdtemp(prefix=f".{manifest.import_id}-", dir=imports_root))
        try:
            artifacts = staging / "artifacts"
            for entry in manifest.files:
                data = _read_relative_regular(source_descriptor, entry.path)
                if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise LegacyImportError(
                        f"legacy source changed during import: {entry.path}"
                    )
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
