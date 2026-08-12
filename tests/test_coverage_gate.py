from __future__ import annotations

import json

from scripts.check_coverage import check_report


def _report(tmp_path, *, statements: int, missing: int, branches: int, missing_branches: int):
    path = tmp_path / "coverage.json"
    summary = {
        "num_statements": statements,
        "missing_lines": missing,
        "num_branches": branches,
        "missing_branches": missing_branches,
    }
    path.write_text(
        json.dumps(
            {
                "totals": summary,
                "files": {
                    "src/qed/domain/state.py": {"summary": summary},
                    "src/qed/decision.py": {"summary": summary},
                    "src/qed/export.py": {"summary": summary},
                    "src/qed/persistence/migrations.py": {"summary": summary},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_coverage_gate_requires_both_repository_and_core_thresholds(tmp_path) -> None:
    passing = check_report(
        _report(tmp_path, statements=100, missing=0, branches=100, missing_branches=0)
    )
    assert passing["passed"] is True

    failing = _report(tmp_path, statements=100, missing=16, branches=100, missing_branches=16)
    assert check_report(failing)["passed"] is False
