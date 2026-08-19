from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RefreshBeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["INCREMENTAL", "FULL_REBUILD"]
    scope_key: str = Field(default="personal", pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    source_from_date: date
    source_to_date: date
    expected_accounts: list[str] = Field(min_length=1, max_length=100)
    cursor_before: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_dates_and_accounts(self) -> "RefreshBeginRequest":
        if self.source_from_date > self.source_to_date:
            raise ValueError("source_from_date cannot be after source_to_date")
        if len(self.expected_accounts) != len(set(self.expected_accounts)):
            raise ValueError("expected_accounts must be unique")
        return self


class RefreshBeginResponse(BaseModel):
    run_id: UUID
    state: str
    mode: str
    config_version_id: UUID
    config_version: int
    rules: list[dict[str, object]]
    max_batch_items: int
    max_request_bytes: int


class TransactionUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_id: str = Field(min_length=1, max_length=128)
    source_transaction_id: str = Field(min_length=1, max_length=192)
    decision: Literal["STORE", "SKIP"]
    transaction_date: date
    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    status: Literal["PENDING", "POSTED"]
    merchant_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=1000)
    category_keys: list[str] = Field(default_factory=list, max_length=100)
    refunded: bool = False
    refund_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=4)
    supersedes_source_transaction_id: str | None = Field(default=None, max_length=192)

    @model_validator(mode="after")
    def validate_decision(self) -> "TransactionUpload":
        if self.decision == "STORE" and not self.category_keys:
            raise ValueError("STORE requires at least one category_key")
        if self.decision == "SKIP" and self.category_keys:
            raise ValueError("SKIP cannot include category_keys")
        if len(self.category_keys) != len(set(self.category_keys)):
            raise ValueError("category_keys cannot contain duplicates")
        if self.refund_amount > self.amount:
            raise ValueError("refund_amount cannot exceed amount")
        if self.refunded != (self.refund_amount > 0):
            raise ValueError("refunded must match whether refund_amount is positive")
        return self


class RefreshBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=255)
    transactions: list[TransactionUpload] = Field(min_length=1, max_length=1000)


class RefreshBatchResponse(BaseModel):
    run_id: UUID
    batch_index: int
    checksum: str
    item_count: int
    store_count: int
    skip_count: int
    replayed: bool


class AccountManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_id: str = Field(min_length=1, max_length=255)
    pages_complete: bool
    observed_count: int = Field(ge=0)
    source_reported_count: int | None = Field(default=None, ge=0)


class RefreshCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_batch_count: int = Field(ge=0)
    expected_item_count: int = Field(ge=0)
    expected_store_count: int = Field(ge=0)
    expected_skip_count: int = Field(ge=0)
    ordered_batch_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    accounts: list[AccountManifest] = Field(min_length=1, max_length=100)
    cursor_after: dict[str, Any] | None = None
    source_complete: bool = True

    @model_validator(mode="after")
    def validate_counts(self) -> "RefreshCommitRequest":
        if self.expected_store_count + self.expected_skip_count != self.expected_item_count:
            raise ValueError("store_count + skip_count must equal item_count")
        account_ids = [account.account_id for account in self.accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("account manifests must be unique")
        return self


class RefreshRunView(BaseModel):
    run_id: UUID
    state: str
    mode: str
    config_version_id: UUID
    batch_count: int
    item_count: int
    store_count: int
    skip_count: int
    input_checksum: str | None
    receipt: str | None
    created_at: datetime
    committed_at: datetime | None
    error_code: str | None
