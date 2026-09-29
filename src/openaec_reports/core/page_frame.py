"""Data-gedreven paginakader, achterblad en statische pagina's voor renderer_v2.

Een tenant zonder stationery-PDF kan zijn kop, voetregel en decoratie
declaratief beschrijven als ``static_elements`` (zie ``core/static_elements.py``)
in ``brand.yaml``:

- ``pages.frame.static_elements``: kader op elke pagina behalve de cover (en
  het achterblad), als overlay na het samenvoegen. Dus ook inhoudsopgave,
  colofon en bijlage-scheidingsbladen. Tokens: alles uit
  :func:`static_context` plus ``{page}`` en ``{page_count}``.
- ``pages.frame_landscape.static_elements``: hetzelfde voor liggende pagina's
  (maten in mm op 297 x 210). Ontbreekt het, dan krijgen liggende pagina's
  geen kader (het portretkader zou buiten beeld vallen).
- Bijlage-scheidingsbladen krijgen geen kader (``skip_pages``).
- ``pages.backcover.static_elements``: achterblad als er geen achterblad-PDF is.

Tenants zonder deze sleutels merken niets (geen overlay, oud gedrag).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import fitz
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rl_canvas

logger = logging.getLogger(__name__)


def brand_page_elements(brand_config: Any, page_type: str) -> list[dict] | None:
    """``pages.<page_type>.static_elements`` uit de brand, of ``None``."""
    if brand_config is None:
        return None
    page_cfg = (getattr(brand_config, "pages", None) or {}).get(page_type, {}) or {}
    elements = page_cfg.get("static_elements")
    return elements if elements else None


def static_context(data: dict, brand_config: Any) -> dict[str, str]:
    """Tokens voor static_elements, gelijk aan die van de cover plus header_label."""
    colofon = data.get("colofon", {}) or {}
    contact = getattr(brand_config, "contact", {}) or {}
    label = data.get("header_label") or ""
    if isinstance(label, dict):
        label = label.get("text", "")
    return {
        "report_type": str(data.get("report_type", "") or ""),
        "kicker": str(data.get("kicker", "") or ""),
        "project": str(data.get("project", "") or ""),
        "project_number": str(data.get("project_number", "") or ""),
        "date": str(data.get("date", "") or ""),
        "version": str(data.get("version", "") or ""),
        "status": str(data.get("status", "") or ""),
        "client": str(colofon.get("opdrachtgever_naam", data.get("client", "")) or ""),
        "author": str(colofon.get("adviseur_naam", data.get("author", "")) or ""),
        "company_name": str(contact.get("name", "") or ""),
        "company_website": str(contact.get("website", "") or ""),
        "company_email": str(contact.get("email", "") or ""),
        "company_address": str(contact.get("address", "") or ""),
        "company_phone": str(contact.get("phone", "") or ""),
        "company_kvk": str(contact.get("kvk", "") or ""),
        "company_btw": str(contact.get("btw", "") or ""),
        "header_label": str(label),
    }


def render_static_pages(
    pages: list[tuple[list[dict], dict[str, str]]],
    *,
    brand_config: Any,
    fonts: Any,
    block: str,
    page_size: tuple[float, float] = A4,
) -> fitz.Document:
    """Render per pagina een lijst static_elements (ReportLab) naar een PDF-document."""
    from openaec_reports.core.static_elements import render_static_elements

    fonts.register_reportlab()
    tenant_dir: Path | None = getattr(brand_config, "brand_dir", None)
    tenant = getattr(brand_config, "tenant", "") or getattr(brand_config, "slug", "")
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=page_size)
    for elements, context in pages:
        render_static_elements(
            c, elements,
            page_height_pt=page_size[1],
            tenant_dir=tenant_dir,
            context=context,
            block=block,
            tenant=tenant,
        )
        c.showPage()
    c.save()
    return fitz.open("pdf", buf.getvalue())


def apply_frame(
    pdf_path: Path,
    elements: list[dict],
    context: dict[str, str],
    *,
    skip_first: bool,
    skip_last: bool,
    brand_config: Any,
    fonts: Any,
    elements_landscape: list[dict] | None = None,
    skip_pages: set[int] | None = None,
) -> int:
    """Stempel het paginakader op elke pagina van ``pdf_path`` (in place).

    Returns:
        Aantal gestempelde pagina's.
    """
    doc = fitz.open(str(pdf_path))
    n = len(doc)
    skip = skip_pages or set()
    targets = [
        i for i in range(n)
        if not (skip_first and i == 0) and not (skip_last and i == n - 1) and i not in skip
    ]
    if not targets:
        doc.close()
        return 0
    def pick(i: int) -> list[dict] | None:
        r = doc[i].rect
        return elements_landscape if r.width > r.height else elements

    skipped = [i + 1 for i in targets if pick(i) is None]
    if skipped:
        logger.info("Geen frame_landscape: geen kader op liggende pagina's %s", skipped)
    targets = [i for i in targets if pick(i) is not None]
    specs = [
        (pick(i), {**context, "page": str(i + 1), "page_count": str(n)}) for i in targets
    ]
    overlays = []
    # Pagina's kunnen verschillen in formaat (landscape); per formaat een overlay-set.
    by_size: dict[tuple[float, float], list[int]] = {}
    for idx, i in enumerate(targets):
        r = doc[i].rect
        by_size.setdefault((round(r.width, 1), round(r.height, 1)), []).append(idx)
    for size, idxs in by_size.items():
        ov = render_static_pages(
            [specs[k] for k in idxs], brand_config=brand_config, fonts=fonts,
            block="frame.static_elements", page_size=size,
        )
        overlays.append(ov)
        for pos, k in enumerate(idxs):
            page = doc[targets[k]]
            page.show_pdf_page(page.rect, ov, pos, overlay=True)
    tmp = pdf_path.with_suffix(".frame.tmp.pdf")
    doc.save(str(tmp), garbage=3, deflate=True)
    doc.close()
    for ov in overlays:
        ov.close()
    tmp.replace(pdf_path)
    return len(targets)
