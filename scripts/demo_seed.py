#!/usr/bin/env python3
"""Upload a deterministic, entirely synthetic dashboard snapshot through the public API."""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE_URL = os.environ.get("DEMO_API_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
AS_OF = "2026-08-19"
ACCOUNT_ID = "demo-checking"


def _request(
    method: str,
    path: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    request = Request(
        f"{API_BASE_URL}{path}",
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read()
            return response.status, json.loads(response_body or b"{}")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach {API_BASE_URL}: {exc.reason}") from exc


def _category(
    *,
    key: str,
    name: str,
    icon: str,
    order: int,
    budget: str,
    days: int,
    instruction: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "icon": icon,
        "sort_order": order,
        "budget_limit": budget,
        "budget_currency": "USD",
        "lookback_days": days,
        "classification_instruction": instruction,
        "enabled": True,
    }


def _stored_transaction(
    transaction_id: str,
    date: str,
    amount: str,
    merchant: str,
    categories: list[str],
    *,
    pending: bool = False,
    refund: str = "0",
) -> dict[str, Any]:
    return {
        "account_id": ACCOUNT_ID,
        "account_name": "Synthetic Demo Checking",
        "source_id": transaction_id,
        "date": date,
        "amount": amount,
        "currency": "USD",
        "categories": categories,
        "pending": pending,
        "pending_source_id": None,
        "name": merchant,
        "merchant": merchant,
        "refunded": refund != "0",
        "refund_amount": refund,
    }


def _transactions() -> list[dict[str, Any]]:
    stored = [
        _stored_transaction(
            "demo-restaurant-001",
            "2026-08-19",
            "64.87",
            "Moonlight Bistro",
            ["restaurant", "dating"],
            pending=True,
        ),
        _stored_transaction(
            "demo-restaurant-002",
            "2026-08-18",
            "10.89",
            "Parkside Gelato",
            ["restaurant", "dating"],
        ),
        _stored_transaction(
            "demo-restaurant-003",
            "2026-08-16",
            "212.12",
            "Riverstone Steakhouse",
            ["restaurant"],
            refund="20.00",
        ),
        _stored_transaction(
            "demo-restaurant-004",
            "2026-08-14",
            "80.00",
            "Cedar Noodle House",
            ["restaurant"],
        ),
        _stored_transaction(
            "demo-restaurant-005",
            "2026-08-12",
            "59.50",
            "Harbor Table",
            ["restaurant"],
        ),
        _stored_transaction(
            "demo-restaurant-006",
            "2026-08-09",
            "42.43",
            "Juniper Kitchen",
            ["restaurant"],
        ),
        _stored_transaction(
            "demo-restaurant-007",
            "2026-08-07",
            "38.14",
            "Sunset Dumpling Co.",
            ["restaurant"],
        ),
        _stored_transaction(
            "demo-grocery-001",
            "2026-08-18",
            "124.68",
            "Neighborhood Market",
            ["groceries"],
        ),
        _stored_transaction(
            "demo-grocery-002",
            "2026-08-15",
            "87.32",
            "Green Basket Foods",
            ["groceries"],
            pending=True,
        ),
        _stored_transaction(
            "demo-grocery-003",
            "2026-08-11",
            "64.00",
            "Orchard Grocery",
            ["groceries"],
        ),
        _stored_transaction(
            "demo-grocery-004",
            "2026-08-06",
            "36.00",
            "Corner Pantry",
            ["groceries"],
        ),
        _stored_transaction(
            "demo-coffee-001",
            "2026-08-19",
            "29.50",
            "Northstar Coffee",
            ["coffee"],
        ),
        _stored_transaction(
            "demo-coffee-002",
            "2026-08-16",
            "26.25",
            "Little Oak Cafe",
            ["coffee"],
        ),
        _stored_transaction(
            "demo-coffee-003",
            "2026-08-12",
            "22.75",
            "Daybreak Roasters",
            ["coffee"],
            pending=True,
        ),
        _stored_transaction(
            "demo-coffee-004",
            "2026-08-06",
            "17.50",
            "Canal Street Espresso",
            ["coffee"],
        ),
        _stored_transaction(
            "demo-entertainment-001",
            "2026-08-17",
            "150.00",
            "Civic Theater",
            ["entertainment"],
        ),
        _stored_transaction(
            "demo-entertainment-002",
            "2026-08-10",
            "64.00",
            "Riverside Cinema",
            ["entertainment"],
        ),
        _stored_transaction(
            "demo-entertainment-003",
            "2026-08-08",
            "51.00",
            "Museum After Dark",
            ["entertainment"],
        ),
    ]
    if len(stored) != 18:
        raise AssertionError(f"Expected 18 synthetic transactions, got {len(stored)}")
    return stored


def main() -> int:
    master_key = os.getenv("API_KEY")
    admin_key = os.getenv("BUDGET_ADMIN_API_KEY") or master_key
    write_key = os.getenv("BUDGET_WRITE_API_KEY") or master_key
    read_key = os.getenv("BUDGET_READ_API_KEY") or master_key
    if not admin_key or not write_key or not read_key:
        raise SystemExit("Set API_KEY, or configure all three BUDGET_*_API_KEY role-specific keys")

    config_payload = {
        "timezone": "America/New_York",
        "display_currency": "USD",
        "aggregation_version": 1,
        "categories": [
            _category(
                key="restaurant",
                name="Restaurant",
                icon="fork-knife",
                order=0,
                budget="750",
                days=30,
                instruction=(
                    "Meals from restaurants, takeout, and delivery. Exclude standalone coffee "
                    "and grocery purchases."
                ),
            ),
            _category(
                key="dating",
                name="Dating",
                icon="users-three",
                order=1,
                budget="500",
                days=45,
                instruction=(
                    "Expenses clearly associated with a date. These may also carry another "
                    "category such as restaurant or entertainment."
                ),
            ),
            _category(
                key="groceries",
                name="Groceries",
                icon="shopping-cart",
                order=2,
                budget="600",
                days=30,
                instruction="Food and household essentials purchased from grocery stores.",
            ),
            _category(
                key="coffee",
                name="Coffee",
                icon="coffee",
                order=3,
                budget="120",
                days=14,
                instruction="Standalone coffee shops, cafes, tea, and related drinks.",
            ),
            _category(
                key="entertainment",
                name="Entertainment",
                icon="film-slate",
                order=4,
                budget="200",
                days=30,
                instruction="Movies, theater, museums, tickets, and recreational events.",
            ),
        ],
    }
    _current_status, current = _request("GET", "/v1/config", token=read_key)
    editing_base = current.get("pending") or current.get("active")
    config_headers = (
        {"If-Match": str(editing_base["config_hash"])}
        if isinstance(editing_base, dict)
        else None
    )
    config_status, config = _request(
        "PUT",
        "/v1/config",
        token=admin_key,
        payload=config_payload,
        headers=config_headers,
    )

    run_key = f"demo-full-{uuid.uuid4()}"
    begin_payload = {
        "mode": "FULL_REBUILD",
        "source_from_date": "2026-07-01",
        "source_to_date": AS_OF,
        "expected_accounts": [ACCOUNT_ID],
    }
    begin_status, run = _request(
        "POST",
        "/v1/refresh-runs",
        token=write_key,
        payload=begin_payload,
        headers={"Idempotency-Key": run_key},
    )
    run_id = run["run_id"]

    transactions = _transactions()
    batches = [transactions[index : index + 25] for index in range(0, len(transactions), 25)]
    for batch_index, batch in enumerate(batches):
        batch_status, _batch_result = _request(
            "PUT",
            f"/v1/refresh-runs/{run_id}/batches/{batch_index}",
            token=write_key,
            payload={
                "idempotency_key": f"{run_key}-batch-{batch_index:03d}",
                "transactions": batch,
            },
        )
        if batch_status != 200:
            raise RuntimeError(f"Unexpected batch status: {batch_status}")

    commit_payload = {
        "expected_batch_count": len(batches),
        "completed_accounts": [ACCOUNT_ID],
    }
    commit_status, receipt = _request(
        "POST",
        f"/v1/refresh-runs/{run_id}/commit",
        token=write_key,
        payload=commit_payload,
    )

    query = urlencode({"as_of": AS_OF})
    dashboard_status, dashboard = _request("GET", f"/v1/dashboard/budgets?{query}", token=read_key)
    print(
        json.dumps(
            {
                "config_http_status": config_status,
                "config_version": config["active"]["version"],
                "begin_http_status": begin_status,
                "commit_http_status": commit_status,
                "dashboard_http_status": dashboard_status,
                "run_id": run_id,
                "receipt": receipt["receipt"],
                "uploaded": {
                    "batches": len(batches),
                    "items": len(transactions),
                },
                "categories": [
                    {
                        "key": category["key"],
                        "spent": category["spent"],
                        "budget": category["budget"],
                        "status": category["status"],
                    }
                    for category in dashboard["categories"]
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"demo seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
