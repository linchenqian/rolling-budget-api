from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, TypeEngine

from .base import Base

MONEY_PRECISION = 18
MONEY_SCALE = 4
MONEY_FACTOR = Decimal(10) ** MONEY_SCALE
MONEY_QUANTUM = Decimal(1) / MONEY_FACTOR
JSON_DOCUMENT = JSON().with_variant(JSONB(none_as_null=True), "postgresql")


class Money(TypeDecorator[Decimal]):
    """Exact NUMERIC on PostgreSQL and scaled integer storage on SQLite."""

    impl = Numeric(MONEY_PRECISION, MONEY_SCALE)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(Numeric(MONEY_PRECISION, MONEY_SCALE))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Decimal | int | None:
        if value is None:
            return None
        normalized = Decimal(value).quantize(MONEY_QUANTUM)
        if dialect.name == "sqlite":
            return int(normalized * MONEY_FACTOR)
        return normalized

    def process_result_value(
        self,
        value: Decimal | int | None,
        dialect: Dialect,
    ) -> Decimal | None:
        if value is None:
            return None
        if dialect.name == "sqlite":
            return Decimal(value) / MONEY_FACTOR
        return Decimal(value).quantize(MONEY_QUANTUM)


class ConfigVersionStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class RefreshMode(enum.StrEnum):
    INCREMENTAL = "incremental"
    FULL = "full"


class RefreshRunState(enum.StrEnum):
    CREATED = "created"
    UPLOADED = "uploaded"
    VALIDATED = "validated"
    COMMITTED = "committed"
    FAILED = "failed"


class StagedDecision(enum.StrEnum):
    STORE = "store"
    SKIP = "skip"


class TransactionStatus(enum.StrEnum):
    PENDING = "pending"
    POSTED = "posted"


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_class]


CONFIG_VERSION_STATUS = SAEnum(
    ConfigVersionStatus,
    name="config_version_status",
    values_callable=_enum_values,
)
REFRESH_MODE = SAEnum(
    RefreshMode,
    name="refresh_mode",
    values_callable=_enum_values,
)
REFRESH_RUN_STATE = SAEnum(
    RefreshRunState,
    name="refresh_run_state",
    values_callable=_enum_values,
)
STAGED_DECISION = SAEnum(
    StagedDecision,
    name="staged_decision",
    values_callable=_enum_values,
)
TRANSACTION_STATUS = SAEnum(
    TransactionStatus,
    name="transaction_status",
    values_callable=_enum_values,
)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("budget_limit >= 0", name="budget_limit_nonnegative"),
        CheckConstraint("sort_order >= 0", name="sort_order_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_limit: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    budget_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    rule_versions: Mapped[list[RuleVersion]] = relationship(back_populates="category")


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("category_id", "version", name="uq_rule_versions_category_version"),
        UniqueConstraint("category_id", "rule_hash", name="uq_rule_versions_category_hash"),
        UniqueConstraint("id", "category_id", name="uq_rule_versions_id_category"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "lookback_days > 0 AND lookback_days <= 3660",
            name="lookback_days_range",
        ),
        CheckConstraint("length(rule_hash) = 64", name="rule_hash_sha256"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    category_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False)
    classification_instruction: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rule_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    category: Mapped[Category] = relationship(back_populates="rule_versions")
    config_links: Mapped[list[ConfigVersionRule]] = relationship(back_populates="rule_version")


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("aggregation_version > 0", name="aggregation_version_positive"),
        CheckConstraint("length(config_hash) = 64", name="config_hash_sha256"),
        CheckConstraint(
            "(status != 'active' OR activated_at IS NOT NULL)",
            name="active_has_timestamp",
        ),
        CheckConstraint(
            "(status != 'superseded' OR superseded_at IS NOT NULL)",
            name="superseded_has_timestamp",
        ),
        Index(
            "uq_config_versions_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_config_versions_config_hash", "config_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        CONFIG_VERSION_STATUS,
        nullable=False,
        default=ConfigVersionStatus.PENDING,
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    display_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    aggregation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_config: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rule_links: Mapped[list[ConfigVersionRule]] = relationship(back_populates="config_version")
    refresh_runs: Mapped[list[RefreshRun]] = relationship(back_populates="config_version")


class ConfigVersionRule(Base):
    __tablename__ = "config_version_rules"
    __table_args__ = (
        PrimaryKeyConstraint("config_version_id", "category_id", name="pk_config_version_rules"),
        UniqueConstraint(
            "config_version_id",
            "category_id",
            "rule_version_id",
            name="uq_config_version_rules_mapping",
        ),
        ForeignKeyConstraint(
            ["rule_version_id", "category_id"],
            ["rule_versions.id", "rule_versions.category_id"],
            name="fk_config_version_rules_rule_category",
            ondelete="RESTRICT",
        ),
    )

    config_version_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("config_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    config_version: Mapped[ConfigVersion] = relationship(back_populates="rule_links")
    rule_version: Mapped[RuleVersion] = relationship(back_populates="config_links")


class RefreshRun(Base):
    __tablename__ = "refresh_runs"
    __table_args__ = (
        UniqueConstraint("id", "scope_key", name="uq_refresh_runs_id_scope"),
        UniqueConstraint(
            "id",
            "scope_key",
            "config_version_id",
            name="uq_refresh_runs_id_scope_config",
        ),
        CheckConstraint(
            "source_from_date IS NULL OR source_to_date IS NULL "
            "OR source_from_date <= source_to_date",
            name="source_date_range",
        ),
        CheckConstraint(
            "expected_batch_count IS NULL OR expected_batch_count >= 0",
            name="expected_batch_count_nonnegative",
        ),
        CheckConstraint(
            "expected_source_count IS NULL OR expected_source_count >= 0",
            name="expected_source_count_nonnegative",
        ),
        CheckConstraint(
            "expected_store_count IS NULL OR expected_store_count >= 0",
            name="expected_store_count_nonnegative",
        ),
        CheckConstraint(
            "expected_skip_count IS NULL OR expected_skip_count >= 0",
            name="expected_skip_count_nonnegative",
        ),
        CheckConstraint("received_batch_count >= 0", name="received_batch_count_nonnegative"),
        CheckConstraint("actual_source_count >= 0", name="actual_source_count_nonnegative"),
        CheckConstraint("actual_store_count >= 0", name="actual_store_count_nonnegative"),
        CheckConstraint("actual_skip_count >= 0", name="actual_skip_count_nonnegative"),
        CheckConstraint(
            "actual_store_count + actual_skip_count = actual_source_count",
            name="actual_counts_match",
        ),
        CheckConstraint(
            "expected_source_count IS NULL OR expected_store_count IS NULL "
            "OR expected_skip_count IS NULL "
            "OR expected_store_count + expected_skip_count = expected_source_count",
            name="expected_counts_match",
        ),
        CheckConstraint(
            "input_checksum IS NULL OR length(input_checksum) = 64",
            name="input_checksum_sha256",
        ),
        CheckConstraint("length(request_hash) = 64", name="request_hash_sha256"),
        CheckConstraint(
            "computed_checksum IS NULL OR length(computed_checksum) = 64",
            name="computed_checksum_sha256",
        ),
        CheckConstraint(
            "state != 'failed' OR failed_at IS NOT NULL",
            name="failed_has_timestamp",
        ),
        CheckConstraint(
            "state != 'committed' OR ("
            "source_complete "
            "AND expected_batch_count IS NOT NULL "
            "AND expected_source_count IS NOT NULL "
            "AND expected_store_count IS NOT NULL "
            "AND expected_skip_count IS NOT NULL "
            "AND input_checksum IS NOT NULL "
            "AND computed_checksum IS NOT NULL "
            "AND input_checksum = computed_checksum "
            "AND expected_batch_count = received_batch_count "
            "AND expected_source_count = actual_source_count "
            "AND expected_store_count = actual_store_count "
            "AND expected_skip_count = actual_skip_count "
            "AND account_manifest IS NOT NULL "
            "AND (expected_batch_count = 0 OR uploaded_at IS NOT NULL) "
            "AND validated_at IS NOT NULL "
            "AND committed_at IS NOT NULL)",
            name="committed_manifest_complete",
        ),
        Index("ix_refresh_runs_scope_created", "scope_key", "created_at"),
        Index("ix_refresh_runs_state", "state"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[RefreshMode] = mapped_column(REFRESH_MODE, nullable=False)
    state: Mapped[RefreshRunState] = mapped_column(
        REFRESH_RUN_STATE,
        nullable=False,
        default=RefreshRunState.CREATED,
    )
    config_version_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("config_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_from_date: Mapped[date | None] = mapped_column(Date)
    source_to_date: Mapped[date | None] = mapped_column(Date)
    expected_accounts: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    account_manifest: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON_DOCUMENT)
    expected_batch_count: Mapped[int | None] = mapped_column(Integer)
    expected_source_count: Mapped[int | None] = mapped_column(Integer)
    expected_store_count: Mapped[int | None] = mapped_column(Integer)
    expected_skip_count: Mapped[int | None] = mapped_column(Integer)
    source_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_checksum: Mapped[str | None] = mapped_column(String(64))
    computed_checksum: Mapped[str | None] = mapped_column(String(64))
    cursor_before: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    cursor_after: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    received_batch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_store_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_skip_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))

    config_version: Mapped[ConfigVersion] = relationship(back_populates="refresh_runs")
    batches: Mapped[list[RefreshBatch]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RefreshBatch(Base):
    __tablename__ = "refresh_batches"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "batch_index", name="pk_refresh_batches"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_refresh_batches_run_idempotency"),
        CheckConstraint("batch_index >= 0", name="batch_index_nonnegative"),
        CheckConstraint("item_count >= 0", name="item_count_nonnegative"),
        CheckConstraint("store_count >= 0", name="store_count_nonnegative"),
        CheckConstraint("skip_count >= 0", name="skip_count_nonnegative"),
        CheckConstraint("store_count + skip_count = item_count", name="item_count_matches"),
        CheckConstraint("length(request_hash) = 64", name="request_hash_sha256"),
        CheckConstraint("length(checksum) = 64", name="checksum_sha256"),
    )

    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("refresh_runs.id", ondelete="CASCADE"), nullable=False
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    store_count: Mapped[int] = mapped_column(Integer, nullable=False)
    skip_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[RefreshRun] = relationship(back_populates="batches")


class StagedTransaction(Base):
    __tablename__ = "staged_transactions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "run_id",
            "scope_key",
            "account_id",
            "source_transaction_id",
            name="pk_staged_transactions",
        ),
        UniqueConstraint(
            "run_id",
            "scope_key",
            "account_id",
            "source_transaction_id",
            "config_version_id",
            name="uq_staged_transactions_identity_config",
        ),
        ForeignKeyConstraint(
            ["run_id", "scope_key", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.scope_key",
                "refresh_runs.config_version_id",
            ],
            name="fk_staged_transactions_run_scope_config",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "batch_index"],
            ["refresh_batches.run_id", "refresh_batches.batch_index"],
            name="fk_staged_transactions_batch",
            ondelete="CASCADE",
        ),
        CheckConstraint("batch_index >= 0", name="batch_index_nonnegative"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount_nonnegative"),
        CheckConstraint("refund_amount >= 0", name="refund_amount_nonnegative"),
        CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND amount IS NOT NULL AND refund_amount > 0 "
            "AND refund_amount <= amount)",
            name="refund_consistent",
        ),
        CheckConstraint(
            "decision != 'store' OR "
            "(transaction_date IS NOT NULL AND amount IS NOT NULL "
            "AND currency IS NOT NULL AND status IS NOT NULL)",
            name="stored_fields_present",
        ),
        CheckConstraint("length(source_hash) = 64", name="source_hash_sha256"),
        Index("ix_staged_transactions_run_batch", "run_id", "batch_index"),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_transaction_id: Mapped[str] = mapped_column(String(255), nullable=False)
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[StagedDecision] = mapped_column(STAGED_DECISION, nullable=False)
    transaction_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal | None] = mapped_column(Money())
    currency: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[TransactionStatus | None] = mapped_column(TRANSACTION_STATUS)
    merchant: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refund_amount: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    supersedes_source_transaction_id: Mapped[str | None] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class StagedTransactionCategory(Base):
    __tablename__ = "staged_transaction_categories"
    __table_args__ = (
        PrimaryKeyConstraint(
            "run_id",
            "scope_key",
            "account_id",
            "source_transaction_id",
            "category_id",
            name="pk_staged_transaction_categories",
        ),
        ForeignKeyConstraint(
            [
                "run_id",
                "scope_key",
                "account_id",
                "source_transaction_id",
                "config_version_id",
            ],
            [
                "staged_transactions.run_id",
                "staged_transactions.scope_key",
                "staged_transactions.account_id",
                "staged_transactions.source_transaction_id",
                "staged_transactions.config_version_id",
            ],
            name="fk_staged_transaction_categories_transaction",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["config_version_id", "category_id", "rule_version_id"],
            [
                "config_version_rules.config_version_id",
                "config_version_rules.category_id",
                "config_version_rules.rule_version_id",
            ],
            name="fk_staged_transaction_categories_config_rule",
            ondelete="RESTRICT",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_transaction_id: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "scope_key",
            "account_id",
            "source_transaction_id",
            name="pk_transactions",
        ),
        UniqueConstraint(
            "scope_key",
            "account_id",
            "source_transaction_id",
            "config_version_id",
            name="uq_transactions_identity_config",
        ),
        ForeignKeyConstraint(
            ["first_refresh_run_id", "scope_key"],
            ["refresh_runs.id", "refresh_runs.scope_key"],
            name="fk_transactions_first_run_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["last_refresh_run_id", "scope_key", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.scope_key",
                "refresh_runs.config_version_id",
            ],
            name="fk_transactions_last_run_scope_config",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount >= 0", name="amount_nonnegative"),
        CheckConstraint("refund_amount >= 0", name="refund_amount_nonnegative"),
        CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND refund_amount > 0 AND refund_amount <= amount)",
            name="refund_consistent",
        ),
        CheckConstraint("length(source_hash) = 64", name="source_hash_sha256"),
        Index("ix_transactions_scope_date", "scope_key", "transaction_date"),
        Index("ix_transactions_scope_status_date", "scope_key", "status", "transaction_date"),
    )

    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_transaction_id: Mapped[str] = mapped_column(String(255), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[TransactionStatus] = mapped_column(TRANSACTION_STATUS, nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refund_amount: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    supersedes_source_transaction_id: Mapped[str | None] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    first_refresh_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    last_refresh_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TransactionCategory(Base):
    __tablename__ = "transaction_categories"
    __table_args__ = (
        PrimaryKeyConstraint(
            "scope_key",
            "account_id",
            "source_transaction_id",
            "category_id",
            name="pk_transaction_categories",
        ),
        ForeignKeyConstraint(
            [
                "scope_key",
                "account_id",
                "source_transaction_id",
                "config_version_id",
            ],
            [
                "transactions.scope_key",
                "transactions.account_id",
                "transactions.source_transaction_id",
                "transactions.config_version_id",
            ],
            name="fk_transaction_categories_transaction",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["config_version_id", "category_id", "rule_version_id"],
            [
                "config_version_rules.config_version_id",
                "config_version_rules.category_id",
                "config_version_rules.rule_version_id",
            ],
            name="fk_transaction_categories_config_rule",
            ondelete="RESTRICT",
        ),
        Index("ix_transaction_categories_category", "category_id"),
    )

    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_transaction_id: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class SyncState(Base):
    __tablename__ = "sync_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["last_refresh_run_id", "scope_key", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.scope_key",
                "refresh_runs.config_version_id",
            ],
            name="fk_sync_states_last_run_scope_config",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        CheckConstraint("length(cursor_hash) = 64", name="cursor_hash_sha256"),
    )

    scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    cursor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    last_refresh_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
