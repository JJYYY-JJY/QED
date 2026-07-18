#!/usr/bin/env python3
"""Run frozen reliability requests through QED's real verifier lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from qed.config import BudgetPolicy, QEDConfig
from qed.decision import decide_candidate
from qed.inputs import RunInput
from qed.runtime import create_codex_runtime
from qed.schemas import (
    CheckStatus,
    Evidence,
    Plan,
    PlanStep,
    ProofCandidate,
    Provenance,
    VerificationVerdict,
    canonical_json,
    sha256_text,
)
from qed.store import RunStage, RunStatus, RunStore, ThreadStatus
from qed.workflow import ResearchWorkflow, WorkflowExecutionError

Backend = Literal["sdk", "app-server"]


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _read_requests(path: Path) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            raise ValueError(f"request line {line_number} is blank")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"request line {line_number} is not an object")
        supplied_hash = value.get("request_sha256")
        content = {key: item for key, item in value.items() if key != "request_sha256"}
        if supplied_hash != _sha256(content):
            raise ValueError(f"request line {line_number} hash does not match")
        requests.append(value)
    if not requests:
        raise ValueError("request file is empty")
    identities = {
        (str(request["case_id"]), int(request["repetition"]))
        for request in requests
    }
    if len(identities) != len(requests):
        raise ValueError("request file contains duplicate case repetitions")
    return requests


def _provenance(
    *,
    source_id: str,
    runtime_version: str,
    model: str,
    captured_at: datetime,
) -> Provenance:
    return Provenance(
        source="benchmark_fixture",
        source_id=source_id,
        model=model,
        runtime_version=runtime_version,
        prompt_version="qed-reliability-adapter-v1",
        captured_at=captured_at,
    )


def _evidence_from_request(
    request: dict[str, Any],
    *,
    runtime_version: str,
    model: str,
    captured_at: datetime,
) -> tuple[Evidence, ...]:
    candidate_content = str(request["candidate_proof"])
    items = [
        Evidence(
            id="benchmark-candidate-bytes",
            kind="human_guidance",
            title="Frozen benchmark candidate bytes",
            content=candidate_content,
            content_sha256=sha256_text(candidate_content),
            provenance=_provenance(
                source_id="benchmark-fixture",
                runtime_version=runtime_version,
                model=model,
                captured_at=captured_at,
            ),
        )
    ]
    citation = request.get("citation")
    if not isinstance(citation, dict):
        return tuple(items)
    for raw in citation["evidence"]:
        content = str(raw["excerpt"])
        items.append(
            Evidence(
                id=str(raw["evidence_id"]),
                kind="source",
                title=str(raw["title"]),
                content=content,
                content_sha256=sha256_text(content),
                provenance=_provenance(
                    source_id="benchmark-fixture",
                    runtime_version=runtime_version,
                    model=model,
                    captured_at=captured_at,
                ),
                source_uri=str(raw["final_uri"]),
                citation=str(raw["title"]),
            )
        )
    return tuple(items)


def _seed_candidate(
    store: RunStore,
    workflow: ResearchWorkflow,
    request: dict[str, Any],
    *,
    config: QEDConfig,
    run_id: str,
    runtime_version: str,
    captured_at: datetime,
) -> tuple[ProofCandidate, tuple[Evidence, ...], str]:
    run_input = RunInput(
        problem=str(request["statement"]),
        prove_guidance=(
            "Reliability benchmark: evaluate only the sealed candidate supplied by "
            "the benchmark fixture."
        ),
        verification_rules=tuple(str(item) for item in request["verification_rules"]),
    )
    workflow.create_run(run_input, config, run_id=run_id)
    store.transition_run(run_id, RunStatus.RUNNING)
    store.transition_stage(run_id, RunStage.LITERATURE)
    evidence = _evidence_from_request(
        request,
        runtime_version=runtime_version,
        model=config.model,
        captured_at=captured_at,
    )
    store.add_evidence_batch(run_id, evidence)
    store.transition_stage(run_id, RunStage.PLANNING)
    plan = Plan(
        id=f"plan-{_sha256({'run_id': run_id, 'kind': 'benchmark'})[:20]}",
        problem_sha256=run_input.sha256,
        strategy="Verify the frozen benchmark candidate without rewriting it.",
        steps=(
            PlanStep(
                id="verify-frozen-candidate",
                statement="Check the supplied candidate against the frozen statement.",
                rationale="The benchmark measures false-PASS behavior on these exact bytes.",
                success_criteria=(
                    "Every configured rule receives structured verifier coverage.",
                ),
                evidence_ids=tuple(item.id for item in evidence),
                key_step=True,
            ),
        ),
        provenance=_provenance(
            source_id="benchmark-fixture",
            runtime_version=runtime_version,
            model=config.model,
            captured_at=captured_at,
        ),
        created_at=captured_at,
    )
    store.add_plan(run_id, plan)
    store.transition_stage(run_id, RunStage.PROVING)
    writer_external_id = f"benchmark-prover-{_sha256(run_id)[:24]}"
    writer_thread_id = f"benchmark-prover-{_sha256({'run_id': run_id})[:20]}"
    writer_provenance = _provenance(
        source_id=writer_thread_id,
        runtime_version=runtime_version,
        model=config.model,
        captured_at=captured_at,
    )
    store.add_thread(
        writer_thread_id,
        run_id=run_id,
        role="prover",
        model=config.model,
        provenance=writer_provenance,
        external_thread_id=writer_external_id,
    )
    store.transition_thread(writer_thread_id, ThreadStatus.COMPLETED)
    proof = str(request["candidate_proof"])
    candidate = ProofCandidate(
        id=f"candidate-{_sha256({'run_id': run_id, 'proof': proof})[:20]}",
        run_id=run_id,
        plan_id=plan.id,
        attempt=1,
        proof=proof,
        proof_sha256=sha256_text(proof),
        evidence_ids=tuple(item.id for item in evidence),
        provenance=writer_provenance,
        created_at=captured_at,
    )
    store.create_candidate(candidate, thread_id=writer_thread_id)
    store.seal_candidate(candidate.id)
    store.transition_stage(run_id, RunStage.VERIFICATION)
    return candidate, evidence, writer_external_id


def _usage(events: tuple[Any, ...], execution_seconds: float) -> dict[str, object]:
    usage_by_turn: dict[tuple[str, str], dict[str, int]] = {}
    search_queries = 0
    for event in events:
        if event.event_type == "runtime.token_usage":
            thread_id = event.payload.get("thread_id")
            turn_id = event.payload.get("turn_id")
            usage = event.payload.get("usage")
            if isinstance(thread_id, str) and isinstance(turn_id, str) and isinstance(
                usage, dict
            ):
                usage_by_turn[(thread_id, turn_id)] = {
                    key: int(usage.get(key, 0))
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    )
                }
        elif (
            event.event_type == "runtime.item_completed"
            and event.payload.get("counts_as_search_query") is True
        ):
            search_queries += 1
    return {
        key: sum(usage[key] for usage in usage_by_turn.values())
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
    } | {
        "search_queries": search_queries,
        "execution_seconds": execution_seconds,
        "cost_amount": None,
        "cost_currency": None,
        "cost_source": "unavailable",
    }


def _verdict(reports: tuple[Any, ...], passed: bool) -> str:
    if passed:
        return "PASS"
    verdicts = {report.verdict for report in reports}
    if VerificationVerdict.FAIL in verdicts:
        return "FAIL"
    return "UNCERTAIN"


def _citation_result(request: dict[str, Any], reports: tuple[Any, ...]) -> str:
    citation = request.get("citation")
    if not isinstance(citation, dict):
        return "NOT_APPLICABLE"
    citation_reports = tuple(report for report in reports if report.kind == "citation")
    if not citation_reports:
        return "UNCERTAIN"
    if any(report.verdict is VerificationVerdict.UNCERTAIN for report in citation_reports):
        return "UNCERTAIN"
    required = set(str(item) for item in citation["cited_evidence_ids"])
    supported = {
        support.evidence_id
        for report in citation_reports
        for check in report.checks
        if check.status is CheckStatus.PASS
        for support in check.citation_support
    }
    return "SUPPORTED" if required <= supported else "UNSUPPORTED"


async def _run(arguments: argparse.Namespace) -> int:
    if os.environ.get("QED_RUN_REAL_RELIABILITY_BENCHMARK") != "1":
        raise RuntimeError(
            "set QED_RUN_REAL_RELIABILITY_BENCHMARK=1 to spend model credentials"
        )
    requests = _read_requests(arguments.requests)
    data_root = arguments.data_root.resolve()
    if arguments.output.resolve() == arguments.requests.resolve():
        raise ValueError("output must not replace the blinded request file")
    if data_root == Path.home() or data_root == Path.home() / ".codex":
        raise ValueError("benchmark data root must not be a personal Codex root")
    data_root.mkdir(parents=True, exist_ok=True)
    runtime = create_codex_runtime(data_root / "codex-home")
    store = RunStore(data_root / "qed.sqlite3")
    workflow = ResearchWorkflow(
        store,
        runtime,
        runtime_version=runtime.runtime_version,
        export_root=data_root / "exports",
    )
    results: list[dict[str, Any]] = []
    try:
        for request in requests:
            started = datetime.now(UTC)
            started_monotonic = time.monotonic()
            run_id = (
                f"benchmark-{request['case_id']}-{int(request['repetition']):03d}-"
                f"{uuid4().hex[:8]}"
            )
            config = QEDConfig(
                model=arguments.model,
                backend=arguments.backend,
                budgets=BudgetPolicy(
                    proof_attempts=1,
                    plan_revisions=0,
                    strategy_rewrites=0,
                ),
            )
            candidate, evidence, writer_external_id = _seed_candidate(
                store,
                workflow,
                request,
                config=config,
                run_id=run_id,
                runtime_version=runtime.runtime_version,
                captured_at=started,
            )
            with suppress(WorkflowExecutionError):
                await workflow.execute(run_id)
            snapshot = store.snapshot(run_id)
            reports = tuple(
                record.report
                for record in snapshot.verifications
                if record.candidate_id == candidate.id
            )
            decision = decide_candidate(
                candidate,
                reports,
                prover_external_thread_id=writer_external_id,
                require_citation=True,
                required_evidence=evidence,
                required_rule_ids=tuple(
                    rule.id
                    for rule in store.get_run_input(run_id).frozen_verification_rules
                ),
            )
            finished = datetime.now(UTC)
            observed_backends = sorted(
                {
                    str(event.payload["backend"])
                    for event in snapshot.events
                    if event.event_type == "runtime.turn_started"
                    and isinstance(event.payload.get("backend"), str)
                }
            )
            result: dict[str, Any] = {
                "schema_version": 1,
                "run_id": run_id,
                "case_id": request["case_id"],
                "case_sha256": request["case_sha256"],
                "repetition": request["repetition"],
                "verdict": _verdict(reports, decision.passed),
                "citation_support": _citation_result(request, reports),
                "runtime": {
                    "adapter": "qed-verifier-lifecycle-v1",
                    "backend": ",".join(observed_backends) or arguments.backend,
                    "model": arguments.model,
                    "model_version": runtime.runtime_version,
                    "configuration_sha256": config.sha256,
                    "fixture": False,
                },
                "started_at": started.isoformat().replace("+00:00", "Z"),
                "finished_at": finished.isoformat().replace("+00:00", "Z"),
                "usage": _usage(
                    snapshot.events,
                    time.monotonic() - started_monotonic,
                ),
            }
            result["result_sha256"] = _sha256(result)
            results.append(result)
    finally:
        await workflow.close()
        await runtime.close()
        store.close()
    payload = "".join(
        json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
        for result in results
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {
                "adapter": "qed-verifier-lifecycle-v1",
                "output": str(arguments.output),
                "result_count": len(results),
                "results_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--backend", choices=("sdk", "app-server"), required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    return parser


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
