import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rolling_budget_api.core.config import Settings
from rolling_budget_api.main import create_app

MASTER_KEY = "synthetic-master-key-at-least-32-characters"
ORIGIN = "https://kitchen.synthetic.test"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"API_KEY": MASTER_KEY}
    values.update(overrides)
    return Settings.model_validate(values)


def _preflight(client: TestClient, origin: str = ORIGIN):
    return client.options(
        "/v1/config",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": (
                "authorization,content-type,idempotency-key,if-match,x-request-id"
            ),
        },
    )


def test_default_cors_allows_any_origin_and_all_api_headers() -> None:
    client = TestClient(create_app(_settings()))

    response = _preflight(client)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    allowed_methods = {
        item.strip() for item in response.headers["access-control-allow-methods"].split(",")
    }
    assert {"GET", "PUT", "POST"} <= allowed_methods
    allowed_headers = {
        item.strip().lower()
        for item in response.headers["access-control-allow-headers"].split(",")
    }
    assert {
        "authorization",
        "content-type",
        "idempotency-key",
        "if-match",
        "x-request-id",
    } <= allowed_headers


def test_cors_exposes_etag_and_request_id_to_browser_clients() -> None:
    client = TestClient(create_app(_settings()))

    response = client.get(
        "/health/live",
        headers={"Origin": ORIGIN, "X-Request-ID": "synthetic-request-id"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    exposed = {
        item.strip().lower()
        for item in response.headers["access-control-expose-headers"].split(",")
    }
    assert {"etag", "x-request-id"} <= exposed
    assert response.headers["x-request-id"] == "synthetic-request-id"


def test_exact_origin_override_allows_only_configured_origins() -> None:
    settings = _settings(
        CORS_ALLOWED_ORIGINS=(
            "https://kitchen.synthetic.test,https://calendar.synthetic.test"
        )
    )
    client = TestClient(create_app(settings))

    allowed = _preflight(client, "https://kitchen.synthetic.test")
    blocked = _preflight(client, "https://untrusted.synthetic.test")

    assert settings.cors_allowed_origins == [
        "https://kitchen.synthetic.test",
        "https://calendar.synthetic.test",
    ]
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == (
        "https://kitchen.synthetic.test"
    )
    assert "access-control-allow-origin" not in blocked.headers


def test_wildcard_cannot_be_mixed_with_exact_origins() -> None:
    with pytest.raises(ValidationError, match="cannot mix"):
        _settings(CORS_ALLOWED_ORIGINS=f"*,{ORIGIN}")


def test_environment_variables_override_non_secret_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", MASTER_KEY)
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/synthetic-budget.db")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("MAX_BATCH_ITEMS", "125")
    monkeypatch.setenv("MAX_REQUEST_BYTES", "131072")
    monkeypatch.setenv("STALE_AFTER_HOURS", "12")
    monkeypatch.setenv("BUDGET_READ_API_KEY", "")

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite:////tmp/synthetic-budget.db"
    assert settings.cors_allowed_origins == [ORIGIN]
    assert settings.max_batch_items == 125
    assert settings.max_request_bytes == 131_072
    assert settings.stale_after_hours == 12
    assert settings.budget_read_api_key is None
