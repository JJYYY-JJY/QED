"""Server-owned Codex controls shared by every runtime adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import WebSearchMode

INHERITED_CODEX_AUTH_ENV_VARS = (
    "CODEX_ACCESS_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
)

SERVER_DISABLED_FEATURES = frozenset(
    {
        "apps",
        "artifact",
        "auth_elicitation",
        "browser_use",
        "browser_use_external",
        "chronicle",
        "code_mode",
        "code_mode_only",
        "computer_use",
        "enable_fanout",
        "enable_mcp_apps",
        "exec_permission_approvals",
        "goals",
        "hooks",
        "image_generation",
        "in_app_browser",
        "memories",
        "network_proxy",
        "plugin_sharing",
        "plugins",
        "remote_plugin",
        "request_permissions_tool",
        "shell_snapshot",
        "shell_tool",
        "skill_mcp_dependency_install",
        "tool_call_mcp_elicitation",
        "tool_suggest",
        "unified_exec",
        "workspace_dependencies",
    }
)


def codex_home_environment(codex_home: Path) -> dict[str, str]:
    """Return the process override for one server-owned Codex state root."""

    if not codex_home.is_absolute():
        raise ValueError("server-owned Codex home must be absolute")
    return {
        **{name: "" for name in INHERITED_CODEX_AUTH_ENV_VARS},
        "CODEX_HOME": str(codex_home),
    }


def codex_subprocess_environment(codex_home: Path) -> dict[str, str]:
    """Return an inherited environment without ambient Codex authentication."""

    environment = os.environ.copy()
    for name in INHERITED_CODEX_AUTH_ENV_VARS:
        environment.pop(name, None)
    environment["CODEX_HOME"] = codex_home_environment(codex_home)["CODEX_HOME"]
    return environment


def prepare_codex_home(codex_home: Path) -> Path:
    """Create and lock down one persistent server-owned Codex state root."""

    expanded = codex_home.expanduser()
    if expanded.is_symlink():
        raise ValueError("server-owned Codex home cannot be a symbolic link")
    expanded.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = expanded.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(f"server-owned Codex home is not a directory: {resolved}")
    resolved.chmod(0o700)
    return resolved


def server_config(web_search: WebSearchMode) -> dict[str, Any]:
    """Return the complete per-turn configuration owned by the QED server."""

    return {
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "web_search": web_search.value,
        "mcp_servers": {},
        "include_apps_instructions": False,
        "features": {
            feature: False for feature in sorted(SERVER_DISABLED_FEATURES)
        },
    }


def server_config_overrides(web_search: WebSearchMode) -> tuple[str, ...]:
    """Project the server configuration into Codex ``-c key=value`` values."""

    config = server_config(web_search)
    overrides = [
        f"approval_policy={json.dumps(config['approval_policy'])}",
        f"sandbox_mode={json.dumps(config['sandbox_mode'])}",
        f"web_search={json.dumps(config['web_search'])}",
        "mcp_servers={}",
        "include_apps_instructions=false",
    ]
    overrides.extend(
        f"features.{feature}=false" for feature in sorted(SERVER_DISABLED_FEATURES)
    )
    return tuple(overrides)
