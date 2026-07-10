"""Landscape-tabel gebruikt de volle paginabreedte, niet het portret-vak.

Regressie voor de bug gemeld door administratie/ypsilon (2026-07-10): een brede
tabel op A4-landscape werd in het portret-``max_width`` (~146mm) geperst,
waardoor kolommen per woord afbraken en de laatste kolom buiten beeld viel.
"""
from __future__ import annotations

import os
from pathlib import Path

import fitz

TENANTS = Path(__file__).resolve().parent.parent / "tenants"


def _render(orientation: str, out: Path, level: str = "section") -> fitz.Document:
    os.environ["OPENAEC_TENANTS_ROOT"] = str(TENANTS)
    os.environ["OPENAEC_TENANTS_DIR"] = str(TENANTS)
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    cols = ["Project", "Adres", "Concept", "Aangevraagd", "In behandeling",
            "Aanvulling", "Vergund", "Bezwaar", "Onherroepelijk", "Verleend"]
    rows = [["A", "Straat 1"] + ["n.v.t."] * 8]
    table = {"type": "table", "title": "Status", "headers": cols, "rows": rows,
             "column_widths": [14, 22] + [8] * 8}
    data = {
        "template": "standaard", "report_type": "T", "project": "P",
        "date": "2026-01-01", "version": "1.0", "status": "C", "client": "X",
        "cover": {"image": str(TENANTS / "kba" / "logos" / "kba.png")},
    }
    if level == "section":
        data["sections"] = [{"title": "S", "orientation": orientation,
                             "content": [table]}]
    else:  # top-level document-oriëntatie (zoals ypsilon stuurt)
        data["orientation"] = orientation
        data["sections"] = [{"title": "S", "content": [table]}]
    ReportGeneratorV2(brand="kba", tenant_slug="kba").generate(
        data, TENANTS / "kba" / "stationery", out)
    return fitz.open(out)


def _table_right_edge(doc: fitz.Document) -> tuple[float, float, bool]:
    """Zoek de content-pagina en geef (paginabreedte, rechterrand tabeltekst,
    'Verleend' zichtbaar binnen de pagina)."""
    for page in doc:
        words = page.get_text("words")
        labels = {w[4] for w in words}
        if "Verleend" in labels:
            right = max(w[2] for w in words)
            verleend_ok = any(
                w[4] == "Verleend" and w[2] <= page.rect.width for w in words)
            return page.rect.width, right, verleend_ok
    raise AssertionError("geen tabelpagina met 'Verleend' gevonden")


def test_landscape_table_spans_wider_than_portrait_box(tmp_path):
    ls = _render("landscape", tmp_path / "ls.pdf")
    pw, right, verleend_ok = _table_right_edge(ls)
    assert pw > 800, f"landscape-pagina moet ~842pt breed zijn, was {pw}"
    # Portret-vak liep tot ~541pt; landscape moet daar ruim overheen.
    assert right > 700, f"tabel te smal in landscape (rechterrand {right}pt)"
    assert verleend_ok, "laatste kolom 'Verleend' valt buiten de pagina"


def test_top_level_orientation_makes_document_landscape(tmp_path):
    """Top-level ``orientation`` (zoals ypsilon stuurt) moet de content
    landscape maken — voorheen stil genegeerd, waardoor de pagina portret
    bleef en de tabel op de portret-cap (~538pt) hing."""
    ls = _render("landscape", tmp_path / "top.pdf", level="document")
    pw, right, verleend_ok = _table_right_edge(ls)
    assert pw > 800, f"top-level orientation moet landscape geven, was {pw}pt"
    assert right > 700, f"tabel te smal (rechterrand {right}pt)"
    assert verleend_ok, "laatste kolom 'Verleend' valt buiten de pagina"


# NB: portret-regressie is pixel-perfect gedekt door scripts/diff_baseline.py
# (0 diff over 8 pagina's). Een portret-unit-test hier zou juist het bestaande
# squeeze-gedrag vastleggen ("Verleend" valt in portret al buiten beeld), wat
# geen zinvolle garantie is — de fix laat het portret-pad bewust ongemoeid.
