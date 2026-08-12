"""Fail-closed checks for server-owned filesystem paths."""

from __future__ import annotations

import stat
from pathlib import Path


class PathSecurityError(ValueError):
    """A managed path cannot be proven to stay within its owner boundary."""


def reject_symlink_components(path: Path) -> None:
    """Reject symlinks and Windows junctions in every existing path component."""

    if not path.is_absolute():
        raise PathSecurityError("managed path must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            current.lstat()
        except FileNotFoundError:
            continue
        if current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)()):
            raise PathSecurityError(f"managed path contains a link: {current}")
        if component in {".", ".."}:
            raise PathSecurityError(f"managed path contains an invalid component: {current}")


def ensure_private_directory(path: Path, *, create: bool = False) -> Path:
    """Check/create a mode-0700 directory without following links."""

    target = path if path.is_absolute() else path.absolute()
    reject_symlink_components(target)
    if create:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        reject_symlink_components(target)
    if not target.exists() or not target.is_dir():
        raise PathSecurityError(f"managed directory is missing or not a directory: {target}")
    metadata = target.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise PathSecurityError(f"managed path is not a directory: {target}")
    mode = metadata.st_mode & 0o777
    if mode & 0o077:
        raise PathSecurityError(f"managed directory must have mode 0700: {target}")
    return target


def require_descendant(root: Path, child: Path) -> None:
    """Reject path traversal after resolving only trusted existing components."""

    reject_symlink_components(root)
    reject_symlink_components(child)
    try:
        child.absolute().relative_to(root.absolute())
    except ValueError as error:
        raise PathSecurityError(f"path escapes managed root: {child}") from error
