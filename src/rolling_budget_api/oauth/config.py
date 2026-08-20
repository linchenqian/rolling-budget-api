from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

SUPPORTED_SCOPES = ("budget:read", "budget:refresh")
CHATGPT_STABLE_CLIENT_ID = "https://chatgpt.com/oauth/client.json"
CHATGPT_STABLE_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
_CALLBACK_CLIENT_RE = re.compile(
    r"^https://chatgpt\.com/oauth/(?P<callback>[A-Za-z0-9_-]{1,128})/client\.json$"
)
_CALLBACK_REDIRECT_RE = re.compile(
    r"^https://chatgpt\.com/connector/oauth/(?P<callback>[A-Za-z0-9_-]{1,128})$"
)


class OAuthSettingsLike(Protocol):
    public_base_url: str | None
    oauth_consent_secret: str | None
    oauth_form_action_origins: list[str]
    api_key: str | None
    oauth_authorization_code_ttl_seconds: int
    oauth_access_token_ttl_seconds: int
    oauth_refresh_token_ttl_seconds: int


@dataclass(frozen=True)
class OAuthConfig:
    public_base_url: str
    consent_secrets: tuple[str, ...] = field(repr=False)
    form_action_origins: tuple[str, ...] = ("https://chatgpt.com",)
    authorization_code_ttl_seconds: int = 300
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 7_776_000

    def __post_init__(self) -> None:
        parsed = urlsplit(self.public_base_url.strip())
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        if parsed.scheme != "https" and not is_loopback_http:
            raise ValueError("PUBLIC_BASE_URL must use HTTPS outside loopback tests")
        if (
            not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be an origin without credentials or a path")
        normalized = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        object.__setattr__(self, "public_base_url", normalized)

        unique_secrets = tuple(dict.fromkeys(secret for secret in self.consent_secrets if secret))
        if not unique_secrets or any(len(secret) < 24 for secret in unique_secrets):
            raise ValueError("At least one owner consent secret of 24 characters is required")
        object.__setattr__(self, "consent_secrets", unique_secrets)

        normalized_form_action_origins: list[str] = []
        for origin in self.form_action_origins:
            origin_parts = urlsplit(origin.strip())
            if (
                origin_parts.scheme != "https"
                or not origin_parts.netloc
                or origin_parts.username is not None
                or origin_parts.password is not None
                or origin_parts.path not in {"", "/"}
                or origin_parts.query
                or origin_parts.fragment
            ):
                raise ValueError("OAuth form actions require exact HTTPS origins")
            exact_origin = urlunsplit(
                (origin_parts.scheme, origin_parts.netloc, "", "", "")
            )
            if exact_origin not in normalized_form_action_origins:
                normalized_form_action_origins.append(exact_origin)
        if not normalized_form_action_origins:
            raise ValueError("At least one OAuth form-action origin is required")
        object.__setattr__(
            self,
            "form_action_origins",
            tuple(normalized_form_action_origins),
        )

        if not 60 <= self.authorization_code_ttl_seconds <= 900:
            raise ValueError("Authorization-code TTL must be between 60 and 900 seconds")
        if not 300 <= self.access_token_ttl_seconds <= 86_400:
            raise ValueError("Access-token TTL must be between 300 and 86400 seconds")
        if not 86_400 <= self.refresh_token_ttl_seconds <= 31_536_000:
            raise ValueError("Refresh-token TTL must be between 86400 and 31536000 seconds")

    @classmethod
    def from_settings(cls, settings: OAuthSettingsLike) -> OAuthConfig:
        if settings.public_base_url is None:
            raise ValueError("PUBLIC_BASE_URL is required to enable remote MCP OAuth")
        primary = settings.oauth_consent_secret or settings.api_key
        if primary is None:
            raise ValueError(
                "OAUTH_CONSENT_SECRET is required when API_KEY is not configured"
            )
        return cls(
            public_base_url=settings.public_base_url,
            consent_secrets=(primary,),
            form_action_origins=tuple(settings.oauth_form_action_origins),
            authorization_code_ttl_seconds=settings.oauth_authorization_code_ttl_seconds,
            access_token_ttl_seconds=settings.oauth_access_token_ttl_seconds,
            refresh_token_ttl_seconds=settings.oauth_refresh_token_ttl_seconds,
        )

    @property
    def issuer(self) -> str:
        return self.public_base_url

    @property
    def resource(self) -> str:
        return f"{self.public_base_url}/mcp"

    @property
    def protected_resource_metadata_url(self) -> str:
        return f"{self.public_base_url}/.well-known/oauth-protected-resource/mcp"

    @property
    def hmac_key(self) -> bytes:
        return hmac.new(
            self.consent_secrets[0].encode("utf-8"),
            b"rolling-budget-oauth-v1",
            hashlib.sha256,
        ).digest()

    @property
    def credential_generation(self) -> str:
        return hmac.new(
            self.hmac_key,
            b"credential-generation",
            hashlib.sha256,
        ).hexdigest()

    def owner_secret_matches(self, candidate: str) -> bool:
        matches = False
        for configured in self.consent_secrets:
            matches = hmac.compare_digest(candidate, configured) or matches
        return matches


def validate_chatgpt_client(client_id: str, redirect_uri: str) -> None:
    if client_id == CHATGPT_STABLE_CLIENT_ID:
        if redirect_uri != CHATGPT_STABLE_REDIRECT_URI:
            raise ValueError("The redirect URI does not match the ChatGPT CIMD client")
        return

    client_match = _CALLBACK_CLIENT_RE.fullmatch(client_id)
    redirect_match = _CALLBACK_REDIRECT_RE.fullmatch(redirect_uri)
    if (
        client_match is None
        or redirect_match is None
        or client_match.group("callback") != redirect_match.group("callback")
    ):
        raise ValueError("Only an exact ChatGPT CIMD client and redirect URI are allowed")


def validate_chatgpt_client_id(client_id: str) -> None:
    if client_id == CHATGPT_STABLE_CLIENT_ID or _CALLBACK_CLIENT_RE.fullmatch(client_id):
        return
    raise ValueError("Only a ChatGPT CIMD client is allowed")
