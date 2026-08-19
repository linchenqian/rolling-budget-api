from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from rolling_budget_api.core.auth import require_read
from rolling_budget_api.core.config import Settings, get_settings
from rolling_budget_api.db.session import get_db
from rolling_budget_api.schemas.dashboard import DashboardResponse
from rolling_budget_api.services.dashboard_service import get_dashboard

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "/budgets",
    response_model=DashboardResponse,
    dependencies=[Depends(require_read)],
)
def read_budget_dashboard(
    as_of: date | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DashboardResponse:
    return get_dashboard(db, as_of=as_of, stale_after_hours=settings.stale_after_hours)
