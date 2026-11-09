"""Tests voor het authenticatie systeem."""

from __future__ import annotations

import importlib

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient

from openaec_reports.api import app


@pytest.fixture()
def client():
    """Ongeauthenticeerde TestClient."""
    return TestClient(app)


class TestLogin:
    """Tests voor POST /api/auth/login."""

    def test_login_success(self, client, _ensure_test_users):
        """Correcte credentials → 200 + cookie + user data + token."""
        r = client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert r.status_code == 200
        data = r.json()
        assert "user" in data
        assert data["user"]["username"] == TEST_USERNAME
        assert "hashed_password" not in data["user"]

        # Cookie moet gezet zijn
        assert "bm_access_token" in r.cookies

        # Token moet in response body staan (voor pyRevit / scripts)
        assert "token" in data
        assert len(data["token"]) > 0

    def test_login_wrong_password(self, client, _ensure_test_users):
        """Fout wachtwoord → 401."""
        r = client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "password": "fout_wachtwoord"},
        )
        assert r.status_code == 401

    def test_login_nonexistent_user(self, client):
        """Onbekende user → 401."""
        r = client.post(
            "/api/auth/login",
            json={"username": "bestaat_niet", "password": "whatever"},
        )
        assert r.status_code == 401

    def test_login_missing_fields(self, client):
        """Lege body → 422."""
        r = client.post("/api/auth/login", json={})
        assert r.status_code == 422


class TestLogout:
    """Tests voor POST /api/auth/logout."""

    def test_logout_clears_cookie(self, authenticated_client):
        """Logout verwijdert de cookie."""
        r = authenticated_client.post("/api/auth/logout")
        assert r.status_code == 200

        # Na logout: /me moet 401 geven
        r2 = authenticated_client.get("/api/auth/me")
        assert r2.status_code == 401


class TestMe:
    """Tests voor GET /api/auth/me."""

    def test_me_authenticated(self, authenticated_client):
        """Ingelogde user → user data."""
        r = authenticated_client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["user"]["username"] == TEST_USERNAME

    def test_me_unauthenticated(self, client):
        """Zonder cookie → 401."""
        r = client.get("/api/auth/me")
        assert r.status_code == 401


class TestProtectedEndpoints:
    """Tests dat business endpoints authenticatie vereisen."""

    def test_health_open(self, client):
        """/api/health is ALTIJD open."""
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_templates_requires_auth(self, client):
        """GET /api/templates zonder auth → 401."""
        r = client.get("/api/templates")
        assert r.status_code == 401

    def test_templates_with_auth(self, authenticated_client):
        """GET /api/templates met auth → 200."""
        r = authenticated_client.get("/api/templates")
        assert r.status_code == 200
        assert "templates" in r.json()

    def test_brands_requires_auth(self, client):
        """GET /api/brands zonder auth → 401."""
        r = client.get("/api/brands")
        assert r.status_code == 401

    def test_brands_with_auth(self, authenticated_client):
        """GET /api/brands met auth → 200."""
        r = authenticated_client.get("/api/brands")
        assert r.status_code == 200

    def test_validate_requires_auth(self, client):
        """POST /api/validate zonder auth → 401."""
        r = client.post("/api/validate", json={"template": "test", "project": "Test"})
        assert r.status_code == 401

    def test_generate_requires_auth(self, client):
        """POST /api/generate zonder auth → 401."""
        r = client.post(
            "/api/generate",
            json={"template": "structural", "project": "Test", "sections": []},
        )
        assert r.status_code == 401

    def test_stationery_requires_auth(self, client):
        """GET /api/stationery zonder auth → 401."""
        r = client.get("/api/stationery")
        assert r.status_code == 401


class TestBearerToken:
    """Tests voor Bearer token authenticatie (pyRevit / scripts)."""

    def test_bearer_token_from_login(self, client, _ensure_test_users):
        """Login → token uit body → gebruik als Bearer header."""
        # Login en haal token op
        r = client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        token = r.json()["token"]

        # Nieuw request zonder cookies, met Bearer token
        bare_client = TestClient(app)
        r2 = bare_client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200
        assert r2.json()["user"]["username"] == TEST_USERNAME

    def test_bearer_token_on_protected_endpoint(self, client, _ensure_test_users):
        """Bearer token werkt op business endpoints."""
        r = client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        token = r.json()["token"]

        bare_client = TestClient(app)
        r2 = bare_client.get(
            "/api/templates",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200
        assert "templates" in r2.json()

    def test_invalid_bearer_token_returns_401(self, client):
        """Ongeldig Bearer token → 401."""
        r = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer ongeldig.token.hier"},
        )
        assert r.status_code == 401

    def test_no_auth_returns_401(self, client):
        """Geen cookie en geen Bearer → 401."""
        r = client.get("/api/auth/me")
        assert r.status_code == 401


class TestSsoOnly:
    """Tests dat lokale login standaard uitgeschakeld is."""

    def test_login_blocked_when_local_auth_disabled(self, client, _ensure_test_users, monkeypatch):
        """Login endpoint geeft 403 als OPENAEC_LOCAL_AUTH_ENABLED niet 'true' is."""
        monkeypatch.setenv("OPENAEC_LOCAL_AUTH_ENABLED", "false")
        r = client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert r.status_code == 403
        assert "SSO" in r.json()["detail"]

    def test_register_blocked_when_local_auth_disabled(self, client, monkeypatch):
        """Register endpoint geeft 403 als OPENAEC_LOCAL_AUTH_ENABLED niet 'true' is."""
        monkeypatch.setenv("OPENAEC_LOCAL_AUTH_ENABLED", "false")
        r = client.post(
            "/api/auth/register",
            json={
                "username": "nieuw",
                "email": "nieuw@test.nl",
                "password": "wachtwoord123",
            },
        )
        assert r.status_code == 403
        assert "SSO" in r.json()["detail"]

    def test_registration_enabled_returns_false_when_local_auth_disabled(self, client, monkeypatch):
        """Registration-enabled endpoint geeft false als lokale auth uit staat."""
        monkeypatch.setenv("OPENAEC_LOCAL_AUTH_ENABLED", "false")
        r = client.get("/api/auth/registration-enabled")
        assert r.json()["enabled"] is False


class TestJwtSecretEnforcement:
    """Tests voor CR-K2 — JWT default secret enforcement."""

    def test_production_raises_with_default_secret(self, monkeypatch):
        """In productie moet een RuntimeError worden gegooid bij default secret."""
        monkeypatch.setenv("OPENAEC_ENV", "production")
        monkeypatch.delenv("OPENAEC_JWT_SECRET", raising=False)

        from openaec_reports.auth import security
        importlib.reload(security)

        with pytest.raises(RuntimeError, match="default waarde in productie"):
            security.enforce_jwt_secret()

    def test_development_warns_with_default_secret(self, monkeypatch, caplog):
        """In development wordt alleen een warning gelogd."""
        monkeypatch.setenv("OPENAEC_ENV", "development")
        monkeypatch.delenv("OPENAEC_JWT_SECRET", raising=False)

        from openaec_reports.auth import security
        importlib.reload(security)

        import logging
        with caplog.at_level(logging.WARNING, logger="openaec_reports.auth.security"):
            security.enforce_jwt_secret()

        assert "default waarde" in caplog.text

    def test_custom_secret_no_error(self, monkeypatch):
        """Met een custom secret wordt geen error/warning gegeven."""
        monkeypatch.setenv("OPENAEC_ENV", "production")
        monkeypatch.setenv("OPENAEC_JWT_SECRET", "mijn-super-veilige-secret-123")

        from openaec_reports.auth import security
        importlib.reload(security)

        # Moet zonder exception doorlopen
        security.enforce_jwt_secret()
