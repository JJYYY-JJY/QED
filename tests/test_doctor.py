from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from qed.config import QEDConfig
from qed.doctor import (
    DoctorCheck,
    DoctorReport,
    _check_port,
    _check_version,
    _command_version,
    _mode_status,
    build_doctor_report,
)
from qed.service_settings import ServiceSettings
from qed.store import RunStore


class _Socket:
    def __init__(self, result: int = 1, error: OSError | None = None) -> None:
        self.result = result
        self.error = error

    def __enter__(self) -> _Socket:
        if self.error is not None:
            raise self.error
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def connect_ex(self, _address: tuple[str, int]) -> int:
        return self.result


def test_doctor_models_and_mode_checks_are_fail_closed(tmp_path: Path) -> None:
    missing = _mode_status(tmp_path / "missing")
    assert missing == ("unknown", "path does not exist yet")

    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    assert _mode_status(file_path) == ("fail", "path is not a directory")
    file_path.chmod(0o600)
    assert _mode_status(file_path, directory=False) == ("pass", "0o600")

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    assert _mode_status(private) == ("pass", "0o700")
    private.chmod(0o755)
    assert _mode_status(private) == ("fail", "0o755")

    report = DoctorReport(
        generated_at=datetime(2026, 8, 11, tzinfo=UTC),
        live=False,
        checks=(
            DoctorCheck(
                id="test.pass",
                status="pass",
                observed="ok",
                remediation="none",
                command="true",
            ),
        ),
    )
    assert report.ok is True
    unknown = report.checks[0].model_copy(update={"status": "unknown"})
    assert report.model_copy(update={"checks": (unknown,)}).ok is False


def test_doctor_port_check_covers_available_occupied_and_socket_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ServiceSettings(host="localhost", port=8123)
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: _Socket(result=1))
    assert _check_port(settings).status == "pass"
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: _Socket(result=0))
    assert _check_port(settings).status == "fail"
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *_args, **_kwargs: _Socket(error=OSError("socket unavailable")),
    )
    failed = _check_port(settings)
    assert failed.status == "unknown"
    assert "unavailable" in failed.observed


def test_doctor_version_checks_fail_closed_without_a_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("qed.doctor.shutil.which", lambda _name: None)
    assert _command_version("missing-tool") is None
    assert _check_version("missing-tool", "install it").status == "fail"


def test_doctor_reports_uninitialized_and_live_environment(tmp_path: Path) -> None:
    report = build_doctor_report(ServiceSettings(data_root=tmp_path), live=True)
    by_id = {check.id: check for check in report.checks}
    assert report.live is True
    assert by_id["storage.sqlite"].status == "unknown"
    assert by_id["codex.live_capability"].status == "unknown"
    assert by_id["codex.live_capability"].limitation is not None


def test_doctor_binds_all_live_codex_identity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = QEDConfig()
    capability = SimpleNamespace(
        model=config.model,
        backend="sdk",
        selected_effort="low",
        model_catalog_sha256="a" * 64,
        capability_response_sha256="b" * 64,
    )
    monkeypatch.setattr("qed.doctor._probe_live_capability", lambda *_args: capability)

    report = build_doctor_report(ServiceSettings(data_root=tmp_path), live=True)
    checks = {check.id: check for check in report.checks}

    assert checks["codex.exact_model"].status == "pass"
    assert checks["codex.selected_backend"].status == "pass"
    assert checks["codex.selected_effort"].status == "pass"
    assert checks["codex.model_catalog"].status == "pass"
    assert checks["codex.capability_response"].status == "pass"


def test_doctor_reports_database_integrity_and_schema_errors(tmp_path: Path) -> None:
    database = tmp_path / "qed.sqlite3"
    with RunStore(database):
        pass
    report = build_doctor_report(ServiceSettings(data_root=tmp_path))
    checks = {check.id: check for check in report.checks}
    assert checks["storage.sqlite"].status == "pass"

    database.write_bytes(b"not sqlite")
    invalid = build_doctor_report(ServiceSettings(data_root=tmp_path))
    assert {check.id: check for check in invalid.checks}["storage.sqlite"].status == "fail"
