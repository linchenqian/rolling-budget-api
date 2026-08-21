"""Repair SQLite category budgets double-scaled by released revision 0003.

Revision ID: 0004_sqlite_money_scale_repair
Revises: 0003_single_user
Create Date: 2026-08-20

The v0.3.1 runtime already stored SQLite money as four-decimal fixed-point
integers even though the physical 0002 columns were declared NUMERIC.  The
released 0003 migration multiplied those raw integers by 10,000 a second time.

This corrective revision is deliberately narrow.  It repairs only a live
category whose raw value exactly matches 10,000 times the fixed-point value in
the active configuration snapshot. Healthy databases and later user edits are
left alone. Because retained pre-0003 transactions may have crossed the same
faulty conversion and cannot be distinguished from healthy post-0003 imports,
any detected repair clears live transaction data and invalidates the singleton
sync state. The next refresh must therefore be a clean FULL_REBUILD.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0004_sqlite_money_scale_repair"
down_revision: str | None = "0003_single_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY_FACTOR = 10_000
_MONEY_QUANTUM = Decimal("0.0001")


def _json_document(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, Mapping) else None


def _fixed_point(value: object) -> int | None:
    try:
        amount = Decimal(str(value)).quantize(_MONEY_QUANTUM)
    except (InvalidOperation, TypeError, ValueError):
        return None
    scaled = amount * _MONEY_FACTOR
    if amount < 0 or scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _active_budget_limits(bind: sa.Connection) -> dict[str, int]:
    source_config = bind.execute(
        sa.text(
            "SELECT source_config FROM config_versions "
            "WHERE status = 'active' ORDER BY version DESC LIMIT 1"
        )
    ).scalar_one_or_none()
    document = _json_document(source_config)
    if document is None:
        return {}
    categories = document.get("categories")
    if not isinstance(categories, list):
        return {}

    budgets: dict[str, int] = {}
    for category in categories:
        if not isinstance(category, Mapping):
            continue
        key = category.get("key")
        budget_limit = _fixed_point(category.get("budget_limit"))
        if isinstance(key, str) and key and budget_limit is not None:
            budgets[key] = budget_limit
    return budgets


def _repair_sqlite_budgets(bind: sa.Connection) -> None:
    expected_budgets = _active_budget_limits(bind)
    if not expected_budgets:
        return

    repaired = False
    rows = list(bind.execute(sa.text("SELECT key, budget_limit FROM categories")).mappings())
    for row in rows:
        expected = expected_budgets.get(row["key"])
        # Zero has the same representation before and after the faulty
        # multiplication, so it cannot prove that this database was affected.
        if expected is None or expected == 0:
            continue
        try:
            current = int(row["budget_limit"])
        except (TypeError, ValueError):
            continue
        if current != expected * _MONEY_FACTOR:
            continue
        bind.execute(
            sa.text("UPDATE categories SET budget_limit = :budget WHERE key = :key"),
            {"budget": expected, "key": row["key"]},
        )
        repaired = True

    if repaired:
        # Existing rows may be old double-scaled data or healthy data imported
        # after 0003. Do not guess: discard the derived live dataset and require
        # an atomic source rebuild from the authoritative financial connector.
        bind.execute(sa.text("DELETE FROM transaction_categories"))
        bind.execute(sa.text("DELETE FROM transactions"))
        bind.execute(sa.text("DELETE FROM sync_states"))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite/Alembic does not assume transactional DDL. Start the write
        # transaction explicitly so the repair, live-data invalidation, and
        # Alembic version-row update commit or roll back together.
        if not bind.connection.driver_connection.in_transaction:
            bind.exec_driver_sql("BEGIN IMMEDIATE")
        _repair_sqlite_budgets(bind)
    elif bind.dialect.name != "postgresql":
        raise RuntimeError(f"Unsupported database dialect: {bind.dialect.name}")


def downgrade() -> None:
    # Corrected monetary data and refresh invalidation are intentionally not
    # reversed. Reintroducing the corruption would be unsafe, and revision 0003
    # can read the repaired fixed-point values.
    dialect = op.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
