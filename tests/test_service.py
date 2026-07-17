from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from qed.config import QEDConfig
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
from qed.service import (
    ApplicationService,
    RunAlreadyActiveError,
    StreamHeartbeat,
    build_service,
)
from qed.service_settings import ServiceSettings
from qed.store import RunStatus, RunStore
from qed.workflow import ResearchWorkflow


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


class BlockingRuntime:
    def __init__(self) -> None:
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

    async def close(self) -> None:
        self.close_count += 1
        await super().close()


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


async def test_service_uses_only_the_explicit_runtime_factory_and_closes_once(
    tmp_path: Path,
) -> None:
    runtime = ClosingRuntime()
    calls = 0

    def factory() -> ClosingRuntime:
        nonlocal calls
        calls += 1
        return runtime

    service = build_service(
        ServiceSettings(data_root=tmp_path),
        runtime_factory=factory,
        runtime_version="explicit-test-runtime",
    )
    service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-factory")

    await service.close()
    await service.close()

    assert calls == 1
    assert runtime.close_count == 1


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
