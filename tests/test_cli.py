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
from qed.service_settings import ServiceSettings
from tests.mock_service import build_mock_service, default_mock_runtime

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
    assert payload["schema_version"] == 5
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
    for command in (
        "init",
        "run",
        "status",
        "cancel",
        "resume",
        "serve",
        "migrate",
        "doctor",
        "reconcile",
        "abandon",
    ):
        assert command in result.stdout


def test_cli_rejects_mock_runtime_selection(tmp_path: Path) -> None:
    result = _RUNNER.invoke(
        app,
        ["run", "Prove P.", "--runtime", "mock", "--data-root", str(tmp_path)],
    )

    assert result.exit_code == 2


def test_run_completes_with_an_explicit_test_service_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_build_runtime_service",
        lambda settings, mode: build_mock_service(settings),
    )
    result = _RUNNER.invoke(
        app,
        [
            "run",
            "Prove P.",
            "--run-id",
            "run-mock-e2e",
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
        "event-chain.json",
        "audit.json",
    }
    manifest_path = next(path for path in exported if path.name == "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["runtime_version"] == "fixture-runtime/1"
    assert {item["runtime_version"] for item in manifest["execution_segments"]} == {
        "fixture-runtime/1"
    }
    assert {item["backend"] for item in manifest["turns"]} == {"mock"}


def test_codex_run_scopes_runtime_state_to_managed_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_homes: list[Path] = []

    def runtime_factory(codex_home: Path) -> object:
        codex_homes.append(codex_home)
        runtime = default_mock_runtime()
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
            "--data-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert codex_homes == [tmp_path / "codex-home"]


def test_cancel_and_resume_commands_share_durable_service_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_build_runtime_service",
        lambda settings, mode: build_mock_service(settings),
    )
    service = build_mock_service(ServiceSettings(data_root=tmp_path))
    service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-cli")
    asyncio.run(service.close())

    cancelled = _RUNNER.invoke(
        app,
        [
            "cancel",
            "run-cli",
            "--data-root",
            str(tmp_path),
        ],
    )
    resumed = _RUNNER.invoke(
        app,
        ["resume", "run-cli", "--data-root", str(tmp_path)],
    )

    assert cancelled.exit_code == 0, cancelled.output
    assert json.loads(cancelled.stdout)["status"] == "cancelled"
    assert resumed.exit_code == 0, resumed.output
    resumed_run = json.loads(resumed.stdout)["run"]
    assert resumed_run["resume_count"] == 1
    assert resumed_run["status"] == "completed"


def test_doctor_reconcile_and_abandon_are_explicit_and_idempotent(
    tmp_path: Path,
) -> None:
    service = build_mock_service(ServiceSettings(data_root=tmp_path))
    service.create_run(RunInput(problem="Prove P."), QEDConfig(), run_id="run-ops")
    asyncio.run(service.close())

    doctor = _RUNNER.invoke(
        app,
        ["doctor", "run-ops", "--data-root", str(tmp_path)],
    )
    reconcile = _RUNNER.invoke(
        app,
        ["reconcile", "run-ops", "--data-root", str(tmp_path)],
    )
    abandon = _RUNNER.invoke(
        app,
        [
            "abandon",
            "run-ops",
            "--reason",
            "Operator ended the exceptional run.",
            "--idempotency-key",
            "operator-ops-1",
            "--data-root",
            str(tmp_path),
        ],
    )
    replay = _RUNNER.invoke(
        app,
        [
            "abandon",
            "run-ops",
            "--reason",
            "Operator ended the exceptional run.",
            "--idempotency-key",
            "operator-ops-1",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert doctor.exit_code == 0, doctor.output
    diagnosis = json.loads(doctor.stdout)
    assert diagnosis["run"]["id"] == "run-ops"
    assert diagnosis["reconciliation"]["available"] is False
    assert diagnosis["blockers"]
    assert reconcile.exit_code == 3, reconcile.output
    assert json.loads(reconcile.stdout)["reconciliation"]["available"] is False
    assert abandon.exit_code == 0, abandon.output
    assert json.loads(abandon.stdout)["status_after"] == "failed"
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.stdout)["replayed"] is True


def test_environment_doctor_fails_closed_on_unknown_checks(tmp_path: Path) -> None:
    result = _RUNNER.invoke(
        app,
        ["doctor", "--json", "--data-root", str(tmp_path / "uninitialized")],
    )

    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["checks"]
    assert any(check["status"] == "unknown" for check in report["checks"])


def test_serve_rejects_non_loopback_bind_without_bearer_token(tmp_path: Path) -> None:
    result = _RUNNER.invoke(
        app,
        [
            "serve",
            "--host",
            str(ip_address(0)),
            "--data-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_input"
