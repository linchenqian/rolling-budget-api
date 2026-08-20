from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from rolling_budget_api.core.config import Settings
from rolling_budget_api.db import Base
from rolling_budget_api.db import session as db_session
from rolling_budget_api.db.session import get_session_factory
from rolling_budget_api.main import create_app
from rolling_budget_api.oauth import OAuthConfig
from rolling_budget_api.oauth.config import (
    CHATGPT_STABLE_CLIENT_ID,
    CHATGPT_STABLE_REDIRECT_URI,
)
from rolling_budget_api.oauth.service import AuthorizationRequest, OAuthService

MASTER_KEY = "remote-mcp-master-key-at-least-32-characters"
PUBLIC_BASE_URL = "http://localhost"
CODE_VERIFIER = "v" * 43
CODE_CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(CODE_VERIFIER.encode()).digest()
).rstrip(b"=").decode()


@pytest.fixture
def remote_settings(tmp_path: Path) -> Iterator[Settings]:
    database_url = f"sqlite:///{tmp_path / 'remote-mcp.db'}"
    settings = Settings(
        _env_file=None,
        DATABASE_URL=database_url,
        API_KEY=MASTER_KEY,
        PUBLIC_BASE_URL=PUBLIC_BASE_URL,
    )
    engine = db_session.get_engine(database_url)
    Base.metadata.create_all(engine)
    try:
        yield settings
    finally:
        engine.dispose()
        db_session._create_session_factory.cache_clear()
        db_session._create_engine.cache_clear()


def _authorization_code(
    settings: Settings,
    *,
    scopes: tuple[str, ...] = ("budget:read", "budget:refresh"),
) -> str:
    oauth_config = OAuthConfig.from_settings(settings)
    service = OAuthService(
        oauth_config,
        get_session_factory(settings.database_url),
    )
    return service.issue_authorization_code(
        AuthorizationRequest(
            client_id=CHATGPT_STABLE_CLIENT_ID,
            redirect_uri=CHATGPT_STABLE_REDIRECT_URI,
            resource=oauth_config.resource,
            scopes=scopes,
            code_challenge=CODE_CHALLENGE,
            state="remote-mcp-e2e",
        )
    )


def _config_payload() -> dict[str, object]:
    return {
        "timezone": "America/New_York",
        "display_currency": "USD",
        "aggregation_version": 1,
        "categories": [
            {
                "key": "restaurant",
                "name": "Restaurant",
                "icon": "fork-knife",
                "sort_order": 0,
                "budget_limit": "100",
                "budget_currency": "USD",
                "lookback_days": 30,
                "classification_instruction": "Meals and takeout",
                "enabled": True,
            }
        ],
    }


def _mcp_request(
    client: TestClient,
    *,
    request_id: int,
    method: str,
    params: dict[str, object],
    bearer: str,
) -> Response:
    return cast(
        Response,
        client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        ),
    )


def test_top_level_oauth_token_reaches_mcp_but_keys_stay_separate(
    remote_settings: Settings,
) -> None:
    app = create_app(remote_settings)
    oauth_config = OAuthConfig.from_settings(remote_settings)

    with TestClient(app) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == f"{PUBLIC_BASE_URL}/mcp"

        token_response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CHATGPT_STABLE_CLIENT_ID,
                "redirect_uri": CHATGPT_STABLE_REDIRECT_URI,
                "resource": oauth_config.resource,
                "code": _authorization_code(remote_settings),
                "code_verifier": CODE_VERIFIER,
            },
        )
        assert token_response.status_code == 200, token_response.text
        access_token = token_response.json()["access_token"]

        initialized = _mcp_request(
            client,
            request_id=1,
            method="initialize",
            params={
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "remote-e2e", "version": "1.0"},
            },
            bearer=access_token,
        )
        assert initialized.status_code == 200, initialized.text

        configured = _mcp_request(
            client,
            request_id=2,
            method="tools/call",
            params={"name": "get_config", "arguments": {}},
            bearer=access_token,
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["result"]["structuredContent"] == {
            "active": None,
            "pending": None,
        }

        old_token_write = _mcp_request(
            client,
            request_id=3,
            method="tools/call",
            params={
                "name": "update_config",
                "arguments": {
                    "configuration": _config_payload(),
                    "expected_config_hash": None,
                },
            },
            bearer=access_token,
        )
        assert old_token_write.status_code == 200, old_token_write.text
        old_token_result = old_token_write.json()["result"]
        assert old_token_result["isError"] is True
        assert 'scope="budget:config"' in old_token_result["_meta"][
            "mcp/www_authenticate"
        ][0]

        config_token_response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CHATGPT_STABLE_CLIENT_ID,
                "redirect_uri": CHATGPT_STABLE_REDIRECT_URI,
                "resource": oauth_config.resource,
                "code": _authorization_code(
                    remote_settings,
                    scopes=("budget:config",),
                ),
                "code_verifier": CODE_VERIFIER,
            },
        )
        assert config_token_response.status_code == 200, config_token_response.text
        config_access_token = config_token_response.json()["access_token"]

        created = _mcp_request(
            client,
            request_id=4,
            method="tools/call",
            params={
                "name": "update_config",
                "arguments": {
                    "configuration": _config_payload(),
                    "expected_config_hash": None,
                },
            },
            bearer=config_access_token,
        )
        assert created.status_code == 200, created.text
        created_content = created.json()["result"]["structuredContent"]
        assert created_content["active"]["version"] == 1
        assert created_content["active"]["requires_full_rebuild"] is True
        assert created_content["pending"] is None

        read_after_write = _mcp_request(
            client,
            request_id=5,
            method="tools/call",
            params={"name": "get_config", "arguments": {}},
            bearer=access_token,
        )
        assert read_after_write.status_code == 200, read_after_write.text
        assert read_after_write.json()["result"]["structuredContent"]["active"][
            "categories"
        ][0]["key"] == "restaurant"

        static_key_attempt = _mcp_request(
            client,
            request_id=6,
            method="tools/call",
            params={"name": "get_config", "arguments": {}},
            bearer=MASTER_KEY,
        )
        assert static_key_attempt.status_code == 200
        assert static_key_attempt.json()["result"]["isError"] is True

        oauth_token_on_rest = client.get(
            "/v1/config",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert oauth_token_on_rest.status_code == 401
