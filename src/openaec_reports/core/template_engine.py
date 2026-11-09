"""Template-driven report engine (v2).

Bouwt PDF rapporten op basis van template YAML configuratie.
Drie pagina-modes:
- special: stationery + text zones (voorblad, achterblad)
- fixed:   stationery + text zones + tabel op vaste positie (BIC controles)
- flow:    stationery + ReportLab flowables (inhoudspagina's)

Gebruikt Optie C: alles via ReportLab DocTemplate.
Special/fixed pagina's zijn PageTemplates met onPage callbacks.
Flow pagina's gebruiken standaard ReportLab flowable paginering.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)

from openaec_reports.core.document import A4, MM_TO_PT
from openaec_reports.core.fonts import get_font_name, register_tenant_fonts
from openaec_reports.core.stationery import StationeryRenderer
from openaec_reports.core.template_config import (
    ContentFrame,
    ImageZone,
    LineZone,
    PageDef,
    PageType,
    TableConfig,
    TemplateConfig,
    TextZone,
)
from openaec_reports.core.template_resolver import TemplateResolver

logger = logging.getLogger(__name__)

# A4 afmetingen in points
A4_W = A4.width_pt
A4_H = A4.height_pt


# ============================================================
# Data binding
# ============================================================


def resolve_bind(data: dict[str, Any], path: str) -> Any:
    """Resolve dot-notatie pad naar waarde in data dict.

    Special prefixes:
    - ``_static.<label>``: returns the label text literally (not from data).
    - ``_page_number``: returns None here; handled by _draw_text_zones.
    """
    if not path:
        return None
    # _static.* → literal label text
    if path.startswith("_static."):
        return path[8:]
    parts = path.split(".")
    current: Any = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def format_value(value: Any, fmt: str | None = None) -> str:
    """Format een waarde voor display."""
    if value is None:
        return ""
    if fmt == "currency_nl":
        try:
            v = float(value)
            formatted = (
                f"{v:,.2f}"
                .replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            )
            return f"\u20ac {formatted}"
        except (ValueError, TypeError):
            return str(value)
    return str(value)


# ============================================================
# Rendering helpers (module-level, geen forward refs)
# ============================================================


def _get_pagesize(orientation: str) -> tuple[float, float]:
    """Geef A4 pagesize in points voor gegeven orientatie."""
    if orientation == "landscape":
        return (A4_H, A4_W)
    return (A4_W, A4_H)


def _resolve_font(font_ref: str, brand) -> str:
    """Resolve font referentie naar ReportLab font naam.

    Lookup order:
    1. Direct match in brand.fonts dict (heading, body, heading_bold, etc.)
    2. Fallback heuristics for heading_bold / body_bold
    3. Treat font_ref as literal ReportLab font name
    """
    if font_ref in brand.fonts:
        font_name = brand.fonts[font_ref]
    elif font_ref == "heading_bold":
        font_name = brand.fonts.get("heading", "LiberationSans-Bold")
    elif font_ref == "body_bold":
        base = brand.fonts.get("body", "LiberationSans")
        font_name = base + "-Bold" if not base.endswith("Bold") else base
    else:
        font_name = font_ref
    try:
        return get_font_name(font_name)
    except Exception:
        return font_name


def _resolve_color(color_ref: str, brand) -> HexColor:
    """Resolve kleur referentie naar HexColor."""
    if color_ref in ("primary", "secondary", "text", "white", "accent"):
        hex_val = brand.colors.get(color_ref, "#000000")
    elif color_ref.startswith("#"):
        hex_val = color_ref
    else:
        hex_val = brand.colors.get(color_ref, "#000000")
    return HexColor(hex_val)


def _wrap_text(text: str, font_name: str, font_size: float, max_width_pt: float) -> list[str]:
    """Breek tekst af in regels die binnen max_width_pt passen."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    words = text.split()
    if not words:
        return [text]

    lines: list[str] = []
    current_line = words[0]

    for word in words[1:]:
        test = current_line + " " + word
        if stringWidth(test, font_name, font_size) <= max_width_pt:
            current_line = test
        else:
            lines.append(current_line)
            current_line = word

    lines.append(current_line)
    return lines


def _apply_flow_layout(
    text_zones: list[TextZone],
    line_zones: list[LineZone],
    image_zones: list[ImageZone],
    data: dict[str, Any],
    brand,
    footer_y_mm: float,
) -> tuple[list[TextZone], list[LineZone], list[ImageZone]]:
    """Verschuif zones naar beneden wanneer text wrapping extra ruimte nodig heeft.

    Algoritme:
    1. Splits content vs footer zones (y >= footer_y_mm = footer, blijft vast).
    2. Groepeer content text_zones per y_mm (±0.5mm tolerantie) → "rijen".
    3. Per rij met max_width_mm zones: bereken wrapped hoogte.
    4. Als wrapped hoogte > natural gap naar volgende rij → accumuleer offset.
    5. Pas offset toe op alle zones (text, line, image) onder die rij.
    """
    from dataclasses import replace


    # Splits content vs footer text zones
    content_tz = [z for z in text_zones if z.y_mm < footer_y_mm]

    if not content_tz:
        return text_zones, line_zones, image_zones

    # Verzamel unieke y-posities (±0.5mm tolerantie)
    y_values: list[float] = sorted({z.y_mm for z in content_tz})
    rows: list[list[float]] = []
    for y in y_values:
        merged = False
        for row in rows:
            if abs(y - row[0]) <= 0.5:
                row.append(y)
                merged = True
                break
        if not merged:
            rows.append([y])

    # Representatieve y per rij (minimum)
    row_ys = sorted(min(r) for r in rows)

    if len(row_ys) < 2:
        return text_zones, line_zones, image_zones

    # Bereken per rij de extra ruimte door wrapping
    accumulated_offset = 0.0
    # Map: originele y → offset toe te passen op zones MET die y
    # We bouwen een lijst van (threshold_y, offset) — zones met y > threshold krijgen offset
    shifts: list[tuple[float, float]] = []  # (rij_y, accumulated_offset na die rij)

    for idx, row_y in enumerate(row_ys):
        # Zones in deze rij (content text zones op ~row_y)
        row_zones = [
            z for z in content_tz if abs(z.y_mm - row_y) <= 0.5
        ]

        # Bereken max wrapped hoogte voor zones met max_width_mm
        max_wrapped_h = 0.0
        for z in row_zones:
            if z.max_width_mm is None:
                continue
            value = resolve_bind(data, z.bind)
            if not value:
                continue
            text = str(value)
            font_name = _resolve_font(z.font, brand)
            max_w_pt = z.max_width_mm * MM_TO_PT
            lines = _wrap_text(text, font_name, z.size, max_w_pt)
            if len(lines) > 1:
                wrapped_h = len(lines) * z.line_height_mm
                max_wrapped_h = max(max_wrapped_h, wrapped_h)

        if max_wrapped_h <= 0:
            shifts.append((row_y, accumulated_offset))
            continue

        # Natural gap: afstand tot volgende rij
        if idx + 1 < len(row_ys):
            natural_gap = row_ys[idx + 1] - row_y
        else:
            natural_gap = footer_y_mm - row_y

        # Extra offset als wrapped hoogte > natural gap
        extra = max_wrapped_h - natural_gap
        if extra > 0:
            accumulated_offset += extra

        shifts.append((row_y, accumulated_offset))

    if accumulated_offset <= 0:
        return text_zones, line_zones, image_zones

    def _get_offset(y_mm: float) -> float:
        """Bepaal de offset voor een zone op basis van y positie.

        Offset geldt alleen voor zones ONDER de rij die overflow veroorzaakte.
        De rij zelf behoudt zijn originele positie.
        """
        if y_mm >= footer_y_mm:
            return 0.0
        result = 0.0
        for row_y, offset in shifts:
            if y_mm > row_y + 0.5:  # strict: alleen zones onder de rij
                result = offset
        return result

    # Pas offsets toe op text zones
    new_text_zones = []
    for z in text_zones:
        offset = _get_offset(z.y_mm)
        if offset > 0:
            new_text_zones.append(replace(z, y_mm=z.y_mm + offset))
        else:
            new_text_zones.append(z)

    # Pas offsets toe op line zones
    new_line_zones = []
    for z in line_zones:
        offset = _get_offset(z.y_mm)
        if offset > 0:
            new_line_zones.append(replace(z, y_mm=z.y_mm + offset))
        else:
            new_line_zones.append(z)

    # Pas offsets toe op image zones
    new_image_zones = []
    for z in image_zones:
        offset = _get_offset(z.y_mm)
        if offset > 0:
            new_image_zones.append(replace(z, y_mm=z.y_mm + offset))
        else:
            new_image_zones.append(z)

    return new_text_zones, new_line_zones, new_image_zones


def _paginate_flow_zones(
    text_zones: list[TextZone],
    line_zones: list[LineZone],
    image_zones: list[ImageZone],
    data: dict[str, Any],
    brand,
    footer_y_mm: float,
    content_start_y_mm: float = 32.0,
) -> list[tuple[list[TextZone], list[LineZone], list[ImageZone]]]:
    """Pas flow layout toe en splits in pagina's als content voorbij footer valt.

    Returns:
        Lijst van (text_zones, line_zones, image_zones) tuples, één per pagina.
        Footer zones worden op elke pagina herhaald.
        Overflow zones worden herpositioneerd vanaf content_start_y_mm.
    """
    from dataclasses import replace

    # 1. Identificeer footer zone indices op basis van ORIGINELE y posities
    footer_tz_idx = {i for i, z in enumerate(text_zones) if z.y_mm >= footer_y_mm}
    footer_lz_idx = {i for i, z in enumerate(line_zones) if z.y_mm >= footer_y_mm}
    footer_iz_idx = {i for i, z in enumerate(image_zones) if z.y_mm >= footer_y_mm}

    # 2. Pas flow layout toe (verschuift content zones, footer zones ongewijzigd)
    shifted_tz, shifted_lz, shifted_iz = _apply_flow_layout(
        text_zones, line_zones, image_zones, data, brand, footer_y_mm,
    )

    # 3. Scheid footer en content zones (index-based, volgorde behouden)
    footer_tz = [z for i, z in enumerate(shifted_tz) if i in footer_tz_idx]
    content_tz = [z for i, z in enumerate(shifted_tz) if i not in footer_tz_idx]

    footer_lz = [z for i, z in enumerate(shifted_lz) if i in footer_lz_idx]
    content_lz = [z for i, z in enumerate(shifted_lz) if i not in footer_lz_idx]

    footer_iz = [z for i, z in enumerate(shifted_iz) if i in footer_iz_idx]
    content_iz = [z for i, z in enumerate(shifted_iz) if i not in footer_iz_idx]

    # 4. Check of er overflow is
    has_overflow = (
        any(z.y_mm >= footer_y_mm for z in content_tz)
        or any(z.y_mm >= footer_y_mm for z in content_lz)
        or any(z.y_mm >= footer_y_mm for z in content_iz)
    )

    if not has_overflow:
        return [(shifted_tz, shifted_lz, shifted_iz)]

    # 5. Splits iteratief in pagina's
    pages: list[tuple[list[TextZone], list[LineZone], list[ImageZone]]] = []
    remaining_tz = content_tz
    remaining_lz = content_lz
    remaining_iz = content_iz
    max_pages = 20  # veiligheid tegen oneindige loop

    for _ in range(max_pages):
        # Wat past op deze pagina?
        fits_tz = [z for z in remaining_tz if z.y_mm < footer_y_mm]
        fits_lz = [z for z in remaining_lz if z.y_mm < footer_y_mm]
        fits_iz = [z for z in remaining_iz if z.y_mm < footer_y_mm]

        overflow_tz = [z for z in remaining_tz if z.y_mm >= footer_y_mm]
        overflow_lz = [z for z in remaining_lz if z.y_mm >= footer_y_mm]
        overflow_iz = [z for z in remaining_iz if z.y_mm >= footer_y_mm]

        # Pagina toevoegen: passende content + footer zones
        pages.append((fits_tz + footer_tz, fits_lz + footer_lz, fits_iz + footer_iz))

        if not overflow_tz and not overflow_lz and not overflow_iz:
            break

        # Herpositioneer overflow voor volgende pagina
        all_ys = (
            [z.y_mm for z in overflow_tz]
            + [z.y_mm for z in overflow_lz]
            + [z.y_mm for z in overflow_iz]
        )
        min_y = min(all_ys)
        reposition = min_y - content_start_y_mm

        remaining_tz = [replace(z, y_mm=z.y_mm - reposition) for z in overflow_tz]
        remaining_lz = [replace(z, y_mm=z.y_mm - reposition) for z in overflow_lz]
        remaining_iz = [replace(z, y_mm=z.y_mm - reposition) for z in overflow_iz]

    return pages


def _draw_text_zones(
    canvas,
    text_zones: list[TextZone],
    data: dict[str, Any],
    brand,
    page_height: float,
    ctx: _BuildContext | None = None,
) -> None:
    """Vul text zones in op het canvas (top-down mm → bottom-up pt).

    YAML y_mm is gemeten als bbox-top (PyMuPDF top-down).
    ReportLab drawString tekent op de baseline.
    Correctie: rl_y = page_height - y_td - ascent.
    """
    from reportlab.pdfbase import pdfmetrics

    for zone in text_zones:
        if zone.bind == "_page_number":
            if ctx:
                abs_page = canvas.getPageNumber()
                content_n = abs_page - ctx.page_number_offset
                total = ctx.content_page_count
                value = f"Pagina {content_n} van {total}"
            else:
                value = str(canvas.getPageNumber())
        else:
            value = resolve_bind(data, zone.bind)
        if not value:
            continue

        text = str(value)
        font_name = _resolve_font(zone.font, brand)
        color = _resolve_color(zone.color, brand)

        x = zone.x_mm * MM_TO_PT
        y_td = zone.y_mm * MM_TO_PT  # top-down: bbox top

        # Corrigeer voor font ascent (bbox top → baseline)
        try:
            ascent = pdfmetrics.getAscent(font_name, zone.size)
        except Exception:
            ascent = zone.size * 0.77  # safe fallback
        rl_y = page_height - y_td - ascent

        canvas.saveState()
        canvas.setFont(font_name, zone.size)
        canvas.setFillColor(color)

        if zone.max_width_mm is not None:
            # Multi-line: wrap tekst binnen max_width
            max_w_pt = zone.max_width_mm * MM_TO_PT
            lines = _wrap_text(text, font_name, zone.size, max_w_pt)
            line_h = zone.line_height_mm * MM_TO_PT
            for i, line in enumerate(lines):
                ly = rl_y - (i * line_h)
                if zone.align == "right":
                    canvas.drawRightString(x, ly, line)
                elif zone.align == "center":
                    canvas.drawCentredString(x, ly, line)
                else:
                    canvas.drawString(x, ly, line)
        else:
            if zone.align == "right":
                canvas.drawRightString(x, rl_y, text)
            elif zone.align == "center":
                canvas.drawCentredString(x, rl_y, text)
            else:
                canvas.drawString(x, rl_y, text)

        canvas.restoreState()


def _draw_image_zones(
    canvas,
    image_zones: list[ImageZone],
    data: dict[str, Any],
    page_height: float,
    assets_dir: Path | None = None,
) -> None:
    """Teken afbeeldingen op vaste posities op het canvas.

    Ondersteunt drie bronnen:
    1. Bestandspad (string) → direct laden
    2. Dict met lat/lon → PDOK kaart ophalen
    3. Fallback afbeelding uit assets/
    """
    for zone in image_zones:
        img_src = resolve_bind(data, zone.bind)

        # Als img_src een dict is met lat/lon → PDOK kaart ophalen
        if isinstance(img_src, dict) and "lat" in img_src and "lon" in img_src:
            img_src = _fetch_pdok_map(img_src, zone)
        elif not img_src and zone.fallback and assets_dir:
            fallback_path = assets_dir / zone.fallback
            if fallback_path.exists():
                img_src = str(fallback_path)

        if not img_src:
            continue

        img_path = Path(str(img_src))
        if not img_path.exists():
            logger.warning("Image zone niet gevonden: %s", img_src)
            continue

        x = zone.x_mm * MM_TO_PT
        w = zone.width_mm * MM_TO_PT
        h = zone.height_mm * MM_TO_PT
        y = page_height - (zone.y_mm * MM_TO_PT) - h  # ReportLab y = bottom-left

        try:
            canvas.drawImage(
                str(img_path), x, y, width=w, height=h,
                preserveAspectRatio=True, anchor="nw", mask="auto",
            )
        except Exception:
            logger.exception("Image zone render fout: %s", img_src)


def _fetch_pdok_map(map_config: dict[str, Any], zone: ImageZone) -> str | None:
    """Haal een PDOK kaart op en sla op als tijdelijk bestand.

    map_config verwacht:
        lat: float — breedtegraad WGS84
        lon: float — lengtegraad WGS84
        radius: float — straal in meters (default 500)
        service: str — luchtfoto, kadaster, bag (default luchtfoto)
        layers: str — laagnamen (optioneel, default per service)

    Als er ook een "image" key is met een bestandspad, wordt dat
    gebruikt als override (combinatie-modus).
    """
    # Override: als er een expliciet pad/base64 is meegegeven, gebruik dat
    override = map_config.get("image", "")
    if override and isinstance(override, str):
        p = Path(override)
        if p.exists():
            return override

    lat = map_config.get("lat")
    lon = map_config.get("lon")
    if lat is None or lon is None:
        return None

    radius = float(map_config.get("radius", 500))
    service = map_config.get("service", "luchtfoto")
    layers = map_config.get("layers")

    default_layers = {
        "luchtfoto": "Actueel_orthoHR",
        "kadaster": "Kadastralekaart",
        "bag": "pand",
    }
    if not layers:
        layers = default_layers.get(service, "Actueel_orthoHR")

    img_format = "image/jpeg" if service == "luchtfoto" else "image/png"
    ext = ".jpg" if service == "luchtfoto" else ".png"

    # Pixel resolutie gebaseerd op zone afmetingen (150 DPI)
    width_px = max(int(zone.width_mm * 150 / 25.4), 800)
    height_px = max(int(zone.height_mm * 150 / 25.4), 600)

    try:
        import requests as req

        from openaec_reports.data.kadaster import KadasterClient

        client = KadasterClient()
        x, y = client.wgs84_to_rd(lat, lon)
        bbox = f"{x - radius},{y - radius},{x + radius},{y + radius}"

        url = client.WMS_SERVICES.get(service, client.WMS_SERVICES["luchtfoto"])
        params = {
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
            "LAYERS": layers, "CRS": "EPSG:28992", "BBOX": bbox,
            "WIDTH": width_px, "HEIGHT": height_px,
            "FORMAT": img_format, "STYLES": "",
        }
        resp = req.get(url, params=params, timeout=30)
        resp.raise_for_status()

        if "xml" in resp.headers.get("content-type", ""):
            logger.warning("PDOK retourneerde XML fout voor %s", service)
            return None

        # Sla op als tijdelijk bestand
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, prefix="pdok_")
        tmp.write(resp.content)
        tmp.close()
        logger.info("PDOK kaart opgehaald: %s (%d bytes)", service, len(resp.content))
        return tmp.name

    except Exception:
        logger.exception("PDOK kaart ophalen mislukt")
        return None


def _draw_line_zones(
    canvas,
    line_zones: list[LineZone],
    brand,
    page_height: float,
) -> None:
    """Teken decoratieve lijnen op het canvas.

    Horizontale lijnen op vaste posities — gebruikt voor sectie-scheiding
    en subtotaal-lijnen in Customer BIC facturen.

    YAML y_mm is top-down. ReportLab y is bottom-up.
    """
    for zone in line_zones:
        x0 = zone.x0_mm * MM_TO_PT
        x1 = zone.x1_mm * MM_TO_PT
        rl_y = page_height - (zone.y_mm * MM_TO_PT)

        color = _resolve_color(zone.color, brand)

        canvas.saveState()
        canvas.setStrokeColor(color)
        canvas.setLineWidth(zone.width_pt)
        canvas.line(x0, rl_y, x1, rl_y)
        canvas.restoreState()


def _truncate_text(
    canvas, text: str, max_width: float,
    font_name: str, size: float, padding: float = 2.0,
) -> str:
    """Truncate tekst met ellipsis als deze breder is dan max_width.

    Args:
        canvas: ReportLab canvas (voor stringWidth berekening).
        text: Originele tekst.
        max_width: Maximale breedte in points.
        font_name: Font naam.
        size: Font grootte.
        padding: Extra marge in points om overlap te voorkomen.

    Returns:
        Originele tekst of afgebroken tekst met "…".
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    usable = max_width - padding
    if usable <= 0:
        return ""
    if stringWidth(text, font_name, size) <= usable:
        return text

    ellipsis = "…"
    ew = stringWidth(ellipsis, font_name, size)
    for i in range(len(text), 0, -1):
        if stringWidth(text[:i], font_name, size) + ew <= usable:
            return text[:i] + ellipsis
    return ellipsis


def _draw_table(
    canvas,
    table_config: TableConfig,
    rows: list[dict[str, Any]],
    brand,
    page_height: float,
) -> None:
    """Teken een transparante tabel met vaste kolommen op canvas."""
    x_origin = table_config.origin_x_mm * MM_TO_PT
    row_h = table_config.row_height_mm * MM_TO_PT
    total_w = sum(c.width_mm for c in table_config.columns) * MM_TO_PT

    y_td = table_config.origin_y_mm * MM_TO_PT  # top-down in pt

    # Optionele kolomkoppen
    if table_config.show_header:
        rl_y = page_height - y_td

        # Header achtergrondkleur
        if table_config.header_bg:
            canvas.saveState()
            canvas.setFillColor(_resolve_color(table_config.header_bg, brand))
            canvas.rect(x_origin, rl_y - row_h * 0.3, total_w, row_h, fill=1, stroke=0)
            canvas.restoreState()

        canvas.saveState()
        header_font = _resolve_font(table_config.header_font, brand)
        header_color = _resolve_color(table_config.header_color, brand)
        canvas.setFont(header_font, table_config.header_size)
        canvas.setFillColor(header_color)

        x = x_origin
        for col in table_config.columns:
            col_w = col.width_mm * MM_TO_PT
            header_text = col.header or col.field

            if col.align == "right":
                canvas.drawRightString(x + col_w, rl_y, header_text)
            elif col.align == "center":
                canvas.drawCentredString(x + col_w / 2, rl_y, header_text)
            else:
                canvas.drawString(x, rl_y, header_text)
            x += col_w

        canvas.restoreState()
        y_td += row_h

    # Data rijen
    for row_idx, row in enumerate(rows):
        rl_y = page_height - y_td
        x = x_origin

        # Alternerende rijkleur
        if table_config.alt_row_bg and row_idx % 2 == 1:
            canvas.saveState()
            canvas.setFillColor(_resolve_color(table_config.alt_row_bg, brand))
            canvas.rect(x_origin, rl_y - row_h * 0.3, total_w, row_h, fill=1, stroke=0)
            canvas.restoreState()

        for col in table_config.columns:
            col_w = col.width_mm * MM_TO_PT
            raw_value = row.get(col.field, "")
            text = format_value(raw_value, col.format)

            if text:
                canvas.saveState()
                # Body overrides op TableConfig niveau
                font_ref = table_config.body_font or col.font
                color_ref = table_config.body_color or col.color
                size = table_config.body_size or col.size

                font_name = _resolve_font(font_ref, brand)
                color = _resolve_color(color_ref, brand)
                canvas.setFont(font_name, size)
                canvas.setFillColor(color)

                # Truncate tekst als deze breder is dan de kolom
                text = _truncate_text(canvas, text, col_w, font_name, size)

                if col.align == "right":
                    canvas.drawRightString(x + col_w, rl_y, text)
                elif col.align == "center":
                    canvas.drawCentredString(x + col_w / 2, rl_y, text)
                else:
                    canvas.drawString(x, rl_y, text)

                canvas.restoreState()
            x += col_w

        # Grid lijnen
        if table_config.grid_color:
            canvas.saveState()
            canvas.setStrokeColor(_resolve_color(table_config.grid_color, brand))
            canvas.setLineWidth(0.25)
            canvas.line(x_origin, rl_y - row_h * 0.3, x_origin + total_w, rl_y - row_h * 0.3)
            canvas.restoreState()

        y_td += row_h


def _paginate_table_data(
    rows: list[dict[str, Any]],
    table_config: TableConfig,
) -> list[list[dict[str, Any]]]:
    """Verdeel tabelrijen over pagina's op basis van max_y."""
    if not rows:
        return []

    available_height_mm = table_config.max_y_mm - table_config.origin_y_mm
    rows_per_page = max(1, int(available_height_mm / table_config.row_height_mm))

    chunks = []
    for i in range(0, len(rows), rows_per_page):
        chunks.append(rows[i:i + rows_per_page])
    return chunks


# ============================================================
# Build context (vóór engine class)
# ============================================================


class _BuildContext:
    """Interne context voor de build operatie."""

    __slots__ = (
        "template", "page_types", "data", "brand",
        "stationery", "stationery_dir",
        "content_page_count", "page_number_offset",
    )

    def __init__(
        self,
        template: TemplateConfig,
        page_types: dict[str, PageType],
        data: dict[str, Any],
        brand,
        stationery: StationeryRenderer,
        stationery_dir: Path,
    ):
        self.template = template
        self.page_types = page_types
        self.data = data
        self.brand = brand
        self.stationery = stationery
        self.stationery_dir = stationery_dir
        self.content_page_count: int = 0
        self.page_number_offset: int = 0


# ============================================================
# Template Engine
# ============================================================


class TemplateEngine:
    """Template-driven report generator.

    Usage::

        engine = TemplateEngine(tenants_dir=Path("tenants"))
        engine.build(
            template_name="bic_factuur",
            tenant="customer",
            data=json_data,
            output_path="output/factuur.pdf",
        )
    """

    def __init__(self, tenants_dir: Path | None = None):
        self._resolver = TemplateResolver(tenants_dir=tenants_dir)
        self._tenants_dir = tenants_dir or self._resolver._tenants_dir

    def build(
        self,
        template_name: str,
        tenant: str,
        data: dict[str, Any],
        output_path: str | Path,
        brand=None,
    ) -> Path:
        """Genereer een PDF rapport op basis van template + data.

        Args:
            template_name: Naam van de template YAML.
            tenant: Tenant identifier.
            data: JSON data dict.
            output_path: Pad voor output PDF.
            brand: Optioneel BrandConfig of brand naam (str).

        Returns:
            Path naar gegenereerd PDF bestand.
        """
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        # Laad configuratie
        template = self._resolver.load_template(template_name, tenant)
        page_types = self._resolver.load_all_page_types(template)

        # Brand laden
        from openaec_reports.core.brand import BrandConfig, BrandLoader

        if isinstance(brand, BrandConfig):
            brand_config = brand
        elif isinstance(brand, str):
            brand_config = BrandLoader(tenants_root=self._tenants_dir).load(brand)
        else:
            brand_config = self._load_tenant_brand(tenant)

        # Registreer tenant-specifieke fonts (vóór eerste pagina)
        font_files = getattr(brand_config, "font_files", None)
        if font_files and isinstance(font_files, dict):
            fonts_dir = self._tenants_dir / tenant
            register_tenant_fonts(font_files, fonts_dir)

        # Stationery renderer met tenant stationery dir
        stationery_dir = self._tenants_dir / tenant / "stationery"
        stationery = StationeryRenderer(brand_dir=stationery_dir)

        ctx = _BuildContext(
            template=template,
            page_types=page_types,
            data=data,
            brand=brand_config,
            stationery=stationery,
            stationery_dir=stationery_dir,
        )

        # Bereken content paginanummering (excl cover)
        cover_pages = 0
        content_pages = 0
        for page_def in template.pages:
            pt = page_types[page_def.page_type]
            if page_def.type == "special" and cover_pages == 0:
                # Eerste special page = cover (telt niet mee)
                cover_pages = 1
            elif pt.flow_layout and page_def.type in ("special", "fixed"):
                # Flow layout kan overflow pagina's genereren
                zone_pages = _paginate_flow_zones(
                    pt.text_zones, pt.line_zones, pt.image_zones,
                    data, brand_config, pt.flow_footer_y_mm,
                    pt.flow_content_start_y_mm,
                )
                content_pages += len(zone_pages)
            elif page_def.type == "fixed" and page_def.repeat == "auto" and pt.table:
                table_data = resolve_bind(data, pt.table.data_bind) or []
                chunks = _paginate_table_data(table_data, pt.table)
                content_pages += max(1, len(chunks))
            else:
                content_pages += 1
        ctx.content_page_count = content_pages
        ctx.page_number_offset = cover_pages

        self._build_pdf(ctx, output)

        logger.info("PDF gegenereerd: %s", output)
        return output

    def _load_tenant_brand(self, tenant: str):
        """Laad brand.yaml uit tenant directory."""
        from openaec_reports.core.brand import BrandLoader
        brand_path = self._tenants_dir / tenant / "brand.yaml"
        if brand_path.exists():
            loader = BrandLoader(tenants_root=self._tenants_dir)
            return loader.load(tenant)
        return BrandLoader(tenants_root=self._tenants_dir).load_default()

    def _build_pdf(self, ctx: _BuildContext, output: Path) -> None:
        """Bouw PDF via ReportLab DocTemplate (Optie C).

        Elke pagina-definitie wordt een PageTemplate.
        Special/fixed: onPage callback tekent alles, Spacer als trigger.
        Flow: flowables in content frame.
        """
        self._pending_chunk_templates: list[PageTemplate] = []

        first_page = ctx.template.pages[0] if ctx.template.pages else None
        initial_pagesize = _get_pagesize(
            first_page.orientation if first_page else "portrait",
        )

        doc = BaseDocTemplate(
            filename=str(output),
            pagesize=initial_pagesize,
            topMargin=0,
            bottomMargin=0,
            leftMargin=0,
            rightMargin=0,
        )

        # Registreer basis PageTemplates + bouw elements
        base_templates = []
        template_info = []

        for i, page_def in enumerate(ctx.template.pages):
            page_type = ctx.page_types[page_def.page_type]
            template_id = f"page_{i}_{page_def.page_type}"
            pagesize = _get_pagesize(page_def.orientation)

            if page_def.type == "fixed" and page_def.repeat == "auto":
                # Chunk templates worden in _build_elements aangemaakt
                template_info.append((template_id, None, page_def, page_type))
            elif page_def.type in ("special", "fixed") and page_type.flow_layout:
                # Flow layout paginering: uitgesteld naar _build_elements
                template_info.append((template_id, None, page_def, page_type))
            elif page_def.type in ("special", "fixed"):
                pt = self._make_special_fixed_pt(
                    template_id, page_def, page_type, pagesize, ctx,
                )
                base_templates.append(pt)
                template_info.append((template_id, pt, page_def, page_type))
            elif page_def.type in ("flow", "toc"):
                pt = self._make_flow_pt(
                    template_id, page_def, page_type, pagesize, ctx,
                )
                base_templates.append(pt)
                template_info.append((template_id, pt, page_def, page_type))

        # Bouw elements (chunk templates worden bijgemaakt)
        elements = self._build_elements(template_info, ctx)

        # Alle templates combineren
        all_templates = base_templates + self._pending_chunk_templates
        if not all_templates:
            logger.warning("Geen page templates — leeg document")
            return

        doc.addPageTemplates(all_templates)
        doc.build(elements)

    # ----------------------------------------------------------
    # PageTemplate factory methods
    # ----------------------------------------------------------

    def _make_special_fixed_pt(
        self,
        template_id: str,
        page_def: PageDef,
        page_type: PageType,
        pagesize: tuple[float, float],
        ctx: _BuildContext,
    ) -> PageTemplate:
        """PageTemplate voor special/fixed (geen repeat)."""
        pw, ph = pagesize
        frame = Frame(0, 0, pw, ph, id=f"f_{template_id}",
                       leftPadding=0, rightPadding=0,
                       topPadding=0, bottomPadding=0)

        def on_page(canvas, doc, _pd=page_def, _pt=page_type, _pw=pw, _ph=ph, _ctx=ctx):
            canvas.setPageSize((_pw, _ph))
            if _pt.stationery:
                _ctx.stationery.draw(canvas, _pt.stationery, _pw, _ph)

            # Flow layout: verschuif zones bij text wrapping overflow
            if _pt.flow_layout:
                tz, lz, iz = _apply_flow_layout(
                    _pt.text_zones, _pt.line_zones, _pt.image_zones,
                    _ctx.data, _ctx.brand, _pt.flow_footer_y_mm,
                )
            else:
                tz, lz, iz = _pt.text_zones, _pt.line_zones, _pt.image_zones

            if lz:
                _draw_line_zones(canvas, lz, _ctx.brand, _ph)
            _draw_text_zones(canvas, tz, _ctx.data, _ctx.brand, _ph, _ctx)
            if iz:
                assets_dir = _ctx.stationery_dir.parent / "assets"
                _draw_image_zones(canvas, iz, _ctx.data, _ph, assets_dir)
            # Fixed zonder repeat: tabel direct tekenen
            if _pd.type == "fixed" and _pt.table:
                rows = resolve_bind(_ctx.data, _pt.table.data_bind)
                if rows:
                    _draw_table(canvas, _pt.table, rows, _ctx.brand, _ph)

        return PageTemplate(
            id=template_id, frames=[frame], pagesize=pagesize, onPage=on_page,
        )

    def _make_flow_pt(
        self,
        template_id: str,
        page_def: PageDef,
        page_type: PageType,
        pagesize: tuple[float, float],
        ctx: _BuildContext,
    ) -> PageTemplate:
        """PageTemplate voor flow pagina's."""
        pw, ph = pagesize
        cf = page_type.content_frame or ContentFrame()

        frame = Frame(
            cf.x_mm * MM_TO_PT,
            ph - (cf.y_mm + cf.height_mm) * MM_TO_PT,
            cf.width_mm * MM_TO_PT,
            cf.height_mm * MM_TO_PT,
            id=f"f_{template_id}",
        )

        def on_page(canvas, doc, _pt=page_type, _pw=pw, _ph=ph, _ctx=ctx):
            canvas.setPageSize((_pw, _ph))
            if _pt.stationery:
                _ctx.stationery.draw(canvas, _pt.stationery, _pw, _ph)

        return PageTemplate(
            id=template_id, frames=[frame], pagesize=pagesize, onPage=on_page,
        )

    def _make_fixed_chunk_pt(
        self,
        template_id: str,
        page_def: PageDef,
        page_type: PageType,
        pagesize: tuple[float, float],
        ctx: _BuildContext,
        chunk: list[dict],
    ) -> PageTemplate:
        """PageTemplate voor een chunk van tabeldata (fixed + repeat:auto)."""
        pw, ph = pagesize
        frame = Frame(0, 0, pw, ph, id=f"f_{template_id}",
                       leftPadding=0, rightPadding=0,
                       topPadding=0, bottomPadding=0)

        def on_page(canvas, doc, _pt=page_type, _pw=pw, _ph=ph,
                     _chunk=chunk, _ctx=ctx):
            canvas.setPageSize((_pw, _ph))
            if _pt.stationery:
                _ctx.stationery.draw(canvas, _pt.stationery, _pw, _ph)

            # Flow layout: verschuif zones bij text wrapping overflow
            if _pt.flow_layout:
                tz, lz, iz = _apply_flow_layout(
                    _pt.text_zones, _pt.line_zones, _pt.image_zones,
                    _ctx.data, _ctx.brand, _pt.flow_footer_y_mm,
                )
            else:
                tz, lz, iz = _pt.text_zones, _pt.line_zones, _pt.image_zones

            if lz:
                _draw_line_zones(canvas, lz, _ctx.brand, _ph)
            _draw_text_zones(canvas, tz, _ctx.data, _ctx.brand, _ph, _ctx)
            if iz:
                assets_dir = _ctx.stationery_dir.parent / "assets"
                _draw_image_zones(canvas, iz, _ctx.data, _ph, assets_dir)
            if _pt.table and _chunk:
                _draw_table(canvas, _pt.table, _chunk, _ctx.brand, _ph)

        return PageTemplate(
            id=template_id, frames=[frame], pagesize=pagesize, onPage=on_page,
        )

    def _make_zone_page_pt(
        self,
        template_id: str,
        page_def: PageDef,
        page_type: PageType,
        pagesize: tuple[float, float],
        ctx: _BuildContext,
        text_zones: list[TextZone],
        line_zones: list[LineZone],
        image_zones: list[ImageZone],
    ) -> PageTemplate:
        """PageTemplate met vooraf berekende zones (flow layout overflow pagina's)."""
        pw, ph = pagesize
        frame = Frame(0, 0, pw, ph, id=f"f_{template_id}",
                       leftPadding=0, rightPadding=0,
                       topPadding=0, bottomPadding=0)

        def on_page(canvas, doc, _pt=page_type, _pw=pw, _ph=ph, _ctx=ctx,
                     _tz=text_zones, _lz=line_zones, _iz=image_zones):
            canvas.setPageSize((_pw, _ph))
            if _pt.stationery:
                _ctx.stationery.draw(canvas, _pt.stationery, _pw, _ph)
            if _lz:
                _draw_line_zones(canvas, _lz, _ctx.brand, _ph)
            _draw_text_zones(canvas, _tz, _ctx.data, _ctx.brand, _ph, _ctx)
            if _iz:
                assets_dir = _ctx.stationery_dir.parent / "assets"
                _draw_image_zones(canvas, _iz, _ctx.data, _ph, assets_dir)
            # Fixed tabel: niet op overflow pagina's (tabel staat op originele pagina)
            if page_def.type == "fixed" and _pt.table:
                rows = resolve_bind(_ctx.data, _pt.table.data_bind)
                if rows:
                    _draw_table(canvas, _pt.table, rows, _ctx.brand, _ph)

        return PageTemplate(
            id=template_id, frames=[frame], pagesize=pagesize, onPage=on_page,
        )

    # ----------------------------------------------------------
    # Elements builder
    # ----------------------------------------------------------

    def _build_elements(
        self,
        template_info: list[tuple[str, PageTemplate | None, PageDef, PageType]],
        ctx: _BuildContext,
    ) -> list:
        """Bouw de volledige elements list voor ReportLab."""
        elements: list = []
        first = True

        for template_id, pt, page_def, page_type in template_info:

            if page_def.type in ("special", "fixed") and page_type.flow_layout:
                # Flow layout met auto page-break bij overflow
                zone_pages = _paginate_flow_zones(
                    page_type.text_zones, page_type.line_zones,
                    page_type.image_zones, ctx.data, ctx.brand,
                    page_type.flow_footer_y_mm, page_type.flow_content_start_y_mm,
                )
                pagesize = _get_pagesize(page_def.orientation)
                for pi, (tz, lz, iz) in enumerate(zone_pages):
                    chunk_id = f"{template_id}_fp{pi}"
                    chunk_pt = self._make_zone_page_pt(
                        chunk_id, page_def, page_type, pagesize, ctx,
                        tz, lz, iz,
                    )
                    self._pending_chunk_templates.append(chunk_pt)
                    elements.append(NextPageTemplate(chunk_id))
                    if not first:
                        elements.append(PageBreak())
                    elements.append(Spacer(1, 1))
                    first = False

            elif page_def.type == "special":
                elements.append(NextPageTemplate(template_id))
                if not first:
                    elements.append(PageBreak())
                elements.append(Spacer(1, 1))
                first = False

            elif page_def.type == "fixed":
                if page_def.repeat == "auto" and page_type.table:
                    table_data = resolve_bind(ctx.data, page_type.table.data_bind) or []
                    chunks = _paginate_table_data(table_data, page_type.table)
                    if not chunks:
                        chunks = [[]]

                    pagesize = _get_pagesize(page_def.orientation)

                    for ci, chunk in enumerate(chunks):
                        chunk_id = f"{template_id}_c{ci}"
                        chunk_pt = self._make_fixed_chunk_pt(
                            chunk_id, page_def, page_type, pagesize, ctx, chunk,
                        )
                        self._pending_chunk_templates.append(chunk_pt)

                        elements.append(NextPageTemplate(chunk_id))
                        if not first:
                            elements.append(PageBreak())
                        elements.append(Spacer(1, 1))
                        first = False
                else:
                    elements.append(NextPageTemplate(template_id))
                    if not first:
                        elements.append(PageBreak())
                    elements.append(Spacer(1, 1))
                    first = False

            elif page_def.type == "flow":
                elements.append(NextPageTemplate(template_id))
                if not first:
                    elements.append(PageBreak())
                first = False

                flow = self._build_flow_content(ctx)
                if flow:
                    elements.extend(flow)
                else:
                    elements.append(Spacer(1, 1))

            elif page_def.type == "toc":
                elements.append(NextPageTemplate(template_id))
                if not first:
                    elements.append(PageBreak())
                first = False
                elements.append(Spacer(1, 20))  # placeholder

        return elements

    def _build_flow_content(self, ctx: _BuildContext) -> list:
        """Bouw flowable content uit data["sections"]."""
        elements: list = []

        sections = ctx.data.get("sections", [])
        if not sections:
            return elements

        try:
            from openaec_reports.core.styles import create_stylesheet
            styles = create_stylesheet(brand=ctx.brand)
        except Exception:
            from reportlab.lib.styles import getSampleStyleSheet
            styles = getSampleStyleSheet()

        for section_data in sections:
            title = section_data.get("title", "")
            level = section_data.get("level", 1)

            if title:
                style_name = f"Heading{level}"
                style = styles.get(style_name, styles.get("Heading1", styles["Normal"]))
                elements.append(Paragraph(title, style))

            for block_data in section_data.get("content", []):
                text = block_data.get("text", "")
                if text:
                    elements.append(Paragraph(text, styles.get("Normal", styles["Normal"])))

            elements.append(Spacer(1, 6))

        return elements
