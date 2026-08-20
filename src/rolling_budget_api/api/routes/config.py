from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from rolling_budget_api.core.auth import require_admin, require_read
from rolling_budget_api.db.session import get_db
from rolling_budget_api.schemas.config import ConfigPutRequest, ConfigView
from rolling_budget_api.services.config_service import get_config, put_config

router = APIRouter(prefix="/config", tags=["configuration"])


@router.get("", response_model=ConfigView, dependencies=[Depends(require_read)])
def read_config(response: Response, db: Session = Depends(get_db)) -> ConfigView:
    view = get_config(db)
    base = view.pending or view.active
    if base is not None:
        response.headers["ETag"] = f'"{base.config_hash}"'
    return view


@router.put("", response_model=ConfigView, dependencies=[Depends(require_admin)])
def replace_config(
    request: ConfigPutRequest,
    response: Response,
    db: Session = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ConfigView:
    view = put_config(db, request, if_match=if_match)
    base = view.pending or view.active
    if base is not None:
        response.headers["ETag"] = f'"{base.config_hash}"'
    return view
