from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class DashboardCategory(BaseModel):
    key: str
    name: str
    icon: str | None
    sort_order: int
    window_days: int
    window_start: date
    window_end: date
    currency: str
    spent: Decimal
    budget: Decimal
    remaining: Decimal
    over: Decimal
    progress: Decimal
    status: str
    transaction_count: int
    pending_count: int
    pending_amount: Decimal
    refund_count: int
    refund_amount: Decimal


class DashboardFreshness(BaseModel):
    status: str
    last_successful_refresh_at: datetime | None
    stale_after_hours: int
    completeness: str


class DashboardResponse(BaseModel):
    as_of: date
    timezone: str
    display_currency: str
    config_version: int
    config_hash: str
    pending_config_version: int | None
    full_rebuild_required: bool
    freshness: DashboardFreshness
    categories: list[DashboardCategory]
