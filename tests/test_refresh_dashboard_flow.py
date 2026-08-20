from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from rolling_budget_api.core.config import get_settings
from rolling_budget_api.db.session import get_engine
from rolling_budget_api.main import create_app


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
        "source_from_date": "2026-07-01",
        "source_to_date": "2026-08-19",
        "expected_accounts": ["synthetic-checking"],
    }


def _transactions() -> list[dict[str, object]]:
    base: dict[str, object] = {
        "account_id": "synthetic-checking",
        "account_name": "Synthetic Checking",
        "currency": "USD",
        "name": "Synthetic transaction",
        "merchant": "Synthetic Merchant",
        "refunded": False,
        "refund_amount": "0",
    }
    return [
        {
            **base,
            "source_id": "synthetic-multilabel-pending",
            "date": "2026-08-19",
            "amount": "50",
            "pending": True,
            "categories": ["restaurant", "dating"],
        },
        {
            **base,
            "source_id": "synthetic-partial-refund",
            "date": "2026-08-18",
            "amount": "30",
            "pending": False,
            "categories": ["restaurant"],
            "refunded": True,
            "refund_amount": "10",
        },
        {
            **base,
            "source_id": "synthetic-outside-window",
            "date": "2026-07-01",
            "amount": "100",
            "pending": False,
            "categories": ["restaurant"],
        },
    ]


def _create_config(client: TestClient, *, restaurant_budget: str = "100") -> dict[str, object]:
    current = client.get("/v1/config", headers=_headers("read"))
    assert current.status_code == 200, current.text
    admin_headers = _headers("admin")
    if current.headers.get("etag") is not None:
        admin_headers["If-Match"] = current.headers["etag"]
    response = client.put(
        "/v1/config",
        headers=admin_headers,
        json=_config_payload(restaurant_budget=restaurant_budget),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _begin_refresh(
    client: TestClient,
    *,
    key: str = "synthetic-full-refresh",
    mode: str = "FULL_REBUILD",
) -> str:
    response = client.post(
        "/v1/refresh-runs",
        headers={**_headers("write"), "Idempotency-Key": key},
        json={**_begin_payload(), "mode": mode},
    )
    assert response.status_code == 201, response.text
    assert all("category_name" not in rule for rule in response.json()["rules"])
    return response.json()["run_id"]


def _upload_transactions(client: TestClient, run_id: str) -> None:
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
        "item_count": 3,
        "replayed": False,
    }


def _commit_payload() -> dict[str, object]:
    return {
        "expected_batch_count": 1,
        "completed_accounts": ["synthetic-checking"],
    }


def _upload_and_commit_one(
    client: TestClient,
    run_id: str,
    *,
    batch_key: str,
    transaction: dict[str, object],
) -> None:
    uploaded = client.put(
        f"/v1/refresh-runs/{run_id}/batches/0",
        headers=_headers("write"),
        json={"idempotency_key": batch_key, "transactions": [transaction]},
    )
    assert uploaded.status_code == 200, uploaded.text
    committed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert committed.status_code == 200, committed.text


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

    _upload_transactions(client, run_id)
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
        json=_commit_payload(),
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "COMMITTED"
    assert committed.json()["receipt"]

    replayed_commit = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert replayed_commit.status_code == 200
    assert replayed_commit.json()["receipt"] == committed.json()["receipt"]

    changed_commit = _commit_payload()
    changed_commit["expected_batch_count"] = 2
    conflicting_commit = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=changed_commit,
    )
    assert conflicting_commit.status_code == 409
    assert conflicting_commit.json()["code"] == "commit_replay_conflict"

    changed_accounts = _commit_payload()
    changed_accounts["completed_accounts"] = ["different-account"]
    conflicting_accounts = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=changed_accounts,
    )
    assert conflicting_accounts.status_code == 409
    assert conflicting_accounts.json()["code"] == "commit_replay_conflict"

    dashboard = client.get(
        "/v1/dashboard/budgets?as_of=2026-08-19",
        headers=_headers("read"),
    )
    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    assert body["as_of"] == "2026-08-19"
    assert body["timezone"] == "America/New_York"
    assert body["freshness"]["status"] == "fresh"
    assert "completeness" not in body["freshness"]
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

    # A classification-rule change creates a pending version and blocks incrementals.
    rule_change = _config_payload(restaurant_budget="60")
    changed_categories = rule_change["categories"]
    assert isinstance(changed_categories, list)
    changed_restaurant = changed_categories[0]
    assert isinstance(changed_restaurant, dict)
    changed_restaurant["classification_instruction"] = "Synthetic meals, takeout, and delivery"
    active_hash = updated["active"]["config_hash"]
    pending = client.put(
        "/v1/config",
        headers={**_headers("admin"), "If-Match": active_hash},
        json=rule_change,
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["active"]["version"] == 1
    assert pending.json()["pending"]["version"] == 2
    assert pending.json()["pending"]["requires_full_rebuild"] is True
    pending_hash = pending.json()["pending"]["config_hash"]
    assert pending.headers["etag"] == f'"{pending_hash}"'

    stale_pending_edit = client.put(
        "/v1/config",
        headers={**_headers("admin"), "If-Match": active_hash},
        json=rule_change,
    )
    assert stale_pending_edit.status_code == 409
    assert stale_pending_edit.json()["code"] == "config_version_conflict"

    replayed_pending = client.put(
        "/v1/config",
        headers={**_headers("admin"), "If-Match": pending.headers["etag"]},
        json=rule_change,
    )
    assert replayed_pending.status_code == 200, replayed_pending.text
    assert replayed_pending.json()["pending"]["config_hash"] == pending_hash

    incremental = {**_begin_payload(), "mode": "INCREMENTAL"}
    blocked_incremental = client.post(
        "/v1/refresh-runs",
        headers={**_headers("write"), "Idempotency-Key": "synthetic-blocked-incremental"},
        json=incremental,
    )
    assert blocked_incremental.status_code == 409
    assert blocked_incremental.json()["code"] == "full_rebuild_required"


def test_pending_display_fields_are_isolated_replaced_and_applied_on_commit(
    client: TestClient,
) -> None:
    initial = _create_config(client)
    initial_active = initial["active"]
    assert isinstance(initial_active, dict)
    initial_hash = initial_active["config_hash"]
    initial_version = initial_active["version"]
    initial_read = client.get("/v1/config", headers=_headers("read"))
    assert initial_read.status_code == 200, initial_read.text
    assert initial_read.headers["etag"] == f'"{initial_hash}"'

    first_pending_payload = _config_payload(restaurant_budget="125")
    first_categories = first_pending_payload["categories"]
    assert isinstance(first_categories, list)
    first_restaurant = first_categories[0]
    assert isinstance(first_restaurant, dict)
    first_restaurant["name"] = "Dining"
    first_restaurant["classification_instruction"] = (
        "Synthetic meals, takeout, and delivery"
    )

    first_pending_response = client.put(
        "/v1/config",
        headers={**_headers("admin"), "If-Match": initial_read.headers["etag"]},
        json=first_pending_payload,
    )
    assert first_pending_response.status_code == 200, first_pending_response.text
    first_view = first_pending_response.json()
    first_active = first_view["active"]
    first_pending = first_view["pending"]
    assert first_active["version"] == initial_version
    assert first_active["categories"][0]["name"] == "Restaurant"
    assert Decimal(first_active["categories"][0]["budget_limit"]) == Decimal("100")
    assert first_active["categories"][0]["budget_currency"] == "USD"
    assert first_pending["categories"][0]["name"] == "Dining"
    assert Decimal(first_pending["categories"][0]["budget_limit"]) == Decimal("125")
    assert first_pending["categories"][0]["budget_currency"] == "USD"
    first_pending_hash = first_pending["config_hash"]
    first_pending_version = first_pending["version"]

    replacement_payload = _config_payload(restaurant_budget="140")
    replacement_payload["display_currency"] = "EUR"
    replacement_categories = replacement_payload["categories"]
    assert isinstance(replacement_categories, list)
    for category in replacement_categories:
        assert isinstance(category, dict)
        category["budget_currency"] = "EUR"
    replacement_restaurant = replacement_categories[0]
    replacement_restaurant["name"] = "European Dining"
    replacement_restaurant["classification_instruction"] = (
        "Synthetic meals, takeout, and delivery"
    )

    replacement_response = client.put(
        "/v1/config",
        headers={**_headers("admin"), "If-Match": str(first_pending_hash)},
        json=replacement_payload,
    )
    assert replacement_response.status_code == 200, replacement_response.text
    replacement_view = replacement_response.json()
    replacement_active = replacement_view["active"]
    replacement_pending = replacement_view["pending"]
    assert replacement_active["display_currency"] == "USD"
    assert replacement_active["categories"][0]["name"] == "Restaurant"
    assert Decimal(replacement_active["categories"][0]["budget_limit"]) == Decimal("100")
    assert replacement_active["categories"][0]["budget_currency"] == "USD"
    assert replacement_pending["version"] > first_pending_version
    assert replacement_pending["config_hash"] != first_pending_hash
    assert replacement_pending["display_currency"] == "EUR"
    assert replacement_pending["categories"][0]["name"] == "European Dining"
    assert Decimal(replacement_pending["categories"][0]["budget_limit"]) == Decimal("140")
    assert replacement_pending["categories"][0]["budget_currency"] == "EUR"

    run_id = _begin_refresh(client, key="synthetic-pending-display-activation")
    committed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json={
            "expected_batch_count": 0,
            "completed_accounts": ["synthetic-checking"],
        },
    )
    assert committed.status_code == 200, committed.text

    activated_response = client.get("/v1/config", headers=_headers("read"))
    assert activated_response.status_code == 200, activated_response.text
    activated_view = activated_response.json()
    assert activated_view["pending"] is None
    activated = activated_view["active"]
    assert activated["version"] == replacement_pending["version"]
    assert activated["display_currency"] == "EUR"
    assert activated["categories"][0]["name"] == "European Dining"
    assert Decimal(activated["categories"][0]["budget_limit"]) == Decimal("140")
    assert activated["categories"][0]["budget_currency"] == "EUR"


def test_missing_final_batch_does_not_publish_partial_data(client: TestClient) -> None:
    _create_config(client)
    run_id = _begin_refresh(client, key="synthetic-integrity-failure")
    _upload_transactions(client, run_id)
    invalid_manifest = _commit_payload()
    invalid_manifest["expected_batch_count"] = 2

    failed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=invalid_manifest,
    )
    assert failed.status_code == 409
    assert failed.json()["code"] == "missing_batch"

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
        json=_commit_payload(),
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "COMMITTED"


def test_empty_full_refresh_can_atomically_clear_the_scope(client: TestClient) -> None:
    _create_config(client)
    populated_run = _begin_refresh(client, key="synthetic-populated-refresh")
    _upload_transactions(client, populated_run)
    populated_commit = client.post(
        f"/v1/refresh-runs/{populated_run}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert populated_commit.status_code == 200, populated_commit.text

    empty_run = _begin_refresh(client, key="synthetic-empty-refresh")
    empty_commit = client.post(
        f"/v1/refresh-runs/{empty_run}/commit",
        headers=_headers("write"),
        json={
            "expected_batch_count": 0,
            "completed_accounts": ["synthetic-checking"],
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


def test_commit_requires_exact_unique_completed_account_set(client: TestClient) -> None:
    _create_config(client)
    begin_payload = {
        **_begin_payload(),
        "expected_accounts": ["synthetic-savings", "synthetic-checking"],
    }
    begin = client.post(
        "/v1/refresh-runs",
        headers={**_headers("write"), "Idempotency-Key": "synthetic-two-accounts"},
        json=begin_payload,
    )
    assert begin.status_code == 201, begin.text
    run_id = begin.json()["run_id"]

    missing_account = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json={
            "expected_batch_count": 0,
            "completed_accounts": ["synthetic-checking"],
        },
    )
    assert missing_account.status_code == 409, missing_account.text
    assert missing_account.json()["code"] == "completed_accounts_mismatch"

    duplicate_account = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json={
            "expected_batch_count": 0,
            "completed_accounts": ["synthetic-checking", "synthetic-checking"],
        },
    )
    assert duplicate_account.status_code == 422, duplicate_account.text

    committed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json={
            "expected_batch_count": 0,
            "completed_accounts": ["synthetic-savings", "synthetic-checking"],
        },
    )
    assert committed.status_code == 200, committed.text


def test_full_rebuild_clears_stale_pending_when_posted_transaction_has_new_id(
    client: TestClient,
) -> None:
    _create_config(client)

    first_run = _begin_refresh(client, key="synthetic-pending-first")
    pending = {
        **_transactions()[0],
        "source_id": "source-pending-id",
        "amount": "40",
        "pending": True,
        "categories": ["restaurant"],
    }
    first_upload = client.put(
        f"/v1/refresh-runs/{first_run}/batches/0",
        headers=_headers("write"),
        json={"idempotency_key": "pending-first-batch", "transactions": [pending]},
    )
    assert first_upload.status_code == 200, first_upload.text
    first_commit = client.post(
        f"/v1/refresh-runs/{first_run}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert first_commit.status_code == 200, first_commit.text

    second_run = _begin_refresh(client, key="synthetic-posted-replacement")
    posted = {
        **pending,
        "source_id": "source-posted-new-id",
        "amount": "45",
        "pending": False,
    }
    second_upload = client.put(
        f"/v1/refresh-runs/{second_run}/batches/0",
        headers=_headers("write"),
        json={"idempotency_key": "posted-second-batch", "transactions": [posted]},
    )
    assert second_upload.status_code == 200, second_upload.text
    second_commit = client.post(
        f"/v1/refresh-runs/{second_run}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert second_commit.status_code == 200, second_commit.text

    with get_engine().connect() as connection:
        source_ids = list(connection.scalars(text("SELECT source_id FROM transactions ORDER BY 1")))
    assert source_ids == ["source-posted-new-id"]

    dashboard = client.get(
        "/v1/dashboard/budgets?as_of=2026-08-19",
        headers=_headers("read"),
    )
    assert dashboard.status_code == 200, dashboard.text
    restaurant = next(
        item for item in dashboard.json()["categories"] if item["key"] == "restaurant"
    )
    assert Decimal(restaurant["spent"]) == Decimal("45")
    assert restaurant["pending_count"] == 0


def test_pending_source_id_is_the_only_cross_id_replacement_signal(
    client: TestClient,
) -> None:
    _create_config(client)
    base = {
        "account_id": "synthetic-checking",
        "account_name": "Synthetic Checking",
        "date": "2026-08-19",
        "amount": "40",
        "currency": "USD",
        "name": "Synthetic pending transition",
        "merchant": "Synthetic Merchant",
        "categories": ["restaurant"],
    }

    initial = _begin_refresh(client, key="pending-link-initial")
    _upload_and_commit_one(
        client,
        initial,
        batch_key="pending-link-initial-batch",
        transaction={**base, "source_id": "pending-old", "pending": True},
    )

    unlinked = _begin_refresh(
        client,
        key="pending-link-unlinked",
        mode="INCREMENTAL",
    )
    _upload_and_commit_one(
        client,
        unlinked,
        batch_key="pending-link-unlinked-batch",
        transaction={**base, "source_id": "posted-unlinked", "pending": False},
    )
    with get_engine().connect() as connection:
        assert set(connection.scalars(text("SELECT source_id FROM transactions"))) == {
            "pending-old",
            "posted-unlinked",
        }

    linked = _begin_refresh(
        client,
        key="pending-link-explicit",
        mode="INCREMENTAL",
    )
    _upload_and_commit_one(
        client,
        linked,
        batch_key="pending-link-explicit-batch",
        transaction={
            **base,
            "source_id": "posted-linked",
            "pending": False,
            "pending_source_id": "pending-old",
        },
    )
    with get_engine().connect() as connection:
        assert set(connection.scalars(text("SELECT source_id FROM transactions"))) == {
            "posted-linked",
            "posted-unlinked",
        }


def test_same_source_id_pending_to_posted_is_an_upsert(client: TestClient) -> None:
    _create_config(client)
    base = {
        "account_id": "synthetic-checking",
        "account_name": "Original Checking Name",
        "source_id": "stable-source-id",
        "date": "2026-08-19",
        "currency": "USD",
        "name": "Stable ID transition",
        "merchant": "Synthetic Merchant",
        "categories": ["restaurant"],
    }

    initial = _begin_refresh(client, key="stable-id-initial")
    _upload_and_commit_one(
        client,
        initial,
        batch_key="stable-id-initial-batch",
        transaction={**base, "amount": "40", "pending": True},
    )
    posted = _begin_refresh(client, key="stable-id-posted", mode="INCREMENTAL")
    _upload_and_commit_one(
        client,
        posted,
        batch_key="stable-id-posted-batch",
        transaction={
            **base,
            "account_name": "Renamed Checking",
            "amount": "45",
            "pending": False,
        },
    )

    with get_engine().connect() as connection:
        rows = (
            connection.execute(
                text("SELECT source_id, account_name, amount, pending FROM transactions")
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["source_id"] == "stable-source-id"
    assert rows[0]["account_name"] == "Renamed Checking"
    assert bool(rows[0]["pending"]) is False


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ({"currency": "JPY"}, "currency_mismatch"),
        ({"date": "2026-08-20"}, "transaction_outside_run_window"),
        ({"date": "2026-06-30"}, "transaction_outside_run_window"),
    ],
)
def test_full_upload_rejects_wrong_currency_and_out_of_window_dates(
    client: TestClient,
    change: dict[str, object],
    expected_code: str,
) -> None:
    _create_config(client)
    run_id = _begin_refresh(client, key=f"upload-boundary-{expected_code}-{change}")
    transaction = {**_transactions()[0], **change}
    response = client.put(
        f"/v1/refresh-runs/{run_id}/batches/0",
        headers=_headers("write"),
        json={"idempotency_key": "upload-boundary-batch", "transactions": [transaction]},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == expected_code


def test_incremental_allows_an_older_pending_or_refund_reconciliation(
    client: TestClient,
) -> None:
    _create_config(client)
    initial = _begin_refresh(client, key="old-reconciliation-initial")
    _upload_and_commit_one(
        client,
        initial,
        batch_key="old-reconciliation-initial-batch",
        transaction=_transactions()[0],
    )

    incremental = _begin_refresh(
        client,
        key="old-reconciliation-incremental",
        mode="INCREMENTAL",
    )
    older = {
        **_transactions()[1],
        "source_id": "older-refund-original",
        "date": "2026-06-01",
    }
    uploaded = client.put(
        f"/v1/refresh-runs/{incremental}/batches/0",
        headers=_headers("write"),
        json={"idempotency_key": "old-reconciliation-batch", "transactions": [older]},
    )
    assert uploaded.status_code == 200, uploaded.text


@pytest.mark.parametrize(
    ("mode", "reverse_order"),
    [
        ("FULL_REBUILD", False),
        ("FULL_REBUILD", True),
        ("INCREMENTAL", False),
        ("INCREMENTAL", True),
    ],
)
def test_staged_pending_target_is_suppressed_regardless_of_mode_or_order(
    client: TestClient,
    mode: str,
    reverse_order: bool,
) -> None:
    _create_config(client)
    pending = {
        **_transactions()[0],
        "source_id": "same-run-pending",
        "pending": True,
        "categories": ["restaurant"],
    }
    if mode == "INCREMENTAL":
        initial = _begin_refresh(client, key=f"replacement-seed-{reverse_order}")
        _upload_and_commit_one(
            client,
            initial,
            batch_key=f"replacement-seed-batch-{reverse_order}",
            transaction=pending,
        )
    posted = {
        **pending,
        "source_id": "same-run-posted",
        "pending": False,
        "pending_source_id": "same-run-pending",
    }
    transactions = [pending, posted]
    if reverse_order:
        transactions.reverse()

    run_id = _begin_refresh(
        client,
        key=f"replacement-{mode}-{reverse_order}",
        mode=mode,
    )
    uploaded = client.put(
        f"/v1/refresh-runs/{run_id}/batches/0",
        headers=_headers("write"),
        json={
            "idempotency_key": f"replacement-batch-{mode}-{reverse_order}",
            "transactions": transactions,
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    committed = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert committed.status_code == 200, committed.text
    with get_engine().connect() as connection:
        source_ids = set(connection.scalars(text("SELECT source_id FROM transactions")))
    assert source_ids == {"same-run-posted"}


def test_duplicate_or_nonpending_replacement_targets_are_rejected(
    client: TestClient,
) -> None:
    _create_config(client)
    posted_target = {
        **_transactions()[0],
        "source_id": "already-posted-target",
        "pending": False,
        "categories": ["restaurant"],
    }
    initial = _begin_refresh(client, key="nonpending-target-initial")
    _upload_and_commit_one(
        client,
        initial,
        batch_key="nonpending-target-initial-batch",
        transaction=posted_target,
    )

    run_id = _begin_refresh(client, key="nonpending-target-link", mode="INCREMENTAL")
    replacement = {
        **posted_target,
        "source_id": "replacement-one",
        "pending_source_id": "already-posted-target",
    }
    uploaded = client.put(
        f"/v1/refresh-runs/{run_id}/batches/0",
        headers=_headers("write"),
        json={"idempotency_key": "nonpending-target-batch", "transactions": [replacement]},
    )
    assert uploaded.status_code == 200, uploaded.text
    rejected = client.post(
        f"/v1/refresh-runs/{run_id}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "pending_replacement_conflict"

    duplicate_run = _begin_refresh(client, key="duplicate-target-link", mode="INCREMENTAL")
    pending = {**posted_target, "source_id": "pending-target", "pending": True}
    duplicate_transactions = [
        pending,
        {
            **replacement,
            "source_id": "duplicate-replacement-one",
            "pending_source_id": "pending-target",
        },
        {
            **replacement,
            "source_id": "duplicate-replacement-two",
            "pending_source_id": "pending-target",
        },
    ]
    duplicate_upload = client.put(
        f"/v1/refresh-runs/{duplicate_run}/batches/0",
        headers=_headers("write"),
        json={
            "idempotency_key": "duplicate-target-batch",
            "transactions": duplicate_transactions,
        },
    )
    assert duplicate_upload.status_code == 200, duplicate_upload.text
    duplicate_commit = client.post(
        f"/v1/refresh-runs/{duplicate_run}/commit",
        headers=_headers("write"),
        json=_commit_payload(),
    )
    assert duplicate_commit.status_code == 409, duplicate_commit.text
    assert duplicate_commit.json()["code"] == "pending_replacement_conflict"
