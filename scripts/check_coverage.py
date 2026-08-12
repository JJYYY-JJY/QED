"""Enforce the stable line/branch coverage contract from coverage.py JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CORE_SUFFIXES = (
    "src/qed/domain/state.py",
    "src/qed/decision.py",
    "src/qed/export.py",
    "src/qed/persistence/migrations.py",
)


def _percentage(summary: dict[str, Any], total: str, missing: str) -> float:
    count = int(summary.get(total, 0))
    missed = int(summary.get(missing, 0))
    return 100.0 if count == 0 else 100.0 * (count - missed) / count


def _aggregate(files: dict[str, Any], suffixes: tuple[str, ...]) -> dict[str, int]:
    selected = [
        value["summary"]
        for path, value in files.items()
        if any(path.endswith(suffix) for suffix in suffixes)
    ]
    if not selected:
        raise ValueError("coverage report contains no selected core files")
    return {
        "num_statements": sum(int(item["num_statements"]) for item in selected),
        "missing_lines": sum(int(item["missing_lines"]) for item in selected),
        "num_branches": sum(int(item["num_branches"]) for item in selected),
        "missing_branches": sum(int(item["missing_branches"]) for item in selected),
    }


def check_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    files = report.get("files")
    total = report.get("totals")
    if not isinstance(files, dict) or not isinstance(total, dict):
        raise ValueError("coverage JSON must contain files and totals")
    core = _aggregate(files, CORE_SUFFIXES)
    result = {
        "repository": {
            "line_percent": _percentage(total, "num_statements", "missing_lines"),
            "branch_percent": _percentage(total, "num_branches", "missing_branches"),
            "required_line_percent": 90.0,
            "required_branch_percent": 85.0,
        },
        "core": {
            "line_percent": _percentage(core, "num_statements", "missing_lines"),
            "branch_percent": _percentage(core, "num_branches", "missing_branches"),
            "required_line_percent": 95.0,
            "required_branch_percent": 90.0,
        },
    }
    result["passed"] = all(
        (
            result["repository"]["line_percent"] >= 90.0,
            result["repository"]["branch_percent"] >= 85.0,
            result["core"]["line_percent"] >= 95.0,
            result["core"]["branch_percent"] >= 90.0,
        )
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path, nargs="?", default=Path("coverage.json"))
    args = parser.parse_args()
    try:
        result = check_report(args.report)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
