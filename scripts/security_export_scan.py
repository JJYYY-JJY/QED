"""Run deterministic provider, unsafe-control, secret, and bundle scans."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "src", ROOT / "frontend")
PRODUCTION_FILES = (
    ROOT / "pyproject.toml",
    ROOT / "uv.lock",
    ROOT / "package.json",
    ROOT / "package-lock.json",
)
FORBIDDEN = re.compile(
    r"anthropic|gemini|copilot|langchain|llama|ollama|openai-agents|"
    r"danger-full-access|approval.?bypass",
    re.IGNORECASE,
)
SECRET = re.compile(
    r"-----BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY-----|"
    r"sk-[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9._-]{20,}"
)
FORBIDDEN_EXPORT_NAMES = {"auth.json", "credentials.json", "token.json", "auth.db"}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _production_files() -> tuple[Path, ...]:
    files = list(PRODUCTION_FILES)
    for root in PRODUCTION_ROOTS:
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return tuple(sorted(set(files)))


def _matches(paths: tuple[Path, ...], pattern: re.Pattern[str]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{path.relative_to(ROOT)}:{line}")
    return findings


def _commit_sha() -> str:
    git_path = shutil.which("git")
    if git_path is None:
        raise RuntimeError("git is required for the security scan")
    git = subprocess.run(  # noqa: S603 - executable is resolved from trusted PATH
        (git_path, "rev-parse", "HEAD"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return git.stdout.strip()


def scan() -> dict[str, object]:
    production = _production_files()
    bundle = ROOT / "artifacts/golden-bundle"
    bundle_files = tuple(sorted(path for path in bundle.rglob("*") if path.is_file()))
    secret_paths = production + bundle_files
    forbidden = _matches(production, FORBIDDEN)
    secrets = _matches(secret_paths, SECRET)
    export_names = [
        str(path.relative_to(bundle))
        for path in bundle_files
        if path.name.lower() in FORBIDDEN_EXPORT_NAMES
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "commit_sha": _commit_sha(),
        "command": "uv run --frozen python scripts/security_export_scan.py",
        "production_files_scanned": len(production),
        "bundle_files_scanned": len(bundle_files),
        "forbidden_provider_matches": forbidden,
        "secret_matches": secrets,
        "forbidden_export_names": export_names,
        "status": "passed" if not forbidden and not secrets and not export_names else "failed",
        "artifact_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in bundle_files
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = scan()
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.output is None:
        print(output)
    else:
        args.output.write_text(output + "\n", encoding="utf-8")
        print(args.output)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
