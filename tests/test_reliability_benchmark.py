from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPOSITORY_ROOT / "benchmarks" / "reliability" / "run.py"
QED_ADAPTER = REPOSITORY_ROOT / "benchmarks" / "reliability" / "qed_adapter.py"
DEMO_RESULTS = REPOSITORY_ROOT / "benchmarks" / "reliability" / "fixtures" / "demo-results.jsonl"


def _run_benchmark(*arguments: str) -> subprocess.CompletedProcess[str]:
    # The test fixes both the interpreter and runner to repository-owned paths.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(RUNNER), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_qed_adapter(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("QED_RUN_REAL_RELIABILITY_BENCHMARK", None)
    return subprocess.run(  # noqa: S603
        [sys.executable, str(QED_ADAPTER), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_checked_in_case_set_is_locked_and_covers_required_failure_modes() -> None:
    completed = _run_benchmark("validate")

    assert completed.returncode == 0, completed.stderr
    validation = json.loads(completed.stdout)
    assert validation["case_count"] == 19
    assert set(validation["categories"]) == {
        "citation_correct",
        "citation_fabricated_metadata",
        "citation_incorrect",
        "citation_unsupported",
        "circular_reasoning",
        "correct_conclusion_invalid_proof",
        "domain_error",
        "false_statement",
        "inequality_direction",
        "invalid_limit_exchange",
        "known_true_correct_proof",
        "missing_assumption",
        "missing_boundary_case",
        "quantifier_error",
        "semantic_mutation",
        "unproved_key_lemma",
    }
    assert validation["semantic_mutation_operators"] == [
        "assumption_replacement",
        "key_step_deletion",
        "quantifier_swap",
        "symbol_change",
    ]


def test_prepare_emits_deterministic_blinded_requests_for_each_repetition(
    tmp_path: Path,
) -> None:
    output = tmp_path / "requests.jsonl"

    completed = _run_benchmark(
        "prepare",
        "--repetitions",
        "3",
        "--output",
        str(output),
    )

    assert completed.returncode == 0, completed.stderr
    requests = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(requests) == 57
    assert requests[0]["request_id"] == "true-odd-sum-induction::001"
    assert requests[0]["repetition"] == 1
    assert requests[19]["request_id"] == "true-odd-sum-induction::002"
    assert requests[-1]["request_id"] == "citation-fabricated-evidence-id::003"
    assert len(requests[0]["case_sha256"]) == 64
    assert len(requests[0]["request_sha256"]) == 64
    assert "expected" not in requests[0]
    assert "category" not in requests[0]
    preparation = json.loads(completed.stdout)
    assert preparation["request_count"] == 57
    assert len(preparation["request_schema_sha256"]) == 64


def test_qed_adapter_is_explicitly_opt_in_before_runtime_construction(
    tmp_path: Path,
) -> None:
    requests = tmp_path / "requests.jsonl"
    prepared = _run_benchmark(
        "prepare",
        "--repetitions",
        "1",
        "--output",
        str(requests),
    )
    assert prepared.returncode == 0, prepared.stderr

    completed = _run_qed_adapter(
        "--requests",
        str(requests),
        "--output",
        str(tmp_path / "results.jsonl"),
        "--data-root",
        str(tmp_path / "managed"),
        "--backend",
        "sdk",
    )

    assert completed.returncode != 0
    assert "QED_RUN_REAL_RELIABILITY_BENCHMARK=1" in completed.stderr
    assert not (tmp_path / "managed").exists()


def test_summarize_writes_raw_resource_usage_and_five_reliability_metrics(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "evaluation"

    completed = _run_benchmark(
        "summarize",
        "--results",
        str(DEMO_RESULTS),
        "--expected-repetitions",
        "1",
        "--output-dir",
        str(output_dir),
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["fixture_only"] is True
    assert summary["runtime_configurations"] == [
        {
            "adapter": "checked-in-demo-fixture",
            "backend": "none",
            "configuration_sha256": (
                "c907dd2162f78f39422ebf135fe52f0dda8eb6c0a01f1bcd9badac5de5d19f84"
            ),
            "fixture": True,
            "model": "none",
            "model_version": "not-applicable",
            "run_count": 19,
        }
    ]
    assert summary["metrics"] == {
        "citation_support_precision": {
            "denominator": 2,
            "numerator": 1,
            "value": 0.5,
        },
        "false_pass_rate": {
            "denominator": 17,
            "numerator": 2,
            "value": 2 / 17,
        },
        "semantic_mutation_detection_rate": {
            "denominator": 4,
            "numerator": 3,
            "value": 0.75,
        },
        "true_proof_acceptance_rate": {
            "denominator": 1,
            "numerator": 1,
            "value": 1.0,
        },
        "uncertain_rate": {
            "denominator": 19,
            "numerator": 1,
            "value": 1 / 19,
        },
    }
    raw_results = [
        json.loads(line)
        for line in (output_dir / "raw-results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(raw_results) == 19
    assert len(raw_results[0]["result_sha256"]) == 64
    assert raw_results[0]["usage"] == {
        "cached_input_tokens": 0,
        "cost_amount": None,
        "cost_currency": None,
        "cost_source": "unavailable",
        "execution_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "search_queries": 0,
    }


def test_multiple_repetitions_expose_per_case_verdict_variance(
    tmp_path: Path,
) -> None:
    first_repetition = [
        json.loads(line) for line in DEMO_RESULTS.read_text(encoding="utf-8").splitlines()
    ]
    second_repetition = json.loads(json.dumps(first_repetition))
    for result in second_repetition:
        result["run_id"] = result["run_id"].replace("-001", "-002")
        result["repetition"] = 2
    second_repetition[0]["verdict"] = "UNCERTAIN"
    results_path = tmp_path / "two-repetitions.jsonl"
    results_path.write_text(
        "".join(
            json.dumps(result, separators=(",", ":")) + "\n"
            for result in first_repetition + second_repetition
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "evaluation"

    completed = _run_benchmark(
        "summarize",
        "--results",
        str(results_path),
        "--expected-repetitions",
        "2",
        "--output-dir",
        str(output_dir),
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    by_case = {item["case_id"]: item for item in summary["per_case_variance"]}
    assert by_case["true-odd-sum-induction"]["varied"] is True
    assert by_case["true-odd-sum-induction"]["verdict_counts"] == {
        "FAIL": 0,
        "PASS": 1,
        "UNCERTAIN": 1,
    }
    assert by_case["false-every-prime-odd"]["varied"] is False


def test_non_fixture_result_must_identify_a_real_backend_and_model(
    tmp_path: Path,
) -> None:
    results = [json.loads(line) for line in DEMO_RESULTS.read_text(encoding="utf-8").splitlines()]
    results[0]["runtime"]["fixture"] = False
    results_path = tmp_path / "ambiguous-runtime.jsonl"
    results_path.write_text(
        "".join(json.dumps(result, separators=(",", ":")) + "\n" for result in results),
        encoding="utf-8",
    )

    completed = _run_benchmark(
        "summarize",
        "--results",
        str(results_path),
        "--expected-repetitions",
        "1",
        "--output-dir",
        str(tmp_path / "evaluation"),
    )

    assert completed.returncode == 2
    assert "non-fixture runtime must identify backend and model" in completed.stderr


def test_case_set_tampering_is_rejected_before_requests_are_prepared(
    tmp_path: Path,
) -> None:
    cases = (REPOSITORY_ROOT / "benchmarks" / "reliability" / "cases.jsonl").read_text(
        encoding="utf-8"
    )
    tampered_cases = tmp_path / "cases.jsonl"
    tampered_cases.write_text(
        cases.replace("Every prime number is odd.", "Every prime number is even."),
        encoding="utf-8",
    )

    completed = _run_benchmark("validate", "--cases", str(tampered_cases))

    assert completed.returncode == 2
    assert "cases file hash does not match case lock" in completed.stderr


def test_result_with_wrong_case_hash_is_rejected(
    tmp_path: Path,
) -> None:
    results = [json.loads(line) for line in DEMO_RESULTS.read_text(encoding="utf-8").splitlines()]
    results[0]["case_sha256"] = "0" * 64
    results_path = tmp_path / "wrong-case-hash.jsonl"
    results_path.write_text(
        "".join(json.dumps(result, separators=(",", ":")) + "\n" for result in results),
        encoding="utf-8",
    )

    completed = _run_benchmark(
        "summarize",
        "--results",
        str(results_path),
        "--expected-repetitions",
        "1",
        "--output-dir",
        str(tmp_path / "evaluation"),
    )

    assert completed.returncode == 2
    assert "case_sha256 does not match case set" in completed.stderr
