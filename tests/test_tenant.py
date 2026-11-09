"""Tests voor TenantConfig — multi-tenant asset scheiding."""

from __future__ import annotations

from pathlib import Path

import pytest

from openaec_reports.core.brand import BrandLoader
from openaec_reports.core.template_loader import TemplateLoader
from openaec_reports.core.tenant import _PACKAGE_ASSETS, TenantConfig

PROJECT_ROOT = Path(__file__).parent.parent
TENANT_DIR = PROJECT_ROOT / "tenants" / "default"

SKIP_NO_TENANT = pytest.mark.skipif(
    not TENANT_DIR.exists(),
    reason="Tenant directory niet aanwezig",
)


# ============================================================
# TenantConfig — basis
# ============================================================


class TestTenantConfigBasics:
    def test_no_env_no_arg(self, monkeypatch):
        """Zonder OPENAEC_TENANT_DIR en zonder argument → geen tenant."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        config = TenantConfig()
        assert config.tenant_dir is None

    def test_explicit_dir(self, tmp_path):
        """Met expliciete directory → tenant dir gezet."""
        config = TenantConfig(tmp_path)
        assert config.tenant_dir == tmp_path

    def test_env_var(self, monkeypatch, tmp_path):
        """Met OPENAEC_TENANT_DIR env var → tenant dir gezet."""
        monkeypatch.setenv("OPENAEC_TENANT_DIR", str(tmp_path))
        config = TenantConfig()
        assert config.tenant_dir == tmp_path

    def test_explicit_overrides_env(self, monkeypatch, tmp_path):
        """Expliciete dir heeft voorrang op env var."""
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("OPENAEC_TENANT_DIR", str(tmp_path))
        config = TenantConfig(other)
        assert config.tenant_dir == other


# ============================================================
# TenantConfig — fallback chain
# ============================================================


class TestTenantConfigFallback:
    def test_templates_dirs_no_tenant(self, monkeypatch):
        """Zonder tenant → alleen package templates dir."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        config = TenantConfig()
        dirs = config.templates_dirs
        assert len(dirs) == 1
        assert dirs[0] == _PACKAGE_ASSETS / "templates"

    def test_templates_dirs_with_tenant(self, tmp_path):
        """Met tenant → tenant templates + package templates."""
        (tmp_path / "templates").mkdir()
        config = TenantConfig(tmp_path)
        dirs = config.templates_dirs
        assert len(dirs) == 2
        assert dirs[0] == tmp_path / "templates"
        assert dirs[1] == _PACKAGE_ASSETS / "templates"

    def test_templates_dirs_tenant_no_templates_subdir(self, tmp_path):
        """Tenant dir zonder templates/ subdir → alleen package."""
        config = TenantConfig(tmp_path)
        dirs = config.templates_dirs
        assert len(dirs) == 1
        assert dirs[0] == _PACKAGE_ASSETS / "templates"

    def test_brand_path_no_tenant(self, monkeypatch):
        """Zonder tenant → package default.yaml."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        config = TenantConfig()
        assert config.brand_path == _PACKAGE_ASSETS / "brands" / "default.yaml"

    def test_brand_path_with_tenant(self, tmp_path):
        """Met tenant brand.yaml → tenant brand."""
        brand_path = tmp_path / "brand.yaml"
        brand_path.write_text("brand:\n  name: Test\n", encoding="utf-8")
        config = TenantConfig(tmp_path)
        assert config.brand_path == brand_path

    def test_brand_path_tenant_no_brand(self, tmp_path):
        """Tenant dir zonder brand.yaml → package default."""
        config = TenantConfig(tmp_path)
        assert config.brand_path == _PACKAGE_ASSETS / "brands" / "default.yaml"

    def test_stationery_dir_no_tenant(self, monkeypatch):
        """Zonder tenant → package stationery fallback (als aanwezig)."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        config = TenantConfig()
        # Package stationery kan aanwezig zijn of niet
        result = config.stationery_dir
        # Geen assertion op waarde, maar moet niet crashen
        assert result is None or result.exists()

    def test_stationery_dir_with_tenant(self, tmp_path):
        """Met tenant stationery/ → tenant dir."""
        (tmp_path / "stationery").mkdir()
        config = TenantConfig(tmp_path)
        assert config.stationery_dir == tmp_path / "stationery"

    def test_logos_dir_no_tenant_no_package(self, monkeypatch, tmp_path):
        """Zonder tenant en zonder package logos → None."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        # We kunnen de package logos niet verwijderen, maar testen dat de logica werkt
        config = TenantConfig()
        result = config.logos_dir
        assert result is None or result.exists()

    def test_logos_dir_with_tenant(self, tmp_path):
        """Met tenant logos/ → tenant dir."""
        (tmp_path / "logos").mkdir()
        config = TenantConfig(tmp_path)
        assert config.logos_dir == tmp_path / "logos"

    def test_fonts_dir_with_tenant(self, tmp_path):
        """Met tenant fonts/ → tenant dir."""
        (tmp_path / "fonts").mkdir()
        config = TenantConfig(tmp_path)
        assert config.fonts_dir == tmp_path / "fonts"


# ============================================================
# TemplateLoader met tenant
# ============================================================


class TestTemplateLoaderTenant:
    def test_list_templates_package_only(self):
        """Zonder tenant → alleen package templates."""
        loader = TemplateLoader()
        templates = loader.list_templates()
        names = [t["name"] for t in templates]
        assert "blank" in names

    def test_list_templates_merged(self, tmp_path):
        """Met tenant → tenant + package templates (dedup)."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "custom.yaml").write_text(
            "report_type: custom\n", encoding="utf-8"
        )
        loader = TemplateLoader(templates_dirs=[tdir, _PACKAGE_ASSETS / "templates"])
        templates = loader.list_templates()
        names = [t["name"] for t in templates]
        assert "custom" in names
        assert "blank" in names

    def test_load_tenant_first(self, tmp_path):
        """Tenant template wint bij naamconflict."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "blank.yaml").write_text(
            "report_type: tenant_blank\n", encoding="utf-8"
        )
        loader = TemplateLoader(templates_dirs=[tdir, _PACKAGE_ASSETS / "templates"])
        config = loader.load("blank")
        assert config.report_type == "tenant_blank"

    def test_load_fallback_to_package(self, tmp_path):
        """Template niet in tenant → fallback naar package."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        loader = TemplateLoader(templates_dirs=[tdir, _PACKAGE_ASSETS / "templates"])
        config = loader.load("blank")
        assert config.name == "blank"

    def test_load_not_found_raises(self, tmp_path):
        """Template niet in tenant en niet in package → FileNotFoundError."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        loader = TemplateLoader(templates_dirs=[tdir, _PACKAGE_ASSETS / "templates"])
        with pytest.raises(FileNotFoundError):
            loader.load("nonexistent_template_xyz")

    def test_scaffold_with_tenant(self, tmp_path):
        """to_scaffold werkt met tenant templates."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "test_tmpl.yaml").write_text(
            "report_type: test\ncover:\n  subtitle_hint: Testrapport\n",
            encoding="utf-8",
        )
        loader = TemplateLoader(templates_dirs=[tdir, _PACKAGE_ASSETS / "templates"])
        scaffold = loader.to_scaffold("test_tmpl")
        assert scaffold["template"] == "test_tmpl"
        assert scaffold["report_type"] == "test"


# ============================================================
# BrandLoader met tenant
# ============================================================


class TestBrandLoaderTenant:
    def test_list_brands_package_only(self):
        """Zonder tenant → package brands."""
        loader = BrandLoader()
        brands = loader.list_brands()
        slugs = [b["slug"] for b in brands]
        assert "default" in slugs

    def test_list_brands_with_tenant(self, tmp_path):
        """Met tenant → tenant brand + package brands (dedup)."""
        (tmp_path / "brand.yaml").write_text(
            "brand:\n  name: Tenant Brand\n  slug: tenant_brand\ncolors: {}\n",
            encoding="utf-8",
        )
        config = TenantConfig(tmp_path)
        loader = BrandLoader(tenant_config=config)
        brands = loader.list_brands()
        slugs = [b["slug"] for b in brands]
        assert "tenant_brand" in slugs
        assert "default" in slugs

    def test_load_tenant_brand(self, tmp_path):
        """Laad tenant brand via None naam."""
        (tmp_path / "brand.yaml").write_text(
            "brand:\n  name: Tenant Brand\n  slug: tenant_brand\ncolors:\n  primary: '#FF0000'\n",
            encoding="utf-8",
        )
        config = TenantConfig(tmp_path)
        loader = BrandLoader(tenant_config=config)
        brand = loader.load()
        assert brand.slug == "tenant_brand"
        assert brand.colors.get("primary") == "#FF0000"

    def test_load_default_without_tenant(self, monkeypatch):
        """Zonder tenant → load_default() retourneert package default."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        loader = BrandLoader()
        brand = loader.load_default()
        assert brand.slug == "default"

    def test_load_by_name_still_works(self):
        """Bestaande name-based load blijft werken."""
        loader = BrandLoader()
        brand = loader.load("default")
        assert brand.slug == "default"


# ============================================================
# Integratie met echte tenant directory
# ============================================================


@SKIP_NO_TENANT
class TestRealTenantIntegration:
    def test_tenant_config_real(self):
        """TenantConfig met echte tenant dir."""
        config = TenantConfig(TENANT_DIR)
        assert config.tenant_dir == TENANT_DIR
        assert len(config.templates_dirs) == 2
        assert config.brand_path == TENANT_DIR / "brand.yaml"
        assert config.stationery_dir == TENANT_DIR / "stationery"
        assert config.logos_dir == TENANT_DIR / "logos"
        assert config.fonts_dir == TENANT_DIR / "fonts"

    def test_template_loader_real_tenant(self):
        """TemplateLoader met echte tenant dir vindt tenant + package templates."""
        config = TenantConfig(TENANT_DIR)
        loader = TemplateLoader(templates_dirs=config.templates_dirs)
        templates = loader.list_templates()
        names = [t["name"] for t in templates]
        # Tenant templates
        assert "structural_report" in names
        # Package defaults
        assert "blank" in names

    @pytest.mark.skip(
        reason=(
            "default tenant fixtures incomplete post-purge "
            "(verwachte tenant-specifieke brand slug/kleur)"
        )
    )
    def test_brand_loader_real_tenant(self):
        """BrandLoader met echte tenant dir laadt tenant brand."""
        config = TenantConfig(TENANT_DIR)
        loader = BrandLoader(tenant_config=config)
        brand = loader.load()
        assert brand.slug == "default"
        assert brand.colors.get("primary") == "#40124A"

    @pytest.mark.skip(
        reason=(
            "default tenant fixtures incomplete post-purge "
            "(verwachte tenant-specifieke brand slug)"
        )
    )
    def test_brand_list_real_tenant(self):
        """BrandLoader list_brands met echte tenant → tenant + default."""
        config = TenantConfig(TENANT_DIR)
        loader = BrandLoader(tenant_config=config)
        brands = loader.list_brands()
        slugs = [b["slug"] for b in brands]
        assert "default" in slugs
        assert "default" in slugs
