from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from threading import Barrier

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from rolling_budget_api.db import (
    Category,
    ConfigVersion,
    ConfigVersionStatus,
    RefreshRun,
    RefreshRunState,
    RuleVersion,
)
from rolling_budget_api.db.session import get_engine, get_session_factory
from rolling_budget_api.schemas.config import ConfigPutRequest, ConfigView
from rolling_budget_api.schemas.refresh import (
    RefreshBeginRequest,
    RefreshBeginResponse,
    RefreshCommitRequest,
    RefreshRunView,
)
from rolling_budget_api.services.config_service import get_config, put_config
from rolling_budget_api.services.errors import ConflictError
from rolling_budget_api.services.refresh_service import begin_refresh, commit_refresh

_TRUNCATE_DATABASE = text(
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
_CONCURRENCY_TIMEOUT_SECONDS = 10

_ConcurrentValue = ConfigView | RefreshBeginResponse | RefreshRunView


@dataclass(frozen=True)
class _Outcome:
    value: _ConcurrentValue | None = None
    conflict: ConflictError | None = None


@pytest.fixture
def postgres_sessions() -> Iterator[sessionmaker[Session]]:
    """Yield a clean PostgreSQL database, or skip when PostgreSQL is unavailable."""

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency regression requires PostgreSQL")
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(_TRUNCATE_DATABASE)
    except OperationalError:
        pytest.skip("PostgreSQL is not available")

    factory = get_session_factory()
    try:
        yield factory
    finally:
        with engine.begin() as connection:
            connection.execute(_TRUNCATE_DATABASE)


def _config_request(*, lookback_days: int = 30) -> ConfigPutRequest:
    return ConfigPutRequest.model_validate(
        {
            "timezone": "America/New_York",
            "display_currency": "USD",
            "aggregation_version": 1,
            "categories": [
                {
                    "key": "restaurant",
                    "name": "Restaurant",
                    "icon": "fork-knife",
                    "sort_order": 0,
                    "budget_limit": "750",
                    "budget_currency": "USD",
                    "lookback_days": lookback_days,
                    "classification_instruction": "Meals, takeout, coffee, and fast food",
                    "enabled": True,
                }
            ],
        }
    )


def _begin_request() -> RefreshBeginRequest:
    return RefreshBeginRequest(
        mode="FULL_REBUILD",
        source_from_date=date(2026, 1, 1),
        source_to_date=date(2026, 8, 20),
        expected_accounts=["synthetic-account"],
    )


def _run_concurrently(
    factory: sessionmaker[Session],
    *operations: Callable[[Session], _ConcurrentValue],
) -> list[_Outcome]:
    """Start independent DB sessions at one barrier and surface non-domain failures.

    PostgreSQL timeouts turn a lock-order regression into a bounded test failure instead of
    leaving CI hung. Only the application's controlled ConflictError is captured as an outcome;
    IntegrityError, OperationalError, and every other unexpected exception fail the future.
    """

    barrier = Barrier(len(operations), timeout=_CONCURRENCY_TIMEOUT_SECONDS)

    def invoke(operation: Callable[[Session], _ConcurrentValue]) -> _Outcome:
        with factory() as db:
            # SET LOCAL starts a transaction on this otherwise independent session. Both
            # settings automatically disappear on commit/rollback.
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            db.execute(text("SET LOCAL lock_timeout = '10s'"))
            barrier.wait()
            try:
                return _Outcome(value=operation(db))
            except ConflictError as exc:
                db.rollback()
                return _Outcome(conflict=exc)

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [
            future.result(timeout=_CONCURRENCY_TIMEOUT_SECONDS + 5) for future in futures
        ]


def _seed_active(factory: sessionmaker[Session]) -> ConfigView:
    with factory() as db:
        return put_config(db, _config_request(), if_match=None)


def test_concurrent_first_config_writes_create_one_active_graph(
    postgres_sessions: sessionmaker[Session],
) -> None:
    request = _config_request()
    outcomes = _run_concurrently(
        postgres_sessions,
        lambda db: put_config(db, request, if_match=None),
        lambda db: put_config(db, request, if_match=None),
    )

    successes = [outcome.value for outcome in outcomes if outcome.value is not None]
    conflicts = [outcome.conflict for outcome in outcomes if outcome.conflict is not None]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0] is not None
    assert conflicts[0].code == "config_version_required"

    with postgres_sessions() as db:
        assert db.scalar(select(func.count()).select_from(ConfigVersion)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(ConfigVersion)
                .where(ConfigVersion.status == ConfigVersionStatus.ACTIVE)
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(Category)) == 1
        assert db.scalar(select(func.count()).select_from(RuleVersion)) == 1


def test_concurrent_begin_refresh_replays_one_idempotent_run(
    postgres_sessions: sessionmaker[Session],
) -> None:
    _seed_active(postgres_sessions)
    request = _begin_request()
    key = "postgres-concurrent-begin"

    def begin(db: Session) -> RefreshBeginResponse:
        return begin_refresh(
            db,
            request,
            idempotency_key=key,
            max_batch_items=1000,
            max_request_bytes=1_000_000,
        )

    outcomes = _run_concurrently(postgres_sessions, begin, begin)
    successes = [outcome.value for outcome in outcomes if outcome.value is not None]
    conflicts = [outcome.conflict for outcome in outcomes if outcome.conflict is not None]

    # The exclusive config-state lock serializes creation with the idempotent replay lookup.
    # Both callers must therefore receive the one durable run rather than surfacing a race.
    assert len(successes) == 2
    assert not conflicts
    assert all(isinstance(response, RefreshBeginResponse) for response in successes)
    run_ids = {
        response.run_id for response in successes if isinstance(response, RefreshBeginResponse)
    }
    assert len(run_ids) == 1
    assert all(conflict is not None and conflict.status_code == 409 for conflict in conflicts)

    with postgres_sessions() as db:
        runs = list(db.scalars(select(RefreshRun).where(RefreshRun.idempotency_key == key)))
        assert len(runs) == 1
        assert runs[0].id in run_ids


def test_pending_config_put_racing_full_commit_has_consistent_winner(
    postgres_sessions: sessionmaker[Session],
) -> None:
    initial = _seed_active(postgres_sessions)
    assert initial.active is not None
    with postgres_sessions() as db:
        pending_view = put_config(
            db,
            _config_request(lookback_days=31),
            if_match=initial.active.config_hash,
        )
    assert pending_view.pending is not None

    with postgres_sessions() as db:
        run = begin_refresh(
            db,
            _begin_request(),
            idempotency_key="postgres-pending-commit-race",
            max_batch_items=1000,
            max_request_bytes=1_000_000,
        )

    commit_request = RefreshCommitRequest(
        expected_batch_count=0,
        completed_accounts=["synthetic-account"],
    )
    pending_hash = pending_view.pending.config_hash

    def commit(db: Session) -> RefreshRunView:
        return commit_refresh(db, run.run_id, commit_request)

    def replace_pending(db: Session) -> ConfigView:
        return put_config(
            db,
            _config_request(lookback_days=32),
            if_match=pending_hash,
        )

    commit_outcome, config_outcome = _run_concurrently(
        postgres_sessions,
        commit,
        replace_pending,
    )
    assert isinstance(config_outcome.value, ConfigView)
    assert config_outcome.conflict is None
    if commit_outcome.conflict is not None:
        assert commit_outcome.conflict.code == "config_version_conflict"
    else:
        assert isinstance(commit_outcome.value, RefreshRunView)
        assert commit_outcome.value.state == "COMMITTED"

    with postgres_sessions() as db:
        final = get_config(db)
        refresh_run = db.get(RefreshRun, run.run_id)
        assert final.active is not None
        assert final.pending is not None
        assert refresh_run is not None
        assert (
            db.scalar(
                select(func.count())
                .select_from(ConfigVersion)
                .where(ConfigVersion.status == ConfigVersionStatus.ACTIVE)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ConfigVersion)
                .where(ConfigVersion.status == ConfigVersionStatus.PENDING)
            )
            == 1
        )

        active_lookback = final.active.categories[0].lookback_days
        pending_lookback = final.pending.categories[0].lookback_days
        assert pending_lookback == 32
        if commit_outcome.value is not None:
            assert active_lookback == 31
            assert refresh_run.state == RefreshRunState.COMMITTED
        else:
            assert active_lookback == 30
            assert refresh_run.state == RefreshRunState.CREATED
