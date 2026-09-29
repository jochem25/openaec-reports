"""Tests voor core/page_frame: paginakader, statische pagina's, text-segmenten (E10)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

fitz = pytest.importorskip("fitz", reason="pymupdf niet geinstalleerd")

from openaec_reports.core.page_frame import (  # noqa: E402
    apply_frame,
    brand_page_elements,
    render_static_pages,
    static_context,
)
from openaec_reports.core.renderer_v2 import FontManager  # noqa: E402

FRAME = [
    {"type": "line", "x1": 22, "y1": 28, "x2": 188, "y2": 28, "width": 0.35, "color": "#DFE7E4"},
    {"type": "text", "content": "Pagina {page} van {page_count}", "x": 188, "y": 280,
     "font": "LiberationSans", "size": 7.5, "align": "right", "color": "#6B7975"},
    {"type": "text", "x": 22, "y": 280, "size": 7.5, "segments": [
        {"content": "{company_name}", "font": "LiberationSans-Bold", "color": "#0D6862"},
        {"content": " - {project_number}", "font": "LiberationSans", "color": "#6B7975"},
    ]},
]


def brand(pages: dict | None = None):
    return SimpleNamespace(
        pages=pages or {}, contact={"name": "Kolthof Bouwadvies"}, brand_dir=None,
        tenant="test", slug="test",
    )


def blank_pdf(path, n):
    doc = fitz.open()
    for _ in range(n):
        doc.new_page()
    doc.save(str(path))
    doc.close()


def test_brand_page_elements():
    assert brand_page_elements(brand({"frame": {"static_elements": FRAME}}), "frame") == FRAME
    assert brand_page_elements(brand(), "frame") is None
    assert brand_page_elements(None, "frame") is None


def test_static_context_header_label_dict():
    ctx = static_context({"header_label": {"text": "Concept"}, "project": "P"}, brand())
    assert ctx["header_label"] == "Concept" and ctx["project"] == "P"
    assert ctx["company_name"] == "Kolthof Bouwadvies"


def test_apply_frame_skips_cover_and_backcover(tmp_path):
    pdf = tmp_path / "r.pdf"
    blank_pdf(pdf, 4)
    ctx = static_context({"project_number": "2459"}, brand())
    n = apply_frame(pdf, FRAME, ctx, skip_first=True, skip_last=True,
                    brand_config=brand(), fonts=FontManager())
    assert n == 2
    texts = [p.get_text() for p in fitz.open(str(pdf))]
    assert "Pagina" not in texts[0] and "Pagina" not in texts[3]
    assert "Pagina 2 van 4" in texts[1] and "Pagina 3 van 4" in texts[2]
    # segmenten achter elkaar op een regel
    assert "Kolthof Bouwadvies - 2459" in texts[1].replace("\n", "")


def test_apply_frame_landscape_page_keeps_size(tmp_path):
    pdf = tmp_path / "r.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.new_page(width=842, height=595)
    doc.save(str(pdf))
    doc.close()
    landscape = [dict(el, y=193) if el.get("type") == "text" else el for el in FRAME]
    n = apply_frame(pdf, FRAME, static_context({}, brand()), skip_first=False,
                    skip_last=False, brand_config=brand(), fonts=FontManager(),
                    elements_landscape=landscape)
    out = fitz.open(str(pdf))
    assert n == 2
    assert round(out[1].rect.width) == 842 and "Pagina 2 van 2" in out[1].get_text()


def test_apply_frame_skips_landscape_without_landscape_frame(tmp_path):
    pdf = tmp_path / "r.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.new_page(width=842, height=595)
    doc.save(str(pdf))
    doc.close()
    n = apply_frame(pdf, FRAME, static_context({}, brand()), skip_first=False,
                    skip_last=False, brand_config=brand(), fonts=FontManager())
    assert n == 1


def test_render_static_pages_one_page_per_spec():
    doc = render_static_pages(
        [(FRAME, {"page": "1", "page_count": "1"}), (FRAME, {"page": "7", "page_count": "9"})],
        brand_config=brand(), fonts=FontManager(), block="test",
    )
    assert len(doc) == 2 and "Pagina 7 van 9" in doc[1].get_text()


def test_row_char_space_does_not_leak(tmp_path):
    """F10: letterspatiering in een row-item mag latere segmenten niet verbreden."""
    elements = [
        {"type": "row", "x1": 22, "x2": 188, "y": 20, "items": [
            {"width": 12},
            {"content": "LABEL", "font": "LiberationSans", "size": 6.5, "char_space": 2.0,
             "color": "#A6342B"},
            {"content": "Titel", "font": "LiberationSans", "size": 7.5, "color": "#6B7975"},
        ]},
        {"type": "text", "x": 22, "y": 280, "size": 7.5, "segments": [
            {"content": "Kolthof Bouwadvies", "font": "LiberationSans-Bold", "color": "#0D6862"},
            {"content": " - 2459", "font": "LiberationSans", "color": "#6B7975"},
        ]},
    ]
    doc = render_static_pages([(elements, {})], brand_config=brand(), fonts=FontManager(),
                              block="test")
    spans = [
        sp for b in doc[0].get_text("dict")["blocks"] for ln in b.get("lines", [])
        for sp in ln["spans"] if sp["bbox"][1] > 700
    ]
    bold = next(sp for sp in spans if "Kolthof" in sp["text"])
    rest = next(sp for sp in spans if "2459" in sp["text"])
    assert rest["bbox"][0] >= bold["bbox"][2] - 0.5
