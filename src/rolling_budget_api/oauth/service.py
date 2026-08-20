from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from rolling_budget_api.db.models import (
    OAuthAccessToken,
    OAuthAuthorizationCode,
    OAuthRefreshToken,
    OAuthTokenFamily,
)
from rolling_budget_api.db.session import begin_write_transaction, get_session_factory
from rolling_budget_api.oauth.config import (
    SUPPORTED_SCOPES,
    OAuthConfig,
    validate_chatgpt_client,
    validate_chatgpt_client_id,
)

_S256_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CODE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_SUBJECT = "owner"


class OAuthProtocolError(Exception):
    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


@dataclass(frozen=True)
class AuthorizationRequest:
    client_id: str
    redirect_uri: str
    resource: str
    scopes: tuple[str, ...]
    code_challenge: str
    state: str | None


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": " ".join(self.scopes),
        }


@dataclass(frozen=True)
class VerifiedToken:
    client_id: str
    scopes: tuple[str, ...]
    expires_at: int
    resource: str
    subject: str


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode_scopes(scopes: tuple[str, ...]) -> str:
    return " ".join(scopes)


def _decode_scopes(scopes: str) -> tuple[str, ...]:
    return tuple(item for item in scopes.split(" ") if item)


def normalize_scopes(raw: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    parts = raw.split() if isinstance(raw, str) else list(raw)
    normalized = tuple(sorted(set(parts)))
    if not normalized or any(scope not in SUPPORTED_SCOPES for scope in normalized):
        raise OAuthProtocolError("invalid_scope", "Unsupported or empty OAuth scope")
    return normalized


def validate_authorization_request(request: AuthorizationRequest, config: OAuthConfig) -> None:
    try:
        validate_chatgpt_client(request.client_id, request.redirect_uri)
    except ValueError as exc:
        raise OAuthProtocolError("unauthorized_client", str(exc)) from exc
    if request.resource != config.resource:
        raise OAuthProtocolError("invalid_target", "The OAuth resource is not this MCP server")
    normalize_scopes(request.scopes)
    if not _S256_CHALLENGE_RE.fullmatch(request.code_challenge):
        raise OAuthProtocolError("invalid_request", "A valid PKCE S256 challenge is required")
    if request.state is not None and len(request.state) > 2048:
        raise OAuthProtocolError("invalid_request", "OAuth state is too long")


class OAuthService:
    def __init__(
        self,
        config: OAuthConfig,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.config = config
        self._session_factory = session_factory or get_session_factory()

    def digest(self, kind: str, raw_value: str) -> str:
        return hmac.new(
            self.config.hmac_key,
            f"{kind}\0{raw_value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def sign_consent(self, request: AuthorizationRequest) -> str:
        validate_authorization_request(request, self.config)
        now = int(_now().timestamp())
        payload = {
            "v": 1,
            "client_id": request.client_id,
            "redirect_uri": request.redirect_uri,
            "resource": request.resource,
            "scopes": list(request.scopes),
            "code_challenge": request.code_challenge,
            "state": request.state,
            "iat": now,
            "exp": now + self.config.authorization_code_ttl_seconds,
            "nonce": secrets.token_urlsafe(18),
        }
        encoded = _b64url(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64url(
            hmac.new(
                self.config.hmac_key,
                f"consent\0{encoded}".encode(),
                hashlib.sha256,
            ).digest()
        )
        return f"{encoded}.{signature}"

    def verify_consent(self, signed: str) -> AuthorizationRequest:
        try:
            encoded, supplied_signature = signed.split(".", 1)
            expected_signature = _b64url(
                hmac.new(
                    self.config.hmac_key,
                    f"consent\0{encoded}".encode(),
                    hashlib.sha256,
                ).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError
            payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("v") != 1:
                raise ValueError
            if int(payload["exp"]) < int(_now().timestamp()):
                raise OAuthProtocolError("invalid_request", "The consent request expired")
            scopes_raw = payload["scopes"]
            if not isinstance(scopes_raw, list) or not all(
                isinstance(scope, str) for scope in scopes_raw
            ):
                raise ValueError
            state_raw = payload.get("state")
            if state_raw is not None and not isinstance(state_raw, str):
                raise ValueError
            request = AuthorizationRequest(
                client_id=str(payload["client_id"]),
                redirect_uri=str(payload["redirect_uri"]),
                resource=str(payload["resource"]),
                scopes=normalize_scopes(scopes_raw),
                code_challenge=str(payload["code_challenge"]),
                state=state_raw,
            )
        except OAuthProtocolError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OAuthProtocolError("invalid_request", "The consent request is invalid") from exc
        validate_authorization_request(request, self.config)
        return request

    def issue_authorization_code(self, request: AuthorizationRequest) -> str:
        validate_authorization_request(request, self.config)
        raw_code = secrets.token_urlsafe(32)
        now = _now()
        with self._session_factory() as db:
            try:
                begin_write_transaction(db)
                self._delete_expired(db, now)
                db.add(
                    OAuthAuthorizationCode(
                        id=uuid4(),
                        code_digest=self.digest("authorization-code", raw_code),
                        client_id=request.client_id,
                        redirect_uri=request.redirect_uri,
                        resource=request.resource,
                        scopes=_encode_scopes(request.scopes),
                        code_challenge=request.code_challenge,
                        subject=_SUBJECT,
                        credential_generation=self.config.credential_generation,
                        created_at=now,
                        expires_at=now
                        + timedelta(seconds=self.config.authorization_code_ttl_seconds),
                    )
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return raw_code

    def exchange_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        resource: str,
        code_verifier: str,
    ) -> TokenPair:
        self._validate_token_client(client_id, resource)
        try:
            validate_chatgpt_client(client_id, redirect_uri)
        except ValueError as exc:
            raise OAuthProtocolError("invalid_grant", "Authorization-code binding failed") from exc
        if not _CODE_VERIFIER_RE.fullmatch(code_verifier):
            raise OAuthProtocolError("invalid_grant", "The PKCE verifier is invalid")

        now = _now()
        digest = self.digest("authorization-code", code)
        with self._session_factory() as db:
            try:
                begin_write_transaction(db)
                stored = db.scalar(
                    select(OAuthAuthorizationCode)
                    .where(OAuthAuthorizationCode.code_digest == digest)
                    .with_for_update()
                )
                if (
                    stored is None
                    or stored.consumed_at is not None
                    or _aware(stored.expires_at) <= now
                    or stored.credential_generation != self.config.credential_generation
                    or stored.client_id != client_id
                    or stored.redirect_uri != redirect_uri
                    or stored.resource != resource
                ):
                    raise OAuthProtocolError("invalid_grant", "The authorization code is invalid")
                computed_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
                if not hmac.compare_digest(computed_challenge, stored.code_challenge):
                    raise OAuthProtocolError("invalid_grant", "The PKCE verifier is invalid")

                stored.consumed_at = now
                pair = self._issue_token_pair(
                    db,
                    now=now,
                    client_id=stored.client_id,
                    resource=stored.resource,
                    scopes=_decode_scopes(stored.scopes),
                    subject=stored.subject,
                )
                db.commit()
                return pair
            except OAuthProtocolError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise

    def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        resource: str,
        requested_scopes: tuple[str, ...] | None = None,
    ) -> TokenPair:
        self._validate_token_client(client_id, resource)
        now = _now()
        digest = self.digest("refresh-token", refresh_token)
        with self._session_factory() as db:
            try:
                begin_write_transaction(db)
                stored = db.scalar(
                    select(OAuthRefreshToken)
                    .where(OAuthRefreshToken.token_digest == digest)
                    .with_for_update()
                )
                if stored is None:
                    raise OAuthProtocolError("invalid_grant", "The refresh token is invalid")
                family = db.scalar(
                    select(OAuthTokenFamily)
                    .where(OAuthTokenFamily.id == stored.family_id)
                    .with_for_update()
                )
                if family is None:
                    raise OAuthProtocolError("invalid_grant", "The refresh token is invalid")
                if stored.consumed_at is not None:
                    family.revoked_at = family.revoked_at or now
                    family.compromise_detected_at = family.compromise_detected_at or now
                    db.commit()
                    raise OAuthProtocolError(
                        "invalid_grant",
                        "Refresh-token replay detected; authorization was revoked",
                    )
                if (
                    stored.revoked_at is not None
                    or family.revoked_at is not None
                    or _aware(stored.expires_at) <= now
                    or _aware(family.expires_at) <= now
                    or family.credential_generation != self.config.credential_generation
                    or family.client_id != client_id
                    or family.resource != resource
                ):
                    raise OAuthProtocolError("invalid_grant", "The refresh token is invalid")

                current_scopes = _decode_scopes(stored.scopes)
                scopes = current_scopes
                if requested_scopes is not None:
                    scopes = normalize_scopes(requested_scopes)
                    if not set(scopes).issubset(current_scopes):
                        raise OAuthProtocolError(
                            "invalid_scope",
                            "A refresh cannot increase the original token scopes",
                        )

                stored.consumed_at = now
                pair = self._rotate_token_pair(db, family, now=now, scopes=scopes)
                db.commit()
                return pair
            except OAuthProtocolError:
                if db.in_transaction():
                    db.rollback()
                raise
            except Exception:
                db.rollback()
                raise

    def verify_access_token(self, token: str) -> VerifiedToken | None:
        if not token:
            return None
        now = _now()
        digest = self.digest("access-token", token)
        with self._session_factory() as db:
            row = db.execute(
                select(OAuthAccessToken, OAuthTokenFamily)
                .join(OAuthTokenFamily, OAuthTokenFamily.id == OAuthAccessToken.family_id)
                .where(OAuthAccessToken.token_digest == digest)
            ).one_or_none()
            if row is None:
                return None
            access, family = row
            if (
                access.revoked_at is not None
                or family.revoked_at is not None
                or _aware(access.expires_at) <= now
                or _aware(family.expires_at) <= now
                or family.credential_generation != self.config.credential_generation
                or family.resource != self.config.resource
            ):
                return None
            return VerifiedToken(
                client_id=family.client_id,
                scopes=_decode_scopes(access.scopes),
                expires_at=int(_aware(access.expires_at).timestamp()),
                resource=family.resource,
                subject=family.subject,
            )

    def revoke(self, token: str) -> None:
        if not token:
            return
        now = _now()
        access_digest = self.digest("access-token", token)
        refresh_digest = self.digest("refresh-token", token)
        with self._session_factory() as db:
            try:
                begin_write_transaction(db)
                access = db.scalar(
                    select(OAuthAccessToken)
                    .where(OAuthAccessToken.token_digest == access_digest)
                    .with_for_update()
                )
                if access is not None:
                    access.revoked_at = access.revoked_at or now
                    family = db.get(OAuthTokenFamily, access.family_id)
                    if family is not None:
                        family.revoked_at = family.revoked_at or now
                refresh = db.scalar(
                    select(OAuthRefreshToken)
                    .where(OAuthRefreshToken.token_digest == refresh_digest)
                    .with_for_update()
                )
                if refresh is not None:
                    refresh.revoked_at = refresh.revoked_at or now
                    family = db.get(OAuthTokenFamily, refresh.family_id)
                    if family is not None:
                        family.revoked_at = family.revoked_at or now
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _issue_token_pair(
        self,
        db: Session,
        *,
        now: datetime,
        client_id: str,
        resource: str,
        scopes: tuple[str, ...],
        subject: str,
    ) -> TokenPair:
        family = OAuthTokenFamily(
            id=uuid4(),
            client_id=client_id,
            resource=resource,
            scopes=_encode_scopes(scopes),
            subject=subject,
            credential_generation=self.config.credential_generation,
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.refresh_token_ttl_seconds),
        )
        db.add(family)
        # No ORM relationship is needed for these security records, so flush the
        # parent explicitly before inserting the FK-bound opaque tokens.
        db.flush()
        return self._rotate_token_pair(db, family, now=now, scopes=scopes)

    def _rotate_token_pair(
        self,
        db: Session,
        family: OAuthTokenFamily,
        *,
        now: datetime,
        scopes: tuple[str, ...],
    ) -> TokenPair:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        access_expires_at = now + timedelta(seconds=self.config.access_token_ttl_seconds)
        refresh_expires_at = now + timedelta(seconds=self.config.refresh_token_ttl_seconds)
        family.expires_at = refresh_expires_at
        db.add(
            OAuthAccessToken(
                id=uuid4(),
                token_digest=self.digest("access-token", access_token),
                family_id=family.id,
                scopes=_encode_scopes(scopes),
                created_at=now,
                expires_at=access_expires_at,
            )
        )
        db.add(
            OAuthRefreshToken(
                id=uuid4(),
                token_digest=self.digest("refresh-token", refresh_token),
                family_id=family.id,
                scopes=_encode_scopes(scopes),
                created_at=now,
                expires_at=refresh_expires_at,
            )
        )
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.config.access_token_ttl_seconds,
            scopes=scopes,
        )

    def _validate_token_client(self, client_id: str, resource: str) -> None:
        try:
            validate_chatgpt_client_id(client_id)
        except ValueError as exc:
            raise OAuthProtocolError("invalid_client", "The OAuth client is invalid") from exc
        if resource != self.config.resource:
            raise OAuthProtocolError("invalid_target", "The OAuth resource is not this MCP server")

    def _delete_expired(self, db: Session, now: datetime) -> None:
        db.execute(delete(OAuthAuthorizationCode).where(OAuthAuthorizationCode.expires_at < now))
        db.execute(delete(OAuthAccessToken).where(OAuthAccessToken.expires_at < now))
        db.execute(delete(OAuthTokenFamily).where(OAuthTokenFamily.expires_at < now))


def parse_authorization_request(
    values: dict[str, str | None],
    config: OAuthConfig,
) -> AuthorizationRequest:
    if values.get("response_type") != "code":
        raise OAuthProtocolError(
            "unsupported_response_type",
            "Only response_type=code is supported",
        )
    if values.get("code_challenge_method") != "S256":
        raise OAuthProtocolError("invalid_request", "PKCE code_challenge_method must be S256")
    required = ("client_id", "redirect_uri", "resource", "scope", "code_challenge")
    if any(not values.get(name) for name in required):
        raise OAuthProtocolError("invalid_request", "The authorization request is incomplete")
    request = AuthorizationRequest(
        client_id=str(values["client_id"]),
        redirect_uri=str(values["redirect_uri"]),
        resource=str(values["resource"]),
        scopes=normalize_scopes(str(values["scope"])),
        code_challenge=str(values["code_challenge"]),
        state=values.get("state"),
    )
    validate_authorization_request(request, config)
    return request
