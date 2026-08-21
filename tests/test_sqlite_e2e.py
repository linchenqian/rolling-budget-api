import json
from collections.abc import Iterator
from decimal import Decimal
from os import PathLike
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import pyfiles
from fastapi.testclient import TestClient
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from rolling_budget_api.core.config import get_settings
from rolling_budget_api.db import RefreshBatch, RefreshRun, RuleVersion
from rolling_budget_api.db import session as db_session
from rolling_budget_api.main import create_app
from rolling_budget_api.services.hashing import checksum_chain, sha256_hex
from tests.test_refresh_dashboard_flow import (
    _begin_refresh,
    _commit_payload,
    _create_config,
    _headers,
    _upload_transactions,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MASTER_KEY = "sqlite-master-key-at-least-32-characters"
SINGLE_USER_REVISION = "0003_single_user"
CURRENT_REVISION = "0004_sqlite_money_scale_repair"
LEGACY_SCOPE_TABLES = {
    "refresh_runs",
    "staged_transactions",
    "staged_transaction_categories",
    "transactions",
    "transaction_categories",
    "sync_states",
}
REMOVED_CURRENT_COLUMNS = {
    "refresh_runs": {
        "scope_key",
        "account_manifest",
        "expected_source_count",
        "expected_store_count",
        "expected_skip_count",
        "source_complete",
        "input_checksum",
        "cursor_before",
        "cursor_after",
        "actual_source_count",
        "actual_store_count",
        "actual_skip_count",
    },
    "refresh_batches": {"store_count", "skip_count"},
    "staged_transactions": {
        "scope_key",
        "supersedes_source_transaction_id",
        "source_transaction_id",
        "decision",
        "status",
        "description",
    },
    "staged_transaction_categories": {"scope_key", "source_transaction_id"},
    "transactions": {
        "scope_key",
        "supersedes_source_transaction_id",
        "source_transaction_id",
        "status",
        "description",
    },
    "transaction_categories": {"scope_key", "source_transaction_id"},
    "sync_states": {"scope_key", "cursor", "cursor_hash"},
}
LEGACY_BUDGET = Decimal("750.1250")
LEGACY_AMOUNT = Decimal("25.1250")
LEGACY_REFUND_AMOUNT = Decimal("5.0625")
CURRENT_STAGED_AMOUNT = Decimal("12.3456")
CURRENT_STAGED_REFUND_AMOUNT = Decimal("2.3456")
MONEY_FACTOR = 10_000


def _clear_runtime_caches(database_url: str) -> None:
    try:
        db_session.get_engine(database_url).dispose()
    finally:
        db_session._create_session_factory.cache_clear()
        db_session._create_engine.cache_clear()
        get_settings.cache_clear()


def _migration_config(database_url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade_to_head(database_url: str) -> None:
    command.upgrade(_migration_config(database_url), "head")


def _assert_single_user_schema(database_url: str) -> None:
    schema = inspect(db_session.get_engine(database_url))
    for table_name, removed_columns in REMOVED_CURRENT_COLUMNS.items():
        columns = {column["name"] for column in schema.get_columns(table_name)}
        assert columns.isdisjoint(removed_columns), table_name


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _legacy_live_and_sync_snapshot(
    database_url: str,
) -> tuple[dict[str, object], dict[str, object]]:
    with db_session.get_engine(database_url).connect() as connection:
        live = dict(
            connection.execute(
                text(
                    "SELECT account_id, source_id, amount, refund_amount, "
                    "config_version_id, last_refresh_run_id FROM transactions"
                )
            )
            .mappings()
            .one()
        )
        sync = dict(
            connection.execute(
                text("SELECT config_version_id, last_refresh_run_id, revision FROM sync_states")
            )
            .mappings()
            .one()
        )
    return live, sync


def _assert_current_money_storage(database_url: str) -> None:
    with db_session.get_engine(database_url).connect() as connection:
        category = connection.execute(
            text("SELECT budget_limit, typeof(budget_limit) AS storage_type FROM categories")
        ).mappings().one()
        live = connection.execute(
            text(
                "SELECT amount, refund_amount, typeof(amount) AS amount_storage_type, "
                "typeof(refund_amount) AS refund_storage_type FROM transactions "
                "WHERE source_id = 'legacy-live-transaction'"
            )
        ).mappings().one()

    assert category["budget_limit"] == int(LEGACY_BUDGET * MONEY_FACTOR)
    assert category["storage_type"] == "integer"
    assert live["amount"] == int(LEGACY_AMOUNT * MONEY_FACTOR)
    assert live["refund_amount"] == int(LEGACY_REFUND_AMOUNT * MONEY_FACTOR)
    assert live["amount_storage_type"] == "integer"
    assert live["refund_storage_type"] == "integer"


def _assert_legacy_money_storage(database_url: str) -> None:
    with db_session.get_engine(database_url).connect() as connection:
        category = connection.execute(
            text("SELECT budget_limit, typeof(budget_limit) AS storage_type FROM categories")
        ).mappings().one()
        live = connection.execute(
            text(
                "SELECT amount, refund_amount, typeof(amount) AS amount_storage_type, "
                "typeof(refund_amount) AS refund_storage_type FROM transactions "
                "WHERE source_transaction_id = 'legacy-live-transaction'"
            )
        ).mappings().one()

    # Released SQLite databases already used Money's fixed-point integer storage
    # before the single-user migration.  A downgrade must not silently change the
    # representation back to human-unit REAL values.
    assert category["budget_limit"] == int(LEGACY_BUDGET * MONEY_FACTOR)
    assert live["amount"] == int(LEGACY_AMOUNT * MONEY_FACTOR)
    assert live["refund_amount"] == int(LEGACY_REFUND_AMOUNT * MONEY_FACTOR)
    assert category["storage_type"] == "integer"
    assert live["amount_storage_type"] == "integer"
    assert live["refund_storage_type"] == "integer"


def _seed_current_staged_money(database_url: str, identifiers: dict[str, str]) -> str:
    run_id = "10000000000000000000000000000006"
    with db_session.get_engine(database_url).begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO refresh_runs
                    (id, idempotency_key, request_hash, mode, state, config_version_id,
                     source_from_date, source_to_date, expected_accounts,
                     received_batch_count, actual_item_count)
                VALUES
                    (:id, 'current-staged-money-run', :request_hash, 'incremental',
                     'created', :config_id, '2026-08-19', '2026-08-19',
                     :expected_accounts, 0, 0)
                """
            ),
            {
                "id": run_id,
                "request_hash": "4" * 64,
                "config_id": identifiers["config"],
                "expected_accounts": json.dumps(["legacy-card"]),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO refresh_batches
                    (run_id, batch_index, idempotency_key, request_hash,
                     checksum, item_count)
                VALUES
                    (:run_id, 0, 'current-staged-money-batch', :request_hash,
                     :checksum, 1)
                """
            ),
            {
                "run_id": run_id,
                "request_hash": "5" * 64,
                "checksum": "6" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO staged_transactions
                    (run_id, config_version_id, account_id, source_id,
                     batch_index, transaction_date, amount, currency, pending,
                     refunded, refund_amount, source_hash)
                VALUES
                    (:run_id, :config_id, 'legacy-card', 'current-staged-money',
                     0, '2026-08-19', :amount, 'USD', 0, 1, :refund_amount,
                     :source_hash)
                """
            ),
            {
                "run_id": run_id,
                "config_id": identifiers["config"],
                "amount": int(CURRENT_STAGED_AMOUNT * MONEY_FACTOR),
                "refund_amount": int(CURRENT_STAGED_REFUND_AMOUNT * MONEY_FACTOR),
                "source_hash": "7" * 64,
            },
        )
    return run_id


def _assert_raw_sql_check_rejects(
    engine: Engine,
    *,
    contract: str,
    statement: str,
    parameters: dict[str, object],
) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(text(statement), parameters)
    except DBAPIError as exc:
        assert "CHECK constraint failed" in str(exc), contract
        return
    pytest.fail(f"SQLite accepted an invalid {contract} value")


def _seed_legacy_0002_database(database_url: str) -> dict[str, str]:
    identifiers = {
        "category": "10000000000000000000000000000001",
        "rule": "10000000000000000000000000000002",
        "config": "10000000000000000000000000000003",
        "committed_run": "10000000000000000000000000000004",
        "open_run": "10000000000000000000000000000005",
    }
    empty_checksum = checksum_chain([])
    legacy_source_config = json.dumps(
        {
            "timezone": "America/New_York",
            "display_currency": "USD",
            "aggregation_version": 1,
            "scope_key": "legacy-personal",
            "account_ids": ["legacy-checking", "legacy-card"],
            "categories": [],
        }
    )
    engine = db_session.get_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO categories
                    (id, key, name, sort_order, budget_limit, budget_currency)
                VALUES
                    (:id, 'restaurant', 'Restaurant', 0, :budget_limit, 'USD')
                """
            ),
            {
                "id": identifiers["category"],
                "budget_limit": int(LEGACY_BUDGET * MONEY_FACTOR),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO rule_versions
                    (id, category_id, version, lookback_days,
                     classification_instruction, is_enabled, rule_hash)
                VALUES
                    (:id, :category_id, 1, 30, 'Legacy restaurant rule', 1, :hash)
                """
            ),
            {
                "id": identifiers["rule"],
                "category_id": identifiers["category"],
                "hash": sha256_hex(
                    {
                        "category_key": "restaurant",
                        "lookback_days": 30,
                        "classification_instruction": "Legacy restaurant rule",
                        "enabled": True,
                    }
                ),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO config_versions
                    (id, version, status, timezone, display_currency, aggregation_version,
                     config_hash, source_config, activated_at)
                VALUES
                    (:id, 1, 'active', 'America/New_York', 'USD', 1,
                     :hash, :source_config, CURRENT_TIMESTAMP)
                """
            ),
            {
                "id": identifiers["config"],
                "hash": "b" * 64,
                "source_config": legacy_source_config,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO config_version_rules
                    (config_version_id, category_id, rule_version_id)
                VALUES
                    (:config_id, :category_id, :rule_id)
                """
            ),
            {
                "config_id": identifiers["config"],
                "category_id": identifiers["category"],
                "rule_id": identifiers["rule"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO refresh_runs
                    (id, idempotency_key, request_hash, mode, state, config_version_id,
                     scope_key, source_from_date, source_to_date, expected_accounts,
                     account_manifest, expected_batch_count, expected_source_count,
                     expected_store_count, expected_skip_count, source_complete,
                     input_checksum, computed_checksum, cursor_before, cursor_after,
                     received_batch_count, actual_source_count, actual_store_count,
                     actual_skip_count, validated_at, committed_at)
                VALUES
                    (:id, 'legacy-committed-run', :request_hash, 'full', 'committed',
                     :config_id, 'legacy-personal', '2026-07-21', '2026-08-19',
                     :expected_accounts, :account_manifest, 0, 0, 0, 0, 1,
                     :checksum, :checksum, '{}', '{}', 0, 0, 0, 0,
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ),
            {
                "id": identifiers["committed_run"],
                "request_hash": "c" * 64,
                "config_id": identifiers["config"],
                "expected_accounts": json.dumps(["legacy-checking", "legacy-card"]),
                "account_manifest": json.dumps(
                    [
                        {
                            "account_id": "legacy-checking",
                            "pages_complete": True,
                            "observed_count": 0,
                            "source_reported_count": 0,
                        },
                        {
                            "account_id": "legacy-card",
                            "pages_complete": True,
                            "observed_count": 0,
                            "source_reported_count": 0,
                        },
                    ]
                ),
                "checksum": empty_checksum,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO transactions
                    (scope_key, account_id, source_transaction_id, transaction_date,
                     amount, currency, status, refunded, refund_amount, source_hash,
                     config_version_id, first_refresh_run_id, last_refresh_run_id)
                VALUES
                    ('legacy-personal', 'legacy-checking', 'legacy-live-transaction',
                     '2026-08-19', :amount, 'USD', 'posted', 1, :refund_amount, :source_hash,
                     :config_id, :run_id, :run_id)
                """
            ),
            {
                "amount": int(LEGACY_AMOUNT * MONEY_FACTOR),
                "refund_amount": int(LEGACY_REFUND_AMOUNT * MONEY_FACTOR),
                "source_hash": "d" * 64,
                "config_id": identifiers["config"],
                "run_id": identifiers["committed_run"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO transaction_categories
                    (scope_key, account_id, source_transaction_id, category_id,
                     config_version_id, rule_version_id)
                VALUES
                    ('legacy-personal', 'legacy-checking', 'legacy-live-transaction',
                     :category_id, :config_id, :rule_id)
                """
            ),
            {
                "category_id": identifiers["category"],
                "config_id": identifiers["config"],
                "rule_id": identifiers["rule"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO sync_states
                    (scope_key, cursor, cursor_hash, config_version_id,
                     last_refresh_run_id, revision)
                VALUES
                    ('legacy-personal', '{}', :cursor_hash, :config_id, :run_id, 1)
                """
            ),
            {
                "cursor_hash": "e" * 64,
                "config_id": identifiers["config"],
                "run_id": identifiers["committed_run"],
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO refresh_runs
                    (id, idempotency_key, request_hash, mode, state, config_version_id,
                     scope_key, source_from_date, source_to_date, expected_accounts,
                     source_complete, cursor_before, received_batch_count,
                     actual_source_count, actual_store_count, actual_skip_count,
                     uploaded_at)
                VALUES
                    (:id, 'legacy-open-run', :request_hash, 'incremental', 'uploaded',
                     :config_id, 'legacy-personal', '2026-08-06', '2026-08-19',
                     :expected_accounts, 0, '{}', 1, 1, 0, 1, CURRENT_TIMESTAMP)
                """
            ),
            {
                "id": identifiers["open_run"],
                "request_hash": "f" * 64,
                "config_id": identifiers["config"],
                "expected_accounts": json.dumps(["legacy-checking", "legacy-card"]),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO refresh_batches
                    (run_id, batch_index, idempotency_key, request_hash, checksum,
                     item_count, store_count, skip_count)
                VALUES
                    (:run_id, 0, 'legacy-open-batch', :request_hash, :checksum, 1, 0, 1)
                """
            ),
            {
                "run_id": identifiers["open_run"],
                "request_hash": "1" * 64,
                "checksum": "2" * 64,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO staged_transactions
                    (run_id, scope_key, config_version_id, account_id,
                     source_transaction_id, batch_index, decision, refunded,
                     refund_amount, source_hash)
                VALUES
                    (:run_id, 'legacy-personal', :config_id, 'legacy-card',
                     'legacy-staged-skip', 0, 'skip', 0, 0, :source_hash)
                """
            ),
            {
                "run_id": identifiers["open_run"],
                "config_id": identifiers["config"],
                "source_hash": "3" * 64,
            },
        )
    return identifiers


def _assert_legacy_data_is_single_user_and_sanitized(
    database_url: str,
    identifiers: dict[str, str],
) -> str:
    _assert_single_user_schema(database_url)
    engine = db_session.get_engine(database_url)
    with engine.connect() as connection:
        source_config = _json_value(
            connection.scalar(text("SELECT source_config FROM config_versions"))
        )
        stored_config_hash = connection.scalar(
            text("SELECT config_hash FROM config_versions WHERE id = :config_id"),
            {"config_id": identifiers["config"]},
        )
        expected_accounts = _json_value(
            connection.scalar(
                text("SELECT expected_accounts FROM refresh_runs WHERE id = :run_id"),
                {"run_id": identifiers["committed_run"]},
            )
        )
        live_account = (
            connection.execute(
                text(
                    "SELECT account_id, account_name FROM transactions "
                    "WHERE source_id = 'legacy-live-transaction'"
                )
            )
            .mappings()
            .one()
        )
        sync_revision_before = connection.scalar(
            text("SELECT sync_revision_before FROM refresh_runs WHERE id = :run_id"),
            {"run_id": identifiers["open_run"]},
        )
        invalidated_run = (
            connection.execute(
                text("SELECT state, failed_at, error_code FROM refresh_runs WHERE id = :run_id"),
                {"run_id": identifiers["open_run"]},
            )
            .mappings()
            .one()
        )
        sync_state_count = connection.scalar(text("SELECT count(*) FROM sync_states"))
        staged_count = connection.scalar(text("SELECT count(*) FROM staged_transactions"))
        legacy_batch_item_count = connection.scalar(
            text("SELECT item_count FROM refresh_batches WHERE run_id = :run_id"),
            {"run_id": identifiers["open_run"]},
        )

    assert isinstance(source_config, dict)
    assert "account_ids" not in source_config
    assert "scope_key" not in source_config
    assert isinstance(stored_config_hash, str)
    assert len(stored_config_hash) == 64
    assert stored_config_hash != "b" * 64
    assert expected_accounts == ["legacy-checking", "legacy-card"]
    assert live_account["account_id"] == "legacy-checking"
    assert live_account["account_name"] is None
    assert sync_revision_before is None
    assert invalidated_run["state"] == "failed"
    assert invalidated_run["failed_at"] is not None
    assert invalidated_run["error_code"] == "upgrade_invalidated"
    assert sync_state_count == 1
    assert staged_count == 0
    assert legacy_batch_item_count == 0
    return stored_config_hash


def test_failed_single_user_rebuild_rolls_back_legacy_schema_and_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-failed-upgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("API_KEY", MASTER_KEY)
    monkeypatch.delenv("BUDGET_READ_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_WRITE_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_ADMIN_API_KEY", raising=False)
    _clear_runtime_caches(database_url)
    migration_config = _migration_config(database_url)
    command.upgrade(migration_config, "0002_oauth")
    identifiers = _seed_legacy_0002_database(database_url)

    class InjectedMigrationFailure(RuntimeError):
        pass

    original_load_module = pyfiles.load_module_py

    def load_module_with_failure(
        module_id: str,
        path: str | PathLike[str],
    ) -> ModuleType:
        module = original_load_module(module_id, path)
        if Path(path).name == "0003_single_user.py":

            def fail_after_table_copy(*_args: object, **_kwargs: object) -> None:
                raise InjectedMigrationFailure("synthetic failure after legacy table copy")

            module._apply_config_rewrites = fail_after_table_copy
        return module

    monkeypatch.setattr(pyfiles, "load_module_py", load_module_with_failure)
    with pytest.raises(InjectedMigrationFailure, match="after legacy table copy"):
        command.upgrade(migration_config, SINGLE_USER_REVISION)

    engine = db_session.get_engine(database_url)
    legacy_schema = inspect(engine)
    for table_name in LEGACY_SCOPE_TABLES:
        columns = {column["name"] for column in legacy_schema.get_columns(table_name)}
        assert "scope_key" in columns, table_name

    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        source_config = _json_value(
            connection.scalar(
                text("SELECT source_config FROM config_versions WHERE id = :config_id"),
                {"config_id": identifiers["config"]},
            )
        )
        stored_config_hash = connection.scalar(
            text("SELECT config_hash FROM config_versions WHERE id = :config_id"),
            {"config_id": identifiers["config"]},
        )
        row_counts = {
            table_name: connection.scalar(text(f"SELECT count(*) FROM {table_name}"))
            for table_name in {
                "refresh_runs",
                "refresh_batches",
                "staged_transactions",
                "transactions",
                "transaction_categories",
                "sync_states",
            }
        }
        open_run = (
            connection.execute(
                text(
                    "SELECT state, received_batch_count, actual_source_count, "
                    "actual_store_count, actual_skip_count FROM refresh_runs WHERE id = :run_id"
                ),
                {"run_id": identifiers["open_run"]},
            )
            .mappings()
            .one()
        )
        live_account = connection.scalar(
            text(
                "SELECT account_id FROM transactions "
                "WHERE source_transaction_id = 'legacy-live-transaction'"
            )
        )
        sync_revision = connection.scalar(
            text("SELECT revision FROM sync_states WHERE scope_key = 'legacy-personal'")
        )
        persistent_temp_tables = set(
            connection.scalars(
                text("SELECT name FROM sqlite_master WHERE name LIKE '_single_user_%'")
            )
        )
        connection_temp_tables = set(
            connection.scalars(
                text("SELECT name FROM sqlite_temp_master WHERE name LIKE '_single_user_%'")
            )
        )
        triggers = set(
            connection.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        )

    assert version == "0002_oauth"
    assert isinstance(source_config, dict)
    assert source_config["scope_key"] == "legacy-personal"
    assert source_config["account_ids"] == ["legacy-checking", "legacy-card"]
    assert stored_config_hash == "b" * 64
    assert row_counts == {
        "refresh_runs": 2,
        "refresh_batches": 1,
        "staged_transactions": 1,
        "transactions": 1,
        "transaction_categories": 1,
        "sync_states": 1,
    }
    assert dict(open_run) == {
        "state": "uploaded",
        "received_batch_count": 1,
        "actual_source_count": 1,
        "actual_store_count": 0,
        "actual_skip_count": 1,
    }
    assert live_account == "legacy-checking"
    assert sync_revision == 1
    assert persistent_temp_tables == set()
    assert connection_temp_tables == set()
    assert {
        "trg_config_versions_content_immutable",
        "trg_refresh_runs_no_delete",
        "trg_refresh_batches_immutable_delete",
    } <= triggers


def test_legacy_upgrade_preserves_all_sqlite_enum_and_boolean_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-check-contracts.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("API_KEY", MASTER_KEY)
    monkeypatch.delenv("BUDGET_READ_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_WRITE_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_ADMIN_API_KEY", raising=False)
    _clear_runtime_caches(database_url)
    migration_config = _migration_config(database_url)
    command.upgrade(migration_config, "0002_oauth")
    identifiers = _seed_legacy_0002_database(database_url)
    _clear_runtime_caches(database_url)
    command.upgrade(migration_config, SINGLE_USER_REVISION)
    _clear_runtime_caches(database_url)

    engine = db_session.get_engine(database_url)
    extra_category_id = "30000000000000000000000000000001"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO categories "
                "(id, key, name, sort_order, budget_limit, budget_currency) "
                "VALUES (:id, 'invalid-boundary-fixture', 'Boundary fixture', 99, 0, 'USD')"
            ),
            {"id": extra_category_id},
        )

    _assert_raw_sql_check_rejects(
        engine,
        contract="rule_versions.is_enabled boolean",
        statement="""
            INSERT INTO rule_versions
                (id, category_id, version, lookback_days,
                 classification_instruction, is_enabled, rule_hash)
            VALUES
                (:id, :category_id, 1, 30, 'Invalid boolean fixture', 2, :rule_hash)
        """,
        parameters={
            "id": "30000000000000000000000000000002",
            "category_id": extra_category_id,
            "rule_hash": "4" * 64,
        },
    )
    _assert_raw_sql_check_rejects(
        engine,
        contract="config_versions.status enum",
        statement="""
            INSERT INTO config_versions
                (id, version, status, timezone, display_currency, aggregation_version,
                 config_hash, source_config)
            VALUES
                (:id, 99, 'invalid', 'America/New_York', 'USD', 1,
                 :config_hash, '{}')
        """,
        parameters={
            "id": "30000000000000000000000000000003",
            "config_hash": "5" * 64,
        },
    )

    refresh_insert = """
        INSERT INTO refresh_runs
            (id, idempotency_key, request_hash, mode, state, config_version_id,
             source_from_date, source_to_date, expected_accounts,
             received_batch_count, actual_item_count)
        VALUES
            (:id, :key, :request_hash, :mode, :state, :config_id,
             '2026-08-01', '2026-08-19', '["legacy-checking"]', 0, 0)
    """
    for contract, identifier, mode, state in (
        (
            "refresh_runs.mode enum",
            "30000000000000000000000000000004",
            "invalid",
            "created",
        ),
        (
            "refresh_runs.state enum",
            "30000000000000000000000000000005",
            "full",
            "invalid",
        ),
    ):
        _assert_raw_sql_check_rejects(
            engine,
            contract=contract,
            statement=refresh_insert,
            parameters={
                "id": identifier,
                "key": f"invalid-{identifier[-2:]}",
                "request_hash": "6" * 64,
                "mode": mode,
                "state": state,
                "config_id": identifiers["config"],
            },
        )

    staged_insert = """
        INSERT INTO staged_transactions
            (run_id, config_version_id, account_id, source_id,
             batch_index, transaction_date, amount, currency, pending, refunded,
             refund_amount, source_hash)
        VALUES
            (:run_id, :config_id, 'legacy-card', :source_id,
             0, '2026-08-19', :amount, 'USD', :pending, :refunded,
             :refund_amount, :source_hash)
    """
    for contract, source_id, pending, refunded, amount, refund_amount in (
        (
            "staged_transactions.pending boolean",
            "invalid-pending",
            2,
            0,
            10,
            0,
        ),
        (
            "staged_transactions.refunded boolean",
            "invalid-refunded",
            0,
            2,
            10,
            5,
        ),
    ):
        _assert_raw_sql_check_rejects(
            engine,
            contract=contract,
            statement=staged_insert,
            parameters={
                "run_id": identifiers["open_run"],
                "config_id": identifiers["config"],
                "source_id": source_id,
                "amount": amount,
                "pending": pending,
                "refunded": refunded,
                "refund_amount": refund_amount,
                "source_hash": "7" * 64,
            },
        )

    _assert_raw_sql_check_rejects(
        engine,
        contract="transactions.pending boolean",
        statement=(
            "UPDATE transactions SET pending = 2 WHERE source_id = 'legacy-live-transaction'"
        ),
        parameters={},
    )
    _assert_raw_sql_check_rejects(
        engine,
        contract="transactions.refunded boolean",
        statement=(
            "UPDATE transactions SET refunded = 2, refund_amount = 5 "
            "WHERE source_id = 'legacy-live-transaction'"
        ),
        parameters={},
    )


def test_legacy_0002_upgrade_downgrade_and_reupgrade_preserve_single_user_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-0002.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("API_KEY", MASTER_KEY)
    monkeypatch.delenv("BUDGET_READ_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_WRITE_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_ADMIN_API_KEY", raising=False)
    _clear_runtime_caches(database_url)
    migration_config = _migration_config(database_url)

    command.upgrade(migration_config, "0002_oauth")
    identifiers = _seed_legacy_0002_database(database_url)
    _assert_legacy_money_storage(database_url)
    _clear_runtime_caches(database_url)

    command.upgrade(migration_config, "head")
    _clear_runtime_caches(database_url)
    _assert_current_money_storage(database_url)
    migrated_config_hash = _assert_legacy_data_is_single_user_and_sanitized(
        database_url,
        identifiers,
    )
    live_before_invalidated_commit, sync_before_invalidated_commit = _legacy_live_and_sync_snapshot(
        database_url
    )
    with TestClient(create_app()) as client:
        config_response = client.get("/v1/config", headers=_headers("read"))
        dashboard_response = client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )
        committed_replay = client.post(
            "/v1/refresh-runs",
            headers={
                **_headers("write"),
                "Idempotency-Key": "legacy-committed-run",
            },
            json={
                "mode": "FULL_REBUILD",
                "source_from_date": "2026-07-21",
                "source_to_date": "2026-08-19",
                "expected_accounts": ["legacy-card", "legacy-checking"],
            },
        )
        open_replay = client.post(
            "/v1/refresh-runs",
            headers={
                **_headers("write"),
                "Idempotency-Key": "legacy-open-run",
            },
            json={
                "mode": "INCREMENTAL",
                "source_from_date": "2026-08-06",
                "source_to_date": "2026-08-19",
                "expected_accounts": ["legacy-checking", "legacy-card"],
            },
        )
        invalidated_commit = client.post(
            f"/v1/refresh-runs/{identifiers['open_run']}/commit",
            headers=_headers("write"),
            json={
                "expected_batch_count": 1,
                "completed_accounts": ["legacy-card", "legacy-checking"],
            },
        )
        same_semantics = client.put(
            "/v1/config",
            headers={
                **_headers("admin"),
                "If-Match": config_response.headers["etag"],
            },
            json={
                "timezone": "America/New_York",
                "display_currency": "USD",
                "aggregation_version": 1,
                "categories": [
                    {
                        "key": "restaurant",
                        "name": "Restaurant",
                        "sort_order": 0,
                        "budget_limit": "750.1250",
                        "budget_currency": "USD",
                        "lookback_days": 30,
                        "classification_instruction": "Legacy restaurant rule",
                        "enabled": True,
                    }
                ],
            },
        )
    assert config_response.status_code == 200, config_response.text
    assert dashboard_response.status_code == 200, dashboard_response.text
    assert committed_replay.status_code == 201, committed_replay.text
    assert open_replay.status_code == 201, open_replay.text
    assert invalidated_commit.status_code == 409, invalidated_commit.text
    assert same_semantics.status_code == 200, same_semantics.text
    assert UUID(committed_replay.json()["run_id"]) == UUID(identifiers["committed_run"])
    assert UUID(open_replay.json()["run_id"]) == UUID(identifiers["open_run"])
    assert open_replay.json()["state"] == "FAILED"
    assert invalidated_commit.json()["code"] == "refresh_run_not_committable"
    public_edit_hash = config_response.json()["active"]["config_hash"]
    assert len(public_edit_hash) == 64
    assert dashboard_response.json()["config_hash"] == public_edit_hash
    assert config_response.headers["etag"] == f'"{public_edit_hash}"'
    assert same_semantics.json()["active"]["config_hash"] == public_edit_hash
    active_categories = {
        category["key"]: category for category in config_response.json()["active"]["categories"]
    }
    dashboard_categories = {
        category["key"]: category for category in dashboard_response.json()["categories"]
    }
    assert Decimal(active_categories["restaurant"]["budget_limit"]) == LEGACY_BUDGET
    assert Decimal(dashboard_categories["restaurant"]["budget"]) == LEGACY_BUDGET
    assert Decimal(dashboard_categories["restaurant"]["spent"]) == (
        LEGACY_AMOUNT - LEGACY_REFUND_AMOUNT
    )
    # The stored hash intentionally covers rescan semantics only; the public edit hash also
    # protects mutable budget and presentation fields from stale concurrent writes.
    assert migrated_config_hash != public_edit_hash
    assert same_semantics.json()["pending"] is None
    live_after_invalidated_commit, sync_after_invalidated_commit = _legacy_live_and_sync_snapshot(
        database_url
    )
    assert live_after_invalidated_commit == live_before_invalidated_commit
    assert sync_after_invalidated_commit == sync_before_invalidated_commit
    with db_session.get_engine(database_url).connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            CURRENT_REVISION
        )

    staged_run_id = _seed_current_staged_money(database_url, identifiers)
    _clear_runtime_caches(database_url)
    command.downgrade(migration_config, "0002_oauth")
    _clear_runtime_caches(database_url)
    _assert_legacy_money_storage(database_url)
    downgraded_schema = inspect(db_session.get_engine(database_url))
    for table_name in LEGACY_SCOPE_TABLES:
        columns = {column["name"] for column in downgraded_schema.get_columns(table_name)}
        assert "scope_key" in columns, table_name
    with db_session.get_engine(database_url).connect() as connection:
        for table_name in {
            "refresh_runs",
            "transactions",
            "transaction_categories",
            "sync_states",
        }:
            restored_scopes = set(
                connection.scalars(text(f"SELECT DISTINCT scope_key FROM {table_name}"))
            )
            assert restored_scopes == {"personal"}, table_name
        assert connection.scalar(text("SELECT count(*) FROM staged_transactions")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_oauth"

        staged = connection.execute(
            text(
                "SELECT amount, refund_amount, "
                "typeof(amount) AS amount_storage_type, "
                "typeof(refund_amount) AS refund_storage_type "
                "FROM staged_transactions "
                "WHERE run_id = :run_id AND source_transaction_id = 'current-staged-money'"
            ),
            {"run_id": staged_run_id},
        ).mappings().one()
        assert staged["amount"] == int(CURRENT_STAGED_AMOUNT * MONEY_FACTOR)
        assert staged["refund_amount"] == int(CURRENT_STAGED_REFUND_AMOUNT * MONEY_FACTOR)
        assert staged["amount_storage_type"] == "integer"
        assert staged["refund_storage_type"] == "integer"

    _clear_runtime_caches(database_url)
    command.upgrade(migration_config, "head")
    _clear_runtime_caches(database_url)
    _assert_legacy_data_is_single_user_and_sanitized(database_url, identifiers)
    _assert_current_money_storage(database_url)
    with db_session.get_engine(database_url).connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM staged_transactions "
                "WHERE source_id = 'current-staged-money'"
            )
        ) == 0


def _prepare_revision_0003_database(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Config:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("API_KEY", MASTER_KEY)
    monkeypatch.delenv("BUDGET_READ_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_WRITE_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_ADMIN_API_KEY", raising=False)
    _clear_runtime_caches(database_url)
    migration_config = _migration_config(database_url)
    command.upgrade(migration_config, SINGLE_USER_REVISION)
    _clear_runtime_caches(database_url)
    return migration_config


def _live_transaction_snapshot(database_url: str) -> dict[str, object]:
    with db_session.get_engine(database_url).connect() as connection:
        transactions = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT account_id, source_id, transaction_date, amount, currency, "
                    "pending, refunded, refund_amount, pending_source_id, source_hash, "
                    "config_version_id, first_refresh_run_id, last_refresh_run_id "
                    "FROM transactions ORDER BY account_id, source_id"
                )
            ).mappings()
        ]
        category_links = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT account_id, source_id, category_id, config_version_id, "
                    "rule_version_id FROM transaction_categories "
                    "ORDER BY account_id, source_id, category_id"
                )
            ).mappings()
        ]
        sync_state = connection.execute(
            text(
                "SELECT id, config_version_id, last_refresh_run_id, revision "
                "FROM sync_states"
            )
        ).mappings().one_or_none()
    return {
        "transactions": transactions,
        "category_links": category_links,
        "sync_state": dict(sync_state) if sync_state is not None else None,
    }


def _create_config_and_committed_refresh() -> None:
    with TestClient(create_app()) as client:
        config = _create_config(client, restaurant_budget="750")
        assert config["active"]["version"] == 1

        run_id = _begin_refresh(client, key="post-0003-money-refresh")
        _upload_transactions(client, run_id)
        committed = client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=_commit_payload(),
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["state"] == "COMMITTED"


def test_money_scale_repair_corrects_only_proven_budgets_and_requires_full_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'released-0003-double-scale.db'}"
    migration_config = _prepare_revision_0003_database(database_url, monkeypatch)

    with TestClient(create_app()) as client:
        _create_config(client, restaurant_budget="750")

    # Reproduce the released v0.3.2 state.  The active snapshot still says $750,
    # while the faulty migration made the ORM/API observe $7,500,000.  The dating
    # value simulates a legitimate later budget-only edit and must not be guessed at.
    with db_session.get_engine(database_url).begin() as connection:
        connection.execute(
            text(
                "UPDATE categories SET budget_limit = :budget "
                "WHERE key = 'restaurant'"
            ),
            {"budget": int(Decimal("750") * MONEY_FACTOR * MONEY_FACTOR)},
        )
        connection.execute(
            text("UPDATE categories SET budget_limit = :budget WHERE key = 'dating'"),
            {"budget": int(Decimal("90") * MONEY_FACTOR)},
        )

    with TestClient(create_app()) as client:
        run_id = _begin_refresh(client, key="post-0003-money-refresh")
        _upload_transactions(client, run_id)
        committed = client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=_commit_payload(),
        )
        assert committed.status_code == 200, committed.text

        before_config = client.get("/v1/config", headers=_headers("read"))
        before_dashboard = client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )

    assert before_config.status_code == 200, before_config.text
    before_categories = {
        item["key"]: item for item in before_config.json()["active"]["categories"]
    }
    assert Decimal(before_categories["restaurant"]["budget_limit"]) == Decimal("7500000")
    assert Decimal(before_categories["dating"]["budget_limit"]) == Decimal("90")
    assert before_dashboard.status_code == 200, before_dashboard.text
    before_dashboard_categories = {
        item["key"]: item for item in before_dashboard.json()["categories"]
    }
    assert Decimal(before_dashboard_categories["restaurant"]["spent"]) == Decimal("70")
    assert _live_transaction_snapshot(database_url)["sync_state"] is not None

    _clear_runtime_caches(database_url)
    command.upgrade(migration_config, "head")
    _clear_runtime_caches(database_url)

    with db_session.get_engine(database_url).connect() as connection:
        raw_budgets = dict(
            connection.execute(
                text("SELECT key, budget_limit FROM categories ORDER BY key")
            ).all()
        )
        assert connection.scalar(text("SELECT count(*) FROM transactions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM transaction_categories")) == 0
        assert connection.scalar(text("SELECT count(*) FROM sync_states")) == 0

    assert raw_budgets["restaurant"] == int(Decimal("750") * MONEY_FACTOR)
    assert raw_budgets["dating"] == int(Decimal("90") * MONEY_FACTOR)

    with TestClient(create_app()) as client:
        repaired_config = client.get("/v1/config", headers=_headers("read"))
        repaired_dashboard = client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )

    assert repaired_config.status_code == 200, repaired_config.text
    repaired_categories = {
        item["key"]: item for item in repaired_config.json()["active"]["categories"]
    }
    assert Decimal(repaired_categories["restaurant"]["budget_limit"]) == Decimal("750")
    assert Decimal(repaired_categories["dating"]["budget_limit"]) == Decimal("90")
    assert repaired_config.json()["active"]["requires_full_rebuild"] is True

    assert repaired_dashboard.status_code == 200, repaired_dashboard.text
    dashboard_body = repaired_dashboard.json()
    assert dashboard_body["full_rebuild_required"] is True
    assert dashboard_body["freshness"]["status"] == "never_refreshed"
    assert all(Decimal(item["spent"]) == 0 for item in dashboard_body["categories"])


def test_money_scale_repair_is_noop_for_healthy_0003_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'healthy-0003-money.db'}"
    migration_config = _prepare_revision_0003_database(database_url, monkeypatch)
    _create_config_and_committed_refresh()

    with db_session.get_engine(database_url).connect() as connection:
        budgets_before = dict(
            connection.execute(
                text("SELECT key, budget_limit FROM categories ORDER BY key")
            ).all()
        )
    live_before = _live_transaction_snapshot(database_url)
    assert live_before["transactions"]
    assert live_before["category_links"]
    assert live_before["sync_state"] is not None

    _clear_runtime_caches(database_url)
    command.upgrade(migration_config, "head")
    _clear_runtime_caches(database_url)

    with db_session.get_engine(database_url).connect() as connection:
        budgets_after = dict(
            connection.execute(
                text("SELECT key, budget_limit FROM categories ORDER BY key")
            ).all()
        )
    assert budgets_after == budgets_before
    assert _live_transaction_snapshot(database_url) == live_before

    with TestClient(create_app()) as client:
        config = client.get("/v1/config", headers=_headers("read"))
        dashboard = client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )

    assert config.status_code == 200, config.text
    categories = {item["key"]: item for item in config.json()["active"]["categories"]}
    assert Decimal(categories["restaurant"]["budget_limit"]) == Decimal("750")
    assert Decimal(categories["dating"]["budget_limit"]) == Decimal("80")
    assert config.json()["active"]["requires_full_rebuild"] is False

    assert dashboard.status_code == 200, dashboard.text
    dashboard_body = dashboard.json()
    dashboard_categories = {item["key"]: item for item in dashboard_body["categories"]}
    assert Decimal(dashboard_categories["restaurant"]["spent"]) == Decimal("70")
    assert dashboard_body["full_rebuild_required"] is False
    assert dashboard_body["freshness"]["status"] == "fresh"


@pytest.fixture
def sqlite_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    database_path = tmp_path / "rolling-budget.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("API_KEY", MASTER_KEY)
    monkeypatch.delenv("BUDGET_READ_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_WRITE_API_KEY", raising=False)
    monkeypatch.delenv("BUDGET_ADMIN_API_KEY", raising=False)
    _clear_runtime_caches(database_url)
    _upgrade_to_head(database_url)
    try:
        yield database_url
    finally:
        _clear_runtime_caches(database_url)


def test_fresh_sqlite_migration_creates_schema_and_integrity_triggers(
    sqlite_database: str,
) -> None:
    engine = db_session.get_engine(sqlite_database)
    expected_tables = {
        "alembic_version",
        "categories",
        "config_version_rules",
        "config_versions",
        "refresh_batches",
        "refresh_runs",
        "rule_versions",
        "staged_transaction_categories",
        "staged_transactions",
        "sync_states",
        "transaction_categories",
        "transactions",
    }

    assert expected_tables <= set(inspect(engine).get_table_names())
    _assert_single_user_schema(sqlite_database)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            CURRENT_REVISION
        )

    with TestClient(create_app()) as client:
        _create_config(client)

    with engine.connect() as connection:
        rule_id = connection.scalar(select(RuleVersion.id))
        source_config_raw = connection.scalar(text("SELECT source_config FROM config_versions"))
    assert rule_id is not None
    assert source_config_raw is not None
    source_config = _json_value(source_config_raw)
    assert isinstance(source_config, dict)
    assert "account_ids" not in source_config
    assert "scope_key" not in source_config
    with pytest.raises(DBAPIError, match="immutable"):
        with engine.begin() as connection:
            connection.execute(
                update(RuleVersion).where(RuleVersion.id == rule_id).values(lookback_days=60)
            )

    with TestClient(create_app()) as client:
        run_id = _begin_refresh(client, key="sqlite-invalid-batch-counts")
    with pytest.raises(DBAPIError, match="immutable"):
        with engine.begin() as connection:
            connection.execute(
                update(RefreshRun)
                .where(RefreshRun.id == UUID(run_id))
                .values(sync_revision_before=1)
            )
    with pytest.raises(DBAPIError, match="item_count_nonnegative"):
        with engine.begin() as connection:
            connection.execute(
                insert(RefreshBatch).values(
                    run_id=UUID(run_id),
                    batch_index=0,
                    idempotency_key="sqlite-invalid-batch",
                    request_hash="a" * 64,
                    checksum="b" * 64,
                    item_count=-1,
                )
            )


def test_sqlite_full_api_flow_is_atomic_and_preserves_budget_semantics(
    sqlite_database: str,
) -> None:
    with TestClient(create_app()) as client:
        config = _create_config(client)
        assert config["active"]["version"] == 1

        run_id = _begin_refresh(client, key="sqlite-synthetic-full-refresh")
        _upload_transactions(client, run_id)
        invalid_commit = _commit_payload()
        invalid_commit["expected_batch_count"] = 2
        failed = client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=invalid_commit,
        )
        assert failed.status_code == 409
        assert failed.json()["code"] == "missing_batch"

        before_commit = client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )
        assert before_commit.status_code == 200
        assert all(Decimal(item["spent"]) == 0 for item in before_commit.json()["categories"])

        committed = client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=_commit_payload(),
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["state"] == "COMMITTED"

        dashboard = client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )
        assert dashboard.status_code == 200, dashboard.text
        categories = {item["key"]: item for item in dashboard.json()["categories"]}
        restaurant = categories["restaurant"]
        dating = categories["dating"]
        assert restaurant["window_start"] == "2026-07-21"
        assert Decimal(restaurant["spent"]) == Decimal("70")
        assert restaurant["transaction_count"] == 2
        assert restaurant["pending_count"] == 1
        assert Decimal(restaurant["pending_amount"]) == Decimal("50")
        assert restaurant["refund_count"] == 1
        assert Decimal(restaurant["refund_amount"]) == Decimal("10")
        assert dating["window_start"] == "2026-07-06"
        assert Decimal(dating["spent"]) == Decimal("50")
        assert dating["transaction_count"] == 1

    with db_session.get_engine(sqlite_database).connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM staged_transactions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM transactions")) == 2
        assert connection.scalar(text("SELECT count(*) FROM transaction_categories")) == 3


def test_sqlite_data_survives_application_restart(sqlite_database: str) -> None:
    with TestClient(create_app()) as first_client:
        config = _create_config(first_client)
        active_hash = config["active"]["config_hash"]
        run_id = _begin_refresh(first_client, key="sqlite-persistence-refresh")
        _upload_transactions(first_client, run_id)
        committed = first_client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=_commit_payload(),
        )
        assert committed.status_code == 200, committed.text
        receipt = committed.json()["receipt"]

    database_path = Path(sqlite_database.removeprefix("sqlite:///"))
    assert database_path.exists()
    assert database_path.stat().st_size > 0
    _clear_runtime_caches(sqlite_database)

    with TestClient(create_app()) as restarted_client:
        restored_config = restarted_client.get(
            "/v1/config",
            headers=_headers("read"),
        )
        restored_run = restarted_client.get(
            f"/v1/refresh-runs/{run_id}",
            headers=_headers("write"),
        )
        restored_dashboard = restarted_client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )

    assert restored_config.status_code == 200
    assert restored_config.json()["active"]["config_hash"] == active_hash
    assert restored_run.status_code == 200
    assert restored_run.json()["state"] == "COMMITTED"
    assert restored_run.json()["receipt"] == receipt
    assert restored_dashboard.status_code == 200
    restored_categories = {item["key"]: item for item in restored_dashboard.json()["categories"]}
    assert Decimal(restored_categories["restaurant"]["spent"]) == Decimal("70")
    assert Decimal(restored_categories["dating"]["spent"]) == Decimal("50")
