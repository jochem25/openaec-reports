"""Tests voor API Key authenticatie en admin endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from conftest import TEST_ADMIN_USERNAME, TEST_USERNAME
from fastapi.testclient import TestClient

from openaec_reports.api import app
from openaec_reports.auth.api_keys import API_KEY_PREFIX, generate_api_key, hash_api_key
from openaec_reports.auth.dependencies import get_api_key_db, get_user_db
from openaec_reports.auth.models import User, UserRole
from openaec_reports.auth.security import hash_password


@pytest.fixture()
def client():
    """Ongeauthenticeerde TestClient."""
    return TestClient(app)


# ============================================================
# Unit tests: api_keys module
# ============================================================


class TestGenerateApiKey:
    """Tests voor API key generatie."""

    def test_key_has_prefix(self):
        """Gegenereerde key begint met oaec_k_ prefix."""
        key = generate_api_key()
        assert key.startswith("oaec_k_")

    def test_key_has_sufficient_length(self):
        """Key is lang genoeg (prefix + 64 hex chars)."""
        key = generate_api_key()
        assert key.startswith(API_KEY_PREFIX)
        assert len(key) == len(API_KEY_PREFIX) + 64  # "oaec_k_" + 32 bytes hex

    def test_keys_are_unique(self):
        """Twee gegenereerde keys zijn verschillend."""
        k1 = generate_api_key()
        k2 = generate_api_key()
        assert k1 != k2


class TestHashApiKey:
    """Tests voor API key hashing."""

    def test_hash_is_deterministic(self):
        """Dezelfde key geeft dezelfde hash."""
        key = "oaec_k_test123"
        assert hash_api_key(key) == hash_api_key(key)

    def test_different_keys_different_hashes(self):
        """Verschillende keys geven verschillende hashes."""
        assert hash_api_key("oaec_k_aaa") != hash_api_key("oaec_k_bbb")

    def test_hash_is_hex_string(self):
        """Hash is een geldige hex string van 64 chars (SHA-256)."""
        h = hash_api_key("test")
        assert len(h) == 64
        int(h, 16)  # Moet geen ValueError geven


# ============================================================
# ApiKeyDB CRUD tests
# ============================================================


class TestApiKeyDB:
    """Tests voor ApiKeyDB CRUD operaties."""

    def test_create_returns_key_and_plaintext(self, _ensure_test_users):
        """create() retourneert ApiKey object + plaintext key."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        api_key, plaintext = db.create(
            name="test-key",
            user_id=admin.id,
        )
        assert api_key.name == "test-key"
        assert api_key.user_id == admin.id
        assert api_key.is_active is True
        assert plaintext.startswith("oaec_k_")

        # Cleanup
        db.delete(api_key.id)

    def test_created_at_is_populated(self, _ensure_test_users):
        """create() vult created_at in op het Python object."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        api_key, _ = db.create(name="ts-test", user_id=admin.id)
        assert api_key.created_at != ""
        # Moet een geldige ISO datetime zijn
        datetime.fromisoformat(api_key.created_at)

        db.delete(api_key.id)

    def test_get_by_key_returns_valid_key(self, _ensure_test_users):
        """get_by_key() vindt een key op basis van plaintext."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        api_key, plaintext = db.create(
            name="lookup-test",
            user_id=admin.id,
        )

        found = db.get_by_key(plaintext)
        assert found is not None
        assert found.id == api_key.id
        assert found.name == "lookup-test"

        db.delete(api_key.id)

    def test_get_by_key_unknown_returns_none(self):
        """get_by_key() met onbekende key retourneert None."""
        db = get_api_key_db()
        result = db.get_by_key("oaec_k_doesnotexist123456")
        assert result is None

    def test_get_by_key_expired_returns_none(self, _ensure_test_users):
        """get_by_key() retourneert None voor verlopen keys."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()

        api_key, plaintext = db.create(
            name="expired-key",
            user_id=admin.id,
            expires_at=yesterday,
        )

        result = db.get_by_key(plaintext)
        assert result is None

        db.delete(api_key.id)

    def test_get_by_key_future_expiry_returns_key(self, _ensure_test_users):
        """get_by_key() retourneert key als expiry in de toekomst ligt."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        tomorrow = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()

        api_key, plaintext = db.create(
            name="future-key",
            user_id=admin.id,
            expires_at=tomorrow,
        )

        result = db.get_by_key(plaintext)
        assert result is not None
        assert result.id == api_key.id

        db.delete(api_key.id)

    def test_list_by_user(self, _ensure_test_users):
        """list_by_user() retourneert alleen keys van die user."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        key1, _ = db.create(name="list-a", user_id=admin.id)
        key2, _ = db.create(name="list-b", user_id=admin.id)

        keys = db.list_by_user(admin.id)
        key_ids = {k.id for k in keys}
        assert key1.id in key_ids
        assert key2.id in key_ids

        db.delete(key1.id)
        db.delete(key2.id)

    def test_list_all(self, _ensure_test_users):
        """list_all() retourneert alle keys."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        key, _ = db.create(name="all-test", user_id=admin.id)
        all_keys = db.list_all()
        assert any(k.id == key.id for k in all_keys)

        db.delete(key.id)

    def test_revoke_deactivates_key(self, _ensure_test_users):
        """revoke() deactiveert een key (soft delete)."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        api_key, plaintext = db.create(
            name="revoke-test",
            user_id=admin.id,
        )

        assert db.revoke(api_key.id) is True
        # Key mag niet meer gevonden worden
        assert db.get_by_key(plaintext) is None

        db.delete(api_key.id)

    def test_revoke_nonexistent_returns_false(self):
        """revoke() voor onbekende key retourneert False."""
        db = get_api_key_db()
        assert db.revoke("nonexistent_key_id") is False

    def test_delete_removes_key(self, _ensure_test_users):
        """delete() verwijdert een key permanent."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        api_key, _ = db.create(name="del-test", user_id=admin.id)
        assert db.delete(api_key.id) is True

        # Mag niet meer in list_all zitten
        assert not any(k.id == api_key.id for k in db.list_all())

    def test_delete_nonexistent_returns_false(self):
        """delete() voor onbekende key retourneert False."""
        db = get_api_key_db()
        assert db.delete("nonexistent_key_id") is False

    def test_to_dict_excludes_hash(self, _ensure_test_users):
        """to_dict() bevat geen key_hash."""
        db = get_api_key_db()
        user_db = get_user_db()
        admin = user_db.get_by_username(TEST_ADMIN_USERNAME)

        api_key, _ = db.create(name="dict-test", user_id=admin.id)
        d = api_key.to_dict()
        assert "key_hash" not in d
        assert "id" in d
        assert "name" in d
        assert "key_prefix" in d

        db.delete(api_key.id)


# ============================================================
# API Key authenticatie (X-API-Key header)
# ============================================================


class TestApiKeyAuth:
    """Tests voor authenticatie via X-API-Key header."""

    def test_api_key_auth_on_protected_endpoint(
        self, client, _ensure_test_users
    ):
        """Geldig X-API-Key header geeft toegang tot protected endpoints."""
        db = get_api_key_db()
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)

        api_key, plaintext = db.create(
            name="auth-test", user_id=user.id
        )

        r = client.get(
            "/api/templates",
            headers={"X-API-Key": plaintext},
        )
        assert r.status_code == 200
        assert "templates" in r.json()

        db.delete(api_key.id)

    def test_api_key_auth_on_me_endpoint(
        self, client, _ensure_test_users
    ):
        """X-API-Key werkt op /api/auth/me."""
        db = get_api_key_db()
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)

        api_key, plaintext = db.create(
            name="me-test", user_id=user.id
        )

        r = client.get(
            "/api/auth/me",
            headers={"X-API-Key": plaintext},
        )
        assert r.status_code == 200
        assert r.json()["user"]["username"] == TEST_USERNAME

        db.delete(api_key.id)

    def test_invalid_api_key_returns_401(self, client):
        """Ongeldige X-API-Key → 401."""
        r = client.get(
            "/api/templates",
            headers={"X-API-Key": "oaec_k_ongeldige_key_hier"},
        )
        assert r.status_code == 401

    def test_revoked_api_key_returns_401(
        self, client, _ensure_test_users
    ):
        """Ingetrokken API key → 401."""
        db = get_api_key_db()
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)

        api_key, plaintext = db.create(
            name="revoke-auth", user_id=user.id
        )
        db.revoke(api_key.id)

        r = client.get(
            "/api/templates",
            headers={"X-API-Key": plaintext},
        )
        assert r.status_code == 401

        db.delete(api_key.id)

    def test_expired_api_key_returns_401(
        self, client, _ensure_test_users
    ):
        """Verlopen API key → 401."""
        db = get_api_key_db()
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)

        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()

        api_key, plaintext = db.create(
            name="expired-auth",
            user_id=user.id,
            expires_at=yesterday,
        )

        r = client.get(
            "/api/templates",
            headers={"X-API-Key": plaintext},
        )
        assert r.status_code == 401

        db.delete(api_key.id)

    def test_api_key_for_inactive_user_returns_401(
        self, client, _ensure_test_users
    ):
        """API key van gedeactiveerde user → 401."""
        user_db = get_user_db()
        db = get_api_key_db()

        # Maak een tijdelijke user
        unique = uuid.uuid4().hex[:8]
        user = user_db.create(
            User(
                username=f"inactive_{unique}",
                role=UserRole.user,
                hashed_password=hash_password("pw123456"),
            )
        )
        api_key, plaintext = db.create(
            name="inactive-user-key", user_id=user.id
        )

        # Deactiveer de user
        user_db.update(user.id, is_active=False)

        r = client.get(
            "/api/templates",
            headers={"X-API-Key": plaintext},
        )
        assert r.status_code == 401

        db.delete(api_key.id)
        user_db.delete(user.id)

    def test_api_key_takes_priority_over_bearer(
        self, client, _ensure_test_users
    ):
        """Als X-API-Key en Bearer beiden aanwezig zijn, wint API key."""
        db = get_api_key_db()
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)

        api_key, plaintext = db.create(
            name="priority-test", user_id=user.id
        )

        r = client.get(
            "/api/auth/me",
            headers={
                "X-API-Key": plaintext,
                "Authorization": "Bearer ongeldig.token",
            },
        )
        # API key wint, ongeldig Bearer wordt genegeerd
        assert r.status_code == 200
        assert r.json()["user"]["username"] == TEST_USERNAME

        db.delete(api_key.id)


# ============================================================
# Admin API Key endpoints
# ============================================================


class TestAdminApiKeyEndpoints:
    """Tests voor admin CRUD endpoints voor API keys."""

    def test_list_api_keys(self, admin_client):
        """GET /api/admin/api-keys retourneert een lijst."""
        r = admin_client.get("/api/admin/api-keys")
        assert r.status_code == 200
        assert "api_keys" in r.json()
        assert isinstance(r.json()["api_keys"], list)

    def test_create_api_key(self, admin_client, _ensure_test_users):
        """POST /api/admin/api-keys maakt een key aan."""
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)

        r = admin_client.post("/api/admin/api-keys", json={
            "name": "admin-created-key",
            "user_id": user.id,
        })
        assert r.status_code == 201
        data = r.json()
        assert "api_key" in data
        assert "plaintext_key" in data
        assert data["api_key"]["name"] == "admin-created-key"
        assert data["plaintext_key"].startswith("oaec_k_")
        assert data["api_key"]["created_at"] != ""

        # Cleanup
        get_api_key_db().delete(data["api_key"]["id"])

    def test_create_api_key_nonexistent_user(self, admin_client):
        """POST /api/admin/api-keys voor onbekende user → 404."""
        r = admin_client.post("/api/admin/api-keys", json={
            "name": "ghost-key",
            "user_id": "nonexistent_user_id",
        })
        assert r.status_code == 404

    def test_create_api_key_with_expiry(
        self, admin_client, _ensure_test_users
    ):
        """POST /api/admin/api-keys met verloopdatum."""
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)
        future = (
            datetime.now(timezone.utc) + timedelta(days=30)
        ).isoformat()

        r = admin_client.post("/api/admin/api-keys", json={
            "name": "expiring-key",
            "user_id": user.id,
            "expires_at": future,
        })
        assert r.status_code == 201
        assert r.json()["api_key"]["expires_at"] == future

        get_api_key_db().delete(r.json()["api_key"]["id"])

    def test_revoke_api_key(self, admin_client, _ensure_test_users):
        """POST /api/admin/api-keys/{id}/revoke deactiveert de key."""
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)
        db = get_api_key_db()

        api_key, plaintext = db.create(
            name="to-revoke", user_id=user.id
        )

        r = admin_client.post(
            f"/api/admin/api-keys/{api_key.id}/revoke"
        )
        assert r.status_code == 200

        # Key mag niet meer werken
        bare_client = TestClient(app)
        r2 = bare_client.get(
            "/api/templates",
            headers={"X-API-Key": plaintext},
        )
        assert r2.status_code == 401

        db.delete(api_key.id)

    def test_revoke_nonexistent_key(self, admin_client):
        """POST /api/admin/api-keys/{id}/revoke voor onbekende key → 404."""
        r = admin_client.post(
            "/api/admin/api-keys/nonexistent_id/revoke"
        )
        assert r.status_code == 404

    def test_delete_api_key(self, admin_client, _ensure_test_users):
        """DELETE /api/admin/api-keys/{id} verwijdert de key."""
        user_db = get_user_db()
        user = user_db.get_by_username(TEST_USERNAME)
        db = get_api_key_db()

        api_key, _ = db.create(
            name="to-delete", user_id=user.id
        )

        r = admin_client.delete(
            f"/api/admin/api-keys/{api_key.id}"
        )
        assert r.status_code == 200

        # Key mag niet meer in de lijst staan
        r2 = admin_client.get("/api/admin/api-keys")
        key_ids = [k["id"] for k in r2.json()["api_keys"]]
        assert api_key.id not in key_ids

    def test_delete_nonexistent_key(self, admin_client):
        """DELETE /api/admin/api-keys/{id} voor onbekende key → 404."""
        r = admin_client.delete(
            "/api/admin/api-keys/nonexistent_id"
        )
        assert r.status_code == 404

    def test_api_key_endpoints_require_admin(self, authenticated_client):
        """API key endpoints vereisen admin rol."""
        r = authenticated_client.get("/api/admin/api-keys")
        assert r.status_code == 403

    def test_api_key_endpoints_require_auth(self, client):
        """API key endpoints vereisen authenticatie."""
        r = client.get("/api/admin/api-keys")
        assert r.status_code == 401
