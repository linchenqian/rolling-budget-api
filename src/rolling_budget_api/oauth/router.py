from __future__ import annotations

import html
from collections.abc import Mapping
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, sessionmaker

from rolling_budget_api.oauth.client_metadata import (
    ClientMetadataError,
    ClientMetadataLoader,
    validate_chatgpt_client_metadata,
)
from rolling_budget_api.oauth.config import (
    SUPPORTED_SCOPES,
    OAuthConfig,
    validate_chatgpt_client,
    validate_chatgpt_client_id,
)
from rolling_budget_api.oauth.service import (
    OAuthProtocolError,
    OAuthService,
    parse_authorization_request,
)

_FORM_LIMIT_BYTES = 65_536
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_oauth_router(
    config: OAuthConfig,
    session_factory: sessionmaker[Session] | None = None,
    client_metadata_loader: ClientMetadataLoader | None = None,
) -> APIRouter:
    router = APIRouter(tags=["oauth"])
    service = OAuthService(config, session_factory)

    @router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    def protected_resource_metadata() -> JSONResponse:
        return JSONResponse(
            {
                "resource": config.resource,
                "authorization_servers": [config.issuer],
                "scopes_supported": list(SUPPORTED_SCOPES),
            }
        )

    @router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def authorization_server_metadata() -> JSONResponse:
        return JSONResponse(
            {
                "issuer": config.issuer,
                "authorization_endpoint": f"{config.issuer}/oauth/authorize",
                "token_endpoint": f"{config.issuer}/oauth/token",
                "revocation_endpoint": f"{config.issuer}/oauth/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "revocation_endpoint_auth_methods_supported": ["none"],
                "client_id_metadata_document_supported": True,
                "authorization_response_iss_parameter_supported": True,
                "scopes_supported": list(SUPPORTED_SCOPES),
            }
        )

    @router.get("/oauth/authorize", include_in_schema=False)
    async def authorize(request: Request) -> Response:
        try:
            values = _unique_query_values(request)
            authorization = parse_authorization_request(values, config)
        except OAuthProtocolError as exc:
            return _authorization_error(request.query_params, config, exc)
        try:
            await validate_chatgpt_client_metadata(
                authorization.client_id,
                authorization.redirect_uri,
                client_metadata_loader,
            )
        except ClientMetadataError:
            return _oauth_error(
                OAuthProtocolError(
                    "unauthorized_client",
                    "The ChatGPT client metadata could not be validated",
                )
            )
        consent_token = service.sign_consent(authorization)
        return _consent_page(
            consent_token,
            authorization.scopes,
            form_action_origins=config.form_action_origins,
        )

    @router.post("/oauth/authorize", include_in_schema=False)
    async def authorize_consent(request: Request) -> Response:
        try:
            form = await _read_form(request)
            consent_token = _required(form, "consent_token")
            authorization = service.verify_consent(consent_token)
        except OAuthProtocolError as exc:
            return _oauth_error(exc)

        if form.get("action") == "deny":
            return _redirect(
                authorization.redirect_uri,
                {
                    "error": "access_denied",
                    "error_description": "The owner denied access",
                    "state": authorization.state,
                    "iss": config.issuer,
                },
            )
        if form.get("action") != "approve":
            return _oauth_error(OAuthProtocolError("invalid_request", "Choose approve or deny"))

        supplied_secret = form.get("owner_secret", "")
        if not config.owner_secret_matches(supplied_secret):
            return _consent_page(
                consent_token,
                authorization.scopes,
                form_action_origins=config.form_action_origins,
                error="The owner secret is invalid",
                status_code=401,
            )
        code = service.issue_authorization_code(authorization)
        return _redirect(
            authorization.redirect_uri,
            {"code": code, "state": authorization.state, "iss": config.issuer},
        )

    @router.post("/oauth/token", include_in_schema=False)
    async def token(request: Request) -> JSONResponse:
        try:
            form = await _read_form(request)
            if "client_secret" in form or "client_assertion" in form:
                raise OAuthProtocolError(
                    "invalid_client",
                    "This CIMD public client does not use client authentication",
                )
            grant_type = _required(form, "grant_type")
            client_id = _required(form, "client_id")
            resource = _required(form, "resource")
            if grant_type == "authorization_code":
                result = service.exchange_authorization_code(
                    code=_required(form, "code"),
                    client_id=client_id,
                    redirect_uri=_required(form, "redirect_uri"),
                    resource=resource,
                    code_verifier=_required(form, "code_verifier"),
                )
            elif grant_type == "refresh_token":
                requested_scopes = None
                if form.get("scope"):
                    requested_scopes = tuple(form["scope"].split())
                result = service.refresh(
                    refresh_token=_required(form, "refresh_token"),
                    client_id=client_id,
                    resource=resource,
                    requested_scopes=requested_scopes,
                )
            else:
                raise OAuthProtocolError(
                    "unsupported_grant_type",
                    "Only authorization_code and refresh_token are supported",
                )
        except OAuthProtocolError as exc:
            return _oauth_error(exc)
        return JSONResponse(result.as_dict(), headers=_token_headers())

    @router.post("/oauth/revoke", include_in_schema=False)
    async def revoke(request: Request) -> Response:
        try:
            form = await _read_form(request)
            client_id = form.get("client_id")
            if client_id is not None:
                try:
                    validate_chatgpt_client_id(client_id)
                except ValueError as exc:
                    raise OAuthProtocolError(
                        "invalid_client",
                        "The OAuth client is invalid",
                    ) from exc
            resource = form.get("resource")
            if resource is not None and resource != config.resource:
                raise OAuthProtocolError(
                    "invalid_target",
                    "The OAuth resource is not this MCP server",
                )
            service.revoke(_required(form, "token"))
        except OAuthProtocolError as exc:
            return _oauth_error(exc)
        return Response(status_code=200, headers=_token_headers())

    return router


def _unique_query_values(request: Request) -> dict[str, str | None]:
    names = (
        "response_type",
        "client_id",
        "redirect_uri",
        "resource",
        "scope",
        "code_challenge",
        "code_challenge_method",
        "state",
    )
    result: dict[str, str | None] = {}
    for name in names:
        values = request.query_params.getlist(name)
        if len(values) > 1:
            raise OAuthProtocolError("invalid_request", f"Duplicate {name} parameter")
        result[name] = values[0] if values else None
    return result


async def _read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise OAuthProtocolError("invalid_request", "OAuth endpoints require form encoding")
    body = await request.body()
    if len(body) > _FORM_LIMIT_BYTES:
        raise OAuthProtocolError("invalid_request", "The OAuth request is too large")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError as exc:
        raise OAuthProtocolError("invalid_request", "The OAuth form is invalid") from exc
    result: dict[str, str] = {}
    for name, values in parsed.items():
        if len(values) != 1:
            raise OAuthProtocolError("invalid_request", f"Duplicate {name} parameter")
        result[name] = values[0]
    return result


def _required(form: Mapping[str, str], name: str) -> str:
    value = form.get(name)
    if value is None or not value:
        raise OAuthProtocolError("invalid_request", f"Missing {name}")
    return value


def _authorization_error(
    values: Mapping[str, str],
    config: OAuthConfig,
    error: OAuthProtocolError,
) -> Response:
    client_id = values.get("client_id")
    redirect_uri = values.get("redirect_uri")
    if client_id and redirect_uri:
        try:
            validate_chatgpt_client(client_id, redirect_uri)
        except ValueError:
            pass
        else:
            return _redirect(
                redirect_uri,
                {
                    "error": error.error,
                    "error_description": error.description,
                    "state": values.get("state"),
                    "iss": config.issuer,
                },
            )
    return _oauth_error(error)


def _redirect(base_uri: str, values: Mapping[str, str | None]) -> RedirectResponse:
    parsed = urlsplit(base_uri)
    query = urlencode([(key, value) for key, value in values.items() if value is not None])
    location = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    return RedirectResponse(location, status_code=303, headers=_token_headers())


def _oauth_error(error: OAuthProtocolError) -> JSONResponse:
    return JSONResponse(
        {"error": error.error, "error_description": error.description},
        status_code=400,
        headers=_token_headers(),
    )


def _token_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _consent_page(
    consent_token: str,
    scopes: tuple[str, ...],
    *,
    form_action_origins: tuple[str, ...],
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    scope_items = "".join(f"<li>{html.escape(scope)}</li>" for scope in scopes)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize Rolling Budget Sync</title>
  <style>
    body {{ font: 16px system-ui; max-width: 36rem; margin: 4rem auto; padding: 0 1rem; }}
    input {{ box-sizing: border-box; width: 100%; padding: .75rem; margin: .5rem 0 1rem; }}
    button {{ padding: .65rem 1rem; margin-right: .5rem; }}
    .error {{ color: #b42318; }}
  </style>
</head>
<body>
  <h1>Authorize Rolling Budget Sync</h1>
  <p>ChatGPT is requesting these permissions:</p>
  <ul>{scope_items}</ul>
  {error_html}
  <form method="post" action="/oauth/authorize">
    <input type="hidden" name="consent_token" value="{html.escape(consent_token)}">
    <label for="owner_secret">Owner secret</label>
    <input id="owner_secret" name="owner_secret" type="password"
           autocomplete="current-password" required>
    <button type="submit" name="action" value="approve">Authorize</button>
    <button type="submit" name="action" value="deny" formnovalidate>Deny</button>
  </form>
</body>
</html>"""
    form_action_sources = " ".join(form_action_origins)
    security_headers = {
        **_SECURITY_HEADERS,
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"form-action 'self' {form_action_sources}; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
    }
    return HTMLResponse(document, status_code=status_code, headers=security_headers)
