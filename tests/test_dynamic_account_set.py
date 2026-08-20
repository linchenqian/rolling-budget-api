"""Single-user dynamic financial-account set contract tests."""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import select

from rolling_budget_api.db import Base, ConfigVersion, RefreshRun, Transaction
from rolling_budget_api.db import session as db_session
from rolling_budget_api.db.session import session_scope
from rolling_budget_api.schemas.config import CategoryConfigInput, ConfigPutRequest
from rolling_budget_api.schemas.refresh import (
    RefreshBatchRequest,
    RefreshBeginRequest,
    RefreshCommitRequest,
)
from rolling_budget_api.services.config_service import get_config, put_config
from rolling_budget_api.services.errors import ConflictError
from rolling_budget_api.services.refresh_service import (
    begin_refresh,
    commit_refresh,
    upload_batch,
)


@pytest.fixture
def dynamic_accounts_database(tmp_path: Path) -> Iterator[str]:
    database_url = f"sqlite:///{tmp_path / 'dynamic-accounts.db'}"
    engine = db_session.get_engine(database_url)
    Base.metadata.create_all(engine)
    try:
        yield database_url
    finally:
        engine.dispose()
        db_session._create_session_factory.cache_clear()
        db_session._create_engine.cache_clear()


def _config_request() -> ConfigPutRequest:
    return ConfigPutRequest(
        timezone="America/New_York",
        display_currency="USD",
        aggregation_version=1,
        categories=[
            CategoryConfigInput(
                key="restaurant",
                name="Restaurant",
                budget_limit=Decimal("750"),
                budget_currency="USD",
                lookback_days=30,
                classification_instruction="Synthetic restaurant rule",
            )
        ],
    )


def _create_config(database_url: str) -> None:
    with session_scope(database_url) as db:
        put_config(db, _config_request())


def _begin(
    database_url: str,
    *,
    key: str,
    mode: Literal["INCREMENTAL", "FULL_REBUILD"],
    accounts: list[str],
) -> UUID:
    with session_scope(database_url) as db:
        response = begin_refresh(
            db,
            RefreshBeginRequest(
                mode=mode,
                source_from_date=date(2026, 7, 1),
                source_to_date=date(2026, 8, 19),
                expected_accounts=accounts,
            ),
            idempotency_key=key,
            max_batch_items=500,
            max_request_bytes=1_000_000,
        )
    return response.run_id


def _upload_one(database_url: str, run_id: UUID, *, account_id: str) -> None:
    with session_scope(database_url) as db:
        upload_batch(
            db,
            run_id,
            0,
            RefreshBatchRequest.model_validate(
                {
                    "idempotency_key": "dynamic-batch-0000",
                    "transactions": [
                        {
                            "account_id": account_id,
                            "account_name": "GPT Checking",
                            "source_id": "dynamic-restaurant-1",
                            "date": "2026-08-19",
                            "amount": "25",
                            "currency": "USD",
                            "pending": False,
                            "name": "Synthetic fixture",
                            "merchant": "Synthetic Merchant",
                            "categories": ["restaurant"],
                            "refunded": False,
                            "refund_amount": "0",
                        }
                    ],
                }
            ),
            max_batch_items=500,
            max_request_bytes=1_000_000,
        )


def _commit(
    database_url: str,
    run_id: UUID,
    *,
    expected_batch_count: int,
    completed_accounts: list[str],
) -> None:
    with session_scope(database_url) as db:
        response = commit_refresh(
            db,
            run_id,
            RefreshCommitRequest.model_validate(
                {
                    "expected_batch_count": expected_batch_count,
                    "completed_accounts": completed_accounts,
                }
            ),
        )
    assert response.state == "COMMITTED"


def test_single_user_config_contains_rules_but_no_account_or_named_scope(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)

    with session_scope(dynamic_accounts_database) as db:
        view = get_config(db)
        stored = db.scalar(select(ConfigVersion))

    assert view.active is not None
    assert stored is not None
    assert "account_ids" not in view.active.model_dump(mode="json")
    assert "scope_key" not in view.active.model_dump(mode="json")
    assert "account_ids" not in stored.source_config
    assert "scope_key" not in stored.source_config


def test_gpt_account_set_is_locked_per_run_and_manifest_must_match_exactly(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)
    run_id = _begin(
        dynamic_accounts_database,
        key="dynamic-first-full",
        mode="FULL_REBUILD",
        accounts=["gpt-savings", "gpt-checking"],
    )

    with session_scope(dynamic_accounts_database) as db:
        run = db.get(RefreshRun, run_id)
        assert run is not None
        assert run.expected_accounts == ["gpt-checking", "gpt-savings"]

    _upload_one(
        dynamic_accounts_database,
        run_id,
        account_id="gpt-checking",
    )
    with pytest.raises(ConflictError) as mismatch:
        _commit(
            dynamic_accounts_database,
            run_id,
            expected_batch_count=1,
            completed_accounts=["gpt-checking"],
        )
    assert mismatch.value.code == "completed_accounts_mismatch"

    _commit(
        dynamic_accounts_database,
        run_id,
        expected_batch_count=1,
        completed_accounts=["gpt-savings", "gpt-checking"],
    )


def test_account_order_is_irrelevant_to_begin_idempotency(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)
    first = _begin(
        dynamic_accounts_database,
        key="dynamic-order-retry",
        mode="FULL_REBUILD",
        accounts=["gpt-savings", "gpt-checking"],
    )
    replay = _begin(
        dynamic_accounts_database,
        key="dynamic-order-retry",
        mode="FULL_REBUILD",
        accounts=["gpt-checking", "gpt-savings"],
    )

    assert replay == first


def test_changed_gpt_account_set_requires_full_rebuild_and_replaces_live_data(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)
    first_run = _begin(
        dynamic_accounts_database,
        key="dynamic-populated-full",
        mode="FULL_REBUILD",
        accounts=["gpt-checking"],
    )
    _upload_one(
        dynamic_accounts_database,
        first_run,
        account_id="gpt-checking",
    )
    _commit(
        dynamic_accounts_database,
        first_run,
        expected_batch_count=1,
        completed_accounts=["gpt-checking"],
    )

    with pytest.raises(ConflictError) as changed_incremental:
        _begin(
            dynamic_accounts_database,
            key="dynamic-changed-incremental",
            mode="INCREMENTAL",
            accounts=["gpt-credit-card"],
        )
    assert changed_incremental.value.code == "account_scope_changed"

    replacement_run = _begin(
        dynamic_accounts_database,
        key="dynamic-replacement-full",
        mode="FULL_REBUILD",
        accounts=["gpt-credit-card"],
    )
    _commit(
        dynamic_accounts_database,
        replacement_run,
        expected_batch_count=0,
        completed_accounts=["gpt-credit-card"],
    )

    with session_scope(dynamic_accounts_database) as db:
        assert db.scalar(select(Transaction)) is None


def test_full_then_incremental_preserve_gpt_provided_account_identity(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)
    full_run = _begin(
        dynamic_accounts_database,
        key="dynamic-account-full",
        mode="FULL_REBUILD",
        accounts=["gpt-checking"],
    )
    _upload_one(
        dynamic_accounts_database,
        full_run,
        account_id="gpt-checking",
    )
    _commit(
        dynamic_accounts_database,
        full_run,
        expected_batch_count=1,
        completed_accounts=["gpt-checking"],
    )

    incremental_run = _begin(
        dynamic_accounts_database,
        key="dynamic-account-incremental",
        mode="INCREMENTAL",
        accounts=["gpt-checking"],
    )
    _upload_one(
        dynamic_accounts_database,
        incremental_run,
        account_id="gpt-checking",
    )
    _commit(
        dynamic_accounts_database,
        incremental_run,
        expected_batch_count=1,
        completed_accounts=["gpt-checking"],
    )

    with session_scope(dynamic_accounts_database) as db:
        run = db.get(RefreshRun, incremental_run)
        transactions = list(db.scalars(select(Transaction)))

    assert run is not None
    assert run.expected_accounts == ["gpt-checking"]
    assert [transaction.account_id for transaction in transactions] == ["gpt-checking"]


def test_first_refresh_cannot_be_incremental(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)

    with pytest.raises(ConflictError) as first_incremental:
        _begin(
            dynamic_accounts_database,
            key="dynamic-first-incremental",
            mode="INCREMENTAL",
            accounts=["gpt-checking"],
        )

    assert first_incremental.value.code == "full_rebuild_required"


def test_stale_incremental_cannot_commit_after_full_rebuild_changes_accounts(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)
    initial_full = _begin(
        dynamic_accounts_database,
        key="dynamic-race-initial-full",
        mode="FULL_REBUILD",
        accounts=["gpt-checking"],
    )
    _commit(
        dynamic_accounts_database,
        initial_full,
        expected_batch_count=0,
        completed_accounts=["gpt-checking"],
    )

    stale_incremental = _begin(
        dynamic_accounts_database,
        key="dynamic-race-stale-incremental",
        mode="INCREMENTAL",
        accounts=["gpt-checking"],
    )
    replacement_full = _begin(
        dynamic_accounts_database,
        key="dynamic-race-replacement-full",
        mode="FULL_REBUILD",
        accounts=["gpt-credit-card"],
    )
    _commit(
        dynamic_accounts_database,
        replacement_full,
        expected_batch_count=0,
        completed_accounts=["gpt-credit-card"],
    )

    with pytest.raises(ConflictError) as stale_commit:
        _commit(
            dynamic_accounts_database,
            stale_incremental,
            expected_batch_count=0,
            completed_accounts=["gpt-checking"],
        )

    assert stale_commit.value.code == "refresh_run_superseded"


def test_sync_revision_rejects_stale_incremental_when_accounts_are_unchanged(
    dynamic_accounts_database: str,
) -> None:
    _create_config(dynamic_accounts_database)
    initial_full = _begin(
        dynamic_accounts_database,
        key="dynamic-revision-initial-full",
        mode="FULL_REBUILD",
        accounts=["gpt-checking"],
    )
    _commit(
        dynamic_accounts_database,
        initial_full,
        expected_batch_count=0,
        completed_accounts=["gpt-checking"],
    )

    stale_incremental = _begin(
        dynamic_accounts_database,
        key="dynamic-revision-stale-incremental",
        mode="INCREMENTAL",
        accounts=["gpt-checking"],
    )
    newer_incremental = _begin(
        dynamic_accounts_database,
        key="dynamic-revision-newer-incremental",
        mode="INCREMENTAL",
        accounts=["gpt-checking"],
    )
    _commit(
        dynamic_accounts_database,
        newer_incremental,
        expected_batch_count=0,
        completed_accounts=["gpt-checking"],
    )

    with pytest.raises(ConflictError) as stale_commit:
        _commit(
            dynamic_accounts_database,
            stale_incremental,
            expected_batch_count=0,
            completed_accounts=["gpt-checking"],
        )

    assert stale_commit.value.code == "refresh_run_superseded"
