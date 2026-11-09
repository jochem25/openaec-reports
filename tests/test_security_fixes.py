"""Tests voor SEC-K1 t/m SEC-K5 security fixes."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from openaec_reports.api import app
from openaec_reports.core.brand import BrandLoader
from openaec_reports.core.template_loader import TemplateLoader
from openaec_reports.storage.models import ReportDB

# ============================================================
# SEC-K1: _resolve_path() tenant isolatie
# ============================================================


class TestSecK1TemplateIsolation:
    """Template loader mag met tenant_slug GEEN templates uit andere dirs laden."""

    def test_resolve_path_with_tenant_blocks_fallback(self, tmp_path):
        """Met tenant_slug: alleen tenant dir, geen package fallback."""
        tenant_dir = tmp_path / "tenant_templates"
        tenant_dir.mkdir()
        package_dir = tmp_path / "package_templates"
        package_dir.mkdir()

        # Template alleen in package dir
        (package_dir / "secret_template.yaml").write_text(
            "report_type: secret", encoding="utf-8",
        )

        loader = TemplateLoader(
            templates_dirs=[tenant_dir, package_dir],
            tenant_slug="my_tenant",
        )

        # Mag NIET gevonden worden via tenant-scoped loader
        with pytest.raises(FileNotFoundError):
            loader.load("secret_template")

    def test_resolve_path_without_tenant_allows_fallback(self, tmp_path):
        """Zonder tenant_slug: backward compat, alle dirs doorzoeken."""
        tenant_dir = tmp_path / "tenant"
        tenant_dir.mkdir()
        package_dir = tmp_path / "package"
        package_dir.mkdir()

        (package_dir / "shared.yaml").write_text(
            "report_type: shared", encoding="utf-8",
        )

        loader = TemplateLoader(
            templates_dirs=[tenant_dir, package_dir],
            tenant_slug="",
        )

        config = loader.load("shared")
        assert config.report_type == "shared"

    def test_tenant_can_load_own_template(self, tmp_path):
        """Tenant kan eigen templates laden."""
        tenant_dir = tmp_path / "my_tenant"
        tenant_dir.mkdir()

        (tenant_dir / "my_template.yaml").write_text(
            "report_type: mine", encoding="utf-8",
        )

        loader = TemplateLoader(
            templates_dirs=[tenant_dir],
            tenant_slug="my_tenant",
        )

        config = loader.load("my_template")
        assert config.report_type == "mine"


class TestSecK1BrandIsolation:
    """Brand loader mag met tenant_slug GEEN brands van andere tenants laden."""

    def test_resolve_path_blocks_other_tenant_brand(self, tmp_path):
        """Met tenant_slug: kan andermans brand niet laden."""
        tenants_root = tmp_path / "tenants"
        (tenants_root / "my_tenant").mkdir(parents=True)
        (tenants_root / "other_tenant").mkdir(parents=True)

        (tenants_root / "my_tenant" / "brand.yaml").write_text(
            "brand:\n  name: Mine\n  slug: my_tenant", encoding="utf-8",
        )
        (tenants_root / "other_tenant" / "brand.yaml").write_text(
            "brand:\n  name: Other\n  slug: other_tenant", encoding="utf-8",
        )

        loader = BrandLoader(
            tenants_root=tenants_root,
            tenant_slug="my_tenant",
        )

        # Andermans brand moet falen
        with pytest.raises(FileNotFoundError):
            loader.load("other_tenant")

    def test_tenant_can_load_own_brand(self, tmp_path):
        """Tenant kan eigen brand laden."""
        tenants_root = tmp_path / "tenants"
        (tenants_root / "my_tenant").mkdir(parents=True)
        (tenants_root / "my_tenant" / "brand.yaml").write_text(
            "brand:\n  name: Mine\n  slug: my_tenant", encoding="utf-8",
        )

        loader = BrandLoader(
            tenants_root=tenants_root,
            tenant_slug="my_tenant",
        )

        brand = loader.load("my_tenant")
        assert brand.slug == "my_tenant"


# ============================================================
# SEC-K2: Brand override validatie
# ============================================================


class TestSecK2BrandOverride:
    """Generate endpoints moeten cross-tenant brand override weigeren."""

    @pytest.fixture()
    def tenant_client(self, _ensure_test_users):
        """Client ingelogd als user met tenant=test_tenant."""
        from openaec_reports.auth.dependencies import get_user_db
        from openaec_reports.auth.models import User, UserRole
        from openaec_reports.auth.security import hash_password

        db = get_user_db()
        username = f"tenant_user_{uuid.uuid4().hex[:6]}"
        if not db.get_by_username(username):
            db.create(
                User(
                    id=uuid.uuid4().hex,
                    username=username,
                    email=f"{username}@test.nl",
                    display_name="Tenant User",
                    role=UserRole.user,
                    tenant="test_tenant",
                    hashed_password=hash_password("password123"),
                )
            )

        client = TestClient(app)
        r = client.post(
            "/api/auth/login",
            json={"username": username, "password": "password123"},
        )
        assert r.status_code == 200
        return client

    def test_cross_tenant_brand_rejected(self, tenant_client):
        """Brand van andere tenant → 403."""
        r = tenant_client.post(
            "/api/generate",
            json={
                "project": "Test",
                "template": "structural",
                "brand": "competitor_brand",
            },
        )
        assert r.status_code == 403
        assert "niet toegestaan" in r.json()["detail"]

    def test_own_brand_allowed(self, tenant_client):
        """Eigen tenant brand → niet 403 (kan wel falen om andere reden)."""
        r = tenant_client.post(
            "/api/generate",
            json={
                "project": "Test",
                "template": "structural",
                "brand": "test_tenant",
            },
        )
        # Niet 403 — template/brand mag niet gevonden worden, maar
        # de brand check is gepasseerd
        assert r.status_code != 403


# ============================================================
# SEC-K3: Upload validatie
# ============================================================


class TestSecK3UploadValidation:
    """Upload endpoint moet bestandstype en grootte valideren."""

    def test_reject_executable(self, authenticated_client):
        """Executable extensie → 400."""
        r = authenticated_client.post(
            "/api/upload",
            files={"file": ("malware.exe", b"MZ...", "application/octet-stream")},
        )
        assert r.status_code == 400
        assert "niet toegestaan" in r.json()["detail"]

    def test_reject_php(self, authenticated_client):
        """.php extensie → 400."""
        r = authenticated_client.post(
            "/api/upload",
            files={"file": ("shell.php", b"<?php ?>", "application/octet-stream")},
        )
        assert r.status_code == 400

    def test_reject_script(self, authenticated_client):
        """.sh extensie → 400."""
        r = authenticated_client.post(
            "/api/upload",
            files={"file": ("script.sh", b"#!/bin/bash", "application/octet-stream")},
        )
        assert r.status_code == 400

    def test_allow_png(self, authenticated_client):
        """.png extensie → accepteerd."""
        r = authenticated_client.post(
            "/api/upload",
            files={"file": ("image.png", b"\x89PNG...", "image/png")},
        )
        assert r.status_code == 200

    def test_allow_pdf(self, authenticated_client):
        """.pdf extensie → accepteerd."""
        r = authenticated_client.post(
            "/api/upload",
            files={"file": ("doc.pdf", b"%PDF-1.4...", "application/pdf")},
        )
        assert r.status_code == 200

    def test_reject_oversized(self, authenticated_client):
        """Bestand > 10MB → 413."""
        large_content = b"x" * (10 * 1024 * 1024 + 1)
        r = authenticated_client.post(
            "/api/upload",
            files={"file": ("big.png", large_content, "image/png")},
        )
        assert r.status_code == 413


# ============================================================
# SEC-K4: Path traversal in _report_path
# ============================================================


class TestSecK4PathTraversal:
    """_report_path moet path traversal via IDs blokkeren."""

    def test_traversal_in_user_id(self, tmp_path):
        """user_id met '..' → ValueError."""
        db = ReportDB(db_path=tmp_path / "test.db")
        with pytest.raises(ValueError, match="Ongeldig ID formaat"):
            db._report_path("../../etc", "abc123")

    def test_traversal_in_report_id(self, tmp_path):
        """report_id met '..' → ValueError."""
        db = ReportDB(db_path=tmp_path / "test.db")
        with pytest.raises(ValueError, match="Ongeldig ID formaat"):
            db._report_path("abc123", "../../etc/passwd")

    def test_slash_in_id(self, tmp_path):
        """Slash in ID → ValueError."""
        db = ReportDB(db_path=tmp_path / "test.db")
        with pytest.raises(ValueError, match="Ongeldig ID formaat"):
            db._report_path("abc/def", "123")

    def test_valid_hex_id_works(self, tmp_path):
        """Valide hex UUID → werkt."""
        db = ReportDB(db_path=tmp_path / "test.db")
        path = db._report_path("abcdef1234567890", "1234567890abcdef")
        assert path.name == "1234567890abcdef.json"

    def test_valid_alphanumeric_id_works(self, tmp_path):
        """Alfanumeriek ID → werkt (backward compat voor tests)."""
        db = ReportDB(db_path=tmp_path / "test.db")
        path = db._report_path("user1", "report1")
        assert path.name == "report1.json"


# ============================================================
# SEC-K5: Brand sessie user isolatie
# ============================================================


class TestSecK5BrandSessionIsolation:
    """Brand sessie endpoints moeten eigendom verifiëren."""

    def test_session_stores_owner(self):
        """Upload-pairs slaat owner_id op in metadata."""
        from openaec_reports.brand_api import BrandSession

        session_id = uuid.uuid4().hex[:12]
        session = BrandSession(session_id)
        session.create()
        session.save_metadata({
            "brand_name": "Test",
            "owner_id": "user_abc123",
        })

        assert session.get_owner() == "user_abc123"

        # Cleanup
        session.cleanup()

    def test_verify_owner_passes_for_owner(self):
        """verify_owner slaagt voor de eigenaar."""
        from openaec_reports.brand_api import BrandSession

        session_id = uuid.uuid4().hex[:12]
        session = BrandSession(session_id)
        session.create()
        session.save_metadata({"owner_id": "owner_123"})

        # Moet niet raisen
        session.verify_owner("owner_123")

        session.cleanup()

    def test_verify_owner_rejects_other_user(self):
        """verify_owner faalt voor een andere user."""
        from fastapi import HTTPException

        from openaec_reports.brand_api import BrandSession

        session_id = uuid.uuid4().hex[:12]
        session = BrandSession(session_id)
        session.create()
        session.save_metadata({"owner_id": "owner_123"})

        with pytest.raises(HTTPException) as exc_info:
            session.verify_owner("attacker_456")
        assert exc_info.value.status_code == 403

        session.cleanup()
