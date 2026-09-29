"""Tests voor de renderer_v2-uitbreidingen voor het bouwveiligheidsplan (E1-E9)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

fitz = pytest.importorskip("fitz", reason="pymupdf niet geinstalleerd")

from openaec_reports.core.renderer_v2 import ContentRenderer, FontManager  # noqa: E402

H1_STYLE = {
    "number": {"x": 90.0, "font": "Inter-Regular", "size": 18.0},
    "title": {"x": 108.0, "font": "Inter-Regular", "size": 18.0},
}


@pytest.fixture(scope="module")
def fake_renderer():
    return SimpleNamespace(fonts=FontManager())


class TestHeadingTitleX:
    """E1: titel schuift op als het nummer breder is dan de template aanneemt."""

    def _x(self, renderer, style, number):
        return ContentRenderer._heading_title_x(renderer, style, number)

    def test_single_digit_keeps_template_x(self, fake_renderer):
        assert self._x(fake_renderer, H1_STYLE, "4") == 108.0

    def test_empty_number_keeps_template_x(self, fake_renderer):
        assert self._x(fake_renderer, H1_STYLE, "") == 108.0

    def test_letter_keeps_template_x(self, fake_renderer):
        assert self._x(fake_renderer, H1_STYLE, "B") == 108.0

    def test_two_digits_shift_by_one_digit_width(self, fake_renderer):
        font = fake_renderer.fonts.get_fitz_font("Inter-Regular")
        digit = font.text_length("0", fontsize=18.0)
        assert self._x(fake_renderer, H1_STYLE, "12") == pytest.approx(108.0 + digit)

    def test_title_clears_number(self, fake_renderer):
        font = fake_renderer.fonts.get_fitz_font("Inter-Regular")
        end_of_number = 90.0 + font.text_length("12", fontsize=18.0)
        assert self._x(fake_renderer, H1_STYLE, "12") > end_of_number

    def test_heading2_multi_part_number(self, fake_renderer):
        style = {
            "number": {"x": 125.4, "font": "Inter-Regular", "size": 10.0},
            "title": {"x": 147.0, "font": "Inter-Regular", "size": 13.0},
        }
        assert self._x(fake_renderer, style, "4.1") == 147.0
        assert self._x(fake_renderer, style, "12.10") > 147.0

    def test_explicit_number_gap(self, fake_renderer):
        style = {**H1_STYLE, "number_gap": 20.0}
        font = fake_renderer.fonts.get_fitz_font("Inter-Regular")
        width = font.text_length("4", fontsize=18.0)
        assert self._x(fake_renderer, style, "4") == pytest.approx(max(108.0, 90.0 + width + 20))


class TestRunColor:
    """E2: runkleur uit merk, alias of hex; onbekend valt terug op de basiskleur."""

    @pytest.fixture
    def fake(self):
        return SimpleNamespace(
            _brand_config=SimpleNamespace(colors={"text_light": "#6B7975", "warning": "#A6342B"}),
            _COLOR_ALIASES=ContentRenderer._COLOR_ALIASES,
        )

    def test_hex(self, fake):
        assert ContentRenderer._run_color(fake, "#123456", "#000000") == "#123456"

    def test_brand_key_and_ref(self, fake):
        assert ContentRenderer._run_color(fake, "warning", "#000000") == "#A6342B"
        assert ContentRenderer._run_color(fake, "$colors.warning", "#000000") == "#A6342B"

    def test_alias(self, fake):
        assert ContentRenderer._run_color(fake, "grijs", "#000000") == "#6B7975"

    def test_unknown_and_empty_fall_back(self, fake):
        assert ContentRenderer._run_color(fake, "paars", "#000000") == "#000000"
        assert ContentRenderer._run_color(fake, None, "#000000") == "#000000"


class TestCellSpec:
    """E3: tabelcellen als object, cell_styles, gewone cellen ongewijzigd."""

    @pytest.fixture
    def fake(self):
        fake = SimpleNamespace(
            _brand_config=SimpleNamespace(colors={"surface": "#F4F8F7", "text_light": "#6B7975"}),
            _COLOR_ALIASES=ContentRenderer._COLOR_ALIASES,
        )
        fake._run_color = lambda value, default: ContentRenderer._run_color(fake, value, default)
        return fake

    def spec(self, fake, value, styles=None, row=0, col=0):
        return ContentRenderer._cell_spec(fake, value, row, col, styles or {})

    def test_plain_cell_keeps_old_path(self, fake):
        sp = self.spec(fake, "<b>Totaal</b>")
        assert (sp.text, sp.bold, sp.rich) == ("Totaal", True, False)

    def test_number_cell(self, fake):
        sp = self.spec(fake, 3.5)
        assert (sp.text, sp.rich) == ("3.5", False)

    def test_object_cell(self, fake):
        sp = self.spec(fake, {"text": "2", "bg_color": "#FFF3C4", "align": "center", "bold": True})
        assert sp.rich and sp.bold and sp.align == "center" and sp.bg_color == "#FFF3C4"

    def test_runs_cell_plain_text(self, fake):
        sp = self.spec(fake, {"runs": [{"text": "Stand "}, {"label": {"text": "ntb"}}]})
        assert sp.text == "Stand ntb" and sp.runs

    def test_cell_styles_applies_to_plain_cell(self, fake):
        sp = self.spec(fake, "a", {"1,2": {"italic": True, "text_color": "grijs"}}, row=1, col=2)
        assert sp.rich and sp.italic and sp.color == "#6B7975" and sp.text == "a"

    def test_cell_value_overrides_cell_styles(self, fake):
        sp = self.spec(fake, {"text": "x", "align": "right"}, {"0,0": {"align": "center"}})
        assert sp.align == "right"

    def test_invalid_align_falls_back_left(self, fake):
        assert self.spec(fake, {"text": "x", "align": "justify"}).align == "left"


TENANTS_DIR = Path(__file__).parent.parent / "tenants"


@pytest.mark.skipif(
    not (TENANTS_DIR / "3bm" / "stationery" / "standaard.pdf").exists(),
    reason="private tenant 3bm niet aanwezig (tenants/ zit niet in git)",
)
def test_render_bvp_blocks_smoke(tmp_path, monkeypatch):
    """E2-E4 renderen samen zonder fouten; tekst en labels staan in de PDF."""
    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(TENANTS_DIR))
    monkeypatch.setenv("OPENAEC_TENANTS_DIR", str(TENANTS_DIR))
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    ntb = {"label": {"text": "n.t.b.", "kind": "ntb"}}
    data = {
        "project": "Rooktest",
        "template": "standaard",
        "colofon": {"enabled": False},
        "toc": {"enabled": False},
        "sections": [{"title": "Blokken", "number": "12", "content": [
            {"type": "paragraph", "runs": [{"text": "Stand ", "bold": True}, ntb]},
            {"type": "bullet_list", "items": ["los", {"runs": [{"text": "runs"}]}]},
            {"type": "table", "rows": [["Groep", ""], ["a", {"text": "3", "bg_color": "#FAD7B5"}]],
             "row_styles": [{"row": 0, "style": "group"}]},
            {"type": "definition_list", "rows": [{"label": "Opdrachtgever", "value": "BV"}]},
            {"type": "checklist", "columns": 2, "items": [
                {"text": "Vooropname", "checked": True}, {"text": "Nulmeting", "checked": None},
            ]},
        ]}],
    }
    out = tmp_path / "bvp.pdf"
    ReportGeneratorV2(brand="3bm", tenant_slug="3bm").generate(
        data, TENANTS_DIR / "3bm" / "stationery", out
    )
    text = "".join(page.get_text() for page in fitz.open(str(out)))
    for expected in (
        "Stand", "N.T.B.", "Groep", "Vooropname", "Nulmeting", "runs", "Opdrachtgever",
    ):
        assert expected in text


class TestDisplayPageNr:
    """TOC-paginanummer als inhoud doorloopt op een al genummerde pagina."""

    def test_fresh_page(self):
        fake = SimpleNamespace(current_page_nr=5, _page_number_written=False)
        assert ContentRenderer._display_page_nr(fake) == 5

    def test_page_number_already_stamped(self):
        # _add_page_number schreef "5" en hoogde de teller op naar 6.
        fake = SimpleNamespace(current_page_nr=6, _page_number_written=True)
        assert ContentRenderer._display_page_nr(fake) == 5


@pytest.mark.skipif(
    not (TENANTS_DIR / "3bm" / "stationery" / "standaard.pdf").exists(),
    reason="private tenant 3bm niet aanwezig (tenants/ zit niet in git)",
)
def test_level1_without_page_break(tmp_path, monkeypatch):
    """E7: continue_on_page houdt een level-1-hoofdstuk op dezelfde pagina.

    page_break_before: false verandert niets: clients (docs/ai-instructions.md,
    Tauri-template) sturen dat standaard mee.
    """
    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(TENANTS_DIR))
    monkeypatch.setenv("OPENAEC_TENANTS_DIR", str(TENANTS_DIR))
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    def pages(**fields):
        second = {"title": "Tweede", "content": [{"type": "paragraph", "text": "b"}], **fields}
        data = {
            "project": "E7", "template": "standaard",
            "colofon": {"enabled": False}, "toc": {"enabled": False},
            "backcover": {"enabled": False},
            "sections": [
                {"title": "Eerste", "content": [{"type": "paragraph", "text": "a"}]},
                second,
            ],
        }
        out = tmp_path / f"e7_{len(list(tmp_path.iterdir()))}.pdf"
        ReportGeneratorV2(brand="3bm", tenant_slug="3bm").generate(
            data, TENANTS_DIR / "3bm" / "stationery", out
        )
        return len(fitz.open(str(out)))

    base = pages()
    assert pages(continue_on_page=True) == base - 1
    assert pages(page_break_before=False) == base
    assert pages(continue_on_page=True, page_break_before=True) == base


@pytest.mark.skipif(
    not (TENANTS_DIR / "3bm" / "stationery" / "standaard.pdf").exists(),
    reason="private tenant 3bm niet aanwezig (tenants/ zit niet in git)",
)
def test_part_and_reference_in_heading_and_toc(tmp_path, monkeypatch):
    """E8: deelkop en verwijzing staan in de kop en in de inhoudsopgave."""
    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(TENANTS_DIR))
    monkeypatch.setenv("OPENAEC_TENANTS_DIR", str(TENANTS_DIR))
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    data = {
        "project": "E8", "template": "standaard",
        "colofon": {"enabled": False}, "backcover": {"enabled": False},
        "sections": [{
            "title": "Risicomatrix", "part": "DEEL B - RISICO'S", "reference": "Bbl 7.4",
            "content": [{"type": "paragraph", "text": "a"}],
        }],
    }
    out = tmp_path / "e8.pdf"
    ReportGeneratorV2(brand="3bm", tenant_slug="3bm").generate(
        data, TENANTS_DIR / "3bm" / "stationery", out
    )
    doc = fitz.open(str(out))
    toc_page = next(p for p in doc if "Inhoud" in p.get_text())
    heading_page = next(p for p in doc if "Risicomatrix" in p.get_text() and p != toc_page)
    for page in (toc_page, heading_page):
        text = page.get_text()
        assert "DEEL B - RISICO'S" in text and "Bbl 7.4" in text


@pytest.mark.skipif(
    not (TENANTS_DIR / "3bm" / "stationery" / "standaard.pdf").exists(),
    reason="private tenant 3bm niet aanwezig (tenants/ zit niet in git)",
)
def test_header_label_on_content_pages_only(tmp_path, monkeypatch):
    """E9: statuslabel op TOC- en inhoudspagina's, niet op cover of achterblad."""
    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(TENANTS_DIR))
    monkeypatch.setenv("OPENAEC_TENANTS_DIR", str(TENANTS_DIR))
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    data = {
        "project": "E9", "template": "standaard", "header_label": "Concept",
        "colofon": {"enabled": False},
        "sections": [
            {"title": "Een", "content": [{"type": "paragraph", "text": "a"}]},
            {"title": "Twee", "content": [{"type": "paragraph", "text": "b"}]},
        ],
    }
    out = tmp_path / "e9.pdf"
    ReportGeneratorV2(brand="3bm", tenant_slug="3bm").generate(
        data, TENANTS_DIR / "3bm" / "stationery", out
    )
    flags = ["CONCEPT" in page.get_text() for page in fitz.open(str(out))]
    # cover, toc, hfst 1, hfst 2, achterblad
    assert flags == [False, True, True, True, False]


SVG = (
    '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
    '<rect x="10" y="10" width="180" height="80" fill="none" stroke="red"/></svg>'
)


class TestIsSvg:
    """E6: SVG herkennen op extensie of inhoud."""

    def test_by_extension(self, tmp_path):
        from openaec_reports.core.renderer_v2 import _is_svg

        path = tmp_path / "a.svg"
        path.write_text("x")
        assert _is_svg(path)

    def test_by_content(self, tmp_path):
        from openaec_reports.core.renderer_v2 import _is_svg

        path = tmp_path / "a.jpg"
        path.write_text(SVG)
        assert _is_svg(path)

    def test_png_is_not_svg(self, tmp_path):
        from openaec_reports.core.renderer_v2 import _is_svg

        path = tmp_path / "a.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert not _is_svg(path)


@pytest.mark.skipif(
    not (TENANTS_DIR / "3bm" / "stationery" / "standaard.pdf").exists(),
    reason="private tenant 3bm niet aanwezig (tenants/ zit niet in git)",
)
def test_svg_image_is_placed_as_vector(tmp_path, monkeypatch):
    """E6: SVG wordt vector (tekenpaden, geen rasterbeeld); kapotte SVG geeft zichtbare fout."""
    import base64

    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(TENANTS_DIR))
    monkeypatch.setenv("OPENAEC_TENANTS_DIR", str(TENANTS_DIR))
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    good = tmp_path / "fig.svg"
    good.write_text(SVG)
    broken = tmp_path / "kapot.svg"
    broken.write_text("<svg xmlns='http://www.w3.org/2000/svg'><rect width=")
    b64 = {"data": base64.b64encode(SVG.encode()).decode(), "media_type": "image/svg+xml"}
    data = {
        "project": "E6", "template": "standaard",
        "colofon": {"enabled": False}, "toc": {"enabled": False},
        "backcover": {"enabled": False},
        "sections": [{"title": "Figuren", "content": [
            {"type": "image", "src": str(good), "width_mm": 80},
            {"type": "image", "src": b64, "width_mm": 80},
            {"type": "image", "src": str(broken)},
        ]}],
    }
    out = tmp_path / "e6.pdf"
    ReportGeneratorV2(brand="3bm", tenant_slug="3bm").generate(
        data, TENANTS_DIR / "3bm" / "stationery", out
    )
    page = fitz.open(str(out))[-1]
    red_rects = [
        d for d in page.get_drawings()
        if d.get("color") and d["color"][0] > 0.9 and d["color"][1] < 0.1
    ]
    assert len(red_rects) >= 2
    assert "SVG kon niet worden geplaatst: kapot.svg" in page.get_text()


class TestReviewFixes:
    """Gemeten review-bevindingen 29-09 (tweede Claude)."""

    @pytest.fixture
    def fake(self):
        fake = SimpleNamespace(
            _brand_config=SimpleNamespace(colors={}),
            _COLOR_ALIASES=ContentRenderer._COLOR_ALIASES,
        )
        fake._run_color = lambda value, default: ContentRenderer._run_color(fake, value, default)
        return fake

    def test_zero_cell_text_is_kept(self, fake):
        sp = ContentRenderer._cell_spec(fake, {"text": 0, "bold": True}, 0, 0, {})
        assert sp.text == "0"


class TestTitleLines:
    """F1: titel breekt af binnen de beschikbare breedte; past hij, dan ongewijzigd."""

    def lines(self, fake, title, max_w):
        return ContentRenderer._title_lines(fake, title, "Inter-Regular", 12.0, max_w)

    def test_fits_single_line(self, fake_renderer):
        assert self.lines(fake_renderer, "Korte titel", 500) == ["Korte titel"]

    def test_wraps_within_width(self, fake_renderer):
        title = "Bouwmethode, bouwplaatsinrichting en veiligheidszones"
        out = self.lines(fake_renderer, title, 200)
        font = fake_renderer.fonts.get_fitz_font("Inter-Regular")
        assert len(out) > 1 and " ".join(out) == title
        assert all(font.text_length(line, fontsize=12.0) <= 200 for line in out)


@pytest.mark.skipif(
    not (TENANTS_DIR / "3bm" / "stationery" / "standaard.pdf").exists(),
    reason="private tenant 3bm niet aanwezig (tenants/ zit niet in git)",
)
def test_table_header_not_orphaned(tmp_path, monkeypatch):
    """F2: kop + de eerste 2 rijen staan op dezelfde pagina."""
    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(TENANTS_DIR))
    monkeypatch.setenv("OPENAEC_TENANTS_DIR", str(TENANTS_DIR))
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    # Spacer in stappen kleiner dan een tabelrij, zodat de kop ergens precies
    # boven de paginabodem valt met ruimte voor 0 of 1 rij.
    for height_mm in range(184, 204, 2):
        content = [{"type": "spacer", "height_mm": height_mm}, {
            "type": "table", "title": "Maatregelen", "headers": ["KopA", "KopB"],
            "rows": [["RijEen", "x"], ["RijTwee", "y"], ["RijDrie", "z"]],
        }]
        data = {
            "project": "F2", "template": "standaard",
            "colofon": {"enabled": False}, "toc": {"enabled": False},
            "backcover": {"enabled": False},
            "sections": [{"title": "T", "content": content}],
        }
        out = tmp_path / f"f2_{height_mm}.pdf"
        ReportGeneratorV2(brand="3bm", tenant_slug="3bm").generate(
            data, TENANTS_DIR / "3bm" / "stationery", out
        )
        for page in fitz.open(str(out)):
            text = page.get_text()
            if "Maatregelen" in text:
                assert "KopA" in text, f"tabeltitel los bij spacer {height_mm} mm"
            if "KopA" in text and "RijDrie" not in text:
                assert "RijEen" in text and "RijTwee" in text, f"wees bij spacer {height_mm} mm"


class TestFontByName:
    """G0: template-fontnamen resolven naar het font-bestand in de cascade."""

    @pytest.fixture
    def fm(self, tmp_path):
        from openaec_reports.core.renderer_v2 import FONT_DIR

        # Liberation Bold onder een tenant-achtige naam: meetbaar anders dan book.
        (tmp_path / "Testfont-Book.ttf").write_bytes(
            (FONT_DIR / "LiberationSans-Bold.ttf").read_bytes()
        )
        return FontManager(font_dir=tmp_path)

    def test_stripped_name_resolves_file(self, fm):
        assert fm.get_fitz_font("TestfontBook").name == "Liberation Sans Bold"
        assert fm.get_fitz_font("Testfont-Book").name == "Liberation Sans Bold"

    def test_unknown_name_falls_back(self, fm):
        assert fm.get_fitz_font("BestaatNiet") is fm.get_fitz_font("OokNiet")

    def test_measure_uses_named_font(self, fm):
        text = "Bouwveiligheidsplan"
        assert fm.measure(text, 10, fontname="TestfontBook") > fm.measure(text, 10)


class TestTableBoldFont:
    """Legacy <b>-cellen: vette font ook als de body-font geen 'Bold'-variant heeft."""

    def bold(self, body, header, role):
        fake = SimpleNamespace(_font_role=lambda name: role if name == "bold" else None)
        return ContentRenderer._table_bold_fontname(fake, body, header)

    def test_template_bold_role(self):
        assert self.bold("SegoeUI", "SegoeUI", "SegoeUI-Bold") == "SegoeUI-Bold"
        assert self.bold("GothamBook", "GothamBook", "GothamBold") == "GothamBold"

    def test_derived_bold_unchanged(self):
        assert self.bold("Inter-Regular", "Inter-Bold", "X") == "Inter-Bold"
        assert self.bold("Foo", "Foo-Bold", None) == "Foo-Bold"

    def test_no_role_falls_back_to_liberation_bold(self):
        assert self.bold("SegoeUI", "SegoeUI", None) == "LiberationSans-Bold"
