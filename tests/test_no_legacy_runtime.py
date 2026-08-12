from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REMOVED_PATHS = (
    "code",
    "ui",
    "verify/verify.py",
    "run.sh",
    "run_verifier.sh",
    "clean.sh",
    "config.yaml",
)

PRODUCTION_ROOTS = (
    "src",
    "frontend",
    "web",
    "client",
    "server",
    ".codex",
)

PRODUCTION_SUFFIXES = {
    ".bash",
    ".json",
    ".lock",
    ".py",
    ".pyi",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}

ROOT_MANIFESTS = (
    "pyproject.toml",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
    "poetry.lock",
    "pnpm-lock.yaml",
    "yarn.lock",
)

FORBIDDEN_RUNTIME_PATTERNS = {
    "Anthropic runtime": re.compile(r"\banthropic\b", re.IGNORECASE),
    "Claude runtime": re.compile(r"\bclaude\b", re.IGNORECASE),
    "Google Generative AI runtime": re.compile(
        r"\bgoogle\.generativeai\b", re.IGNORECASE
    ),
    "Gemini runtime": re.compile(r"\bgemini\b", re.IGNORECASE),
    "Streamlit runtime": re.compile(r"\bstreamlit(?:-autorefresh)?\b", re.IGNORECASE),
    "legacy provider dispatch": re.compile(
        r"\bprovider\s+dispatch\b|\b(?:run|dispatch)_(?:claude|gemini)\b",
        re.IGNORECASE,
    ),
    "dangerous bypass flag": re.compile(
        r"--dangerously-(?:bypass|skip)[a-z-]*", re.IGNORECASE
    ),
    "permission bypass mode": re.compile(r"\bbypassPermissions\b", re.IGNORECASE),
    "YOLO approval mode": re.compile(
        r"\bapproval[_-]?mode\s*[:=]\s*[\"']?yolo\b", re.IGNORECASE
    ),
    "Conda runtime launcher": re.compile(
        r"\bconda(?:\.exe)?\s+(?:run|activate)\b", re.IGNORECASE
    ),
}


def _production_files() -> Iterator[Path]:
    seen: set[Path] = set()

    for relative in ROOT_MANIFESTS:
        path = ROOT / relative
        if path.is_file():
            seen.add(path)
            yield path

    for path in sorted(ROOT.glob("requirements*.txt")):
        if path.is_file() and path not in seen:
            seen.add(path)
            yield path

    for relative in PRODUCTION_ROOTS:
        base = ROOT / relative
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                path.is_file()
                and (
                    path.suffix.lower() in PRODUCTION_SUFFIXES
                    or path.name.startswith("requirements")
                )
                and path not in seen
            ):
                seen.add(path)
                yield path

    for path in sorted(ROOT.iterdir()):
        if (
            path.is_file()
            and path.suffix.lower() in {".bash", ".sh", ".yaml", ".yml"}
            and path not in seen
        ):
            yield path


def test_legacy_runtime_and_ui_paths_are_removed() -> None:
    remaining = [relative for relative in REMOVED_PATHS if (ROOT / relative).exists()]
    assert remaining == []


def test_production_surfaces_do_not_restore_legacy_runtime() -> None:
    violations: list[str] = []

    for path in _production_files():
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN_RUNTIME_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(ROOT)}:{line}: {label}")

    assert violations == [], "Legacy production runtime references found:\n" + "\n".join(
        violations
    )
