"""Run the stable frontend gate and persist concise, non-secret evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = (
    ("npm", "ci"),
    ("npm", "run", "lint"),
    ("npm", "run", "typecheck"),
    ("npm", "test", "--", "--run"),
    ("npm", "run", "build"),
    ("npm", "run", "test:e2e"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is required")
    results: list[dict[str, object]] = []
    started_at = datetime.now(UTC)
    for command in COMMANDS:
        started = time.monotonic()
        completed = subprocess.run(  # noqa: S603 - executable is resolved from trusted PATH
            (npm, *command[1:]),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        results.append(
            {
                "command": " ".join(command),
                "returncode": completed.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
                "output_tail": output[-2000:],
            }
        )
        if completed.returncode != 0:
            break
    result = {
        "schema_version": 1,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "commands": results,
        "status": (
            "passed"
            if results
            and all(item["returncode"] == 0 for item in results)
            and len(results) == len(COMMANDS)
            else "failed"
        ),
        "limitation": (
            "Playwright mobile-only cases are explicitly skipped; desktop E2E "
            "assertions ran after Chromium installation."
        ),
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
