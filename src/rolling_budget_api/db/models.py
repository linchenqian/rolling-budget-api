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
        UniqueConstraint(
            "id",
            "config_version_id",
            name="uq_refresh_runs_id_config",
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
        CheckConstraint("received_batch_count >= 0", name="received_batch_count_nonnegative"),
        CheckConstraint("actual_item_count >= 0", name="actual_item_count_nonnegative"),
        CheckConstraint(
            "sync_revision_before IS NULL OR sync_revision_before >= 0",
            name="sync_revision_before_nonnegative",
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
            "expected_batch_count IS NOT NULL "
            "AND computed_checksum IS NOT NULL "
            "AND expected_batch_count = received_batch_count "
            "AND completed_accounts IS NOT NULL "
            "AND (expected_batch_count = 0 OR uploaded_at IS NOT NULL) "
            "AND validated_at IS NOT NULL "
            "AND committed_at IS NOT NULL)",
            name="committed_refresh_complete",
        ),
        Index("ix_refresh_runs_created", "created_at"),
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
    source_from_date: Mapped[date | None] = mapped_column(Date)
    source_to_date: Mapped[date | None] = mapped_column(Date)
    expected_accounts: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    completed_accounts: Mapped[list[str] | None] = mapped_column(JSON_DOCUMENT)
    expected_batch_count: Mapped[int | None] = mapped_column(Integer)
    computed_checksum: Mapped[str | None] = mapped_column(String(64))
    sync_revision_before: Mapped[int | None] = mapped_column(Integer)
    received_batch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[RefreshRun] = relationship(back_populates="batches")


class StagedTransaction(Base):
    __tablename__ = "staged_transactions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "run_id",
            "account_id",
            "source_id",
            name="pk_staged_transactions",
        ),
        UniqueConstraint(
            "run_id",
            "account_id",
            "source_id",
            "config_version_id",
            name="uq_staged_transactions_identity_config",
        ),
        ForeignKeyConstraint(
            ["run_id", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.config_version_id",
            ],
            name="fk_staged_transactions_run_config",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["run_id", "batch_index"],
            ["refresh_batches.run_id", "refresh_batches.batch_index"],
            name="fk_staged_transactions_batch",
            ondelete="CASCADE",
        ),
        CheckConstraint("batch_index >= 0", name="batch_index_nonnegative"),
        CheckConstraint("amount >= 0", name="amount_nonnegative"),
        CheckConstraint("refund_amount >= 0", name="refund_amount_nonnegative"),
        CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND refund_amount > 0 AND refund_amount <= amount)",
            name="refund_consistent",
        ),
        CheckConstraint("pending IN (false, true)", name="pending_boolean"),
        CheckConstraint("length(source_hash) = 64", name="source_hash_sha256"),
        Index("ix_staged_transactions_run_batch", "run_id", "batch_index"),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255))
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(500))
    name: Mapped[str | None] = mapped_column(Text)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refund_amount: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    pending_source_id: Mapped[str | None] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class StagedTransactionCategory(Base):
    __tablename__ = "staged_transaction_categories"
    __table_args__ = (
        PrimaryKeyConstraint(
            "run_id",
            "account_id",
            "source_id",
            "category_id",
            name="pk_staged_transaction_categories",
        ),
        ForeignKeyConstraint(
            [
                "run_id",
                "account_id",
                "source_id",
                "config_version_id",
            ],
            [
                "staged_transactions.run_id",
                "staged_transactions.account_id",
                "staged_transactions.source_id",
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
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "account_id",
            "source_id",
            name="pk_transactions",
        ),
        UniqueConstraint(
            "account_id",
            "source_id",
            "config_version_id",
            name="uq_transactions_identity_config",
        ),
        ForeignKeyConstraint(
            ["first_refresh_run_id"],
            ["refresh_runs.id"],
            name="fk_transactions_first_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["last_refresh_run_id", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.config_version_id",
            ],
            name="fk_transactions_last_run_config",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount >= 0", name="amount_nonnegative"),
        CheckConstraint("refund_amount >= 0", name="refund_amount_nonnegative"),
        CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND refund_amount > 0 AND refund_amount <= amount)",
            name="refund_consistent",
        ),
        CheckConstraint("pending IN (false, true)", name="pending_boolean"),
        CheckConstraint("length(source_hash) = 64", name="source_hash_sha256"),
        Index("ix_transactions_date", "transaction_date"),
        Index("ix_transactions_pending_date", "pending", "transaction_date"),
    )

    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255))
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(500))
    name: Mapped[str | None] = mapped_column(Text)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refund_amount: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )
    pending_source_id: Mapped[str | None] = mapped_column(String(255))
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
            "account_id",
            "source_id",
            "category_id",
            name="pk_transaction_categories",
        ),
        ForeignKeyConstraint(
            [
                "account_id",
                "source_id",
                "config_version_id",
            ],
            [
                "transactions.account_id",
                "transactions.source_id",
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

    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class SyncState(Base):
    __tablename__ = "sync_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["last_refresh_run_id", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.config_version_id",
            ],
            name="fk_sync_states_last_run_config",
            ondelete="RESTRICT",
        ),
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        default=1,
        server_default=text("1"),
    )
    config_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    last_refresh_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OAuthAuthorizationCode(Base):
    __tablename__ = "oauth_authorization_codes"
    __table_args__ = (
        CheckConstraint("length(code_digest) = 64", name="code_digest_sha256"),
        CheckConstraint("length(code_challenge) = 43", name="code_challenge_s256"),
        CheckConstraint(
            "length(credential_generation) = 64",
            name="credential_generation_sha256",
        ),
        Index("ix_oauth_authorization_codes_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(String(512), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    resource: Mapped[str] = mapped_column(String(1024), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(43), nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthTokenFamily(Base):
    __tablename__ = "oauth_token_families"
    __table_args__ = (
        CheckConstraint(
            "length(credential_generation) = 64",
            name="credential_generation_sha256",
        ),
        Index("ix_oauth_token_families_expires_at", "expires_at"),
        Index("ix_oauth_token_families_revoked_at", "revoked_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    client_id: Mapped[str] = mapped_column(String(512), nullable=False)
    resource: Mapped[str] = mapped_column(String(1024), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compromise_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class OAuthAccessToken(Base):
    __tablename__ = "oauth_access_tokens"
    __table_args__ = (
        CheckConstraint("length(token_digest) = 64", name="token_digest_sha256"),
        Index("ix_oauth_access_tokens_expires_at", "expires_at"),
        Index("ix_oauth_access_tokens_family_id", "family_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("oauth_token_families.id", ondelete="CASCADE"),
        nullable=False,
    )
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthRefreshToken(Base):
    __tablename__ = "oauth_refresh_tokens"
    __table_args__ = (
        CheckConstraint("length(token_digest) = 64", name="token_digest_sha256"),
        Index("ix_oauth_refresh_tokens_expires_at", "expires_at"),
        Index("ix_oauth_refresh_tokens_family_id", "family_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("oauth_token_families.id", ondelete="CASCADE"),
        nullable=False,
    )
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
