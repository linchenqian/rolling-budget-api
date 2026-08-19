from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CategoryConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    icon: str | None = Field(default=None, max_length=32)
    sort_order: int = Field(default=0, ge=0, le=10_000)
    budget_limit: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    budget_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    lookback_days: int = Field(ge=1, le=3650)
    classification_instruction: str = Field(min_length=1, max_length=4000)
    enabled: bool = True


class ConfigPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    timezone: str = Field(default="America/New_York", min_length=1, max_length=64)
    display_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    aggregation_version: int = Field(default=1, ge=1)
    scope_key: str = Field(default="personal", pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    account_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1,
        max_length=100,
    )
    categories: list[CategoryConfigInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_category_keys(self) -> "ConfigPutRequest":
        keys = [category.key for category in self.categories]
        if len(keys) != len(set(keys)):
            raise ValueError("category keys must be unique")
        if len(self.account_ids) != len(set(self.account_ids)):
            raise ValueError("account_ids must be unique")
        return self


class CategoryConfigView(CategoryConfigInput):
    id: str
    rule_version: int
    rule_hash: str


class ConfigVersionView(BaseModel):
    id: str
    version: int
    status: Literal["ACTIVE", "PENDING", "SUPERSEDED"]
    timezone: str
    display_currency: str
    aggregation_version: int
    scope_key: str
    account_ids: list[str]
    config_hash: str
    requires_full_rebuild: bool
    created_at: datetime
    activated_at: datetime | None
    categories: list[CategoryConfigView]


class ConfigView(BaseModel):
    active: ConfigVersionView | None
    pending: ConfigVersionView | None
