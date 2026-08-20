from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from rolling_budget_api.oauth.config import OAuthConfig
from rolling_budget_api.oauth.service import OAuthService

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken


class DatabaseTokenVerifier:
    """Structural implementation of mcp 1.29's TokenVerifier protocol."""

    def __init__(
        self,
        config: OAuthConfig,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.config = config
        self._service = OAuthService(config, session_factory)

    async def verify_token(self, token: str) -> AccessToken | None:
        from mcp.server.auth.provider import AccessToken

        verified = self._service.verify_access_token(token)
        if verified is None:
            return None
        return AccessToken(
            token=token,
            client_id=verified.client_id,
            scopes=list(verified.scopes),
            expires_at=verified.expires_at,
            resource=verified.resource,
            subject=verified.subject,
            claims={"iss": self.config.issuer},
        )
