from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from qed.config import BudgetPolicy, ParallelismPolicy, QEDConfig
from qed.inputs import RunInput
from qed.runtime import (
    CapabilityRequest,
    FreshThread,
    ItemCompleted,
    RunRequest,
    RuntimeBackend,
    RuntimeCapabilities,
    RuntimeErrorEvent,
    RuntimeEvent,
    RuntimePreference,
    ThreadStarted,
    TokenUsage,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
)
from qed.schemas import canonical_sha256
from qed.store import (
    ConflictError,
    RunStage,
    RunStatus,
    RunStore,
    ThreadRole,
    ThreadStatus,
)
from qed.workflow import ResearchWorkflow, WorkflowExecutionError


class ScriptedRuntime:
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]],
        *,
        synchronize_verifiers: bool = False,
        synchronize_provers: int = 0,
        block_verification_number: int | None = None,
        probe_error: str | None = None,
        literature_queries: int = 0,
        block_title: str | None = None,
        constant_turn_id: bool = False,
    ) -> None:
        self.responses = {title: deque(values) for title, values in responses.items()}
        self.counts: defaultdict[str, int] = defaultdict(int)
        self.requests: list[RunRequest] = []
        self.probes: list[CapabilityRequest] = []
        self.interruptions: list[TurnRef] = []
        self.synchronize_verifiers = synchronize_verifiers
        self.verifiers_in_flight = 0
        self.max_verifiers_in_flight = 0
        self._verifier_barrier = asyncio.Event()
        self.synchronize_provers = synchronize_provers
        self.provers_in_flight = 0
        self.max_provers_in_flight = 0
        self._prover_barrier = asyncio.Event()
        self.block_verification_number = block_verification_number
        self.probe_error = probe_error
        self.literature_queries = literature_queries
        self.block_title = block_title
        self.constant_turn_id = constant_turn_id
        self.turn_reached = asyncio.Event()
        self._blocked_turn: TurnRef | None = None
        self._release_blocked_turn = asyncio.Event()

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        self.probes.append(request)
        if self.probe_error is not None:
            raise RuntimeError(self.probe_error)
        return RuntimeCapabilities(
            model=request.model,
            advertised_efforts=("low", "high", "ultra"),
            default_effort="low",
            selected_effort="ultra" if request.proactive else "low",
            multi_agent=True,
            proactive_multi_agent=request.proactive,
        )

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        title = str(request.output_schema["title"])
        self.counts[title] += 1
        number = self.counts[title]
        thread_id = f"{title.lower()}-{number}-thread"
        turn = TurnRef(
            thread_id=thread_id,
            turn_id=(
                "shared-turn"
                if self.constant_turn_id
                else f"{title.lower()}-{number}-turn"
            ),
            backend=RuntimeBackend.MOCK,
        )
        yield ThreadStarted(thread_id=thread_id, backend=RuntimeBackend.MOCK)
        yield TurnStarted(turn=turn)
        yield TokenUsageUpdated(
            thread_id=thread_id,
            turn_id=turn.turn_id,
            usage=TokenUsage(
                input_tokens=10,
                output_tokens=5,
                cached_input_tokens=2,
                reasoning_output_tokens=3,
            ),
        )
        if title == "EvidenceBatch":
            for query_number in range(self.literature_queries):
                yield ItemCompleted(
                    thread_id=thread_id,
                    turn_id=turn.turn_id,
                    item_id=f"search-{query_number}",
                    item_type="webSearch",
                    payload={
                        "id": f"search-{query_number}",
                        "type": "webSearch",
                        "action": {
                            "type": "search",
                            "query": f"query {query_number}",
                        },
                    },
                )
        if title == self.block_title:
            self._blocked_turn = turn
            self.turn_reached.set()
            await self._release_blocked_turn.wait()
            yield TurnCompleted(turn=turn, status="interrupted")
            return
        if title == "VerificationDraft" and self.synchronize_verifiers:
            self.verifiers_in_flight += 1
            self.max_verifiers_in_flight = max(
                self.max_verifiers_in_flight, self.verifiers_in_flight
            )
            if self.verifiers_in_flight == 2:
                self._verifier_barrier.set()
            await self._verifier_barrier.wait()
        if title == "VerificationDraft" and number == self.block_verification_number:
            self._blocked_turn = turn
            self.turn_reached.set()
            await self._release_blocked_turn.wait()
            yield TurnCompleted(turn=turn, status="interrupted")
            return
        if title == "ProofDraft" and self.synchronize_provers:
            self.provers_in_flight += 1
            self.max_provers_in_flight = max(
                self.max_provers_in_flight, self.provers_in_flight
            )
            if self.provers_in_flight == self.synchronize_provers:
                self._prover_barrier.set()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._prover_barrier.wait(), timeout=0.05)
        try:
            response = self.responses[title].popleft()
            if response.get("__eof__") is True:
                return
            if "__runtime_error__" in response:
                yield RuntimeErrorEvent(
                    message=str(response["__runtime_error__"]),
                    retryable=bool(response.get("retryable", True)),
                )
                yield TurnCompleted(turn=turn, status="failed")
                return
            if "__protocol_error__" in response:
                raise ValueError(str(response["__protocol_error__"]))
            if "__runtime_warning__" in response:
                yield RuntimeErrorEvent(
                    message=str(response["__runtime_warning__"]),
                    retryable=True,
                )
                response = response["output"]
            if title == "VerificationDraft" and request.role.value == "citation":
                start = request.prompt.index(
                    '<frozen-input encoding="canonical-json">'
                )
                start = request.prompt.index("\n", start) + 1
                end = request.prompt.index("\n</frozen-input>", start)
                payload = json.loads(request.prompt[start:end])
                response = json.loads(json.dumps(response))
                response["checks"][0]["evidence_ids"] = [
                    item["id"] for item in payload["evidence"]
                ]
            yield TurnCompleted(
                turn=turn,
                status="completed",
                output=json.dumps(response),
            )
        finally:
            if title == "VerificationDraft" and self.synchronize_verifiers:
                self.verifiers_in_flight -= 1
            if title == "ProofDraft" and self.synchronize_provers:
                self.provers_in_flight -= 1

    async def interrupt(self, turn: TurnRef) -> None:
        self.interruptions.append(turn)
        if turn == self._blocked_turn:
            self._release_blocked_turn.set()

    async def close(self) -> None:
        return None


class BlockingProbeRuntime(ScriptedRuntime):
    def __init__(self) -> None:
        super().__init__(passing_responses())
        self.probe_reached = asyncio.Event()
        self.probe_cancelled = False
        self.release_probe = asyncio.Event()

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        self.probes.append(request)
        self.probe_reached.set()
        try:
            await self.release_probe.wait()
        except asyncio.CancelledError:
            self.probe_cancelled = True
            raise
        return await super().probe(request)


def config() -> QEDConfig:
    return QEDConfig(
        parallelism=ParallelismPolicy(
            runs=1,
            proof_candidates=1,
            verifiers=2,
            proactive_multi_agent=True,
        ),
        budgets=BudgetPolicy(
            run_seconds=300,
            stage_seconds=60,
            max_tokens=10_000,
            proof_attempts=1,
            plan_revisions=0,
            strategy_rewrites=0,
            turn_retries=0,
        ),
    )


def passing_responses() -> dict[str, list[dict[str, Any]]]:
    return {
        "EvidenceBatch": [
            {
                "schema_version": 1,
                "items": [
                    {
                        "kind": "theorem",
                        "title": "Euclid IX.20",
                        "content": "Every finite list of primes omits another prime.",
                        "citation": "Euclid, Elements IX.20",
                    }
                ],
            }
        ],
        "PlanDraft": [
            {
                "schema_version": 1,
                "strategy": "Assume finitely many primes and construct a new divisor.",
                "steps": [
                    {
                        "id": "contradiction",
                        "statement": "A divisor of the product plus one is new.",
                        "rationale": "Each listed prime leaves remainder one.",
                        "success_criteria": ["The finite list is contradicted."],
                    }
                ],
            }
        ],
        "ProofDraft": [
            {
                "schema_version": 1,
                "proof": (
                    "Assume p_1,...,p_n list all primes. A prime divisor of "
                    "p_1...p_n+1 is not listed, a contradiction."
                ),
            }
        ],
        "VerificationDraft": [
            {
                "schema_version": 1,
                "checks": [
                    {
                        "id": "structure",
                        "category": "coverage",
                        "status": "pass",
                        "summary": "The argument reaches the stated target.",
                    }
                ],
            },
            {
                "schema_version": 1,
                "checks": [
                    {
                        "id": "detail",
                        "category": "logical-correctness",
                        "status": "pass",
                        "summary": "Each divisibility inference is valid.",
                    }
                ],
            },
            {
                "schema_version": 1,
                "checks": [
                    {
                        "id": "citation",
                        "category": "citation-integrity",
                        "status": "pass",
                        "summary": "The frozen evidence ledger supports the proof context.",
                    }
                ],
            },
        ],
        "AdjudicationDraft": [
            {
                "schema_version": 1,
                "outcome": "accept",
                "rationale": "Both independent verification reports pass.",
            }
        ],
    }


async def test_workflow_completes_with_sealed_independently_verified_candidate(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime(passing_responses())
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        created = workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        completed = await workflow.execute(created.id)
        snapshot = store.snapshot(created.id)
        runtime_resolution = store.get_execution_resolution(
            snapshot.execution_segments[0].id
        )
        manifest_artifact = next(
            artifact for artifact in snapshot.artifacts if artifact.kind == "manifest"
        )
        assert manifest_artifact.relative_path is not None
        exported_manifest = json.loads(
            (tmp_path / "exports" / manifest_artifact.relative_path).read_text(
                encoding="utf-8"
            )
        )

    assert completed.status is RunStatus.COMPLETED
    assert completed.stage is RunStage.COMPLETE
    assert runtime_resolution["model"] == config().model
    assert runtime_resolution["selected_effort"] == "ultra"
    assert any(
        event.event_type == "execution.runtime_resolved"
        for event in snapshot.events
    )
    assert exported_manifest["runtime_resolutions"][0]["resolution"] == runtime_resolution
    assert exported_manifest["execution_segments"][0]["runtime_resolution_sha256"] == (
        snapshot.execution_segments[0].runtime_resolution_sha256
    )
    assert exported_manifest["usage"] == {
        "cached_input_tokens": 14,
        "execution_seconds": exported_manifest["usage"]["execution_seconds"],
        "input_tokens": 70,
        "output_tokens": 35,
        "reasoning_output_tokens": 21,
        "search_queries": 0,
        "turns": 7,
    }
    assert len(snapshot.turn_inputs) == 7
    assert all(
        turn_input.payload_sha256 == canonical_sha256(turn_input.payload)
        for turn_input in snapshot.turn_inputs
    )
    manifest_turn_input_ids = {
        turn_input["id"] for turn_input in exported_manifest["turn_inputs"]
    }
    assert manifest_turn_input_ids == {
        turn["turn_input_id"] for turn in exported_manifest["turns"]
    }
    manifest_external_threads = {
        thread["external_thread_id"]
        for thread in exported_manifest["threads"]
        if thread["external_thread_id"] is not None
    }
    assert all(
        turn["thread_id"] in manifest_external_threads
        for turn in exported_manifest["turns"]
    )
    assert exported_manifest["findings"] == []
    assert len(snapshot.candidates) == 1
    assert snapshot.candidates[0].sealed_at is not None
    assert snapshot.candidates[0].candidate.evidence_ids == ()
    assert {record.kind for record in snapshot.verifications} == {
        "structural",
        "detailed",
        "citation",
    }
    verifier_threads = {record.thread_id for record in snapshot.verifications}
    assert len(verifier_threads) == 3
    assert all(
        thread.role is ThreadRole.VERIFIER
        and thread.status is ThreadStatus.COMPLETED
        and thread.parent_thread_id is None
        for thread in snapshot.threads
        if thread.id in verifier_threads
    )
    assert all(isinstance(request.thread, FreshThread) for request in runtime.requests)
    assert all(
        request.output_schema["additionalProperties"] is False for request in runtime.requests
    )
    verification_requests = tuple(
        request
        for request in runtime.requests
        if request.output_schema["title"] == "VerificationDraft"
    )
    assert len(verification_requests) == 3
    assert all('"plan":{' in request.prompt for request in verification_requests)
    assert all('"evidence":[{' in request.prompt for request in verification_requests)
    assert sum(request.role.value == "citation" for request in verification_requests) == 1
    assert any(thread.role is ThreadRole.ADJUDICATOR for thread in snapshot.threads)
    assert any(event.event_type == "runtime.token_usage" for event in snapshot.events)
    accepted = [output for output in snapshot.stage_outputs if output.kind == "accepted_candidate"]
    assert accepted[0].content["candidate_id"] == snapshot.candidates[0].id
    assert accepted[0].content["passed"] is True
    assert {artifact.kind for artifact in snapshot.artifacts} == {
        "manifest",
        "proof",
        "report",
    }


async def test_usage_identity_includes_thread_when_turn_ids_repeat(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(passing_responses(), constant_turn_id=True)
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-shared-turns",
        )
        await workflow.execute("run-shared-turns")
        snapshot = store.snapshot("run-shared-turns")
        artifact = next(item for item in snapshot.artifacts if item.kind == "manifest")
        assert artifact.relative_path is not None
        manifest = json.loads(
            (tmp_path / "exports" / artifact.relative_path).read_text(encoding="utf-8")
        )

    assert manifest["usage"]["turns"] == 7
    assert manifest["usage"]["input_tokens"] == 70
    assert manifest["usage"]["output_tokens"] == 35


async def test_schema_failure_marks_run_failed_without_creating_candidate(
    tmp_path: Path,
) -> None:
    responses = passing_responses()
    responses["ProofDraft"] = [{"schema_version": 1, "proof": ""}]
    runtime = ScriptedRuntime(responses)
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="ProofDraft"):
            await workflow.execute("run-1")

        failed = store.get_run("run-1")
        assert failed.status is RunStatus.FAILED
        assert failed.resumable is True
        assert store.list_candidates("run-1") == ()


async def test_failed_proof_output_consumes_durable_attempt_budget(tmp_path: Path) -> None:
    database = tmp_path / "qed.sqlite3"
    responses = passing_responses()
    responses["ProofDraft"] = [{"schema_version": 1, "proof": ""}]
    first_runtime = ScriptedRuntime(responses)
    with RunStore(database) as store:
        first = ResearchWorkflow(store, first_runtime, runtime_version="test-runtime")
        first.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        with pytest.raises(WorkflowExecutionError, match="ProofDraft"):
            await first.execute("run-1")
        assert store.get_run("run-1").proof_attempt_count == 1

    second_runtime = ScriptedRuntime(passing_responses())
    second_runtime.counts["ProofDraft"] = 1
    with RunStore(database) as reopened:
        second = ResearchWorkflow(reopened, second_runtime, runtime_version="test-runtime")
        with pytest.raises(WorkflowExecutionError, match="proof attempt budget"):
            await second.resume("run-1", idempotency_key="resume-exhausted-proof")

        assert reopened.list_candidates("run-1") == ()
        assert second_runtime.counts["ProofDraft"] == 1


async def test_capability_probe_failure_preserves_error_and_marks_run_resumable(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime(passing_responses(), probe_error="probe boom")
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="probe boom"):
            await workflow.execute("run-1")
        failed = store.get_run("run-1")

    assert failed.status is RunStatus.FAILED
    assert failed.resumable is True


async def test_cancellation_during_capability_probe_is_fenced_and_resumable(
    tmp_path: Path,
) -> None:
    runtime = BlockingProbeRuntime()
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        running = asyncio.create_task(workflow.execute("run-1"))
        await asyncio.wait_for(runtime.probe_reached.wait(), timeout=1)

        cancelled = await asyncio.wait_for(workflow.cancel("run-1"), timeout=1)
        assert await asyncio.wait_for(running, timeout=1) == cancelled
        snapshot = store.snapshot("run-1")

    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.resumable is True
    assert runtime.probe_cancelled is True
    assert len(snapshot.execution_segments) == 1
    assert snapshot.execution_segments[0].released_at is not None
    assert snapshot.execution_segments[0].runtime_resolution_sha256 is None
    assert all(
        output.kind != "runtime_capabilities" for output in snapshot.stage_outputs
    )


async def test_capability_probe_obeys_stage_timeout_and_cancels_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BlockingProbeRuntime()
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="runtime-v1")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        monkeypatch.setattr(workflow, "_remaining_stage_seconds", lambda _run_id: 0.01)

        with pytest.raises(WorkflowExecutionError, match="capability probe time budget"):
            await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

    assert runtime.probe_cancelled is True
    assert snapshot.run.status is RunStatus.FAILED
    assert snapshot.execution_segments[0].released_at is not None
    assert snapshot.execution_segments[0].runtime_resolution_sha256 is None


async def test_cancelled_unresolved_probe_resumes_with_new_runtime_provenance(
    tmp_path: Path,
) -> None:
    database = tmp_path / "qed.sqlite3"
    first_runtime = BlockingProbeRuntime()
    with RunStore(database) as store:
        first = ResearchWorkflow(store, first_runtime, runtime_version="runtime-v1")
        first.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        running = asyncio.create_task(first.execute("run-1"))
        await asyncio.wait_for(first_runtime.probe_reached.wait(), timeout=1)
        cancelled = await asyncio.wait_for(first.cancel("run-1"), timeout=1)
        assert await asyncio.wait_for(running, timeout=1) == cancelled

    second_runtime = ScriptedRuntime(passing_responses())
    with RunStore(database) as reopened:
        second = ResearchWorkflow(
            reopened,
            second_runtime,
            runtime_version="runtime-v2",
        )
        completed = await second.resume("run-1", idempotency_key="resume-runtime-v2")
        snapshot = reopened.snapshot("run-1")
        manifest_artifact = next(
            artifact for artifact in snapshot.artifacts if artifact.kind == "manifest"
        )
        assert manifest_artifact.relative_path is not None
        manifest = json.loads(
            (tmp_path / "exports" / manifest_artifact.relative_path).read_text(
                encoding="utf-8"
            )
        )

    assert completed.status is RunStatus.COMPLETED
    assert [segment["runtime_version"] for segment in manifest["execution_segments"]] == [
        "runtime-v1",
        "runtime-v2",
    ]
    assert manifest["execution_segments"][0]["runtime_resolution_sha256"] is None
    assert manifest["execution_segments"][1]["runtime_resolution_sha256"] is not None
    assert len(manifest["runtime_resolutions"]) == 1
    assert manifest["runtime_resolutions"][0]["segment_id"] == (
        manifest["execution_segments"][1]["id"]
    )


async def test_resume_records_observed_capability_drift_without_changing_controls(
    tmp_path: Path,
) -> None:
    class DriftedRuntime(ScriptedRuntime):
        async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
            self.probes.append(request)
            return RuntimeCapabilities(
                model=request.model,
                advertised_efforts=("high", "ultra"),
                default_effort="high",
                selected_effort="ultra",
                multi_agent=True,
                proactive_multi_agent=True,
            )

    database = tmp_path / "qed.sqlite3"
    resumable_config = config().model_copy(
        update={"budgets": config().budgets.model_copy(update={"proof_attempts": 2})}
    )
    first_runtime = ScriptedRuntime(passing_responses(), block_title="ProofDraft")
    with RunStore(database) as store:
        first = ResearchWorkflow(store, first_runtime, runtime_version="runtime-v1")
        first.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            resumable_config,
            run_id="run-capability-drift",
        )
        running = asyncio.create_task(first.execute("run-capability-drift"))
        await asyncio.wait_for(first_runtime.turn_reached.wait(), timeout=1)
        await first.cancel("run-capability-drift")
        await running

    second_runtime = DriftedRuntime(passing_responses())
    second_runtime.counts["ProofDraft"] = 1
    with RunStore(database) as reopened:
        second = ResearchWorkflow(
            reopened,
            second_runtime,
            runtime_version="runtime-v2",
        )
        completed = await second.resume(
            "run-capability-drift",
            idempotency_key="resume-capability-drift",
        )
        snapshot = reopened.snapshot("run-capability-drift")
        resolution_by_segment = {
            item.segment_id: item.resolution for item in snapshot.runtime_resolutions
        }

    assert completed.status is RunStatus.COMPLETED
    first_segment, second_segment = snapshot.execution_segments
    first_resolution = resolution_by_segment[first_segment.id]
    second_resolution = resolution_by_segment[second_segment.id]
    assert isinstance(first_resolution, dict)
    assert isinstance(second_resolution, dict)
    assert first_resolution["advertised_efforts"] == [
        "low",
        "high",
        "ultra",
    ]
    assert second_resolution["advertised_efforts"] == [
        "high",
        "ultra",
    ]
    assert all(request.effort == "ultra" for request in second_runtime.requests)
    assert all(request.proactive is True for request in second_runtime.requests)


async def test_independent_verifiers_use_configured_parallelism(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(passing_responses(), synchronize_verifiers=True)
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        completed = await asyncio.wait_for(workflow.execute("run-1"), timeout=1)

    assert completed.status is RunStatus.COMPLETED
    assert runtime.max_verifiers_in_flight == 2


async def test_parallel_verifier_failure_cancels_and_interrupts_sibling(
    tmp_path: Path,
) -> None:
    responses = passing_responses()
    responses["VerificationDraft"][0] = {"schema_version": 1, "checks": []}
    runtime = ScriptedRuntime(
        responses,
        synchronize_verifiers=True,
        block_verification_number=2,
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="VerificationDraft"):
            await workflow.execute("run-1")
        await asyncio.sleep(0)
        snapshot = store.snapshot("run-1")

    assert runtime.interruptions
    assert all(thread.status is not ThreadStatus.ACTIVE for thread in snapshot.threads)


async def test_generates_configured_proof_candidates_concurrently(tmp_path: Path) -> None:
    responses = passing_responses()
    proof = responses["ProofDraft"][0]
    responses["ProofDraft"] = [
        {**proof, "proof": f"{proof['proof']} Variant {number}."}
        for number in range(1, 4)
    ]
    responses["VerificationDraft"] *= 3
    runtime = ScriptedRuntime(responses, synchronize_provers=3)
    candidate_config = config().model_copy(
        update={
            "parallelism": config().parallelism.model_copy(
                update={"proof_candidates": 3}
            ),
            "budgets": config().budgets.model_copy(update={"proof_attempts": 3}),
        }
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            candidate_config,
            run_id="run-1",
        )

        completed = await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert runtime.max_provers_in_flight == 3
    assert [record.attempt for record in snapshot.candidates] == [1, 2, 3]
    assert len(snapshot.verifications) == 9
    accepted = next(
        output for output in snapshot.stage_outputs if output.kind == "accepted_candidate"
    )
    assert accepted.content["candidate_id"] == snapshot.candidates[0].id


async def test_adjudication_can_request_a_bounded_fresh_proof_cycle(
    tmp_path: Path,
) -> None:
    responses = passing_responses()
    proof = responses["ProofDraft"][0]
    responses["ProofDraft"] = [
        proof,
        {**proof, "proof": f"{proof['proof']} Revised after adjudication."},
    ]
    responses["VerificationDraft"] *= 2
    responses["AdjudicationDraft"] = [
        {
            "schema_version": 1,
            "outcome": "revise_proof",
            "rationale": "A clearer proof presentation is required.",
        },
        {
            "schema_version": 1,
            "outcome": "accept",
            "rationale": "The revised proof passes both checks.",
        },
    ]
    runtime = ScriptedRuntime(responses)
    revision_config = config().model_copy(
        update={"budgets": config().budgets.model_copy(update={"proof_attempts": 2})}
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            revision_config,
            run_id="run-1",
        )

        completed = await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert [candidate.attempt for candidate in snapshot.candidates] == [1, 2]
    assert [item.outcome for item in snapshot.adjudications] == [
        "revise_proof",
        "accept",
    ]
    assert len(snapshot.plans) == 1
    assert runtime.counts["ProofDraft"] == 2


async def test_erroneous_accept_is_not_persisted_and_resume_uses_fresh_retry(
    tmp_path: Path,
) -> None:
    responses = passing_responses()
    passing_verifications = json.loads(json.dumps(responses["VerificationDraft"]))
    responses["VerificationDraft"][0]["checks"][0]["status"] = "fail"
    responses["VerificationDraft"][0]["findings"] = [
        {
            "id": "missing-case",
            "check_id": "structure",
            "severity": "major",
            "summary": "The proof omits a required case.",
            "detail": "The stated argument does not cover the target in full.",
        }
    ]
    responses["VerificationDraft"] += passing_verifications
    proof = responses["ProofDraft"][0]
    responses["ProofDraft"] = [
        proof,
        {**proof, "proof": f"{proof['proof']} Revised after failed verification."},
    ]
    responses["AdjudicationDraft"] = [
        {
            "schema_version": 1,
            "outcome": "accept",
            "rationale": "The failing report can be ignored.",
        },
        {
            "schema_version": 1,
            "outcome": "revise_proof",
            "rationale": "The failed verification requires a fresh proof cycle.",
        },
        {
            "schema_version": 1,
            "outcome": "accept",
            "rationale": "The revised candidate passes every required report.",
        },
    ]
    runtime = ScriptedRuntime(responses)
    recovery_config = config().model_copy(
        update={
            "budgets": config().budgets.model_copy(
                update={"proof_attempts": 2, "turn_retries": 1}
            )
        }
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            recovery_config,
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="did not pass"):
            await workflow.execute("run-1")
        failed = store.snapshot("run-1")

        assert failed.run.status is RunStatus.FAILED
        assert failed.adjudications == ()

        completed = await workflow.resume(
            "run-1",
            idempotency_key="resume-after-erroneous-accept",
        )
        snapshot = store.snapshot("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert [item.outcome for item in snapshot.adjudications] == [
        "revise_proof",
        "accept",
    ]
    assert runtime.counts["AdjudicationDraft"] == 3
    adjudicator_threads = [
        thread for thread in snapshot.threads if thread.role is ThreadRole.ADJUDICATOR
    ]
    assert len({thread.external_thread_id for thread in adjudicator_threads}) == 3


@pytest.mark.parametrize(
    ("outcome", "budget_field", "expected_evidence"),
    [
        ("revise_plan", "plan_revisions", 1),
        ("rewrite", "strategy_rewrites", 2),
    ],
)
async def test_adjudication_revision_cycles_regenerate_typed_inputs(
    tmp_path: Path,
    outcome: str,
    budget_field: str,
    expected_evidence: int,
) -> None:
    responses = passing_responses()
    proof = responses["ProofDraft"][0]
    responses["ProofDraft"] = [
        proof,
        {**proof, "proof": f"{proof['proof']} Revised through {outcome}."},
    ]
    plan = responses["PlanDraft"][0]
    responses["PlanDraft"] = [
        plan,
        {**plan, "strategy": f"{plan['strategy']} Revised through {outcome}."},
    ]
    if outcome == "rewrite":
        evidence = responses["EvidenceBatch"][0]
        responses["EvidenceBatch"] = [
            evidence,
            {
                "schema_version": 1,
                "items": [
                    {
                        "kind": "theorem",
                        "title": "A rewritten literature strategy",
                        "content": "A second frozen evidence item.",
                        "citation": "A reproducible source",
                    }
                ],
            },
        ]
    responses["VerificationDraft"] *= 2
    responses["AdjudicationDraft"] = [
        {
            "schema_version": 1,
            "outcome": outcome,
            "rationale": f"The adjudicator requested {outcome}.",
        },
        {
            "schema_version": 1,
            "outcome": "accept",
            "rationale": "The revised cycle now passes.",
        },
    ]
    runtime = ScriptedRuntime(responses)
    budgets = config().budgets.model_copy(
        update={"proof_attempts": 2, budget_field: 1}
    )
    revision_config = config().model_copy(update={"budgets": budgets})
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            revision_config,
            run_id="run-1",
        )

        completed = await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert len(snapshot.evidence) == expected_evidence
    assert len(snapshot.plans) == 2
    assert len(snapshot.candidates) == 2
    assert snapshot.candidates[0].plan_id != snapshot.candidates[1].plan_id
    assert [item.outcome for item in snapshot.adjudications] == [outcome, "accept"]
    if outcome == "revise_plan":
        assert snapshot.run.plan_revision_count == 1
    else:
        assert snapshot.run.strategy_rewrite_count == 1


async def test_active_run_can_stop_and_resume_from_durable_stage(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(passing_responses(), block_title="ProofDraft")
    resumable_config = config().model_copy(
        update={"budgets": config().budgets.model_copy(update={"proof_attempts": 2})}
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            resumable_config,
            run_id="run-1",
        )
        running = asyncio.create_task(workflow.execute("run-1"))
        await asyncio.wait_for(runtime.turn_reached.wait(), timeout=1)

        cancelled = await workflow.cancel("run-1")
        assert await asyncio.wait_for(running, timeout=1) == cancelled
        assert cancelled.status is RunStatus.CANCELLED
        assert cancelled.resumable is True
        assert len(runtime.interruptions) == 1
        assert store.list_candidates("run-1") == ()

        runtime.block_title = None
        completed = await workflow.resume("run-1", idempotency_key="resume-command-1")
        snapshot = store.snapshot("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert completed.resume_count == 1
    assert len(snapshot.evidence) == 1
    assert len(snapshot.plans) == 1
    assert runtime.counts["EvidenceBatch"] == 1
    assert runtime.counts["PlanDraft"] == 1
    assert runtime.counts["ProofDraft"] == 2
    assert [segment.version for segment in snapshot.execution_segments] == [1, 2]
    assert all(segment.released_at is not None for segment in snapshot.execution_segments)


async def test_external_cancel_request_interrupts_worker_and_closes_active_threads(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime(passing_responses(), block_title="ProofDraft")
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        running = asyncio.create_task(workflow.execute("run-1"))
        await asyncio.wait_for(runtime.turn_reached.wait(), timeout=1)

        external = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        externally_cancelled = await asyncio.wait_for(external.cancel("run-1"), timeout=1)
        cancelled = await asyncio.wait_for(running, timeout=1)
        snapshot = store.snapshot("run-1")

    assert cancelled.status is RunStatus.CANCELLED
    assert externally_cancelled == cancelled
    assert runtime.interruptions
    assert all(thread.status is not ThreadStatus.ACTIVE for thread in snapshot.threads)


async def test_cancellation_drains_final_usage_before_interrupted_terminal(
    tmp_path: Path,
) -> None:
    class FinalUsageRuntime(ScriptedRuntime):
        async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
            self.requests.append(request)
            turn = TurnRef(
                thread_id="draining-thread",
                turn_id="draining-turn",
                backend=RuntimeBackend.MOCK,
            )
            yield ThreadStarted(
                thread_id=turn.thread_id,
                backend=RuntimeBackend.MOCK,
            )
            yield TurnStarted(turn=turn)
            self._blocked_turn = turn
            self.turn_reached.set()
            await self._release_blocked_turn.wait()
            yield TokenUsageUpdated(
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
            yield TurnCompleted(turn=turn, status="interrupted")

    runtime = FinalUsageRuntime(passing_responses())
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        running = asyncio.create_task(workflow.execute("run-1"))
        await asyncio.wait_for(runtime.turn_reached.wait(), timeout=1)

        cancelled = await asyncio.wait_for(workflow.cancel("run-1"), timeout=1)
        assert await asyncio.wait_for(running, timeout=1) == cancelled
        snapshot = store.snapshot("run-1")

    assert cancelled.status is RunStatus.CANCELLED
    assert any(event.event_type == "runtime.token_usage" for event in snapshot.events)
    assert any(event.event_type == "runtime.turn_completed" for event in snapshot.events)
    assert not any(
        event.event_type == "runtime.turn_terminal_unconfirmed"
        for event in snapshot.events
    )


async def test_worker_task_cancellation_pauses_run_and_interrupts_active_turn(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime(passing_responses(), block_title="ProofDraft")
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        running = asyncio.create_task(workflow.execute("run-1"))
        await asyncio.wait_for(runtime.turn_reached.wait(), timeout=1)

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        paused = store.get_run("run-1")
        snapshot = store.snapshot("run-1")

    assert paused.status is RunStatus.PAUSED
    assert paused.resumable is True
    assert runtime.interruptions
    assert all(thread.status is not ThreadStatus.ACTIVE for thread in snapshot.threads)


async def test_heartbeat_failure_fails_run_and_always_releases_worker_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ScriptedRuntime(passing_responses(), block_title="ProofDraft")
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        async def broken_heartbeat(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("heartbeat exploded")

        monkeypatch.setattr(workflow, "_heartbeat_execution", broken_heartbeat)
        with pytest.raises(WorkflowExecutionError, match="heartbeat"):
            await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

    assert snapshot.run.status is RunStatus.FAILED
    assert all(segment.released_at is not None for segment in snapshot.execution_segments)
    assert all(thread.status is not ThreadStatus.ACTIVE for thread in snapshot.threads)


async def test_retryable_turn_error_uses_bounded_fresh_retry(tmp_path: Path) -> None:
    class IsolatedRetryRuntime(ScriptedRuntime):
        def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
            super().__init__(responses)
            self.preflight_requests: list[RunRequest] = []

        def preflight(self, request: RunRequest) -> None:
            self.preflight_requests.append(request)
            assert request.cwd.is_absolute()
            assert {entry.name for entry in request.cwd.iterdir()} == {".git"}
            assert (request.cwd / ".git").is_dir()

    responses = passing_responses()
    responses["EvidenceBatch"].insert(0, {"__runtime_error__": "temporary App Server overload"})
    runtime = IsolatedRetryRuntime(responses)
    retry_config = config().model_copy(
        update={"budgets": config().budgets.model_copy(update={"turn_retries": 1})}
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            retry_config,
            run_id="run-1",
        )

        completed = await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert runtime.counts["EvidenceBatch"] == 2
    evidence_attempts = [
        request
        for request in runtime.preflight_requests
        if request.output_schema["title"] == "EvidenceBatch"
    ]
    assert len(evidence_attempts) == 2
    assert evidence_attempts[0].cwd != evidence_attempts[1].cwd
    assert all(not request.cwd.exists() for request in runtime.preflight_requests)
    assert list((tmp_path / "runtime-workspaces").iterdir()) == []
    literature_threads = [
        thread for thread in snapshot.threads if thread.role is ThreadRole.LITERATURE
    ]
    assert [thread.status for thread in literature_threads] == [
        ThreadStatus.FAILED,
        ThreadStatus.COMPLETED,
    ]
    assert any(event.event_type == "runtime.turn_retry" for event in snapshot.events)
    assert not any(
        event.event_type == "runtime.turn_terminal_unconfirmed"
        for event in snapshot.events
    )


async def test_retryable_runtime_notification_can_recover_in_the_same_turn(
    tmp_path: Path,
) -> None:
    responses = passing_responses()
    evidence = responses["EvidenceBatch"][0]
    responses["EvidenceBatch"] = [
        {
            "__runtime_warning__": "transport recovered internally",
            "output": evidence,
        }
    ]
    runtime = ScriptedRuntime(responses)
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        completed = await workflow.execute("run-1")
        events = store.list_events("run-1")

    assert completed.status is RunStatus.COMPLETED
    assert runtime.counts["EvidenceBatch"] == 1
    assert runtime.interruptions == []
    assert any(event.event_type == "runtime.error" for event in events)


async def test_runtime_error_payload_never_persists_raw_message(tmp_path: Path) -> None:
    secret = "Bearer super-secret-runtime-token"
    responses = passing_responses()
    responses["EvidenceBatch"] = [
        {"__runtime_error__": secret, "retryable": False}
    ]
    runtime = ScriptedRuntime(responses)
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="runtime error"):
            await workflow.execute("run-1")
        events = store.list_events("run-1")

    assert secret not in json.dumps(
        [event.payload for event in events],
        sort_keys=True,
    )
    runtime_error = next(event for event in events if event.event_type == "runtime.error")
    assert set(runtime_error.payload) == {"code", "diagnostic_id", "retryable"}


async def test_schema_retry_never_persists_rejected_secret(tmp_path: Path) -> None:
    secret = "Bearer TOP-SECRET-schema-output"
    responses = passing_responses()
    rejected = dict(responses["EvidenceBatch"][0])
    rejected["authorization"] = secret
    responses["EvidenceBatch"].insert(0, rejected)
    runtime = ScriptedRuntime(responses)
    retry_config = config().model_copy(
        update={"budgets": config().budgets.model_copy(update={"turn_retries": 1})}
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            retry_config,
            run_id="run-1",
        )

        assert (await workflow.execute("run-1")).status is RunStatus.COMPLETED
        events = store.list_events("run-1")

    assert secret not in json.dumps(
        [event.payload for event in events],
        sort_keys=True,
    )
    retry = next(event for event in events if event.event_type == "runtime.turn_retry")
    assert retry.payload["reason_code"] == "output_schema_validation"
    assert isinstance(retry.payload["diagnostic_id"], str)


async def test_stream_eof_without_terminal_fails_closed_without_retry(
    tmp_path: Path,
) -> None:
    responses = passing_responses()
    responses["EvidenceBatch"].insert(0, {"__eof__": True})
    runtime = ScriptedRuntime(responses)
    retry_config = config().model_copy(
        update={"budgets": config().budgets.model_copy(update={"turn_retries": 1})}
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            retry_config,
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="terminal"):
            await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")
        literature_threads = tuple(
            thread
            for thread in snapshot.threads
            if thread.role is ThreadRole.LITERATURE
        )

    assert snapshot.run.status is RunStatus.FAILED
    assert [thread.status for thread in literature_threads] == [ThreadStatus.ACTIVE]
    assert runtime.counts["EvidenceBatch"] == 1
    assert any(
        event.event_type == "runtime.turn_terminal_unconfirmed"
        for event in snapshot.events
    )
    assert snapshot.execution_segments[-1].released_at is None


async def test_protocol_error_after_turn_start_keeps_ownership_fail_closed(
    tmp_path: Path,
) -> None:
    responses = passing_responses()
    responses["EvidenceBatch"] = [{"__protocol_error__": "malformed notification"}]
    runtime = ScriptedRuntime(responses)
    database = tmp_path / "qed.sqlite3"
    with RunStore(database) as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="malformed notification"):
            await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

    assert snapshot.run.status is RunStatus.FAILED
    assert runtime.interruptions
    assert snapshot.execution_segments[-1].released_at is None
    assert any(
        event.event_type == "runtime.turn_terminal_unconfirmed"
        for event in snapshot.events
    )


async def test_lost_turn_start_response_keeps_attempt_ownership_fail_closed(
    tmp_path: Path,
) -> None:
    class LostStartResponseRuntime(ScriptedRuntime):
        remote_accepted = False

        async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
            self.requests.append(request)
            yield ThreadStarted(
                thread_id="accepted-thread",
                backend=RuntimeBackend.APP_SERVER,
            )
            self.remote_accepted = True
            raise RuntimeError("turn/start response lost")

    runtime = LostStartResponseRuntime(passing_responses())
    database = tmp_path / "qed.sqlite3"
    with RunStore(database) as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="turn/start response lost"):
            await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

        assert runtime.remote_accepted is True
        assert snapshot.run.status is RunStatus.FAILED
        assert snapshot.execution_segments[-1].released_at is None
        assert any(
            event.event_type == "runtime.turn_start_unconfirmed"
            for event in snapshot.events
        )
        assert not any(
            event.event_type == "runtime.turn_started" for event in snapshot.events
        )
        with pytest.raises(ConflictError, match="unconfirmed terminal"):
            store.resume_run("run-1")


async def test_local_preflight_rejection_does_not_open_a_turn_attempt(
    tmp_path: Path,
) -> None:
    class RejectingExecRuntime(ScriptedRuntime):
        def preflight(self, request: RunRequest) -> None:
            if (
                request.runtime is RuntimePreference.EXEC
                and request.output_schema["title"] == "ProofDraft"
            ):
                raise ValueError(
                    "requested controls are not representable by codex exec fallback"
                )

    runtime = RejectingExecRuntime(passing_responses())
    rejected_exec = config().model_copy(update={"backend": "exec"})
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            rejected_exec,
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="exec fallback"):
            await workflow.execute("run-1")
        snapshot = store.snapshot("run-1")

        assert snapshot.run.status is RunStatus.FAILED
        assert snapshot.execution_segments[-1].released_at is not None
        assert not any(
            event.event_type == "runtime.turn_attempt_started"
            and event.payload.get("output_schema") == "ProofDraft"
            for event in snapshot.events
        )
        assert store.resume_run("run-1").status is RunStatus.RUNNING


async def test_late_interrupted_terminal_reconciles_after_settle_deadline(
    tmp_path: Path,
) -> None:
    class LateTerminalRuntime(ScriptedRuntime):
        terminal_emitted: asyncio.Event

        def __init__(self) -> None:
            super().__init__(passing_responses())
            self.terminal_emitted = asyncio.Event()

        async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
            self.requests.append(request)
            turn = TurnRef(
                thread_id="late-thread",
                turn_id="late-turn",
                backend=RuntimeBackend.MOCK,
            )
            yield ThreadStarted(thread_id=turn.thread_id, backend=RuntimeBackend.MOCK)
            yield TurnStarted(turn=turn)
            yield TokenUsageUpdated(
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
            self._blocked_turn = turn
            self.turn_reached.set()
            await self._release_blocked_turn.wait()
            await asyncio.sleep(1.1)
            self.terminal_emitted.set()
            yield TurnCompleted(turn=turn, status="interrupted")

    runtime = LateTerminalRuntime()
    limited = config().model_copy(
        update={"budgets": config().budgets.model_copy(update={"stage_seconds": 1})}
    )
    with RunStore(tmp_path / "qed.sqlite3") as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            limited,
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="stage timeout"):
            await workflow.execute("run-1")
        await asyncio.wait_for(runtime.terminal_emitted.wait(), timeout=1)
        await asyncio.sleep(0.05)
        snapshot = store.snapshot("run-1")

    assert snapshot.run.status is RunStatus.FAILED
    assert snapshot.execution_segments[-1].released_at is not None
    assert any(event.event_type == "runtime.turn_completed" for event in snapshot.events)


async def test_stage_timeout_is_shared_across_fresh_turn_retries(tmp_path: Path) -> None:
    now = [datetime(2026, 7, 16, 12, 0, tzinfo=UTC)]
    responses = passing_responses()
    responses["EvidenceBatch"].insert(
        0,
        {"__runtime_error__": "temporary App Server overload"},
    )

    class AdvancingRuntime(ScriptedRuntime):
        async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
            async for event in super().stream(request):
                if isinstance(event, RuntimeErrorEvent):
                    now[0] += timedelta(seconds=4)
                yield event

    runtime = AdvancingRuntime(responses)
    limited = config().model_copy(
        update={
            "budgets": config().budgets.model_copy(
                update={"stage_seconds": 3, "turn_retries": 1}
            )
        }
    )
    with RunStore(tmp_path / "qed.sqlite3", clock=lambda: now[0]) as store:
        workflow = ResearchWorkflow(
            store,
            runtime,
            runtime_version="test-runtime",
            clock=lambda: now[0],
        )
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            limited,
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="stage timeout"):
            await workflow.execute("run-1")

        assert runtime.counts["EvidenceBatch"] == 1
        assert store.get_run("run-1").status is RunStatus.FAILED


async def test_token_budget_survives_process_restart_and_fails_before_candidate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "qed.sqlite3"
    limited = config().model_copy(
        update={
            "budgets": config().budgets.model_copy(
                update={"max_tokens": 50, "proof_attempts": 2}
            )
        }
    )
    first_runtime = ScriptedRuntime(passing_responses(), block_title="ProofDraft")
    with RunStore(database) as store:
        first = ResearchWorkflow(store, first_runtime, runtime_version="test-runtime")
        first.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            limited,
            run_id="run-1",
        )
        running = asyncio.create_task(first.execute("run-1"))
        await asyncio.wait_for(first_runtime.turn_reached.wait(), timeout=1)
        await first.cancel("run-1")
        await running

    second_runtime = ScriptedRuntime(passing_responses())
    second_runtime.counts["ProofDraft"] = 1
    with RunStore(database) as reopened:
        second = ResearchWorkflow(reopened, second_runtime, runtime_version="test-runtime")
        with pytest.raises(WorkflowExecutionError, match="token budget"):
            await second.resume("run-1", idempotency_key="resume-after-restart")

        assert reopened.get_run("run-1").status is RunStatus.FAILED
        assert reopened.list_candidates("run-1") == ()
        assert second_runtime.counts["VerificationDraft"] == 0


async def test_run_time_budget_survives_process_restart(tmp_path: Path) -> None:
    now = [datetime(2026, 7, 16, 12, 0, tzinfo=UTC)]
    database = tmp_path / "qed.sqlite3"
    limited = config().model_copy(
        update={
            "budgets": config().budgets.model_copy(
                update={"run_seconds": 5, "stage_seconds": 5}
            )
        }
    )
    first_runtime = ScriptedRuntime(passing_responses(), block_title="ProofDraft")
    with RunStore(database, clock=lambda: now[0]) as store:
        first = ResearchWorkflow(
            store,
            first_runtime,
            runtime_version="test-runtime",
            clock=lambda: now[0],
        )
        first.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            limited,
            run_id="run-1",
        )
        running = asyncio.create_task(first.execute("run-1"))
        await asyncio.wait_for(first_runtime.turn_reached.wait(), timeout=1)
        now[0] += timedelta(seconds=6)
        await first.cancel("run-1")
        await running

    second_runtime = ScriptedRuntime(passing_responses())
    second_runtime.counts["ProofDraft"] = 1
    with RunStore(database, clock=lambda: now[0]) as reopened:
        second = ResearchWorkflow(
            reopened,
            second_runtime,
            runtime_version="test-runtime",
            clock=lambda: now[0],
        )
        with pytest.raises(WorkflowExecutionError, match="run time budget"):
            await second.resume("run-1", idempotency_key="resume-after-time-budget")

        assert reopened.get_run("run-1").status is RunStatus.FAILED
        assert second_runtime.requests == []


async def test_search_query_budget_is_durable_and_interrupts_excess_turn(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime(passing_responses(), literature_queries=2)
    limited = config().model_copy(
        update={
            "search": config().search.model_copy(update={"max_queries_per_stage": 1})
        }
    )
    database = tmp_path / "qed.sqlite3"
    with RunStore(database) as store:
        workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        workflow.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            limited,
            run_id="run-1",
        )

        with pytest.raises(WorkflowExecutionError, match="search query budget"):
            await workflow.execute("run-1")
        query_events = tuple(
            event
            for event in store.list_events("run-1")
            if event.event_type == "runtime.item_completed"
            and event.payload.get("counts_as_search_query") is True
        )

    assert len(query_events) == 2
    assert runtime.interruptions


async def test_resume_from_export_does_not_repeat_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import qed.workflow as workflow_module

    runtime = ScriptedRuntime(passing_responses())
    database = tmp_path / "qed.sqlite3"
    original_writer = workflow_module.write_export_bundle

    def fail_publish(*_args: Any, **_kwargs: Any) -> None:
        raise WorkflowExecutionError("simulated export publish failure")

    monkeypatch.setattr(workflow_module, "write_export_bundle", fail_publish)
    with RunStore(database) as store:
        first = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
        first.create_run(
            RunInput(problem="Prove that there are infinitely many primes."),
            config(),
            run_id="run-1",
        )
        with pytest.raises(WorkflowExecutionError, match="export publish"):
            await first.execute("run-1")
        assert store.get_run("run-1").stage is RunStage.EXPORT

    counts_before_resume = dict(runtime.counts)
    monkeypatch.setattr(workflow_module, "write_export_bundle", original_writer)
    with RunStore(database) as reopened:
        resumed = ResearchWorkflow(reopened, runtime, runtime_version="test-runtime")
        completed = await resumed.resume("run-1", idempotency_key="resume-export")

    assert completed.status is RunStatus.COMPLETED
    assert dict(runtime.counts) == counts_before_resume
