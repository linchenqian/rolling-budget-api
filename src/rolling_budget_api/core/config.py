from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        env_ignore_empty=True,
        extra="ignore",
    )

    app_env: str = Field(default="production", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_url: str = Field(
        default="sqlite:////data/budget.db",
        alias="DATABASE_URL",
    )
    api_key: str | None = Field(default=None, alias="API_KEY", min_length=24)
    budget_read_api_key: str | None = Field(
        default=None,
        alias="BUDGET_READ_API_KEY",
        min_length=24,
    )
    budget_write_api_key: str | None = Field(
        default=None,
        alias="BUDGET_WRITE_API_KEY",
        min_length=24,
    )
    budget_admin_api_key: str | None = Field(
        default=None,
        alias="BUDGET_ADMIN_API_KEY",
        min_length=24,
    )
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"],
        alias="CORS_ALLOWED_ORIGINS",
    )
    max_batch_items: int = Field(default=250, ge=1, le=1000, alias="MAX_BATCH_ITEMS")
    max_request_bytes: int = Field(
        default=262_144,
        ge=16_384,
        le=4_194_304,
        alias="MAX_REQUEST_BYTES",
    )
    stale_after_hours: int = Field(default=36, ge=1, le=720, alias="STALE_AFTER_HOURS")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    oauth_consent_secret: str | None = Field(
        default=None,
        alias="OAUTH_CONSENT_SECRET",
        min_length=24,
    )
    oauth_form_action_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["https://chatgpt.com"],
        alias="OAUTH_FORM_ACTION_ORIGINS",
    )
    oauth_authorization_code_ttl_seconds: int = Field(
        default=300,
        ge=60,
        le=900,
        alias="OAUTH_AUTHORIZATION_CODE_TTL_SECONDS",
    )
    oauth_access_token_ttl_seconds: int = Field(
        default=900,
        ge=300,
        le=86_400,
        alias="OAUTH_ACCESS_TOKEN_TTL_SECONDS",
    )
    oauth_refresh_token_ttl_seconds: int = Field(
        default=7_776_000,
        ge=86_400,
        le=31_536_000,
        alias="OAUTH_REFRESH_TOKEN_TTL_SECONDS",
    )
    mcp_max_request_bytes: int = Field(
        default=524_288,
        ge=65_536,
        le=8_388_608,
        alias="MCP_MAX_REQUEST_BYTES",
    )

    @field_validator("cors_allowed_origins", "oauth_form_action_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_wildcard(cls, value: list[str]) -> list[str]:
        if "*" in value and len(value) != 1:
            raise ValueError("CORS_ALLOWED_ORIGINS cannot mix '*' with exact origins")
        return value

    @field_validator("oauth_form_action_origins")
    @classmethod
    def validate_oauth_form_action_origins(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("OAUTH_FORM_ACTION_ORIGINS must contain at least one HTTPS origin")
        normalized: list[str] = []
        for origin in value:
            parsed = urlsplit(origin.strip())
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "OAUTH_FORM_ACTION_ORIGINS must contain exact HTTPS origins"
                )
            exact_origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
            if exact_origin not in normalized:
                normalized.append(exact_origin)
        return normalized

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value.strip())
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        if parsed.scheme != "https" and not is_loopback_http:
            raise ValueError("PUBLIC_BASE_URL must use HTTPS (HTTP is allowed only for loopback)")
        if (
            not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "PUBLIC_BASE_URL must be an origin without credentials, path, or query"
            )
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @model_validator(mode="after")
    def validate_api_keys(self) -> "Settings":
        role_keys = (
            self.budget_read_api_key,
            self.budget_write_api_key,
            self.budget_admin_api_key,
        )
        if self.api_key is None and not all(role_keys):
            raise ValueError(
                "API_KEY is required unless all three role-specific API keys are configured"
            )
        configured = [key for key in (self.api_key, *role_keys) if key is not None]
        if len(configured) != len(set(configured)):
            raise ValueError("All configured API keys must be different")
        if self.mcp_max_request_bytes < self.max_request_bytes + 65_536:
            raise ValueError(
                "MCP_MAX_REQUEST_BYTES must leave at least 65536 bytes for the JSON-RPC envelope"
            )
        if (
            self.public_base_url is not None
            and self.oauth_consent_secret is None
            and self.api_key is None
        ):
            raise ValueError(
                "OAUTH_CONSENT_SECRET is required when remote MCP uses role-specific API keys"
            )
        return self

    @property
    def mcp_public_url(self) -> str | None:
        if self.public_base_url is None:
            return None
        return f"{self.public_base_url}/mcp"

    @property
    def oauth_owner_secret(self) -> str:
        secret = self.oauth_consent_secret or self.api_key
        if secret is None:  # guarded by validate_api_keys
            raise RuntimeError("An owner secret is required")
        return secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
