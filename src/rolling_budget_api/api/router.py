from fastapi import APIRouter

from rolling_budget_api.api.routes import config, dashboard, health, refresh

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(config.router, prefix="/v1")
api_router.include_router(refresh.router, prefix="/v1")
api_router.include_router(dashboard.router, prefix="/v1")
