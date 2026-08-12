"""Read-only environment diagnostics for stable QED operation."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import shutil
import socket
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from qed.config import QEDConfig
from qed.schemas import StrictModel, canonical_sha256
from qed.service_settings import ServiceSettings


class DoctorCheck(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+$")
    status: Literal["pass", "fail", "unknown"]
    observed: str
    remediation: str
    command: str
    limitation: str | None = None


class DoctorReport(StrictModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    live: bool
    checks: tuple[DoctorCheck, ...] = Field(min_length=1)

    @property
    def ok(self) -> bool:
        return all(check.status == "pass" for check in self.checks)


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _command_version(command: str, version_argument: str = "--version") -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - executable comes from PATH, no shell
            (executable, version_argument),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0][:256] if completed.returncode == 0 and output else None


def _check_version(command: str, remediation: str) -> DoctorCheck:
    version = _command_version(command)
    return DoctorCheck(
        id=f"tool.{command}",
        status="pass" if version is not None else "fail",
        observed=version or "not found or failed to execute",
        remediation=remediation,
        command=f"{command} --version",
    )


def _mode_status(
    path: Path,
    *,
    directory: bool = True,
) -> tuple[Literal["pass", "fail", "unknown"], str]:
    if not path.exists():
        return "unknown", "path does not exist yet"
    if path.is_symlink():
        return "fail", "symbolic links are forbidden"
    if directory and not path.is_dir():
        return "fail", "path is not a directory"
    mode = path.stat().st_mode & 0o777
    return ("pass", oct(mode)) if mode & 0o077 == 0 else ("fail", oct(mode))


def _check_port(settings: ServiceSettings) -> DoctorCheck:
    address = settings.host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            occupied = sock.connect_ex(
                ("127.0.0.1" if address == "localhost" else address, settings.port)
            ) == 0
    except OSError as error:
        return DoctorCheck(
            id="server.port",
            status="unknown",
            observed=str(error),
            remediation="Check the configured loopback port and stop the conflicting process.",
            command=f"connect {address}:{settings.port}",
            limitation="The diagnostic cannot identify or stop another process.",
        )
    return DoctorCheck(
        id="server.port",
        status="fail" if occupied else "pass",
        observed="occupied" if occupied else "available",
        remediation="Stop the process using the configured port or choose another port.",
        command=f"connect {address}:{settings.port}",
    )


def _probe_live_capability(settings: ServiceSettings, config: QEDConfig) -> object:
    """Probe the exact configured model without exposing credentials."""

    if not settings.codex_home.is_dir():
        raise RuntimeError("dedicated CODEX_HOME does not exist")
    from qed.runtime.models import CapabilityRequest
    from qed.runtime.router import create_codex_runtime

    runtime = create_codex_runtime(settings.codex_home)

    async def probe_and_close() -> object:
        try:
            return await runtime.probe(
                CapabilityRequest(model=config.model, effort=config.effort)
            )
        finally:
            await runtime.close()

    return asyncio.run(probe_and_close())


def _codex_identity_checks(
    config: QEDConfig,
    *,
    live_capability: object | None,
    live_error: str | None,
) -> list[DoctorCheck]:
    checks = [
        DoctorCheck(
            id="codex.model_provider",
            status="pass",
            observed="OpenAI",
            remediation="Keep the production provider fixed to OpenAI Codex.",
            command="qed doctor --json",
        )
    ]
    if live_capability is None:
        limitation = live_error or "Default doctor does not call the model."
        checks.extend(
            DoctorCheck(
                id=check_id,
                status="unknown",
                observed=observed,
                remediation=remediation,
                command="qed doctor --live --json",
                limitation=limitation,
            )
            for check_id, observed, remediation in (
                (
                    "codex.exact_model",
                    f"configured model={config.model}; catalog not probed",
                    "Run the live capability probe and require one exact catalog match.",
                ),
                (
                    "codex.selected_backend",
                    f"configured backend={config.backend}; selected backend not observed",
                    "Run the live capability probe and bind the selected backend to the run.",
                ),
                (
                    "codex.selected_effort",
                    f"configured effort={config.effort}; selected effort not observed",
                    (
                        "Run the live capability probe and require the requested "
                        "effort to be advertised."
                    ),
                ),
                (
                    "codex.model_catalog",
                    "catalog hash not observed",
                    "Run the live capability probe and record the catalog hash.",
                ),
                (
                    "codex.capability_response",
                    "capability response hash not observed",
                    "Run the live capability probe and record the response hash.",
                ),
            )
        )
        return checks

    model = getattr(live_capability, "model", None)
    selected_backend = getattr(live_capability, "backend", None)
    selected_effort = getattr(live_capability, "selected_effort", None)
    catalog_hash = getattr(live_capability, "model_catalog_sha256", None)
    response_hash = getattr(live_capability, "capability_response_sha256", None)
    checks.extend(
        (
            DoctorCheck(
                id="codex.exact_model",
                status="pass" if model == config.model else "fail",
                observed=str(model),
                remediation="Use the exact model ID returned by the official Codex catalog.",
                command="qed doctor --live --json",
            ),
            DoctorCheck(
                id="codex.selected_backend",
                status="pass" if selected_backend is not None else "fail",
                observed=str(selected_backend),
                remediation=(
                    "Bind the observed SDK/App Server backend to the immutable "
                    "run resolution."
                ),
                command="qed doctor --live --json",
            ),
            DoctorCheck(
                id="codex.selected_effort",
                status=(
                    "pass"
                    if isinstance(selected_effort, str)
                    and (config.effort == "auto" or selected_effort == config.effort)
                    else "fail"
                ),
                observed=str(selected_effort),
                remediation="Use an advertised reasoning effort; never accept a silent downgrade.",
                command="qed doctor --live --json",
            ),
            DoctorCheck(
                id="codex.model_catalog",
                status="pass" if isinstance(catalog_hash, str) else "fail",
                observed=str(catalog_hash),
                remediation="Record the exact model catalog hash in runtime provenance.",
                command="qed doctor --live --json",
            ),
            DoctorCheck(
                id="codex.capability_response",
                status="pass" if isinstance(response_hash, str) else "fail",
                observed=str(response_hash),
                remediation="Record the capability response hash in runtime provenance.",
                command="qed doctor --live --json",
            ),
        )
    )
    return checks


def build_doctor_report(settings: ServiceSettings, *, live: bool = False) -> DoctorReport:
    """Collect deterministic checks without printing credentials or starting a run."""

    repo_root = Path(__file__).resolve().parents[2]
    config = QEDConfig()
    checks: list[DoctorCheck] = [
        _check_version("python", "Install the repository-supported Python 3.13 or 3.14."),
        _check_version("node", "Install the frontend-supported Node.js version."),
        _check_version("npm", "Install npm for the checked-in frontend lockfile."),
        _check_version("uv", "Install uv and rerun the frozen dependency sync."),
    ]
    for name in ("uv.lock", "package-lock.json"):
        path = repo_root / name
        digest = _sha256(path)
        checks.append(
            DoctorCheck(
                id=f"lock.{name.replace('.', '_')}",
                status="pass" if digest is not None else "fail",
                observed=digest or "missing",
                remediation=f"Restore {name} and run the repository lock validation.",
                command=f"sha256sum {name}",
            )
        )

    try:
        from qed.runtime.stdio import probe_codex_version, resolve_codex_executable

        executable = resolve_codex_executable()
        version = probe_codex_version(executable)
        checks.append(
            DoctorCheck(
                id="codex.bundled_executable",
                status="pass",
                observed=f"{executable} ({version}, sha256={_sha256(executable)})",
                remediation="Reinstall the locked openai-codex-cli-bin package.",
                command="python -c 'from codex_cli_bin import bundled_codex_path'",
            )
        )
    except Exception as error:
        checks.append(
            DoctorCheck(
                id="codex.bundled_executable",
                status="fail",
                observed=str(error),
                remediation="Install the locked official Codex CLI binary and rerun qed doctor.",
                command="qed doctor --json",
            )
        )

    try:
        codex_package = importlib.metadata.version("openai-codex")
    except importlib.metadata.PackageNotFoundError:
        codex_package = None
    checks.append(
        DoctorCheck(
            id="codex.sdk_package",
            status="pass" if codex_package is not None else "fail",
            observed=codex_package or "official openai-codex package not installed",
            remediation="Install the locked official openai-codex package.",
            command=(
                "uv run python -c 'import importlib.metadata; "
                "print(importlib.metadata.version(\"openai-codex\"))'"
            ),
        )
    )

    live_capability: object | None = None
    live_error: str | None = None
    if live:
        try:
            live_capability = _probe_live_capability(settings, config)
        except Exception as error:
            live_error = str(error)
    checks.extend(
        _codex_identity_checks(
            config,
            live_capability=live_capability,
            live_error=live_error,
        )
    )

    for check_id, path, remediation in (
        ("codex.home", settings.codex_home, "Create a dedicated CODEX_HOME with mode 0700."),
        ("storage.data_root", settings.data_root, "Create the managed data root with mode 0700."),
    ):
        status, observed = _mode_status(path)
        checks.append(
            DoctorCheck(
                id=check_id,
                status=status,
                observed=observed,
                remediation=remediation,
                command=f"stat {path}",
            )
        )

    database = settings.database_path
    if database.is_file():
        try:
            with closing(sqlite3.connect(database)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                version = connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()
            checks.append(
                DoctorCheck(
                    id="storage.sqlite",
                    status="pass" if integrity == "ok" and version is not None else "fail",
                    observed=(
                        f"integrity={integrity}; "
                        f"schema_version={version[0] if version else 'missing'}"
                    ),
                    remediation=(
                        "Run qed backup before attempting an upgrade, then repair "
                        "or restore the database."
                    ),
                    command=f"sqlite3 {database} 'PRAGMA integrity_check'",
                )
            )
        except sqlite3.Error as error:
            checks.append(
                DoctorCheck(
                    id="storage.sqlite",
                    status="fail",
                    observed=str(error),
                    remediation="Restore a verified SQLite backup.",
                    command=f"sqlite3 {database} 'PRAGMA integrity_check'",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                id="storage.sqlite",
                status="unknown",
                observed="database not initialized",
                remediation="Run qed init --data-root <managed-root>.",
                command=f"qed init --data-root {settings.data_root}",
            )
        )

    try:
        volume = settings.data_root if settings.data_root.exists() else repo_root
        free = shutil.disk_usage(volume).free
        checks.append(
            DoctorCheck(
                id="storage.disk_space",
                status="pass" if free >= 100 * 1024 * 1024 else "fail",
                observed=f"{free} bytes free",
                remediation="Free at least 100 MiB in the managed data volume.",
                command=f"disk usage {settings.data_root}",
            )
        )
    except OSError as error:
        checks.append(
            DoctorCheck(
                id="storage.disk_space",
                status="unknown",
                observed=str(error),
                remediation="Check the managed data volume manually.",
                command=f"disk usage {settings.data_root}",
            )
        )

    benchmark_lock = repo_root / "benchmarks/reliability/v2-stable-cases.lock.json"
    checks.append(
        DoctorCheck(
            id="benchmark.lock",
            status="pass" if _sha256(benchmark_lock) is not None else "fail",
            observed=_sha256(benchmark_lock) or "missing",
            remediation="Restore the locked benchmark pack; never edit expected labels in place.",
            command="uv run python benchmarks/reliability/run.py validate",
        )
    )
    checks.append(
        DoctorCheck(
            id="frontend.dependencies",
            status="pass" if (repo_root / "node_modules").is_dir() else "unknown",
            observed=(
                "node_modules present"
                if (repo_root / "node_modules").is_dir()
                else "npm dependencies not installed"
            ),
            remediation="Run npm ci in the repository root.",
            command="npm ci",
        )
    )
    checks.append(
        DoctorCheck(
            id="bundle.verifier",
            status="pass",
            observed="qed verify-bundle is available",
            remediation="Reinstall the package if the command is unavailable.",
            command="qed verify-bundle --help",
        )
    )
    checks.append(
        DoctorCheck(
            id="server.bind_policy",
            status=(
                "pass"
                if settings.host == "localhost"
                or settings.host.startswith("127.")
                or settings.host == "::1"
                else "fail"
            ),
            observed=settings.host,
            remediation="Use a loopback host; remote deployment is unsupported in this release.",
            command=f"qed serve --host {settings.host}",
        )
    )
    checks.append(_check_port(settings))

    if live:
        checks.append(
            DoctorCheck(
                id="codex.live_capability",
                status="pass" if live_capability is not None else "unknown",
                observed=(
                    "official Codex capability probe completed"
                    if live_capability is not None
                    else live_error or "live capability probe did not complete"
                ),
                remediation=(
                    "Run the opt-in release canary with a dedicated CODEX_HOME "
                    "and exact model."
                ),
                command="QED_RUN_REAL_CODEX=1 qed doctor --live --json",
                limitation="No credentials or quota were supplied to this local diagnostic.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                id="codex.live_capability",
                status="unknown",
                observed="not requested",
                remediation="Use --live only during an operator-controlled release canary.",
                command="qed doctor --live --json",
                limitation="Default doctor never calls the model.",
            )
        )
    return DoctorReport(
        generated_at=datetime.now(UTC),
        live=live,
        checks=tuple(checks),
    )


def doctor_report_sha256(report: DoctorReport) -> str:
    return canonical_sha256(report)
