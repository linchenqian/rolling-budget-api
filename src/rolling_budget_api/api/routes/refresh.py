from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, status
from sqlalchemy.orm import Session

from rolling_budget_api.core.auth import require_write
from rolling_budget_api.core.config import Settings, get_settings
from rolling_budget_api.db.session import get_db
from rolling_budget_api.schemas.refresh import (
    RefreshBatchRequest,
    RefreshBatchResponse,
    RefreshBeginRequest,
    RefreshBeginResponse,
    RefreshCommitRequest,
    RefreshRunView,
)
from rolling_budget_api.services.refresh_service import (
    begin_refresh,
    commit_refresh,
    get_refresh_run,
    upload_batch,
)

router = APIRouter(prefix="/refresh-runs", tags=["refresh"])


@router.post(
    "",
    response_model=RefreshBeginResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_write)],
)
def create_refresh_run(
    request: RefreshBeginRequest,
    idempotency_key: str = Header(min_length=8, max_length=128, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RefreshBeginResponse:
    return begin_refresh(
        db,
        request,
        idempotency_key=idempotency_key,
        max_batch_items=settings.max_batch_items,
        max_request_bytes=settings.max_request_bytes,
    )


@router.put(
    "/{run_id}/batches/{batch_index}",
    response_model=RefreshBatchResponse,
    dependencies=[Depends(require_write)],
)
def put_refresh_batch(
    request: RefreshBatchRequest,
    run_id: UUID,
    batch_index: int = Path(ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RefreshBatchResponse:
    return upload_batch(
        db,
        run_id,
        batch_index,
        request,
        max_batch_items=settings.max_batch_items,
        max_request_bytes=settings.max_request_bytes,
    )


@router.post(
    "/{run_id}/commit",
    response_model=RefreshRunView,
    dependencies=[Depends(require_write)],
)
def finalize_refresh_run(
    request: RefreshCommitRequest,
    run_id: UUID,
    db: Session = Depends(get_db),
) -> RefreshRunView:
    return commit_refresh(db, run_id, request)


@router.get(
    "/{run_id}",
    response_model=RefreshRunView,
    dependencies=[Depends(require_write)],
)
def read_refresh_run(run_id: UUID, db: Session = Depends(get_db)) -> RefreshRunView:
    return get_refresh_run(db, run_id)
