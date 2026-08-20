import json
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, OperationalError

from rolling_budget_api.db.session import get_engine


@pytest.fixture
def db_connection() -> Iterator[Connection]:
    try:
        connection = get_engine().connect()
    except OperationalError:
        pytest.skip("PostgreSQL is not available")

    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def _seed_config_graph(connection: Connection) -> tuple[UUID, UUID, UUID]:
    category_id = uuid4()
    rule_id = uuid4()
    config_id = uuid4()
    suffix = category_id.hex

    connection.execute(
        text(
            """
            INSERT INTO categories
                (id, key, name, sort_order, budget_limit, budget_currency)
            VALUES
                (:id, :key, 'Synthetic Restaurant', 0, 500, 'USD')
            """
        ),
        {"id": category_id, "key": f"synthetic-{suffix}"},
    )
    connection.execute(
        text(
            """
            INSERT INTO rule_versions
                (id, category_id, version, lookback_days,
                 classification_instruction, is_enabled, rule_hash)
            VALUES
                (:id, :category_id, 1, 30, 'Synthetic classification rule', true, :hash)
            """
        ),
        {"id": rule_id, "category_id": category_id, "hash": "a" * 64},
    )
    connection.execute(
        text(
            """
            INSERT INTO config_versions
                (id, version, status, timezone, display_currency, aggregation_version,
                 config_hash, source_config, activated_at)
            VALUES
                (:id, 1000000 + :ordinal, 'active', 'America/New_York', 'USD', 1,
                 :hash, CAST(:source_config AS jsonb), now())
            """
        ),
        {
            "id": config_id,
            "ordinal": category_id.int % 1_000_000,
            "hash": "b" * 64,
            "source_config": json.dumps({"fixture": "synthetic"}),
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
        {"config_id": config_id, "category_id": category_id, "rule_id": rule_id},
    )
    return category_id, rule_id, config_id


def _seed_refresh_run(connection: Connection, config_id: UUID) -> UUID:
    run_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO refresh_runs
                (id, idempotency_key, request_hash, mode, state, config_version_id,
                 received_batch_count, actual_item_count)
            VALUES
                (:id, :idempotency_key, :request_hash, 'full', 'created', :config_id,
                 0, 0)
            """
        ),
        {
            "id": run_id,
            "idempotency_key": f"synthetic-run-{run_id}",
            "request_hash": "c" * 64,
            "config_id": config_id,
        },
    )
    return run_id


def test_rule_versions_are_immutable_in_the_database(db_connection: Connection) -> None:
    _category_id, rule_id, _config_id = _seed_config_graph(db_connection)

    with pytest.raises(DBAPIError, match="rule_versions are immutable"):
        db_connection.execute(
            text("UPDATE rule_versions SET lookback_days = 60 WHERE id = :id"),
            {"id": rule_id},
        )


def test_refresh_batch_item_count_cannot_be_negative_at_database_boundary(
    db_connection: Connection,
) -> None:
    _category_id, _rule_id, config_id = _seed_config_graph(db_connection)
    run_id = _seed_refresh_run(db_connection, config_id)

    with pytest.raises(DBAPIError, match="item_count_nonnegative"):
        db_connection.execute(
            text(
                """
                INSERT INTO refresh_batches
                    (run_id, batch_index, idempotency_key, request_hash, checksum, item_count)
                VALUES
                    (:run_id, 0, 'synthetic-batch', :hash, :checksum, -1)
                """
            ),
            {"run_id": run_id, "hash": "d" * 64, "checksum": "e" * 64},
        )


def test_refresh_run_sync_revision_is_immutable(db_connection: Connection) -> None:
    _category_id, _rule_id, config_id = _seed_config_graph(db_connection)
    run_id = _seed_refresh_run(db_connection, config_id)

    with pytest.raises(DBAPIError, match="identity/manifest is immutable"):
        db_connection.execute(
            text("UPDATE refresh_runs SET sync_revision_before = 1 WHERE id = :run_id"),
            {"run_id": run_id},
        )


def test_staged_categories_require_a_parent_transaction(
    db_connection: Connection,
) -> None:
    category_id, rule_id, config_id = _seed_config_graph(db_connection)
    run_id = _seed_refresh_run(db_connection, config_id)
    with pytest.raises(DBAPIError):
        db_connection.execute(
            text(
                """
                INSERT INTO staged_transaction_categories
                    (run_id, config_version_id, account_id,
                     source_id, category_id, rule_version_id)
                VALUES
                    (:run_id, :config_id, 'synthetic-checking',
                     'missing-parent-001', :category_id, :rule_id)
                """
            ),
            {
                "run_id": run_id,
                "config_id": config_id,
                "category_id": category_id,
                "rule_id": rule_id,
            },
        )
