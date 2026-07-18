"""Durable orchestration for one Codex-native mathematical research run."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel, JsonValue, ValidationError

from qed.config import QEDConfig
from qed.decision import CandidateDecision, decide_candidate
from qed.export import build_export_bundle, write_export_bundle
from qed.inputs import RunInput
from qed.logging import get_logger
from qed.model_outputs import (
    AdjudicationDraft,
    EvidenceBatch,
    PlanDraft,
    ProofDraft,
    VerificationDraft,
    VerificationKind,
    materialize_adjudication,
    materialize_candidate,
    materialize_evidence,
    materialize_plan,
    materialize_report,
)
from qed.prompting import FrozenTurnInput, TurnRole, freeze_turn_input, render_turn_prompt
from qed.runtime import (
    CapabilityRequest,
    CodexRuntime,
    FreshThread,
    ItemCompleted,
    RunRequest,
    RuntimeCapabilities,
    RuntimeErrorEvent,
    RuntimePreference,
    ThreadStarted,
    TokenUsageUpdated,
    TurnCompleted,
    TurnRef,
    TurnStarted,
    UnknownNotification,
    WebSearchMode,
    WorkRole,
)
from qed.schemas import (
    Adjudication,
    Evidence,
    Plan,
    ProofCandidate,
    Provenance,
    VerificationReport,
    canonical_json,
    canonical_sha256,
)
from qed.store import (
    ArtifactRecord,
    ConflictError,
    ExecutionToken,
    InvalidTransitionError,
    RunRecord,
    RunStage,
    RunStatus,
    RunStore,
    StageOutputRecord,
    ThreadRole,
    ThreadStatus,
)
from qed.workflow_support import (
    counts_as_search_query,
    gather_strict,
    make_local_thread_id,
    runtime_preference,
    web_search_observation,
)

PROMPT_VERSIONS: dict[TurnRole, str] = {
    "literature": "qed-literature-v1",
    "planning": "qed-planning-v1",
    "proof": "qed-proof-v1",
    "structural_verifier": "qed-structural-verifier-v1",
    "detailed_verifier": "qed-detailed-verifier-v1",
    "citation_verifier": "qed-citation-verifier-v1",
    "adjudication": "qed-adjudication-v1",
}

DraftT = TypeVar("DraftT", bound=BaseModel)
_LOGGER = get_logger(__name__)
_RECONCILIATION_CLOSE_SECONDS = 5.0


class WorkflowExecutionError(RuntimeError):
    """Raised when a durable run cannot safely advance."""


class _WorkflowCancelled(WorkflowExecutionError):
    """Internal signal that lets the worker acknowledge a requested stop."""


class _RetryableTurnError(WorkflowExecutionError):
    """Internal signal for one bounded fresh-thread retry."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        diagnostic_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.diagnostic_id = diagnostic_id


@dataclass(frozen=True, slots=True)
class _TurnResult:
    output: BaseModel
    thread_id: str
    external_thread_id: str
    provenance: Provenance
    observation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CapabilityResolution:
    effective: RuntimeCapabilities
    observed: RuntimeCapabilities


@dataclass(frozen=True, slots=True)
class _PreparedTurnInput:
    id: str
    frozen: FrozenTurnInput
    output_schema: dict[str, Any]


@dataclass(slots=True)
class _TurnWorkspace:
    directory: tempfile.TemporaryDirectory[str]
    cwd: Path

    def close(self) -> None:
        self.directory.cleanup()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ResearchWorkflow:
    """Advance runs only through typed, persisted stage boundaries."""

    def __init__(
        self,
        store: RunStore,
        runtime: CodexRuntime,
        *,
        runtime_version: str,
        export_root: str | Path | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._runtime_version = runtime_version
        self._export_root = (
            Path(export_root) if export_root is not None else store.path.parent / "exports"
        )
        self._workspace_root = (
            store.path.resolve(strict=False).parent / "runtime-workspaces"
        )
        self._clock = clock
        self._active_turns: dict[str, set[TurnRef]] = defaultdict(set)
        self._unconfirmed_turns: dict[str, set[TurnRef]] = defaultdict(set)
        self._interrupting_turns: set[TurnRef] = set()
        self._usage_by_turn: dict[tuple[str, str, str], int] = {}
        self._queries_by_stage: dict[tuple[str, RunStage], int] = {}
        self._executing_runs: set[str] = set()
        self._execution_done: dict[str, asyncio.Event] = {}
        self._execution_tokens: dict[str, ExecutionToken] = {}
        self._reconciliation_tasks: dict[str, set[asyncio.Task[_TurnResult]]] = (
            defaultdict(set)
        )
        self._worker_id = f"worker-{uuid4().hex}"

    def create_run(
        self,
        run_input: RunInput,
        config: QEDConfig,
        *,
        run_id: str,
    ) -> RunRecord:
        """Persist a frozen run request before any model work starts."""

        provenance = self._application_provenance(
            config,
            source_id=run_id,
            prompt_version="qed-intake-v1",
        )
        created = self._store.create_run(
            run_id,
            config=config,
            run_input=run_input,
            provenance=provenance,
        )
        return created

    async def execute(self, run_id: str) -> RunRecord:
        """Run or continue a research run from its last durable stage boundary."""

        if run_id in self._executing_runs:
            raise WorkflowExecutionError(f"run {run_id} already has an active worker")
        run = self._store.get_run(run_id)
        if run.status is RunStatus.CREATED:
            run = self._store.transition_run(run_id, RunStatus.RUNNING)
        if run.status is not RunStatus.RUNNING:
            raise WorkflowExecutionError(
                f"run {run_id} must be created or running, not {run.status.value}"
            )

        done = self._execution_done.setdefault(run_id, asyncio.Event())
        done.clear()
        self._executing_runs.add(run_id)
        heartbeat: asyncio.Task[None] | None = None
        try:
            run_input = self._load_run_input(run_id)
            lease_secret = secrets.token_urlsafe(32)
            lease = self._store.acquire_execution(
                run_id,
                segment_id=f"segment-{uuid4().hex}",
                worker_id=self._worker_id,
                lease_token=lease_secret,
                lease_seconds=60,
                runtime_version=self._runtime_version,
            )
            execution = ExecutionToken(
                segment_id=lease.id,
                version=lease.version,
                lease_token=lease_secret,
            )
            self._execution_tokens[run_id] = execution
            heartbeat = asyncio.create_task(self._heartbeat_execution(run_id, execution))
            remaining_run = self._remaining_run_seconds(run_id)
            remaining_stage = self._remaining_stage_seconds(run_id)
            if remaining_run <= 0:
                raise WorkflowExecutionError("run time budget exceeded")
            if remaining_stage <= 0:
                raise WorkflowExecutionError("stage time budget exceeded")
            capability_timeout = min(remaining_run, remaining_stage)
            if capability_timeout <= 0:
                raise WorkflowExecutionError("capability probe time budget exceeded")
            try:
                async with asyncio.timeout(capability_timeout):
                    capability_resolution = await self._capabilities_with_heartbeat(
                        run,
                        heartbeat,
                    )
            except TimeoutError as error:
                raise WorkflowExecutionError(
                    "capability probe time budget exceeded"
                ) from error
            capabilities = capability_resolution.effective
            capabilities_json = cast(
                JsonValue,
                capabilities.model_dump(mode="json"),
            )
            observed_capabilities_json = cast(
                JsonValue,
                capability_resolution.observed.model_dump(mode="json"),
            )
            self._store.record_execution_resolution(
                execution,
                runtime_version=self._runtime_version,
                resolution=observed_capabilities_json,
            )
            self._put_output(
                run.id,
                RunStage.INTAKE,
                "runtime_capabilities",
                capabilities_json,
                self._application_provenance(
                    run.config,
                    source_id="runtime-capabilities",
                    prompt_version="qed-capabilities-v1",
                ),
            )
            self._restore_token_usage(run_id)
            self._restore_search_query_usage(run_id)
            return await self._advance_with_heartbeat(
                run_id,
                run_input,
                capabilities,
                execution,
                heartbeat,
            )
        except _WorkflowCancelled:
            await self._interrupt_active_turns(run_id)
            return self._acknowledge_cancel(run_id)
        except asyncio.CancelledError:
            await self._interrupt_active_turns(run_id)
            current = self._store.get_run(run_id)
            if current.status is RunStatus.RUNNING:
                self._store.transition_run(
                    run_id,
                    RunStatus.PAUSED,
                    execution=self._execution(run_id),
                )
            raise
        except ConflictError as error:
            raise WorkflowExecutionError(str(error)) from error
        except WorkflowExecutionError as error:
            if self._store.get_run(run_id).status is RunStatus.CANCELLING:
                await self._interrupt_active_turns(run_id)
                if self._store.has_unconfirmed_runtime_turns(run_id):
                    raise WorkflowExecutionError(
                        f"run {run_id} has no confirmed terminal event for an interrupted turn"
                    ) from error
                return self._acknowledge_cancel(run_id)
            self._mark_failed(run_id)
            raise
        except (RuntimeError, ValidationError, ValueError) as error:
            if self._store.get_run(run_id).status is RunStatus.CANCELLING:
                await self._interrupt_active_turns(run_id)
                if self._store.has_unconfirmed_runtime_turns(run_id):
                    raise WorkflowExecutionError(
                        f"run {run_id} has no confirmed terminal event for an interrupted turn"
                    ) from error
                return self._acknowledge_cancel(run_id)
            self._mark_failed(run_id)
            raise WorkflowExecutionError(str(error)) from error
        finally:
            try:
                if heartbeat is not None:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await heartbeat
            finally:
                try:
                    execution_to_release = self._execution_tokens.get(run_id)
                    pending_terminal = self._store.has_unconfirmed_runtime_turns(run_id)
                    reconciling = bool(self._reconciliation_tasks.get(run_id))
                    if execution_to_release is not None and not pending_terminal:
                        self._execution_tokens.pop(run_id, None)
                        self._store.release_execution(execution_to_release)
                    elif execution_to_release is not None and not reconciling:
                        self._execution_tokens.pop(run_id, None)
                finally:
                    self._executing_runs.discard(run_id)
                    done.set()

    async def cancel(self, run_id: str) -> RunRecord:
        """Request a durable stop and interrupt every active runtime turn."""

        requested = self._store.request_cancel(run_id)
        await self._interrupt_active_turns(run_id)
        if run_id in self._executing_runs:
            timeout = requested.config.budgets.stage_seconds
            try:
                async with asyncio.timeout(timeout):
                    await self._execution_done[run_id].wait()
            except TimeoutError as error:
                raise WorkflowExecutionError(
                    f"run {run_id} did not stop within {timeout} seconds"
                ) from error
            try:
                return self._acknowledge_cancel(run_id)
            except ConflictError as error:
                raise WorkflowExecutionError(
                    f"run {run_id} is waiting for terminal turn confirmation"
                ) from error

        timeout = requested.config.budgets.stage_seconds
        try:
            async with asyncio.timeout(timeout):
                while True:
                    current = self._store.get_run(run_id)
                    if current.status is not RunStatus.CANCELLING:
                        return current
                    try:
                        return self._acknowledge_cancel(run_id)
                    except ConflictError:
                        pass
                    await asyncio.sleep(0.05)
        except TimeoutError as error:
            raise WorkflowExecutionError(
                f"run {run_id} did not acknowledge cancellation within {timeout} seconds"
            ) from error

    async def resume(self, run_id: str, *, idempotency_key: str) -> RunRecord:
        """Start a new worker from the last durable stage after a stopped run."""

        if run_id in self._executing_runs:
            raise WorkflowExecutionError(f"run {run_id} already has an active worker")
        command = self._store.resume_run_command(
            run_id,
            idempotency_key=idempotency_key,
        )
        if command.replayed or command.run.status is not RunStatus.RUNNING:
            return command.run
        return await self.execute(run_id)

    async def close(self) -> None:
        """Drain late terminal pumps before their runtime and store are closed."""

        tasks = tuple(
            task
            for run_tasks in self._reconciliation_tasks.values()
            for task in run_tasks
        )
        if not tasks:
            return
        _, pending = await asyncio.wait(
            tasks,
            timeout=_RECONCILIATION_CLOSE_SECONDS,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

    async def _advance(
        self,
        run_id: str,
        run_input: RunInput,
        capabilities: RuntimeCapabilities,
        execution: ExecutionToken,
    ) -> RunRecord:
        while True:
            stage = self._store.get_run(run_id).stage
            if stage is RunStage.COMPLETE:
                self._store.transition_run(
                    run_id,
                    RunStatus.COMPLETED,
                    execution=execution,
                )
                return self._store.get_run(run_id)
            if stage is RunStage.EXPORT:
                adjudications = self._store.list_adjudications(run_id)
                if not adjudications or adjudications[-1].outcome != "accept":
                    raise WorkflowExecutionError("export stage has no accepting adjudication")
                await self._export(run_id, adjudications[-1].candidate_id)
                return self._store.get_run(run_id)
            if stage is RunStage.INTAKE:
                self._ensure_stage(run_id, RunStage.LITERATURE)
                continue
            if stage is RunStage.LITERATURE:
                await self._literature(run_id, run_input, capabilities)
                self._ensure_stage(run_id, RunStage.PLANNING)
                continue
            if stage is RunStage.PLANNING:
                evidence = self._store.list_evidence(run_id)
                if not evidence:
                    raise WorkflowExecutionError("run has no durable evidence")
                await self._planning(run_id, run_input, evidence, capabilities)
                self._ensure_stage(run_id, RunStage.PROVING)
                continue
            if stage is RunStage.PROVING:
                evidence = self._store.list_evidence(run_id)
                plans = self._store.list_plans(run_id)
                if not evidence or not plans:
                    raise WorkflowExecutionError("proving requires durable evidence and plan")
                await self._proving(
                    run_id,
                    run_input,
                    plans[-1],
                    evidence,
                    capabilities,
                )
                self._ensure_stage(run_id, RunStage.VERIFICATION)
                continue
            if stage is RunStage.VERIFICATION:
                candidates = self._current_cycle_candidates(run_id)
                if not candidates:
                    raise WorkflowExecutionError(
                        "verification has no candidates from the current proving cycle"
                    )
                await self._verification(
                    run_id,
                    run_input,
                    candidates,
                    capabilities,
                )
                self._ensure_stage(run_id, RunStage.ADJUDICATION)
                continue
            if stage is not RunStage.ADJUDICATION:
                raise WorkflowExecutionError(f"unsupported run stage: {stage.value}")

            candidates = self._current_cycle_candidates(run_id)
            if not candidates:
                raise WorkflowExecutionError(
                    "adjudication has no candidates from the current proving cycle"
                )
            grouped: dict[str, list[VerificationReport]] = {
                candidate.id: [] for candidate in candidates
            }
            for record in self._store.list_verifications(run_id):
                if record.candidate_id in grouped:
                    grouped[record.candidate_id].append(record.report)
            reports_by_candidate = {
                candidate_id: tuple(reports)
                for candidate_id, reports in grouped.items()
            }
            evidence = self._store.list_evidence(run_id)
            required_rule_ids = tuple(
                rule.id for rule in run_input.frozen_verification_rules
            )
            require_citation = bool(evidence)
            candidate_records = {
                record.id: record
                for record in self._store.list_candidates(run_id)
            }
            threads = {
                thread.id: thread
                for thread in self._store.list_threads(run_id)
            }

            prover_external_ids: dict[str, str] = {}
            for cycle_candidate in candidates:
                candidate_id = cycle_candidate.id
                candidate_record = candidate_records.get(candidate_id)
                prover_thread = (
                    threads.get(candidate_record.thread_id)
                    if candidate_record is not None
                    and candidate_record.thread_id is not None
                    else None
                )
                if (
                    prover_thread is None
                    or prover_thread.role is not ThreadRole.PROVER
                    or prover_thread.external_thread_id is None
                ):
                    raise WorkflowExecutionError(
                        f"candidate {candidate_id} is missing prover external identity"
                    )
                prover_external_ids[candidate_id] = (
                    prover_thread.external_thread_id
                )

            advisory_decisions = {
                candidate.id: decide_candidate(
                    candidate,
                    reports_by_candidate[candidate.id],
                    prover_external_thread_id=prover_external_ids[candidate.id],
                    require_citation=require_citation,
                    required_evidence=evidence,
                    required_rule_ids=required_rule_ids,
                )
                for candidate in candidates
            }
            existing = self._current_stage_adjudication(run_id)
            if existing is not None:
                candidate = next(
                    (item for item in candidates if item.id == existing.candidate_id),
                    None,
                )
                if candidate is None:
                    raise WorkflowExecutionError(
                        "latest adjudication references an unknown candidate"
                    )
            else:
                candidate = next(
                    (item for item in candidates if advisory_decisions[item.id].passed),
                    candidates[0],
                )
            reports = reports_by_candidate[candidate.id]
            adjudication = await self._adjudication(
                run_id,
                run_input,
                candidate,
                reports,
                advisory_decisions[candidate.id],
                capabilities,
            )
            decision = self._store.record_decision(
                run_id,
                candidate.id,
                require_citation=require_citation,
                execution=execution,
            )
            if adjudication.outcome == "accept":
                if not decision.passed:
                    reasons = ", ".join(decision.reasons)
                    raise WorkflowExecutionError(
                        f"candidate {candidate.id} did not pass: {reasons}"
                    )
                self._put_output(
                    run_id,
                    RunStage.ADJUDICATION,
                    "accepted_candidate",
                    cast(JsonValue, decision.model_dump(mode="json")),
                    self._application_provenance(
                        self._store.get_run(run_id).config,
                        source_id=candidate.id,
                        prompt_version="qed-decision-v1",
                    ),
                )
                await self._export(run_id, candidate.id)
                return self._store.get_run(run_id)
            if adjudication.outcome == "abandon":
                raise WorkflowExecutionError(
                    f"adjudication abandoned candidate {candidate.id}"
                )
            target = {
                "revise_proof": RunStage.PROVING,
                "revise_plan": RunStage.PLANNING,
                "rewrite": RunStage.LITERATURE,
            }[adjudication.outcome]
            self._ensure_stage(run_id, target)

    async def _advance_with_heartbeat(
        self,
        run_id: str,
        run_input: RunInput,
        capabilities: RuntimeCapabilities,
        execution: ExecutionToken,
        heartbeat: asyncio.Task[None],
    ) -> RunRecord:
        remaining = self._remaining_run_seconds(run_id)
        if remaining <= 0:
            raise WorkflowExecutionError("run time budget exceeded")
        advance = asyncio.create_task(
            self._advance(run_id, run_input, capabilities, execution),
            name=f"qed-advance-{run_id}",
        )
        try:
            try:
                async with asyncio.timeout(remaining):
                    done, _ = await asyncio.wait(
                        {advance, heartbeat},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if heartbeat in done:
                        heartbeat_error = heartbeat.exception()
                        if heartbeat_error is not None:
                            raise WorkflowExecutionError(
                                "execution heartbeat failed"
                            ) from heartbeat_error
                    return await advance
            except TimeoutError as error:
                raise WorkflowExecutionError("run time budget exceeded") from error
        finally:
            if not advance.done():
                advance.cancel()
            await asyncio.gather(advance, return_exceptions=True)

    def _remaining_run_seconds(self, run_id: str) -> float:
        run = self._store.get_run(run_id)
        now = self._clock()
        elapsed = 0.0
        for segment in self._store.list_execution_segments(run_id):
            end = min(
                segment.released_at or now,
                segment.lease_expires_at,
                now,
            )
            duration = (end - segment.created_at).total_seconds()
            if duration < 0:
                raise WorkflowExecutionError(
                    f"execution segment {segment.id} has an invalid duration"
                )
            elapsed += duration
        return run.config.budgets.run_seconds - elapsed

    def _remaining_stage_seconds(self, run_id: str) -> float:
        run = self._store.get_run(run_id)
        stage = run.stage
        if stage is RunStage.INTAKE:
            started_at = run.created_at
        else:
            entry = self._store.latest_stage_entry(run_id, stage)
            if entry is None or entry.payload.get("to") != stage.value:
                raise WorkflowExecutionError(
                    f"run has no durable {stage.value} stage entry"
                )
            started_at = entry.created_at

        now = self._clock()
        elapsed = 0.0
        for segment in self._store.list_execution_segments(run_id):
            segment_start = max(started_at, segment.created_at)
            segment_end = min(
                segment.released_at or now,
                segment.lease_expires_at,
                now,
            )
            if segment_end > segment_start:
                elapsed += (segment_end - segment_start).total_seconds()
        return run.config.budgets.stage_seconds - elapsed

    def _stage_entry_seq(self, run_id: str, stage: RunStage) -> int:
        event = self._store.latest_stage_entry(run_id, stage)
        if event is not None and event.payload.get("to") == stage.value:
            return event.seq
        if stage is RunStage.INTAKE:
            return 0
        raise WorkflowExecutionError(f"run has no durable {stage.value} stage entry")

    def _current_cycle_candidates(self, run_id: str) -> tuple[ProofCandidate, ...]:
        entry_seq = self._stage_entry_seq(run_id, RunStage.PROVING)
        candidate_ids = {
            event.payload.get("candidate_id")
            for event in self._store.list_events(run_id, after_seq=entry_seq)
            if event.event_type == "candidate.created"
            and isinstance(event.payload.get("candidate_id"), str)
        }
        records = tuple(
            record
            for record in self._store.list_candidates(run_id)
            if record.id in candidate_ids
        )
        if any(record.sealed_at is None for record in records):
            raise WorkflowExecutionError("current proving cycle contains an unsealed candidate")
        return tuple(record.candidate for record in records)

    def _current_stage_adjudication(self, run_id: str) -> Adjudication | None:
        entry_seq = self._stage_entry_seq(run_id, RunStage.ADJUDICATION)
        matching = tuple(
            event
            for event in self._store.list_events(run_id, after_seq=entry_seq)
            if event.event_type == "adjudication.created"
        )
        if not matching:
            return None
        adjudication_id = matching[-1].payload.get("adjudication_id")
        if not isinstance(adjudication_id, str):
            raise WorkflowExecutionError("adjudication event has no typed identity")
        return self._store.get_adjudication(adjudication_id)

    async def _capabilities(self, run: RunRecord) -> _CapabilityResolution:
        existing = self._find_output(run.id, "runtime_capabilities")
        if existing is not None:
            recorded = RuntimeCapabilities.model_validate_json(canonical_json(existing.content))
            if recorded.model != run.config.model:
                raise WorkflowExecutionError(
                    "persisted runtime capability model does not match run config"
                )
            try:
                available = await self._runtime.probe(
                    CapabilityRequest(
                        model=recorded.model,
                        effort=recorded.selected_effort,
                        proactive=recorded.proactive_multi_agent,
                    )
                )
            except Exception as error:
                raise WorkflowExecutionError(
                    f"persisted runtime capability is no longer available: {error}"
                ) from error
            if (
                available.selected_effort != recorded.selected_effort
                or available.proactive_multi_agent != recorded.proactive_multi_agent
            ):
                raise WorkflowExecutionError(
                    "persisted runtime controls no longer match current capabilities"
                )
            return _CapabilityResolution(effective=recorded, observed=available)
        try:
            capabilities = await self._runtime.probe(
                CapabilityRequest(
                    model=run.config.model,
                    effort=run.config.effort,
                    proactive=run.config.parallelism.proactive_multi_agent,
                )
            )
        except Exception as error:
            raise WorkflowExecutionError(f"runtime capability probe failed: {error}") from error
        return _CapabilityResolution(effective=capabilities, observed=capabilities)

    async def _capabilities_with_heartbeat(
        self,
        run: RunRecord,
        heartbeat: asyncio.Task[None],
    ) -> _CapabilityResolution:
        probe = asyncio.create_task(
            self._capabilities(run),
            name=f"qed-capability-probe-{run.id}",
        )
        try:
            done, _ = await asyncio.wait(
                {probe, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                current = self._store.get_run(run.id)
                if current.status is RunStatus.CANCELLING:
                    raise _WorkflowCancelled("capability probe cancelled")
                heartbeat_error = heartbeat.exception()
                if heartbeat_error is not None:
                    raise WorkflowExecutionError(
                        "execution heartbeat failed during capability probe"
                    ) from heartbeat_error
                raise WorkflowExecutionError(
                    "execution heartbeat stopped during capability probe"
                )
            return await probe
        finally:
            if not probe.done():
                probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)

    async def _literature(
        self,
        run_id: str,
        run_input: RunInput,
        capabilities: RuntimeCapabilities,
    ) -> tuple[Evidence, ...]:
        existing = self._store.list_evidence(run_id)
        run = self._store.get_run(run_id)
        expected_batches = run.strategy_rewrite_count + 1
        persisted_batches = sum(
            event.event_type == "evidence.batch_created"
            for event in self._store.list_events(run_id)
        )
        if existing and persisted_batches >= expected_batches:
            return existing

        self._ensure_stage(run_id, RunStage.LITERATURE)
        result = await self._turn(
            run_id,
            prompt_role="literature",
            thread_role=ThreadRole.LITERATURE,
            work_role=WorkRole.LITERATURE,
            schema=EvidenceBatch,
            payload={
                "run_input": run_input.model_dump(mode="json"),
                "prior_evidence": [
                    item.model_dump(mode="json") for item in existing
                ],
                "revision_context": self._revision_context(run_id),
            },
            capabilities=capabilities,
        )
        observations = tuple(
            self._store.get_web_search_observation(observation_id)
            for observation_id in result.observation_ids
        )
        evidence = materialize_evidence(
            cast(EvidenceBatch, result.output),
            result.provenance,
            observations=observations,
        )
        self._store.add_evidence_batch(
            run_id,
            evidence,
            execution=self._execution(run_id),
        )
        return self._store.list_evidence(run_id)

    async def _planning(
        self,
        run_id: str,
        run_input: RunInput,
        evidence: tuple[Evidence, ...],
        capabilities: RuntimeCapabilities,
    ) -> Plan:
        existing = self._store.list_plans(run_id)
        run = self._store.get_run(run_id)
        expected_plans = run.plan_revision_count + run.strategy_rewrite_count + 1
        if len(existing) >= expected_plans:
            return existing[-1]

        self._ensure_stage(run_id, RunStage.PLANNING)
        result = await self._turn(
            run_id,
            prompt_role="planning",
            thread_role=ThreadRole.PLANNER,
            work_role=WorkRole.GENERAL,
            schema=PlanDraft,
            payload={
                "run_input": run_input.model_dump(mode="json"),
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "prior_plans": [
                    item.model_dump(mode="json") for item in existing
                ],
                "revision_context": self._revision_context(run_id),
            },
            capabilities=capabilities,
        )
        plan = materialize_plan(
            cast(PlanDraft, result.output),
            problem_sha256=run_input.sha256,
            provenance=result.provenance,
            created_at=result.provenance.captured_at,
        )
        self._store.add_plan(run_id, plan, execution=self._execution(run_id))
        return plan

    async def _proving(
        self,
        run_id: str,
        run_input: RunInput,
        plan: Plan,
        evidence: tuple[Evidence, ...],
        capabilities: RuntimeCapabilities,
    ) -> tuple[ProofCandidate, ...]:
        self._ensure_stage(run_id, RunStage.PROVING)
        existing = self._current_cycle_candidates(run_id)
        run = self._store.get_run(run_id)
        missing_count = max(
            0,
            run.config.parallelism.proof_candidates - len(existing),
        )
        available_attempts = run.config.budgets.proof_attempts - run.proof_attempt_count
        scheduled_count = min(missing_count, available_attempts)
        if not existing and scheduled_count == 0:
            raise WorkflowExecutionError("proof attempt budget exhausted")
        missing_attempts = tuple(
            self._store.reserve_proof_attempt(
                run_id,
                execution=self._execution(run_id),
            )
            for _ in range(scheduled_count)
        )

        async def prove_one(attempt: int) -> _TurnResult:
            return await self._turn(
                run_id,
                prompt_role="proof",
                thread_role=ThreadRole.PROVER,
                work_role=WorkRole.GENERAL,
                schema=ProofDraft,
                payload={
                    "run_input": run_input.model_dump(mode="json"),
                    "plan": plan.model_dump(mode="json"),
                    "evidence": [item.model_dump(mode="json") for item in evidence],
                    "attempt": attempt,
                    "revision_context": self._revision_context(run_id),
                },
                capabilities=capabilities,
            )

        results = await gather_strict(
            tuple(prove_one(attempt) for attempt in missing_attempts)
        )
        created: list[ProofCandidate] = list(existing)
        for attempt, result in zip(missing_attempts, results, strict=True):
            candidate = materialize_candidate(
                cast(ProofDraft, result.output),
                run_id=run_id,
                plan_id=plan.id,
                attempt=attempt,
                provenance=result.provenance,
                created_at=result.provenance.captured_at,
            )
            self._store.create_candidate(
                candidate,
                thread_id=result.thread_id,
                sealed=True,
                execution=self._execution(run_id),
            )
            created.append(candidate)
        return tuple(created)

    def _revision_context(self, run_id: str) -> dict[str, JsonValue]:
        run = self._store.get_run(run_id)
        return {
            "plan_revision_count": run.plan_revision_count,
            "strategy_rewrite_count": run.strategy_rewrite_count,
            "prior_candidates": [
                record.candidate.model_dump(mode="json")
                for record in self._store.list_candidates(run_id)
            ],
            "prior_reports": [
                record.report.model_dump(mode="json")
                for record in self._store.list_verifications(run_id)
            ],
            "prior_adjudications": [
                item.model_dump(mode="json")
                for item in self._store.list_adjudications(run_id)
            ],
        }

    async def _verification(
        self,
        run_id: str,
        run_input: RunInput,
        candidates: tuple[ProofCandidate, ...],
        capabilities: RuntimeCapabilities,
    ) -> dict[str, tuple[VerificationReport, ...]]:
        self._ensure_stage(run_id, RunStage.VERIFICATION)
        stored = {
            (record.candidate_id, record.kind): record.report
            for record in self._store.list_verifications(run_id)
        }
        verifier_limit = self._store.get_run(run_id).config.parallelism.verifiers
        semaphore = asyncio.Semaphore(verifier_limit)
        evidence = self._store.list_evidence(run_id)
        plans = {plan.id: plan for plan in self._store.list_plans(run_id)}

        async def verify_one(
            candidate: ProofCandidate,
            kind: VerificationKind,
            prompt_role: TurnRole,
        ) -> VerificationReport:
            if (candidate.id, kind) in stored:
                return stored[(candidate.id, kind)]
            plan = plans.get(candidate.plan_id)
            if plan is None:
                raise WorkflowExecutionError(
                    f"candidate {candidate.id} references an unknown plan"
                )
            async with semaphore:
                result = await self._turn(
                    run_id,
                    prompt_role=prompt_role,
                    thread_role=ThreadRole.VERIFIER,
                    work_role=(WorkRole.CITATION if kind == "citation" else WorkRole.VERIFIER),
                    schema=VerificationDraft,
                    payload={
                        "problem": run_input.problem,
                        "verification_rules": [
                            rule.model_dump(mode="json")
                            for rule in run_input.frozen_verification_rules
                        ],
                        "candidate": candidate.model_dump(mode="json"),
                        "plan": plan.model_dump(mode="json"),
                        "evidence": [item.model_dump(mode="json") for item in evidence],
                        "verification_kind": kind,
                    },
                    capabilities=capabilities,
                )
                return materialize_report(
                    cast(VerificationDraft, result.output),
                    candidate=candidate,
                    kind=kind,
                    verifier_thread_id=result.thread_id,
                    verifier_external_thread_id=result.external_thread_id,
                    provenance=result.provenance,
                    created_at=self._clock(),
                )

        requests: list[tuple[ProofCandidate, VerificationKind, TurnRole]] = []
        for candidate in candidates:
            required: list[tuple[VerificationKind, TurnRole]] = [
                ("structural", "structural_verifier"),
                ("detailed", "detailed_verifier"),
            ]
            if evidence:
                required.append(("citation", "citation_verifier"))
            requests.extend(
                (candidate, kind, prompt_role) for kind, prompt_role in required
            )
        reports = await gather_strict(
            tuple(
                verify_one(candidate, kind, prompt_role)
                for candidate, kind, prompt_role in requests
            )
        )
        for report in reports:
            if (report.candidate_id, report.kind) not in stored:
                self._store.add_verification(run_id, report, execution=self._execution(run_id))
        grouped: dict[str, list[VerificationReport]] = {
            candidate.id: [] for candidate in candidates
        }
        for report in reports:
            grouped[report.candidate_id].append(report)
        return {candidate_id: tuple(items) for candidate_id, items in grouped.items()}

    async def _adjudication(
        self,
        run_id: str,
        run_input: RunInput,
        candidate: ProofCandidate,
        reports: tuple[VerificationReport, ...],
        decision: CandidateDecision,
        capabilities: RuntimeCapabilities,
    ) -> Adjudication:
        existing = self._current_stage_adjudication(run_id)
        if existing is not None:
            return existing

        self._ensure_stage(run_id, RunStage.ADJUDICATION)
        result = await self._turn(
            run_id,
            prompt_role="adjudication",
            thread_role=ThreadRole.ADJUDICATOR,
            work_role=WorkRole.GENERAL,
            schema=AdjudicationDraft,
            payload={
                "problem": run_input.problem,
                "candidate": candidate.model_dump(mode="json"),
                "reports": [report.model_dump(mode="json") for report in reports],
                "code_decision": decision.model_dump(mode="json"),
            },
            capabilities=capabilities,
        )
        adjudication = materialize_adjudication(
            cast(AdjudicationDraft, result.output),
            candidate_id=candidate.id,
            report_ids=tuple(report.id for report in reports),
            provenance=result.provenance,
            created_at=result.provenance.captured_at,
        )
        if adjudication.outcome == "accept" and not decision.passed:
            reasons = ", ".join(decision.reasons)
            raise WorkflowExecutionError(
                f"candidate {candidate.id} did not pass: {reasons}"
            )
        self._store.add_adjudication(run_id, adjudication, execution=self._execution(run_id))
        return adjudication

    async def _export(self, run_id: str, candidate_id: str) -> None:
        self._ensure_stage(run_id, RunStage.EXPORT)
        run = self._store.get_run(run_id)
        provenance = self._application_provenance(
            run.config,
            source_id=candidate_id,
            prompt_version="qed-export-v1",
        )
        intent = self._find_output(run_id, "export_intent")
        if intent is None:
            generated_at = self._clock()
            intent = self._put_output(
                run_id,
                RunStage.EXPORT,
                "export_intent",
                {"generated_at": generated_at.isoformat()},
                provenance,
            )
        generated_at = datetime.fromisoformat(cast(dict[str, str], intent.content)["generated_at"])
        bundle = build_export_bundle(
            self._store.snapshot(run_id),
            candidate_id=candidate_id,
            generated_at=generated_at,
        )
        destination = write_export_bundle(bundle, self._export_root)
        artifacts = (
            ("proof", "text/markdown", "proof.md", bundle.proof_md.encode()),
            (
                "report",
                "text/markdown",
                "report.md",
                bundle.report_md.encode(),
            ),
            (
                "manifest",
                "application/json",
                "manifest.json",
                bundle.manifest_json.encode(),
            ),
        )
        for kind, media_type, filename, content in artifacts:
            self._register_artifact(
                run_id,
                kind=kind,
                media_type=media_type,
                content_sha256=(
                    bundle.manifest.artifacts[0].sha256
                    if kind == "proof"
                    else bundle.manifest.artifacts[1].sha256
                    if kind == "report"
                    else bundle.bundle_sha256
                ),
                size_bytes=len(content),
                relative_path=(destination / filename)
                .relative_to(self._export_root.absolute())
                .as_posix(),
                provenance=provenance,
            )
        self._ensure_stage(run_id, RunStage.COMPLETE)
        self._store.transition_run(
            run_id,
            RunStatus.COMPLETED,
            execution=self._execution(run_id),
        )

    async def _turn(
        self,
        run_id: str,
        *,
        prompt_role: TurnRole,
        thread_role: ThreadRole,
        work_role: WorkRole,
        schema: type[DraftT],
        payload: dict[str, JsonValue],
        capabilities: RuntimeCapabilities,
    ) -> _TurnResult:
        run = self._store.get_run(run_id)
        retries = run.config.budgets.turn_retries
        prepared = self._prepare_turn_input(
            run_id,
            prompt_role=prompt_role,
            schema=schema,
            payload=payload,
        )
        attempts_started = sum(
            1
            for event in self._store.list_events(run_id)
            if event.event_type == "runtime.turn_attempt_started"
            and event.payload.get("turn_input_id") == prepared.id
        )
        total_attempts = retries + 1
        if attempts_started >= total_attempts:
            raise WorkflowExecutionError(
                f"{schema.__name__} exhausted {retries} turn retries"
            )
        for attempt in range(attempts_started, total_attempts):
            remaining = self._remaining_stage_seconds(run_id)
            if remaining <= 0:
                raise WorkflowExecutionError(
                    f"{schema.__name__} exceeded the stage timeout"
                )
            workspace = self._new_turn_workspace()
            try:
                request = self._build_turn_request(
                    run_id,
                    work_role=work_role,
                    prepared=prepared,
                    capabilities=capabilities,
                    cwd=workspace.cwd,
                )
                preflight = getattr(self._runtime, "preflight", None)
                if preflight is not None:
                    preflight(request)
            except BaseException:
                workspace.close()
                raise
            try:
                self._store.append_event(
                    run_id,
                    event_type="runtime.turn_attempt_started",
                    stage=self._store.get_run(run_id).stage,
                    payload={
                        "turn_input_id": prepared.id,
                        "output_schema": schema.__name__,
                        "attempt": attempt + 1,
                    },
                    execution=self._execution(run_id),
                )
                turn_task = asyncio.create_task(
                    self._turn_once_in_workspace(
                        run_id,
                        workspace=workspace,
                        attempt=attempt + 1,
                        prompt_role=prompt_role,
                        thread_role=thread_role,
                        schema=schema,
                        prepared=prepared,
                        request=request,
                    ),
                    name=f"qed-turn-{run_id}-{prepared.id}-{attempt + 1}",
                )
            except BaseException:
                workspace.close()
                raise
            try:
                async with asyncio.timeout(remaining):
                    return await asyncio.shield(turn_task)
            except asyncio.CancelledError:
                await self._settle_turn_task(run_id, turn_task)
                raise
            except _RetryableTurnError as error:
                if attempt >= retries:
                    raise WorkflowExecutionError(
                        f"{schema.__name__} exhausted {retries} turn retries: {error}"
                    ) from error
                retry_payload: dict[str, JsonValue] = {
                    "output_schema": schema.__name__,
                    "turn_input_id": prepared.id,
                    "failed_attempt": attempt + 1,
                    "next_attempt": attempt + 2,
                    "reason_code": error.reason_code,
                }
                if error.diagnostic_id is not None:
                    retry_payload["diagnostic_id"] = error.diagnostic_id
                self._store.append_event(
                    run_id,
                    event_type="runtime.turn_retry",
                    stage=self._store.get_run(run_id).stage,
                    payload=retry_payload,
                    execution=self._execution(run_id),
                )
            except TimeoutError as error:
                await self._settle_turn_task(run_id, turn_task)
                raise WorkflowExecutionError(
                    f"{schema.__name__} exceeded the stage timeout"
                ) from error
        raise AssertionError("bounded retry loop did not return")

    def _prepare_turn_input(
        self,
        run_id: str,
        *,
        prompt_role: TurnRole,
        schema: type[DraftT],
        payload: dict[str, JsonValue],
    ) -> _PreparedTurnInput:
        frozen = freeze_turn_input(prompt_role, payload)
        output_schema = schema.model_json_schema()
        output_schema_sha256 = canonical_sha256(output_schema)
        turn_input_identity: dict[str, JsonValue] = {
            "run_id": run_id,
            "role": prompt_role,
            "prompt_version": PROMPT_VERSIONS[prompt_role],
            "payload_sha256": frozen.payload_sha256,
            "output_schema_sha256": output_schema_sha256,
        }
        turn_input_id = f"turn-input-{canonical_sha256(turn_input_identity)[:24]}"
        self._store.put_turn_input(
            turn_input_id,
            run_id=run_id,
            role=prompt_role,
            prompt_version=PROMPT_VERSIONS[prompt_role],
            output_schema_sha256=output_schema_sha256,
            payload=frozen.payload,
            payload_sha256=frozen.payload_sha256,
            execution=self._execution(run_id),
        )
        return _PreparedTurnInput(
            id=turn_input_id,
            frozen=frozen,
            output_schema=output_schema,
        )

    def _build_turn_request(
        self,
        run_id: str,
        *,
        work_role: WorkRole,
        prepared: _PreparedTurnInput,
        capabilities: RuntimeCapabilities,
        cwd: Path,
    ) -> RunRequest:
        run = self._store.get_run(run_id)
        can_search = (
            run.config.search.enabled
            and work_role.value in run.config.search.allowed_roles
        )
        return RunRequest(
            model=run.config.model,
            effort=capabilities.selected_effort,
            proactive=capabilities.proactive_multi_agent,
            prompt=render_turn_prompt(prepared.frozen),
            output_schema=prepared.output_schema,
            thread=FreshThread(),
            role=work_role,
            web_search=WebSearchMode.LIVE if can_search else WebSearchMode.DISABLED,
            runtime=runtime_preference(run.config.backend),
            cwd=cwd,
        )

    def _new_turn_workspace(self) -> _TurnWorkspace:
        root = self._workspace_root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise WorkflowExecutionError("runtime workspace root is not a directory")
        os.chmod(root, 0o700)
        executable = shutil.which("git")
        if executable is None:
            raise WorkflowExecutionError("git is required for isolated runtime workspaces")
        git = Path(executable).resolve(strict=True)
        template = tempfile.TemporaryDirectory(prefix=".git-template-", dir=root)
        directory: tempfile.TemporaryDirectory[str] | None = None
        try:
            directory = tempfile.TemporaryDirectory(prefix=".turn-", dir=root)
            cwd = Path(directory.name).resolve(strict=True)
            subprocess.run(  # noqa: S603 - resolved executable and fixed argv
                (
                    str(git),
                    "init",
                    "--quiet",
                    f"--template={template.name}",
                    "--initial-branch=qed",
                    str(cwd),
                ),
                check=True,
                capture_output=True,
                env={
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "LC_ALL": "C",
                },
            )
            if {entry.name for entry in cwd.iterdir()} != {".git"}:
                raise WorkflowExecutionError(
                    "isolated runtime workspace was not initialized empty"
                )
            git_directory = cwd / ".git"
            if git_directory.is_symlink() or not git_directory.is_dir():
                raise WorkflowExecutionError(
                    "isolated runtime workspace has an invalid Git directory"
                )
            return _TurnWorkspace(directory=directory, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as error:
            if directory is not None:
                directory.cleanup()
            raise WorkflowExecutionError(
                "could not initialize an isolated runtime workspace"
            ) from error
        except BaseException:
            if directory is not None:
                directory.cleanup()
            raise
        finally:
            template.cleanup()

    async def _turn_once_in_workspace(
        self,
        run_id: str,
        *,
        workspace: _TurnWorkspace,
        attempt: int,
        prompt_role: TurnRole,
        thread_role: ThreadRole,
        schema: type[DraftT],
        prepared: _PreparedTurnInput,
        request: RunRequest,
    ) -> _TurnResult:
        try:
            return await self._turn_once(
                run_id,
                attempt=attempt,
                prompt_role=prompt_role,
                thread_role=thread_role,
                schema=schema,
                prepared=prepared,
                request=request,
            )
        finally:
            workspace.close()

    async def _turn_once(
        self,
        run_id: str,
        *,
        attempt: int,
        prompt_role: TurnRole,
        thread_role: ThreadRole,
        schema: type[DraftT],
        prepared: _PreparedTurnInput,
        request: RunRequest,
    ) -> _TurnResult:
        run = self._store.get_run(run_id)
        local_thread_id: str | None = None
        turn: TurnRef | None = None
        provenance: Provenance | None = None
        pending_runtime_error: WorkflowExecutionError | None = None
        usage_observed = False
        observation_ids: list[str] = []
        events = self._runtime.stream(request)
        try:
            async for event in events:
                if isinstance(event, ThreadStarted):
                    local_thread_id = make_local_thread_id(
                        run_id,
                        thread_role,
                        event.thread_id,
                    )
                    provenance = Provenance(
                        source="codex",
                        source_id=local_thread_id,
                        model=run.config.model,
                        runtime_version=self._runtime_version,
                        prompt_version=PROMPT_VERSIONS[prompt_role],
                        captured_at=self._clock(),
                    )
                    self._store.add_thread(
                        local_thread_id,
                        run_id=run_id,
                        role=thread_role,
                        model=run.config.model,
                        provenance=provenance,
                        external_thread_id=event.thread_id,
                        execution=self._execution(run_id),
                        runtime_lifecycle=True,
                    )
                elif isinstance(event, TurnStarted):
                    turn = event.turn
                    self._active_turns[run_id].add(turn)
                    self._store.append_event(
                        run_id,
                        event_type="runtime.turn_started",
                        stage=self._store.get_run(run_id).stage,
                        payload={
                            "thread_id": event.turn.thread_id,
                            "turn_id": event.turn.turn_id,
                            "backend": event.turn.backend.value,
                            "turn_input_id": prepared.id,
                            "attempt": attempt,
                        },
                        execution=self._execution(run_id),
                    )
                elif isinstance(event, TokenUsageUpdated):
                    if turn is not None and (
                        event.thread_id != turn.thread_id
                        or event.turn_id != turn.turn_id
                    ):
                        raise WorkflowExecutionError(
                            f"{schema.__name__} received usage for another turn"
                        )
                    usage_observed = True
                    total = event.usage.input_tokens + event.usage.output_tokens
                    self._usage_by_turn[
                        (run_id, event.thread_id, event.turn_id)
                    ] = total
                    self._store.append_event(
                        run_id,
                        event_type="runtime.token_usage",
                        stage=self._store.get_run(run_id).stage,
                        payload={
                            "thread_id": event.thread_id,
                            "turn_id": event.turn_id,
                            "usage": event.usage.model_dump(mode="json"),
                        },
                        execution=self._execution(run_id),
                    )
                    if (
                        sum(
                            value
                            for (event_run_id, _, _), value in self._usage_by_turn.items()
                            if event_run_id == run_id
                        )
                        > run.config.budgets.max_tokens
                    ):
                        pending_runtime_error = WorkflowExecutionError(
                            "run token budget exceeded"
                        )
                        if turn is not None:
                            await self._interrupt_turn(turn)
                        continue
                elif isinstance(event, ItemCompleted):
                    if local_thread_id is None or turn is None:
                        raise WorkflowExecutionError(
                            f"{schema.__name__} received an item before turn start"
                        )
                    if (
                        event.thread_id != turn.thread_id
                        or event.turn_id != turn.turn_id
                    ):
                        raise WorkflowExecutionError(
                            f"{schema.__name__} received an item for another turn"
                        )
                    counts_as_query = counts_as_search_query(event)
                    stage = self._store.get_run(run_id).stage
                    self._store.append_event(
                        run_id,
                        event_type="runtime.item_completed",
                        stage=stage,
                        payload={
                            "thread_id": event.thread_id,
                            "turn_id": event.turn_id,
                            "item_id": event.item_id,
                            "item_type": event.item_type,
                            "counts_as_search_query": counts_as_query,
                        },
                        execution=self._execution(run_id),
                    )
                    observation = web_search_observation(
                        event,
                        run_id=run_id,
                        local_thread_id=local_thread_id,
                        turn=turn,
                        captured_at=event.completed_at or self._clock(),
                    )
                    if observation is not None:
                        if request.web_search is WebSearchMode.DISABLED:
                            raise WorkflowExecutionError(
                                "runtime emitted a web action for an offline turn"
                            )
                        persisted = self._store.add_web_search_observation(
                            observation,
                            execution=self._execution(run_id),
                        )
                        observation_ids.append(persisted.id)
                    if counts_as_query:
                        key = (run_id, stage)
                        self._queries_by_stage[key] = self._queries_by_stage.get(key, 0) + 1
                        if request.web_search is WebSearchMode.DISABLED:
                            pending_runtime_error = WorkflowExecutionError(
                                "runtime emitted a search query for an offline turn"
                            )
                            if turn is not None:
                                await self._interrupt_turn(turn)
                            continue
                        if (
                            self._queries_by_stage[key]
                            > run.config.search.max_queries_per_stage
                        ):
                            pending_runtime_error = WorkflowExecutionError(
                                f"{stage.value} search query budget exceeded"
                            )
                            if turn is not None:
                                await self._interrupt_turn(turn)
                            continue
                elif isinstance(event, UnknownNotification):
                    self._store.append_event(
                        run_id,
                        event_type="runtime.unknown_notification",
                        stage=self._store.get_run(run_id).stage,
                        payload={"method": event.method},
                        execution=self._execution(run_id),
                    )
                elif isinstance(event, RuntimeErrorEvent):
                    diagnostic_id = f"diag-{uuid4().hex}"
                    _LOGGER.warning(
                        "runtime.error",
                        diagnostic_id=diagnostic_id,
                        retryable=event.retryable,
                        output_schema=schema.__name__,
                        message_length=len(event.message),
                    )
                    if event.retryable:
                        pending_runtime_error = _RetryableTurnError(
                            f"{schema.__name__} runtime error ({diagnostic_id})",
                            reason_code="runtime_error",
                            diagnostic_id=diagnostic_id,
                        )
                    else:
                        pending_runtime_error = WorkflowExecutionError(
                            f"{schema.__name__} runtime error ({diagnostic_id})"
                        )
                    self._store.append_event(
                        run_id,
                        event_type="runtime.error",
                        stage=self._store.get_run(run_id).stage,
                        payload={
                            "code": "runtime_error",
                            "diagnostic_id": diagnostic_id,
                            "retryable": event.retryable,
                        },
                        execution=self._execution(run_id),
                    )
                    if not event.retryable and turn is not None:
                        await self._interrupt_turn(turn)
                elif isinstance(event, TurnCompleted):
                    self._store.record_turn_completed(
                        run_id,
                        payload={
                            "thread_id": event.turn.thread_id,
                            "turn_id": event.turn.turn_id,
                            "backend": event.turn.backend.value,
                            "status": event.status,
                        },
                        execution=self._execution(run_id),
                    )
                    self._active_turns[run_id].discard(event.turn)
                    self._unconfirmed_turns[run_id].discard(event.turn)
                    self._interrupting_turns.discard(event.turn)
                    if local_thread_id is None or provenance is None:
                        raise WorkflowExecutionError(
                            f"{schema.__name__} completed before thread start"
                        )
                    if event.status != "completed":
                        if self._store.get_run(run_id).status is RunStatus.CANCELLING:
                            raise _WorkflowCancelled(f"{schema.__name__} turn interrupted")
                        if pending_runtime_error is not None:
                            self._store.transition_thread(
                                local_thread_id,
                                ThreadStatus.FAILED,
                                execution=self._execution(run_id),
                                runtime_lifecycle=True,
                            )
                            raise pending_runtime_error
                        target = (
                            ThreadStatus.CANCELLED
                            if event.status == "interrupted"
                            else ThreadStatus.FAILED
                        )
                        self._store.transition_thread(
                            local_thread_id,
                            target,
                            execution=self._execution(run_id),
                            runtime_lifecycle=True,
                        )
                        if event.status == "interrupted":
                            raise _WorkflowCancelled(f"{schema.__name__} turn interrupted")
                        raise _RetryableTurnError(
                            f"{schema.__name__} turn failed",
                            reason_code="runtime_terminal_failed",
                        )
                    if pending_runtime_error is not None and not isinstance(
                        pending_runtime_error,
                        _RetryableTurnError,
                    ):
                        self._store.transition_thread(
                            local_thread_id,
                            ThreadStatus.FAILED,
                            execution=self._execution(run_id),
                            runtime_lifecycle=True,
                        )
                        raise pending_runtime_error
                    if event.output is None:
                        self._store.transition_thread(
                            local_thread_id,
                            ThreadStatus.FAILED,
                            execution=self._execution(run_id),
                            runtime_lifecycle=True,
                        )
                        raise WorkflowExecutionError(f"{schema.__name__} completed without output")
                    if not usage_observed:
                        self._store.transition_thread(
                            local_thread_id,
                            ThreadStatus.FAILED,
                            execution=self._execution(run_id),
                            runtime_lifecycle=True,
                        )
                        raise WorkflowExecutionError(
                            f"{schema.__name__} completed without token usage"
                        )
                    try:
                        parsed = event.parse_output_as(schema)
                    except (ValidationError, ValueError) as error:
                        diagnostic_id = f"diag-{uuid4().hex}"
                        _LOGGER.warning(
                            "runtime.output_validation_failed",
                            diagnostic_id=diagnostic_id,
                            output_schema=schema.__name__,
                            error_type=type(error).__name__,
                            error_count=(
                                error.error_count()
                                if isinstance(error, ValidationError)
                                else 1
                            ),
                        )
                        self._store.transition_thread(
                            local_thread_id,
                            ThreadStatus.FAILED,
                            execution=self._execution(run_id),
                            runtime_lifecycle=True,
                        )
                        raise _RetryableTurnError(
                            f"{schema.__name__} output failed schema validation "
                            f"({diagnostic_id})",
                            reason_code="output_schema_validation",
                            diagnostic_id=diagnostic_id,
                        ) from error
                    self._store.transition_thread(
                        local_thread_id,
                        ThreadStatus.COMPLETED,
                        execution=self._execution(run_id),
                        runtime_lifecycle=True,
                    )
                    return _TurnResult(
                        output=parsed,
                        thread_id=local_thread_id,
                        external_thread_id=event.turn.thread_id,
                        provenance=provenance,
                        observation_ids=tuple(observation_ids),
                    )
        except asyncio.CancelledError:
            if turn is not None:
                with suppress(Exception):
                    await self._interrupt_turn(turn)
                self._record_unconfirmed_turn(run_id, turn, reason="stream_cancelled")
                self._interrupting_turns.discard(turn)
            else:
                self._record_unconfirmed_start(
                    run_id,
                    prepared,
                    attempt=attempt,
                    backend_preference=request.runtime,
                    reason="stream_cancelled_before_turn_start",
                )
            raise
        except BaseException:
            if turn is None:
                self._record_unconfirmed_start(
                    run_id,
                    prepared,
                    attempt=attempt,
                    backend_preference=request.runtime,
                    reason="stream_failed_before_turn_start",
                )
            await self._fail_active_turn(run_id, turn, local_thread_id)
            raise
        else:
            await self._fail_active_turn(run_id, turn, local_thread_id)
            if turn is None:
                if local_thread_id is None:
                    self._store.append_event(
                        run_id,
                        event_type="runtime.turn_not_started",
                        stage=self._store.get_run(run_id).stage,
                        payload={
                            "turn_input_id": prepared.id,
                            "attempt": attempt,
                            "backend_preference": request.runtime.value,
                            "reason": "stream_ended_before_thread_start",
                        },
                        execution=self._execution(run_id),
                    )
                    raise _RetryableTurnError(
                        f"{schema.__name__} stream ended before thread start",
                        reason_code="runtime_turn_not_started",
                    )
                self._record_unconfirmed_start(
                    run_id,
                    prepared,
                    attempt=attempt,
                    backend_preference=request.runtime,
                    reason="stream_ended_before_turn_start",
                )
                raise WorkflowExecutionError(
                    f"{schema.__name__} stream ended before turn start confirmation"
                )
            if turn is not None and turn in self._unconfirmed_turns[run_id]:
                raise WorkflowExecutionError(
                    f"{schema.__name__} stream ended without terminal confirmation"
                )
            if pending_runtime_error is not None:
                raise pending_runtime_error
            raise _RetryableTurnError(
                f"{schema.__name__} stream ended without completion",
                reason_code="runtime_stream_ended",
            )
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()

    async def _settle_turn_task(
        self,
        run_id: str,
        task: asyncio.Task[_TurnResult],
    ) -> None:
        """Interrupt an active turn while its shielded event pump owns the stream."""

        timeout = min(5, self._store.get_run(run_id).config.budgets.stage_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not task.done() and loop.time() < deadline:
            await self._interrupt_active_turns(run_id)
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait({task}, timeout=min(0.05, remaining))
            if done:
                break
        if not task.done():
            self._retain_reconciliation_task(run_id, task)
            return
        await asyncio.gather(task, return_exceptions=True)

    def _retain_reconciliation_task(
        self,
        run_id: str,
        task: asyncio.Task[_TurnResult],
    ) -> None:
        tasks = self._reconciliation_tasks[run_id]
        if task in tasks:
            return
        tasks.add(task)

        def finish(completed: asyncio.Task[_TurnResult]) -> None:
            self._finish_reconciliation_task(run_id, completed)

        task.add_done_callback(finish)

    def _finish_reconciliation_task(
        self,
        run_id: str,
        task: asyncio.Task[_TurnResult],
    ) -> None:
        with suppress(BaseException):
            task.result()
        tasks = self._reconciliation_tasks.get(run_id)
        if tasks is None:
            return
        tasks.discard(task)
        if tasks:
            return
        self._reconciliation_tasks.pop(run_id, None)
        token = self._execution_tokens.get(run_id)
        if token is None:
            return
        if self._store.has_unconfirmed_runtime_turns(run_id):
            self._execution_tokens.pop(run_id, None)
            return
        self._execution_tokens.pop(run_id, None)
        if self._store.get_run(run_id).status is RunStatus.CANCELLING:
            try:
                self._acknowledge_cancel(run_id)
                return
            except (ConflictError, InvalidTransitionError):
                pass
        with suppress(ConflictError):
            self._store.release_execution(token)

    async def _interrupt_turn(self, turn: TurnRef) -> None:
        if turn in self._interrupting_turns:
            return
        self._interrupting_turns.add(turn)
        try:
            await self._runtime.interrupt(turn)
        except BaseException:
            self._interrupting_turns.discard(turn)
            raise

    async def _interrupt_active_turns(self, run_id: str) -> None:
        active = tuple(self._active_turns.get(run_id, ()))
        if active:
            timeout = min(5, self._store.get_run(run_id).config.budgets.stage_seconds)
            with suppress(Exception, TimeoutError):
                async with asyncio.timeout(timeout):
                    await asyncio.gather(
                        *(self._interrupt_turn(turn) for turn in active),
                        return_exceptions=True,
                    )

    async def _fail_active_turn(
        self,
        run_id: str,
        turn: TurnRef | None,
        local_thread_id: str | None,
    ) -> None:
        if turn is not None and turn in self._active_turns[run_id]:
            timeout = min(5, self._store.get_run(run_id).config.budgets.stage_seconds)
            with suppress(Exception, TimeoutError):
                async with asyncio.timeout(timeout):
                    await self._interrupt_turn(turn)
            self._record_unconfirmed_turn(run_id, turn, reason="stream_ended")
            self._interrupting_turns.discard(turn)
            return
        if local_thread_id is None:
            return
        thread = self._store.get_thread(local_thread_id)
        if (
            thread.status is ThreadStatus.ACTIVE
            and self._store.get_run(run_id).status is RunStatus.RUNNING
        ):
            self._store.transition_thread(
                local_thread_id,
                ThreadStatus.FAILED,
                execution=self._execution(run_id),
            )

    def _record_unconfirmed_turn(
        self,
        run_id: str,
        turn: TurnRef,
        *,
        reason: str,
    ) -> None:
        self._unconfirmed_turns[run_id].add(turn)
        if any(
            event.event_type == "runtime.turn_terminal_unconfirmed"
            and event.payload.get("thread_id") == turn.thread_id
            and event.payload.get("turn_id") == turn.turn_id
            for event in self._store.list_events(run_id)
        ):
            return
        self._store.record_turn_terminal_unconfirmed(
            run_id,
            payload={
                "thread_id": turn.thread_id,
                "turn_id": turn.turn_id,
                "backend": turn.backend.value,
                "reason": reason,
            },
            execution=self._execution(run_id),
        )

    def _record_unconfirmed_start(
        self,
        run_id: str,
        prepared: _PreparedTurnInput,
        *,
        attempt: int,
        backend_preference: RuntimePreference,
        reason: str,
    ) -> None:
        if any(
            event.event_type == "runtime.turn_start_unconfirmed"
            and event.payload.get("turn_input_id") == prepared.id
            and event.payload.get("attempt") == attempt
            for event in self._store.list_events(run_id)
        ):
            return
        self._store.record_turn_start_unconfirmed(
            run_id,
            payload={
                "turn_input_id": prepared.id,
                "attempt": attempt,
                "backend_preference": backend_preference.value,
                "reason": reason,
            },
            execution=self._execution(run_id),
        )

    def _acknowledge_cancel(self, run_id: str) -> RunRecord:
        run = self._store.get_run(run_id)
        if run.status is RunStatus.CANCELLING:
            run = self._store.acknowledge_cancel(
                run_id,
                execution=self._execution_tokens.get(run_id),
            )
        if run.status is RunStatus.CANCELLED:
            terminal_turns = set(self._active_turns.get(run_id, set())) | set(
                self._unconfirmed_turns.get(run_id, set())
            )
            self._active_turns.pop(run_id, None)
            self._unconfirmed_turns.pop(run_id, None)
            self._interrupting_turns.difference_update(terminal_turns)
        return run

    def _ensure_stage(self, run_id: str, target: RunStage) -> None:
        current = self._store.get_run(run_id).stage
        if current is target:
            return
        self._store.transition_stage(run_id, target, execution=self._execution(run_id))

    def _load_run_input(self, run_id: str) -> RunInput:
        value = self._store.get_run_input(run_id)
        run = self._store.get_run(run_id)
        if value.sha256 != run.input_sha256:
            raise WorkflowExecutionError("frozen run input hash does not match run")
        return value

    def _find_output(self, run_id: str, kind: str) -> StageOutputRecord | None:
        matches = tuple(
            output for output in self._store.list_stage_outputs(run_id) if output.kind == kind
        )
        if len(matches) > 1:
            raise WorkflowExecutionError(f"multiple durable outputs found for {kind}")
        return matches[0] if matches else None

    def _put_output(
        self,
        run_id: str,
        stage: RunStage,
        kind: str,
        content: JsonValue,
        provenance: Provenance,
    ) -> StageOutputRecord:
        existing = self._find_output(run_id, kind)
        if existing is not None:
            if existing.content != content:
                raise WorkflowExecutionError(f"durable output conflicts for {kind}")
            return existing
        identity: dict[str, JsonValue] = {
            "run_id": run_id,
            "kind": kind,
            "content": content,
        }
        return self._store.add_stage_output(
            f"output-{canonical_sha256(identity)[:24]}",
            run_id=run_id,
            stage=stage,
            kind=kind,
            content=content,
            provenance=provenance,
            execution=self._execution(run_id),
        )

    def _register_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        media_type: str,
        content_sha256: str,
        size_bytes: int,
        relative_path: str,
        provenance: Provenance,
    ) -> ArtifactRecord:
        matches = tuple(
            artifact for artifact in self._store.list_artifacts(run_id) if artifact.kind == kind
        )
        if matches:
            existing = matches[0]
            if existing.sha256 != content_sha256 or existing.relative_path != relative_path:
                raise WorkflowExecutionError(f"artifact conflicts for {kind}")
            return existing
        identity: dict[str, JsonValue] = {
            "run_id": run_id,
            "kind": kind,
            "sha256": content_sha256,
        }
        return self._store.add_artifact(
            f"artifact-{canonical_sha256(identity)[:24]}",
            run_id=run_id,
            kind=kind,
            media_type=media_type,
            sha256=content_sha256,
            size_bytes=size_bytes,
            provenance=provenance,
            relative_path=relative_path,
            execution=self._execution(run_id),
        )

    def _application_provenance(
        self,
        config: QEDConfig,
        *,
        source_id: str,
        prompt_version: str,
    ) -> Provenance:
        return Provenance(
            source="qed",
            source_id=source_id,
            model=config.model,
            runtime_version=self._runtime_version,
            prompt_version=prompt_version,
            captured_at=self._clock(),
        )

    def _mark_failed(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        if run.status is RunStatus.RUNNING:
            try:
                self._store.transition_run(
                    run_id,
                    RunStatus.FAILED,
                    execution=self._execution_tokens.get(run_id),
                )
            except ConflictError:
                return

    def _execution(self, run_id: str) -> ExecutionToken:
        try:
            return self._execution_tokens[run_id]
        except KeyError as error:
            raise WorkflowExecutionError(f"run {run_id} has no active execution lease") from error

    def _restore_token_usage(self, run_id: str) -> None:
        for key in tuple(self._usage_by_turn):
            if key[0] == run_id:
                del self._usage_by_turn[key]
        for event in self._store.list_events(run_id):
            if event.event_type != "runtime.token_usage":
                continue
            turn_id = event.payload.get("turn_id")
            thread_id = event.payload.get("thread_id")
            usage = event.payload.get("usage")
            if (
                not isinstance(thread_id, str)
                or not isinstance(turn_id, str)
                or not isinstance(usage, dict)
            ):
                raise WorkflowExecutionError("persisted token usage event is invalid")
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if (
                type(input_tokens) is not int
                or input_tokens < 0
                or type(output_tokens) is not int
                or output_tokens < 0
            ):
                raise WorkflowExecutionError("persisted token usage event is invalid")
            self._usage_by_turn[(run_id, thread_id, turn_id)] = (
                input_tokens + output_tokens
            )

        used = sum(
            value
            for (event_run_id, _, _), value in self._usage_by_turn.items()
            if event_run_id == run_id
        )
        if used > self._store.get_run(run_id).config.budgets.max_tokens:
            raise WorkflowExecutionError("run token budget exceeded")

    def _restore_search_query_usage(self, run_id: str) -> None:
        for key in tuple(self._queries_by_stage):
            if key[0] == run_id:
                del self._queries_by_stage[key]
        for event in self._store.list_events(run_id):
            if (
                event.event_type == "runtime.item_completed"
                and event.payload.get("counts_as_search_query") is True
            ):
                key = (run_id, RunStage(event.stage))
                self._queries_by_stage[key] = self._queries_by_stage.get(key, 0) + 1
        maximum = self._store.get_run(run_id).config.search.max_queries_per_stage
        if any(
            count > maximum
            for (event_run_id, _), count in self._queries_by_stage.items()
            if event_run_id == run_id
        ):
            raise WorkflowExecutionError("run search query budget exceeded")

    async def _heartbeat_execution(self, run_id: str, execution: ExecutionToken) -> None:
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()
        while True:
            await asyncio.sleep(0.1)
            run = self._store.get_run(run_id)
            if run.status is RunStatus.CANCELLING:
                await self._interrupt_active_turns(run_id)
                return
            if run.status in {
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.COMPLETED,
            }:
                return
            now = loop.time()
            if now - last_heartbeat >= 20:
                self._store.heartbeat_execution(execution, lease_seconds=60)
                last_heartbeat = now
