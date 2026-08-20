from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

AccountId = Annotated[str, Field(min_length=1, max_length=128)]


class RefreshBeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["INCREMENTAL", "FULL_REBUILD"]
    source_from_date: Date
    source_to_date: Date
    expected_accounts: list[AccountId] = Field(
        min_length=1,
        max_length=100,
        description="Every stable source account ID enumerated for this refresh.",
    )

    @model_validator(mode="after")
    def validate_dates_and_accounts(self) -> "RefreshBeginRequest":
        if self.source_from_date > self.source_to_date:
            raise ValueError("source_from_date cannot be after source_to_date")
        if len(self.expected_accounts) != len(set(self.expected_accounts)):
            raise ValueError("expected_accounts must be unique")
        # Account order has no meaning. Canonicalize it so a retry that enumerates the
        # same accounts in a different source order retains the same request hash.
        self.expected_accounts = sorted(self.expected_accounts)
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

    account_id: AccountId = Field(
        description="Stable source account ID. This, not account_name, identifies the account."
    )
    account_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Optional display label only; never use it as account identity.",
    )
    source_id: str = Field(
        min_length=1,
        max_length=192,
        description="Stable source transaction ID within account_id.",
    )
    date: Date = Field(description="Transaction date in the configured local-date semantics.")
    amount: Decimal = Field(
        ge=0,
        max_digits=18,
        decimal_places=4,
        description="Nonnegative amount normalized to the configured display currency.",
    )
    currency: str = Field(
        default="USD",
        pattern=r"^[A-Z]{3}$",
        description="Must equal the target configuration's display currency.",
    )
    categories: list[str] = Field(
        min_length=1,
        max_length=100,
        description="Every matching enabled category key returned by begin_refresh.",
    )
    pending: bool = Field(description="Copy the source's pending state; pending spend is counted.")
    pending_source_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=192,
        description=(
            "Prior pending source ID only when the source explicitly links it to this posted "
            "transaction. Never infer from merchant, amount, or date."
        ),
    )
    name: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional untrusted source transaction label.",
    )
    merchant: str | None = Field(
        default=None,
        max_length=256,
        description="Optional untrusted source merchant label.",
    )
    refunded: bool = Field(
        default=False,
        description="True only when refund_amount reliably belongs to this original transaction.",
    )
    refund_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=18,
        decimal_places=4,
        description="Refund total applied to this original transaction; never guess the link.",
    )

    @model_validator(mode="after")
    def validate_transaction(self) -> "TransactionUpload":
        if len(self.categories) != len(set(self.categories)):
            raise ValueError("categories cannot contain duplicates")
        self.categories = sorted(self.categories)
        if self.refund_amount > self.amount:
            raise ValueError("refund_amount cannot exceed amount")
        if self.refunded != (self.refund_amount > 0):
            raise ValueError("refunded must match whether refund_amount is positive")
        if self.pending_source_id is not None:
            if self.pending:
                raise ValueError("pending_source_id is valid only on a posted transaction")
            if self.pending_source_id == self.source_id:
                raise ValueError("pending_source_id cannot equal source_id")
        return self


class RefreshBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=255)
    transactions: list[TransactionUpload] = Field(min_length=1, max_length=1000)


class RefreshBatchResponse(BaseModel):
    run_id: UUID
    batch_index: int
    item_count: int
    replayed: bool


class RefreshCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_batch_count: int = Field(ge=0)
    completed_accounts: list[AccountId] = Field(
        min_length=1,
        max_length=100,
        description="Exact expected_accounts set, including accounts that returned zero matches.",
    )

    @model_validator(mode="after")
    def validate_completed_accounts(self) -> "RefreshCommitRequest":
        if len(self.completed_accounts) != len(set(self.completed_accounts)):
            raise ValueError("completed_accounts must be unique")
        self.completed_accounts = sorted(self.completed_accounts)
        return self


class RefreshRunView(BaseModel):
    run_id: UUID
    state: str
    mode: str
    config_version_id: UUID
    batch_count: int
    item_count: int
    receipt: str | None
    created_at: datetime
    committed_at: datetime | None
    error_code: str | None
