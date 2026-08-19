from functools import lru_cache
from typing import Annotated

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

    @field_validator("cors_allowed_origins", mode="before")
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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
