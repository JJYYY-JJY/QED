"""Application service shared by QED's HTTP and command-line interfaces."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from qed.config import QEDConfig
from qed.inputs import RunInput
from qed.logging import get_logger
from qed.migration import ImportedLegacyRun, import_legacy_run
from qed.runtime import CodexRuntime, MockRuntime, RuntimeCapabilities
from qed.schemas import Event
from qed.service_settings import ServiceSettings
from qed.store import (
    ConflictError,
    RunRecord,
    RunSnapshot,
    RunStatus,
    RunStore,
    StoreInfo,
)
from qed.workflow import ResearchWorkflow

RuntimeFactory = Callable[[Path], CodexRuntime]
_LOGGER = get_logger(__name__)
_COMMAND_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WorkflowService(Protocol):
    """Public orchestration seam consumed by transports and the supervisor."""

    def create_run(
        self,
        run_input: RunInput,
        config: QEDConfig,
        *,
        run_id: str,
    ) -> RunRecord: ...

    async def execute(self, run_id: str) -> RunRecord: ...

    async def cancel(self, run_id: str) -> RunRecord: ...

    async def resume(self, run_id: str, *, idempotency_key: str) -> RunRecord: ...


def _default_mock_runtime() -> MockRuntime:
    def verification_response(request: Any) -> dict[str, Any]:
        evidence_ids: list[str] = []
        if request.role.value == "citation":
            start = request.prompt.index('<frozen-input encoding="canonical-json">')
            start = request.prompt.index("\n", start) + 1
            end = request.prompt.index("\n</frozen-input>", start)
            payload = json.loads(request.prompt[start:end])
            evidence_ids = [item["id"] for item in payload["evidence"]]
        return {
            "schema_version": 1,
            "checks": [
                {
                    "id": "mock-check",
                    "category": "fixture-integrity",
                    "status": "pass",
                    "summary": (
                        "The deterministic mock fixture is internally consistent."
                    ),
                    "evidence_ids": evidence_ids,
                }
            ],
        }

    return MockRuntime(
        capabilities=RuntimeCapabilities(
            model="gpt-5.6-sol",
            advertised_efforts=("high",),
            default_effort="high",
            selected_effort="high",
            multi_agent=False,
            proactive_multi_agent=False,
        ),
        responses={
            "EvidenceBatch": {
                "schema_version": 1,
                "items": [
                    {
                        "kind": "note",
                        "title": "Mock evidence",
                        "content": "A deterministic fixture for local workflow validation.",
                    }
                ],
            },
            "PlanDraft": {
                "schema_version": 1,
                "strategy": "Apply the deterministic mock argument.",
                "steps": [
                    {
                        "id": "mock-step",
                        "statement": "Establish the requested conclusion.",
                        "rationale": "This fixture exercises orchestration, not mathematics.",
                        "success_criteria": ["The conclusion is stated."],
                    }
                ],
            },
            "ProofDraft": {
                "schema_version": 1,
                "proof": "Deterministic mock proof for end-to-end system validation.",
            },
            "VerificationDraft": verification_response,
            "AdjudicationDraft": {
                "schema_version": 1,
                "outcome": "accept",
                "rationale": "Every required mock verification report passed.",
            },
        },
    )


def build_service(
    settings: ServiceSettings,
    *,
    runtime_factory: RuntimeFactory | None = None,
    runtime_version: str | None = None,
) -> ApplicationService:
    """Construct managed state with a mock runtime unless a factory is explicit."""

    if runtime_factory is None:
        runtime: CodexRuntime = _default_mock_runtime()
        selected_version = runtime_version or "mock-runtime/1"
    else:
        runtime = runtime_factory(settings.codex_home)
        observed_version = getattr(runtime, "runtime_version", None)
        if runtime_version is None and (
            not isinstance(observed_version, str) or not observed_version.strip()
        ):
            raise ValueError(
                "a non-mock runtime must report its observed runtime_version"
            )
        selected_version = cast(str, runtime_version or observed_version)
    store = RunStore(settings.database_path)
    workflow = ResearchWorkflow(
        store,
        runtime,
        runtime_version=selected_version,
        export_root=settings.data_root / "exports",
    )
    return ApplicationService(
        store=store,
        workflow=workflow,
        runtime=runtime,
        managed_root=settings.data_root,
    )


class RunAlreadyActiveError(RuntimeError):
    """Raised when a second worker is requested for an active run."""


class CommandReceipt(BaseModel):
    """Stable acknowledgement returned for an accepted run command."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    command: Literal["start", "cancel", "resume"]
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    accepted: Literal[True] = True
    status: RunStatus


class StreamHeartbeat(BaseModel):
    """Typed liveness marker for transports that keep event streams open."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    occurred_at: datetime


class ApplicationService:
    """Expose durable research operations without coupling callers to transports."""

    def __init__(
        self,
        *,
        store: RunStore,
        workflow: WorkflowService,
        runtime: CodexRuntime,
        managed_root: str | Path | None = None,
    ) -> None:
        self._store = store
        self._workflow = workflow
        self._runtime = runtime
        self._managed_root = Path(managed_root) if managed_root is not None else store.path.parent
        self._closed = False
        self._workers: dict[str, asyncio.Task[RunRecord]] = {}
        self._receipts: dict[tuple[str, str, str], CommandReceipt] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._run_capacity = asyncio.Condition()
        self._executing_run_limits: dict[str, int] = {}
        self._queued_runs: set[str] = set()

    def create_run(
        self,
        run_input: RunInput,
        config: QEDConfig,
        *,
        run_id: str,
    ) -> RunRecord:
        created = self._workflow.create_run(run_input, config, run_id=run_id)
        _LOGGER.info("run.created", run_id=run_id)
        return created

    def get_run(self, run_id: str) -> RunRecord:
        return self._store.get_run(run_id)

    def list_runs(self) -> tuple[RunRecord, ...]:
        return self._store.list_runs()

    def snapshot(self, run_id: str) -> RunSnapshot:
        return self._store.snapshot(run_id)

    def store_info(self) -> StoreInfo:
        return self._store.info()

    def migrate_legacy(self, source: str | Path) -> ImportedLegacyRun:
        imported = import_legacy_run(Path(source), self._managed_root)
        _LOGGER.info(
            "legacy.imported",
            import_id=imported.manifest.import_id,
            file_count=len(imported.manifest.files),
        )
        return imported

    async def stream_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        poll_interval: float = 0.1,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[Event | StreamHeartbeat]:
        """Replay durable events, then follow them until the run is terminal."""

        if after_seq < 0:
            raise ValueError("after_seq cannot be negative")
        if poll_interval <= 0 or heartbeat_interval <= 0:
            raise ValueError("stream intervals must be positive")
        self._store.get_run(run_id)
        cursor = after_seq
        loop = asyncio.get_running_loop()
        last_delivery = loop.time()
        terminal = {
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.COMPLETED,
        }

        while not self._closed:
            events = self._store.list_events(run_id, after_seq=cursor)
            for event in events:
                cursor = event.seq
                last_delivery = loop.time()
                yield event

            run = self._store.get_run(run_id)
            if run.status in terminal:
                tail = self._store.list_events(run_id, after_seq=cursor)
                for event in tail:
                    cursor = event.seq
                    yield event
                return

            now = loop.time()
            if now - last_delivery >= heartbeat_interval:
                last_delivery = now
                yield StreamHeartbeat(occurred_at=datetime.now(UTC))
            await asyncio.sleep(poll_interval)

    async def start_run(self, run_id: str, *, idempotency_key: str) -> CommandReceipt:
        """Schedule one worker, returning the same receipt for a repeated command."""

        return await self._schedule(
            run_id,
            command="start",
            idempotency_key=idempotency_key,
            operation_factory=lambda: self._workflow.execute(run_id),
        )

    async def cancel_run(self, run_id: str, *, idempotency_key: str) -> CommandReceipt:
        """Ask orchestration to interrupt active turns and persist cancellation."""

        if _COMMAND_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("idempotency_key has an invalid format")
        self._store.get_run(run_id)
        key = (run_id, "cancel", idempotency_key)
        async with self._run_lock(run_id):
            existing = self._receipts.get(key)
            if existing is not None:
                return existing
            claim = self._store.cancel_run_command(
                run_id,
                idempotency_key=idempotency_key,
            )
            receipt = CommandReceipt(
                run_id=run_id,
                command="cancel",
                idempotency_key=idempotency_key,
                status=RunStatus.CANCELLED,
            )
            if claim.replayed and (
                claim.run.status is not RunStatus.CANCELLING
                or claim.run.execution_version != claim.accepted_execution_version
            ):
                self._receipts[key] = receipt
                return receipt
            queued = self._workers.get(run_id)
            if (
                queued is not None
                and not queued.done()
                and run_id in self._queued_runs
            ):
                self._queued_runs.discard(run_id)
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)
            cancelled = await self._workflow.cancel(run_id)
            if cancelled.status is not RunStatus.CANCELLED:
                raise RuntimeError(
                    f"cancel command did not reach cancelled state: {cancelled.status.value}"
                )
            self._receipts[key] = receipt
            _LOGGER.info("run.command_accepted", run_id=run_id, command="cancel")
            return receipt

    async def resume_run(self, run_id: str, *, idempotency_key: str) -> CommandReceipt:
        """Schedule one durable resume attempt through orchestration."""

        return await self._schedule(
            run_id,
            command="resume",
            idempotency_key=idempotency_key,
            operation_factory=lambda: self._workflow.execute(run_id),
        )

    async def wait(self, run_id: str) -> RunRecord:
        """Wait for an in-process worker without changing durable run state."""

        task = self._workers.get(run_id)
        if task is None:
            return self._store.get_run(run_id)
        with suppress(Exception):
            await asyncio.shield(task)
        return self._store.get_run(run_id)

    async def _schedule(
        self,
        run_id: str,
        *,
        command: Literal["start", "resume"],
        idempotency_key: str,
        operation_factory: Callable[[], Coroutine[Any, Any, RunRecord]],
    ) -> CommandReceipt:
        if _COMMAND_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("idempotency_key has an invalid format")
        self._store.get_run(run_id)
        key = (run_id, command, idempotency_key)
        async with self._run_lock(run_id):
            existing = self._receipts.get(key)
            if existing is not None:
                return existing
            active = self._workers.get(run_id)
            if active is not None and not active.done():
                raise RunAlreadyActiveError(f"run already has an active worker: {run_id}")

            claim = (
                self._store.start_run_command(
                    run_id,
                    idempotency_key=idempotency_key,
                )
                if command == "start"
                else self._store.resume_run_command(
                    run_id,
                    idempotency_key=idempotency_key,
                )
            )
            run = claim.run
            receipt = CommandReceipt(
                run_id=run_id,
                command=command,
                idempotency_key=idempotency_key,
                status=claim.accepted_status,
            )
            self._receipts[key] = receipt
            if claim.replayed and run.status is not RunStatus.RUNNING:
                return receipt

            self._queued_runs.add(run_id)
            task = asyncio.create_task(
                self._run_with_capacity(
                    run_id,
                    limit=run.config.parallelism.runs,
                    operation_factory=operation_factory,
                ),
                name=f"qed-{command}-{run_id}",
            )
            self._workers[run_id] = task
            task.add_done_callback(lambda completed: self._worker_done(run_id, completed))
            _LOGGER.info(
                "run.command_accepted",
                run_id=run_id,
                command=command,
            )
            return receipt

    async def _run_with_capacity(
        self,
        run_id: str,
        *,
        limit: int,
        operation_factory: Callable[[], Coroutine[Any, Any, RunRecord]],
    ) -> RunRecord:
        acquired_capacity = False
        try:
            async with self._run_capacity:
                await self._run_capacity.wait_for(lambda: self._has_run_capacity(limit))
                self._queued_runs.discard(run_id)
                self._executing_run_limits[run_id] = limit
                acquired_capacity = True
            try:
                return await operation_factory()
            finally:
                async with self._run_capacity:
                    self._executing_run_limits.pop(run_id, None)
                    self._run_capacity.notify_all()
        except asyncio.CancelledError:
            if not acquired_capacity:
                run = self._store.get_run(run_id)
                if run.status is RunStatus.RUNNING:
                    self._store.pause_unleased_run(run_id)
            raise
        finally:
            self._queued_runs.discard(run_id)

    def _has_run_capacity(self, requested_limit: int) -> bool:
        proposed_count = len(self._executing_run_limits) + 1
        return proposed_count <= requested_limit and all(
            proposed_count <= active_limit
            for active_limit in self._executing_run_limits.values()
        )

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._run_locks.setdefault(run_id, asyncio.Lock())

    def _worker_done(self, run_id: str, task: asyncio.Task[RunRecord]) -> None:
        self._queued_runs.discard(run_id)
        if self._workers.get(run_id) is task:
            del self._workers[run_id]
        if task.cancelled():
            _LOGGER.info("run.worker_stopped", run_id=run_id, reason="service_shutdown")
            return
        error = task.exception()
        if error is None:
            _LOGGER.info("run.worker_finished", run_id=run_id)
        else:
            _LOGGER.error(
                "run.worker_failed",
                run_id=run_id,
                error_type=type(error).__name__,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for run_id in tuple(self._queued_runs):
            if self._store.get_run(run_id).status is RunStatus.RUNNING:
                with suppress(ConflictError):
                    self._store.pause_unleased_run(run_id)
        tasks = tuple(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._queued_runs.clear()
        self._run_locks.clear()
        try:
            workflow_close = getattr(self._workflow, "close", None)
            if workflow_close is not None:
                await workflow_close()
        finally:
            try:
                await self._runtime.close()
            finally:
                self._store.close()
        _LOGGER.info("service.closed")
