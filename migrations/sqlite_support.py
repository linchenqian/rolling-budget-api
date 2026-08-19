"""SQLite DDL for the initial schema.

This module is intentionally static. Importing application ORM metadata from an
old migration would make a fresh database change whenever future models change.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

UUID = sa.Uuid
JSON = sa.JSON


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=None if nullable else sa.text("CURRENT_TIMESTAMP"),
        nullable=nullable,
    )


def upgrade_sqlite() -> None:
    op.create_table(
        "categories",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("icon", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("budget_limit", sa.BigInteger(), nullable=False),
        sa.Column("budget_currency", sa.String(length=3), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "budget_limit >= 0",
            name="ck_categories_budget_limit_nonnegative",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_categories_sort_order_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("key", name="uq_categories_key"),
    )

    op.create_table(
        "rule_versions",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("category_id", UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column("classification_instruction", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("rule_hash", sa.String(length=64), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "version > 0",
            name="ck_rule_versions_version_positive",
        ),
        sa.CheckConstraint(
            "lookback_days > 0 AND lookback_days <= 3660",
            name="ck_rule_versions_lookback_days_range",
        ),
        sa.CheckConstraint(
            "length(rule_hash) = 64",
            name="ck_rule_versions_rule_hash_sha256",
        ),
        sa.CheckConstraint(
            "is_enabled IN (0, 1)",
            name="ck_rule_versions_is_enabled_boolean",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_rule_versions_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rule_versions"),
        sa.UniqueConstraint(
            "category_id",
            "version",
            name="uq_rule_versions_category_version",
        ),
        sa.UniqueConstraint(
            "category_id",
            "rule_hash",
            name="uq_rule_versions_category_hash",
        ),
        sa.UniqueConstraint(
            "id",
            "category_id",
            name="uq_rule_versions_id_category",
        ),
    )

    op.create_table(
        "config_versions",
        sa.Column("id", UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("display_currency", sa.String(length=3), nullable=False),
        sa.Column("aggregation_version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("source_config", JSON(), nullable=False),
        _timestamp("created_at"),
        _timestamp("activated_at", nullable=True),
        _timestamp("superseded_at", nullable=True),
        sa.CheckConstraint(
            "version > 0",
            name="ck_config_versions_version_positive",
        ),
        sa.CheckConstraint(
            "aggregation_version > 0",
            name="ck_config_versions_aggregation_version_positive",
        ),
        sa.CheckConstraint(
            "length(config_hash) = 64",
            name="ck_config_versions_config_hash_sha256",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'superseded')",
            name="ck_config_versions_status_enum",
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
        "ix_config_versions_config_hash",
        "config_versions",
        ["config_hash"],
        unique=False,
    )
    op.create_index(
        "uq_config_versions_one_active",
        "config_versions",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "config_version_rules",
        sa.Column("config_version_id", UUID(), nullable=False),
        sa.Column("category_id", UUID(), nullable=False),
        sa.Column("rule_version_id", UUID(), nullable=False),
        _timestamp("created_at"),
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
            "config_version_id",
            "category_id",
            name="pk_config_version_rules",
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
        sa.Column("id", UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=11), nullable=False),
        sa.Column("state", sa.String(length=9), nullable=False),
        sa.Column("config_version_id", UUID(), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("source_from_date", sa.Date(), nullable=True),
        sa.Column("source_to_date", sa.Date(), nullable=True),
        sa.Column(
            "expected_accounts",
            JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("account_manifest", JSON(), nullable=True),
        sa.Column("expected_batch_count", sa.Integer(), nullable=True),
        sa.Column("expected_source_count", sa.Integer(), nullable=True),
        sa.Column("expected_store_count", sa.Integer(), nullable=True),
        sa.Column("expected_skip_count", sa.Integer(), nullable=True),
        sa.Column("source_complete", sa.Boolean(), nullable=False),
        sa.Column("input_checksum", sa.String(length=64), nullable=True),
        sa.Column("computed_checksum", sa.String(length=64), nullable=True),
        sa.Column("cursor_before", JSON(), nullable=True),
        sa.Column("cursor_after", JSON(), nullable=True),
        sa.Column("received_batch_count", sa.Integer(), nullable=False),
        sa.Column("actual_source_count", sa.Integer(), nullable=False),
        sa.Column("actual_store_count", sa.Integer(), nullable=False),
        sa.Column("actual_skip_count", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("uploaded_at", nullable=True),
        _timestamp("validated_at", nullable=True),
        _timestamp("committed_at", nullable=True),
        _timestamp("failed_at", nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "mode IN ('incremental', 'full')",
            name="ck_refresh_runs_mode_enum",
        ),
        sa.CheckConstraint(
            "state IN ('created', 'uploaded', 'validated', 'committed', 'failed')",
            name="ck_refresh_runs_state_enum",
        ),
        sa.CheckConstraint(
            "source_complete IN (0, 1)",
            name="ck_refresh_runs_source_complete_boolean",
        ),
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
            "received_batch_count >= 0",
            name="ck_refresh_runs_received_batch_count_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_source_count >= 0",
            name="ck_refresh_runs_actual_source_count_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_store_count >= 0",
            name="ck_refresh_runs_actual_store_count_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_skip_count >= 0",
            name="ck_refresh_runs_actual_skip_count_nonnegative",
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
            "length(request_hash) = 64",
            name="ck_refresh_runs_request_hash_sha256",
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
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_refresh_runs_idempotency_key",
        ),
        sa.UniqueConstraint(
            "id",
            "scope_key",
            name="uq_refresh_runs_id_scope",
        ),
        sa.UniqueConstraint(
            "id",
            "scope_key",
            "config_version_id",
            name="uq_refresh_runs_id_scope_config",
        ),
    )
    op.create_index(
        "ix_refresh_runs_scope_created",
        "refresh_runs",
        ["scope_key", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_refresh_runs_state",
        "refresh_runs",
        ["state"],
        unique=False,
    )

    _create_remaining_tables()
    _create_triggers()


def _create_remaining_tables() -> None:
    op.create_table(
        "refresh_batches",
        sa.Column("run_id", UUID(), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("store_count", sa.Integer(), nullable=False),
        sa.Column("skip_count", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "batch_index >= 0",
            name="ck_refresh_batches_batch_index_nonnegative",
        ),
        sa.CheckConstraint(
            "item_count >= 0",
            name="ck_refresh_batches_item_count_nonnegative",
        ),
        sa.CheckConstraint(
            "store_count >= 0",
            name="ck_refresh_batches_store_count_nonnegative",
        ),
        sa.CheckConstraint(
            "skip_count >= 0",
            name="ck_refresh_batches_skip_count_nonnegative",
        ),
        sa.CheckConstraint(
            "store_count + skip_count = item_count",
            name="ck_refresh_batches_item_count_matches",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_refresh_batches_request_hash_sha256",
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name="ck_refresh_batches_checksum_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["refresh_runs.id"],
            name="fk_refresh_batches_run_id_refresh_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "batch_index",
            name="pk_refresh_batches",
        ),
        sa.UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_refresh_batches_run_idempotency",
        ),
    )

    op.create_table(
        "staged_transactions",
        sa.Column("run_id", UUID(), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("config_version_id", UUID(), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=5), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("status", sa.String(length=7), nullable=True),
        sa.Column("merchant", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("refunded", sa.Boolean(), nullable=False),
        sa.Column("refund_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "supersedes_source_transaction_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "decision IN ('store', 'skip')",
            name="ck_staged_transactions_decision_enum",
        ),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('pending', 'posted')",
            name="ck_staged_transactions_status_enum",
        ),
        sa.CheckConstraint(
            "refunded IN (0, 1)",
            name="ck_staged_transactions_refunded_boolean",
        ),
        sa.CheckConstraint(
            "batch_index >= 0",
            name="ck_staged_transactions_batch_index_nonnegative",
        ),
        sa.CheckConstraint(
            "amount IS NULL OR amount >= 0",
            name="ck_staged_transactions_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "refund_amount >= 0",
            name="ck_staged_transactions_refund_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND amount IS NOT NULL AND refund_amount > 0 "
            "AND refund_amount <= amount)",
            name="ck_staged_transactions_refund_consistent",
        ),
        sa.CheckConstraint(
            "decision != 'store' OR "
            "(transaction_date IS NOT NULL AND amount IS NOT NULL "
            "AND currency IS NOT NULL AND status IS NOT NULL)",
            name="ck_staged_transactions_stored_fields_present",
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name="ck_staged_transactions_source_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "batch_index"],
            ["refresh_batches.run_id", "refresh_batches.batch_index"],
            name="fk_staged_transactions_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "scope_key", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.scope_key",
                "refresh_runs.config_version_id",
            ],
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
        sa.Column("run_id", UUID(), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("config_version_id", UUID(), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("category_id", UUID(), nullable=False),
        sa.Column("rule_version_id", UUID(), nullable=False),
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

    _create_live_tables()


def _create_live_tables() -> None:
    op.create_table(
        "transactions",
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=7), nullable=False),
        sa.Column("merchant", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("refunded", sa.Boolean(), nullable=False),
        sa.Column("refund_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "supersedes_source_transaction_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("config_version_id", UUID(), nullable=False),
        sa.Column("first_refresh_run_id", UUID(), nullable=False),
        sa.Column("last_refresh_run_id", UUID(), nullable=False),
        _timestamp("first_seen_at"),
        _timestamp("last_seen_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'posted')",
            name="ck_transactions_status_enum",
        ),
        sa.CheckConstraint(
            "refunded IN (0, 1)",
            name="ck_transactions_refunded_boolean",
        ),
        sa.CheckConstraint(
            "amount >= 0",
            name="ck_transactions_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "refund_amount >= 0",
            name="ck_transactions_refund_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "(NOT refunded AND refund_amount = 0) OR "
            "(refunded AND refund_amount > 0 AND refund_amount <= amount)",
            name="ck_transactions_refund_consistent",
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name="ck_transactions_source_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["first_refresh_run_id", "scope_key"],
            ["refresh_runs.id", "refresh_runs.scope_key"],
            name="fk_transactions_first_run_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_refresh_run_id", "scope_key", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.scope_key",
                "refresh_runs.config_version_id",
            ],
            name="fk_transactions_last_run_scope_config",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "scope_key",
            "account_id",
            "source_transaction_id",
            name="pk_transactions",
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
        sa.Column("category_id", UUID(), nullable=False),
        sa.Column("config_version_id", UUID(), nullable=False),
        sa.Column("rule_version_id", UUID(), nullable=False),
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
        sa.Column("cursor", JSON(), nullable=False),
        sa.Column("cursor_hash", sa.String(length=64), nullable=False),
        sa.Column("config_version_id", UUID(), nullable=False),
        sa.Column("last_refresh_run_id", UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_sync_states_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "length(cursor_hash) = 64",
            name="ck_sync_states_cursor_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["last_refresh_run_id", "scope_key", "config_version_id"],
            [
                "refresh_runs.id",
                "refresh_runs.scope_key",
                "refresh_runs.config_version_id",
            ],
            name="fk_sync_states_last_run_scope_config",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("scope_key", name="pk_sync_states"),
        sa.UniqueConstraint(
            "last_refresh_run_id",
            name="uq_sync_states_last_refresh_run_id",
        ),
    )


def _create_triggers() -> None:
    trigger_statements = (
        """
        CREATE TRIGGER trg_rule_versions_immutable_update
        BEFORE UPDATE ON rule_versions
        BEGIN
            SELECT RAISE(ABORT, 'rule_versions are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_rule_versions_immutable_delete
        BEFORE DELETE ON rule_versions
        BEGIN
            SELECT RAISE(ABORT, 'rule_versions are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_config_version_rules_immutable_update
        BEFORE UPDATE ON config_version_rules
        BEGIN
            SELECT RAISE(ABORT, 'config_version_rules are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_config_version_rules_immutable_delete
        BEFORE DELETE ON config_version_rules
        BEGIN
            SELECT RAISE(ABORT, 'config_version_rules are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_config_versions_no_delete
        BEFORE DELETE ON config_versions
        BEGIN
            SELECT RAISE(ABORT, 'config_versions cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER trg_config_versions_content_immutable
        BEFORE UPDATE ON config_versions
        WHEN NEW.version IS NOT OLD.version
          OR NEW.timezone IS NOT OLD.timezone
          OR NEW.display_currency IS NOT OLD.display_currency
          OR NEW.aggregation_version IS NOT OLD.aggregation_version
          OR NEW.config_hash IS NOT OLD.config_hash
          OR NEW.source_config IS NOT OLD.source_config
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'config version content is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_config_versions_status_guard
        BEFORE UPDATE OF status ON config_versions
        WHEN NEW.status IS NOT OLD.status
         AND NOT (
             (OLD.status = 'pending' AND NEW.status IN ('active', 'superseded'))
             OR (OLD.status = 'active' AND NEW.status = 'superseded')
         )
        BEGIN
            SELECT RAISE(ABORT, 'invalid config version status transition');
        END
        """,
        """
        CREATE TRIGGER trg_config_activation_requires_rebuild
        AFTER UPDATE OF status ON config_versions
        WHEN NEW.status = 'active'
         AND OLD.status IS NOT 'active'
         AND NOT EXISTS (
             SELECT 1
             FROM refresh_runs
             WHERE config_version_id = NEW.id
               AND mode = 'full'
               AND state = 'committed'
         )
        BEGIN
            SELECT RAISE(
                ABORT,
                'config activation requires a committed full rebuild'
            );
        END
        """,
        """
        CREATE TRIGGER trg_sync_state_committed_run_insert
        BEFORE INSERT ON sync_states
        WHEN NOT EXISTS (
            SELECT 1
            FROM refresh_runs
            WHERE id = NEW.last_refresh_run_id
              AND scope_key = NEW.scope_key
              AND config_version_id = NEW.config_version_id
              AND state = 'committed'
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'sync cursor must reference a committed refresh run'
            );
        END
        """,
        """
        CREATE TRIGGER trg_sync_state_committed_run_update
        BEFORE UPDATE ON sync_states
        WHEN NOT EXISTS (
            SELECT 1
            FROM refresh_runs
            WHERE id = NEW.last_refresh_run_id
              AND scope_key = NEW.scope_key
              AND config_version_id = NEW.config_version_id
              AND state = 'committed'
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'sync cursor must reference a committed refresh run'
            );
        END
        """,
        """
        CREATE TRIGGER trg_refresh_runs_no_delete
        BEFORE DELETE ON refresh_runs
        BEGIN
            SELECT RAISE(ABORT, 'refresh_runs cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER trg_refresh_runs_identity_immutable
        BEFORE UPDATE ON refresh_runs
        WHEN NEW.id IS NOT OLD.id
          OR NEW.idempotency_key IS NOT OLD.idempotency_key
          OR NEW.request_hash IS NOT OLD.request_hash
          OR NEW.mode IS NOT OLD.mode
          OR NEW.config_version_id IS NOT OLD.config_version_id
          OR NEW.scope_key IS NOT OLD.scope_key
          OR NEW.source_from_date IS NOT OLD.source_from_date
          OR NEW.source_to_date IS NOT OLD.source_to_date
          OR NEW.expected_accounts IS NOT OLD.expected_accounts
          OR NEW.cursor_before IS NOT OLD.cursor_before
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(
                ABORT,
                'refresh run identity/manifest is immutable'
            );
        END
        """,
        """
        CREATE TRIGGER trg_refresh_runs_terminal_immutable
        BEFORE UPDATE ON refresh_runs
        WHEN OLD.state IN ('committed', 'failed')
        BEGIN
            SELECT RAISE(ABORT, 'terminal refresh runs are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_refresh_runs_state_guard
        BEFORE UPDATE OF state ON refresh_runs
        WHEN NEW.state IS NOT OLD.state
         AND NOT (
             (OLD.state = 'created' AND NEW.state IN ('uploaded', 'failed'))
             OR (
                 OLD.state = 'created'
                 AND NEW.state = 'validated'
                 AND COALESCE(NEW.expected_batch_count, -1) = 0
                 AND COALESCE(NEW.expected_source_count, -1) = 0
                 AND COALESCE(NEW.expected_store_count, -1) = 0
                 AND COALESCE(NEW.expected_skip_count, -1) = 0
                 AND NEW.received_batch_count = 0
                 AND NEW.actual_source_count = 0
                 AND NEW.actual_store_count = 0
                 AND NEW.actual_skip_count = 0
             )
             OR (
                 OLD.state = 'uploaded'
                 AND NEW.state IN ('validated', 'failed')
             )
             OR (
                 OLD.state = 'validated'
                 AND NEW.state IN ('committed', 'failed')
             )
         )
        BEGIN
            SELECT RAISE(ABORT, 'invalid refresh run state transition');
        END
        """,
        """
        CREATE TRIGGER trg_refresh_runs_active_incremental
        BEFORE INSERT ON refresh_runs
        WHEN NEW.mode = 'incremental'
         AND NOT EXISTS (
             SELECT 1
             FROM config_versions
             WHERE id = NEW.config_version_id
               AND status = 'active'
         )
        BEGIN
            SELECT RAISE(
                ABORT,
                'incremental refresh requires the active config version'
            );
        END
        """,
        """
        CREATE TRIGGER trg_refresh_batches_immutable_update
        BEFORE UPDATE ON refresh_batches
        BEGIN
            SELECT RAISE(ABORT, 'refresh_batches are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_refresh_batches_immutable_delete
        BEFORE DELETE ON refresh_batches
        BEGIN
            SELECT RAISE(ABORT, 'refresh_batches are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_refresh_batches_open_run
        BEFORE INSERT ON refresh_batches
        WHEN NOT EXISTS (
            SELECT 1
            FROM refresh_runs
            WHERE id = NEW.run_id
              AND state IN ('created', 'uploaded')
        )
        BEGIN
            SELECT RAISE(ABORT, 'refresh batch requires an open refresh run');
        END
        """,
        """
        CREATE TRIGGER trg_staged_categories_store_only_insert
        BEFORE INSERT ON staged_transaction_categories
        WHEN NOT EXISTS (
            SELECT 1
            FROM staged_transactions
            WHERE run_id = NEW.run_id
              AND scope_key = NEW.scope_key
              AND config_version_id = NEW.config_version_id
              AND account_id = NEW.account_id
              AND source_transaction_id = NEW.source_transaction_id
              AND decision = 'store'
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'only STORE staging rows may have categories'
            );
        END
        """,
        """
        CREATE TRIGGER trg_staged_categories_store_only_update
        BEFORE UPDATE ON staged_transaction_categories
        WHEN NOT EXISTS (
            SELECT 1
            FROM staged_transactions
            WHERE run_id = NEW.run_id
              AND scope_key = NEW.scope_key
              AND config_version_id = NEW.config_version_id
              AND account_id = NEW.account_id
              AND source_transaction_id = NEW.source_transaction_id
              AND decision = 'store'
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'only STORE staging rows may have categories'
            );
        END
        """,
    )
    for statement in trigger_statements:
        op.execute(sa.text(statement))


def downgrade_sqlite() -> None:
    # SQLite drops table-owned triggers and indexes with each table. Drop tables
    # in reverse dependency order while foreign-key enforcement remains enabled.
    op.drop_table("sync_states")
    op.drop_table("transaction_categories")
    op.drop_table("transactions")
    op.drop_table("staged_transaction_categories")
    op.drop_table("staged_transactions")
    op.drop_table("refresh_batches")
    op.drop_table("refresh_runs")
    op.drop_table("config_version_rules")
    op.drop_table("config_versions")
    op.drop_table("rule_versions")
    op.drop_table("categories")
