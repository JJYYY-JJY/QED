from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path

import pytest
from pydantic import ValidationError

from qed.service_settings import ServiceSettings


def test_service_defaults_to_local_only_managed_state(tmp_path: Path) -> None:
    settings = ServiceSettings(data_root=tmp_path)

    assert settings.host == "127.0.0.1"
    assert settings.auth_required is False
    assert settings.database_path == tmp_path / "qed.sqlite3"
    assert settings.codex_home == tmp_path / "codex-home"
    assert settings.allowed_origins == ("http://127.0.0.1:5173", "http://localhost:5173")


def test_non_loopback_bind_is_fail_closed_even_with_a_bearer_token(tmp_path: Path) -> None:
    all_interfaces = str(ip_address(0))
    with pytest.raises(ValidationError, match="non-loopback"):
        ServiceSettings(data_root=tmp_path, host=all_interfaces)

    with pytest.raises(ValidationError, match="non-loopback"):
        ServiceSettings(
            data_root=tmp_path,
            host=all_interfaces,
            auth_token="a" * 32,
        )


@pytest.mark.parametrize("database_name", ("../qed.sqlite3", "state/qed.sqlite3"))
def test_database_name_cannot_escape_the_managed_root(
    tmp_path: Path, database_name: str
) -> None:
    with pytest.raises(ValidationError, match="plain filename"):
        ServiceSettings(data_root=tmp_path, database_name=database_name)


def test_cors_wildcards_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        ServiceSettings(data_root=tmp_path, allowed_origins=("*",))
