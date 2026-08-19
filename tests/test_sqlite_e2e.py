from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError

from rolling_budget_api.core.config import get_settings
from rolling_budget_api.db import RefreshBatch, RuleVersion
from rolling_budget_api.db import session as db_session
from rolling_budget_api.main import create_app
from tests.test_refresh_dashboard_flow import (
    _begin_refresh,
    _commit_payload,
    _create_config,
    _headers,
    _upload_transactions,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MASTER_KEY = "sqlite-master-key-at-least-32-characters"


def _clear_runtime_caches(database_url: str) -> None:
    try:
        db_session.get_engine(database_url).dispose()
    finally:
        db_session._create_session_factory.cache_clear()
        db_session._create_engine.cache_clear()
        get_settings.cache_clear()


def _upgrade_to_head(database_url: str) -> None:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


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
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0001_initial"
        )

    with TestClient(create_app()) as client:
        _create_config(client)

    with engine.connect() as connection:
        rule_id = connection.scalar(select(RuleVersion.id))
    assert rule_id is not None
    with pytest.raises(DBAPIError, match="immutable"):
        with engine.begin() as connection:
            connection.execute(
                update(RuleVersion)
                .where(RuleVersion.id == rule_id)
                .values(lookback_days=60)
            )

    with TestClient(create_app()) as client:
        run_id = _begin_refresh(client, key="sqlite-invalid-batch-counts")
    with pytest.raises(DBAPIError, match="item_count_matches"):
        with engine.begin() as connection:
            connection.execute(
                insert(RefreshBatch).values(
                    run_id=UUID(run_id),
                    batch_index=0,
                    idempotency_key="sqlite-invalid-batch",
                    request_hash="a" * 64,
                    checksum="b" * 64,
                    item_count=3,
                    store_count=1,
                    skip_count=1,
                )
            )


def test_sqlite_full_api_flow_is_atomic_and_preserves_budget_semantics(
    sqlite_database: str,
) -> None:
    with TestClient(create_app()) as client:
        config = _create_config(client)
        assert config["active"]["version"] == 1

        run_id = _begin_refresh(client, key="sqlite-synthetic-full-refresh")
        checksum = _upload_transactions(client, run_id)
        invalid_commit = _commit_payload(checksum)
        invalid_commit["ordered_batch_checksum"] = "0" * 64
        failed = client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=invalid_commit,
        )
        assert failed.status_code == 409
        assert failed.json()["code"] == "checksum_mismatch"

        before_commit = client.get(
            "/v1/dashboard/budgets?as_of=2026-08-19",
            headers=_headers("read"),
        )
        assert before_commit.status_code == 200
        assert all(
            Decimal(item["spent"]) == 0 for item in before_commit.json()["categories"]
        )

        committed = client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=_commit_payload(checksum),
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
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM transactions "
                    "WHERE source_transaction_id = 'synthetic-skipped'"
                )
            )
            == 0
        )


def test_sqlite_data_survives_application_restart(sqlite_database: str) -> None:
    with TestClient(create_app()) as first_client:
        config = _create_config(first_client)
        active_hash = config["active"]["config_hash"]
        run_id = _begin_refresh(first_client, key="sqlite-persistence-refresh")
        checksum = _upload_transactions(first_client, run_id)
        committed = first_client.post(
            f"/v1/refresh-runs/{run_id}/commit",
            headers=_headers("write"),
            json=_commit_payload(checksum),
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
    restored_categories = {
        item["key"]: item for item in restored_dashboard.json()["categories"]
    }
    assert Decimal(restored_categories["restaurant"]["spent"]) == Decimal("70")
    assert Decimal(restored_categories["dating"]["spent"]) == Decimal("50")
