from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from qed.runtime import (
    CapabilityRequest,
    FreshThread,
    RunRequest,
    RuntimeBackend,
    RuntimePreference,
    SandboxMode,
    ThreadStarted,
    TurnCompleted,
    WebSearchMode,
    WorkRole,
    create_codex_runtime,
)

MODEL = "gpt-5.6-sol"
EFFORT = "low"


class SmokeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    ok: Literal[True]


def _required_dedicated_roots() -> tuple[Path, Path]:
    if os.environ.get("QED_RUN_REAL_CODEX") != "1":
        pytest.skip("set QED_RUN_REAL_CODEX=1 to permit a real Codex call")

    values: dict[str, Path] = {}
    for name in ("QED_REAL_CODEX_DATA_ROOT", "QED_REAL_CODEX_HOME"):
        raw = os.environ.get(name)
        if raw is None:
            pytest.skip(f"set {name} to an existing absolute dedicated directory")
        path = Path(raw)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            pytest.skip(f"{name} must be an existing absolute non-symlink directory")
        values[name] = path.resolve(strict=True)

    data_root = values["QED_REAL_CODEX_DATA_ROOT"]
    codex_home = values["QED_REAL_CODEX_HOME"]
    if codex_home != (data_root / "codex-home").resolve(strict=False):
        pytest.skip("QED_REAL_CODEX_HOME must be <QED_REAL_CODEX_DATA_ROOT>/codex-home")

    personal_codex_home = (Path.home() / ".codex").resolve(strict=False)
    if codex_home == personal_codex_home or codex_home.is_relative_to(personal_codex_home):
        pytest.skip("the smoke test refuses to use the personal ~/.codex tree")
    return data_root, codex_home


def _initialize_empty_git_workspace(data_root: Path) -> tempfile.TemporaryDirectory[str]:
    git_path = shutil.which("git")
    if git_path is None:
        pytest.skip("git is required for an isolated real-Codex workspace")
    git = Path(git_path).resolve(strict=True)
    workspace = tempfile.TemporaryDirectory(prefix=".real-codex-", dir=data_root)
    template = tempfile.TemporaryDirectory(prefix=".git-template-", dir=data_root)
    try:
        subprocess.run(  # noqa: S603 - resolved executable and fixed argv
            (
                str(git),
                "init",
                "--quiet",
                f"--template={template.name}",
                "--initial-branch=qed",
                workspace.name,
            ),
            check=True,
            capture_output=True,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "LC_ALL": "C",
            },
        )
    except BaseException:
        workspace.cleanup()
        raise
    finally:
        template.cleanup()
    return workspace


@pytest.mark.real_codex
async def test_authenticated_runtime_completes_schema_constrained_offline_turn() -> None:
    data_root, codex_home = _required_dedicated_roots()
    workspace = _initialize_empty_git_workspace(data_root)
    try:
        runtime = create_codex_runtime(codex_home)
        try:
            capabilities = await runtime.probe(CapabilityRequest(model=MODEL, effort=EFFORT))
            assert capabilities.model == MODEL
            assert capabilities.selected_effort == EFFORT
            assert EFFORT in capabilities.advertised_efforts

            request = RunRequest(
                model=MODEL,
                effort=capabilities.selected_effort,
                prompt=(
                    "Return schema_version 1 and ok true as the schema-constrained "
                    "final response. Do not perform any other work."
                ),
                output_schema=SmokeOutput.model_json_schema(),
                thread=FreshThread(),
                role=WorkRole.VERIFIER,
                sandbox=SandboxMode.READ_ONLY,
                web_search=WebSearchMode.DISABLED,
                runtime=RuntimePreference.SDK,
                cwd=Path(workspace.name),
            )
            events = [event async for event in runtime.stream(request)]
        finally:
            await runtime.close()
    finally:
        workspace.cleanup()

    threads = [event for event in events if isinstance(event, ThreadStarted)]
    terminals = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(threads) == 1
    assert threads[0].backend is RuntimeBackend.SDK
    assert len(terminals) == 1
    assert terminals[0].status == "completed"
    assert terminals[0].turn.thread_id == threads[0].thread_id
    assert terminals[0].parse_output_as(SmokeOutput) == SmokeOutput(
        schema_version=1,
        ok=True,
    )
