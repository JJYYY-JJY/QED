"""Read-only run diagnosis and explicit operator disposition helpers."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from qed.schemas import Event, StrictModel, event_chain_sha256
from qed.store import (
    ExecutionLease,
    OperatorDecisionRecord,
    RunRecord,
    RunStatus,
    RunStore,
    ThreadRecord,
)


class PendingRuntimeIdentity(StrictModel):
    """One unresolved runtime start attempt or known external turn."""

    kind: Literal["attempt", "turn"]
    turn_input_id: str | None = None
    attempt: Annotated[int, Field(ge=1)] | None = None
    backend: str | None = None
    external_thread_id: str | None = None
    external_turn_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> PendingRuntimeIdentity:
        if self.kind == "attempt":
            if self.turn_input_id is None or self.attempt is None:
                raise ValueError("attempt identity requires turn_input_id and attempt")
            if any(
                value is not None
                for value in (
                    self.backend,
                    self.external_thread_id,
                    self.external_turn_id,
                )
            ):
                raise ValueError("attempt identity cannot contain a known runtime turn")
        elif (
            self.backend is None
            or self.external_thread_id is None
            or self.external_turn_id is None
        ):
            raise ValueError("turn identity requires backend, thread, and turn")
        return self


class StageQueryUsage(StrictModel):
    stage: str
    queries: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1)]


class BudgetDiagnosis(StrictModel):
    token_usage: Annotated[int, Field(ge=0)]
    token_limit: Annotated[int, Field(ge=1)]
    search_queries: tuple[StageQueryUsage, ...]
    turn_retries_consumed_total: Annotated[int, Field(ge=0)]
    turn_retry_limit_per_turn: Annotated[int, Field(ge=0)]
    proof_attempts_consumed: Annotated[int, Field(ge=0)]
    proof_attempt_limit: Annotated[int, Field(ge=1)]
    plan_revisions_consumed: Annotated[int, Field(ge=0)]
    plan_revision_limit: Annotated[int, Field(ge=0)]
    strategy_rewrites_consumed: Annotated[int, Field(ge=0)]
    strategy_rewrite_limit: Annotated[int, Field(ge=0)]
    run_seconds_consumed: Annotated[float, Field(ge=0)]
    run_seconds_limit: Annotated[int, Field(ge=1)]
    stage_seconds_consumed: Annotated[float, Field(ge=0)]
    stage_seconds_limit: Annotated[int, Field(ge=1)]


class ReconciliationCapability(StrictModel):
    available: Literal[False] = False
    reason: str


class RunDiagnosis(StrictModel):
    """Self-contained diagnostic manifest for an exceptional durable run."""

    schema_version: Literal[1] = 1
    run: RunRecord
    observed_at: datetime
    execution_segments: tuple[ExecutionLease, ...]
    threads: tuple[ThreadRecord, ...]
    pending_runtime: tuple[PendingRuntimeIdentity, ...]
    unconfirmed_events: tuple[Event, ...]
    budget: BudgetDiagnosis
    blockers: tuple[str, ...]
    operator_decisions: tuple[OperatorDecisionRecord, ...]
    reconciliation: ReconciliationCapability
    event_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _pending_identity(value: tuple[str, ...]) -> PendingRuntimeIdentity:
    if len(value) == 3 and value[0] == "attempt":
        return PendingRuntimeIdentity(
            kind="attempt",
            turn_input_id=value[1],
            attempt=int(value[2]),
        )
    if len(value) == 4 and value[0] == "turn":
        return PendingRuntimeIdentity(
            kind="turn",
            backend=value[1],
            external_thread_id=value[2],
            external_turn_id=value[3],
        )
    raise ValueError(f"unknown persisted runtime identity: {value!r}")


def _elapsed_seconds(
    segments: tuple[ExecutionLease, ...],
    observed_at: datetime,
    *,
    not_before: datetime | None = None,
) -> float:
    elapsed = 0.0
    for segment in segments:
        start = (
            max(segment.created_at, not_before)
            if not_before is not None
            else segment.created_at
        )
        end = min(
            segment.released_at or observed_at,
            segment.lease_expires_at,
            observed_at,
        )
        if end > start:
            elapsed += (end - start).total_seconds()
    return elapsed


def _budget_diagnosis(
    run: RunRecord,
    events: tuple[Event, ...],
    segments: tuple[ExecutionLease, ...],
    observed_at: datetime,
) -> BudgetDiagnosis:
    usage_by_turn: dict[tuple[str, str], int] = {}
    queries_by_stage: dict[str, int] = defaultdict(int)
    retries = 0
    for event in events:
        if event.event_type == "runtime.token_usage":
            thread_id = event.payload.get("thread_id")
            turn_id = event.payload.get("turn_id")
            usage = event.payload.get("usage")
            input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            output_tokens = (
                usage.get("output_tokens") if isinstance(usage, dict) else None
            )
            if (
                isinstance(thread_id, str)
                and isinstance(turn_id, str)
                and isinstance(usage, dict)
                and type(input_tokens) is int
                and type(output_tokens) is int
            ):
                usage_by_turn[(thread_id, turn_id)] = input_tokens + output_tokens
        elif (
            event.event_type == "runtime.item_completed"
            and event.payload.get("counts_as_search_query") is True
        ):
            queries_by_stage[event.stage] += 1
        elif event.event_type == "runtime.turn_retry":
            retries += 1

    stage_entries = tuple(
        event
        for event in events
        if event.event_type == "run.stage_changed"
        and event.payload.get("to") == run.stage.value
    )
    stage_started_at = stage_entries[-1].created_at if stage_entries else run.created_at
    budgets = run.config.budgets
    return BudgetDiagnosis(
        token_usage=sum(usage_by_turn.values()),
        token_limit=budgets.max_tokens,
        search_queries=tuple(
            StageQueryUsage(
                stage=stage,
                queries=count,
                limit=run.config.search.max_queries_per_stage,
            )
            for stage, count in sorted(queries_by_stage.items())
        ),
        turn_retries_consumed_total=retries,
        turn_retry_limit_per_turn=budgets.turn_retries,
        proof_attempts_consumed=run.proof_attempt_count,
        proof_attempt_limit=budgets.proof_attempts,
        plan_revisions_consumed=run.plan_revision_count,
        plan_revision_limit=budgets.plan_revisions,
        strategy_rewrites_consumed=run.strategy_rewrite_count,
        strategy_rewrite_limit=budgets.strategy_rewrites,
        run_seconds_consumed=_elapsed_seconds(segments, observed_at),
        run_seconds_limit=budgets.run_seconds,
        stage_seconds_consumed=_elapsed_seconds(
            segments,
            observed_at,
            not_before=stage_started_at,
        ),
        stage_seconds_limit=budgets.stage_seconds,
    )


def _operator_decisions(events: tuple[Event, ...]) -> tuple[OperatorDecisionRecord, ...]:
    decisions: list[OperatorDecisionRecord] = []
    for event in events:
        if event.event_type != "operator.run_abandoned":
            continue
        payload = event.payload
        decisions.append(
            OperatorDecisionRecord(
                run_id=event.run_id,
                action="abandon",
                idempotency_key=str(payload["idempotency_key"]),
                reason=str(payload["reason"]),
                status_before=RunStatus(str(payload["from"])),
                status_after=RunStatus(str(payload["to"])),
                event_seq=event.seq,
                created_at=event.created_at,
            )
        )
    return tuple(decisions)


def diagnose_run(
    store: RunStore,
    run_id: str,
    *,
    observed_at: datetime | None = None,
) -> RunDiagnosis:
    """Build a read-only explanation of why a run can or cannot resume."""

    now = observed_at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    snapshot = store.snapshot(run_id)
    pending = tuple(
        _pending_identity(identity)
        for identity in store.pending_runtime_identities(run_id)
    )
    live_segments = tuple(
        segment
        for segment in snapshot.execution_segments
        if segment.released_at is None and segment.lease_expires_at > now
    )
    blockers: list[str] = []
    for identity in pending:
        if identity.kind == "attempt":
            blockers.append(
                "resume blocked: runtime start result is ambiguous for "
                f"{identity.turn_input_id} attempt {identity.attempt}"
            )
        else:
            blockers.append(
                "resume blocked: terminal event is missing for "
                f"{identity.backend}/{identity.external_thread_id}/"
                f"{identity.external_turn_id}"
            )
    blockers.extend(
        f"resume blocked: live execution lease {segment.id} is owned by {segment.worker_id}"
        for segment in live_segments
    )
    if (
        not snapshot.run.resumable
        or snapshot.run.status
        not in {RunStatus.PAUSED, RunStatus.CANCELLED, RunStatus.FAILED}
    ):
        blockers.append(
            "resume blocked: durable run status "
            f"{snapshot.run.status.value} is not resumable"
        )
    if any(event.event_type == "operator.run_abandoned" for event in snapshot.events):
        blockers.append("resume blocked: an immutable operator abandon decision exists")

    unconfirmed_events = tuple(
        event
        for event in snapshot.events
        if event.event_type
        in {
            "runtime.turn_start_unconfirmed",
            "runtime.turn_terminal_unconfirmed",
        }
    )
    return RunDiagnosis(
        run=snapshot.run,
        observed_at=now,
        execution_segments=snapshot.execution_segments,
        threads=snapshot.threads,
        pending_runtime=pending,
        unconfirmed_events=unconfirmed_events,
        budget=_budget_diagnosis(
            snapshot.run,
            snapshot.events,
            snapshot.execution_segments,
            now,
        ),
        blockers=tuple(blockers),
        operator_decisions=_operator_decisions(snapshot.events),
        reconciliation=ReconciliationCapability(
            reason=(
                "The locked SDK/App Server/exec runtime abstraction does not expose "
                "one authoritative exact backend/thread/turn status lookup across "
                "all configured transports; QED therefore cannot safely synthesize "
                "terminal evidence."
            )
        ),
        event_chain_sha256=event_chain_sha256(snapshot.events),
    )
