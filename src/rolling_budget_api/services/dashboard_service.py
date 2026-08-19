from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from rolling_budget_api.db import (
    ConfigVersion,
    ConfigVersionStatus,
    RefreshRun,
    SyncState,
    Transaction,
    TransactionCategory,
    TransactionStatus,
)
from rolling_budget_api.schemas.dashboard import (
    DashboardCategory,
    DashboardFreshness,
    DashboardResponse,
)
from rolling_budget_api.services.config_service import rules_for_config
from rolling_budget_api.services.errors import ConflictError


def get_dashboard(
    db: Session,
    *,
    as_of: date | None,
    stale_after_hours: int,
) -> DashboardResponse:
    active = db.scalar(
        select(ConfigVersion).where(ConfigVersion.status == ConfigVersionStatus.ACTIVE)
    )
    if active is None:
        raise ConflictError("Create a configuration first", code="config_required")
    pending_config = db.scalar(
        select(ConfigVersion)
        .where(ConfigVersion.status == ConfigVersionStatus.PENDING)
        .order_by(ConfigVersion.version.desc())
    )
    local_today = datetime.now(ZoneInfo(active.timezone)).date()
    window_end = as_of or local_today
    scope_key = str(active.source_config.get("scope_key", "personal"))

    categories: list[DashboardCategory] = []
    for _link, rule, category in rules_for_config(db, active.id):
        if not rule.is_enabled:
            continue
        window_start = window_end - timedelta(days=rule.lookback_days - 1)
        rows = list(
            db.scalars(
                select(Transaction)
                .join(
                    TransactionCategory,
                    (TransactionCategory.scope_key == Transaction.scope_key)
                    & (TransactionCategory.account_id == Transaction.account_id)
                    & (
                        TransactionCategory.source_transaction_id
                        == Transaction.source_transaction_id
                    ),
                )
                .where(
                    Transaction.scope_key == scope_key,
                    Transaction.config_version_id == active.id,
                    TransactionCategory.category_id == category.id,
                    Transaction.transaction_date >= window_start,
                    Transaction.transaction_date <= window_end,
                )
            )
        )
        spent = sum((item.amount - item.refund_amount for item in rows), Decimal("0"))
        pending_rows = [item for item in rows if item.status == TransactionStatus.PENDING]
        pending_amount = sum(
            (item.amount - item.refund_amount for item in pending_rows), Decimal("0")
        )
        refunds = [item for item in rows if item.refunded]
        refund_amount = sum((item.refund_amount for item in refunds), Decimal("0"))
        remaining = max(category.budget_limit - spent, Decimal("0"))
        over = max(spent - category.budget_limit, Decimal("0"))
        if category.budget_limit > 0:
            progress = spent / category.budget_limit
        else:
            progress = Decimal("1") if spent > 0 else Decimal("0")
        if over > 0:
            status = "over"
        elif progress >= Decimal("0.8"):
            status = "near_limit"
        else:
            status = "ok"
        categories.append(
            DashboardCategory(
                key=category.key,
                name=category.name,
                icon=category.icon,
                sort_order=category.sort_order,
                window_days=rule.lookback_days,
                window_start=window_start,
                window_end=window_end,
                currency=category.budget_currency,
                spent=spent,
                budget=category.budget_limit,
                remaining=remaining,
                over=over,
                progress=progress,
                status=status,
                transaction_count=len(rows),
                pending_count=len(pending_rows),
                pending_amount=pending_amount,
                refund_count=len(refunds),
                refund_amount=refund_amount,
            )
        )

    sync_state = db.get(SyncState, scope_key)
    last_refresh = sync_state.updated_at if sync_state is not None else None
    if last_refresh is None:
        freshness_status = "never_refreshed"
    else:
        if last_refresh.tzinfo is None:
            last_refresh = last_refresh.replace(tzinfo=UTC)
        age = datetime.now(UTC) - last_refresh
        freshness_status = "stale" if age > timedelta(hours=stale_after_hours) else "fresh"
    completeness = "none"
    if sync_state is not None:
        last_run = db.get(RefreshRun, sync_state.last_refresh_run_id)
        manifests = last_run.account_manifest if last_run is not None else None
        if manifests:
            completeness = (
                "source_reported"
                if all(item.get("source_reported_count") is not None for item in manifests)
                else "best_effort"
            )

    return DashboardResponse(
        as_of=window_end,
        timezone=active.timezone,
        display_currency=active.display_currency,
        config_version=active.version,
        config_hash=active.config_hash,
        pending_config_version=(pending_config.version if pending_config is not None else None),
        full_rebuild_required=pending_config is not None,
        freshness=DashboardFreshness(
            status=freshness_status,
            last_successful_refresh_at=last_refresh,
            stale_after_hours=stale_after_hours,
            completeness=completeness,
        ),
        categories=categories,
    )
