"""Lange cover-titels lopen terug binnen de pagina i.p.v. eraf te lopen.

De cover-H1 werd met ``drawString`` zonder terugloop getekend; een lange
projecttitel liep buiten de rechtermarge. De titel loopt nu terug (naar
beneden, eerste regel op de oorspronkelijke plek zodat de kicker erboven vrij
blijft) en de subtitel zakt mee. Een korte titel blijft één regel → identiek
(pixel-baseline 0 diff, gedekt door scripts/diff_baseline.py).
"""
from __future__ import annotations

from pathlib import Path

import fitz
import pytest

TENANTS = Path(__file__).resolve().parent.parent / "tenants"

pytestmark = pytest.mark.skipif(
    not (TENANTS / "kba" / "brand.yaml").exists(),
    reason="Prive-tenant kba niet aanwezig; staat niet in git",
)


@pytest.fixture(autouse=True)
def _tenants_env(monkeypatch):
    """Tenants-root alleen binnen deze test zetten (lekte eerder naar andere tests)."""
    monkeypatch.setenv("OPENAEC_TENANTS_ROOT", str(TENANTS))
    monkeypatch.setenv("OPENAEC_TENANTS_DIR", str(TENANTS))
LONG = "Herbestemming voormalig schoolgebouw met bijgebouwen"


def _cover(report_type: str, tmp: Path) -> fitz.Page:
    from openaec_reports.core.renderer_v2 import ReportGeneratorV2

    data = {
        "template": "standaard", "report_type": report_type,
        "project": "Leeuwendeel — Deventer", "kicker": "Constructief advies",
        "date": "2026-01-01", "version": "1.0", "status": "C", "client": "X",
        "cover": {"image": str(TENANTS / "kba" / "logos" / "kba.png")},
        "sections": [{"title": "S", "content": [
            {"type": "paragraph", "text": "x"}]}],
    }
    out = tmp / "c.pdf"
    ReportGeneratorV2(brand="kba", tenant_slug="kba").generate(
        data, TENANTS / "kba" / "stationery", out)
    return fitz.open(out)[0]


def test_long_title_wraps_within_page(tmp_path):
    page = _cover(LONG, tmp_path)
    words = [w for w in page.get_text("words")
             if w[4] in LONG.split()]
    assert words, "titelwoorden niet gevonden op de cover"
    right = max(w[2] for w in words)
    lines = len(set(round(w[1]) for w in words))
    assert right < page.rect.width - 40, \
        f"titel loopt buiten de marge (rechterrand {right:.0f}pt)"
    assert lines >= 2, f"lange titel hoort terug te lopen, was {lines} regel(s)"


def test_subtitle_below_wrapped_title(tmp_path):
    """De subtitel moet ONDER de laatste titelregel blijven (geen overlap)."""
    page = _cover(LONG, tmp_path)
    words = page.get_text("words")
    title_bottom = max(w[3] for w in words if w[4] in LONG.split())
    sub = [w for w in words if w[4] in ("Leeuwendeel", "Deventer")]
    assert sub, "subtitel niet gevonden"
    sub_top = min(w[1] for w in sub)
    assert sub_top >= title_bottom - 2, \
        "subtitel overlapt de titel (moet eronder staan)"
