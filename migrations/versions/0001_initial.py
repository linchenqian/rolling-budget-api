"""Create the integrity-first rolling budget schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from migrations.sqlite_support import downgrade_sqlite, upgrade_sqlite

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


config_version_status = postgresql.ENUM(
    "pending", "active", "superseded", name="config_version_status", create_type=False
)
refresh_mode = postgresql.ENUM(
    "incremental", "full", name="refresh_mode", create_type=False
)
refresh_run_state = postgresql.ENUM(
    "created",
    "uploaded",
    "validated",
    "committed",
    "failed",
    name="refresh_run_state",
    create_type=False,
)
staged_decision = postgresql.ENUM(
    "store", "skip", name="staged_decision", create_type=False
)
transaction_status = postgresql.ENUM(
    "pending", "posted", name="transaction_status", create_type=False
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        upgrade_sqlite()
        return
    if dialect != "postgresql":
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    _upgrade_postgresql()


def _upgrade_postgresql() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        "pending", "active", "superseded", name="config_version_status"
    ).create(bind, checkfirst=True)
    postgresql.ENUM("incremental", "full", name="refresh_mode").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(
        "created",
        "uploaded",
        "validated",
        "committed",
        "failed",
        name="refresh_run_state",
    ).create(bind, checkfirst=True)
    postgresql.ENUM("store", "skip", name="staged_decision").create(
        bind, checkfirst=True
    )
    postgresql.ENUM("pending", "posted", name="transaction_status").create(
        bind, checkfirst=True
    )

    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("icon", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("budget_limit", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("budget_currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("budget_limit >= 0", name="ck_categories_budget_limit_nonnegative"),
        sa.CheckConstraint("sort_order >= 0", name="ck_categories_sort_order_nonnegative"),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("key", name="uq_categories_key"),
    )

    op.create_table(
        "rule_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column("classification_instruction", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("rule_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_rule_versions_version_positive"),
        sa.CheckConstraint(
            "lookback_days > 0 AND lookback_days <= 3660",
            name="ck_rule_versions_lookback_days_range",
        ),
        sa.CheckConstraint("length(rule_hash) = 64", name="ck_rule_versions_rule_hash_sha256"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_rule_versions_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rule_versions"),
        sa.UniqueConstraint("category_id", "version", name="uq_rule_versions_category_version"),
        sa.UniqueConstraint("category_id", "rule_hash", name="uq_rule_versions_category_hash"),
        sa.UniqueConstraint("id", "category_id", name="uq_rule_versions_id_category"),
    )

    op.create_table(
        "config_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", config_version_status, nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("display_currency", sa.String(length=3), nullable=False),
        sa.Column("aggregation_version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("source_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_config_versions_version_positive"),
        sa.CheckConstraint(
            "aggregation_version > 0", name="ck_config_versions_aggregation_version_positive"
        ),
        sa.CheckConstraint(
            "length(config_hash) = 64",
            name="ck_config_versions_config_hash_sha256",
        ),
        sa.CheckConstraint(
            "status != 'active' OR activated_at IS NOT NULL",
            name="ck_config_versions_active_has_timestamp",
        ),
        sa.CheckConstraint(
            "status != 'superseded' OR superseded_at IS NOT NULL",
            name="ck_config_versions_superseded_has_timestamp",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_config_versions"),
        sa.UniqueConstraint("version", name="uq_config_versions_version"),
    )
    op.create_index(
        "ix_config_versions_config_hash", "config_versions", ["config_hash"], unique=False
    )
    op.create_index(
        "uq_config_versions_one_active",
        "config_versions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "config_version_rules",
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["config_version_id"],
            ["config_versions.id"],
            name="fk_config_version_rules_config_version_id_config_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id", "category_id"],
            ["rule_versions.id", "rule_versions.category_id"],
            name="fk_config_version_rules_rule_category",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "config_version_id", "category_id", name="pk_config_version_rules"
        ),
        sa.UniqueConstraint(
            "config_version_id",
            "category_id",
            "rule_version_id",
            name="uq_config_version_rules_mapping",
        ),
    )

    op.create_table(
        "refresh_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("mode", refresh_mode, nullable=False),
        sa.Column("state", refresh_run_state, nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("source_from_date", sa.Date(), nullable=True),
        sa.Column("source_to_date", sa.Date(), nullable=True),
        sa.Column(
            "expected_accounts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("account_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expected_batch_count", sa.Integer(), nullable=True),
        sa.Column("expected_source_count", sa.Integer(), nullable=True),
        sa.Column("expected_store_count", sa.Integer(), nullable=True),
        sa.Column("expected_skip_count", sa.Integer(), nullable=True),
        sa.Column("source_complete", sa.Boolean(), nullable=False),
        sa.Column("input_checksum", sa.String(length=64), nullable=True),
        sa.Column("computed_checksum", sa.String(length=64), nullable=True),
        sa.Column("cursor_before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cursor_after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("received_batch_count", sa.Integer(), nullable=False),
        sa.Column("actual_source_count", sa.Integer(), nullable=False),
        sa.Column("actual_store_count", sa.Integer(), nullable=False),
        sa.Column("actual_skip_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "source_from_date IS NULL OR source_to_date IS NULL "
            "OR source_from_date <= source_to_date",
            name="ck_refresh_runs_source_date_range",
        ),
        sa.CheckConstraint(
            "expected_batch_count IS NULL OR expected_batch_count >= 0",
            name="ck_refresh_runs_expected_batch_count_nonnegative",
        ),
        sa.CheckConstraint(
            "expected_source_count IS NULL OR expected_source_count >= 0",
            name="ck_refresh_runs_expected_source_count_nonnegative",
        ),
        sa.CheckConstraint(
            "expected_store_count IS NULL OR expected_store_count >= 0",
            name="ck_refresh_runs_expected_store_count_nonnegative",
        ),
        sa.CheckConstraint(
            "expected_skip_count IS NULL OR expected_skip_count >= 0",
            name="ck_refresh_runs_expected_skip_count_nonnegative",
        ),
        sa.CheckConstraint(
            "received_batch_count >= 0", name="ck_refresh_runs_received_batch_count_nonnegative"
        ),
        sa.CheckConstraint(
            "actual_source_count >= 0", name="ck_refresh_runs_actual_source_count_nonnegative"
        ),
        sa.CheckConstraint(
            "actual_store_count >= 0", name="ck_refresh_runs_actual_store_count_nonnegative"
        ),
        sa.CheckConstraint(
            "actual_skip_count >= 0", name="ck_refresh_runs_actual_skip_count_nonnegative"
        ),
        sa.CheckConstraint(
            "actual_store_count + actual_skip_count = actual_source_count",
            name="ck_refresh_runs_actual_counts_match",
        ),
        sa.CheckConstraint(
            "expected_source_count IS NULL OR expected_store_count IS NULL "
            "OR expected_skip_count IS NULL "
            "OR expected_store_count + expected_skip_count = expected_source_count",
            name="ck_refresh_runs_expected_counts_match",
        ),
        sa.CheckConstraint(
            "input_checksum IS NULL OR length(input_checksum) = 64",
            name="ck_refresh_runs_input_checksum_sha256",
        ),
        sa.CheckConstraint(
            "computed_checksum IS NULL OR length(computed_checksum) = 64",
            name="ck_refresh_runs_computed_checksum_sha256",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_refresh_runs_request_hash_sha256"
        ),
        sa.CheckConstraint(
            "state != 'failed' OR failed_at IS NOT NULL",
            name="ck_refresh_runs_failed_has_timestamp",
        ),
        sa.CheckConstraint(
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
            name="ck_refresh_runs_committed_manifest_complete",
        ),
        sa.ForeignKeyConstraint(
            ["config_version_id"],
            ["config_versions.id"],
            name="fk_refresh_runs_config_version_id_config_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_runs"),
        sa.UniqueConstraint("idempotency_key", name="uq_refresh_runs_idempotency_key"),
        sa.UniqueConstraint("id", "scope_key", name="uq_refresh_runs_id_scope"),
        sa.UniqueConstraint(
            "id", "scope_key", "config_version_id", name="uq_refresh_runs_id_scope_config"
        ),
    )
    op.create_index(
        "ix_refresh_runs_scope_created", "refresh_runs", ["scope_key", "created_at"], unique=False
    )
    op.create_index("ix_refresh_runs_state", "refresh_runs", ["state"], unique=False)

    op.create_table(
        "refresh_batches",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("store_count", sa.Integer(), nullable=False),
        sa.Column("skip_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("batch_index >= 0", name="ck_refresh_batches_batch_index_nonnegative"),
        sa.CheckConstraint("item_count >= 0", name="ck_refresh_batches_item_count_nonnegative"),
        sa.CheckConstraint("store_count >= 0", name="ck_refresh_batches_store_count_nonnegative"),
        sa.CheckConstraint("skip_count >= 0", name="ck_refresh_batches_skip_count_nonnegative"),
        sa.CheckConstraint(
            "store_count + skip_count = item_count", name="ck_refresh_batches_item_count_matches"
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_refresh_batches_request_hash_sha256"
        ),
        sa.CheckConstraint("length(checksum) = 64", name="ck_refresh_batches_checksum_sha256"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["refresh_runs.id"],
            name="fk_refresh_batches_run_id_refresh_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "batch_index", name="pk_refresh_batches"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_refresh_batches_run_idempotency"
        ),
    )

    op.create_table(
        "staged_transactions",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("decision", staged_decision, nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("status", transaction_status, nullable=True),
        sa.Column("merchant", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("refunded", sa.Boolean(), nullable=False),
        sa.Column("refund_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("supersedes_source_transaction_id", sa.String(length=255), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "batch_index >= 0", name="ck_staged_transactions_batch_index_nonnegative"
        ),
        sa.CheckConstraint(
            "amount IS NULL OR amount >= 0", name="ck_staged_transactions_amount_nonnegative"
        ),
        sa.CheckConstraint(
            "refund_amount >= 0", name="ck_staged_transactions_refund_amount_nonnegative"
        ),
        sa.CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND amount IS NOT NULL AND refund_amount > 0 AND refund_amount <= amount)",
            name="ck_staged_transactions_refund_consistent",
        ),
        sa.CheckConstraint(
            "decision != 'store' OR "
            "(transaction_date IS NOT NULL AND amount IS NOT NULL AND currency IS NOT NULL "
            "AND status IS NOT NULL)",
            name="ck_staged_transactions_stored_fields_present",
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64", name="ck_staged_transactions_source_hash_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "batch_index"],
            ["refresh_batches.run_id", "refresh_batches.batch_index"],
            name="fk_staged_transactions_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "scope_key", "config_version_id"],
            ["refresh_runs.id", "refresh_runs.scope_key", "refresh_runs.config_version_id"],
            name="fk_staged_transactions_run_scope_config",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "scope_key",
            "account_id",
            "source_transaction_id",
            name="pk_staged_transactions",
        ),
        sa.UniqueConstraint(
            "run_id",
            "scope_key",
            "account_id",
            "source_transaction_id",
            "config_version_id",
            name="uq_staged_transactions_identity_config",
        ),
    )
    op.create_index(
        "ix_staged_transactions_run_batch",
        "staged_transactions",
        ["run_id", "batch_index"],
        unique=False,
    )

    op.create_table(
        "staged_transaction_categories",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["config_version_id", "category_id", "rule_version_id"],
            [
                "config_version_rules.config_version_id",
                "config_version_rules.category_id",
                "config_version_rules.rule_version_id",
            ],
            name="fk_staged_transaction_categories_config_rule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint(
            "run_id",
            "scope_key",
            "account_id",
            "source_transaction_id",
            "category_id",
            name="pk_staged_transaction_categories",
        ),
    )

    op.create_table(
        "transactions",
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", transaction_status, nullable=False),
        sa.Column("merchant", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("refunded", sa.Boolean(), nullable=False),
        sa.Column("refund_amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("supersedes_source_transaction_id", sa.String(length=255), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_refresh_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_refresh_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount >= 0", name="ck_transactions_amount_nonnegative"),
        sa.CheckConstraint(
            "refund_amount >= 0", name="ck_transactions_refund_amount_nonnegative"
        ),
        sa.CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND refund_amount > 0 AND refund_amount <= amount)",
            name="ck_transactions_refund_consistent",
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64", name="ck_transactions_source_hash_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["first_refresh_run_id", "scope_key"],
            ["refresh_runs.id", "refresh_runs.scope_key"],
            name="fk_transactions_first_run_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_refresh_run_id", "scope_key", "config_version_id"],
            ["refresh_runs.id", "refresh_runs.scope_key", "refresh_runs.config_version_id"],
            name="fk_transactions_last_run_scope_config",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "scope_key", "account_id", "source_transaction_id", name="pk_transactions"
        ),
        sa.UniqueConstraint(
            "scope_key",
            "account_id",
            "source_transaction_id",
            "config_version_id",
            name="uq_transactions_identity_config",
        ),
    )
    op.create_index(
        "ix_transactions_scope_date",
        "transactions",
        ["scope_key", "transaction_date"],
        unique=False,
    )
    op.create_index(
        "ix_transactions_scope_status_date",
        "transactions",
        ["scope_key", "status", "transaction_date"],
        unique=False,
    )

    op.create_table(
        "transaction_categories",
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["config_version_id", "category_id", "rule_version_id"],
            [
                "config_version_rules.config_version_id",
                "config_version_rules.category_id",
                "config_version_rules.rule_version_id",
            ],
            name="fk_transaction_categories_config_rule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_key", "account_id", "source_transaction_id", "config_version_id"],
            [
                "transactions.scope_key",
                "transactions.account_id",
                "transactions.source_transaction_id",
                "transactions.config_version_id",
            ],
            name="fk_transaction_categories_transaction",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "scope_key",
            "account_id",
            "source_transaction_id",
            "category_id",
            name="pk_transaction_categories",
        ),
    )
    op.create_index(
        "ix_transaction_categories_category",
        "transaction_categories",
        ["category_id"],
        unique=False,
    )

    op.create_table(
        "sync_states",
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cursor_hash", sa.String(length=64), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_refresh_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 0", name="ck_sync_states_revision_nonnegative"),
        sa.CheckConstraint(
            "length(cursor_hash) = 64", name="ck_sync_states_cursor_hash_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["last_refresh_run_id", "scope_key", "config_version_id"],
            ["refresh_runs.id", "refresh_runs.scope_key", "refresh_runs.config_version_id"],
            name="fk_sync_states_last_run_scope_config",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("scope_key", name="pk_sync_states"),
        sa.UniqueConstraint("last_refresh_run_id", name="uq_sync_states_last_refresh_run_id"),
    )

    # Rule bodies and config snapshots are append-only. Config status is the only
    # mutable part, and can move only toward active/superseded.
    op.execute(
        """
        CREATE FUNCTION reject_rule_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'rule_versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_rule_versions_immutable
        BEFORE UPDATE OR DELETE ON rule_versions
        FOR EACH ROW EXECUTE FUNCTION reject_rule_version_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_config_rule_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'config_version_rules are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_config_version_rules_immutable
        BEFORE UPDATE OR DELETE ON config_version_rules
        FOR EACH ROW EXECUTE FUNCTION reject_config_rule_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_config_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'config_versions cannot be deleted';
            END IF;

            IF NEW.version IS DISTINCT FROM OLD.version
               OR NEW.timezone IS DISTINCT FROM OLD.timezone
               OR NEW.display_currency IS DISTINCT FROM OLD.display_currency
               OR NEW.aggregation_version IS DISTINCT FROM OLD.aggregation_version
               OR NEW.config_hash IS DISTINCT FROM OLD.config_hash
               OR NEW.source_config IS DISTINCT FROM OLD.source_config
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'config version content is immutable';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'pending' AND NEW.status IN ('active', 'superseded'))
                OR (OLD.status = 'active' AND NEW.status = 'superseded')
            ) THEN
                RAISE EXCEPTION 'invalid config version status transition: % -> %',
                    OLD.status, NEW.status;
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_config_versions_guard
        BEFORE UPDATE OR DELETE ON config_versions
        FOR EACH ROW EXECUTE FUNCTION guard_config_version_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION require_full_rebuild_for_activation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'active' AND OLD.status IS DISTINCT FROM 'active'
               AND NOT EXISTS (
                   SELECT 1 FROM refresh_runs
                   WHERE config_version_id = NEW.id
                     AND mode = 'full'
                     AND state = 'committed'
               ) THEN
                RAISE EXCEPTION 'config activation requires a committed full rebuild';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_config_activation_requires_rebuild
        AFTER UPDATE OF status ON config_versions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION require_full_rebuild_for_activation()
        """
    )

    # The cursor can advance only to a committed run from the exact same
    # scope/config. The composite FK checks identity; this deferred trigger checks
    # terminal state after all writes in the atomic completion transaction.
    op.execute(
        """
        CREATE FUNCTION require_committed_sync_run()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_state refresh_run_state;
        BEGIN
            SELECT state INTO run_state
            FROM refresh_runs
            WHERE id = NEW.last_refresh_run_id
              AND scope_key = NEW.scope_key
              AND config_version_id = NEW.config_version_id;

            IF run_state IS DISTINCT FROM 'committed' THEN
                RAISE EXCEPTION 'sync cursor must reference a committed refresh run';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_sync_state_committed_run
        AFTER INSERT OR UPDATE ON sync_states
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION require_committed_sync_run()
        """
    )

    # Run identity and request manifest are immutable. Mutable counters/checksums
    # may advance while the state machine moves forward one step at a time.
    op.execute(
        """
        CREATE FUNCTION guard_refresh_run_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'refresh_runs cannot be deleted';
            END IF;

            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
               OR NEW.mode IS DISTINCT FROM OLD.mode
               OR NEW.config_version_id IS DISTINCT FROM OLD.config_version_id
               OR NEW.scope_key IS DISTINCT FROM OLD.scope_key
               OR NEW.source_from_date IS DISTINCT FROM OLD.source_from_date
               OR NEW.source_to_date IS DISTINCT FROM OLD.source_to_date
               OR NEW.expected_accounts IS DISTINCT FROM OLD.expected_accounts
               OR NEW.cursor_before IS DISTINCT FROM OLD.cursor_before
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'refresh run identity/manifest is immutable';
            END IF;

            IF OLD.state IN ('committed', 'failed') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'terminal refresh runs are immutable';
            END IF;

            IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
                (OLD.state = 'created' AND NEW.state IN ('uploaded', 'failed'))
                OR (
                    OLD.state = 'created'
                    AND NEW.state = 'validated'
                    AND NEW.expected_batch_count = 0
                    AND NEW.expected_source_count = 0
                    AND NEW.expected_store_count = 0
                    AND NEW.expected_skip_count = 0
                    AND NEW.received_batch_count = 0
                    AND NEW.actual_source_count = 0
                    AND NEW.actual_store_count = 0
                    AND NEW.actual_skip_count = 0
                )
                OR (OLD.state = 'uploaded' AND NEW.state IN ('validated', 'failed'))
                OR (OLD.state = 'validated' AND NEW.state IN ('committed', 'failed'))
            ) THEN
                RAISE EXCEPTION 'invalid refresh run state transition: % -> %',
                    OLD.state, NEW.state;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_refresh_runs_guard
        BEFORE UPDATE OR DELETE ON refresh_runs
        FOR EACH ROW EXECUTE FUNCTION guard_refresh_run_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION require_active_config_for_incremental()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE config_status config_version_status;
        BEGIN
            SELECT status INTO config_status
            FROM config_versions WHERE id = NEW.config_version_id;
            IF NEW.mode = 'incremental' AND config_status IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION 'incremental refresh requires the active config version';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_refresh_runs_active_incremental
        BEFORE INSERT ON refresh_runs
        FOR EACH ROW EXECUTE FUNCTION require_active_config_for_incremental()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_refresh_batch_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'refresh_batches are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_refresh_batches_immutable
        BEFORE UPDATE OR DELETE ON refresh_batches
        FOR EACH ROW EXECUTE FUNCTION reject_refresh_batch_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION require_open_run_for_batch()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_state refresh_run_state;
        BEGIN
            SELECT state INTO run_state FROM refresh_runs WHERE id = NEW.run_id;
            IF run_state NOT IN ('created', 'uploaded') THEN
                RAISE EXCEPTION 'refresh batch requires an open refresh run';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_refresh_batches_open_run
        BEFORE INSERT ON refresh_batches
        FOR EACH ROW EXECUTE FUNCTION require_open_run_for_batch()
        """
    )

    # A SKIP is retained only long enough to prove refresh completeness; it can
    # never acquire a category and the live schema has no SKIP representation.
    op.execute(
        """
        CREATE FUNCTION require_staged_store_for_category()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_decision staged_decision;
        BEGIN
            SELECT decision INTO parent_decision
            FROM staged_transactions
            WHERE run_id = NEW.run_id
              AND scope_key = NEW.scope_key
              AND account_id = NEW.account_id
              AND source_transaction_id = NEW.source_transaction_id;

            IF parent_decision IS DISTINCT FROM 'store' THEN
                RAISE EXCEPTION 'only STORE staging rows may have categories';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_staged_categories_store_only
        BEFORE INSERT OR UPDATE ON staged_transaction_categories
        FOR EACH ROW EXECUTE FUNCTION require_staged_store_for_category()
        """
    )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        downgrade_sqlite()
        return
    if dialect != "postgresql":
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    _downgrade_postgresql()


def _downgrade_postgresql() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_staged_categories_store_only "
        "ON staged_transaction_categories"
    )
    op.execute("DROP FUNCTION IF EXISTS require_staged_store_for_category()")
    op.execute("DROP TRIGGER IF EXISTS trg_refresh_batches_open_run ON refresh_batches")
    op.execute("DROP FUNCTION IF EXISTS require_open_run_for_batch()")
    op.execute("DROP TRIGGER IF EXISTS trg_refresh_batches_immutable ON refresh_batches")
    op.execute("DROP FUNCTION IF EXISTS reject_refresh_batch_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_refresh_runs_active_incremental ON refresh_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS require_active_config_for_incremental()")
    op.execute("DROP TRIGGER IF EXISTS trg_refresh_runs_guard ON refresh_runs")
    op.execute("DROP FUNCTION IF EXISTS guard_refresh_run_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_state_committed_run ON sync_states")
    op.execute("DROP FUNCTION IF EXISTS require_committed_sync_run()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_config_activation_requires_rebuild ON config_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS require_full_rebuild_for_activation()")
    op.execute("DROP TRIGGER IF EXISTS trg_config_versions_guard ON config_versions")
    op.execute("DROP FUNCTION IF EXISTS guard_config_version_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_config_version_rules_immutable ON config_version_rules"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_config_rule_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_rule_versions_immutable ON rule_versions")
    op.execute("DROP FUNCTION IF EXISTS reject_rule_version_mutation()")

    op.drop_table("sync_states")
    op.drop_index("ix_transaction_categories_category", table_name="transaction_categories")
    op.drop_table("transaction_categories")
    op.drop_index("ix_transactions_scope_status_date", table_name="transactions")
    op.drop_index("ix_transactions_scope_date", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("staged_transaction_categories")
    op.drop_index("ix_staged_transactions_run_batch", table_name="staged_transactions")
    op.drop_table("staged_transactions")
    op.drop_table("refresh_batches")
    op.drop_index("ix_refresh_runs_state", table_name="refresh_runs")
    op.drop_index("ix_refresh_runs_scope_created", table_name="refresh_runs")
    op.drop_table("refresh_runs")
    op.drop_table("config_version_rules")
    op.drop_index("uq_config_versions_one_active", table_name="config_versions")
    op.drop_index("ix_config_versions_config_hash", table_name="config_versions")
    op.drop_table("config_versions")
    op.drop_table("rule_versions")
    op.drop_table("categories")

    bind = op.get_bind()
    postgresql.ENUM(name="transaction_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="staged_decision").drop(bind, checkfirst=True)
    postgresql.ENUM(name="refresh_run_state").drop(bind, checkfirst=True)
    postgresql.ENUM(name="refresh_mode").drop(bind, checkfirst=True)
    postgresql.ENUM(name="config_version_status").drop(bind, checkfirst=True)
