"""Command-line entry point for managed QED research runs."""

from __future__ import annotations

import asyncio
import json
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import uuid4

import typer
import uvicorn
from pydantic import BaseModel, ValidationError

from qed.api import create_app
from qed.config import QEDConfig
from qed.inputs import RunInput
from qed.logging import configure_logging
from qed.runtime import create_codex_runtime
from qed.schemas import canonical_json
from qed.service import (
    ApplicationService,
    RunAlreadyActiveError,
    build_management_service,
    build_mock_service,
    build_service,
)
from qed.service_settings import ServiceSettings
from qed.store import (
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
    RunStatus,
)
from qed.workflow import WorkflowExecutionError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Codex-native mathematical research with thread-isolated policy checks.",
)


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    CONFLICT = 3
    NOT_FOUND = 4
    EXECUTION_FAILED = 5


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RuntimeMode(StrEnum):
    MOCK = "mock"
    CODEX = "codex"


class BackendMode(StrEnum):
    AUTO = "auto"
    SDK = "sdk"
    APP_SERVER = "app-server"
    EXEC = "exec"


@app.callback()
def main(
    log_level: Annotated[LogLevel, typer.Option(help="Structured log level.")] = (
        LogLevel.WARNING
    ),
    log_format: Annotated[LogFormat, typer.Option(help="Log rendering format.")] = LogFormat.JSON,
) -> None:
    configure_logging(
        level=log_level.value,
        json_output=log_format is LogFormat.JSON,
    )


def _settings(data_root: Path) -> ServiceSettings:
    return ServiceSettings(data_root=data_root)


def _build_runtime_service(
    settings: ServiceSettings,
    mode: RuntimeMode,
) -> ApplicationService:
    if mode is RuntimeMode.MOCK:
        return build_mock_service(settings)
    return build_service(settings, runtime_factory=create_codex_runtime)


def _write(value: object, *, error: bool = False) -> None:
    if isinstance(value, BaseModel):
        rendered = canonical_json(value)
    else:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    typer.echo(rendered, err=error)


def _abort(error: Exception) -> NoReturn:
    if isinstance(error, NotFoundError):
        exit_code = ExitCode.NOT_FOUND
        code = "run_not_found"
        message = "The requested run was not found."
    elif isinstance(error, (ConflictError, InvalidTransitionError, RunAlreadyActiveError)):
        exit_code = ExitCode.CONFLICT
        code = "state_conflict"
        message = "The command conflicts with durable run state."
    elif isinstance(error, (ValidationError, ValueError)):
        exit_code = ExitCode.USAGE
        code = "invalid_input"
        message = "The supplied input is invalid."
    elif isinstance(error, WorkflowExecutionError):
        exit_code = ExitCode.EXECUTION_FAILED
        code = "execution_failed"
        message = "The research run did not complete successfully."
    else:
        exit_code = ExitCode.ERROR
        code = "internal_error"
        message = "The operation could not be completed."
    _write(
        {"schema_version": 1, "error": {"code": code, "message": message}},
        error=True,
    )
    raise typer.Exit(code=int(exit_code)) from error


async def _close(service: ApplicationService) -> None:
    await service.close()


@app.command()
def init(
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Initialize the durable SQLite store."""

    service: ApplicationService | None = None
    try:
        service = build_management_service(_settings(data_root))
        _write(service.store_info())
    except Exception as error:
        _abort(error)
    finally:
        if service is not None:
            asyncio.run(_close(service))


@app.command()
def status(
    run_id: Annotated[str, typer.Argument(help="Durable run identifier.")],
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Show the current durable status for one run."""

    service: ApplicationService | None = None
    try:
        service = build_management_service(_settings(data_root))
        _write(service.get_run(run_id))
    except Exception as error:
        _abort(error)
    finally:
        if service is not None:
            asyncio.run(_close(service))


async def _run_research(
    *,
    data_root: Path,
    run_id: str,
    run_input: RunInput,
    config: QEDConfig,
    runtime_mode: RuntimeMode,
) -> RunStatus:
    service = _build_runtime_service(_settings(data_root), runtime_mode)
    try:
        created = service.create_run(run_input, config, run_id=run_id)
        receipt = await service.start_run(
            created.id,
            idempotency_key=f"cli-start-{uuid4().hex}",
        )
        completed = await service.wait(created.id)
        _write(
            {
                "schema_version": 1,
                "receipt": receipt.model_dump(mode="json"),
                "run": completed.model_dump(mode="json"),
            }
        )
        return completed.status
    finally:
        await service.close()


@app.command()
def run(
    problem: Annotated[str, typer.Argument(help="Mathematical statement or problem.")],
    guidance: Annotated[str, typer.Option(help="Optional proof guidance.")] = "",
    verification_rule: Annotated[
        list[str] | None,
        typer.Option("--verification-rule", help="Repeatable verification rule."),
    ] = None,
    run_id: Annotated[str | None, typer.Option(help="Optional durable run identifier.")] = None,
    model: Annotated[str, typer.Option(help="Codex model.")] = "gpt-5.6-sol",
    effort: Annotated[str, typer.Option(help="Reasoning effort or auto.")] = "auto",
    backend: Annotated[BackendMode, typer.Option(help="Codex transport preference.")] = (
        BackendMode.AUTO
    ),
    runtime: Annotated[RuntimeMode, typer.Option(help="Runtime implementation.")] = (
        RuntimeMode.CODEX
    ),
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Create and synchronously execute one research run."""

    selected_run_id = run_id or f"run-{uuid4().hex}"
    try:
        result = asyncio.run(
            _run_research(
                data_root=data_root,
                run_id=selected_run_id,
                run_input=RunInput(
                    problem=problem,
                    prove_guidance=guidance,
                    verification_rules=tuple(verification_rule or ()),
                ),
                config=QEDConfig(
                    model=model,
                    effort=effort,
                    backend=backend.value,
                ),
                runtime_mode=runtime,
            )
        )
    except Exception as error:
        _abort(error)
    if result is not RunStatus.COMPLETED:
        raise typer.Exit(code=int(ExitCode.EXECUTION_FAILED))


async def _cancel(
    *,
    data_root: Path,
    run_id: str,
    idempotency_key: str,
    runtime_mode: RuntimeMode,
) -> None:
    service = _build_runtime_service(_settings(data_root), runtime_mode)
    try:
        _write(await service.cancel_run(run_id, idempotency_key=idempotency_key))
    finally:
        await service.close()


@app.command()
def cancel(
    run_id: Annotated[str, typer.Argument(help="Durable run identifier.")],
    idempotency_key: Annotated[
        str | None,
        typer.Option(help="Stable command key for safe retries."),
    ] = None,
    runtime: Annotated[RuntimeMode, typer.Option(help="Runtime implementation.")] = (
        RuntimeMode.CODEX
    ),
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Interrupt active Codex turns and persist cancellation."""

    try:
        asyncio.run(
            _cancel(
                data_root=data_root,
                run_id=run_id,
                idempotency_key=idempotency_key or f"cli-cancel-{uuid4().hex}",
                runtime_mode=runtime,
            )
        )
    except Exception as error:
        _abort(error)


async def _resume(
    *,
    data_root: Path,
    run_id: str,
    idempotency_key: str,
    runtime_mode: RuntimeMode,
) -> RunStatus:
    service = _build_runtime_service(_settings(data_root), runtime_mode)
    try:
        receipt = await service.resume_run(run_id, idempotency_key=idempotency_key)
        completed = await service.wait(run_id)
        _write(
            {
                "schema_version": 1,
                "receipt": receipt.model_dump(mode="json"),
                "run": completed.model_dump(mode="json"),
            }
        )
        return completed.status
    finally:
        await service.close()


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Durable run identifier.")],
    idempotency_key: Annotated[
        str | None,
        typer.Option(help="Stable command key for safe retries."),
    ] = None,
    runtime: Annotated[RuntimeMode, typer.Option(help="Runtime implementation.")] = (
        RuntimeMode.CODEX
    ),
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Resume from the last durable stage boundary."""

    try:
        result = asyncio.run(
            _resume(
                data_root=data_root,
                run_id=run_id,
                idempotency_key=idempotency_key or f"cli-resume-{uuid4().hex}",
                runtime_mode=runtime,
            )
        )
    except Exception as error:
        _abort(error)
    if result is not RunStatus.COMPLETED:
        raise typer.Exit(code=int(ExitCode.EXECUTION_FAILED))


@app.command()
def migrate(
    source: Annotated[Path, typer.Argument(help="Legacy run directory.")],
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Import a legacy run without modifying or trusting the source."""

    service: ApplicationService | None = None
    try:
        service = build_management_service(_settings(data_root))
        imported = service.migrate_legacy(source)
        _write(
            {
                "schema_version": 1,
                "import_dir": str(imported.import_dir),
                "manifest": imported.manifest.model_dump(mode="json"),
            }
        )
    except Exception as error:
        _abort(error)
    finally:
        if service is not None:
            asyncio.run(_close(service))


@app.command()
def doctor(
    run_id: Annotated[str, typer.Argument(help="Durable run identifier.")],
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Read one run's execution, runtime identity, budget, and resume blockers."""

    service: ApplicationService | None = None
    try:
        service = build_management_service(_settings(data_root))
        _write(service.diagnose_run(run_id))
    except Exception as error:
        _abort(error)
    finally:
        if service is not None:
            asyncio.run(_close(service))


@app.command()
def reconcile(
    run_id: Annotated[str, typer.Argument(help="Durable run identifier.")],
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Explain why authoritative automatic runtime reconciliation is unavailable."""

    service: ApplicationService | None = None
    try:
        service = build_management_service(_settings(data_root))
        diagnosis = service.diagnose_run(run_id)
        _write(
            {
                "schema_version": 1,
                "run_id": run_id,
                "reconciliation": diagnosis.reconciliation.model_dump(mode="json"),
            }
        )
    except Exception as error:
        _abort(error)
    finally:
        if service is not None:
            asyncio.run(_close(service))
    raise typer.Exit(code=int(ExitCode.CONFLICT))


@app.command()
def abandon(
    run_id: Annotated[str, typer.Argument(help="Durable run identifier.")],
    reason: Annotated[str, typer.Option(help="Required immutable operator rationale.")],
    idempotency_key: Annotated[
        str | None,
        typer.Option(help="Stable command key for safe retries."),
    ] = None,
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
) -> None:
    """Record a terminal non-PASS operator decision without inventing runtime events."""

    service: ApplicationService | None = None
    try:
        service = build_management_service(_settings(data_root))
        decision = service.abandon_run(
            run_id,
            reason=reason,
            idempotency_key=idempotency_key or f"cli-abandon-{uuid4().hex}",
        )
        _write(decision)
    except Exception as error:
        _abort(error)
    finally:
        if service is not None:
            asyncio.run(_close(service))


@app.command()
def serve(
    data_root: Annotated[Path, typer.Option(help="Managed QED data directory.")] = Path(".qed"),
    host: Annotated[str, typer.Option(help="Listen host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Listen port.")] = 8000,
    auth_token: Annotated[
        str | None,
        typer.Option(help="Bearer token; required for non-loopback binds.", hide_input=True),
    ] = None,
    runtime: Annotated[RuntimeMode, typer.Option(help="Runtime implementation.")] = (
        RuntimeMode.CODEX
    ),
) -> None:
    """Serve the authenticated REST and SSE API."""

    try:
        if auth_token is None:
            settings = ServiceSettings(data_root=data_root, host=host, port=port)
        else:
            settings = ServiceSettings(
                data_root=data_root,
                host=host,
                port=port,
                auth_token=auth_token,
            )
        application = create_app(
            settings=settings,
            service=_build_runtime_service(settings, runtime),
        )
    except Exception as error:
        _abort(error)
    uvicorn.run(
        application,
        host=settings.host,
        port=settings.port,
        log_config=None,
    )
