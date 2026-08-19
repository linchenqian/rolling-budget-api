import secrets
from collections.abc import Callable
from enum import IntEnum

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from rolling_budget_api.core.config import Settings, get_settings


class AccessLevel(IntEnum):
    READ = 1
    WRITE = 2
    ADMIN = 3


bearer = HTTPBearer(auto_error=False)


def _provided_level(token: str, settings: Settings) -> AccessLevel | None:
    candidates = (
        (settings.budget_admin_api_key, AccessLevel.ADMIN),
        (settings.budget_write_api_key, AccessLevel.WRITE),
        (settings.budget_read_api_key, AccessLevel.READ),
        (settings.api_key, AccessLevel.ADMIN),
    )
    for configured, level in candidates:
        if configured is not None and secrets.compare_digest(token, configured):
            return level
    return None


def require_access(minimum: AccessLevel) -> Callable[..., AccessLevel]:
    def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        settings: Settings = Depends(get_settings),
    ) -> AccessLevel:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A bearer API key is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        level = _provided_level(credentials.credentials, settings)
        if level is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if level < minimum:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient access")
        return level

    return dependency


require_read = require_access(AccessLevel.READ)
require_write = require_access(AccessLevel.WRITE)
require_admin = require_access(AccessLevel.ADMIN)
