"""Tests voor special_pages.py — cover, colofon en backcover canvas rendering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from openaec_reports import A4, Report
from openaec_reports.core.brand import BrandConfig, BrandLoader
from openaec_reports.core.document import DocumentConfig
from openaec_reports.core.special_pages import (
    _brand_color,
    _resolve_logo_path,
    draw_appendix_divider_page,
    draw_backcover_page,
    draw_colofon_page,
    draw_cover_page,
)


def _make_config(**kwargs) -> DocumentConfig:
    """Maak een test DocumentConfig."""
    defaults = {
        "project": "Testproject",
        "project_number": "2026-001",
        "client": "Test BV",
        "author": "Ing. Test",
        "report_type": "Constructief rapport",
        "subtitle": "Constructieve berekening",
    }
    defaults.update(kwargs)
    return DocumentConfig(format=A4, **defaults)


TENANTS = Path(__file__).resolve().parent.parent / "tenants"


def _make_brand() -> BrandConfig:
    """Laad de publieke default-tenant brand (``tenants/default``), zoals productie.

    Expliciete tenants_root: resultaat mag niet afhangen van een
    ``OPENAEC_TENANTS_ROOT`` die een andere test heeft laten staan.
    """
    return BrandLoader(tenants_root=TENANTS).load("default")


def _assert_state_balanced(canvas) -> None:
    """Canvas-state is gezet en netjes hersteld.

    Het aantal saveState-aanroepen hangt af van de brand (een SVG-logo nest
    eigen state); wat telt is dat elke saveState een restoreState heeft.
    """
    assert canvas.saveState.call_count >= 1
    assert canvas.saveState.call_count == canvas.restoreState.call_count


def _make_canvas():
    """Maak een mock ReportLab canvas."""
    canvas = MagicMock()
    canvas.beginPath.return_value = MagicMock()
    return canvas


class TestBrandColor:
    def test_primary_from_brand(self):
        brand = _make_brand()
        color = _brand_color(brand, "primary", "#000000")
        from reportlab.lib.colors import Color
        assert isinstance(color, Color)

    def test_fallback_used_when_key_missing(self):
        brand = BrandConfig()  # Lege brand, geen kleuren
        color = _brand_color(brand, "primary", "#FF0000")
        from reportlab.lib.colors import Color
        assert isinstance(color, Color)


class TestResolveLogoPath:
    def test_resolve_from_brand_logos(self):
        brand = _make_brand()
        path = _resolve_logo_path(brand, "main", "default.png")
        # Pad moet bestaan of een Path zijn
        assert isinstance(path, Path)

    def test_fallback_when_key_not_in_brand(self):
        brand = BrandConfig()  # Lege brand, geen logos
        path = _resolve_logo_path(brand, "white", "default-wit.png")
        assert isinstance(path, Path)
        assert "default-wit.png" in str(path)


class TestDrawCoverPage:
    def test_calls_save_state_and_restore_state(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_cover_page(canvas, None, config, brand)

        _assert_state_balanced(canvas)

    def test_draws_purple_rectangle(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_cover_page(canvas, None, config, brand)

        # Controleer dat setFillColor en rect minstens één keer aangeroepen zijn
        assert canvas.setFillColor.called
        assert canvas.rect.called

    def test_draws_title_string(self):
        canvas = _make_canvas()
        config = _make_config(project="Mijn Project")
        brand = _make_brand()

        draw_cover_page(canvas, None, config, brand)

        # Controleer dat drawString aangeroepen is (voor de titel)
        assert canvas.drawString.called
        # Minstens één aanroep moet de projectnaam bevatten
        found_project = any(
            "Mijn Project" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found_project

    def test_draws_subtitle_when_present(self):
        canvas = _make_canvas()
        config = _make_config(subtitle="Constructieve berekening")
        brand = _make_brand()

        draw_cover_page(canvas, None, config, brand)

        found_subtitle = any(
            "Constructieve berekening" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found_subtitle

    def test_no_subtitle_no_extra_drawstring(self):
        canvas = _make_canvas()
        config = _make_config(subtitle="")
        brand = _make_brand()

        draw_cover_page(canvas, None, config, brand)

        # Mag niet crashen
        _assert_state_balanced(canvas)

    def test_no_crash_with_missing_logo(self, tmp_path):
        canvas = _make_canvas()
        config = _make_config()
        brand = BrandConfig(
            logos={"main": "logos/nonexistent.png"},
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica"},
        )

        # Mag niet crashen ook zonder logo
        draw_cover_page(canvas, None, config, brand)
        _assert_state_balanced(canvas)


class TestDrawColofonPage:
    def test_calls_save_state_and_restore_state(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_colofon_page(canvas, None, config, brand)

        _assert_state_balanced(canvas)

    def test_draws_report_type(self):
        canvas = _make_canvas()
        config = _make_config(report_type="BBL-toetsingsrapportage")
        brand = _make_brand()

        draw_colofon_page(canvas, None, config, brand)

        found = any(
            "BBL-toetsingsrapportage" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found

    def test_draws_project_label(self):
        canvas = _make_canvas()
        config = _make_config(project="Mijn Project")
        brand = _make_brand()

        draw_colofon_page(canvas, None, config, brand)

        # Project waarde moet getekend zijn
        found = any(
            "Mijn Project" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found

    def test_draws_with_colofon_data(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()
        data = {"status": "DEFINITIEF", "date": "2026-02-19"}

        draw_colofon_page(canvas, None, config, brand, data)

        _assert_state_balanced(canvas)

    def test_draws_footer_rect(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_colofon_page(canvas, None, config, brand)

        # Turquoise footer rect
        assert canvas.rect.called

    def test_draws_separator_lines(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_colofon_page(canvas, None, config, brand)

        # Scheidingslijnen
        assert canvas.line.called

    def test_draws_page_number(self):
        canvas = _make_canvas()
        canvas.getPageNumber.return_value = 2
        config = _make_config()
        brand = _make_brand()

        draw_colofon_page(canvas, None, config, brand)

        # Paginanummer rechts uitgelijnd
        assert canvas.drawRightString.called

    def test_no_crash_without_colofon_data(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_colofon_page(canvas, None, config, brand, colofon_data=None)

        _assert_state_balanced(canvas)


class TestDrawBackcoverPage:
    def test_calls_save_state_and_restore_state(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_backcover_page(canvas, None, config, brand)

        _assert_state_balanced(canvas)

    def test_draws_turquoise_background(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_backcover_page(canvas, None, config, brand)

        # Turquoise achtergrond via rect
        assert canvas.rect.called

    def test_draws_polygon_paths(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_backcover_page(canvas, None, config, brand)

        # Polygons via beginPath
        assert canvas.beginPath.called
        # Minstens 2 polygons (wit + donkerpaars driehoek)
        assert canvas.beginPath.call_count >= 2

    def test_draws_contact_info(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()
        # Publieke default-tenant heeft (sinds de tenant-purge) leeg contact.
        brand.contact = {"name": "OpenAEC", "address": "Zwijndrecht", "website": ""}

        draw_backcover_page(canvas, None, config, brand)

        # Contactgegevens worden getekend
        found_contact = any(
            "Zwijndrecht" in str(args) or "OpenAEC" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found_contact


class TestDrawAppendixDividerPage:
    def test_calls_save_state_and_restore_state(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_appendix_divider_page(canvas, None, config, brand)

        _assert_state_balanced(canvas)

    def test_draws_turquoise_background(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_appendix_divider_page(canvas, None, config, brand)

        # Achtergrond rect + paurs rect = minstens 2
        assert canvas.rect.call_count >= 2

    def test_draws_bijlage_number(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_appendix_divider_page(
            canvas, None, config, brand,
            appendix_number=3, appendix_title="Test",
        )

        found = any(
            "Bijlage 3" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found

    def test_draws_title(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_appendix_divider_page(
            canvas, None, config, brand,
            appendix_number=1, appendix_title="Berekeningen",
        )

        found = any(
            "Berekeningen" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found

    def test_draws_multiline_title(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()

        draw_appendix_divider_page(
            canvas, None, config, brand,
            appendix_number=1,
            appendix_title="Constructieve\nberekeningen",
        )

        found_line1 = any(
            "Constructieve" in str(args)
            for args in canvas.drawString.call_args_list
        )
        found_line2 = any(
            "berekeningen" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found_line1
        assert found_line2

    def test_draws_tagline(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = _make_brand()
        # Publieke default-tenant heeft (sinds de tenant-purge) geen tagline.
        brand.pages["appendix_divider"] = {
            **brand.pages.get("appendix_divider", {}),
            "tagline": "Projecten die inspireren",
        }

        draw_appendix_divider_page(canvas, None, config, brand)

        found = any(
            "Projecten die inspireren" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found

    def test_no_crash_with_default_brand(self):
        canvas = _make_canvas()
        config = _make_config()
        brand = BrandConfig()  # Lege brand, geen pages sectie

        draw_appendix_divider_page(canvas, None, config, brand)

        _assert_state_balanced(canvas)


class TestCoverFromBrandConfig:
    """Tests dat cover visuele parameters uit brand.pages.cover komen."""

    def test_cover_uses_brand_title_position(self):
        """Cover titel positie komt uit brand.pages.cover."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica", "medium": "Helvetica"},
        )
        brand.pages["cover"] = {
            "title_x_ref": 100.0,
            "title_y_ref": 200.0,
            "title_size_ref": 30.0,
        }
        canvas = _make_canvas()
        config = _make_config(project="BrandTest")

        draw_cover_page(canvas, None, config, brand)

        assert canvas.drawString.called
        found = any(
            "BrandTest" in str(args) for args in canvas.drawString.call_args_list
        )
        assert found

    def test_cover_uses_brand_badges(self):
        """Cover badges komen uit brand.pages.cover.badges."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica", "medium": "Helvetica"},
        )
        brand.pages["cover"] = {
            "badges": [
                {"label": "CUSTOM", "bg_color": "#FF0000", "text_color": "#000000",
                 "x_ref": 100, "y_ref": 100, "w_ref": 80, "h_ref": 30},
            ],
            "badge_radius_ref": 10,
            "badge_font_size_ref": 9.0,
        }
        canvas = _make_canvas()
        config = _make_config(project="BadgeTest")

        draw_cover_page(canvas, None, config, brand)

        assert canvas.roundRect.called
        # Slechts 1 badge (niet de default 3)
        assert canvas.roundRect.call_count == 1

    def test_cover_uses_brand_logo_key(self):
        """Cover logo key komt uit brand.pages.cover.logo_key."""
        brand = _make_brand()
        brand.pages["cover"] = {"logo_key": "tagline", "logo_fallback": "main"}
        canvas = _make_canvas()
        config = _make_config()

        # Mag niet crashen
        draw_cover_page(canvas, None, config, brand)
        _assert_state_balanced(canvas)

    def test_cover_uses_brand_ontdek_text(self):
        """Cover ontdek tekst komt uit brand.pages.cover."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica", "medium": "Helvetica"},
        )
        brand.pages["cover"] = {
            "ontdek_text": "Discover us ",
            "ontdek_url": "example.com",
        }
        canvas = _make_canvas()
        config = _make_config()

        draw_cover_page(canvas, None, config, brand)

        found_text = any(
            "Discover us " in str(args) for args in canvas.drawString.call_args_list
        )
        found_url = any(
            "example.com" in str(args) for args in canvas.drawString.call_args_list
        )
        assert found_text
        assert found_url

    def test_cover_defaults_without_spec(self):
        """Cover werkt zonder pages.cover in brand YAML."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica", "medium": "Helvetica"},
        )
        canvas = _make_canvas()
        config = _make_config(project="Fallback Test")

        draw_cover_page(canvas, None, config, brand)

        assert canvas.drawString.called
        # Geen default badges meer — badges vereisen expliciete brand config
        assert not canvas.roundRect.called


class TestBackcoverFromBrandConfig:
    """Tests dat backcover visuele parameters uit brand.pages.backcover komen."""

    def test_backcover_uses_brand_polygons(self):
        """Backcover polygonen komen uit brand.pages.backcover."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica"},
        )
        brand.pages["backcover"] = {
            "white_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            "purple_triangle": [[0, 100], [50, 50], [0, 0]],
        }
        canvas = _make_canvas()
        config = _make_config()

        draw_backcover_page(canvas, None, config, brand)

        assert canvas.drawPath.called
        # 2 polygonen: wit + paars
        assert canvas.drawPath.call_count >= 2

    def test_backcover_uses_brand_logo_key(self):
        """Backcover logo key komt uit brand.pages.backcover.logo_key."""
        brand = _make_brand()
        brand.pages["backcover"] = {"logo_key": "white"}
        canvas = _make_canvas()
        config = _make_config()

        draw_backcover_page(canvas, None, config, brand)
        _assert_state_balanced(canvas)

    def test_backcover_uses_brand_contact_position(self):
        """Backcover contact posities komen uit brand.pages.backcover."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica"},
            contact={"name": "Test BV", "address": "Teststraat 1", "website": "test.nl"},
        )
        brand.pages["backcover"] = {
            "contact_x_ref": 300,
            "contact_y_ref": 200,
            "contact_line_h_ref": 25,
        }
        canvas = _make_canvas()
        config = _make_config()

        draw_backcover_page(canvas, None, config, brand)

        found = any(
            "Test BV" in str(args) for args in canvas.drawString.call_args_list
        )
        assert found

    def test_backcover_uses_brand_ontdek_prefix(self):
        """Backcover ontdek prefix komt uit brand.pages.backcover."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica"},
            contact={"name": "Test", "website": "test.nl"},
        )
        brand.pages["backcover"] = {"ontdek_prefix": "Visit us -> "}
        canvas = _make_canvas()
        config = _make_config()

        draw_backcover_page(canvas, None, config, brand)

        found = any(
            "Visit us -> test.nl" in str(args)
            for args in canvas.drawString.call_args_list
        )
        assert found

    def test_backcover_defaults_without_spec(self):
        """Backcover werkt zonder pages.backcover in brand YAML."""
        brand = BrandConfig(
            colors={"primary": "#40124A", "secondary": "#38BDA0"},
            fonts={"heading": "Helvetica-Bold", "body": "Helvetica"},
        )
        canvas = _make_canvas()
        config = _make_config()

        draw_backcover_page(canvas, None, config, brand)

        # Geen default polygonen meer — vereisen expliciete brand config
        assert not canvas.drawPath.called
        # Achtergrond rect wordt nog wel getekend
        assert canvas.rect.called


class TestIntegration:
    """Integratie tests: Report bouwt PDF met special pages."""

    def test_report_with_cover_builds_pdf(self, tmp_path):
        report = Report(
            format=A4,
            project="Cover Integratie Test",
            project_number="T-001",
            client="Test BV",
            brand="default",
        )
        report.add_cover(subtitle="Integratie test")
        report.add_section("Inhoud", content=["Test tekst."])

        output = tmp_path / "cover_test.pdf"
        report.build(output)

        assert output.exists()
        assert output.stat().st_size > 1000

    def test_report_with_cover_and_colofon(self, tmp_path):
        report = Report(
            format=A4,
            project="Colofon Test",
            project_number="T-002",
            client="Test BV",
            brand="default",
        )
        report.add_cover(subtitle="Met colofon")
        report.add_colofon(versie="1.0", datum="2026-02-18")
        report.add_section("Inhoud", content=["Test."])

        output = tmp_path / "colofon_test.pdf"
        report.build(output)

        assert output.exists()
        assert output.stat().st_size > 1000

    def test_report_with_all_special_pages(self, tmp_path):
        report = Report(
            format=A4,
            project="Volledig Special Pages Test",
            project_number="T-003",
            client="Opdrachtgever BV",
            author="Ing. Tester",
            brand="default",
        )
        report.add_cover(subtitle="Constructieve berekening")
        report.add_colofon(versie="1.0")
        report.add_section("Hoofdstuk 1", content=["Inhoud."])
        report.add_backcover()

        output = tmp_path / "all_special_pages.pdf"
        report.build(output)

        assert output.exists()
        assert output.stat().st_size > 2000

    def test_report_backcover_only(self, tmp_path):
        report = Report(
            format=A4,
            project="Backcover Only",
            brand="default",
        )
        report.add_section("Inhoud", content=["Test."])
        report.add_backcover()

        output = tmp_path / "backcover_only.pdf"
        report.build(output)

        assert output.exists()

    def test_has_colofon_flag_set(self):
        report = Report(format=A4, project="Test")
        assert not report._has_colofon

        report.add_colofon()
        assert report._has_colofon

    def test_colofon_data_stored(self):
        report = Report(format=A4, project="Test")
        report.add_colofon(versie="2.0", status="DEFINITIEF")

        assert report._colofon is not None
        assert report._colofon.get("versie") == "2.0"
        assert report._colofon.get("status") == "DEFINITIEF"

    def test_cover_image_stored(self):
        report = Report(format=A4, project="Test")
        report.add_cover(subtitle="Sub", image="/pad/naar/foto.jpg")

        assert report._cover_image == "/pad/naar/foto.jpg"
        assert report.document.config.subtitle == "Sub"

    def test_report_with_appendix(self, tmp_path):
        report = Report(
            format=A4,
            project="Appendix Test",
            brand="default",
        )
        report.add_cover(subtitle="Met bijlage")
        report.add_section("Inhoud", content=["Test."])
        report.add_appendix("Berekeningen", number=1)
        report.add_backcover()

        output = tmp_path / "appendix_test.pdf"
        report.build(output)

        assert output.exists()
        assert output.stat().st_size > 2000

    def test_report_with_multiple_appendices(self, tmp_path):
        report = Report(
            format=A4,
            project="Multi Appendix",
            brand="default",
        )
        report.add_section("Inhoud", content=["Test."])
        report.add_appendix("Eerste bijlage")
        report.add_appendix("Tweede bijlage")

        output = tmp_path / "multi_appendix.pdf"
        report.build(output)

        assert output.exists()

    def test_add_appendix_auto_number(self):
        report = Report(format=A4, project="Test")
        report.add_appendix("Eerste")
        report.add_appendix("Tweede")

        assert report._appendices[0]["number"] == 1
        assert report._appendices[1]["number"] == 2
