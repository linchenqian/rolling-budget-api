from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from rolling_budget_api.core.config import get_settings
from rolling_budget_api.db.session import get_engine
from rolling_budget_api.main import create_app
from rolling_budget_api.services.hashing import checksum_chain


@pytest.fixture
def clean_database() -> Iterator[None]:
    try:
        connection = get_engine().connect()
    except OperationalError:
        pytest.skip("PostgreSQL is not available")

    truncate = text(
        """
        TRUNCATE TABLE
            sync_states,
            transaction_categories,
            transactions,
            staged_transaction_categories,
            staged_transactions,
            refresh_batches,
            refresh_runs,
            config_version_rules,
            config_versions,
            rule_versions,
            categories
        CASCADE
        """
    )
    connection.execute(truncate)
    connection.commit()
    connection.close()
    try:
        yield
    finally:
        with get_engine().begin() as cleanup:
            cleanup.execute(truncate)


@pytest.fixture
def client(clean_database: None) -> Iterator[TestClient]:
    del clean_database
    with TestClient(create_app()) as test_client:
        yield test_client


def _headers(level: str) -> dict[str, str]:
    settings = get_settings()
    token = {
        "read": settings.budget_read_api_key,
        "write": settings.budget_write_api_key,
        "admin": settings.budget_admin_api_key,
    }[level] or settings.api_key
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


def _config_payload(*, restaurant_budget: str = "100") -> dict[str, object]:
    return {
        "timezone": "America/New_York",
        "display_currency": "USD",
        "aggregation_version": 1,
        "scope_key": "synthetic-personal",
        "account_ids": ["synthetic-checking"],
        "categories": [
            {
                "key": "restaurant",
                "name": "Restaurant",
                "icon": "fork-knife",
                "sort_order": 0,
                "budget_limit": restaurant_budget,
                "budget_currency": "USD",
                "lookback_days": 30,
                "classification_instruction": "Synthetic meals and takeout",
                "enabled": True,
            },
            {
                "key": "dating",
                "name": "Dating",
                "icon": "people",
                "sort_order": 1,
                "budget_limit": "80",
                "budget_currency": "USD",
                "lookback_days": 45,
                "classification_instruction": "Synthetic date expenses",
                "enabled": True,
            },
        ],
    }


def _begin_payload() -> dict[str, object]:
    return {
        "mode": "FULL_REBUILD",
        "scope_key": "synthetic-personal",
        "source_from_date": "2026-07-01",
        "source_to_date": "2026-08-19",
        "expected_accounts": ["synthetic-checking"],
        "cursor_before": None,
    }


def _transactions() -> list[dict[str, object]]:
    base: dict[str, object] = {
        "account_id": "synthetic-checking",
        "decision": "STORE",
        "currency": "USD",
        "merchant_name": "Synthetic Merchant",
        "description": "Synthetic fixture only",
        "refunded": False,
        "refund_amount": "0",
        "supersedes_source_transaction_id": None,
    }
    return [
        {
            **base,
            "source_transaction_id": "synthetic-multilabel-pending",
            "transaction_date": "2026-08-19",
            "amount": "50",
            "status": "PENDING",
            "category_keys": ["restaurant", "dating"],
        },
        {
            **base,
            "source_transaction_id": "synthetic-partial-refund",
            "transaction_date": "2026-08-18",
            "amount": "30",
            "status": "POSTED",
            "category_keys": ["restaurant"],
            "refunded": True,
            "refund_amount": "10",
        },
        {
            **base,
            "source_transaction_id": "synthetic-outside-window",
            "transaction_date": "2026-07-01",
            "amount": "100",
            "status": "POSTED",
            "category_keys": ["restaurant"],
        },
        {
            **base,
            "source_transaction_id": "synthetic-skipped",
            "decision": "SKIP",
            "transaction_date": "2026-08-19",
            "amount": "12",
            "status": "POSTED",
            "category_keys": [],
        },
    ]


def _create_config(client: TestClient, *, restaurant_budget: str = "100") -> dict[str, object]:
    response = client.put(
        "/v1/config",
        headers=_headers("admin"),
        json=_config_payload(restaurant_budget=restaurant_budget),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _begin_refresh(client: TestClient, *, key: str = "synthetic-full-refresh") -> str:
    response = client.post(
        "/v1/refresh-runs",
        headers={**_headers("write"), "Idempotency-Key": key},
        json=_begin_payload(),
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def _upload_transactions(client: TestClient, run_id: str) -> str:
    response = client.put(
        f"/v1/refresh-runs/{run_id}/batches/0",
        headers=_headers("write"),
        json={
            "idempotency_key": "synthetic-batch-000",
            "transactions": _transactions(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "run_id": run_id,
        "batch_index": 0,
        "checksum": response.json()["checksum"],
        "item_count": 4,
        "store_count": 3,
        "skip_count": 1,
        "replayed": False,
    }
    return response.json()["checksum"]


def _commit_payload(checksum: str) -> dict[str, object]:
    return {
        "expected_batch_count": 1,
        "expected_item_count": 4,
        "expected_store_count": 3,
        "expected_skip_count": 1,
        "ordered_batch_checksum": checksum_chain([checksum]),
        "accounts": [
            {
                "account_id": "synthetic-checking",
                "pages_complete": True,
                "observed_count": 4,
                "source_reported_count": 4,
            }
        ],
        "cursor_after": {"synthetic_page": 1},
        "source_complete": True,
    }


def test_full_refresh_is_idempotent_and_dashboard_nets_multilabel_refunds(
    client: TestClient,
) -> None:
    config = _create_config(client)
    assert config["active"]["version"] == 1
    assert config["pending"] is None

    run_id = _begin_refresh(client)
    replayed_begin = client.post(
        "/v1/refresh-runs",
        headers={**_headers("write"), "Idempotency-Key": "synthetic-full-refresh"},
        json=_begin_payload(),
    )
    assert replayed_begin.status_code == 201
    assert replayed_begin.json()["run_id"] == run_id

    conflicting_begin = client.post(
        "/v1/refresh-runs",
        headers={**_headers("write"), "Idempotency-Key": "synthetic-full-refresh"},
        json={**_begin_payload(), "source_from_date": "2026-07-02"},
    )
    assert conflicting_begin.status_code == 409
    assert conflicting_begin.json()["code"] == "idempotency_conflict"

    checksum = _upload_transactions(client, run_id)
    replayed_batch = client.put(
        f"/v1/refresh-runs/{run_id}/batches/0",
        headers=_headers("write"),
        json={
            "idempotency_key": "synthetic-batch-000",
            "transactions": _transactions(),
        },
    )
    assert replayed_batch.status_code == 200
    assert replayed_batch.json()["replayed"] is True

    changed = _transactions()
    changed[0] = {**changed[0], "amount": "51"}
    conflicting_batch = client.put(
        f"/v1/refresh-runs/{run_id}/batches/0",
        headers=_headers("write"),
        json={"idempotency_key": "synthetic-batch-000", "transactions": changed},
    )
    assert conflicting_batch.status_code == 409
    assert conflicting_batch.json()["code"] == "batch_content_conflict"

    committed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=_commit_payload(checksum),
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "COMMITTED"
    assert committed.json()["receipt"]

    replayed_commit = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=_commit_payload(checksum),
    )
    assert replayed_commit.status_code == 200
    assert replayed_commit.json()["receipt"] == committed.json()["receipt"]

    changed_commit = _commit_payload(checksum)
    changed_commit["cursor_after"] = {"synthetic_page": 999}
    conflicting_commit = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=changed_commit,
    )
    assert conflicting_commit.status_code == 409
    assert conflicting_commit.json()["code"] == "commit_replay_conflict"

    dashboard = client.get(
        "/v1/dashboard/budgets?as_of=2026-08-19",
        headers=_headers("read"),
    )
    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    assert body["as_of"] == "2026-08-19"
    assert body["timezone"] == "America/New_York"
    assert body["freshness"]["status"] == "fresh"
    assert body["freshness"]["completeness"] == "source_reported"
    categories = {item["key"]: item for item in body["categories"]}

    restaurant = categories["restaurant"]
    assert restaurant["window_start"] == "2026-07-21"
    assert restaurant["window_end"] == "2026-08-19"
    assert Decimal(restaurant["spent"]) == Decimal("70")
    assert restaurant["transaction_count"] == 2
    assert restaurant["pending_count"] == 1
    assert Decimal(restaurant["pending_amount"]) == Decimal("50")
    assert restaurant["refund_count"] == 1
    assert Decimal(restaurant["refund_amount"]) == Decimal("10")

    dating = categories["dating"]
    assert dating["window_start"] == "2026-07-06"
    assert Decimal(dating["spent"]) == Decimal("50")
    assert dating["transaction_count"] == 1
    assert dating["pending_count"] == 1

    # Budget-only edits reuse the active rules and historical classifications.
    updated = _create_config(client, restaurant_budget="60")
    assert updated["active"]["version"] == 1
    assert updated["pending"] is None
    after_budget_edit = client.get(
        "/v1/dashboard/budgets?as_of=2026-08-19",
        headers=_headers("read"),
    ).json()
    updated_restaurant = {item["key"]: item for item in after_budget_edit["categories"]}[
        "restaurant"
    ]
    assert Decimal(updated_restaurant["spent"]) == Decimal("70")
    assert Decimal(updated_restaurant["budget"]) == Decimal("60")
    assert Decimal(updated_restaurant["over"]) == Decimal("10")
    assert updated_restaurant["status"] == "over"

    with get_engine().connect() as connection:
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

    # A classification-rule change creates a pending version and blocks incrementals.
    rule_change = _config_payload(restaurant_budget="60")
    changed_categories = rule_change["categories"]
    assert isinstance(changed_categories, list)
    changed_restaurant = changed_categories[0]
    assert isinstance(changed_restaurant, dict)
    changed_restaurant["classification_instruction"] = "Synthetic meals, takeout, and delivery"
    pending = client.put(
        "/v1/config",
        headers=_headers("admin"),
        json=rule_change,
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["active"]["version"] == 1
    assert pending.json()["pending"]["version"] == 2
    assert pending.json()["pending"]["requires_full_rebuild"] is True

    incremental = {**_begin_payload(), "mode": "INCREMENTAL"}
    incremental["cursor_before"] = {"synthetic_page": 1}
    blocked_incremental = client.post(
        "/v1/refresh-runs",
        headers={**_headers("write"), "Idempotency-Key": "synthetic-blocked-incremental"},
        json=incremental,
    )
    assert blocked_incremental.status_code == 409
    assert blocked_incremental.json()["code"] == "full_rebuild_required"


def test_failed_manifest_commit_does_not_publish_partial_data(client: TestClient) -> None:
    _create_config(client)
    run_id = _begin_refresh(client, key="synthetic-integrity-failure")
    checksum = _upload_transactions(client, run_id)
    invalid_manifest = _commit_payload(checksum)
    invalid_manifest["ordered_batch_checksum"] = "0" * 64

    failed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=invalid_manifest,
    )
    assert failed.status_code == 409
    assert failed.json()["code"] == "checksum_mismatch"

    run = client.get(
        f"/v1/refresh-runs/{run_id}",
        headers=_headers("write"),
    )
    assert run.status_code == 200
    assert run.json()["state"] == "UPLOADED"

    dashboard = client.get(
        "/v1/dashboard/budgets?as_of=2026-08-19",
        headers=_headers("read"),
    )
    assert dashboard.status_code == 200
    assert all(Decimal(item["spent"]) == 0 for item in dashboard.json()["categories"])
    assert dashboard.json()["freshness"]["status"] == "never_refreshed"

    committed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=_commit_payload(checksum),
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "COMMITTED"


def test_empty_full_refresh_can_atomically_clear_the_scope(client: TestClient) -> None:
    _create_config(client)
    populated_run = _begin_refresh(client, key="synthetic-populated-refresh")
    populated_checksum = _upload_transactions(client, populated_run)
    populated_commit = client.post(
        f"/v1/refresh-runs/{populated_run}/commit",
        headers=_headers("write"),
        json=_commit_payload(populated_checksum),
    )
    assert populated_commit.status_code == 200, populated_commit.text

    empty_run = _begin_refresh(client, key="synthetic-empty-refresh")
    empty_commit = client.post(
        f"/v1/refresh-runs/{empty_run}/commit",
        headers=_headers("write"),
        json={
            "expected_batch_count": 0,
            "expected_item_count": 0,
            "expected_store_count": 0,
            "expected_skip_count": 0,
            "ordered_batch_checksum": checksum_chain([]),
            "accounts": [
                {
                    "account_id": "synthetic-checking",
                    "pages_complete": True,
                    "observed_count": 0,
                    "source_reported_count": 0,
                }
            ],
            "cursor_after": {"synthetic_page": 2},
            "source_complete": True,
        },
    )
    assert empty_commit.status_code == 200, empty_commit.text
    assert empty_commit.json()["state"] == "COMMITTED"
    assert empty_commit.json()["item_count"] == 0

    dashboard = client.get(
        "/v1/dashboard/budgets?as_of=2026-08-19",
        headers=_headers("read"),
    )
    assert dashboard.status_code == 200
    assert all(Decimal(item["spent"]) == 0 for item in dashboard.json()["categories"])
    with get_engine().connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM transactions")) == 0
