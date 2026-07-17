"""Server-only settings kept outside immutable research run snapshots."""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """Process settings sourced from environment variables or trusted CLI input."""

    model_config = SettingsConfigDict(
        env_prefix="QED_",
        extra="forbid",
        frozen=True,
        case_sensitive=False,
    )

    data_root: Path = Path(".qed")
    database_name: str = "qed.sqlite3"
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    auth_token: Annotated[SecretStr, Field(min_length=32)] | None = None
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    @field_validator("database_name")
    @classmethod
    def validate_database_name(cls, value: str) -> str:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError("database_name must be a plain filename")
        return value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if value == "localhost":
            return value
        try:
            ip_address(value)
        except ValueError as exc:
            raise ValueError("host must be localhost or an IP address") from exc
        return value

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("allowed_origins cannot be empty")
        for value in values:
            parsed = urlsplit(value)
            if "*" in value:
                raise ValueError("CORS wildcard origins are forbidden")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"invalid CORS origin: {value}")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("CORS origins cannot contain credentials")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("CORS origins cannot contain paths, queries, or fragments")
        return values

    @model_validator(mode="after")
    def require_auth_for_remote_bind(self) -> Self:
        loopback = self.host == "localhost" or ip_address(self.host).is_loopback
        if not loopback and self.auth_token is None:
            raise ValueError("a bearer token is required for a non-loopback bind")
        return self

    @property
    def database_path(self) -> Path:
        return self.data_root / self.database_name

    @property
    def codex_home(self) -> Path:
        return self.data_root / "codex-home"

    @property
    def auth_required(self) -> bool:
        return self.auth_token is not None
