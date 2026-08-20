from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from rolling_budget_api.db import Base
from rolling_budget_api.db.models import (
    OAuthAccessToken,
    OAuthAuthorizationCode,
    OAuthRefreshToken,
    OAuthTokenFamily,
)
from rolling_budget_api.db.session import get_engine, get_session_factory
from rolling_budget_api.oauth import DatabaseTokenVerifier, OAuthConfig, create_oauth_router
from rolling_budget_api.oauth.config import (
    CHATGPT_STABLE_CLIENT_ID,
    CHATGPT_STABLE_REDIRECT_URI,
)
from rolling_budget_api.oauth.service import OAuthService

OWNER_SECRET = "synthetic-oauth-owner-secret-at-least-32-characters"
API_KEY = "synthetic-master-api-key-at-least-32-characters"
ADMIN_KEY = "synthetic-admin-api-key-at-least-32-characters"
PUBLIC_BASE_URL = "https://budget.example.com"
CODE_VERIFIER = "v" * 43
CODE_CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(CODE_VERIFIER.encode()).digest()
).rstrip(b"=").decode()


async def _chatgpt_client_metadata(client_id: str) -> dict[str, object]:
    return {
        "client_id": client_id,
        "redirect_uris": [CHATGPT_STABLE_REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "private_key_jwt",
        "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
    }


@dataclass(frozen=True)
class OAuthHarness:
    client: TestClient
    config: OAuthConfig
    session_factory: sessionmaker[Session]


@pytest.fixture
def oauth(tmp_path: Path) -> OAuthHarness:
    database_path = tmp_path / "oauth.db"
    database_url = f"sqlite:///{database_path}"
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(database_url)
    config = OAuthConfig(
        public_base_url=PUBLIC_BASE_URL,
        consent_secrets=(OWNER_SECRET,),
    )
    app = FastAPI()
    app.include_router(
        create_oauth_router(
            config,
            factory,
            client_metadata_loader=_chatgpt_client_metadata,
        )
    )
    return OAuthHarness(client=TestClient(app), config=config, session_factory=factory)


def _authorization_params(config: OAuthConfig, **overrides: str) -> dict[str, str]:
    params = {
        "response_type": "code",
        "client_id": CHATGPT_STABLE_CLIENT_ID,
        "redirect_uri": CHATGPT_STABLE_REDIRECT_URI,
        "resource": config.resource,
        "scope": "budget:read budget:refresh",
        "code_challenge": CODE_CHALLENGE,
        "code_challenge_method": "S256",
        "state": "synthetic-state",
    }
    params.update(overrides)
    return params


def _consent_token(response_text: str) -> str:
    match = re.search(r'name="consent_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _authorize(oauth: OAuthHarness, *, owner_secret: str = OWNER_SECRET) -> str:
    prompt = oauth.client.get("/oauth/authorize", params=_authorization_params(oauth.config))
    assert prompt.status_code == 200
    assert OWNER_SECRET not in prompt.text
    approval = oauth.client.post(
        "/oauth/authorize",
        data={
            "consent_token": _consent_token(prompt.text),
            "owner_secret": owner_secret,
            "action": "approve",
        },
        follow_redirects=False,
    )
    assert approval.status_code == 303
    query = parse_qs(urlsplit(approval.headers["location"]).query)
    assert query["state"] == ["synthetic-state"]
    assert query["iss"] == [oauth.config.issuer]
    return query["code"][0]


def _exchange(oauth: OAuthHarness, code: str, *, verifier: str = CODE_VERIFIER):
    return oauth.client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CHATGPT_STABLE_CLIENT_ID,
            "redirect_uri": CHATGPT_STABLE_REDIRECT_URI,
            "resource": oauth.config.resource,
            "code": code,
            "code_verifier": verifier,
        },
    )


def _complete_flow(oauth: OAuthHarness) -> dict[str, object]:
    response = _exchange(oauth, _authorize(oauth))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_metadata_is_cimd_pkce_only_and_uses_exact_mcp_resource(oauth: OAuthHarness) -> None:
    resource = oauth.client.get("/.well-known/oauth-protected-resource")
    path_resource = oauth.client.get("/.well-known/oauth-protected-resource/mcp")
    server = oauth.client.get("/.well-known/oauth-authorization-server")

    assert resource.json() == {
        "resource": f"{PUBLIC_BASE_URL}/mcp",
        "authorization_servers": [PUBLIC_BASE_URL],
        "scopes_supported": ["budget:read", "budget:refresh"],
    }
    assert path_resource.json() == resource.json()
    metadata = server.json()
    assert metadata["issuer"] == PUBLIC_BASE_URL
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert "registration_endpoint" not in metadata


def test_authorization_rejects_wrong_resource_and_non_chatgpt_redirect(
    oauth: OAuthHarness,
) -> None:
    wrong_resource = oauth.client.get(
        "/oauth/authorize",
        params=_authorization_params(oauth.config, resource="https://other.example/mcp"),
        follow_redirects=False,
    )
    assert wrong_resource.status_code == 303
    error_query = parse_qs(urlsplit(wrong_resource.headers["location"]).query)
    assert error_query["error"] == ["invalid_target"]
    assert error_query["iss"] == [oauth.config.issuer]

    wrong_redirect = oauth.client.get(
        "/oauth/authorize",
        params=_authorization_params(oauth.config, redirect_uri="https://evil.example/callback"),
        follow_redirects=False,
    )
    assert wrong_redirect.status_code == 400
    assert "location" not in wrong_redirect.headers
    assert wrong_redirect.json()["error"] == "unauthorized_client"


def test_authorization_fetches_and_validates_cimd(oauth: OAuthHarness) -> None:
    async def untrusted_metadata(client_id: str) -> dict[str, object]:
        document = await _chatgpt_client_metadata(client_id)
        document["redirect_uris"] = ["https://chatgpt.com/unregistered"]
        return document

    app = FastAPI()
    app.include_router(
        create_oauth_router(
            oauth.config,
            oauth.session_factory,
            client_metadata_loader=untrusted_metadata,
        )
    )
    response = TestClient(app).get(
        "/oauth/authorize",
        params=_authorization_params(oauth.config),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "location" not in response.headers
    assert response.json() == {
        "error": "unauthorized_client",
        "error_description": "The ChatGPT client metadata could not be validated",
    }


def test_consent_accepts_only_the_effective_owner_secret_and_returns_none(
    oauth: OAuthHarness,
) -> None:
    assert _authorize(oauth, owner_secret=OWNER_SECRET)

    for rejected_secret in (API_KEY, ADMIN_KEY, "wrong-owner-secret"):
        prompt = oauth.client.get(
            "/oauth/authorize",
            params=_authorization_params(oauth.config),
        )
        denied = oauth.client.post(
            "/oauth/authorize",
            data={
                "consent_token": _consent_token(prompt.text),
                "owner_secret": rejected_secret,
                "action": "approve",
            },
            follow_redirects=False,
        )
        assert denied.status_code == 401
        assert OWNER_SECRET not in denied.text
        assert API_KEY not in denied.text
        assert ADMIN_KEY not in denied.text


def test_code_is_single_use_pkce_bound_and_raw_tokens_are_never_stored(
    oauth: OAuthHarness,
) -> None:
    code = _authorize(oauth)
    bad_exchange = _exchange(oauth, code, verifier="x" * 43)
    assert bad_exchange.status_code == 400
    assert bad_exchange.json()["error"] == "invalid_grant"

    exchange = _exchange(oauth, code)
    assert exchange.status_code == 200
    body = exchange.json()
    replay = _exchange(oauth, code)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    with oauth.session_factory() as db:
        stored_code = db.scalar(select(OAuthAuthorizationCode))
        access = db.scalar(select(OAuthAccessToken))
        refresh = db.scalar(select(OAuthRefreshToken))
        assert stored_code is not None and stored_code.code_digest != code
        assert access is not None and access.token_digest != body["access_token"]
        assert refresh is not None and refresh.token_digest != body["refresh_token"]
        assert len(stored_code.code_digest) == 64
        assert len(access.token_digest) == 64
        assert len(refresh.token_digest) == 64


def test_refresh_rotates_and_replay_revokes_entire_family(oauth: OAuthHarness) -> None:
    initial = _complete_flow(oauth)
    rotated_response = oauth.client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CHATGPT_STABLE_CLIENT_ID,
            "resource": oauth.config.resource,
            "refresh_token": initial["refresh_token"],
        },
    )
    assert rotated_response.status_code == 200
    rotated = rotated_response.json()
    assert rotated["refresh_token"] != initial["refresh_token"]

    replay = oauth.client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CHATGPT_STABLE_CLIENT_ID,
            "resource": oauth.config.resource,
            "refresh_token": initial["refresh_token"],
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    service = OAuthService(oauth.config, oauth.session_factory)
    assert service.verify_access_token(str(rotated["access_token"])) is None
    with oauth.session_factory() as db:
        family = db.scalar(select(OAuthTokenFamily))
        assert family is not None
        assert family.revoked_at is not None
        assert family.compromise_detected_at is not None


def test_refresh_scope_cannot_be_increased(oauth: OAuthHarness) -> None:
    code = _authorize(oauth)
    initial_response = _exchange(oauth, code)
    initial = initial_response.json()

    narrowed = oauth.client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CHATGPT_STABLE_CLIENT_ID,
            "resource": oauth.config.resource,
            "refresh_token": initial["refresh_token"],
            "scope": "budget:read",
        },
    )
    assert narrowed.status_code == 200
    assert narrowed.json()["scope"] == "budget:read"

    escalation = oauth.client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CHATGPT_STABLE_CLIENT_ID,
            "resource": oauth.config.resource,
            "refresh_token": narrowed.json()["refresh_token"],
            "scope": "budget:read budget:refresh",
        },
    )
    assert escalation.status_code == 400
    assert escalation.json()["error"] == "invalid_scope"


def test_database_verifier_matches_mcp_129_protocol(oauth: OAuthHarness) -> None:
    body = _complete_flow(oauth)
    verifier = DatabaseTokenVerifier(oauth.config, oauth.session_factory)

    verified = asyncio.run(verifier.verify_token(str(body["access_token"])))

    assert verified is not None
    assert verified.client_id == CHATGPT_STABLE_CLIENT_ID
    assert verified.scopes == ["budget:read", "budget:refresh"]
    assert verified.resource == oauth.config.resource
    assert verified.subject == "owner"
    assert verified.claims == {"iss": oauth.config.issuer}
    assert asyncio.run(verifier.verify_token(API_KEY)) is None


@pytest.mark.parametrize("token_key", ["access_token", "refresh_token"])
def test_revocation_invalidates_refresh_family(
    oauth: OAuthHarness,
    token_key: str,
) -> None:
    body = _complete_flow(oauth)
    revoke = oauth.client.post(
        "/oauth/revoke",
        data={
            "token": body[token_key],
            "token_type_hint": token_key,
        },
    )
    assert revoke.status_code == 200

    service = OAuthService(oauth.config, oauth.session_factory)
    assert service.verify_access_token(str(body["access_token"])) is None
