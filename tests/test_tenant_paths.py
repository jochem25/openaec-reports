"""Tests voor de tenant → package asset-cascade (TenantConfig.find_template / find_font).

Deze tests verifiëren dat de renderer_v2 en font manager de bind-mount
``/app/tenants/<slug>/`` als primaire bron gebruiken en terugvallen op
package-defaults wanneer een tenant-specifiek bestand ontbreekt.

Incident 2026-04-16: na tenant-purge waren per-tenant templates en brands
uit het git-repo verwijderd; requests met ``brand="3bm"`` faalden op
``FileNotFoundError: Template directory not found``. Fix via
``TenantConfig.find_template`` + ``ReportGeneratorV2(tenant_slug=...)``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openaec_reports.core.renderer_v2 import FontManager, TemplateSet
from openaec_reports.core.tenant import _PACKAGE_ASSETS, TenantConfig

PKG_BRAND = "pkg_brand"


@pytest.fixture()
def package_brand_assets(tmp_path, monkeypatch):
    """Nep-package-assets met een per-brand template-subdir.

    De echte package-subdir (``assets/templates/3bm_cooperatie``) is in de
    tenant-purge (a2edfe2) uit git gehaald; deze fixture bouwt het
    backward-compat-scenario na zonder prive-tenantdata.
    """
    from openaec_reports.core import renderer_v2, tenant

    assets = tmp_path / "pkg_assets"
    brand_dir = assets / "templates" / PKG_BRAND
    brand_dir.mkdir(parents=True)
    (brand_dir / "cover.yaml").write_text("name: cover\n", encoding="utf-8")
    monkeypatch.setattr(tenant, "_PACKAGE_ASSETS", assets)
    monkeypatch.setattr(renderer_v2, "TEMPLATES_DIR", assets / "templates")
    return assets

# ============================================================
# TenantConfig.find_template — cascade
# ============================================================


class TestFindTemplate:
    def test_tenant_template_preferred(self, tmp_path):
        """Tenant-specifiek bestand wint boven package default."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        tenant_cover = tdir / "cover.yaml"
        tenant_cover.write_text("tenant: true\n", encoding="utf-8")

        tc = TenantConfig(tmp_path)
        resolved = tc.find_template("cover.yaml")

        assert resolved == tenant_cover

    def test_fallback_to_package_shared_template(self, tmp_path):
        """Zonder tenant-versie valt ``find_template`` terug op package defaults."""
        tdir = tmp_path / "templates"
        tdir.mkdir()

        tc = TenantConfig(tmp_path)
        resolved = tc.find_template("blank.yaml")

        # blank.yaml zit in de package (gedeelde default)
        assert resolved is not None
        assert resolved == _PACKAGE_ASSETS / "templates" / "blank.yaml"

    def test_brand_subdir_in_package(self, tmp_path, package_brand_assets):
        """Bij brand-argument zoekt cascade ook ``<package>/templates/<brand>/<file>``."""
        # Geen tenant-templates; zoek in package per-brand subdir.
        tenant_dir = tmp_path / "tenant"
        (tenant_dir / "templates").mkdir(parents=True)

        tc = TenantConfig(tenant_dir)
        resolved = tc.find_template("cover.yaml", brand=PKG_BRAND)

        assert resolved is not None
        assert resolved.parent.name == PKG_BRAND
        assert resolved.name == "cover.yaml"

    def test_missing_template_returns_none(self, tmp_path):
        """Bestand dat nergens bestaat → ``None`` (geen exception)."""
        tc = TenantConfig(tmp_path)
        assert tc.find_template("does_not_exist_xyz.yaml") is None

    def test_no_tenant_dir_uses_package_only(self, monkeypatch):
        """Zonder tenant-dir vindt cascade alleen package-bestanden."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        tc = TenantConfig()
        resolved = tc.find_template("blank.yaml")
        assert resolved == _PACKAGE_ASSETS / "templates" / "blank.yaml"


# ============================================================
# TenantConfig.find_font — cascade
# ============================================================


class TestFindFont:
    def test_tenant_font_preferred(self, tmp_path):
        """Tenant font wint boven package font (zelfde bestandsnaam)."""
        fdir = tmp_path / "fonts"
        fdir.mkdir()
        tenant_lib = fdir / "LiberationSans-Regular.ttf"
        tenant_lib.write_bytes(b"fake-font-data")

        tc = TenantConfig(tmp_path)
        resolved = tc.find_font("LiberationSans-Regular.ttf")

        assert resolved == tenant_lib

    def test_fallback_to_package_font(self, tmp_path):
        """Zonder tenant fonts → package Liberation Sans."""
        tc = TenantConfig(tmp_path)
        resolved = tc.find_font("LiberationSans-Regular.ttf")

        # Package bundelt Liberation Sans altijd
        assert resolved is not None
        assert resolved.parent == _PACKAGE_ASSETS / "fonts"

    def test_missing_font_returns_none(self, tmp_path):
        """Font dat nergens bestaat → ``None``."""
        tc = TenantConfig(tmp_path)
        assert tc.find_font("NonExistent-Font.ttf") is None


# ============================================================
# TenantConfig.fonts_dirs — cascade lijst
# ============================================================


class TestFontsDirsCascade:
    def test_tenant_first_then_package(self, tmp_path):
        """``fonts_dirs`` produceert tenant fonts/ gevolgd door package fonts/."""
        fdir = tmp_path / "fonts"
        fdir.mkdir()

        tc = TenantConfig(tmp_path)
        dirs = tc.fonts_dirs

        assert len(dirs) == 2
        assert dirs[0] == fdir
        assert dirs[1] == _PACKAGE_ASSETS / "fonts"

    def test_only_package_when_no_tenant_fonts(self, tmp_path):
        """Tenant zonder fonts/ subdir → alleen package fonts/ in cascade."""
        tc = TenantConfig(tmp_path)
        dirs = tc.fonts_dirs
        assert len(dirs) == 1
        assert dirs[0] == _PACKAGE_ASSETS / "fonts"


# ============================================================
# TemplateSet — integratie met TenantConfig
# ============================================================


class TestTemplateSetWithTenantConfig:
    def test_loads_from_tenant_dir(self, tmp_path):
        """``TemplateSet`` met ``tenant_config`` gebruikt tenant YAML's."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        # Minimaal set: alleen cover.yaml tenant-specifiek, rest fallback
        (tdir / "cover.yaml").write_text(
            "dynamic_fields:\n  rapport_type:\n    font: LiberationSans-Bold\n    size: 42\n",
            encoding="utf-8",
        )

        tc = TenantConfig(tmp_path)
        ts = TemplateSet(brand="3bm_cooperatie", tenant_config=tc)

        # Tenant cover gebruikt (size 42 is tenant-specifiek)
        assert ts.cover["dynamic_fields"]["rapport_type"]["size"] == 42

    def test_fallback_to_package_for_missing_template(self, tmp_path):
        """Ontbrekend tenant-bestand → package default wordt geladen (of leeg dict)."""
        tdir = tmp_path / "templates"
        tdir.mkdir()

        tc = TenantConfig(tmp_path)
        # Zonder brand='3bm_cooperatie' zoekt find_template ook naar
        # '<package>/templates/cover.yaml' — die bestaat NIET op package niveau
        # (package heeft alleen blank/building_code/daylight/structural_report
        # als gedeelde templates; cover zit per-brand). Resultaat: lege dict.
        ts = TemplateSet(brand="3bm_cooperatie", tenant_config=tc)

        # cover/colofon/toc/standaard zouden lege dicts moeten zijn als ze
        # niet in tenant én niet in package root bestaan. Maar per-brand
        # fallback ('3bm_cooperatie' subdir) levert ze wel op.
        # Test dat er geen FileNotFoundError komt — graceful fallback.
        assert isinstance(ts.cover, dict)
        assert isinstance(ts.standaard, dict)

    def test_no_tenant_config_backward_compat(self, package_brand_assets):
        """Zonder ``tenant_config`` blijft oud gedrag: package-per-brand-subdir."""
        ts = TemplateSet(brand=PKG_BRAND)
        assert ts.dir == package_brand_assets / "templates" / PKG_BRAND
        assert ts.dir.exists()

    def test_no_tenant_config_raises_on_missing_brand(self):
        """Zonder ``tenant_config`` + onbekende brand → ``FileNotFoundError`` (backward compat)."""
        with pytest.raises(FileNotFoundError):
            TemplateSet(brand="nonexistent_brand_xyz")


# ============================================================
# FontManager — integratie met TenantConfig
# ============================================================


class TestFontManagerWithTenantConfig:
    def test_cascade_includes_tenant_fonts(self, tmp_path):
        """``FontManager`` cascade start bij tenant fonts/ als die bestaat."""
        fdir = tmp_path / "fonts"
        fdir.mkdir()
        tc = TenantConfig(tmp_path)

        fm = FontManager(tenant_config=tc)

        # Eerste entry = tenant fonts, tweede = package
        assert fm._font_cascade[0] == fdir
        assert _PACKAGE_ASSETS / "fonts" in fm._font_cascade

    def test_liberation_loaded_from_package_fallback(self, tmp_path):
        """Tenant zonder Liberation fonts → FontManager laadt uit package."""
        fdir = tmp_path / "fonts"
        fdir.mkdir()
        tc = TenantConfig(tmp_path)

        fm = FontManager(tenant_config=tc)

        # Liberation fonts moeten geregistreerd zijn ondanks lege tenant/fonts/
        assert fm._liberation_bold is not None
        assert fm._liberation_regular is not None

    def test_no_tenant_config_backward_compat(self):
        """Zonder tenant_config werkt FontManager via package FONT_DIR (oud gedrag)."""
        fm = FontManager()
        # Liberation wordt ingeladen vanuit package
        assert fm._liberation_bold is not None
        # Cascade bevat tenminste package FONT_DIR
        assert any("assets" in str(p) and "fonts" in str(p) for p in fm._font_cascade)


# ============================================================
# ReportGeneratorV2 — end-to-end met tenant_slug
# ============================================================


class TestReportGeneratorV2Tenant:
    def test_tenant_slug_resolves_via_tenant_resolver(self, tmp_path, monkeypatch):
        """Met ``tenant_slug`` vindt de generator templates in de bind-mount."""
        tenants_root = tmp_path / "tenants"
        tenant_dir = tenants_root / "acme"
        (tenant_dir / "templates").mkdir(parents=True)

        # Minimale brand.yaml voor BrandLoader
        (tenant_dir / "brand.yaml").write_text(
            "brand:\n  name: Acme\n  slug: acme\n",
            encoding="utf-8",
        )
        # Tenant cover template
        (tenant_dir / "templates" / "cover.yaml").write_text(
            "dynamic_fields:\n  rapport_type:\n    size: 99\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(tenants_root))
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)

        # Cache busten omdat get_tenant_config @lru_cache gebruikt
        from openaec_reports.core import tenant_resolver

        tenant_resolver.get_tenant_config.cache_clear()
        tenant_resolver.get_template_loader.cache_clear()
        tenant_resolver.get_brand_loader.cache_clear()

        from openaec_reports.core.renderer_v2 import ReportGeneratorV2

        gen = ReportGeneratorV2(brand="acme", tenant_slug="acme")

        # Templates uit tenant-dir geladen
        assert gen.templates.cover["dynamic_fields"]["rapport_type"]["size"] == 99

    def test_no_tenant_slug_env_var_still_works(self, tmp_path, monkeypatch):
        """Legacy pad: OPENAEC_TENANT_DIR env var zonder expliciete slug."""
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "cover.yaml").write_text(
            "dynamic_fields:\n  rapport_type:\n    size: 77\n",
            encoding="utf-8",
        )
        (tmp_path / "brand.yaml").write_text(
            "brand:\n  name: Acme\n  slug: acme\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("OPENAEC_TENANT_DIR", str(tmp_path))

        from openaec_reports.core.renderer_v2 import ReportGeneratorV2

        gen = ReportGeneratorV2(brand="acme")

        assert gen.templates.cover["dynamic_fields"]["rapport_type"]["size"] == 77

    def test_no_tenant_no_env_backward_compat(self, monkeypatch, package_brand_assets):
        """Zonder tenant-info en een bestaande package-brand: backward compat werkt."""
        monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)
        monkeypatch.delenv("OPENAEC_TENANTS_ROOT", raising=False)

        from openaec_reports.core import tenant_resolver

        tenant_resolver.get_tenant_config.cache_clear()

        from openaec_reports.core.renderer_v2 import ReportGeneratorV2

        gen = ReportGeneratorV2(brand=PKG_BRAND)
        assert gen.templates.dir.name == PKG_BRAND


# ============================================================
# Integratie via FastAPI TestClient — forward_auth header simulatie
# ============================================================


@pytest.fixture()
def _acme_tenant_fixture(tmp_path, monkeypatch):
    """Zet een acme tenant op onder tmp_path/tenants/acme/ en wijs env-vars aan."""
    tenants_root = tmp_path / "tenants"
    tenant_dir = tenants_root / "acme"
    (tenant_dir / "templates").mkdir(parents=True)
    (tenant_dir / "stationery").mkdir()
    (tenant_dir / "fonts").mkdir()
    (tenant_dir / "logos").mkdir()

    (tenant_dir / "brand.yaml").write_text(
        "brand:\n  name: Acme Corp\n  slug: acme\n  tagline: Tests\n"
        "colors:\n  primary: '#123456'\n",
        encoding="utf-8",
    )

    # Minimale template set — cover/colofon etc. fallback naar package voor
    # gedeelde templates of leeg als niks matcht.
    (tenant_dir / "templates" / "cover.yaml").write_text(
        "dynamic_fields:\n  rapport_type:\n    size: 88\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(tenants_root))
    monkeypatch.delenv("OPENAEC_TENANT_DIR", raising=False)

    # Cache clear zodat tenant_resolver opnieuw resolvert
    from openaec_reports.core import tenant_resolver

    tenant_resolver.get_tenant_config.cache_clear()
    tenant_resolver.get_template_loader.cache_clear()
    tenant_resolver.get_brand_loader.cache_clear()

    return tenant_dir


def test_report_generator_v2_picks_tenant_from_bind_mount(
    _acme_tenant_fixture: Path,
):
    """End-to-end: bind-mount /tmp/tenants/acme/templates/cover.yaml gevonden.

    Verifieert dat de volledige cascade werkt zoals in productie na
    B-1 fase: requests met ``X-Authentik-Meta-Tenant: acme`` →
    ``ReportGeneratorV2(tenant_slug='acme')`` → templates uit bind-mount.
    """
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    gen = ReportGeneratorV2(brand="acme", tenant_slug="acme")

    # Cover uit bind-mount (size 88 is uniek voor deze fixture)
    assert gen.templates.cover["dynamic_fields"]["rapport_type"]["size"] == 88
    # Fonts cascade heeft tenant fonts/ als eerste entry
    assert gen.fonts._font_cascade[0] == _acme_tenant_fixture / "fonts"
