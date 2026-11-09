"""Tests voor het brand systeem — dataclasses, loader, renderer, integratie."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openaec_reports.core.brand import (
    BRANDS_DIR,
    BrandConfig,
    BrandLoader,
    ElementConfig,
    ZoneConfig,
)
from openaec_reports.core.brand_renderer import BrandRenderer
from openaec_reports.core.document import A4, MM_TO_PT, DocumentConfig

# ============================================================
# TestBrandConfig — dataclass creatie en defaults
# ============================================================

class TestBrandConfig:
    def test_default_values(self):
        config = BrandConfig()
        assert config.name == "Default"
        assert config.slug == "default"
        assert config.colors == {}
        assert config.fonts == {}
        assert config.header.height == 0.0
        assert config.footer.height == 0.0
        assert config.logos == {}
        assert config.styles == {}
        assert config.pages == {}

    def test_styles_field(self):
        config = BrandConfig(styles={"Normal": {"fontSize": 9.5}})
        assert config.styles["Normal"]["fontSize"] == 9.5

    def test_pages_field(self):
        config = BrandConfig(pages={"colofon": {"label_x_pt": 103}})
        assert config.pages["colofon"]["label_x_pt"] == 103

    def test_custom_values(self):
        config = BrandConfig(
            name="Test Brand",
            slug="test-brand",
            colors={"primary": "#FF0000"},
            fonts={"heading": "Helvetica-Bold"},
            header=ZoneConfig(height=15),
            footer=ZoneConfig(height=12),
            logos={"main": "logos/test.png"},
        )
        assert config.name == "Test Brand"
        assert config.colors["primary"] == "#FF0000"
        assert config.header.height == 15
        assert config.footer.height == 12


class TestElementConfig:
    def test_default_values(self):
        elem = ElementConfig()
        assert elem.type == ""
        assert elem.x == 0.0
        assert elem.y == 0.0
        assert elem.stroke_width == 0.5
        assert elem.align == "left"

    def test_text_element(self):
        elem = ElementConfig(
            type="text",
            content="{project}",
            x=20,
            y=10,
            font="$body",
            size=7.5,
            color="$text_light",
        )
        assert elem.type == "text"
        assert elem.content == "{project}"
        assert elem.font == "$body"


class TestZoneConfig:
    def test_default_values(self):
        zone = ZoneConfig()
        assert zone.height == 0.0
        assert zone.elements == []

    def test_with_elements(self):
        zone = ZoneConfig(
            height=15,
            elements=[ElementConfig(type="text", content="Test")],
        )
        assert zone.height == 15
        assert len(zone.elements) == 1


# ============================================================
# TestBrandLoader — load, load_default, list_brands, errors
# ============================================================

class TestBrandLoader:
    def test_load_default(self):
        loader = BrandLoader()
        brand = loader.load_default()

        assert isinstance(brand, BrandConfig)
        assert brand.name == "Default"
        assert brand.slug == "default"
        assert "primary" in brand.colors
        assert "heading" in brand.fonts
        assert brand.header.height == 15
        assert brand.footer.height == 12

    def test_load_default_header_elements(self):
        loader = BrandLoader()
        brand = loader.load_default()

        assert len(brand.header.elements) == 2
        text_elem = brand.header.elements[0]
        assert text_elem.type == "text"
        assert "{project_number}" in text_elem.content
        line_elem = brand.header.elements[1]
        assert line_elem.type == "line"

    def test_load_default_footer_elements(self):
        loader = BrandLoader()
        brand = loader.load_default()

        assert len(brand.footer.elements) == 3
        types = [e.type for e in brand.footer.elements]
        assert types == ["line", "text", "text"]

    @pytest.mark.skip(
        reason=(
            "default tenant fixtures incomplete post-purge "
            "(verwachte tenant-specifieke brand assertions)"
        )
    )
    def test_load_tenant_brand(self):
        loader = BrandLoader()
        brand = loader.load("default")

        assert brand.name == "OpenAEC"
        assert brand.slug == "default"
        assert brand.colors["secondary"] == "#38BDA0"
        assert brand.colors["text_accent"] == "#56B49B"
        assert brand.header.height == 0
        assert brand.footer.height == 17  # was 25

    @pytest.mark.skip(
        reason="default tenant fixtures incomplete post-purge (verwachte tenant-specifieke footer)"
    )
    def test_load_tenant_brand_footer_elements(self):
        loader = BrandLoader()
        brand = loader.load("default")

        # Nieuwe footer: alleen paginanummer
        assert len(brand.footer.elements) == 1
        text_elem = brand.footer.elements[0]
        assert text_elem.type == "text"
        assert text_elem.content == "{page}"
        assert text_elem.align == "right"
        assert text_elem.size == 9.5

    @pytest.mark.skip(
        reason="default tenant fixtures incomplete post-purge (verwachte tenant-specifieke styles)"
    )
    def test_load_tenant_brand_styles(self):
        loader = BrandLoader()
        brand = loader.load("default")
        assert "Normal" in brand.styles
        assert brand.styles["Normal"]["fontSize"] == 9.5
        assert brand.styles["Heading1"]["fontName"] == "Inter-Regular"
        assert brand.styles["Heading2"]["textColor"] == "#56B49B"

    @pytest.mark.skip(
        reason="default tenant fixtures incomplete post-purge (verwachte tenant-specifieke pages)"
    )
    def test_load_tenant_brand_pages(self):
        loader = BrandLoader()
        brand = loader.load("default")
        assert "colofon" in brand.pages
        assert brand.pages["colofon"]["label_x_pt"] == 103
        assert "appendix_divider" in brand.pages
        assert brand.pages["appendix_divider"]["bg_color"] == "#37BCAB"

    def test_load_nonexistent_raises(self):
        loader = BrandLoader()
        with pytest.raises(FileNotFoundError, match="niet gevonden"):
            loader.load("nonexistent_brand")

    def test_load_with_yaml_extension(self):
        loader = BrandLoader()
        brand = loader.load("default.yaml")
        assert brand.slug == "default"

    def test_list_brands(self):
        loader = BrandLoader()
        brands = loader.list_brands()

        assert isinstance(brands, list)
        assert len(brands) >= 1

        slugs = [b["slug"] for b in brands]
        assert "default" in slugs

    def test_list_brands_empty_dir(self, tmp_path):
        loader = BrandLoader(brands_dir=tmp_path)
        brands = loader.list_brands()
        assert brands == []

    def test_custom_brands_dir(self, tmp_path):
        yaml_content = "brand:\n  name: Custom\n  slug: custom\ncolors:\n  primary: '#123456'\n"
        (tmp_path / "custom.yaml").write_text(yaml_content, encoding="utf-8")

        loader = BrandLoader(brands_dir=tmp_path)
        brand = loader.load("custom")

        assert brand.name == "Custom"
        assert brand.colors["primary"] == "#123456"

    def test_brands_dir_constant(self):
        assert BRANDS_DIR.name == "brands"
        assert BRANDS_DIR.parent.name == "assets"


# ============================================================
# TestBrandRenderer — per element type met mock canvas
# ============================================================

class TestBrandRenderer:
    """Tests voor BrandRenderer met een mock canvas."""

    def _make_renderer(self, brand: BrandConfig | None = None) -> BrandRenderer:
        if brand is None:
            brand = BrandConfig(
                colors={"primary": "#1B3A5C", "text_light": "#7F8C8D", "rule": "#BDC3C7"},
                fonts={"heading": "Helvetica-Bold", "body": "Helvetica"},
            )
        return BrandRenderer(brand, assets_dir=Path(__file__).parent / "fixtures")

    def _make_canvas(self) -> MagicMock:
        canvas = MagicMock()
        canvas.getPageNumber.return_value = 3
        return canvas

    def _make_config(self) -> DocumentConfig:
        return DocumentConfig(
            format=A4,
            project="Testproject",
            project_number="2026-001",
            client="Test BV",
            author="Ing. Test",
            report_type="structural",
        )

    def test_draw_rect_position_and_color(self):
        brand = BrandConfig(
            colors={"secondary": "#38BDA0"},
            footer=ZoneConfig(height=25, elements=[
                ElementConfig(type="rect", x=0, y=0, width=99, height=25, fill="$secondary"),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        config = self._make_config()
        variables = renderer._build_variables(config, 1)

        renderer._draw_zone(canvas, brand.footer, 0.0, variables)

        canvas.setFillColor.assert_called()
        canvas.rect.assert_called_once()
        args, kwargs = canvas.rect.call_args
        assert kwargs["fill"] == 1
        # x=0, y=0, w=99*MM_TO_PT, h=25*MM_TO_PT
        assert abs(args[0] - 0.0) < 0.01
        assert abs(args[1] - 0.0) < 0.01
        assert abs(args[2] - 99 * MM_TO_PT) < 0.1
        assert abs(args[3] - 25 * MM_TO_PT) < 0.1

    def test_draw_text_with_variables(self):
        brand = BrandConfig(
            colors={"text_light": "#7F8C8D"},
            fonts={"body": "Helvetica"},
            footer=ZoneConfig(height=12, elements=[
                ElementConfig(
                    type="text", content="{author} - {project}",
                    x=20, y=3, font="$body", size=7, color="$text_light",
                ),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        config = self._make_config()
        variables = renderer._build_variables(config, 1)

        renderer._draw_zone(canvas, brand.footer, 0.0, variables)

        canvas.setFont.assert_called_with("Helvetica", 7)
        canvas.drawString.assert_called_once()
        drawn_text = canvas.drawString.call_args[0][2]
        assert "Ing. Test" in drawn_text
        assert "Testproject" in drawn_text

    def test_draw_text_alignment_right(self):
        brand = BrandConfig(
            colors={"text": "#2C3E50"},
            fonts={"body": "Helvetica"},
            footer=ZoneConfig(height=12, elements=[
                ElementConfig(
                    type="text", content="Pagina {page}",
                    x=195, y=3, font="$body", size=7, color="$text", align="right",
                ),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        config = self._make_config()
        variables = renderer._build_variables(config, 5)

        renderer._draw_zone(canvas, brand.footer, 0.0, variables)

        canvas.drawRightString.assert_called_once()
        drawn_text = canvas.drawRightString.call_args[0][2]
        assert drawn_text == "Pagina 5"

    def test_draw_text_alignment_center(self):
        brand = BrandConfig(
            colors={},
            fonts={},
            footer=ZoneConfig(height=12, elements=[
                ElementConfig(
                    type="text", content="Centered",
                    x=105, y=5, size=9, color="#000000", align="center",
                ),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        variables = {"page": "1"}

        renderer._draw_zone(canvas, brand.footer, 0.0, variables)

        canvas.drawCentredString.assert_called_once()

    def test_draw_line(self):
        brand = BrandConfig(
            colors={"rule": "#BDC3C7"},
            footer=ZoneConfig(height=12, elements=[
                ElementConfig(type="line", x=20, y=10, width=175, color="$rule", stroke_width=0.5),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        variables = {}

        renderer._draw_zone(canvas, brand.footer, 0.0, variables)

        canvas.setLineWidth.assert_called_with(0.5)
        canvas.line.assert_called_once()
        args = canvas.line.call_args[0]
        x1, y, x2 = args[0], args[1], args[2]
        assert abs(x1 - 20 * MM_TO_PT) < 0.1
        assert abs(y - 10 * MM_TO_PT) < 0.1
        assert abs(x2 - (20 + 175) * MM_TO_PT) < 0.1

    def test_draw_image_missing_file_no_crash(self):
        """Ontbrekende afbeelding mag niet crashen."""
        brand = BrandConfig(
            footer=ZoneConfig(height=25, elements=[
                ElementConfig(type="image", src="logos/nonexistent.png", x=128, y=2, height=20),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        variables = {}

        # Moet niet crashen
        renderer._draw_zone(canvas, brand.footer, 0.0, variables)

        # drawImage mag niet zijn aangeroepen
        canvas.drawImage.assert_not_called()

    def test_resolve_color_dollar_ref(self):
        renderer = self._make_renderer()
        assert renderer._resolve_color("$primary") == "#1B3A5C"
        assert renderer._resolve_color("$text_light") == "#7F8C8D"

    def test_resolve_color_hex_literal(self):
        renderer = self._make_renderer()
        assert renderer._resolve_color("#38BDA0") == "#38BDA0"

    def test_resolve_color_unknown_ref(self):
        renderer = self._make_renderer()
        assert renderer._resolve_color("$nonexistent") == "#45243D"  # BM_COLORS.text

    def test_resolve_font_dollar_ref(self):
        renderer = self._make_renderer()
        assert renderer._resolve_font("$heading") == "Helvetica-Bold"
        assert renderer._resolve_font("$body") == "Helvetica"

    def test_resolve_font_literal(self):
        renderer = self._make_renderer()
        assert renderer._resolve_font("Courier") == "Courier"

    def test_resolve_font_unknown_ref(self):
        renderer = self._make_renderer()
        assert renderer._resolve_font("$nonexistent") == "LiberationSans"

    def test_zero_height_header_skipped(self):
        brand = BrandConfig(
            header=ZoneConfig(height=0, elements=[
                ElementConfig(type="text", content="Mag niet zichtbaar zijn"),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        config = self._make_config()
        variables = renderer._build_variables(config, 1)

        renderer.draw_header(canvas, config, variables)

        # Geen saveState/drawString want zone is overgeslagen
        canvas.saveState.assert_not_called()

    def test_zero_height_footer_skipped(self):
        brand = BrandConfig(
            footer=ZoneConfig(height=0, elements=[
                ElementConfig(type="text", content="Mag niet zichtbaar zijn"),
            ]),
        )
        renderer = self._make_renderer(brand)
        canvas = self._make_canvas()
        config = self._make_config()
        variables = renderer._build_variables(config, 1)

        renderer.draw_footer(canvas, config, variables)

        canvas.saveState.assert_not_called()

    def test_build_variables(self):
        renderer = self._make_renderer()
        config = self._make_config()
        variables = renderer._build_variables(config, 7)

        assert variables["page"] == "7"
        assert variables["project"] == "Testproject"
        assert variables["project_number"] == "2026-001"
        assert variables["client"] == "Test BV"
        assert variables["author"] == "Ing. Test"
        assert variables["report_type"] == "structural"
        assert "date" in variables

    def test_resolve_variables(self):
        renderer = self._make_renderer()
        result = renderer._resolve_variables(
            "{project_number} | {project}",
            {"project_number": "2026-001", "project": "Test"},
        )
        assert result == "2026-001 | Test"


# ============================================================
# TestIntegration — full build met brand parameter
# ============================================================

class TestIntegration:
    """Integratietests: volledige PDF build met brand systeem."""

    def test_build_with_default_brand(self, tmp_path):
        """Report zonder brand parameter gebruikt default brand."""
        from openaec_reports import A4, Report

        report = Report(
            format=A4,
            project="Default Brand Test",
            project_number="2026-100",
            client="Test BV",
            author="Tester",
        )
        report.add_section("Sectie", content=["Tekst met default brand."])

        output = tmp_path / "default_brand.pdf"
        result = report.build(output)

        assert result.exists()
        assert result.stat().st_size > 0

    def test_build_with_named_brand(self, tmp_path):
        """Report met brand='default' bouwt correct."""
        from openaec_reports import A4, Report

        report = Report(
            format=A4,
            project="OpenAEC Brand Test",
            project_number="2026-101",
            client="Klant BV",
            author="OpenAEC Engineering",
            brand="default",
        )
        report.add_section("Sectie", content=["Tekst met OpenAEC brand."])

        output = tmp_path / "openaec_brand.pdf"
        result = report.build(output)

        assert result.exists()
        assert result.stat().st_size > 0

    def test_build_with_brand_config_object(self, tmp_path):
        """Report accepteert een BrandConfig object direct."""
        from openaec_reports import A4, BrandConfig, Report

        brand = BrandConfig(
            name="Custom",
            slug="custom",
            colors={"text": "#000000"},
            fonts={"body": "Helvetica"},
            header=ZoneConfig(height=0),
            footer=ZoneConfig(height=10, elements=[
                ElementConfig(
                    type="text", content="Pagina {page}",
                    x=105, y=3, size=8, color="#000000", align="center",
                ),
            ]),
        )

        report = Report(
            format=A4,
            project="Custom Brand",
            project_number="2026-102",
            brand=brand,
        )
        report.add_section("Test", content=["Custom brand object."])

        output = tmp_path / "custom_brand.pdf"
        result = report.build(output)

        assert result.exists()
        assert result.stat().st_size > 0

    def test_backward_compatible_no_brand(self, tmp_path):
        """Bestaande code zonder brand parameter blijft werken."""
        from openaec_reports import A4, Report

        report = Report(format=A4, project="Backward Compat")
        report.add_section("Test", content=["Oude API blijft werken."])

        output = tmp_path / "compat.pdf"
        result = report.build(output)

        assert result.exists()

    def test_build_with_cover_and_brand(self, tmp_path):
        """Volledige report met cover, secties, backcover en brand."""
        from openaec_reports import A4, Report

        report = Report(
            format=A4,
            project="Volledig Rapport",
            project_number="2026-103",
            client="Klant",
            author="Auteur",
            brand="default",
        )
        report.add_cover(subtitle="Test Rapport")
        report.add_section("Hoofdstuk 1", content=["Inhoud."])
        report.add_section("Hoofdstuk 2", content=["Meer inhoud."])
        report.add_backcover()

        output = tmp_path / "full_brand.pdf"
        result = report.build(output)

        assert result.exists()
        assert result.stat().st_size > 1000
