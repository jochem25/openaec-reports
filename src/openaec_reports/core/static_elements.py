"""Static elements — data-gedreven statische pagina-geometrie.

Achtergrond: vóór dit module tekenden ``CoverGenerator``/``ColofonGenerator``
(``core/renderer_v2.py``) een groot deel van hun lay-out via losse
ReportLab-aanroepen die impliciet 3BM's ontwerp aannamen (teal placeholder-
vlak, foto-clip als rechthoek, tekstvelden op vaste posities). Een tenant
met een wezenlijk ander coverontwerp — bijv. KBA's petrol fotoband met
duotone-foto, kleurverloop-streep en 3-koloms metagrid — kon dat ontwerp
niet uitdrukken zonder Python-code te wijzigen.

Dit module rendert een LIJST van simpele, declaratieve elementen (dicts met
een ``type``-sleutel) op een ReportLab-canvas. De lijst leeft in
tenant-config (``brand.yaml``/``brand.base.yaml`` → ``pages.<paginatype>.
static_elements``) en wordt door ``BrandLoader.load()`` al door
``core/refs.py``'s ``resolve_refs`` gehaald, dus ``$colors.<naam>``/
``$fonts.<naam>``-referenties zijn op het moment dat ze hier binnenkomen
al vervangen door hun letterlijke waarde.

Coördinatenconventie — BELANGRIJK
----------------------------------
Alle ``x``/``y``/``w``/``h``/``radius``/``x1``/``y1``/``x2``/``y2``-velden
zijn in **MILLIMETER**, met de oorsprong **linksboven** (top-down: ``y``
groeit naar ONDEREN). Dit is bewust dezelfde conventie als de canonieke
KBA-referentietemplates (``X:\\10_3BM_bouwkunde\\10_huisstijl\\KBA\\
templates\\coverblad.html``, bijv. ``.a .logo { top: 20mm; left: 22mm }``)
— zodat maten 1-op-1 uit die bron overgenomen kunnen worden zonder mentale
omrekening.

ReportLab's canvas werkt in **punten** (1mm = 2.8346pt) met de oorsprong
**linksonder** (y-as omhoog). Elke coördinaat wordt hier intern
geconverteerd via :func:`_td_mm_to_bl_pt`. Een verwisselde conventie is de
klassieke bron van "alles staat op zijn kop" — vandaar deze expliciete
paragraaf, hier én in ``tenants/kba/brand.base.yaml`` bovenaan
``pages.cover.static_elements``.

Voor tekst-elementen benadert dit module de baseline als
``y_td_pt + size_pt * 0.8`` (top van het lettertype tot baseline) — dezelfde
vuistregel die ``renderer_v2.ContentRenderer._text`` al gebruikt.

Ondersteunde ``type``-waarden: ``rect``, ``rounded_rect``, ``line``,
``polygon``, ``image``, ``text``. Een onbekend ``type`` is een
``ValueError`` — geen stille no-op, in lijn met de rest van deze refactor
(zie ``core/refs.py``/``renderer_v2._style_color``: geen stille fallback).

``text``-elementen kennen een optioneel ``transform``-veld (``upper``,
``lower`` of ``title``), toegepast NA de ``{token}``-substitutie (bijv.
``{kicker}`` → ``"Constructief advies"`` → ``"CONSTRUCTIEF ADVIES"`` bij
``transform: upper``) — zo blijft de brontekst in report-data leesbare
spreektaal en is de opmaakregel (CSS' ``text-transform: uppercase``)
puur een presentatie-keuze in de tenant-config. Ontbreekt het veld, dan
blijft de tekst ongewijzigd. Een onbekende waarde is een ``ValueError``,
zelfde fail-loud-patroon als een onbekend ``type``.

``image``-elementen kennen een optioneel ``required``-veld (default
``False``). Ontbreekt de bron van een ``required: true``-image (geen
data, of een pad dat niet bestaat), dan is dat een ``ValueError`` — niet
de stille skip-met-warning die voor optionele images geldt (bv. logo's,
watermerken). Zie de KBA-README, sectie "Coverblad": een projectfoto is
daar expliciet verplicht.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader

if TYPE_CHECKING:
    from reportlab.pdfgen.canvas import Canvas

logger = logging.getLogger(__name__)

MM_TO_PT = 2.8346456693

_KNOWN_TYPES = {"rect", "rounded_rect", "line", "polygon", "image", "text", "row"}
_KNOWN_TRANSFORMS = {"upper", "lower", "title"}

_TRANSFORM_FUNCS = {
    "upper": str.upper,
    "lower": str.lower,
    "title": str.title,
}


class _SafeFormatDict(dict):
    """``dict`` voor ``str.format_map`` die ontbrekende sleutels stil
    negeert (levert ``""`` i.p.v. een ``KeyError``).

    Bewuste keuze: een ontbrekend datapunt (bijv. geen ``project_number``
    op een concept-rapport) mag een tekstregel laten inkorten, maar mag
    het hele rapport niet laten crashen — in tegenstelling tot een
    ontbrekende KLEUR, waar deze refactor juist bewust wél hard faalt (zie
    module-docstring van ``renderer_v2._style_color``). Lay-out-tokens en
    merk-kleuren zijn een andere risicoklasse.
    """

    def __missing__(self, key: str) -> str:
        logger.debug("static_elements: onbekende data-token '{%s}'", key)
        return ""


def _mm(value: Any, default: float = 0.0) -> float:
    return float(value) if value is not None else default


def _td_mm_to_bl_pt(page_height_pt: float, y_mm: float, h_mm: float = 0.0) -> float:
    """Converteer een top-down y (mm) naar ReportLab bottom-left punten.

    ``h_mm`` is de hoogte van het element (0 voor een enkel punt, zoals een
    lijnpunt of tekst-baseline-anker) — het element wordt zo geplaatst dat
    zijn BOVENrand op ``y_mm`` valt.
    """
    return page_height_pt - (y_mm * MM_TO_PT) - (h_mm * MM_TO_PT)


def _resolve_color(spec: dict, key: str, *, block: str) -> str:
    val = spec.get(key)
    if not val:
        raise ValueError(
            f"static_elements: element van type '{spec.get('type')}' "
            f"({block}) mist verplicht kleurveld '{key}'."
        )
    return val


def _resolve_image_src(
    src: str, tenant_dir: Path | None, context: dict
) -> tuple[Path | None, str]:
    """Resolve een image-element se ``src``.

    Ondersteunt twee vormen:
    - Een ``{token}``-verwijzing naar ``context`` (bijv. ``"{cover_photo}"``)
      voor data-gebonden afbeeldingen zoals de projectfoto — de context-
      waarde is dan al een absoluut ``Path``-achtig object.
    - Een tenant-relatief pad (bijv. ``"logos/kba-wit.png"``), opgelost
      t.o.v. ``tenant_dir``.

    Returns:
        Tuple van (opgelost pad of ``None`` bij falen, een leesbare
        "gezocht pad"-string voor foutmeldingen — ook gevuld als resolutie
        faalt, zodat een ``required: true``-image een bruikbare foutmelding
        kan geven in plaats van alleen "niet gevonden").
    """
    if src.startswith("{") and src.endswith("}"):
        token = src[1:-1]
        val = context.get(token)
        if not val:
            return None, f"context-token '{{{token}}}' (geen waarde ingevuld)"
        path = Path(val)
        return (path if path.exists() else None), str(path)
    if tenant_dir is not None:
        candidate = tenant_dir / src
        if candidate.exists():
            return candidate, str(candidate)
        attempted = str(candidate)
    else:
        attempted = src
    candidate = Path(src)
    if candidate.exists():
        return candidate, str(candidate)
    return None, attempted


def render_static_elements(
    c: Canvas,
    elements: list[dict],
    *,
    page_height_pt: float,
    tenant_dir: Path | None = None,
    context: dict | None = None,
    block: str = "static_elements",
    tenant: str = "",
) -> None:
    """Render een lijst ``static_elements`` op een ReportLab canvas.

    Args:
        c: Actieve ReportLab ``Canvas``. Fonts moeten al geregistreerd
            zijn (``FontManager.register_reportlab()``) door de
            aanroeper — een onbekende fontnaam faalt luid via
            ReportLab's eigen ``KeyError`` (hier opgevangen met een
            fallback naar ``LiberationSans``, zie ``text``-tak hieronder).
        elements: Lijst van element-dicts, zie module-docstring voor de
            ondersteunde ``type``-waarden en hun velden.
        page_height_pt: Paginahoogte in punten (voor de mm→pt/top-down→
            bottom-left conversie).
        tenant_dir: Tenant-directory voor relatieve ``image``-paden.
        context: Data-bindingen voor ``{token}``-substitutie in
            ``text.content`` en ``image.src`` (bijv. ``{"project_naam":
            ..., "cover_photo": Path(...)}``).
        block: Label voor foutmeldingen (bijv. ``"cover.static_elements"``).
        tenant: Tenant-slug, uitsluitend voor foutmeldingen (bijv. de
            ``ValueError`` van een ontbrekende ``required: true``-image
            noemt hiermee welke tenant getroffen is).

    Raises:
        ValueError: Bij een onbekend ``type``, een ontbrekend verplicht
            kleurveld, of een ontbrekende ``required: true``-afbeelding.
    """
    context = context or {}
    safe_ctx = _SafeFormatDict(context)

    for i, el in enumerate(elements):
        el_type = el.get("type", "")
        el_label = f"{block}[{i}]({el_type or '?'})"

        if el_type not in _KNOWN_TYPES:
            raise ValueError(
                f"static_elements: onbekend element-type '{el_type}' in "
                f"{el_label}. Bekende types: {sorted(_KNOWN_TYPES)}."
            )

        if el_type == "rect":
            x, y = _mm(el.get("x")), _mm(el.get("y"))
            w, h = _mm(el.get("w")), _mm(el.get("h"))
            fill = _resolve_color(el, "fill", block=el_label)
            c.saveState()
            c.setFillColor(HexColor(fill))
            c.rect(
                x * MM_TO_PT,
                _td_mm_to_bl_pt(page_height_pt, y, h),
                w * MM_TO_PT,
                h * MM_TO_PT,
                fill=1,
                stroke=0,
            )
            c.restoreState()

        elif el_type == "rounded_rect":
            x, y = _mm(el.get("x")), _mm(el.get("y"))
            w, h = _mm(el.get("w")), _mm(el.get("h"))
            radius = _mm(el.get("radius"))
            fill = _resolve_color(el, "fill", block=el_label)
            c.saveState()
            c.setFillColor(HexColor(fill))
            c.roundRect(
                x * MM_TO_PT,
                _td_mm_to_bl_pt(page_height_pt, y, h),
                w * MM_TO_PT,
                h * MM_TO_PT,
                radius * MM_TO_PT,
                fill=1,
                stroke=0,
            )
            c.restoreState()

        elif el_type == "line":
            x1, y1 = _mm(el.get("x1")), _mm(el.get("y1"))
            x2, y2 = _mm(el.get("x2")), _mm(el.get("y2"))
            width = _mm(el.get("width"), 0.35)
            color = _resolve_color(el, "color", block=el_label)
            c.saveState()
            c.setStrokeColor(HexColor(color))
            c.setLineWidth(width * MM_TO_PT)
            c.line(
                x1 * MM_TO_PT,
                _td_mm_to_bl_pt(page_height_pt, y1),
                x2 * MM_TO_PT,
                _td_mm_to_bl_pt(page_height_pt, y2),
            )
            c.restoreState()

        elif el_type == "polygon":
            points = el.get("points") or []
            if len(points) < 2:
                raise ValueError(
                    f"static_elements: polygon in {el_label} heeft minder "
                    "dan 2 punten."
                )
            fill = _resolve_color(el, "fill", block=el_label)
            c.saveState()
            c.setFillColor(HexColor(fill))
            path = c.beginPath()
            first_x, first_y = points[0]
            path.moveTo(
                first_x * MM_TO_PT, _td_mm_to_bl_pt(page_height_pt, first_y)
            )
            for px, py in points[1:]:
                path.lineTo(px * MM_TO_PT, _td_mm_to_bl_pt(page_height_pt, py))
            path.close()
            c.drawPath(path, fill=1, stroke=0)
            c.restoreState()

        elif el_type == "image":
            x, y = _mm(el.get("x")), _mm(el.get("y"))
            w, h = _mm(el.get("w")), _mm(el.get("h"))
            src = el.get("src", "")
            required = bool(el.get("required", False))
            img_path, attempted_path = _resolve_image_src(src, tenant_dir, context)
            if img_path is None:
                if required:
                    raise ValueError(
                        f"static_elements: verplichte afbeelding ontbreekt — "
                        f"tenant '{tenant or '?'}', {el_label}, "
                        f"gezocht pad: {attempted_path}. Een 'required: true'"
                        "-image die niet gevonden wordt mag NIET stilzwijgend "
                        "worden overgeslagen (zie KBA-README, sectie "
                        "'Coverblad')."
                    )
                logger.warning(
                    "static_elements: image '%s' niet gevonden in %s — "
                    "overgeslagen.", src, el_label,
                )
                continue
            fit = el.get("fit", "stretch")
            box_x_pt = x * MM_TO_PT
            box_y_pt = _td_mm_to_bl_pt(page_height_pt, y, h)
            box_w_pt = w * MM_TO_PT
            box_h_pt = h * MM_TO_PT
            if fit == "cover":
                img = ImageReader(str(img_path))
                iw, ih = img.getSize()
                scale = max(box_w_pt / iw, box_h_pt / ih)
                draw_w, draw_h = iw * scale, ih * scale
                draw_x = box_x_pt - (draw_w - box_w_pt) / 2
                draw_y = box_y_pt - (draw_h - box_h_pt) / 2
                c.saveState()
                clip = c.beginPath()
                clip.rect(box_x_pt, box_y_pt, box_w_pt, box_h_pt)
                c.clipPath(clip, stroke=0)
                c.drawImage(img, draw_x, draw_y, width=draw_w, height=draw_h)
                c.restoreState()
            else:
                c.drawImage(
                    str(img_path), box_x_pt, box_y_pt,
                    width=box_w_pt, height=box_h_pt,
                    preserveAspectRatio=(fit == "contain"), mask="auto",
                )

        elif el_type == "row":
            # Regel als CSS flex space-between: items tussen x1 en x2, eerste
            # links, laatste rechts, gelijke tussenruimte. Een item met
            # "width" (mm) en zonder content is een vaste ruimte (bijv. het
            # beeldmerk links in een kopregel). Leeg item = breedte 0.
            from reportlab.pdfbase.pdfmetrics import stringWidth

            x1, x2 = _mm(el.get("x1")) * MM_TO_PT, _mm(el.get("x2")) * MM_TO_PT
            y = _mm(el.get("y"))
            base_size = float(el.get("size", 10.0))
            placed: list[tuple[str, str, float, str, float, float]] = []
            for j, item in enumerate(el.get("items") or []):
                if "content" not in item:
                    placed.append(("", "", 0.0, "", 0.0, _mm(item.get("width")) * MM_TO_PT))
                    continue
                text = str(item.get("content", "")).format_map(safe_ctx)
                tf = item.get("transform")
                if tf is not None:
                    if tf not in _KNOWN_TRANSFORMS:
                        raise ValueError(
                            f"static_elements: onbekende 'transform'-waarde '{tf}' in "
                            f"{el_label}.items[{j}]."
                        )
                    text = _TRANSFORM_FUNCS[tf](text)
                size = float(item.get("size", base_size))
                font = item.get("font", el.get("font", "LiberationSans"))
                try:
                    stringWidth("x", font, size)
                except KeyError:
                    font = "LiberationSans"
                cs = float(item.get("char_space", 0.0))
                color = _resolve_color(
                    {"color": item.get("color", el.get("color"))}, "color",
                    block=f"{el_label}.items[{j}]",
                ) if text else ""
                width = stringWidth(text, font, size) + cs * max(len(text) - 1, 0) if text else 0.0
                placed.append((text, font, size, color, cs, width))
            if placed:
                gaps = max(len(placed) - 1, 1)
                gap = max(0.0, (x2 - x1 - sum(p[5] for p in placed)) / gaps)
                cx = x1 if len(placed) > 1 else x2 - placed[0][5]
                for text, font, size, color, cs, width in placed:
                    if text:
                        # saveState: letterspatiering (Tc) mag niet doorlekken
                        # naar latere tekst op dezelfde pagina.
                        c.saveState()
                        y_bl = page_height_pt - (y * MM_TO_PT) - size * 0.8
                        tx = c.beginText(cx, y_bl)
                        tx.setFont(font, size)
                        tx.setFillColor(HexColor(color))
                        if cs:
                            tx.setCharSpace(cs)
                        tx.textOut(text)
                        c.drawText(tx)
                        c.restoreState()
                    cx += width + gap

        elif el_type == "text" and el.get("segments"):
            # Tekstregel uit stukken met eigen font/kleur, achter elkaar gezet
            # (bijv. "Bedrijfsnaam" semibold petrol + " - 2459 - versie 0.2"
            # grijs). align: left (default) of right voor de hele regel.
            from reportlab.pdfbase.pdfmetrics import stringWidth

            x, y = _mm(el.get("x")), _mm(el.get("y"))
            size = float(el.get("size", 10.0))
            parts: list[tuple[str, str, float, str]] = []
            for j, seg in enumerate(el["segments"]):
                text = str(seg.get("content", "")).format_map(safe_ctx)
                if not text:
                    continue
                seg_size = float(seg.get("size", size))
                font = seg.get("font", el.get("font", "LiberationSans"))
                try:
                    stringWidth("x", font, seg_size)
                except KeyError:
                    font = "LiberationSans"
                color = _resolve_color(
                    {"color": seg.get("color", el.get("color"))}, "color",
                    block=f"{el_label}.segments[{j}]",
                )
                parts.append((text, font, seg_size, color))
            if not parts:
                continue
            total = sum(stringWidth(t, f, s) for t, f, s, _ in parts)
            x_pt = x * MM_TO_PT
            if el.get("align") == "right":
                x_pt -= total
            y_bl = page_height_pt - (y * MM_TO_PT) - size * 0.8
            for text, font, seg_size, color in parts:
                c.saveState()
                c.setFillColor(HexColor(color))
                c.setFont(font, seg_size)
                c.drawString(x_pt, y_bl, text)
                c.restoreState()
                x_pt += stringWidth(text, font, seg_size)

        elif el_type == "text":
            x, y = _mm(el.get("x")), _mm(el.get("y"))
            content_raw = str(el.get("content", ""))
            content = content_raw.format_map(safe_ctx)
            transform = el.get("transform")
            if transform is not None:
                if transform not in _KNOWN_TRANSFORMS:
                    raise ValueError(
                        f"static_elements: onbekende 'transform'-waarde "
                        f"'{transform}' in {el_label}. Bekende waarden: "
                        f"{sorted(_KNOWN_TRANSFORMS)}."
                    )
                content = _TRANSFORM_FUNCS[transform](content)
            if not content.strip():
                continue
            font = el.get("font", "LiberationSans")
            size = float(el.get("size", 10.0))
            color = _resolve_color(el, "color", block=el_label)
            align = el.get("align", "left")
            # Optioneel: CSS `letter-spacing` (bijv. de KBA-kicker,
            # ".kicker { letter-spacing: .14em }") heeft geen ReportLab-
            # equivalent via drawString. ReportLab's PDFTextObject kent wél
            # `setCharSpace` — een echt PDF-tekststate-veld (de "Tc"-
            # operator), dus de tekst blijft selecteerbaar/doorzoekbaar in
            # de PDF (in tegenstelling tot een benadering met losse spaties
            # tussen letters, wat dat juist zou breken). char_space is in
            # punten (zelfde eenheid als size), niet in mm.
            char_space = el.get("char_space")
            c.saveState()
            c.setFillColor(HexColor(color))
            resolved_font = font
            try:
                c.setFont(font, size)
            except KeyError:
                logger.warning(
                    "static_elements: font '%s' niet geregistreerd bij "
                    "ReportLab (%s) — val terug op LiberationSans.",
                    font, el_label,
                )
                c.setFont("LiberationSans", size)
                resolved_font = "LiberationSans"
            # Baseline-benadering: y_td_pt + size*0.8 (top-van-glyph tot
            # baseline), dan naar ReportLab's bottom-left omgerekend.
            y_bl = page_height_pt - (y * MM_TO_PT) - size * 0.8
            x_pt = x * MM_TO_PT
            if char_space:
                from reportlab.pdfbase.pdfmetrics import stringWidth

                char_space_pt = float(char_space)
                text_x = x_pt
                if align in ("center", "right"):
                    # 'Tc' voegt spacing toe NA elk getoond glyph (ook het
                    # laatste), maar de zichtbare breedte wordt bepaald
                    # door de eerste (n-1) toevoegingen — vandaar
                    # max(len-1, 0) i.p.v. len().
                    extra = char_space_pt * max(len(content) - 1, 0)
                    total_w = stringWidth(content, resolved_font, size) + extra
                    text_x = x_pt - (total_w / 2 if align == "center" else total_w)
                tx = c.beginText(text_x, y_bl)
                tx.setFont(resolved_font, size)
                tx.setFillColor(HexColor(color))
                tx.setCharSpace(char_space_pt)
                tx.textOut(content)
                c.drawText(tx)
            elif align == "center":
                c.drawCentredString(x_pt, y_bl, content)
            elif align == "right":
                c.drawRightString(x_pt, y_bl, content)
            else:
                c.drawString(x_pt, y_bl, content)
            c.restoreState()
