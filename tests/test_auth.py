import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rolling_budget_api.core.auth import require_admin, require_read, require_write
from rolling_budget_api.core.config import Settings, get_settings

MASTER_KEY = "synthetic-master-key-at-least-32-characters"
READ_KEY = "synthetic-read-key-at-least-32-characters"
WRITE_KEY = "synthetic-write-key-at-least-32-characters"
ADMIN_KEY = "synthetic-admin-key-at-least-32-characters"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"API_KEY": MASTER_KEY}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _auth_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/read")
    def read(_: object = Depends(require_read)) -> dict[str, bool]:  # noqa: B008
        return {"ok": True}

    @app.post("/write")
    def write(_: object = Depends(require_write)) -> dict[str, bool]:  # noqa: B008
        return {"ok": True}

    @app.post("/admin")
    def admin(_: object = Depends(require_admin)) -> dict[str, bool]:  # noqa: B008
        return {"ok": True}

    return app


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_missing_and_invalid_tokens_are_unauthorized() -> None:
    client = TestClient(_auth_app(_settings()))

    missing = client.get("/read")
    invalid = client.get("/read", headers=_bearer("not-a-real-token"))

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 401


def test_access_levels_are_hierarchical() -> None:
    settings = _settings(
        BUDGET_READ_API_KEY=READ_KEY,
        BUDGET_WRITE_API_KEY=WRITE_KEY,
        BUDGET_ADMIN_API_KEY=ADMIN_KEY,
    )
    client = TestClient(_auth_app(settings))

    assert client.get("/read", headers=_bearer(READ_KEY)).status_code == 200
    assert client.post("/write", headers=_bearer(READ_KEY)).status_code == 403
    assert client.post("/admin", headers=_bearer(WRITE_KEY)).status_code == 403
    assert client.get("/read", headers=_bearer(ADMIN_KEY)).status_code == 200
    assert client.post("/write", headers=_bearer(ADMIN_KEY)).status_code == 200
    assert client.post("/admin", headers=_bearer(ADMIN_KEY)).status_code == 200


def test_master_api_key_has_full_access_without_role_keys() -> None:
    client = TestClient(_auth_app(_settings()))

    assert client.get("/read", headers=_bearer(MASTER_KEY)).status_code == 200
    assert client.post("/write", headers=_bearer(MASTER_KEY)).status_code == 200
    assert client.post("/admin", headers=_bearer(MASTER_KEY)).status_code == 200


def test_role_keys_are_individually_optional_when_master_key_is_present() -> None:
    settings = _settings(BUDGET_READ_API_KEY=READ_KEY)
    client = TestClient(_auth_app(settings))

    assert settings.budget_write_api_key is None
    assert settings.budget_admin_api_key is None
    assert client.get("/read", headers=_bearer(READ_KEY)).status_code == 200
    assert client.post("/write", headers=_bearer(READ_KEY)).status_code == 403
    assert client.post("/admin", headers=_bearer(MASTER_KEY)).status_code == 200


def test_only_master_key_is_required_and_other_settings_have_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None, API_KEY=MASTER_KEY)

    assert settings.api_key == MASTER_KEY
    assert settings.budget_read_api_key is None
    assert settings.budget_write_api_key is None
    assert settings.budget_admin_api_key is None
    assert settings.database_url == "sqlite:////data/budget.db"
    assert settings.cors_allowed_origins == ["*"]
    assert settings.max_batch_items == 250
    assert settings.max_request_bytes == 262_144
    assert settings.mcp_max_request_bytes == 524_288
    assert settings.stale_after_hours == 36
    assert settings.public_base_url is None
    assert settings.mcp_public_url is None


def test_public_base_url_enables_a_canonical_mcp_resource() -> None:
    settings = _settings(PUBLIC_BASE_URL="https://budget.example.com/")

    assert settings.public_base_url == "https://budget.example.com"
    assert settings.mcp_public_url == "https://budget.example.com/mcp"
    assert settings.oauth_owner_secret == MASTER_KEY


@pytest.mark.parametrize(
    "url",
    [
        "http://budget.example.com",
        "https://user:secret@budget.example.com",
        "https://budget.example.com/prefix",
        "https://budget.example.com?tenant=owner",
    ],
)
def test_public_base_url_rejects_ambiguous_or_insecure_origins(url: str) -> None:
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
        _settings(PUBLIC_BASE_URL=url)


def test_remote_mcp_role_key_mode_requires_a_separate_consent_secret() -> None:
    role_keys = {
        "BUDGET_READ_API_KEY": READ_KEY,
        "BUDGET_WRITE_API_KEY": WRITE_KEY,
        "BUDGET_ADMIN_API_KEY": ADMIN_KEY,
        "PUBLIC_BASE_URL": "https://budget.example.com",
    }
    with pytest.raises(ValidationError, match="OAUTH_CONSENT_SECRET"):
        Settings(_env_file=None, API_KEY=None, **role_keys)

    settings = Settings(
        _env_file=None,
        API_KEY=None,
        **role_keys,
        OAUTH_CONSENT_SECRET="synthetic-consent-secret-at-least-32-characters",
    )
    assert settings.oauth_owner_secret == "synthetic-consent-secret-at-least-32-characters"


def test_legacy_three_role_key_configuration_remains_supported() -> None:
    settings = Settings.model_validate(
        {
            "API_KEY": None,
            "BUDGET_READ_API_KEY": READ_KEY,
            "BUDGET_WRITE_API_KEY": WRITE_KEY,
            "BUDGET_ADMIN_API_KEY": ADMIN_KEY,
        }
    )
    client = TestClient(_auth_app(settings))

    assert settings.api_key is None
    assert client.get("/read", headers=_bearer(READ_KEY)).status_code == 200
    assert client.post("/write", headers=_bearer(WRITE_KEY)).status_code == 200
    assert client.post("/admin", headers=_bearer(ADMIN_KEY)).status_code == 200


def test_api_keys_must_be_distinct_to_prevent_privilege_collapse() -> None:
    shared = "synthetic-shared-key-at-least-32-characters"

    with pytest.raises(ValidationError, match="must be different"):
        Settings.model_validate(
            {
                "API_KEY": shared,
                "BUDGET_READ_API_KEY": shared,
                "BUDGET_WRITE_API_KEY": WRITE_KEY,
                "BUDGET_ADMIN_API_KEY": "synthetic-admin-key-at-least-32-characters",
            }
        )


def test_missing_master_requires_all_three_legacy_role_keys() -> None:
    with pytest.raises(ValidationError, match="API_KEY is required"):
        Settings.model_validate(
            {
                "API_KEY": None,
                "BUDGET_READ_API_KEY": READ_KEY,
                "BUDGET_WRITE_API_KEY": WRITE_KEY,
                "BUDGET_ADMIN_API_KEY": None,
            }
        )
