#!/usr/bin/env python3
"""Validate and evaluate the frozen QED reliability benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

BENCHMARK_ROOT = Path(__file__).resolve().parent
DEFAULT_CASES = BENCHMARK_ROOT / "cases.jsonl"
DEFAULT_LOCK = BENCHMARK_ROOT / "cases.lock.json"
CASE_SCHEMA = BENCHMARK_ROOT / "case.schema.json"
REQUEST_SCHEMA = BENCHMARK_ROOT / "request.schema.json"
RESULT_SCHEMA = BENCHMARK_ROOT / "result.schema.json"

CASE_CATEGORIES = frozenset(
    {
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
)
MUTATION_OPERATORS = frozenset(
    {
        "assumption_replacement",
        "key_step_deletion",
        "quantifier_swap",
        "symbol_change",
    }
)
CASE_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "title",
        "category",
        "statement",
        "candidate_proof",
        "verification_rules",
        "expected",
        "tags",
        "citation",
        "mutation",
    }
)
EXPECTED_KEYS = frozenset({"policy_verdict", "citation_support"})
MUTATION_KEYS = frozenset({"base_case_id", "operator", "description"})
CITATION_KEYS = frozenset({"claim", "cited_evidence_ids", "evidence"})
EVIDENCE_KEYS = frozenset(
    {
        "evidence_id",
        "title",
        "source_uri",
        "final_uri",
        "retrieved_at",
        "excerpt",
        "provenance",
        "content_sha256",
    }
)
LOCK_KEYS = frozenset(
    {
        "schema_version",
        "case_schema_sha256",
        "cases_file_sha256",
        "case_count",
        "case_hashes",
    }
)
RESULT_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "case_id",
        "case_sha256",
        "repetition",
        "verdict",
        "citation_support",
        "runtime",
        "started_at",
        "finished_at",
        "usage",
    }
)
RUNTIME_KEYS = frozenset(
    {
        "adapter",
        "backend",
        "model",
        "model_version",
        "configuration_sha256",
        "fixture",
    }
)
USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "search_queries",
        "execution_seconds",
        "cost_amount",
        "cost_currency",
        "cost_source",
    }
)


class BenchmarkValidationError(ValueError):
    """The benchmark input does not match its frozen schema or hashes."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_sha256(value: dict[str, Any], *, hash_field: str) -> str:
    content = {key: item for key, item in value.items() if key != hash_field}
    return _sha256_bytes(_canonical_json(content))


def _expect_exact_keys(
    value: dict[str, Any],
    expected: frozenset[str],
    *,
    location: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise BenchmarkValidationError(
            f"{location} has invalid keys; missing={missing}, unknown={unknown}"
        )


def _expect_nonempty_string(value: Any, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkValidationError(f"{location} must be a non-empty string")
    return value


def _expect_sha256(value: Any, *, location: str) -> str:
    text_value = _expect_nonempty_string(value, location=location)
    if len(text_value) != 64 or any(
        character not in "0123456789abcdef" for character in text_value
    ):
        raise BenchmarkValidationError(f"{location} must be a lowercase SHA-256")
    return text_value


def _expect_string_list(value: Any, *, location: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise BenchmarkValidationError(f"{location} must be a string list")
    for index, item in enumerate(value):
        _expect_nonempty_string(item, location=f"{location}[{index}]")
    if len(value) != len(set(value)):
        raise BenchmarkValidationError(f"{location} must not contain duplicates")
    return value


def _expect_nonnegative_integer(value: Any, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BenchmarkValidationError(f"{location} must be a nonnegative integer")
    return value


def _expect_nonnegative_number(value: Any, *, location: str) -> float | int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise BenchmarkValidationError(f"{location} must be a finite nonnegative number")
    return value


def _parse_timestamp(value: Any, *, location: str) -> datetime:
    text_value = _expect_nonempty_string(value, location=location)
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BenchmarkValidationError(f"{location} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise BenchmarkValidationError(f"{location} must include a UTC offset")
    return parsed


def _validate_evidence(value: Any, *, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BenchmarkValidationError(f"{location} must be an object")
    _expect_exact_keys(value, EVIDENCE_KEYS, location=location)
    for key in (
        "evidence_id",
        "title",
        "source_uri",
        "final_uri",
        "retrieved_at",
        "excerpt",
        "provenance",
    ):
        _expect_nonempty_string(value[key], location=f"{location}.{key}")
    if value["provenance"] != "benchmark_fixture":
        raise BenchmarkValidationError(f"{location}.provenance must be 'benchmark_fixture'")
    expected_hash = _sha256_bytes(value["excerpt"].encode())
    if value["content_sha256"] != expected_hash:
        raise BenchmarkValidationError(f"{location}.content_sha256 does not match excerpt")
    return value


def _validate_case(value: Any, *, line_number: int) -> dict[str, Any]:
    location = f"cases line {line_number}"
    if not isinstance(value, dict):
        raise BenchmarkValidationError(f"{location} must be an object")
    _expect_exact_keys(value, CASE_KEYS, location=location)
    if value["schema_version"] != 1:
        raise BenchmarkValidationError(f"{location}.schema_version must be 1")
    for key in ("case_id", "title", "statement", "candidate_proof"):
        _expect_nonempty_string(value[key], location=f"{location}.{key}")
    category = value["category"]
    if category not in CASE_CATEGORIES:
        raise BenchmarkValidationError(f"{location}.category is unknown: {category!r}")
    _expect_string_list(value["verification_rules"], location=f"{location}.verification_rules")
    _expect_string_list(value["tags"], location=f"{location}.tags", allow_empty=True)

    expected = value["expected"]
    if not isinstance(expected, dict):
        raise BenchmarkValidationError(f"{location}.expected must be an object")
    _expect_exact_keys(expected, EXPECTED_KEYS, location=f"{location}.expected")
    if expected["policy_verdict"] not in {"PASS", "NON_PASS"}:
        raise BenchmarkValidationError(
            f"{location}.expected.policy_verdict must be PASS or NON_PASS"
        )
    if expected["citation_support"] not in {
        "SUPPORTED",
        "UNSUPPORTED",
        "NOT_APPLICABLE",
    }:
        raise BenchmarkValidationError(f"{location}.expected.citation_support is invalid")

    mutation = value["mutation"]
    if category == "semantic_mutation":
        if not isinstance(mutation, dict):
            raise BenchmarkValidationError(f"{location}.mutation must be an object")
        _expect_exact_keys(mutation, MUTATION_KEYS, location=f"{location}.mutation")
        _expect_nonempty_string(
            mutation["base_case_id"], location=f"{location}.mutation.base_case_id"
        )
        _expect_nonempty_string(
            mutation["description"], location=f"{location}.mutation.description"
        )
        if mutation["operator"] not in MUTATION_OPERATORS:
            raise BenchmarkValidationError(
                f"{location}.mutation.operator is unknown: {mutation['operator']!r}"
            )
    elif mutation is not None:
        raise BenchmarkValidationError(
            f"{location}.mutation is only valid for semantic_mutation cases"
        )

    citation = value["citation"]
    if category.startswith("citation_"):
        if not isinstance(citation, dict):
            raise BenchmarkValidationError(f"{location}.citation must be an object")
        _expect_exact_keys(citation, CITATION_KEYS, location=f"{location}.citation")
        _expect_nonempty_string(citation["claim"], location=f"{location}.citation.claim")
        cited_ids = _expect_string_list(
            citation["cited_evidence_ids"],
            location=f"{location}.citation.cited_evidence_ids",
        )
        evidence_values = citation["evidence"]
        if not isinstance(evidence_values, list):
            raise BenchmarkValidationError(f"{location}.citation.evidence must be a list")
        evidence = [
            _validate_evidence(item, location=f"{location}.citation.evidence[{index}]")
            for index, item in enumerate(evidence_values)
        ]
        evidence_ids = [item["evidence_id"] for item in evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise BenchmarkValidationError(f"{location}.citation.evidence IDs must be unique")
        unknown_ids = sorted(set(cited_ids) - set(evidence_ids))
        if category == "citation_fabricated_metadata":
            if not unknown_ids:
                raise BenchmarkValidationError(
                    f"{location} must cite at least one unregistered evidence ID"
                )
        elif unknown_ids:
            raise BenchmarkValidationError(f"{location} cites unknown evidence IDs: {unknown_ids}")
    elif citation is not None:
        raise BenchmarkValidationError(f"{location}.citation is only valid for citation cases")

    return value


def _read_json(path: Path, *, location: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkValidationError(f"cannot read {location} {path}: {error}") from error


def _load_cases(cases_path: Path, lock_path: Path) -> list[dict[str, Any]]:
    try:
        case_bytes = cases_path.read_bytes()
    except OSError as error:
        raise BenchmarkValidationError(f"cannot read cases {cases_path}: {error}") from error

    lock = _read_json(lock_path, location="case lock")
    if not isinstance(lock, dict):
        raise BenchmarkValidationError("case lock must be an object")
    _expect_exact_keys(lock, LOCK_KEYS, location="case lock")
    if lock["schema_version"] != 1:
        raise BenchmarkValidationError("case lock schema_version must be 1")
    if lock["case_schema_sha256"] != _sha256_bytes(CASE_SCHEMA.read_bytes()):
        raise BenchmarkValidationError("case schema hash does not match case lock")
    if lock["cases_file_sha256"] != _sha256_bytes(case_bytes):
        raise BenchmarkValidationError("cases file hash does not match case lock")

    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(case_bytes.decode().splitlines(), start=1):
        if not line.strip():
            raise BenchmarkValidationError(f"cases line {line_number} is blank")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise BenchmarkValidationError(
                f"cases line {line_number} is invalid JSON: {error}"
            ) from error
        cases.append(_validate_case(value, line_number=line_number))

    case_ids = [case["case_id"] for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise BenchmarkValidationError("case IDs must be unique")
    case_hashes = {case["case_id"]: _sha256_bytes(_canonical_json(case)) for case in cases}
    if lock["case_count"] != len(cases):
        raise BenchmarkValidationError("case count does not match case lock")
    if lock["case_hashes"] != case_hashes:
        raise BenchmarkValidationError("case hashes do not match case lock")

    known_ids = set(case_ids)
    for case in cases:
        mutation = case["mutation"]
        if mutation is not None and mutation["base_case_id"] not in known_ids:
            raise BenchmarkValidationError(
                f"case {case['case_id']} names unknown mutation base {mutation['base_case_id']}"
            )
    return cases


def _validate_result(
    value: Any,
    *,
    line_number: int,
    cases_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    location = f"results line {line_number}"
    if not isinstance(value, dict):
        raise BenchmarkValidationError(f"{location} must be an object")
    actual_keys = frozenset(value)
    allowed_keys = RESULT_REQUIRED_KEYS | {"result_sha256"}
    missing = sorted(RESULT_REQUIRED_KEYS - actual_keys)
    unknown = sorted(actual_keys - allowed_keys)
    if missing or unknown:
        raise BenchmarkValidationError(
            f"{location} has invalid keys; missing={missing}, unknown={unknown}"
        )
    if value["schema_version"] != 1:
        raise BenchmarkValidationError(f"{location}.schema_version must be 1")
    _expect_nonempty_string(value["run_id"], location=f"{location}.run_id")
    case_id = _expect_nonempty_string(value["case_id"], location=f"{location}.case_id")
    case = cases_by_id.get(case_id)
    if case is None:
        raise BenchmarkValidationError(f"{location} names unknown case {case_id!r}")
    expected_case_hash = _sha256_bytes(_canonical_json(case))
    if value["case_sha256"] != expected_case_hash:
        raise BenchmarkValidationError(f"{location}.case_sha256 does not match case set")
    repetition = _expect_nonnegative_integer(value["repetition"], location=f"{location}.repetition")
    if repetition < 1:
        raise BenchmarkValidationError(f"{location}.repetition must be at least 1")
    if value["verdict"] not in {"PASS", "FAIL", "UNCERTAIN"}:
        raise BenchmarkValidationError(f"{location}.verdict is invalid")
    if value["citation_support"] not in {
        "SUPPORTED",
        "UNSUPPORTED",
        "UNCERTAIN",
        "NOT_APPLICABLE",
    }:
        raise BenchmarkValidationError(f"{location}.citation_support is invalid")
    if not case["category"].startswith("citation_") and (
        value["citation_support"] != "NOT_APPLICABLE"
    ):
        raise BenchmarkValidationError(
            f"{location}.citation_support must be NOT_APPLICABLE for a non-citation case"
        )

    runtime = value["runtime"]
    if not isinstance(runtime, dict):
        raise BenchmarkValidationError(f"{location}.runtime must be an object")
    _expect_exact_keys(runtime, RUNTIME_KEYS, location=f"{location}.runtime")
    for key in ("adapter", "backend", "model", "model_version"):
        _expect_nonempty_string(runtime[key], location=f"{location}.runtime.{key}")
    _expect_sha256(
        runtime["configuration_sha256"],
        location=f"{location}.runtime.configuration_sha256",
    )
    if not isinstance(runtime["fixture"], bool):
        raise BenchmarkValidationError(f"{location}.runtime.fixture must be boolean")

    started_at = _parse_timestamp(value["started_at"], location=f"{location}.started_at")
    finished_at = _parse_timestamp(value["finished_at"], location=f"{location}.finished_at")
    if finished_at < started_at:
        raise BenchmarkValidationError(f"{location}.finished_at precedes started_at")

    usage = value["usage"]
    if not isinstance(usage, dict):
        raise BenchmarkValidationError(f"{location}.usage must be an object")
    _expect_exact_keys(usage, USAGE_KEYS, location=f"{location}.usage")
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "search_queries",
    ):
        _expect_nonnegative_integer(usage[key], location=f"{location}.usage.{key}")
    _expect_nonnegative_number(
        usage["execution_seconds"], location=f"{location}.usage.execution_seconds"
    )
    cost_source = usage["cost_source"]
    if cost_source not in {"provider_reported", "operator_supplied", "unavailable"}:
        raise BenchmarkValidationError(f"{location}.usage.cost_source is invalid")
    if cost_source == "unavailable":
        if usage["cost_amount"] is not None or usage["cost_currency"] is not None:
            raise BenchmarkValidationError(
                f"{location}.usage unavailable cost must use null amount and currency"
            )
    else:
        _expect_nonnegative_number(usage["cost_amount"], location=f"{location}.usage.cost_amount")
        _expect_nonempty_string(usage["cost_currency"], location=f"{location}.usage.cost_currency")

    if runtime["fixture"]:
        if runtime["backend"] != "none" or runtime["model"] != "none":
            raise BenchmarkValidationError(
                f"{location} fixture runtime must use backend and model 'none'"
            )
        metered_values = [
            usage["input_tokens"],
            usage["cached_input_tokens"],
            usage["output_tokens"],
            usage["reasoning_output_tokens"],
            usage["search_queries"],
            usage["execution_seconds"],
        ]
        if any(metered_values) or cost_source != "unavailable":
            raise BenchmarkValidationError(
                f"{location} fixture runtime must not report model usage or cost"
            )
    elif runtime["backend"] == "none" or runtime["model"] == "none":
        raise BenchmarkValidationError(
            f"{location} non-fixture runtime must identify backend and model"
        )

    normalized = {key: item for key, item in value.items() if key != "result_sha256"}
    expected_hash = _sha256_bytes(_canonical_json(normalized))
    supplied_hash = value.get("result_sha256")
    if supplied_hash is not None and supplied_hash != expected_hash:
        raise BenchmarkValidationError(f"{location}.result_sha256 does not match result")
    normalized["result_sha256"] = expected_hash
    return normalized


def _load_results(
    results_path: Path,
    *,
    cases: list[dict[str, Any]],
    expected_repetitions: int,
) -> tuple[list[dict[str, Any]], bytes]:
    if expected_repetitions < 1:
        raise BenchmarkValidationError("expected repetitions must be at least 1")
    try:
        result_bytes = results_path.read_bytes()
    except OSError as error:
        raise BenchmarkValidationError(f"cannot read results {results_path}: {error}") from error
    cases_by_id = {case["case_id"]: case for case in cases}
    results: list[dict[str, Any]] = []
    for line_number, line in enumerate(result_bytes.decode().splitlines(), start=1):
        if not line.strip():
            raise BenchmarkValidationError(f"results line {line_number} is blank")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise BenchmarkValidationError(
                f"results line {line_number} is invalid JSON: {error}"
            ) from error
        results.append(
            _validate_result(
                value,
                line_number=line_number,
                cases_by_id=cases_by_id,
            )
        )

    observed_pairs = [(result["case_id"], result["repetition"]) for result in results]
    if len(observed_pairs) != len(set(observed_pairs)):
        raise BenchmarkValidationError("results contain a duplicate case repetition")
    run_ids = [result["run_id"] for result in results]
    if len(run_ids) != len(set(run_ids)):
        raise BenchmarkValidationError("results contain a duplicate run_id")
    expected_pairs = {
        (case["case_id"], repetition)
        for case in cases
        for repetition in range(1, expected_repetitions + 1)
    }
    actual_pairs = set(observed_pairs)
    if actual_pairs != expected_pairs:
        missing = sorted(expected_pairs - actual_pairs)
        unexpected = sorted(actual_pairs - expected_pairs)
        raise BenchmarkValidationError(
            "results do not cover the requested case repetitions; "
            f"missing={missing}, unexpected={unexpected}"
        )
    case_order = {case["case_id"]: index for index, case in enumerate(cases)}
    results.sort(key=lambda result: (result["repetition"], case_order[result["case_id"]]))
    return results, result_bytes


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _build_summary(
    *,
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    input_results_sha256: str,
    raw_results_sha256: str,
    cases_file_sha256: str,
    expected_repetitions: int,
) -> dict[str, Any]:
    cases_by_id = {case["case_id"]: case for case in cases}
    false_pass_population = [
        result
        for result in results
        if cases_by_id[result["case_id"]]["expected"]["policy_verdict"] == "NON_PASS"
    ]
    true_proof_population = [
        result
        for result in results
        if cases_by_id[result["case_id"]]["category"] == "known_true_correct_proof"
    ]
    mutation_population = [
        result
        for result in results
        if cases_by_id[result["case_id"]]["category"] == "semantic_mutation"
    ]
    predicted_supported = [
        result
        for result in results
        if cases_by_id[result["case_id"]]["category"].startswith("citation_")
        and result["citation_support"] == "SUPPORTED"
    ]
    metrics = {
        "false_pass_rate": _rate(
            sum(result["verdict"] == "PASS" for result in false_pass_population),
            len(false_pass_population),
        ),
        "true_proof_acceptance_rate": _rate(
            sum(result["verdict"] == "PASS" for result in true_proof_population),
            len(true_proof_population),
        ),
        "semantic_mutation_detection_rate": _rate(
            sum(result["verdict"] != "PASS" for result in mutation_population),
            len(mutation_population),
        ),
        "uncertain_rate": _rate(
            sum(result["verdict"] == "UNCERTAIN" for result in results),
            len(results),
        ),
        "citation_support_precision": _rate(
            sum(
                cases_by_id[result["case_id"]]["expected"]["citation_support"] == "SUPPORTED"
                for result in predicted_supported
            ),
            len(predicted_supported),
        ),
    }
    per_case = []
    for case in cases:
        case_verdicts = Counter(
            result["verdict"] for result in results if result["case_id"] == case["case_id"]
        )
        per_case.append(
            {
                "case_id": case["case_id"],
                "runs": sum(case_verdicts.values()),
                "varied": len(case_verdicts) > 1,
                "verdict_counts": {
                    verdict: case_verdicts.get(verdict, 0)
                    for verdict in ("PASS", "FAIL", "UNCERTAIN")
                },
            }
        )
    usage_totals = {
        key: sum(result["usage"][key] for result in results)
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "search_queries",
            "execution_seconds",
        )
    }
    cost_totals: dict[str, float] = {}
    for result in results:
        usage = result["usage"]
        if usage["cost_amount"] is not None:
            currency = usage["cost_currency"]
            cost_totals[currency] = cost_totals.get(currency, 0.0) + usage["cost_amount"]
    runtime_counts = Counter(
        (
            result["runtime"]["adapter"],
            result["runtime"]["backend"],
            result["runtime"]["model"],
            result["runtime"]["model_version"],
            result["runtime"]["configuration_sha256"],
            result["runtime"]["fixture"],
        )
        for result in results
    )
    runtime_configurations = [
        {
            "adapter": adapter,
            "backend": backend,
            "model": model,
            "model_version": model_version,
            "configuration_sha256": configuration_sha256,
            "fixture": fixture,
            "run_count": run_count,
        }
        for (
            adapter,
            backend,
            model,
            model_version,
            configuration_sha256,
            fixture,
        ), run_count in sorted(runtime_counts.items())
    ]
    summary: dict[str, Any] = {
        "schema_version": 1,
        "case_count": len(cases),
        "result_count": len(results),
        "expected_repetitions": expected_repetitions,
        "fixture_only": all(result["runtime"]["fixture"] for result in results),
        "cases_file_sha256": cases_file_sha256,
        "input_results_sha256": input_results_sha256,
        "raw_results_sha256": raw_results_sha256,
        "result_schema_sha256": _sha256_bytes(RESULT_SCHEMA.read_bytes()),
        "metrics": metrics,
        "verdict_counts": {
            verdict: sum(result["verdict"] == verdict for result in results)
            for verdict in ("PASS", "FAIL", "UNCERTAIN")
        },
        "usage_totals": usage_totals,
        "cost_totals": [
            {"currency": currency, "amount": amount}
            for currency, amount in sorted(cost_totals.items())
        ],
        "runtime_configurations": runtime_configurations,
        "per_case_variance": per_case,
    }
    summary["summary_sha256"] = _sha256_bytes(_canonical_json(summary))
    return summary


def _validate_command(arguments: argparse.Namespace) -> int:
    cases = _load_cases(arguments.cases, arguments.lock)
    validation = {
        "schema_version": 1,
        "case_count": len(cases),
        "categories": sorted({case["category"] for case in cases}),
        "semantic_mutation_operators": sorted(
            case["mutation"]["operator"] for case in cases if case["mutation"] is not None
        ),
        "cases_file_sha256": _sha256_bytes(arguments.cases.read_bytes()),
    }
    print(json.dumps(validation, sort_keys=True))
    return 0


def _prepare_command(arguments: argparse.Namespace) -> int:
    if arguments.repetitions < 1:
        raise BenchmarkValidationError("repetitions must be at least 1")
    if arguments.output.resolve() in {
        arguments.cases.resolve(),
        arguments.lock.resolve(),
        CASE_SCHEMA.resolve(),
        REQUEST_SCHEMA.resolve(),
    }:
        raise BenchmarkValidationError("output must not replace a benchmark input")

    cases = _load_cases(arguments.cases, arguments.lock)
    requests: list[dict[str, Any]] = []
    for repetition in range(1, arguments.repetitions + 1):
        for case in cases:
            request = {
                "schema_version": 1,
                "request_id": f"{case['case_id']}::{repetition:03d}",
                "case_id": case["case_id"],
                "case_sha256": _sha256_bytes(_canonical_json(case)),
                "repetition": repetition,
                "statement": case["statement"],
                "candidate_proof": case["candidate_proof"],
                "verification_rules": case["verification_rules"],
                "citation": case["citation"],
            }
            request["request_sha256"] = _content_sha256(request, hash_field="request_sha256")
            requests.append(request)

    payload = b"".join(_canonical_json(request) + b"\n" for request in requests)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(payload)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "case_count": len(cases),
                "repetitions": arguments.repetitions,
                "request_count": len(requests),
                "request_schema_sha256": _sha256_bytes(REQUEST_SCHEMA.read_bytes()),
                "requests_file_sha256": _sha256_bytes(payload),
                "output": str(arguments.output),
            },
            sort_keys=True,
        )
    )
    return 0


def _summarize_command(arguments: argparse.Namespace) -> int:
    cases = _load_cases(arguments.cases, arguments.lock)
    results, input_bytes = _load_results(
        arguments.results,
        cases=cases,
        expected_repetitions=arguments.expected_repetitions,
    )
    raw_path = arguments.output_dir / "raw-results.jsonl"
    summary_path = arguments.output_dir / "summary.json"
    if arguments.results.resolve() in {raw_path.resolve(), summary_path.resolve()}:
        raise BenchmarkValidationError("output directory must not replace input results")
    raw_bytes = b"".join(_canonical_json(result) + b"\n" for result in results)
    summary = _build_summary(
        cases=cases,
        results=results,
        input_results_sha256=_sha256_bytes(input_bytes),
        raw_results_sha256=_sha256_bytes(raw_bytes),
        cases_file_sha256=_sha256_bytes(arguments.cases.read_bytes()),
        expected_repetitions=arguments.expected_repetitions,
    )
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw_bytes)
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "result_count": len(results),
                "raw_results": str(raw_path),
                "summary": str(summary_path),
                "summary_sha256": summary["summary_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate the frozen case set")
    validate.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    validate.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    validate.set_defaults(handler=_validate_command)

    prepare = subparsers.add_parser(
        "prepare", help="write blinded execution requests for an external adapter"
    )
    prepare.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    prepare.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    prepare.add_argument("--repetitions", type=int, default=1)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.set_defaults(handler=_prepare_command)

    summarize = subparsers.add_parser(
        "summarize", help="validate per-run results and write metrics"
    )
    summarize.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    summarize.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    summarize.add_argument("--results", type=Path, required=True)
    summarize.add_argument("--expected-repetitions", type=int, required=True)
    summarize.add_argument("--output-dir", type=Path, required=True)
    summarize.set_defaults(handler=_summarize_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except BenchmarkValidationError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    sys.exit(main())
