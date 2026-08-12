from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from qed.config import BudgetPolicy, QEDConfig
from qed.inputs import RunInput
from qed.runtime import (
    CapabilityRequest,
    MockRuntime,
    RunRequest,
    RuntimeBackend,
    RuntimeCapabilities,
    RuntimeEvent,
    ThreadStarted,
    TurnCompleted,
    TurnRef,
    TurnStarted,
)
from qed.schemas import Provenance
from qed.service import (
    ApplicationService,
    RunAlreadyActiveError,
    StreamHeartbeat,
    build_management_service,
    build_service,
)
from qed.service_settings import ServiceSettings
from qed.store import ExecutionToken, RunRecord, RunStatus, RunStore
from qed.workflow import ResearchWorkflow
from tests.mock_service import build_mock_service


def _mock_runtime() -> MockRuntime:
    return MockRuntime(
        capabilities=RuntimeCapabilities(
            model="gpt-5.6-sol",
            advertised_efforts=("high",),
            default_effort="high",
            selected_effort="high",
            multi_agent=False,
            proactive_multi_agent=False,
        )
    )


async def test_management_service_cannot_start_research_execution(
    tmp_path: Path,
) -> None:
    service = build_management_service(ServiceSettings(data_root=tmp_path))
    try:
        assert service.store_info().journal_mode == "wal"
        with pytest.raises(RuntimeError, match="management-only"):
            service.create_run(
                RunInput(problem="Prove P."),
                QEDConfig(),
                run_id="run-management",
            )
    finally:
        await service.close()


@pytest.mark.parametrize("command", ["start", "resume", "cancel"])
async def test_management_commands_fail_before_mutating_run_state(
    tmp_path: Path,
    command: str,
) -> None:
    settings = ServiceSettings(data_root=tmp_path)
    with RunStore(settings.database_path) as store:
        created = store.create_run(
            f"run-management-{command}",
            config=QEDConfig(),
            run_input=RunInput(problem="Prove P."),
            provenance=Provenance(
                source="application",
                model="gpt-5.6-sol",
                runtime_version="test-runtime",
                captured_at=datetime(2026, 7, 17, tzinfo=UTC),
            ),
        )
        event_count = len(store.list_events(created.id))

    service = build_management_service(settings)
    try:
        method = getattr(service, f"{command}_run")
        with pytest.raises(RuntimeError, match="management-only"):
            await method(created.id, idempotency_key=f"{command}-key")
        assert service.get_run(created.id).status is RunStatus.CREATED
        assert len(service.snapshot(created.id).events) == event_count
    finally:
        await service.close()


class BlockingRuntime:
    def __init__(self) -> None:
        self.runtime_version = "fixture-runtime/1"
        self.requests: list[RunRequest] = []
        self.turn_started = asyncio.Event()
        self.release = asyncio.Event()
        self.interruptions: list[TurnRef] = []
        self._turn: TurnRef | None = None

    async def probe(self, request: CapabilityRequest) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            model=request.model,
            advertised_efforts=("high",),
            default_effort="high",
            selected_effort="high",
            multi_agent=False,
            proactive_multi_agent=False,
        )

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        thread_id = "blocking-thread"
        turn = TurnRef(
            thread_id=thread_id,
            turn_id="blocking-turn",
            backend=RuntimeBackend.MOCK,
        )
        self._turn = turn
        yield ThreadStarted(thread_id=thread_id, backend=RuntimeBackend.MOCK)
        yield TurnStarted(turn=turn)
        self.turn_started.set()
        await self.release.wait()
        if turn in self.interruptions:
            yield TurnCompleted(turn=turn, status="interrupted")

    async def interrupt(self, turn: TurnRef) -> None:
        self.interruptions.append(turn)
        self.release.set()

    async def close(self) -> None:
        self.release.set()


class ClosingRuntime(BlockingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0

    def runtime_resolution(
        self,
        capabilities: RuntimeCapabilities,
        *,
        requested_effort: str,
        config_sha256: str,
        prompt_sha256: str,
        schema_sha256: str,
        requested_backend: object | None = None,
    ) -> dict[str, object]:
        del requested_backend
        return {
            "schema_version": 1,
            "model_provider": "OpenAI",
            "model": capabilities.model,
            "model_version": "fixture-model-version",
            "backend": "fixture",
            "codex_runtime_version": self.runtime_version,
            "codex_cli_version": "fixture-cli/1",
            "sdk_version": "fixture-sdk/1",
            "app_server_version": "fixture-app-server/1",
            "requested_effort": requested_effort,
            "selected_effort": capabilities.selected_effort,
            "model_catalog_sha256": "0" * 64,
            "config_sha256": config_sha256,
            "prompt_sha256": prompt_sha256,
            "schema_sha256": schema_sha256,
            "executable_sha256": "1" * 64,
            "capability_response_sha256": "2" * 64,
            "protocol_version": "qed-fixture-protocol-v1",
        }

    async def close(self) -> None:
        self.close_count += 1
        await super().close()


class LateCloseRuntime(BlockingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_emitted = asyncio.Event()

    async def stream(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        turn = TurnRef(
            thread_id="late-close-thread",
            turn_id="late-close-turn",
            backend=RuntimeBackend.MOCK,
        )
        self._turn = turn
        yield ThreadStarted(thread_id=turn.thread_id, backend=RuntimeBackend.MOCK)
        yield TurnStarted(turn=turn)
        self.turn_started.set()
        await self.release.wait()
        await asyncio.sleep(1.1)
        self.terminal_emitted.set()
        yield TurnCompleted(turn=turn, status="interrupted")


class BoundedWorkflow:
    def __init__(self, store: RunStore, delegate: ResearchWorkflow) -> None:
        self._store = store
        self._delegate = delegate
        self.started: list[str] = []
        self.active = 0
        self.max_active = 0
        self.two_started = asyncio.Event()
        self.three_started = asyncio.Event()
        self.release: asyncio.Queue[None] = asyncio.Queue()

    def create_run(
        self,
        run_input: RunInput,
        config: QEDConfig,
        *,
        run_id: str,
    ) -> RunRecord:
        return self._delegate.create_run(run_input, config, run_id=run_id)

    async def execute(self, run_id: str) -> RunRecord:
        self.started.append(run_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if len(self.started) >= 2:
            self.two_started.set()
        if len(self.started) >= 3:
            self.three_started.set()
        try:
            await self.release.get()
            return self._store.get_run(run_id)
        finally:
            self.active -= 1

    async def cancel(self, run_id: str) -> RunRecord:
        return self._store.get_run(run_id)

    async def resume(self, run_id: str, *, idempotency_key: str) -> RunRecord:
        return await self.execute(run_id)


async def test_created_run_is_available_in_list_and_snapshot(tmp_path: Path) -> None:
    runtime = _mock_runtime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)

    created = service.create_run(
        RunInput(problem="Prove that there are infinitely many primes."),
        QEDConfig(),
        run_id="run-service-1",
    )

    assert service.get_run(created.id) == created
    assert service.list_runs() == (created,)
    assert service.snapshot(created.id).run == created

    await service.close()


async def test_configured_run_parallelism_bounds_concurrent_workers(
    tmp_path: Path,
) -> None:
    runtime = _mock_runtime()
    store = RunStore(tmp_path / "qed.sqlite3")
    delegate = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    workflow = BoundedWorkflow(store, delegate)
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    default_config = QEDConfig()
    config = default_config.model_copy(
        update={
            "parallelism": default_config.parallelism.model_copy(
                update={"runs": 2}
            )
        }
    )
    runs = tuple(
        service.create_run(
            RunInput(problem=f"Prove P{index}."),
            config,
            run_id=f"run-bounded-{index}",
        )
        for index in range(3)
    )

    for index, run in enumerate(runs):
        await service.start_run(run.id, idempotency_key=f"start-bounded-{index}")

    await asyncio.wait_for(workflow.two_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert len(workflow.started) == 2
    assert workflow.max_active == 2

    workflow.release.put_nowait(None)
    await asyncio.wait_for(workflow.three_started.wait(), timeout=1)
    workflow.release.put_nowait(None)
    workflow.release.put_nowait(None)
    await asyncio.gather(*(service.wait(run.id) for run in runs))

    assert workflow.max_active == 2

    await service.close()


async def test_cancelled_queued_run_never_waits_for_capacity(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    active = service.create_run(
        RunInput(problem="Prove active P."),
        QEDConfig(),
        run_id="run-active",
    )
    queued = service.create_run(
        RunInput(problem="Prove queued Q."),
        QEDConfig(),
        run_id="run-queued",
    )

    await service.start_run(active.id, idempotency_key="start-active")
    await asyncio.wait_for(runtime.turn_started.wait(), timeout=1)
    await service.start_run(queued.id, idempotency_key="start-queued")

    receipt = await asyncio.wait_for(
        service.cancel_run(queued.id, idempotency_key="cancel-queued"),
        timeout=1,
    )
    stopped = await asyncio.wait_for(service.wait(queued.id), timeout=1)

    assert receipt.status is RunStatus.CANCELLED
    assert stopped.status is RunStatus.CANCELLED
    assert len(runtime.requests) == 1

    await service.cancel_run(active.id, idempotency_key="cancel-active")
    await service.close()


async def test_service_shutdown_pauses_claimed_run_that_was_still_queued(
    tmp_path: Path,
) -> None:
    database = tmp_path / "qed.sqlite3"
    runtime = BlockingRuntime()
    store = RunStore(database)
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    active = service.create_run(
        RunInput(problem="Prove active P."),
        QEDConfig(),
        run_id="run-shutdown-active",
    )
    queued = service.create_run(
        RunInput(problem="Prove queued Q."),
        QEDConfig(),
        run_id="run-shutdown-queued",
    )
    await service.start_run(active.id, idempotency_key="start-shutdown-active")
    await asyncio.wait_for(runtime.turn_started.wait(), timeout=1)
    await service.start_run(queued.id, idempotency_key="start-shutdown-queued")

    await service.close()

    with RunStore(database) as reopened:
        assert reopened.get_run(queued.id).status is RunStatus.PAUSED
        assert reopened.get_run(queued.id).resumable is True


async def test_service_shutdown_pauses_queued_resume_with_execution_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "qed.sqlite3"
    runtime = BlockingRuntime()
    store = RunStore(database)
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    active = service.create_run(
        RunInput(problem="Prove active P."),
        QEDConfig(),
        run_id="run-resume-active",
    )
    queued = service.create_run(
        RunInput(problem="Prove queued Q."),
        QEDConfig(),
        run_id="run-resume-queued",
    )
    store.transition_run(queued.id, RunStatus.RUNNING)
    lease = store.acquire_execution(
        queued.id,
        segment_id="segment-resume-history",
        worker_id="historical-worker",
        lease_token="historical-secret",
    )
    token = ExecutionToken(
        segment_id=lease.id,
        version=lease.version,
        lease_token="historical-secret",
    )
    store.transition_run(queued.id, RunStatus.FAILED, execution=token)
    store.release_execution(token)

    await service.start_run(active.id, idempotency_key="start-resume-active")
    await asyncio.wait_for(runtime.turn_started.wait(), timeout=1)
    await service.resume_run(queued.id, idempotency_key="resume-queued")

    await service.close()

    with RunStore(database) as reopened:
        paused = reopened.get_run(queued.id)
        assert paused.status is RunStatus.PAUSED
        assert paused.resumable is True


async def test_service_uses_only_the_explicit_runtime_factory_and_closes_once(
    tmp_path: Path,
) -> None:
    runtime = ClosingRuntime()
    calls = 0
    codex_homes: list[Path] = []

    def factory(codex_home: Path) -> ClosingRuntime:
        nonlocal calls
        calls += 1
        codex_homes.append(codex_home)
        runtime.runtime_version = "observed-test-runtime"
        return runtime

    service = build_service(
        ServiceSettings(data_root=tmp_path),
        runtime_factory=factory,
    )
    service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-factory")

    await service.close()
    await service.close()

    assert calls == 1
    assert codex_homes == [tmp_path / "codex-home"]
    assert runtime.close_count == 1


def test_production_service_requires_an_explicit_runtime_factory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime_factory is required"):
        build_service(ServiceSettings(data_root=tmp_path))


async def test_fixture_service_is_test_only_and_records_fixture_provenance(
    tmp_path: Path,
) -> None:
    service = build_mock_service(ServiceSettings(data_root=tmp_path))

    created = service.create_run(
        RunInput(problem="Prove P."),
        QEDConfig(),
        run_id="run-explicit-mock",
    )

    assert created.runtime_version == "fixture-runtime/1"
    assert created.provenance.runtime_version == "fixture-runtime/1"

    await service.close()


async def test_service_close_drains_late_terminal_before_closing_store(
    tmp_path: Path,
) -> None:
    runtime = LateCloseRuntime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    limited = QEDConfig(budgets=BudgetPolicy(stage_seconds=1))
    run = service.create_run(
        RunInput(problem="Prove P."),
        limited,
        run_id="run-late-close",
    )
    await service.start_run(run.id, idempotency_key="start-late-close")
    await asyncio.wait_for(runtime.turn_started.wait(), timeout=1)

    await service.close()

    assert runtime.terminal_emitted.is_set()


async def test_start_is_idempotent_for_one_key_and_rejects_a_second_worker(
    tmp_path: Path,
) -> None:
    runtime = BlockingRuntime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    run = service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-1")

    first = await service.start_run(run.id, idempotency_key="start-command-1")
    await asyncio.wait_for(runtime.turn_started.wait(), timeout=1)
    repeated = await service.start_run(run.id, idempotency_key="start-command-1")

    assert repeated == first
    assert len(runtime.requests) == 1
    with pytest.raises(RunAlreadyActiveError):
        await service.start_run(run.id, idempotency_key="start-command-2")

    runtime.release.set()
    await service.wait(run.id)
    await service.close()


async def test_cancel_interrupts_through_workflow_without_cancelling_worker_task(
    tmp_path: Path,
) -> None:
    runtime = BlockingRuntime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    run = service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-cancel")
    await service.start_run(run.id, idempotency_key="start-cancel")
    await asyncio.wait_for(runtime.turn_started.wait(), timeout=1)

    cancelled = await service.cancel_run(run.id, idempotency_key="cancel-1")
    stopped = await service.wait(run.id)

    assert cancelled.command == "cancel"
    assert cancelled.status is RunStatus.CANCELLED
    assert stopped.status is RunStatus.CANCELLED
    assert len(runtime.interruptions) == 1

    await service.close()


async def test_failed_run_resume_is_scheduled_once_and_increments_durable_count(
    tmp_path: Path,
) -> None:
    runtime = _mock_runtime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    run = service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-resume")
    await service.start_run(run.id, idempotency_key="start-resume")
    failed = await service.wait(run.id)

    resumed = await service.resume_run(run.id, idempotency_key="resume-1")
    repeated = await service.resume_run(run.id, idempotency_key="resume-1")
    failed_again = await service.wait(run.id)

    assert failed.status is RunStatus.FAILED
    assert repeated == resumed
    assert resumed.command == "resume"
    assert failed_again.status is RunStatus.FAILED
    assert failed_again.resume_count == 1

    await service.close()


async def test_resume_receipt_replays_from_sqlite_before_state_precondition(
    tmp_path: Path,
) -> None:
    runtime = _mock_runtime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    workflow.create_run(
        RunInput(problem="Prove P."),
        QEDConfig(),
        run_id="run-durable-resume",
    )
    store.transition_run("run-durable-resume", RunStatus.RUNNING)
    store.transition_run("run-durable-resume", RunStatus.FAILED)
    command = store.resume_run_command(
        "run-durable-resume",
        idempotency_key="resume-durable-1",
    )
    store.transition_run("run-durable-resume", RunStatus.FAILED)
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)

    receipt = await service.resume_run(
        "run-durable-resume",
        idempotency_key="resume-durable-1",
    )

    assert command.accepted_status is RunStatus.FAILED
    assert receipt.status is RunStatus.FAILED
    assert service.get_run("run-durable-resume").resume_count == 1
    assert await service.wait("run-durable-resume") == service.get_run(
        "run-durable-resume"
    )

    await service.close()


async def test_replayed_cancel_key_cannot_cancel_a_later_execution(
    tmp_path: Path,
) -> None:
    runtime = _mock_runtime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    workflow.create_run(
        RunInput(problem="Prove P."),
        QEDConfig(),
        run_id="run-cancel-replay",
    )
    original = store.cancel_run_command(
        "run-cancel-replay",
        idempotency_key="cancel-stable-1",
    )
    store.acknowledge_cancel("run-cancel-replay")
    store.resume_run_command(
        "run-cancel-replay",
        idempotency_key="resume-after-cancel-1",
    )
    execution = store.acquire_execution(
        "run-cancel-replay",
        segment_id="segment-later",
        worker_id="worker-later",
        lease_token="later-secret",
        lease_seconds=60,
        runtime_version="test-runtime",
    )
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)

    receipt = await service.cancel_run(
        "run-cancel-replay",
        idempotency_key="cancel-stable-1",
    )

    assert original.accepted_status is RunStatus.CREATED
    assert receipt.status is RunStatus.CANCELLED
    assert service.get_run("run-cancel-replay").status is RunStatus.RUNNING

    store.release_execution(
        ExecutionToken(
            segment_id=execution.id,
            version=execution.version,
            lease_token="later-secret",
        )
    )
    await service.close()


async def test_event_stream_replays_after_sequence_and_disconnect_does_not_cancel(
    tmp_path: Path,
) -> None:
    runtime = _mock_runtime()
    store = RunStore(tmp_path / "qed.sqlite3")
    workflow = ResearchWorkflow(store, runtime, runtime_version="test-runtime")
    service = ApplicationService(store=store, workflow=workflow, runtime=runtime)
    run = service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-events")
    appended = store.list_events(run.id)[-1]

    stream = service.stream_events(
        run.id,
        after_seq=appended.seq - 1,
        poll_interval=0.001,
        heartbeat_interval=0.01,
    )
    replayed = await anext(stream)
    heartbeat = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert replayed == appended
    assert isinstance(heartbeat, StreamHeartbeat)
    assert service.get_run(run.id).status is RunStatus.CREATED

    await service.close()
