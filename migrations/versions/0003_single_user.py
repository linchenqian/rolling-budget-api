"""Collapse the legacy named scope into one global user dataset.

Revision ID: 0003_single_user
Revises: 0002_oauth
Create Date: 2026-08-20

The upgrade is intentionally fail-safe: databases containing more than one
legacy scope are rejected before any schema or data mutation.  It also removes
source-specific completeness and cursor metadata that the API cannot verify.
The downgrade restores the structural legacy scope as ``personal`` and uses
safe derived/default values for metadata that was deliberately discarded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_single_user"
down_revision: str | None = "0002_oauth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPED_TABLES = (
    "refresh_runs",
    "staged_transactions",
    "staged_transaction_categories",
    "transactions",
    "transaction_categories",
    "sync_states",
)
_SQLITE_REBUILD_TABLES = (*_SCOPED_TABLES, "refresh_batches")


def _json_value(value: object) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _canonical_hash(value: Mapping[str, Any] | list[Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _iso_date(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, str) and value:
        return value
    raise RuntimeError("Every legacy refresh run must have a complete source date range")


def _legacy_scopes(bind: sa.Connection) -> set[str]:
    scopes: set[str] = set()
    for table_name in _SCOPED_TABLES:
        rows = bind.execute(sa.text(f"SELECT DISTINCT scope_key FROM {table_name}"))
        scopes.update(str(value) for value in rows.scalars() if value is not None)
    source_configs = bind.execute(
        sa.text("SELECT source_config FROM config_versions")
    ).scalars()
    for source_config in source_configs:
        document = _json_value(source_config)
        if isinstance(document, dict):
            scope = document.get("scope_key")
            if scope is not None:
                scopes.add(str(scope))
    return scopes


def _assert_single_legacy_scope(bind: sa.Connection) -> None:
    scopes = _legacy_scopes(bind)
    if len(scopes) > 1:
        raise RuntimeError(
            "Cannot migrate a database containing multiple legacy scopes without data loss"
        )


def _config_rewrites(bind: sa.Connection, *, restore_scope: bool) -> list[dict[str, Any]]:
    rewrites: list[dict[str, Any]] = []
    configs = bind.execute(
        sa.text(
            "SELECT id, timezone, display_currency, aggregation_version, source_config "
            "FROM config_versions"
        )
    ).mappings()
    for config in configs:
        source = _json_value(config["source_config"])
        if not isinstance(source, dict):
            raise RuntimeError("config_versions.source_config must contain a JSON object")
        source = dict(source)
        source.pop("account_ids", None)
        source.pop("scope_key", None)
        if restore_scope:
            source["scope_key"] = "personal"

        rules = [
            {"category_key": row.category_key, "rule_hash": row.rule_hash}
            for row in bind.execute(
                sa.text(
                    "SELECT c.key AS category_key, rv.rule_hash AS rule_hash "
                    "FROM config_version_rules cvr "
                    "JOIN categories c ON c.id = cvr.category_id "
                    "JOIN rule_versions rv ON rv.id = cvr.rule_version_id "
                    "WHERE cvr.config_version_id = :config_id "
                    "ORDER BY c.key"
                ),
                {"config_id": config["id"]},
            )
        ]
        semantic_hash = _canonical_hash(
            {
                "timezone": config["timezone"],
                "display_currency": config["display_currency"],
                "aggregation_version": config["aggregation_version"],
                "rules": rules,
            }
        )
        rewrites.append(
            {
                "id": config["id"],
                "source_config": source,
                "config_hash": semantic_hash,
            }
        )
    return rewrites


def _request_hash_rewrites(
    bind: sa.Connection,
    *,
    restore_scope: bool,
) -> list[dict[str, Any]]:
    rewrites: list[dict[str, Any]] = []
    rows = bind.execute(
        sa.text(
            "SELECT id, mode, source_from_date, source_to_date, expected_accounts "
            "FROM refresh_runs"
        )
    ).mappings()
    for row in rows:
        accounts = _json_value(row["expected_accounts"])
        if not isinstance(accounts, list) or not all(isinstance(item, str) for item in accounts):
            raise RuntimeError("refresh_runs.expected_accounts must contain a JSON string list")
        mode = str(row["mode"])
        if mode not in {"full", "incremental"}:
            raise RuntimeError(f"Unsupported legacy refresh mode: {mode}")
        request: dict[str, Any] = {
            "mode": "FULL_REBUILD" if mode == "full" else "INCREMENTAL",
            "source_from_date": _iso_date(row["source_from_date"]),
            "source_to_date": _iso_date(row["source_to_date"]),
            "expected_accounts": sorted(accounts),
        }
        if restore_scope:
            request["scope_key"] = "personal"
            request["cursor_before"] = {}
        rewrites.append({"id": row["id"], "request_hash": _canonical_hash(request)})
    return rewrites


def _apply_config_rewrites(bind: sa.Connection, rewrites: list[dict[str, Any]]) -> None:
    id_type: sa.types.TypeEngine[Any] = (
        sa.String(32) if bind.dialect.name == "sqlite" else sa.Uuid()
    )
    table = sa.table(
        "config_versions",
        sa.column("id", id_type),
        sa.column("source_config", sa.JSON()),
        sa.column("config_hash", sa.String(64)),
    )
    for rewrite in rewrites:
        bind.execute(
            sa.update(table)
            .where(table.c.id == rewrite["id"])
            .values(
                source_config=rewrite["source_config"],
                config_hash=rewrite["config_hash"],
            )
        )


def _apply_request_hash_rewrites(bind: sa.Connection, rewrites: list[dict[str, Any]]) -> None:
    for rewrite in rewrites:
        bind.execute(
            sa.text("UPDATE refresh_runs SET request_hash = :request_hash WHERE id = :id"),
            rewrite,
        )


_SQLITE_NEW_TABLES = """
CREATE TABLE refresh_runs (
    id CHAR(32) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    mode VARCHAR(11) NOT NULL,
    state VARCHAR(9) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    source_from_date DATE,
    source_to_date DATE,
    expected_accounts JSON NOT NULL DEFAULT '[]',
    completed_accounts JSON,
    expected_batch_count INTEGER,
    computed_checksum VARCHAR(64),
    sync_revision_before INTEGER,
    received_batch_count INTEGER NOT NULL DEFAULT 0,
    actual_item_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    uploaded_at DATETIME,
    validated_at DATETIME,
    committed_at DATETIME,
    failed_at DATETIME,
    error_code VARCHAR(64),
    PRIMARY KEY (id),
    CONSTRAINT uq_refresh_runs_id_config UNIQUE (id, config_version_id),
    UNIQUE (idempotency_key),
    FOREIGN KEY(config_version_id) REFERENCES config_versions (id) ON DELETE RESTRICT,
    CONSTRAINT ck_refresh_runs_mode_enum CHECK (mode IN ('incremental', 'full')),
    CONSTRAINT ck_refresh_runs_state_enum CHECK (
        state IN ('created', 'uploaded', 'validated', 'committed', 'failed')
    ),
    CONSTRAINT source_date_range CHECK (
        source_from_date IS NULL OR source_to_date IS NULL OR source_from_date <= source_to_date
    ),
    CONSTRAINT expected_batch_count_nonnegative CHECK (
        expected_batch_count IS NULL OR expected_batch_count >= 0
    ),
    CONSTRAINT received_batch_count_nonnegative CHECK (received_batch_count >= 0),
    CONSTRAINT actual_item_count_nonnegative CHECK (actual_item_count >= 0),
    CONSTRAINT sync_revision_before_nonnegative CHECK (
        sync_revision_before IS NULL OR sync_revision_before >= 0
    ),
    CONSTRAINT request_hash_sha256 CHECK (length(request_hash) = 64),
    CONSTRAINT computed_checksum_sha256 CHECK (
        computed_checksum IS NULL OR length(computed_checksum) = 64
    ),
    CONSTRAINT failed_has_timestamp CHECK (state != 'failed' OR failed_at IS NOT NULL),
    CONSTRAINT committed_refresh_complete CHECK (
        state != 'committed' OR (
            expected_batch_count IS NOT NULL
            AND computed_checksum IS NOT NULL
            AND expected_batch_count = received_batch_count
            AND completed_accounts IS NOT NULL
            AND (expected_batch_count = 0 OR uploaded_at IS NOT NULL)
            AND validated_at IS NOT NULL
            AND committed_at IS NOT NULL
        )
    )
);
CREATE INDEX ix_refresh_runs_created ON refresh_runs (created_at);
CREATE INDEX ix_refresh_runs_state ON refresh_runs (state);

CREATE TABLE refresh_batches (
    run_id CHAR(32) NOT NULL,
    batch_index INTEGER NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    checksum VARCHAR(64) NOT NULL,
    item_count INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_refresh_batches PRIMARY KEY (run_id, batch_index),
    CONSTRAINT uq_refresh_batches_run_idempotency UNIQUE (run_id, idempotency_key),
    FOREIGN KEY(run_id) REFERENCES refresh_runs (id) ON DELETE CASCADE,
    CONSTRAINT batch_index_nonnegative CHECK (batch_index >= 0),
    CONSTRAINT item_count_nonnegative CHECK (item_count >= 0),
    CONSTRAINT request_hash_sha256 CHECK (length(request_hash) = 64),
    CONSTRAINT checksum_sha256 CHECK (length(checksum) = 64)
);

CREATE TABLE staged_transactions (
    run_id CHAR(32) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    account_name VARCHAR(255),
    source_id VARCHAR(255) NOT NULL,
    batch_index INTEGER NOT NULL,
    transaction_date DATE NOT NULL,
    amount BIGINT NOT NULL,
    currency VARCHAR(3) NOT NULL,
    pending BOOLEAN NOT NULL,
    merchant VARCHAR(500),
    name TEXT,
    refunded BOOLEAN NOT NULL DEFAULT 0,
    refund_amount BIGINT NOT NULL DEFAULT 0,
    pending_source_id VARCHAR(255),
    source_hash VARCHAR(64) NOT NULL,
    CONSTRAINT pk_staged_transactions PRIMARY KEY (
        run_id, account_id, source_id
    ),
    CONSTRAINT uq_staged_transactions_identity_config UNIQUE (
        run_id, account_id, source_id, config_version_id
    ),
    CONSTRAINT fk_staged_transactions_run_config FOREIGN KEY (
        run_id, config_version_id
    ) REFERENCES refresh_runs (id, config_version_id) ON DELETE CASCADE,
    CONSTRAINT fk_staged_transactions_batch FOREIGN KEY (
        run_id, batch_index
    ) REFERENCES refresh_batches (run_id, batch_index) ON DELETE CASCADE,
    CONSTRAINT ck_staged_transactions_pending_boolean CHECK (pending IN (0, 1)),
    CONSTRAINT ck_staged_transactions_refunded_boolean CHECK (
        refunded IN (0, 1)
    ),
    CONSTRAINT batch_index_nonnegative CHECK (batch_index >= 0),
    CONSTRAINT amount_nonnegative CHECK (amount >= 0),
    CONSTRAINT refund_amount_nonnegative CHECK (refund_amount >= 0),
    CONSTRAINT refund_consistent CHECK (
        (NOT refunded AND refund_amount = 0)
        OR (refunded AND refund_amount > 0 AND refund_amount <= amount)
    ),
    CONSTRAINT source_hash_sha256 CHECK (length(source_hash) = 64)
);
CREATE INDEX ix_staged_transactions_run_batch ON staged_transactions (run_id, batch_index);

CREATE TABLE staged_transaction_categories (
    run_id CHAR(32) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    category_id CHAR(32) NOT NULL,
    rule_version_id CHAR(32) NOT NULL,
    CONSTRAINT pk_staged_transaction_categories PRIMARY KEY (
        run_id, account_id, source_id, category_id
    ),
    CONSTRAINT fk_staged_transaction_categories_transaction FOREIGN KEY (
        run_id, account_id, source_id, config_version_id
    ) REFERENCES staged_transactions (
        run_id, account_id, source_id, config_version_id
    ) ON DELETE CASCADE,
    CONSTRAINT fk_staged_transaction_categories_config_rule FOREIGN KEY (
        config_version_id, category_id, rule_version_id
    ) REFERENCES config_version_rules (
        config_version_id, category_id, rule_version_id
    ) ON DELETE RESTRICT
);

CREATE TABLE transactions (
    account_id VARCHAR(255) NOT NULL,
    account_name VARCHAR(255),
    source_id VARCHAR(255) NOT NULL,
    transaction_date DATE NOT NULL,
    amount BIGINT NOT NULL,
    currency VARCHAR(3) NOT NULL,
    pending BOOLEAN NOT NULL,
    merchant VARCHAR(500),
    name TEXT,
    refunded BOOLEAN NOT NULL DEFAULT 0,
    refund_amount BIGINT NOT NULL DEFAULT 0,
    pending_source_id VARCHAR(255),
    source_hash VARCHAR(64) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    first_refresh_run_id CHAR(32) NOT NULL,
    last_refresh_run_id CHAR(32) NOT NULL,
    first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_transactions PRIMARY KEY (account_id, source_id),
    CONSTRAINT uq_transactions_identity_config UNIQUE (
        account_id, source_id, config_version_id
    ),
    CONSTRAINT fk_transactions_first_run FOREIGN KEY (first_refresh_run_id)
        REFERENCES refresh_runs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_last_run_config FOREIGN KEY (
        last_refresh_run_id, config_version_id
    ) REFERENCES refresh_runs (id, config_version_id) ON DELETE RESTRICT,
    CONSTRAINT ck_transactions_pending_boolean CHECK (pending IN (0, 1)),
    CONSTRAINT ck_transactions_refunded_boolean CHECK (refunded IN (0, 1)),
    CONSTRAINT amount_nonnegative CHECK (amount >= 0),
    CONSTRAINT refund_amount_nonnegative CHECK (refund_amount >= 0),
    CONSTRAINT refund_consistent CHECK (
        (NOT refunded AND refund_amount = 0)
        OR (refunded AND refund_amount > 0 AND refund_amount <= amount)
    ),
    CONSTRAINT source_hash_sha256 CHECK (length(source_hash) = 64)
);
CREATE INDEX ix_transactions_date ON transactions (transaction_date);
CREATE INDEX ix_transactions_pending_date ON transactions (pending, transaction_date);

CREATE TABLE transaction_categories (
    account_id VARCHAR(255) NOT NULL,
    source_id VARCHAR(255) NOT NULL,
    category_id CHAR(32) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    rule_version_id CHAR(32) NOT NULL,
    CONSTRAINT pk_transaction_categories PRIMARY KEY (
        account_id, source_id, category_id
    ),
    CONSTRAINT fk_transaction_categories_transaction FOREIGN KEY (
        account_id, source_id, config_version_id
    ) REFERENCES transactions (
        account_id, source_id, config_version_id
    ) ON DELETE CASCADE,
    CONSTRAINT fk_transaction_categories_config_rule FOREIGN KEY (
        config_version_id, category_id, rule_version_id
    ) REFERENCES config_version_rules (
        config_version_id, category_id, rule_version_id
    ) ON DELETE RESTRICT
);
CREATE INDEX ix_transaction_categories_category ON transaction_categories (category_id);

CREATE TABLE sync_states (
    id INTEGER NOT NULL DEFAULT 1,
    config_version_id CHAR(32) NOT NULL,
    last_refresh_run_id CHAR(32) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE (last_refresh_run_id),
    CONSTRAINT fk_sync_states_last_run_config FOREIGN KEY (
        last_refresh_run_id, config_version_id
    ) REFERENCES refresh_runs (id, config_version_id) ON DELETE RESTRICT,
    CONSTRAINT singleton CHECK (id = 1),
    CONSTRAINT revision_nonnegative CHECK (revision >= 0)
);
"""


_SQLITE_OLD_TABLES = """
CREATE TABLE refresh_runs (
    id CHAR(32) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    mode VARCHAR(11) NOT NULL,
    state VARCHAR(9) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    scope_key VARCHAR(128) NOT NULL,
    source_from_date DATE,
    source_to_date DATE,
    expected_accounts JSON NOT NULL DEFAULT '[]',
    account_manifest JSON,
    expected_batch_count INTEGER,
    expected_source_count INTEGER,
    expected_store_count INTEGER,
    expected_skip_count INTEGER,
    source_complete BOOLEAN NOT NULL DEFAULT 0,
    input_checksum VARCHAR(64),
    computed_checksum VARCHAR(64),
    cursor_before JSON,
    cursor_after JSON,
    received_batch_count INTEGER NOT NULL DEFAULT 0,
    actual_source_count INTEGER NOT NULL DEFAULT 0,
    actual_store_count INTEGER NOT NULL DEFAULT 0,
    actual_skip_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    uploaded_at DATETIME,
    validated_at DATETIME,
    committed_at DATETIME,
    failed_at DATETIME,
    error_code VARCHAR(64),
    PRIMARY KEY (id),
    CONSTRAINT uq_refresh_runs_id_scope UNIQUE (id, scope_key),
    CONSTRAINT uq_refresh_runs_id_scope_config UNIQUE (id, scope_key, config_version_id),
    UNIQUE (idempotency_key),
    FOREIGN KEY(config_version_id) REFERENCES config_versions (id) ON DELETE RESTRICT,
    CONSTRAINT ck_refresh_runs_mode_enum CHECK (mode IN ('incremental', 'full')),
    CONSTRAINT ck_refresh_runs_state_enum CHECK (
        state IN ('created', 'uploaded', 'validated', 'committed', 'failed')
    ),
    CONSTRAINT ck_refresh_runs_source_complete_boolean CHECK (
        source_complete IN (0, 1)
    ),
    CONSTRAINT source_date_range CHECK (
        source_from_date IS NULL OR source_to_date IS NULL OR source_from_date <= source_to_date
    ),
    CONSTRAINT expected_batch_count_nonnegative CHECK (
        expected_batch_count IS NULL OR expected_batch_count >= 0
    ),
    CONSTRAINT expected_source_count_nonnegative CHECK (
        expected_source_count IS NULL OR expected_source_count >= 0
    ),
    CONSTRAINT expected_store_count_nonnegative CHECK (
        expected_store_count IS NULL OR expected_store_count >= 0
    ),
    CONSTRAINT expected_skip_count_nonnegative CHECK (
        expected_skip_count IS NULL OR expected_skip_count >= 0
    ),
    CONSTRAINT received_batch_count_nonnegative CHECK (received_batch_count >= 0),
    CONSTRAINT actual_source_count_nonnegative CHECK (actual_source_count >= 0),
    CONSTRAINT actual_store_count_nonnegative CHECK (actual_store_count >= 0),
    CONSTRAINT actual_skip_count_nonnegative CHECK (actual_skip_count >= 0),
    CONSTRAINT actual_counts_match CHECK (
        actual_store_count + actual_skip_count = actual_source_count
    ),
    CONSTRAINT expected_counts_match CHECK (
        expected_source_count IS NULL OR expected_store_count IS NULL
        OR expected_skip_count IS NULL
        OR expected_store_count + expected_skip_count = expected_source_count
    ),
    CONSTRAINT input_checksum_sha256 CHECK (
        input_checksum IS NULL OR length(input_checksum) = 64
    ),
    CONSTRAINT request_hash_sha256 CHECK (length(request_hash) = 64),
    CONSTRAINT computed_checksum_sha256 CHECK (
        computed_checksum IS NULL OR length(computed_checksum) = 64
    ),
    CONSTRAINT failed_has_timestamp CHECK (state != 'failed' OR failed_at IS NOT NULL),
    CONSTRAINT committed_manifest_complete CHECK (
        state != 'committed' OR (
            source_complete
            AND expected_batch_count IS NOT NULL
            AND expected_source_count IS NOT NULL
            AND expected_store_count IS NOT NULL
            AND expected_skip_count IS NOT NULL
            AND input_checksum IS NOT NULL
            AND computed_checksum IS NOT NULL
            AND input_checksum = computed_checksum
            AND expected_batch_count = received_batch_count
            AND expected_source_count = actual_source_count
            AND expected_store_count = actual_store_count
            AND expected_skip_count = actual_skip_count
            AND account_manifest IS NOT NULL
            AND (expected_batch_count = 0 OR uploaded_at IS NOT NULL)
            AND validated_at IS NOT NULL
            AND committed_at IS NOT NULL
        )
    )
);
CREATE INDEX ix_refresh_runs_scope_created ON refresh_runs (scope_key, created_at);
CREATE INDEX ix_refresh_runs_state ON refresh_runs (state);

CREATE TABLE refresh_batches (
    run_id CHAR(32) NOT NULL,
    batch_index INTEGER NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    checksum VARCHAR(64) NOT NULL,
    item_count INTEGER NOT NULL,
    store_count INTEGER NOT NULL,
    skip_count INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_refresh_batches PRIMARY KEY (run_id, batch_index),
    CONSTRAINT uq_refresh_batches_run_idempotency UNIQUE (run_id, idempotency_key),
    FOREIGN KEY(run_id) REFERENCES refresh_runs (id) ON DELETE CASCADE,
    CONSTRAINT batch_index_nonnegative CHECK (batch_index >= 0),
    CONSTRAINT item_count_nonnegative CHECK (item_count >= 0),
    CONSTRAINT store_count_nonnegative CHECK (store_count >= 0),
    CONSTRAINT skip_count_nonnegative CHECK (skip_count >= 0),
    CONSTRAINT item_count_matches CHECK (store_count + skip_count = item_count),
    CONSTRAINT request_hash_sha256 CHECK (length(request_hash) = 64),
    CONSTRAINT checksum_sha256 CHECK (length(checksum) = 64)
);

CREATE TABLE staged_transactions (
    run_id CHAR(32) NOT NULL,
    scope_key VARCHAR(128) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    source_transaction_id VARCHAR(255) NOT NULL,
    batch_index INTEGER NOT NULL,
    decision VARCHAR(5) NOT NULL,
    transaction_date DATE,
    amount NUMERIC(18, 4),
    currency VARCHAR(3),
    status VARCHAR(7),
    merchant VARCHAR(500),
    description TEXT,
    refunded BOOLEAN NOT NULL DEFAULT 0,
    refund_amount NUMERIC(18, 4) NOT NULL DEFAULT 0,
    supersedes_source_transaction_id VARCHAR(255),
    source_hash VARCHAR(64) NOT NULL,
    CONSTRAINT pk_staged_transactions PRIMARY KEY (
        run_id, scope_key, account_id, source_transaction_id
    ),
    CONSTRAINT uq_staged_transactions_identity_config UNIQUE (
        run_id, scope_key, account_id, source_transaction_id, config_version_id
    ),
    CONSTRAINT fk_staged_transactions_run_scope_config FOREIGN KEY (
        run_id, scope_key, config_version_id
    ) REFERENCES refresh_runs (id, scope_key, config_version_id) ON DELETE CASCADE,
    CONSTRAINT fk_staged_transactions_batch FOREIGN KEY (
        run_id, batch_index
    ) REFERENCES refresh_batches (run_id, batch_index) ON DELETE CASCADE,
    CONSTRAINT ck_staged_transactions_decision_enum CHECK (
        decision IN ('store', 'skip')
    ),
    CONSTRAINT ck_staged_transactions_status_enum CHECK (
        status IS NULL OR status IN ('pending', 'posted')
    ),
    CONSTRAINT ck_staged_transactions_refunded_boolean CHECK (
        refunded IN (0, 1)
    ),
    CONSTRAINT batch_index_nonnegative CHECK (batch_index >= 0),
    CONSTRAINT amount_nonnegative CHECK (amount IS NULL OR amount >= 0),
    CONSTRAINT refund_amount_nonnegative CHECK (refund_amount >= 0),
    CONSTRAINT refund_consistent CHECK (
        (NOT refunded AND refund_amount = 0)
        OR (refunded AND amount IS NOT NULL AND refund_amount > 0 AND refund_amount <= amount)
    ),
    CONSTRAINT stored_fields_present CHECK (
        decision != 'store' OR (
            transaction_date IS NOT NULL AND amount IS NOT NULL
            AND currency IS NOT NULL AND status IS NOT NULL
        )
    ),
    CONSTRAINT source_hash_sha256 CHECK (length(source_hash) = 64)
);
CREATE INDEX ix_staged_transactions_run_batch ON staged_transactions (run_id, batch_index);

CREATE TABLE staged_transaction_categories (
    run_id CHAR(32) NOT NULL,
    scope_key VARCHAR(128) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    source_transaction_id VARCHAR(255) NOT NULL,
    category_id CHAR(32) NOT NULL,
    rule_version_id CHAR(32) NOT NULL,
    CONSTRAINT pk_staged_transaction_categories PRIMARY KEY (
        run_id, scope_key, account_id, source_transaction_id, category_id
    ),
    CONSTRAINT fk_staged_transaction_categories_transaction FOREIGN KEY (
        run_id, scope_key, account_id, source_transaction_id, config_version_id
    ) REFERENCES staged_transactions (
        run_id, scope_key, account_id, source_transaction_id, config_version_id
    ) ON DELETE CASCADE,
    CONSTRAINT fk_staged_transaction_categories_config_rule FOREIGN KEY (
        config_version_id, category_id, rule_version_id
    ) REFERENCES config_version_rules (
        config_version_id, category_id, rule_version_id
    ) ON DELETE RESTRICT
);

CREATE TABLE transactions (
    scope_key VARCHAR(128) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    source_transaction_id VARCHAR(255) NOT NULL,
    transaction_date DATE NOT NULL,
    amount NUMERIC(18, 4) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    status VARCHAR(7) NOT NULL,
    merchant VARCHAR(500),
    description TEXT,
    refunded BOOLEAN NOT NULL DEFAULT 0,
    refund_amount NUMERIC(18, 4) NOT NULL DEFAULT 0,
    supersedes_source_transaction_id VARCHAR(255),
    source_hash VARCHAR(64) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    first_refresh_run_id CHAR(32) NOT NULL,
    last_refresh_run_id CHAR(32) NOT NULL,
    first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_transactions PRIMARY KEY (
        scope_key, account_id, source_transaction_id
    ),
    CONSTRAINT uq_transactions_identity_config UNIQUE (
        scope_key, account_id, source_transaction_id, config_version_id
    ),
    CONSTRAINT fk_transactions_first_run_scope FOREIGN KEY (
        first_refresh_run_id, scope_key
    ) REFERENCES refresh_runs (id, scope_key) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_last_run_scope_config FOREIGN KEY (
        last_refresh_run_id, scope_key, config_version_id
    ) REFERENCES refresh_runs (id, scope_key, config_version_id) ON DELETE RESTRICT,
    CONSTRAINT ck_transactions_status_enum CHECK (
        status IN ('pending', 'posted')
    ),
    CONSTRAINT ck_transactions_refunded_boolean CHECK (refunded IN (0, 1)),
    CONSTRAINT amount_nonnegative CHECK (amount >= 0),
    CONSTRAINT refund_amount_nonnegative CHECK (refund_amount >= 0),
    CONSTRAINT refund_consistent CHECK (
        (NOT refunded AND refund_amount = 0)
        OR (refunded AND refund_amount > 0 AND refund_amount <= amount)
    ),
    CONSTRAINT source_hash_sha256 CHECK (length(source_hash) = 64)
);
CREATE INDEX ix_transactions_scope_date ON transactions (scope_key, transaction_date);
CREATE INDEX ix_transactions_scope_status_date ON transactions (
    scope_key, status, transaction_date
);

CREATE TABLE transaction_categories (
    scope_key VARCHAR(128) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    source_transaction_id VARCHAR(255) NOT NULL,
    category_id CHAR(32) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    rule_version_id CHAR(32) NOT NULL,
    CONSTRAINT pk_transaction_categories PRIMARY KEY (
        scope_key, account_id, source_transaction_id, category_id
    ),
    CONSTRAINT fk_transaction_categories_transaction FOREIGN KEY (
        scope_key, account_id, source_transaction_id, config_version_id
    ) REFERENCES transactions (
        scope_key, account_id, source_transaction_id, config_version_id
    ) ON DELETE CASCADE,
    CONSTRAINT fk_transaction_categories_config_rule FOREIGN KEY (
        config_version_id, category_id, rule_version_id
    ) REFERENCES config_version_rules (
        config_version_id, category_id, rule_version_id
    ) ON DELETE RESTRICT
);
CREATE INDEX ix_transaction_categories_category ON transaction_categories (category_id);

CREATE TABLE sync_states (
    scope_key VARCHAR(128) NOT NULL,
    cursor JSON NOT NULL,
    cursor_hash VARCHAR(64) NOT NULL,
    config_version_id CHAR(32) NOT NULL,
    last_refresh_run_id CHAR(32) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_sync_states PRIMARY KEY (scope_key),
    UNIQUE (last_refresh_run_id),
    CONSTRAINT fk_sync_states_last_run_scope_config FOREIGN KEY (
        last_refresh_run_id, scope_key, config_version_id
    ) REFERENCES refresh_runs (id, scope_key, config_version_id) ON DELETE RESTRICT,
    CONSTRAINT revision_nonnegative CHECK (revision >= 0),
    CONSTRAINT cursor_hash_sha256 CHECK (length(cursor_hash) = 64)
);
"""


_SQLITE_CONFIG_CONTENT_TRIGGER = """
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
"""


_SQLITE_NEW_TRIGGERS = (
    """CREATE TRIGGER trg_refresh_runs_no_delete BEFORE DELETE ON refresh_runs
    BEGIN SELECT RAISE(ABORT, 'refresh_runs cannot be deleted'); END""",
    """CREATE TRIGGER trg_refresh_runs_identity_immutable BEFORE UPDATE ON refresh_runs
    WHEN NEW.id IS NOT OLD.id
      OR NEW.idempotency_key IS NOT OLD.idempotency_key
      OR NEW.request_hash IS NOT OLD.request_hash
      OR NEW.mode IS NOT OLD.mode
      OR NEW.config_version_id IS NOT OLD.config_version_id
      OR NEW.source_from_date IS NOT OLD.source_from_date
      OR NEW.source_to_date IS NOT OLD.source_to_date
      OR NEW.expected_accounts IS NOT OLD.expected_accounts
      OR NEW.sync_revision_before IS NOT OLD.sync_revision_before
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT, 'refresh run identity/manifest is immutable'); END""",
    """CREATE TRIGGER trg_refresh_runs_terminal_immutable BEFORE UPDATE ON refresh_runs
    WHEN OLD.state IN ('committed', 'failed')
    BEGIN SELECT RAISE(ABORT, 'terminal refresh runs are immutable'); END""",
    """CREATE TRIGGER trg_refresh_runs_state_guard BEFORE UPDATE OF state ON refresh_runs
    WHEN NEW.state IS NOT OLD.state AND NOT (
        (OLD.state = 'created' AND NEW.state IN ('uploaded', 'failed'))
        OR (OLD.state = 'created' AND NEW.state = 'validated'
            AND COALESCE(NEW.expected_batch_count, -1) = 0
            AND NEW.received_batch_count = 0 AND NEW.actual_item_count = 0)
        OR (OLD.state = 'uploaded' AND NEW.state IN ('validated', 'failed'))
        OR (OLD.state = 'validated' AND NEW.state IN ('committed', 'failed'))
    ) BEGIN SELECT RAISE(ABORT, 'invalid refresh run state transition'); END""",
    """CREATE TRIGGER trg_refresh_runs_active_incremental BEFORE INSERT ON refresh_runs
    WHEN NEW.mode = 'incremental' AND NOT EXISTS (
        SELECT 1 FROM config_versions WHERE id = NEW.config_version_id AND status = 'active'
    ) BEGIN SELECT RAISE(ABORT, 'incremental refresh requires the active config version'); END""",
    """CREATE TRIGGER trg_sync_state_committed_run_insert BEFORE INSERT ON sync_states
    WHEN NOT EXISTS (
        SELECT 1 FROM refresh_runs WHERE id = NEW.last_refresh_run_id
          AND config_version_id = NEW.config_version_id AND state = 'committed'
    ) BEGIN SELECT RAISE(ABORT, 'sync state must reference a committed refresh run'); END""",
    """CREATE TRIGGER trg_sync_state_committed_run_update BEFORE UPDATE ON sync_states
    WHEN NOT EXISTS (
        SELECT 1 FROM refresh_runs WHERE id = NEW.last_refresh_run_id
          AND config_version_id = NEW.config_version_id AND state = 'committed'
    ) BEGIN SELECT RAISE(ABORT, 'sync state must reference a committed refresh run'); END""",
)


_SQLITE_OLD_TRIGGERS = tuple(
    statement.replace(
        "config_version_id = NEW.config_version_id AND state",
        "scope_key = NEW.scope_key AND config_version_id = NEW.config_version_id AND state",
    )
    .replace(
        "OR NEW.sync_revision_before IS NOT OLD.sync_revision_before\n",
        "",
    )
    .replace(
        "OR NEW.expected_accounts IS NOT OLD.expected_accounts\n",
        "OR NEW.expected_accounts IS NOT OLD.expected_accounts\n"
        "      OR NEW.cursor_before IS NOT OLD.cursor_before\n",
    )
    .replace(
        "AND COALESCE(NEW.expected_batch_count, -1) = 0\n"
        "            AND NEW.received_batch_count = 0 AND NEW.actual_item_count = 0",
        "AND COALESCE(NEW.expected_batch_count, -1) = 0\n"
        "            AND COALESCE(NEW.expected_source_count, -1) = 0\n"
        "            AND COALESCE(NEW.expected_store_count, -1) = 0\n"
        "            AND COALESCE(NEW.expected_skip_count, -1) = 0\n"
        "            AND NEW.received_batch_count = 0 AND NEW.actual_source_count = 0\n"
        "            AND NEW.actual_store_count = 0 AND NEW.actual_skip_count = 0",
    )
    .replace(
        "OR NEW.config_version_id IS NOT OLD.config_version_id\n"
        "      OR NEW.source_from_date",
        "OR NEW.config_version_id IS NOT OLD.config_version_id\n"
        "      OR NEW.scope_key IS NOT OLD.scope_key\n"
        "      OR NEW.source_from_date",
    )
    for statement in _SQLITE_NEW_TRIGGERS
) + (
    """CREATE TRIGGER trg_staged_categories_store_only_insert
    BEFORE INSERT ON staged_transaction_categories WHEN NOT EXISTS (
        SELECT 1 FROM staged_transactions WHERE run_id = NEW.run_id
          AND scope_key = NEW.scope_key AND config_version_id = NEW.config_version_id
          AND account_id = NEW.account_id
          AND source_transaction_id = NEW.source_transaction_id AND decision = 'store'
    ) BEGIN SELECT RAISE(ABORT, 'only STORE staging rows may have categories'); END""",
    """CREATE TRIGGER trg_staged_categories_store_only_update
    BEFORE UPDATE ON staged_transaction_categories WHEN NOT EXISTS (
        SELECT 1 FROM staged_transactions WHERE run_id = NEW.run_id
          AND scope_key = NEW.scope_key AND config_version_id = NEW.config_version_id
          AND account_id = NEW.account_id
          AND source_transaction_id = NEW.source_transaction_id AND decision = 'store'
    ) BEGIN SELECT RAISE(ABORT, 'only STORE staging rows may have categories'); END""",
)

_SQLITE_BATCH_TRIGGERS = (
    """CREATE TRIGGER trg_refresh_batches_immutable_update
    BEFORE UPDATE ON refresh_batches
    BEGIN SELECT RAISE(ABORT, 'refresh_batches are immutable'); END""",
    """CREATE TRIGGER trg_refresh_batches_immutable_delete
    BEFORE DELETE ON refresh_batches
    BEGIN SELECT RAISE(ABORT, 'refresh_batches are immutable'); END""",
    """CREATE TRIGGER trg_refresh_batches_open_run BEFORE INSERT ON refresh_batches
    WHEN NOT EXISTS (
        SELECT 1 FROM refresh_runs WHERE id = NEW.run_id
          AND state IN ('created', 'uploaded')
    ) BEGIN SELECT RAISE(ABORT, 'refresh batch requires an open refresh run'); END""",
)


def _sqlite_execute_script(bind: sa.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            bind.exec_driver_sql(statement)


def _sqlite_atomic(bind: sa.Connection, work: Callable[[], None]) -> None:
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
        raise RuntimeError("SQLite foreign-key enforcement must be enabled before migration")
    dbapi_connection = bind.connection.driver_connection
    if not dbapi_connection.in_transaction:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    # All referencing tables are backed up and dropped child-first, so foreign keys
    # can stay enabled and Alembic can commit the schema plus its version row atomically.
    bind.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    work()
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"Foreign-key violations after single-user migration: {violations}")


def _sqlite_backup_and_drop(bind: sa.Connection) -> None:
    for table_name in _SQLITE_REBUILD_TABLES:
        bind.exec_driver_sql(
            f"CREATE TEMP TABLE _single_user_{table_name} AS SELECT * FROM {table_name}"
        )
    for table_name in (
        "sync_states",
        "transaction_categories",
        "transactions",
        "staged_transaction_categories",
        "staged_transactions",
        "refresh_batches",
        "refresh_runs",
    ):
        bind.exec_driver_sql(f"DROP TABLE {table_name}")


def _sqlite_upgrade(
    bind: sa.Connection,
    config_rewrites: list[dict[str, Any]],
    request_rewrites: list[dict[str, Any]],
) -> None:
    def work() -> None:
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_config_versions_content_immutable")
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_refresh_runs_no_delete")
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_refresh_batches_immutable_delete")
        _sqlite_backup_and_drop(bind)
        _sqlite_execute_script(bind, _SQLITE_NEW_TABLES)
        # The released application already used the Money TypeDecorator on
        # SQLite. Although the 0002 column was declared NUMERIC, application
        # writes were already four-decimal fixed-point integers. Preserve the
        # raw values while rebuilding the column as BIGINT.
        bind.exec_driver_sql(
            """INSERT INTO refresh_runs (
                id, idempotency_key, request_hash, mode, state, config_version_id,
                source_from_date, source_to_date, expected_accounts, completed_accounts,
                expected_batch_count, computed_checksum, sync_revision_before,
                received_batch_count, actual_item_count, created_at,
                uploaded_at, validated_at, committed_at, failed_at, error_code
            ) SELECT
                r.id, r.idempotency_key, r.request_hash, r.mode,
                CASE WHEN r.state IN ('created', 'uploaded', 'validated')
                     THEN 'failed' ELSE r.state END,
                r.config_version_id, r.source_from_date, r.source_to_date,
                r.expected_accounts,
                CASE WHEN r.state = 'committed' THEN r.expected_accounts ELSE NULL END,
                r.expected_batch_count, r.computed_checksum, NULL, r.received_batch_count,
                r.actual_store_count,
                r.created_at, r.uploaded_at, r.validated_at, r.committed_at,
                CASE WHEN r.state IN ('created', 'uploaded', 'validated')
                     THEN CURRENT_TIMESTAMP ELSE r.failed_at END,
                CASE WHEN r.state IN ('created', 'uploaded', 'validated')
                     THEN 'upgrade_invalidated' ELSE r.error_code END
            FROM _single_user_refresh_runs r"""
        )
        bind.exec_driver_sql(
            """INSERT INTO refresh_batches
            SELECT run_id, batch_index, idempotency_key, request_hash, checksum,
                   store_count, created_at
            FROM _single_user_refresh_batches"""
        )
        bind.exec_driver_sql(
            """INSERT INTO transactions (
                account_id, account_name, source_id, transaction_date, amount, currency, pending,
                merchant, name, refunded, refund_amount, pending_source_id,
                source_hash, config_version_id, first_refresh_run_id,
                last_refresh_run_id, first_seen_at, last_seen_at
            )
            SELECT account_id, NULL, source_transaction_id, transaction_date,
                   amount, currency,
                   CASE WHEN status = 'pending' THEN 1 ELSE 0 END,
                   merchant, description, refunded,
                   refund_amount,
                   supersedes_source_transaction_id, source_hash, config_version_id,
                   first_refresh_run_id, last_refresh_run_id, first_seen_at, last_seen_at
            FROM _single_user_transactions"""
        )
        bind.exec_driver_sql(
            """INSERT INTO transaction_categories (
                account_id, source_id, category_id, config_version_id, rule_version_id
            )
            SELECT account_id, source_transaction_id, category_id,
                   config_version_id, rule_version_id
            FROM _single_user_transaction_categories"""
        )
        bind.exec_driver_sql(
            """INSERT INTO sync_states
            SELECT 1, config_version_id, last_refresh_run_id, revision, updated_at
            FROM _single_user_sync_states"""
        )
        _apply_config_rewrites(bind, config_rewrites)
        _apply_request_hash_rewrites(bind, request_rewrites)
        bind.exec_driver_sql(_SQLITE_CONFIG_CONTENT_TRIGGER)
        for statement in _SQLITE_NEW_TRIGGERS:
            bind.exec_driver_sql(statement)
        for statement in _SQLITE_BATCH_TRIGGERS:
            bind.exec_driver_sql(statement)
        for table_name in _SQLITE_REBUILD_TABLES:
            bind.exec_driver_sql(f"DROP TABLE _single_user_{table_name}")

    _sqlite_atomic(bind, work)


def _sqlite_downgrade(
    bind: sa.Connection,
    config_rewrites: list[dict[str, Any]],
    request_rewrites: list[dict[str, Any]],
) -> None:
    def work() -> None:
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_config_versions_content_immutable")
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_refresh_runs_no_delete")
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_refresh_batches_immutable_delete")
        _sqlite_backup_and_drop(bind)
        _sqlite_execute_script(bind, _SQLITE_OLD_TABLES)
        # The 0002 runtime also used the Money TypeDecorator, so retain the same
        # raw fixed-point integers in its NUMERIC-declared tables.
        bind.exec_driver_sql(
            """INSERT INTO refresh_runs (
                id, idempotency_key, request_hash, mode, state, config_version_id,
                scope_key, source_from_date, source_to_date, expected_accounts,
                account_manifest, expected_batch_count, expected_source_count,
                expected_store_count, expected_skip_count, source_complete,
                input_checksum, computed_checksum, cursor_before, cursor_after,
                received_batch_count, actual_source_count, actual_store_count,
                actual_skip_count, created_at, uploaded_at, validated_at,
                committed_at, failed_at, error_code
            ) SELECT
                id, idempotency_key, request_hash, mode, state, config_version_id,
                'personal', source_from_date, source_to_date, expected_accounts,
                CASE WHEN state = 'committed' THEN (
                    SELECT COALESCE(
                        json_group_array(json_object(
                            'account_id', value,
                            'pages_complete', 1,
                            'observed_count', 0,
                            'source_reported_count', NULL
                        )),
                        json('[]')
                    )
                    FROM json_each(COALESCE(completed_accounts, expected_accounts))
                ) ELSE NULL END,
                expected_batch_count, actual_item_count,
                actual_item_count, 0,
                CASE WHEN state = 'committed' THEN 1 ELSE 0 END,
                CASE WHEN state = 'committed' THEN computed_checksum ELSE NULL END,
                computed_checksum, json('{}'), json('{}'),
                received_batch_count, actual_item_count, actual_item_count,
                0, created_at, uploaded_at, validated_at,
                committed_at, failed_at, error_code
            FROM _single_user_refresh_runs"""
        )
        bind.exec_driver_sql(
            """INSERT INTO refresh_batches (
                run_id, batch_index, idempotency_key, request_hash, checksum,
                item_count, store_count, skip_count, created_at
            )
            SELECT run_id, batch_index, idempotency_key, request_hash, checksum,
                   item_count, item_count, 0, created_at
            FROM _single_user_refresh_batches"""
        )
        bind.exec_driver_sql(
            """INSERT INTO staged_transactions (
                run_id, scope_key, config_version_id, account_id,
                source_transaction_id, batch_index, decision, transaction_date,
                amount, currency, status, merchant, description, refunded,
                refund_amount, supersedes_source_transaction_id, source_hash
            )
            SELECT run_id, 'personal', config_version_id, account_id,
                   source_id, batch_index, 'store', transaction_date,
                   amount, currency,
                   CASE WHEN pending THEN 'pending' ELSE 'posted' END,
                   merchant, name, refunded, refund_amount,
                   pending_source_id, source_hash
            FROM _single_user_staged_transactions"""
        )
        bind.exec_driver_sql(
            """INSERT INTO staged_transaction_categories (
                run_id, scope_key, config_version_id, account_id,
                source_transaction_id, category_id, rule_version_id
            )
            SELECT run_id, 'personal', config_version_id, account_id,
                   source_id, category_id, rule_version_id
            FROM _single_user_staged_transaction_categories"""
        )
        bind.exec_driver_sql(
            """INSERT INTO transactions (
                scope_key, account_id, source_transaction_id, transaction_date,
                amount, currency, status, merchant, description, refunded,
                refund_amount, supersedes_source_transaction_id, source_hash,
                config_version_id, first_refresh_run_id, last_refresh_run_id,
                first_seen_at, last_seen_at
            )
            SELECT 'personal', account_id, source_id, transaction_date,
                   amount, currency,
                   CASE WHEN pending THEN 'pending' ELSE 'posted' END,
                   merchant, name, refunded, refund_amount,
                   pending_source_id, source_hash,
                   config_version_id, first_refresh_run_id, last_refresh_run_id,
                   first_seen_at, last_seen_at
            FROM _single_user_transactions"""
        )
        bind.exec_driver_sql(
            """INSERT INTO transaction_categories (
                scope_key, account_id, source_transaction_id, category_id,
                config_version_id, rule_version_id
            )
            SELECT 'personal', account_id, source_id, category_id,
                   config_version_id, rule_version_id
            FROM _single_user_transaction_categories"""
        )
        bind.exec_driver_sql(
            """INSERT INTO sync_states
            SELECT 'personal', json('{}'),
                   '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
                   config_version_id,
                   last_refresh_run_id, revision, updated_at
            FROM _single_user_sync_states"""
        )
        _apply_config_rewrites(bind, config_rewrites)
        _apply_request_hash_rewrites(bind, request_rewrites)
        bind.exec_driver_sql(_SQLITE_CONFIG_CONTENT_TRIGGER)
        for statement in _SQLITE_OLD_TRIGGERS:
            bind.exec_driver_sql(statement)
        for statement in _SQLITE_BATCH_TRIGGERS:
            bind.exec_driver_sql(statement)
        for table_name in _SQLITE_REBUILD_TABLES:
            bind.exec_driver_sql(f"DROP TABLE _single_user_{table_name}")

    _sqlite_atomic(bind, work)


_POSTGRES_CONFIG_GUARD = """
CREATE OR REPLACE FUNCTION guard_config_version_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'config_versions cannot be deleted'; END IF;
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
        RAISE EXCEPTION 'invalid config version status transition: % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$
"""


_POSTGRES_REFRESH_GUARD_NEW = """
CREATE OR REPLACE FUNCTION guard_refresh_run_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'refresh_runs cannot be deleted'; END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
       OR NEW.mode IS DISTINCT FROM OLD.mode
       OR NEW.config_version_id IS DISTINCT FROM OLD.config_version_id
       OR NEW.source_from_date IS DISTINCT FROM OLD.source_from_date
       OR NEW.source_to_date IS DISTINCT FROM OLD.source_to_date
       OR NEW.expected_accounts IS DISTINCT FROM OLD.expected_accounts
       OR NEW.sync_revision_before IS DISTINCT FROM OLD.sync_revision_before
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'refresh run identity/manifest is immutable';
    END IF;
    IF OLD.state IN ('committed', 'failed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal refresh runs are immutable';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        (OLD.state = 'created' AND NEW.state IN ('uploaded', 'failed'))
        OR (OLD.state = 'created' AND NEW.state = 'validated'
            AND NEW.expected_batch_count = 0
            AND NEW.received_batch_count = 0 AND NEW.actual_item_count = 0)
        OR (OLD.state = 'uploaded' AND NEW.state IN ('validated', 'failed'))
        OR (OLD.state = 'validated' AND NEW.state IN ('committed', 'failed'))
    ) THEN
        RAISE EXCEPTION 'invalid refresh run state transition: % -> %', OLD.state, NEW.state;
    END IF;
    RETURN NEW;
END;
$$
"""


_POSTGRES_REFRESH_GUARD_OLD = _POSTGRES_REFRESH_GUARD_NEW.replace(
    "OR NEW.config_version_id IS DISTINCT FROM OLD.config_version_id\n"
    "       OR NEW.source_from_date",
    "OR NEW.config_version_id IS DISTINCT FROM OLD.config_version_id\n"
    "       OR NEW.scope_key IS DISTINCT FROM OLD.scope_key\n       OR NEW.source_from_date",
).replace(
    "       OR NEW.sync_revision_before IS DISTINCT FROM OLD.sync_revision_before\n",
    "",
).replace(
    "       OR NEW.expected_accounts IS DISTINCT FROM OLD.expected_accounts\n",
    "       OR NEW.expected_accounts IS DISTINCT FROM OLD.expected_accounts\n"
    "       OR NEW.cursor_before IS DISTINCT FROM OLD.cursor_before\n",
).replace(
    "            AND NEW.expected_batch_count = 0\n"
    "            AND NEW.received_batch_count = 0 AND NEW.actual_item_count = 0",
    "            AND NEW.expected_batch_count = 0 AND NEW.expected_source_count = 0\n"
    "            AND NEW.expected_store_count = 0 AND NEW.expected_skip_count = 0\n"
    "            AND NEW.received_batch_count = 0 AND NEW.actual_source_count = 0\n"
    "            AND NEW.actual_store_count = 0 AND NEW.actual_skip_count = 0",
)


_POSTGRES_SYNC_GUARD_NEW = """
CREATE OR REPLACE FUNCTION require_committed_sync_run()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run_state refresh_run_state;
BEGIN
    SELECT state INTO run_state FROM refresh_runs
    WHERE id = NEW.last_refresh_run_id AND config_version_id = NEW.config_version_id;
    IF run_state IS DISTINCT FROM 'committed' THEN
        RAISE EXCEPTION 'sync cursor must reference a committed refresh run';
    END IF;
    RETURN NEW;
END;
$$
"""


_POSTGRES_SYNC_GUARD_OLD = _POSTGRES_SYNC_GUARD_NEW.replace(
    "WHERE id = NEW.last_refresh_run_id AND config_version_id",
    "WHERE id = NEW.last_refresh_run_id AND scope_key = NEW.scope_key AND config_version_id",
)


_POSTGRES_STAGED_GUARD_OLD = """
CREATE OR REPLACE FUNCTION require_staged_store_for_category()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_decision staged_decision;
BEGIN
    SELECT decision INTO parent_decision FROM staged_transactions
    WHERE run_id = NEW.run_id AND scope_key = NEW.scope_key
      AND config_version_id = NEW.config_version_id
      AND account_id = NEW.account_id
      AND source_transaction_id = NEW.source_transaction_id;
    IF parent_decision IS DISTINCT FROM 'store' THEN
        RAISE EXCEPTION 'only STORE staging rows may have categories';
    END IF;
    RETURN NEW;
END;
$$
"""


def _drop_postgres_guards() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_config_versions_guard ON config_versions")
    op.execute("DROP FUNCTION IF EXISTS guard_config_version_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_refresh_runs_guard ON refresh_runs")
    op.execute("DROP FUNCTION IF EXISTS guard_refresh_run_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_state_committed_run ON sync_states")
    op.execute("DROP FUNCTION IF EXISTS require_committed_sync_run()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_staged_categories_store_only "
        "ON staged_transaction_categories"
    )
    op.execute("DROP FUNCTION IF EXISTS require_staged_store_for_category()")


def _drop_postgres_legacy_checks(table_name: str, constraint_names: Sequence[str]) -> None:
    """Drop checks from either pristine 0002 or a 0003 downgrade.

    The original migration embedded the naming-convention prefix in several
    explicit names, so pristine PostgreSQL databases contain a double prefix.
    Alembic-created downgrade constraints contain the normal single prefix.
    Both spellings are equivalent and are accepted for a repeatable round trip.
    """

    for constraint_name in constraint_names:
        double_prefixed = f"ck_{table_name}_{constraint_name}"
        op.execute(
            sa.text(
                f'ALTER TABLE "{table_name}" '
                f'DROP CONSTRAINT IF EXISTS "{constraint_name}"'
            )
        )
        op.execute(
            sa.text(
                f'ALTER TABLE "{table_name}" '
                f'DROP CONSTRAINT IF EXISTS "{double_prefixed}"'
            )
        )


def _create_postgres_guards(*, legacy_scope: bool) -> None:
    op.execute(_POSTGRES_CONFIG_GUARD)
    op.execute(
        "CREATE TRIGGER trg_config_versions_guard BEFORE UPDATE OR DELETE ON config_versions "
        "FOR EACH ROW EXECUTE FUNCTION guard_config_version_mutation()"
    )
    op.execute(_POSTGRES_REFRESH_GUARD_OLD if legacy_scope else _POSTGRES_REFRESH_GUARD_NEW)
    op.execute(
        "CREATE TRIGGER trg_refresh_runs_guard BEFORE UPDATE OR DELETE ON refresh_runs "
        "FOR EACH ROW EXECUTE FUNCTION guard_refresh_run_mutation()"
    )
    op.execute(_POSTGRES_SYNC_GUARD_OLD if legacy_scope else _POSTGRES_SYNC_GUARD_NEW)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_sync_state_committed_run "
        "AFTER INSERT OR UPDATE ON sync_states DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION require_committed_sync_run()"
    )
    if legacy_scope:
        op.execute(_POSTGRES_STAGED_GUARD_OLD)
        op.execute(
            "CREATE TRIGGER trg_staged_categories_store_only "
            "BEFORE INSERT OR UPDATE ON staged_transaction_categories "
            "FOR EACH ROW EXECUTE FUNCTION require_staged_store_for_category()"
        )


def _postgres_upgrade(
    bind: sa.Connection,
    config_rewrites: list[dict[str, Any]],
    request_rewrites: list[dict[str, Any]],
) -> None:
    _drop_postgres_guards()
    _drop_postgres_legacy_checks(
        "refresh_runs",
        (
            "ck_refresh_runs_committed_manifest_complete",
            "ck_refresh_runs_expected_source_count_nonnegative",
            "ck_refresh_runs_expected_store_count_nonnegative",
            "ck_refresh_runs_expected_skip_count_nonnegative",
            "ck_refresh_runs_expected_counts_match",
            "ck_refresh_runs_input_checksum_sha256",
            "ck_refresh_runs_actual_source_count_nonnegative",
            "ck_refresh_runs_actual_store_count_nonnegative",
            "ck_refresh_runs_actual_skip_count_nonnegative",
            "ck_refresh_runs_actual_counts_match",
        ),
    )
    _drop_postgres_legacy_checks(
        "sync_states", ("ck_sync_states_cursor_hash_sha256",)
    )
    _drop_postgres_legacy_checks(
        "refresh_batches",
        (
            "ck_refresh_batches_store_count_nonnegative",
            "ck_refresh_batches_skip_count_nonnegative",
            "ck_refresh_batches_item_count_matches",
        ),
    )
    _drop_postgres_legacy_checks(
        "staged_transactions",
        ("ck_staged_transactions_stored_fields_present",),
    )
    op.alter_column(
        "refresh_runs",
        "account_manifest",
        new_column_name="completed_accounts",
    )
    op.add_column("refresh_runs", sa.Column("sync_revision_before", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "sync_revision_before_nonnegative",
        "refresh_runs",
        "sync_revision_before IS NULL OR sync_revision_before >= 0",
    )
    op.execute(
        "UPDATE refresh_runs SET state = 'failed', failed_at = CURRENT_TIMESTAMP, "
        "error_code = 'upgrade_invalidated' "
        "WHERE state IN ('created', 'uploaded', 'validated')"
    )
    op.execute(
        "UPDATE refresh_runs SET completed_accounts = "
        "CASE WHEN state = 'committed' THEN expected_accounts ELSE NULL END"
    )
    _apply_config_rewrites(bind, config_rewrites)
    _apply_request_hash_rewrites(bind, request_rewrites)

    for column_name in (
        "expected_source_count",
        "expected_store_count",
        "expected_skip_count",
        "source_complete",
        "input_checksum",
        "cursor_before",
        "cursor_after",
    ):
        op.drop_column("refresh_runs", column_name)
    op.drop_column("sync_states", "cursor")
    op.drop_column("sync_states", "cursor_hash")

    op.drop_constraint(
        "fk_staged_transaction_categories_transaction",
        "staged_transaction_categories",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_transaction_categories_transaction", "transaction_categories", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_sync_states_last_run_scope_config", "sync_states", type_="foreignkey"
    )
    op.drop_constraint("fk_transactions_first_run_scope", "transactions", type_="foreignkey")
    op.drop_constraint(
        "fk_transactions_last_run_scope_config", "transactions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_staged_transactions_run_scope_config", "staged_transactions", type_="foreignkey"
    )
    for table_name, constraint_name in (
        ("staged_transaction_categories", "pk_staged_transaction_categories"),
        ("staged_transactions", "pk_staged_transactions"),
        ("transaction_categories", "pk_transaction_categories"),
        ("transactions", "pk_transactions"),
        ("sync_states", "pk_sync_states"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="primary")
    for table_name, constraint_name in (
        ("staged_transactions", "uq_staged_transactions_identity_config"),
        ("transactions", "uq_transactions_identity_config"),
        ("refresh_runs", "uq_refresh_runs_id_scope"),
        ("refresh_runs", "uq_refresh_runs_id_scope_config"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="unique")
    op.drop_index("ix_refresh_runs_scope_created", table_name="refresh_runs")
    op.drop_index("ix_transactions_scope_date", table_name="transactions")
    op.drop_index("ix_transactions_scope_status_date", table_name="transactions")

    op.alter_column(
        "refresh_runs", "actual_store_count", new_column_name="actual_item_count"
    )
    op.drop_column("refresh_runs", "actual_source_count")
    op.drop_column("refresh_runs", "actual_skip_count")
    op.create_check_constraint(
        "actual_item_count_nonnegative",
        "refresh_runs",
        "actual_item_count >= 0",
    )
    op.execute("UPDATE refresh_batches SET item_count = store_count")
    op.drop_column("refresh_batches", "store_count")
    op.drop_column("refresh_batches", "skip_count")

    # Legacy staging belongs only to runs that have already committed (where it is
    # redundant) or to nonterminal runs invalidated above.  Do not carry those raw
    # work rows into the durable single-user schema.
    op.execute("DELETE FROM staged_transaction_categories")
    op.execute("DELETE FROM staged_transactions")
    op.add_column(
        "staged_transactions", sa.Column("account_name", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "transactions", sa.Column("account_name", sa.String(length=255), nullable=True)
    )
    for table_name in (
        "staged_transactions",
        "staged_transaction_categories",
        "transactions",
        "transaction_categories",
    ):
        op.alter_column(
            table_name,
            "source_transaction_id",
            new_column_name="source_id",
        )
    for table_name in ("staged_transactions", "transactions"):
        op.alter_column(table_name, "description", new_column_name="name")
        op.alter_column(
            table_name,
            "supersedes_source_transaction_id",
            new_column_name="pending_source_id",
        )
        op.add_column(table_name, sa.Column("pending", sa.Boolean(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table_name} SET pending = (status = 'pending')"
            )
        )
        op.alter_column(table_name, "pending", nullable=False)
        op.drop_column(table_name, "status")
        op.create_check_constraint(
            "pending_boolean", table_name, "pending IN (false, true)"
        )
    for column_name in ("transaction_date", "amount", "currency"):
        op.alter_column("staged_transactions", column_name, nullable=False)
    op.drop_column("staged_transactions", "decision")
    op.execute("DROP TYPE staged_decision")
    op.execute("DROP TYPE transaction_status")

    op.add_column(
        "sync_states",
        sa.Column("id", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    for table_name in _SCOPED_TABLES:
        op.drop_column(table_name, "scope_key")

    op.create_unique_constraint(
        "uq_refresh_runs_id_config",
        "refresh_runs",
        ["id", "config_version_id"],
    )
    op.create_primary_key(
        "pk_staged_transactions",
        "staged_transactions",
        ["run_id", "account_id", "source_id"],
    )
    op.create_unique_constraint(
        "uq_staged_transactions_identity_config",
        "staged_transactions",
        ["run_id", "account_id", "source_id", "config_version_id"],
    )
    op.create_primary_key(
        "pk_staged_transaction_categories",
        "staged_transaction_categories",
        ["run_id", "account_id", "source_id", "category_id"],
    )
    op.create_primary_key(
        "pk_transactions", "transactions", ["account_id", "source_id"]
    )
    op.create_unique_constraint(
        "uq_transactions_identity_config",
        "transactions",
        ["account_id", "source_id", "config_version_id"],
    )
    op.create_primary_key(
        "pk_transaction_categories",
        "transaction_categories",
        ["account_id", "source_id", "category_id"],
    )
    op.create_primary_key("pk_sync_states", "sync_states", ["id"])
    op.create_check_constraint("singleton", "sync_states", "id = 1")
    op.create_check_constraint(
        "committed_refresh_complete",
        "refresh_runs",
        "state != 'committed' OR ("
        "expected_batch_count IS NOT NULL "
        "AND computed_checksum IS NOT NULL "
        "AND expected_batch_count = received_batch_count "
        "AND completed_accounts IS NOT NULL "
        "AND (expected_batch_count = 0 OR uploaded_at IS NOT NULL) "
        "AND validated_at IS NOT NULL "
        "AND committed_at IS NOT NULL)",
    )

    op.create_foreign_key(
        "fk_staged_transactions_run_config",
        "staged_transactions",
        "refresh_runs",
        ["run_id", "config_version_id"],
        ["id", "config_version_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_staged_transaction_categories_transaction",
        "staged_transaction_categories",
        "staged_transactions",
        ["run_id", "account_id", "source_id", "config_version_id"],
        ["run_id", "account_id", "source_id", "config_version_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_transactions_first_run",
        "transactions",
        "refresh_runs",
        ["first_refresh_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_transactions_last_run_config",
        "transactions",
        "refresh_runs",
        ["last_refresh_run_id", "config_version_id"],
        ["id", "config_version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_transaction_categories_transaction",
        "transaction_categories",
        "transactions",
        ["account_id", "source_id", "config_version_id"],
        ["account_id", "source_id", "config_version_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_sync_states_last_run_config",
        "sync_states",
        "refresh_runs",
        ["last_refresh_run_id", "config_version_id"],
        ["id", "config_version_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_refresh_runs_created", "refresh_runs", ["created_at"])
    op.create_index("ix_transactions_date", "transactions", ["transaction_date"])
    op.create_index(
        "ix_transactions_pending_date", "transactions", ["pending", "transaction_date"]
    )
    _create_postgres_guards(legacy_scope=False)


def _postgres_downgrade(
    bind: sa.Connection,
    config_rewrites: list[dict[str, Any]],
    request_rewrites: list[dict[str, Any]],
) -> None:
    _drop_postgres_guards()
    op.drop_column("staged_transactions", "account_name")
    op.drop_column("transactions", "account_name")
    op.drop_constraint(
        "committed_refresh_complete", "refresh_runs", type_="check"
    )
    op.alter_column(
        "refresh_runs",
        "completed_accounts",
        new_column_name="account_manifest",
    )
    for column in (
        sa.Column("expected_source_count", sa.Integer(), nullable=True),
        sa.Column("expected_store_count", sa.Integer(), nullable=True),
        sa.Column("expected_skip_count", sa.Integer(), nullable=True),
        sa.Column("source_complete", sa.Boolean(), nullable=True),
        sa.Column("input_checksum", sa.String(length=64), nullable=True),
        sa.Column("cursor_before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cursor_after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    ):
        op.add_column("refresh_runs", column)
    op.add_column(
        "sync_states",
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "sync_states", sa.Column("cursor_hash", sa.String(length=64), nullable=True)
    )
    op.execute(
        """UPDATE refresh_runs r SET
            account_manifest = CASE WHEN state = 'committed' THEN (
                SELECT COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'account_id', account_id,
                            'pages_complete', true,
                            'observed_count', 0,
                            'source_reported_count', NULL
                        ) ORDER BY account_id
                    ),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements_text(r.expected_accounts) AS accounts(account_id)
            ) ELSE NULL END,
            expected_source_count = actual_item_count,
            expected_store_count = actual_item_count,
            expected_skip_count = 0,
            source_complete = (state = 'committed'),
            input_checksum = CASE WHEN state = 'committed' THEN computed_checksum ELSE NULL END,
            cursor_before = '{}'::jsonb,
            cursor_after = '{}'::jsonb"""
    )
    op.execute(
        "UPDATE sync_states SET cursor = '{}'::jsonb, "
        "cursor_hash = '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'"
    )
    op.alter_column("refresh_runs", "source_complete", nullable=False)
    op.alter_column("sync_states", "cursor", nullable=False)
    op.alter_column("sync_states", "cursor_hash", nullable=False)
    for constraint_name, condition in (
        (
            "ck_refresh_runs_expected_source_count_nonnegative",
            "expected_source_count IS NULL OR expected_source_count >= 0",
        ),
        (
            "ck_refresh_runs_expected_store_count_nonnegative",
            "expected_store_count IS NULL OR expected_store_count >= 0",
        ),
        (
            "ck_refresh_runs_expected_skip_count_nonnegative",
            "expected_skip_count IS NULL OR expected_skip_count >= 0",
        ),
        (
            "ck_refresh_runs_expected_counts_match",
            "expected_source_count IS NULL OR expected_store_count IS NULL "
            "OR expected_skip_count IS NULL "
            "OR expected_store_count + expected_skip_count = expected_source_count",
        ),
        (
            "ck_refresh_runs_input_checksum_sha256",
            "input_checksum IS NULL OR length(input_checksum) = 64",
        ),
    ):
        op.create_check_constraint(constraint_name, "refresh_runs", condition)
    op.create_check_constraint(
        "ck_sync_states_cursor_hash_sha256", "sync_states", "length(cursor_hash) = 64"
    )
    for table_name, constraint_name in (
        ("staged_transaction_categories", "fk_staged_transaction_categories_transaction"),
        ("transaction_categories", "fk_transaction_categories_transaction"),
        ("sync_states", "fk_sync_states_last_run_config"),
        ("transactions", "fk_transactions_first_run"),
        ("transactions", "fk_transactions_last_run_config"),
        ("staged_transactions", "fk_staged_transactions_run_config"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")
    for table_name, constraint_name in (
        ("staged_transaction_categories", "pk_staged_transaction_categories"),
        ("staged_transactions", "pk_staged_transactions"),
        ("transaction_categories", "pk_transaction_categories"),
        ("transactions", "pk_transactions"),
        ("sync_states", "pk_sync_states"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="primary")
    for table_name, constraint_name in (
        ("staged_transactions", "uq_staged_transactions_identity_config"),
        ("transactions", "uq_transactions_identity_config"),
        ("refresh_runs", "uq_refresh_runs_id_config"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="unique")
    op.drop_constraint("singleton", "sync_states", type_="check")
    op.drop_index("ix_refresh_runs_created", table_name="refresh_runs")
    op.drop_index("ix_transactions_date", table_name="transactions")
    op.drop_index("ix_transactions_pending_date", table_name="transactions")

    op.drop_constraint(
        "actual_item_count_nonnegative", "refresh_runs", type_="check"
    )
    op.alter_column(
        "refresh_runs", "actual_item_count", new_column_name="actual_store_count"
    )
    op.add_column(
        "refresh_runs",
        sa.Column("actual_source_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "refresh_runs",
        sa.Column("actual_skip_count", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE refresh_runs SET actual_source_count = actual_store_count, "
        "actual_skip_count = 0"
    )
    op.alter_column("refresh_runs", "actual_source_count", nullable=False)
    op.alter_column("refresh_runs", "actual_skip_count", nullable=False)
    for constraint_name, condition in (
        ("ck_refresh_runs_actual_source_count_nonnegative", "actual_source_count >= 0"),
        ("ck_refresh_runs_actual_store_count_nonnegative", "actual_store_count >= 0"),
        ("ck_refresh_runs_actual_skip_count_nonnegative", "actual_skip_count >= 0"),
        (
            "ck_refresh_runs_actual_counts_match",
            "actual_store_count + actual_skip_count = actual_source_count",
        ),
    ):
        op.create_check_constraint(constraint_name, "refresh_runs", condition)
    op.create_check_constraint(
        "ck_refresh_runs_committed_manifest_complete",
        "refresh_runs",
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
    )

    op.add_column(
        "refresh_batches", sa.Column("store_count", sa.Integer(), nullable=True)
    )
    op.add_column(
        "refresh_batches", sa.Column("skip_count", sa.Integer(), nullable=True)
    )
    op.execute(
        "UPDATE refresh_batches SET store_count = item_count, skip_count = 0"
    )
    op.alter_column("refresh_batches", "store_count", nullable=False)
    op.alter_column("refresh_batches", "skip_count", nullable=False)
    for constraint_name, condition in (
        ("ck_refresh_batches_store_count_nonnegative", "store_count >= 0"),
        ("ck_refresh_batches_skip_count_nonnegative", "skip_count >= 0"),
        ("ck_refresh_batches_item_count_matches", "store_count + skip_count = item_count"),
    ):
        op.create_check_constraint(constraint_name, "refresh_batches", condition)

    op.execute("CREATE TYPE staged_decision AS ENUM ('store', 'skip')")
    op.execute("CREATE TYPE transaction_status AS ENUM ('pending', 'posted')")
    legacy_decision = postgresql.ENUM(
        "store", "skip", name="staged_decision", create_type=False
    )
    legacy_status = postgresql.ENUM(
        "pending", "posted", name="transaction_status", create_type=False
    )
    op.add_column(
        "staged_transactions", sa.Column("decision", legacy_decision, nullable=True)
    )
    op.execute("UPDATE staged_transactions SET decision = 'store'")
    op.alter_column("staged_transactions", "decision", nullable=False)
    for table_name in ("staged_transactions", "transactions"):
        op.drop_constraint("pending_boolean", table_name, type_="check")
        op.add_column(table_name, sa.Column("status", legacy_status, nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table_name} SET status = "
                "CASE WHEN pending THEN 'pending'::transaction_status "
                "ELSE 'posted'::transaction_status END"
            )
        )
        if table_name == "transactions":
            op.alter_column(table_name, "status", nullable=False)
        op.drop_column(table_name, "pending")
        op.alter_column(table_name, "name", new_column_name="description")
        op.alter_column(
            table_name,
            "pending_source_id",
            new_column_name="supersedes_source_transaction_id",
        )
    for column_name in ("transaction_date", "amount", "currency"):
        op.alter_column("staged_transactions", column_name, nullable=True)
    op.create_check_constraint(
        "ck_staged_transactions_stored_fields_present",
        "staged_transactions",
        "decision != 'store' OR ("
        "transaction_date IS NOT NULL AND amount IS NOT NULL "
        "AND currency IS NOT NULL AND status IS NOT NULL)",
    )
    for table_name in (
        "staged_transactions",
        "staged_transaction_categories",
        "transactions",
        "transaction_categories",
    ):
        op.alter_column(table_name, "source_id", new_column_name="source_transaction_id")

    for table_name in _SCOPED_TABLES:
        op.add_column(
            table_name,
            sa.Column(
                "scope_key",
                sa.String(length=128),
                nullable=False,
                server_default=sa.text("'personal'"),
            ),
        )
    op.drop_column("sync_states", "id")
    op.drop_constraint(
        "sync_revision_before_nonnegative", "refresh_runs", type_="check"
    )
    op.drop_column("refresh_runs", "sync_revision_before")

    op.create_unique_constraint(
        "uq_refresh_runs_id_scope", "refresh_runs", ["id", "scope_key"]
    )
    op.create_unique_constraint(
        "uq_refresh_runs_id_scope_config",
        "refresh_runs",
        ["id", "scope_key", "config_version_id"],
    )
    op.create_primary_key(
        "pk_staged_transactions",
        "staged_transactions",
        ["run_id", "scope_key", "account_id", "source_transaction_id"],
    )
    op.create_unique_constraint(
        "uq_staged_transactions_identity_config",
        "staged_transactions",
        ["run_id", "scope_key", "account_id", "source_transaction_id", "config_version_id"],
    )
    op.create_primary_key(
        "pk_staged_transaction_categories",
        "staged_transaction_categories",
        ["run_id", "scope_key", "account_id", "source_transaction_id", "category_id"],
    )
    op.create_primary_key(
        "pk_transactions", "transactions", ["scope_key", "account_id", "source_transaction_id"]
    )
    op.create_unique_constraint(
        "uq_transactions_identity_config",
        "transactions",
        ["scope_key", "account_id", "source_transaction_id", "config_version_id"],
    )
    op.create_primary_key(
        "pk_transaction_categories",
        "transaction_categories",
        ["scope_key", "account_id", "source_transaction_id", "category_id"],
    )
    op.create_primary_key("pk_sync_states", "sync_states", ["scope_key"])
    op.create_foreign_key(
        "fk_staged_transactions_run_scope_config",
        "staged_transactions",
        "refresh_runs",
        ["run_id", "scope_key", "config_version_id"],
        ["id", "scope_key", "config_version_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_staged_transaction_categories_transaction",
        "staged_transaction_categories",
        "staged_transactions",
        ["run_id", "scope_key", "account_id", "source_transaction_id", "config_version_id"],
        ["run_id", "scope_key", "account_id", "source_transaction_id", "config_version_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_transactions_first_run_scope",
        "transactions",
        "refresh_runs",
        ["first_refresh_run_id", "scope_key"],
        ["id", "scope_key"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_transactions_last_run_scope_config",
        "transactions",
        "refresh_runs",
        ["last_refresh_run_id", "scope_key", "config_version_id"],
        ["id", "scope_key", "config_version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_transaction_categories_transaction",
        "transaction_categories",
        "transactions",
        ["scope_key", "account_id", "source_transaction_id", "config_version_id"],
        ["scope_key", "account_id", "source_transaction_id", "config_version_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_sync_states_last_run_scope_config",
        "sync_states",
        "refresh_runs",
        ["last_refresh_run_id", "scope_key", "config_version_id"],
        ["id", "scope_key", "config_version_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_refresh_runs_scope_created", "refresh_runs", ["scope_key", "created_at"]
    )
    op.create_index(
        "ix_transactions_scope_date", "transactions", ["scope_key", "transaction_date"]
    )
    op.create_index(
        "ix_transactions_scope_status_date",
        "transactions",
        ["scope_key", "status", "transaction_date"],
    )
    _apply_config_rewrites(bind, config_rewrites)
    _apply_request_hash_rewrites(bind, request_rewrites)
    for table_name in _SCOPED_TABLES:
        op.alter_column(table_name, "scope_key", server_default=None)
    _create_postgres_guards(legacy_scope=True)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    _assert_single_legacy_scope(bind)
    config_rewrites = _config_rewrites(bind, restore_scope=False)
    request_rewrites = _request_hash_rewrites(bind, restore_scope=False)
    if dialect == "sqlite":
        _sqlite_upgrade(bind, config_rewrites, request_rewrites)
    else:
        _postgres_upgrade(bind, config_rewrites, request_rewrites)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    config_rewrites = _config_rewrites(bind, restore_scope=True)
    request_rewrites = _request_hash_rewrites(bind, restore_scope=True)
    if dialect == "sqlite":
        _sqlite_downgrade(bind, config_rewrites, request_rewrites)
    else:
        _postgres_downgrade(bind, config_rewrites, request_rewrites)
