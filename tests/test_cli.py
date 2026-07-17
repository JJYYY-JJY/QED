from __future__ import annotations

import asyncio
import json
from io import StringIO
from ipaddress import ip_address
from pathlib import Path

import pytest
from typer.testing import CliRunner

import qed.cli as cli_module
from qed.cli import app
from qed.config import QEDConfig
from qed.inputs import RunInput
from qed.logging import configure_logging, get_logger
from qed.service import _default_mock_runtime, build_service
from qed.service_settings import ServiceSettings

_RUNNER = CliRunner()


def test_structured_logging_redacts_secret_fields_and_bearer_values() -> None:
    stream = StringIO()
    configure_logging(level="INFO", json_output=True, stream=stream)

    get_logger("qed.test").info(
        "service.started",
        authorization="Bearer top-secret",
        nested={"auth_token": "also-secret"},
        note="request rejected: Bearer embedded-secret",
        safe="visible",
    )

    record = json.loads(stream.getvalue())
    assert record["event"] == "service.started"
    assert record["safe"] == "visible"
    assert record["authorization"] == "[redacted]"
    assert record["nested"]["auth_token"] == "[redacted]"
    assert record["note"] == "request rejected: Bearer [redacted]"
    assert "top-secret" not in stream.getvalue()
    assert "also-secret" not in stream.getvalue()
    assert "embedded-secret" not in stream.getvalue()


def test_structured_logging_redacts_bearer_values_from_exception_text() -> None:
    stream = StringIO()
    configure_logging(level="ERROR", json_output=True, stream=stream)

    try:
        raise ValueError("request contained Bearer exception-secret")
    except ValueError:
        get_logger("qed.test").exception("service.failed")

    record = json.loads(stream.getvalue())
    assert record["event"] == "service.failed"
    assert "Bearer [redacted]" in record["exception"]
    assert "exception-secret" not in stream.getvalue()


def test_init_creates_managed_sqlite_store(tmp_path: Path) -> None:
    data_root = tmp_path / "managed"

    result = _RUNNER.invoke(app, ["init", "--data-root", str(data_root)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["journal_mode"] == "wal"
    assert (data_root / "qed.sqlite3").is_file()


def test_status_uses_stable_not_found_exit_code(tmp_path: Path) -> None:
    result = _RUNNER.invoke(
        app,
        ["status", "missing-run", "--data-root", str(tmp_path)],
    )

    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "run_not_found"


def test_migrate_preserves_source_and_returns_manifest(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    proof = source / "proof.md"
    proof.write_text("legacy proof\n", encoding="utf-8")

    result = _RUNNER.invoke(
        app,
        ["migrate", str(source), "--data-root", str(tmp_path / "managed")],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["manifest"]["trust"] == "legacy_untrusted"
    assert proof.read_text(encoding="utf-8") == "legacy proof\n"


def test_help_exposes_required_operational_commands() -> None:
    result = _RUNNER.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("init", "run", "status", "cancel", "resume", "serve", "migrate"):
        assert command in result.stdout


def test_mock_run_completes_and_exports_a_reproducible_bundle(tmp_path: Path) -> None:
    result = _RUNNER.invoke(
        app,
        [
            "run",
            "Prove P.",
            "--run-id",
            "run-mock-e2e",
            "--runtime",
            "mock",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["run"]["status"] == "completed"
    exported = tuple((tmp_path / "exports" / "run-mock-e2e").glob("*/*"))
    assert {path.name for path in exported} == {
        "manifest.json",
        "proof.md",
        "report.md",
    }


def test_codex_run_scopes_runtime_state_to_managed_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_homes: list[Path] = []

    def runtime_factory(codex_home: Path) -> object:
        codex_homes.append(codex_home)
        runtime = _default_mock_runtime()
        runtime.runtime_version = "test-codex/1"
        return runtime

    monkeypatch.setattr(cli_module, "create_codex_runtime", runtime_factory)

    result = _RUNNER.invoke(
        app,
        [
            "run",
            "Prove P.",
            "--run-id",
            "run-codex-home",
            "--runtime",
            "codex",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert codex_homes == [tmp_path / "codex-home"]


def test_cancel_and_resume_commands_share_durable_service_state(tmp_path: Path) -> None:
    service = build_service(ServiceSettings(data_root=tmp_path))
    service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-cli")
    asyncio.run(service.close())

    cancelled = _RUNNER.invoke(
        app,
        ["cancel", "run-cli", "--data-root", str(tmp_path)],
    )
    resumed = _RUNNER.invoke(
        app,
        ["resume", "run-cli", "--runtime", "mock", "--data-root", str(tmp_path)],
    )

    assert cancelled.exit_code == 0, cancelled.output
    assert json.loads(cancelled.stdout)["status"] == "cancelled"
    assert resumed.exit_code == 0, resumed.output
    resumed_run = json.loads(resumed.stdout)["run"]
    assert resumed_run["resume_count"] == 1
    assert resumed_run["status"] == "completed"


def test_serve_rejects_non_loopback_bind_without_bearer_token(tmp_path: Path) -> None:
    result = _RUNNER.invoke(
        app,
        [
            "serve",
            "--host",
            str(ip_address(0)),
            "--runtime",
            "mock",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_input"
