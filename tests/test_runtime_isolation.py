import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from codex_cli_bin import bundled_codex_path

from qed.runtime.exec import _spawn as spawn_exec
from qed.runtime.isolation import (
    INHERITED_CODEX_AUTH_ENV_VARS,
    SERVER_DISABLED_FEATURES,
    server_config,
    server_config_overrides,
)
from qed.runtime.models import WebSearchMode
from qed.runtime.stdio import _spawn as spawn_stdio
from qed.runtime.stdio import build_app_server_argv


def test_server_config_disables_local_tools_and_preserves_search_and_multi_agent() -> None:
    config = server_config(WebSearchMode.LIVE)

    assert config["approval_policy"] == "never"
    assert config["sandbox_mode"] == "read-only"
    assert config["web_search"] == "live"
    assert config["mcp_servers"] == {}
    assert config["include_apps_instructions"] is False
    assert config["features"] == {
        feature: False for feature in SERVER_DISABLED_FEATURES
    }
    assert {
        "apps",
        "browser_use",
        "code_mode",
        "hooks",
        "plugins",
        "shell_tool",
        "unified_exec",
    }.issubset(SERVER_DISABLED_FEATURES)
    assert "multi_agent" not in SERVER_DISABLED_FEATURES
    assert "multi_agent_v2" not in SERVER_DISABLED_FEATURES
    assert "include_collaboration_mode_instructions" not in config


def test_server_config_overrides_are_a_lossless_cli_projection() -> None:
    overrides = server_config_overrides(WebSearchMode.DISABLED)

    assert 'web_search="disabled"' in overrides
    assert "mcp_servers={}" in overrides
    assert "features.shell_tool=false" in overrides
    assert "features.plugins=false" in overrides
    assert "features.multi_agent=false" not in overrides


async def test_stdio_does_not_inherit_codex_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setenv("QED_RUNTIME_SENTINEL", "preserved")
    for name in INHERITED_CODEX_AUTH_ENV_VARS:
        monkeypatch.setenv(name, f"parent-{name.lower()}")

    async def create_subprocess_exec(
        *_argv: str, **kwargs: Any
    ) -> SimpleNamespace:
        captured.update(kwargs["env"])
        return SimpleNamespace(stdin=object(), stdout=object())

    monkeypatch.setattr(
        "qed.runtime.stdio.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )

    await spawn_stdio(("/opt/codex/bin/codex", "app-server"), Path("/qed/codex"))

    assert captured["QED_RUNTIME_SENTINEL"] == "preserved"
    assert captured["CODEX_HOME"] == "/qed/codex"
    assert not INHERITED_CODEX_AUTH_ENV_VARS & captured.keys()


async def test_exec_does_not_inherit_codex_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("QED_RUNTIME_SENTINEL", "preserved")
    for name in INHERITED_CODEX_AUTH_ENV_VARS:
        monkeypatch.setenv(name, f"parent-{name.lower()}")

    async def create_subprocess_exec(
        *_argv: str, **kwargs: Any
    ) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(stdout=object(), stderr=object())

    monkeypatch.setattr(
        "qed.runtime.exec.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )

    await spawn_exec(
        ("/opt/codex/bin/codex", "exec"),
        Path("/qed/work"),
        Path("/qed/codex"),
    )

    assert captured["cwd"] == Path("/qed/work")
    environment = captured["env"]
    assert environment["QED_RUNTIME_SENTINEL"] == "preserved"
    assert environment["CODEX_HOME"] == "/qed/codex"
    assert not INHERITED_CODEX_AUTH_ENV_VARS & environment.keys()


def test_pinned_codex_parses_server_config_without_user_config_pollution(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user"
    user_config = user_home / ".codex" / "config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('model_reasoning_effort = "max"\n', encoding="utf-8")
    server_codex_home = tmp_path / "server-codex-home"
    server_codex_home.mkdir()
    environment = os.environ.copy()
    environment["HOME"] = str(user_home)
    environment["CODEX_HOME"] = str(server_codex_home)
    for name in INHERITED_CODEX_AUTH_ENV_VARS:
        environment[name] = ""
    executable = bundled_codex_path().resolve(strict=True)

    completed = subprocess.run(  # noqa: S603 - pinned absolute package binary
        build_app_server_argv(executable),
        check=False,
        capture_output=True,
        input="",
        text=True,
        timeout=10,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
