"""Tests voor page_templates.py — stationery-first callbacks en text zones."""

from pathlib import Path
from unittest.mock import MagicMock

from openaec_reports.core.brand import BrandConfig, BrandLoader
from openaec_reports.core.document import A4, DocumentConfig
from openaec_reports.core.page_templates import (
    _draw_text_zones,
    _resolve_binding,
    create_page_templates,
)

TENANTS =Path(__file__).resolve().parent.parent / "tenants"


def _make_config(**kwargs) -> DocumentConfig:
    defaults = {
        "project": "Test Project",
        "project_number": "2026-001",
        "client": "Test BV",
        "author": "Ing. Test",
        "report_type": "structural",
        "subtitle": "Test subtitle",
    }
    defaults.update(kwargs)
    return DocumentConfig(format=A4, **defaults)


class TestCreatePageTemplates:
    def test_returns_five_templates(self):
        config = _make_config()
        templates = create_page_templates(config)
        assert len(templates) == 5

    def test_template_ids_correct(self):
        config = _make_config()
        templates = create_page_templates(config)
        ids = [t.id for t in templates]
        assert ids == ["cover", "colofon", "content", "appendix_divider", "backcover"]

    def test_with_brand_parameter(self):
        config = _make_config()
        brand = BrandLoader().load("default")
        templates = create_page_templates(config, brand=brand)
        assert len(templates) == 5

    def test_content_frame_from_brand(self):
        """Content frame dimensies komen uit brand stationery config."""
        config = _make_config()
        # Publieke default-tenant (tenants/default), los van env-instellingen.
        brand = BrandLoader(tenants_root=TENANTS).load("default")
        templates = create_page_templates(config, brand=brand)

        content = templates[2]  # content template
        frame = content.frames[0]
        cf = brand.stationery["content"].content_frame
        assert cf, "default brand hoort een content_frame te hebben"
        assert abs(frame._x1 - cf["x_pt"]) < 1.0
        assert abs(frame._width - cf["width_pt"]) < 1.0


class TestResolveBinding:
    def test_project(self):
        config = _make_config(project="Mijn Project")
        brand = BrandConfig()
        result = _resolve_binding("project", config, brand)
        assert result == "Mijn Project"

    def test_project_number(self):
        config = _make_config(project_number="2026-042")
        brand = BrandConfig()
        result = _resolve_binding("project_number", config, brand)
        assert result == "2026-042"

    def test_client(self):
        config = _make_config(client="Klant BV")
        brand = BrandConfig()
        result = _resolve_binding("client", config, brand)
        assert result == "Klant BV"

    def test_author(self):
        config = _make_config(author="Ing. J. Test")
        brand = BrandConfig()
        result = _resolve_binding("author", config, brand)
        assert result == "Ing. J. Test"

    def test_subtitle(self):
        config = _make_config(subtitle="Constructief")
        brand = BrandConfig()
        result = _resolve_binding("subtitle", config, brand)
        assert result == "Constructief"

    def test_contact_binding(self):
        config = _make_config()
        brand = BrandConfig(contact={"name": "OpenAEC", "website": "open-aec.com"})
        result = _resolve_binding("contact.name", config, brand)
        assert result == "OpenAEC"

    def test_unknown_binding_returns_empty(self):
        config = _make_config()
        brand = BrandConfig()
        result = _resolve_binding("nonexistent", config, brand)
        assert result == ""

    def test_contact_unknown_key_returns_empty(self):
        config = _make_config()
        brand = BrandConfig(contact={"name": "OpenAEC"})
        result = _resolve_binding("contact.unknown", config, brand)
        assert result == ""


class TestDrawTextZones:
    def test_draws_text_with_correct_font(self):
        canvas = MagicMock()
        config = _make_config(project="TestProject")
        brand = BrandConfig(
            fonts={"body": "Helvetica", "heading": "Helvetica-Bold"},
            colors={"text": "#000000"},
        )
        text_zones = [
            {
                "type": "text",
                "bind": "project",
                "font": "$fonts.body",
                "color": "$colors.text",
                "size": 12.0,
                "x_pt": 100,
                "y_pt": 200,
            }
        ]

        _draw_text_zones(canvas, text_zones, config, brand, 595, 842)

        canvas.saveState.assert_called()
        canvas.setFont.assert_called()
        canvas.drawString.assert_called_once()
        drawn_text = canvas.drawString.call_args[0][2]
        assert drawn_text == "TestProject"

    def test_skips_non_text_zones(self):
        canvas = MagicMock()
        config = _make_config()
        brand = BrandConfig()
        text_zones = [
            {"type": "clipped_image", "src": "something.png"},
        ]

        _draw_text_zones(canvas, text_zones, config, brand, 595, 842)

        canvas.drawString.assert_not_called()

    def test_skips_empty_binding(self):
        canvas = MagicMock()
        config = _make_config(project="")
        brand = BrandConfig(fonts={"body": "Helvetica"}, colors={"text": "#000"})
        text_zones = [
            {
                "type": "text",
                "bind": "project",
                "font": "$fonts.body",
                "color": "$colors.text",
                "size": 10,
                "x_pt": 50,
                "y_pt": 100,
            }
        ]

        _draw_text_zones(canvas, text_zones, config, brand, 595, 842)

        canvas.drawString.assert_not_called()

    def test_right_alignment(self):
        canvas = MagicMock()
        config = _make_config(project="Test")
        brand = BrandConfig(fonts={"body": "Helvetica"}, colors={"text": "#000"})
        text_zones = [
            {
                "type": "text",
                "bind": "project",
                "font": "$fonts.body",
                "color": "$colors.text",
                "size": 10,
                "x_pt": 500,
                "y_pt": 100,
                "align": "right",
            }
        ]

        _draw_text_zones(canvas, text_zones, config, brand, 595, 842)

        canvas.drawRightString.assert_called_once()

    def test_center_alignment(self):
        canvas = MagicMock()
        config = _make_config(project="Test")
        brand = BrandConfig(fonts={"body": "Helvetica"}, colors={"text": "#000"})
        text_zones = [
            {
                "type": "text",
                "bind": "project",
                "font": "$fonts.body",
                "color": "$colors.text",
                "size": 10,
                "x_pt": 300,
                "y_pt": 100,
                "align": "center",
            }
        ]

        _draw_text_zones(canvas, text_zones, config, brand, 595, 842)

        canvas.drawCentredString.assert_called_once()

    def test_y_coordinate_conversion(self):
        """y_pt (top-down) wordt geconverteerd naar ReportLab (bottom-up)."""
        canvas = MagicMock()
        config = _make_config(project="Test")
        brand = BrandConfig(fonts={"body": "Helvetica"}, colors={"text": "#000"})
        ph = 842.0
        y_pt = 200.0
        text_zones = [
            {
                "type": "text",
                "bind": "project",
                "font": "$fonts.body",
                "color": "$colors.text",
                "size": 10,
                "x_pt": 50,
                "y_pt": y_pt,
            }
        ]

        _draw_text_zones(canvas, text_zones, config, brand, 595, ph)

        # ReportLab y = ph - y_pt = 842 - 200 = 642
        args = canvas.drawString.call_args[0]
        rl_y = args[1]
        assert abs(rl_y - (ph - y_pt)) < 0.1
