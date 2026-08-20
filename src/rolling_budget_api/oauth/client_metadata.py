from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping

import httpx

from rolling_budget_api.oauth.config import validate_chatgpt_client

ClientMetadataLoader = Callable[[str], Awaitable[Mapping[str, object]]]

_MAX_DOCUMENT_BYTES = 65_536


class ClientMetadataError(ValueError):
    pass


async def fetch_chatgpt_client_metadata(client_id: str) -> Mapping[str, object]:
    """Fetch a pre-allowlisted ChatGPT CIMD document without following redirects."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(5.0),
        ) as client:
            async with client.stream(
                "GET",
                client_id,
                headers={"Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise ClientMetadataError("The client metadata document is unavailable")
                content_type = response.headers.get("content-type", "").partition(";")[0]
                if content_type.strip().lower() != "application/json":
                    raise ClientMetadataError("The client metadata document is not JSON")

                chunks: list[bytes] = []
                length = 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > _MAX_DOCUMENT_BYTES:
                        raise ClientMetadataError("The client metadata document is too large")
                    chunks.append(chunk)
    except ClientMetadataError:
        raise
    except httpx.HTTPError as exc:
        raise ClientMetadataError("The client metadata document is unavailable") from exc

    try:
        document = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientMetadataError("The client metadata document is invalid") from exc
    if not isinstance(document, dict) or not all(
        isinstance(key, str) for key in document
    ):
        raise ClientMetadataError("The client metadata document is invalid")
    return document


async def validate_chatgpt_client_metadata(
    client_id: str,
    redirect_uri: str,
    loader: ClientMetadataLoader | None = None,
) -> None:
    """Resolve and validate the ChatGPT CIMD fields used by this public client."""
    try:
        validate_chatgpt_client(client_id, redirect_uri)
    except ValueError as exc:
        raise ClientMetadataError(str(exc)) from exc

    document = await (loader or fetch_chatgpt_client_metadata)(client_id)
    if document.get("client_id") != client_id:
        raise ClientMetadataError("The client metadata identity does not match")

    redirect_uris = document.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not all(
        isinstance(uri, str) for uri in redirect_uris
    ):
        raise ClientMetadataError("The client metadata redirect URIs are invalid")
    if redirect_uri not in redirect_uris:
        raise ClientMetadataError("The redirect URI is not registered by the client")

    grant_types = document.get("grant_types")
    if not isinstance(grant_types, list) or not all(
        isinstance(grant_type, str) for grant_type in grant_types
    ):
        raise ClientMetadataError("The client metadata grant types are invalid")
    if not {"authorization_code", "refresh_token"}.issubset(grant_types):
        raise ClientMetadataError("The client does not support the required grants")

    response_types = document.get("response_types")
    if not isinstance(response_types, list) or not all(
        isinstance(response_type, str) for response_type in response_types
    ):
        raise ClientMetadataError("The client metadata response types are invalid")
    if "code" not in response_types:
        raise ClientMetadataError("The client does not support authorization codes")

    methods: set[str] = set()
    supported_methods = document.get("token_endpoint_auth_methods_supported")
    if supported_methods is not None:
        if not isinstance(supported_methods, list) or not all(
            isinstance(method, str) for method in supported_methods
        ):
            raise ClientMetadataError("The client token authentication methods are invalid")
        methods.update(supported_methods)
    legacy_method = document.get("token_endpoint_auth_method")
    if legacy_method is not None:
        if not isinstance(legacy_method, str):
            raise ClientMetadataError("The client token authentication method is invalid")
        methods.add(legacy_method)
    if "none" not in methods:
        raise ClientMetadataError("The client does not support public token exchange")
