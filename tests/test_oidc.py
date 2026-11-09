"""Tests voor Authentik forward_auth headers en de bijbehorende dependencies.

Sinds april 2026 doet de backend geen JWT/JWKS-validatie meer; auth wordt
volledig door Caddy + Authentik proxy outpost afgehandeld en als
``X-Authentik-*`` headers naar deze service gestuurd. Deze test-suite
checkt de header-parsing, user provisioning en profiel-sync.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from openaec_reports.auth.dependencies import _authenticate_via_authentik
from openaec_reports.auth.models import User, UserDB, UserRole
from openaec_reports.auth.oidc import (
    HEADER_COMPANY,
    HEADER_EMAIL,
    HEADER_GROUPS,
    HEADER_JOB_TITLE,
    HEADER_NAME,
    HEADER_PHONE,
    HEADER_REGISTRATION_NUMBER,
    HEADER_TENANT,
    HEADER_UID,
    HEADER_USERNAME,
    AuthentikHeaders,
    is_authentik_enabled,
    parse_authentik_headers,
)
from openaec_reports.auth.security import hash_password

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture()
def tmp_db(tmp_path):
    """Verse UserDB in tijdelijke directory."""
    return UserDB(db_path=tmp_path / "test_auth.db")


def _headers(**overrides) -> dict[str, str]:
    """Bouw een complete set Authentik forward_auth headers.

    Args:
        **overrides: Per-header overschrijving (default `_headers()` levert
            de baseline ``test-3bm`` user op).

    Returns:
        Dict van header → waarde.
    """
    base = {
        HEADER_USERNAME: "test-3bm",
        HEADER_UID: "ak-uid-123",
        HEADER_EMAIL: "test-3bm@open-aec.com",
        HEADER_NAME: "Test 3BM User",
        HEADER_GROUPS: "openaec-users|tenant-3bm",
        HEADER_TENANT: "3bm",
        HEADER_COMPANY: "3BM Bouwkunde",
        HEADER_JOB_TITLE: "Engineer",
        HEADER_PHONE: "+31 6 1234 5678",
        HEADER_REGISTRATION_NUMBER: "REG-001",
    }
    base.update(overrides)
    return base


# ============================================================
# Header parsing
# ============================================================


class TestParseHeaders:
    """Tests voor parse_authentik_headers()."""

    def test_full_headers(self):
        """Volledige set forward_auth headers → AuthentikHeaders dataclass."""
        parsed = parse_authentik_headers(_headers())
        assert parsed is not None
        assert isinstance(parsed, AuthentikHeaders)
        assert parsed.subject == "ak-uid-123"
        assert parsed.username == "test-3bm"
        assert parsed.email == "test-3bm@open-aec.com"
        assert parsed.name == "Test 3BM User"
        assert parsed.groups == ["openaec-users", "tenant-3bm"]
        assert parsed.tenant == "3bm"
        assert parsed.company == "3BM Bouwkunde"
        assert parsed.job_title == "Engineer"
        assert parsed.phone == "+31 6 1234 5678"
        assert parsed.registration_number == "REG-001"

    def test_missing_username_returns_none(self):
        """Zonder X-Authentik-Username → None (= 401 in dependency)."""
        headers = _headers()
        del headers[HEADER_USERNAME]
        assert parse_authentik_headers(headers) is None

    def test_blank_username_returns_none(self):
        """Lege username header (whitespace) → None."""
        assert parse_authentik_headers(_headers(**{HEADER_USERNAME: "   "})) is None

    def test_subject_falls_back_to_username(self):
        """Zonder X-Authentik-Uid wordt de username de stabiele subject."""
        headers = _headers()
        del headers[HEADER_UID]
        parsed = parse_authentik_headers(headers)
        assert parsed is not None
        assert parsed.subject == "test-3bm"

    def test_groups_pipe_split(self):
        """Groups header wordt op pipe-character gesplitst."""
        parsed = parse_authentik_headers(_headers(**{HEADER_GROUPS: "g1|g2|g3"}))
        assert parsed is not None
        assert parsed.groups == ["g1", "g2", "g3"]

    def test_empty_groups(self):
        """Lege groups header → lege lijst."""
        parsed = parse_authentik_headers(_headers(**{HEADER_GROUPS: ""}))
        assert parsed is not None
        assert parsed.groups == []


# ============================================================
# is_authentik_enabled
# ============================================================


class TestAuthentikEnabled:
    """Tests voor is_authentik_enabled()."""

    def test_default_enabled(self, monkeypatch):
        """Zonder env override is Authentik forward_auth de default."""
        monkeypatch.delenv("OPENAEC_AUTH_MODE", raising=False)
        assert is_authentik_enabled() is True

    def test_explicit_authentik(self, monkeypatch):
        monkeypatch.setenv("OPENAEC_AUTH_MODE", "authentik")
        assert is_authentik_enabled() is True

    def test_local_disables(self, monkeypatch):
        monkeypatch.setenv("OPENAEC_AUTH_MODE", "local")
        assert is_authentik_enabled() is False


# ============================================================
# Dependency — _authenticate_via_authentik
# ============================================================


class _FakeRequest:
    """Minimal stand-in voor FastAPI Request met alleen .headers."""

    def __init__(self, headers: dict[str, str]):
        # Starlette accepteert dicts als case-insensitive mapping in tests
        # door .get() lookups; emuleer dat:
        self._headers = {k.lower(): v for k, v in headers.items()}

    @property
    def headers(self):
        return _CaseInsensitiveHeaders(self._headers)


class _CaseInsensitiveHeaders:
    def __init__(self, store: dict[str, str]):
        self._store = store

    def get(self, key: str, default: str = "") -> str:
        return self._store.get(key.lower(), default)


class TestAuthentikAuthentication:
    """Tests voor _authenticate_via_authentik() dependency."""

    @pytest.fixture(autouse=True)
    def _isolate_db(self, tmp_path, monkeypatch):
        """Forceer een schone DB per test om global state te vermijden."""
        monkeypatch.setenv("OPENAEC_AUTH_DB", str(tmp_path / "test_auth.db"))
        # Re-init dependencies
        from openaec_reports.auth import api_keys, dependencies, models

        db = models.UserDB(db_path=tmp_path / "test_auth.db")
        dependencies.init_user_db(db)
        dependencies.init_api_key_db(api_keys.ApiKeyDB(db_path=tmp_path / "test_auth.db"))
        dependencies.init_organisation_db(models.OrganisationDB(db_path=tmp_path / "test_auth.db"))
        yield db

    def test_auto_provisions_new_user(self, _isolate_db):
        """Onbekende Authentik user → auto-create in lokale DB."""
        db: UserDB = _isolate_db
        request = _FakeRequest(_headers())

        user = _authenticate_via_authentik(request)

        assert user is not None
        assert user.username == "test-3bm"
        assert user.email == "test-3bm@open-aec.com"
        assert user.tenant == "3bm"
        assert user.auth_provider == "authentik"
        assert user.oidc_subject == "ak-uid-123"

        stored = db.get_by_oidc_subject("ak-uid-123")
        assert stored is not None
        assert stored.username == "test-3bm"

    def test_returns_existing_user_on_subject_match(self, _isolate_db):
        """Bekende subject → bestaande user wordt teruggegeven (geen create)."""
        db: UserDB = _isolate_db
        existing = db.create(
            User(
                id=uuid.uuid4().hex,
                username="alice",
                email="alice@open-aec.com",
                display_name="Alice",
                role=UserRole.user,
                hashed_password=hash_password("irrelevant"),
                auth_provider="authentik",
                oidc_subject="ak-uid-alice",
            )
        )
        request = _FakeRequest(_headers(**{HEADER_UID: "ak-uid-alice", HEADER_USERNAME: "alice"}))

        user = _authenticate_via_authentik(request)
        assert user is not None
        assert user.id == existing.id

    def test_email_fallback_links_local_user(self, _isolate_db):
        """Bestaande lokale user met matchend email → koppelen aan subject."""
        db: UserDB = _isolate_db
        local = db.create(
            User(
                id=uuid.uuid4().hex,
                username="bob-local",
                email="bob@open-aec.com",
                display_name="Bob",
                role=UserRole.user,
                hashed_password=hash_password("irrelevant"),
                auth_provider="local",
            )
        )
        request = _FakeRequest(
            _headers(
                **{
                    HEADER_UID: "ak-uid-bob",
                    HEADER_USERNAME: "bob",
                    HEADER_EMAIL: "bob@open-aec.com",
                }
            )
        )

        user = _authenticate_via_authentik(request)
        assert user is not None
        assert user.id == local.id
        assert user.oidc_subject == "ak-uid-bob"
        assert user.auth_provider == "authentik"

    def test_profile_sync_updates_changed_fields(self, _isolate_db):
        """Veranderingen in headers worden naar de DB gesynct."""
        db: UserDB = _isolate_db
        request = _FakeRequest(_headers())
        first = _authenticate_via_authentik(request)
        assert first is not None
        assert first.company == "3BM Bouwkunde"

        # Tweede call met nieuwe company-header
        request2 = _FakeRequest(_headers(**{HEADER_COMPANY: "3BM Cooperatie"}))
        second = _authenticate_via_authentik(request2)

        assert second is not None
        assert second.id == first.id
        assert second.company == "3BM Cooperatie"
        # En ook persistent in DB
        assert db.get_by_id(first.id).company == "3BM Cooperatie"

    def test_no_username_returns_none(self, _isolate_db):
        """Geen Authentik-headers → None (dependency rolt door naar 401)."""
        request = _FakeRequest({})
        assert _authenticate_via_authentik(request) is None


# ============================================================
# Integratie via /api/auth/me
# ============================================================


class TestMeEndpointWithAuthentik:
    """End-to-end check: forward_auth headers + /api/auth/me."""

    @pytest.fixture(autouse=True)
    def _force_authentik(self, monkeypatch):
        monkeypatch.setenv("OPENAEC_AUTH_MODE", "authentik")
        monkeypatch.setenv("OPENAEC_LOCAL_AUTH_ENABLED", "false")
        # Tests gebruiken dezelfde shared TestClient app — we hoeven niets
        # te reloaden, headers zijn per request.

    def test_me_with_authentik_headers(self):
        """Met X-Authentik-Username → 200 + user data."""
        from openaec_reports.api import app

        client = TestClient(app)
        r = client.get(
            "/api/auth/me", headers=_headers(**{HEADER_USERNAME: f"hdr-{uuid.uuid4().hex[:6]}"})
        )
        assert r.status_code == 200
        body = r.json()
        assert "user" in body
        assert body["user"]["tenant"] == "3bm"
        assert body["user"]["company"] == "3BM Bouwkunde"

    def test_me_without_headers_returns_401(self, monkeypatch):
        """Zonder headers en zonder cookie → 401."""
        from openaec_reports.api import app

        # Lokale auth uit, dus geen cookie/Bearer fallback mogelijk
        monkeypatch.setenv("OPENAEC_LOCAL_AUTH_ENABLED", "false")
        client = TestClient(app)
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_service_token_returns_501(self):
        """Bearer ak_* tokens worden gemarkeerd als TODO (fase 6)."""
        from openaec_reports.api import app

        client = TestClient(app)
        r = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer ak_dummy_service_token_123"},
        )
        assert r.status_code == 501
        assert "fase 6" in r.json()["detail"].lower()
