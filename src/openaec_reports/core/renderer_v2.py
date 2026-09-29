"""
Renderer V2 — PyMuPDF-based PDF report generator.

Accepts JSON data (conforming to report.schema.json) and generates
pixel-perfect PDF reports using YAML-driven templates and stationery overlays.

Architecture:
    JSON input → ReportGeneratorV2 → loads YAML templates → renders pages → PDF output

    Cover:      ReportLab canvas (3-layer: photo + PNG overlay + text)
    Colofon:    PyMuPDF insert_text on colofon.pdf stationery
    TOC:        PyMuPDF dynamic rendering on standaard.pdf
    Content:    PyMuPDF dynamic rendering on standaard.pdf (H1, H2, paragraph, bullets)
    Bijlage:    PyMuPDF insert_text on bijlagen.pdf stationery
    Achterblad: Static PDF insert
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fitz
import yaml
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

from openaec_reports.core.rich_text import (
    Break,
    LabelStyle,
    Line,
    Piece,
    Space,
    TextStyle,
    label_piece,
    layout_pieces,
    plain_text,
    runs_of,
    text_pieces,
)

if TYPE_CHECKING:
    from openaec_reports.core.tenant import TenantConfig  # noqa: F401

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ASSETS_DIR = Path(__file__).parent.parent / "assets"
FONT_DIR = ASSETS_DIR / "fonts"
TEMPLATES_DIR = ASSETS_DIR / "templates"

# A4 page dimensions in points (1 pt = 1/72 inch)
A4_PORTRAIT_WIDTH = 595.28
A4_PORTRAIT_HEIGHT = 841.89
A4_LANDSCAPE_WIDTH = 841.89
A4_LANDSCAPE_HEIGHT = 595.28

# Maximum y coordinate for content (bottom boundary, top-down).
# Conservatieve fallback waarden — tenants overrulen dit via
# `templates/<brand>/standaard.yaml` content_area.y_td_end. Typische
# stationery begint de footer/logo zone rond y=768; 760 geeft 8pt clearance.
Y_MAX_PORTRAIT = 760.0
Y_MAX_LANDSCAPE = 533.0


def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    """Convert hex color string to (r, g, b) tuple with 0-1 range."""
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _style_color(
    style: dict,
    key: str,
    semantic: str,
    brand_config: Any | None,
    *,
    block: str,
) -> str:
    """Resolveer een hex-kleur voor ``style[key]`` zonder stille merk-fallback.

    Achtergrond: renderer_v2 bevatte historisch ~50 ``.get(key, "#40124A")``
    -achtige aanroepen. Zolang een tenant-template een blok volledig
    definieerde (zoals 3BM's ``content_styles.yaml`` voor de meeste blokken
    deed) was dat onschuldig — de default werd nooit geraakt. Maar zodra een
    tenant een blok NIET definieert (bijv. KBA's ontbrekende
    ``calculation``/``check``-secties), rendert de pagina alsnog, in 3BM's
    paars/turquoise — een silent brand-lekkage die geen enkele test ving
    (zie orchestrator-sessie 2026-07-10, "699 paarse pixels").

    Volgorde:
    1. ``style.get(key)`` — expliciete waarde uit het tenant-template
       (letterlijke hex, of een ``$colors.<naam>``-ref die door
       ``core/refs.py`` al is opgelost tijdens het laden — zie
       ``TemplateSet._load``).
    2. ``brand_config.colors[semantic]`` — semantische merk-kleur, gebruikt
       wanneer het blok de sleutel niet expliciet zet.
    3. Geen van beide beschikbaar → ``ValueError`` met tenant, blok en
       sleutel. Er is bewust GEEN derde stap die terugvalt op een
       hardcoded kleur.

    Args:
        style: Style-dict voor het huidige blok (bijv. de ``calc_s`` /
            ``chk_s`` dict uit ``ContentRenderer.calculation``/``check``).
        key: Veldnaam binnen ``style`` (bijv. ``"background"``).
        semantic: Sleutel in ``brand_config.colors`` om op terug te vallen
            (bijv. ``"surface"``, ``"primary"``, ``"warning"``).
        brand_config: Actieve ``BrandConfig``, of ``None`` wanneer de
            renderer buiten de normale ``ReportGeneratorV2``-pijplijn om
            wordt gebruikt (bijv. losse unit tests zonder brand-context).
        block: Naam van het blok/veld, uitsluitend voor de foutmelding
            (bijv. ``"calculation.background"``).

    Returns:
        Hex-kleur string (bijv. ``"#40124A"``).

    Raises:
        ValueError: Als noch het template noch het merk-palet de kleur
            definieert.
    """
    val = style.get(key)
    if val:
        return val
    colors = getattr(brand_config, "colors", None) if brand_config is not None else None
    if colors and semantic in colors:
        return colors[semantic]
    tenant_label = "?"
    if brand_config is not None:
        tenant_label = (
            getattr(brand_config, "tenant", "") or getattr(brand_config, "slug", "?")
        )
    raise ValueError(
        f"Ontbrekende kleur voor tenant '{tenant_label}', blok '{block}': "
        f"veld '{key}' is niet gezet in het template EN brand.colors."
        f"{semantic} bestaat niet (beschikbare merk-kleuren: "
        f"{sorted(colors) if colors else []}). Geen stille fallback naar "
        "een hardcoded kleur — voeg de sleutel toe aan content_styles.yaml "
        "(of het relevante template-bestand), of aan brand.colors."
    )


_RE_HTML_TAG = re.compile(r"<[^>]+>")
# Matcht alleen als de VOLLEDIGE celinhoud omhuld is met ``<b>...</b>``
# of ``<strong>...</strong>``. Partial markup zoals ``"<b>foo</b> bar"``
# wordt NIET als bold gemarkeerd; de tags worden dan wel gestript door
# ``_strip_html`` zodat ze niet letterlijk in de PDF verschijnen.
_RE_BOLD_WRAPPER = re.compile(
    r"^\s*<(?:b|strong)>(.*?)</(?:b|strong)>\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _derive_bold_fontname(
    base_fontname: str, fallback_bold: str = "Inter-Bold"
) -> str:
    """Leid de bold variant af van een font naam.

    - Als ``base_fontname`` al ``"Bold"`` bevat: retourneer ongewijzigd.
    - Als het een Inter-variant is: retourneer ``"Inter-Bold"``.
    - Anders: retourneer ``fallback_bold``.

    Gebruikt om inline ``<b>``-cells in tabellen correct te renderen
    zonder per cel de font-naamdetectie te dupliceren.
    """
    if "Bold" in base_fontname:
        return base_fontname
    if base_fontname.startswith("Inter"):
        return "Inter-Bold"
    return fallback_bold


def _strip_html(text: str) -> str:
    """Strip HTML tags from text, preserving readable content."""
    if "<" not in text:
        return text
    clean = _RE_HTML_TAG.sub("", text)
    # Collapse whitespace from removed tags
    clean = re.sub(r"[ \t]+", " ", clean).strip()
    return clean


def _parse_cell(value: object) -> tuple[str, bool]:
    """Parse a table cell value to (plain_text, is_bold).

    Detecteert of de cell volledig omhuld is met ``<b>...</b>`` of
    ``<strong>...</strong>`` en retourneert dan ``is_bold=True``. Alle
    overige HTML tags worden gestript zodat ze niet letterlijk in de
    PDF verschijnen.

    Args:
        value: Ruwe celwaarde (str, int, float, etc.).

    Returns:
        Tuple van (geschoonde tekst, is_bold flag).
    """
    if value is None:
        return "", False
    text = str(value)
    if "<" not in text:
        return text, False
    match = _RE_BOLD_WRAPPER.match(text)
    is_bold = match is not None
    return _strip_html(text), is_bold


_PT_PER_MM = 72 / 25.4


@dataclass
class _CellSpec:
    """Genormaliseerde tabelcel. ``rich=False`` = oud pad (tekst + hele-cel-vet)."""

    text: str
    bold: bool
    italic: bool = False
    color: str | None = None
    bg_color: str | None = None
    align: str = "left"
    runs: list[dict] | None = None
    rich: bool = False

    def as_runs(self) -> list[dict]:
        """Runs voor de regelopbouw; zonder eigen runs een run met de celopmaak."""
        if self.runs:
            return self.runs
        return [{"text": self.text, "bold": self.bold, "italic": self.italic}]


# ---------------------------------------------------------------------------
# Template Loader
# ---------------------------------------------------------------------------
class TemplateSet:
    """Loads and holds all YAML templates + stationery paths for a brand.

    Sinds 2026-04-20 multi-tenant aware: een optionele ``tenant_config``
    activeert de cascade ``<tenant>/templates/[<brand>/]<file>`` →
    ``<package>/templates/<brand>/<file>`` → ``<package>/templates/<file>``
    (gedeelde defaults). Bij gebruik zonder ``tenant_config`` gedraagt de
    klasse zich backward-compatible: zoekt in ``<package>/templates/<brand>/``
    en faalt met ``FileNotFoundError`` als die directory ontbreekt.
    """

    def __init__(
        self,
        brand: str | None = None,
        tenant_config: TenantConfig | None = None,  # noqa: F821 — fwd-ref
        brand_config: Any | None = None,  # BrandConfig — fwd-ref, zie core.brand
    ):
        self.brand = brand or os.environ.get("OPENAEC_DEFAULT_BRAND", "default")
        self._tenant_config = tenant_config
        # BrandConfig voor $colors./$fonts.-substitutie tijdens het laden
        # (zie core/refs.py). Optioneel en achterwaarts compatibel: zonder
        # brand_config wordt geen substitutie uitgevoerd (bestaande
        # call-sites/tests die TemplateSet zonder brand_config aanroepen
        # blijven ongewijzigd werken).
        self.brand_config = brand_config

        if tenant_config is not None:
            # Cascade mode: directory wordt per-template bepaald, maar exposeer
            # ``self.dir`` (primaire bron) voor backward compat met callers
            # die het attribuut inspecteren.
            if tenant_config.tenant_dir:
                self.dir = tenant_config.tenant_dir / "templates"
            else:
                self.dir = TEMPLATES_DIR / self.brand
        else:
            self.dir = TEMPLATES_DIR / self.brand
            if not self.dir.exists():
                raise FileNotFoundError(
                    f"Template directory not found: {self.dir}"
                )

        self.cover = self._load("cover.yaml")
        self.colofon = self._load("colofon.yaml")
        self.toc = self._load("toc.yaml")
        self.standaard = self._load("standaard.yaml")
        self.content_styles = self._load("content_styles.yaml")
        self.bijlage = self._load("bijlage.yaml")

    def _load(self, filename: str) -> dict:
        """Laad een template-bestand met tenant → package cascade.

        Wanneer ``tenant_config`` beschikbaar is, gebruikt ``find_template``
        de volledige cascade. Zonder tenant_config blijft het oude gedrag
        actief (package-only).
        """
        path: Path | None
        if self._tenant_config is not None:
            path = self._tenant_config.find_template(filename, brand=self.brand)
            if path is None:
                logger.warning(
                    "Template niet gevonden in tenant '%s' of package: %s",
                    getattr(self._tenant_config, "tenant_dir", None),
                    filename,
                )
                return {}
        else:
            path = self.dir / filename
            if not path.exists():
                logger.warning("Template not found: %s", path)
                return {}
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if self.brand_config is not None:
            from openaec_reports.core.refs import resolve_refs

            data = resolve_refs(
                data,
                self.brand_config,
                tenant=getattr(self.brand_config, "tenant", "") or self.brand,
                source=filename,
            )
        return data

    @property
    def blocks(self) -> dict:
        return self.content_styles.get("blocks", {})

    @property
    def page_number(self) -> dict:
        return self.content_styles.get("page_number", {})


# ---------------------------------------------------------------------------
# Font Manager
# ---------------------------------------------------------------------------
class FontManager:
    """Manages font registration for both ReportLab and PyMuPDF.

    Ondersteunt twee modi:
    - Custom fonts (Inter): laad TTF bestanden uit font_dir
    - Fallback: Liberation Sans (altijd embedded, nooit Type1 Helvetica)
    """

    _DEFAULT_FONT_MAP = {
        "Inter-Bold": "Inter-Bold.ttf",
        "Inter-Regular": "Inter-Regular.ttf",
        "Inter-Medium": "Inter-Medium.ttf",
    }

    # Liberation Sans fallback fonts (altijd beschikbaar in FONT_DIR)
    _LIBERATION_FONT_MAP = {
        "LiberationSans": "LiberationSans-Regular.ttf",
        "LiberationSans-Bold": "LiberationSans-Bold.ttf",
        "LiberationSans-Italic": "LiberationSans-Italic.ttf",
    }

    _BUILTIN_FONTS = {
        "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
        "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
        "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
        "Symbol", "ZapfDingbats",
        "Arial", "ArialMT", "Arial-BoldMT",
        "LiberationSans", "LiberationSans-Bold", "LiberationSans-Italic",
    }

    def __init__(
        self,
        font_dir: Path | None = None,
        brand_fonts: dict | None = None,
        tenant_config: TenantConfig | None = None,  # noqa: F821 — fwd-ref
    ):
        # Primair font_dir = tenant override / backward-compat single dir
        self.font_dir = font_dir or FONT_DIR
        self._tenant_config = tenant_config

        # Effectieve cascade: tenant → expliciete font_dir → package. Laatste
        # wint bij duplicate naam; zie ``_find_font_path`` voor lookup-volgorde.
        cascade: list[Path] = []
        if tenant_config is not None:
            for d in tenant_config.fonts_dirs:
                if d not in cascade:
                    cascade.append(d)
        if font_dir and font_dir not in cascade:
            cascade.insert(0, font_dir)
        if FONT_DIR not in cascade and FONT_DIR.exists():
            cascade.append(FONT_DIR)
        self._font_cascade: list[Path] = cascade

        self._rl_registered = False
        self._fitz_fonts: dict[str, fitz.Font] = {}
        self._missing_fonts: set[str] = set()
        self._uses_custom_fonts = True

        # Laad Liberation Sans als embedded fallback (altijd)
        self._load_liberation_fonts()

        # Bepaal of we custom fonts of builtin/liberation fonts gebruiken
        if brand_fonts:
            heading = brand_fonts.get("heading", "")
            body = brand_fonts.get("body", "")
            heading_fb = brand_fonts.get("heading_fallback", heading)
            body_fb = brand_fonts.get("body_fallback", body)
            if self._is_builtin(heading or heading_fb) and self._is_builtin(body or body_fb):
                self._uses_custom_fonts = False

        if self._uses_custom_fonts:
            self.FONT_MAP = dict(self._DEFAULT_FONT_MAP)
            for name, filename in self.FONT_MAP.items():
                path = self._find_font_path(filename)
                if path is not None:
                    self._fitz_fonts[name] = fitz.Font(fontfile=str(path))
            self._bold_font = self._fitz_fonts.get("Inter-Bold") or self._liberation_bold
            self._book_font = self._fitz_fonts.get("Inter-Regular") or self._liberation_regular
        else:
            self.FONT_MAP = {}
            self._bold_font = self._liberation_bold
            self._book_font = self._liberation_regular

    def _find_font_path(self, filename: str) -> Path | None:
        """Zoek een font-bestand via de cascade (tenant → font_dir → package).

        Eerste match wint.
        """
        for base in self._font_cascade:
            candidate = base / filename
            if candidate.exists():
                return candidate
        return None

    def _load_liberation_fonts(self) -> None:
        """Laad Liberation Sans als embedded fallback voor PyMuPDF."""
        lib_bold_path = self._find_font_path("LiberationSans-Bold.ttf")
        lib_regular_path = self._find_font_path("LiberationSans-Regular.ttf")
        lib_italic_path = self._find_font_path("LiberationSans-Italic.ttf")

        if lib_bold_path is not None:
            self._liberation_bold = fitz.Font(fontfile=str(lib_bold_path))
            self._fitz_fonts["LiberationSans-Bold"] = self._liberation_bold
        else:
            logger.warning("LiberationSans-Bold.ttf niet gevonden, val terug op helv")
            self._liberation_bold = fitz.Font("helv")

        if lib_regular_path is not None:
            self._liberation_regular = fitz.Font(fontfile=str(lib_regular_path))
            self._fitz_fonts["LiberationSans"] = self._liberation_regular
        else:
            logger.warning("LiberationSans-Regular.ttf niet gevonden, val terug op helv")
            self._liberation_regular = fitz.Font("helv")

        if lib_italic_path is not None:
            self._liberation_italic = fitz.Font(fontfile=str(lib_italic_path))
            self._fitz_fonts["LiberationSans-Italic"] = self._liberation_italic
        else:
            self._liberation_italic = self._liberation_regular

    @property
    def inter_book(self) -> fitz.Font:
        """Backward compatibility alias."""
        return self._book_font

    @property
    def inter_bold(self) -> fitz.Font:
        """Backward compatibility alias."""
        return self._bold_font

    @classmethod
    def _is_builtin(cls, fontname: str) -> bool:
        """Check of een fontnaam een ingebouwde of Liberation font is."""
        return (
            fontname in cls._BUILTIN_FONTS
            or fontname.startswith("Helvetica")
            or fontname.startswith("Arial")
            or fontname.startswith("Liberation")
        )

    def register_reportlab(self) -> None:
        """Register fonts with ReportLab (once).

        Volgorde:
        1. Liberation Sans als embedded fallback (via core/fonts module).
        2. Expliciete FONT_MAP (legacy Inter-* conventie).
        3. **Alle .ttf/.otf in de font-cascade** — elk bestand wordt
           geregistreerd onder zijn stem (``Gotham-Bold``) én onder de
           variant zonder separators (``GothamBold``). Dit is noodzakelijk
           voor tenant-specifieke fonts (brand.yaml verwijst vaak naar
           ``GothamBold`` terwijl het bestand ``Gotham-Bold.ttf`` heet).
        """
        if self._rl_registered:
            return
        from openaec_reports.core.fonts import register_liberation_fonts
        lib_dir = None
        for base in self._font_cascade:
            if (base / "LiberationSans-Regular.ttf").exists():
                lib_dir = base
                break
        register_liberation_fonts(lib_dir)

        # Legacy Inter FONT_MAP (blijft voor backward compat)
        for name, filename in self.FONT_MAP.items():
            path = self._find_font_path(filename)
            if path is not None:
                try:
                    pdfmetrics.registerFont(TTFont(name, str(path)))
                except (OSError, ValueError):
                    logger.warning("Could not register font: %s", name)

        # Cascade auto-register — alle .ttf/.otf in tenant fonts + fallback
        # dirs. Eerste match wint (cascade-order) bij duplicate keys.
        _registered: set[str] = set()
        for base in self._font_cascade:
            if not base.exists():
                continue
            for font_path in sorted(base.glob("*.ttf")) + sorted(base.glob("*.otf")):
                stem = font_path.stem  # "Gotham-Bold"
                stripped = stem.replace("-", "").replace("_", "").replace(" ", "")
                for key in {stem, stripped}:
                    if key in _registered:
                        continue
                    try:
                        pdfmetrics.registerFont(TTFont(key, str(font_path)))
                        _registered.add(key)
                    except (OSError, ValueError) as e:  # noqa: PERF203
                        logger.debug(
                            "Could not auto-register font %s from %s: %s",
                            key, font_path, e,
                        )
        self._rl_registered = True

    def insert_into_page(self, page: fitz.Page) -> None:
        """Insert fonts into a PyMuPDF page (custom + Liberation fallback)."""
        # Altijd Liberation Sans inserten als embedded fallback (cascade)
        for name, filename in self._LIBERATION_FONT_MAP.items():
            path = self._find_font_path(filename)
            if path is not None:
                page.insert_font(fontname=name, fontfile=str(path))
        # Custom fonts
        if self._uses_custom_fonts:
            for name, filename in self.FONT_MAP.items():
                path = self._find_font_path(filename)
                if path is not None:
                    page.insert_font(fontname=name, fontfile=str(path))

    def _find_font_by_name(self, fontname: str) -> fitz.Font | None:
        """Laad een font uit de cascade op naam, zoals ``register_reportlab`` registreert.

        Match op bestandsstam (``Gotham-Book``) of op de stam zonder ``-``,
        ``_`` en spaties (``GothamBook``), hoofdletterongevoelig; .ttf gaat
        voor .otf. Resultaat wordt gecachet onder ``fontname``.
        """
        key = fontname.replace("-", "").replace("_", "").replace(" ", "").lower()
        if not key:
            return None
        for suffix in (".ttf", ".otf"):
            for base in self._font_cascade:
                if not base.exists():
                    continue
                for path in sorted(base.glob(f"*{suffix}")):
                    stem = path.stem
                    stripped = stem.replace("-", "").replace("_", "").replace(" ", "").lower()
                    if stem.lower() == fontname.lower() or stripped == key:
                        try:
                            font = fitz.Font(fontfile=str(path))
                        except (RuntimeError, ValueError) as e:  # pragma: no cover
                            logger.warning("Font %s niet te laden: %s", path, e)
                            return None
                        self._fitz_fonts[fontname] = font
                        return font
        return None

    def get_fitz_font(self, fontname: str) -> fitz.Font:
        """Return fitz.Font object for given fontname string.

        Volgorde: geladen fonts, dan een font-bestand met die naam in de
        tenant-cascade (bijv. ``SegoeUI`` -> ``SegoeUI.ttf``), dan de
        fallback (Inter of Liberation; vet als de naam 'bold' bevat).
        """
        if fontname in self._fitz_fonts:
            return self._fitz_fonts[fontname]
        if fontname in self._missing_fonts:
            found = None
        else:
            found = self._find_font_by_name(fontname)
            if found is None:
                self._missing_fonts.add(fontname)
        if found is not None:
            return found
        is_bold = "bold" in fontname.lower() or "Bold" in fontname
        return self._bold_font if is_bold else self._book_font

    def get_variant(
        self,
        base_fontname: str,
        *,
        bold: bool = False,
        italic: bool = False,
        bold_fontname: str | None = None,
        italic_fontname: str | None = None,
    ) -> fitz.Font:
        """Font voor opgemaakte runs: vet, cursief of beide.

        Zonder opmaak is dit ``get_fitz_font(base_fontname)``, zodat runs
        hetzelfde font krijgen als gewone tekst. Voor vet en cursief eerst
        de template-namen (``fonts.bold`` / ``fonts.book_italic``), anders
        de Liberation-variant. Vet-cursief valt terug op Liberation
        BoldItalic, of vet als dat bestand ontbreekt.
        """
        if bold and italic:
            cached = self._fitz_fonts.get("LiberationSans-BoldItalic")
            if cached is not None:
                return cached
            path = self._find_font_path("LiberationSans-BoldItalic.ttf")
            if path is None:
                return self._bold_font
            font = fitz.Font(fontfile=str(path))
            self._fitz_fonts["LiberationSans-BoldItalic"] = font
            return font
        if bold:
            if bold_fontname:
                return self.get_fitz_font(bold_fontname)
            return self._bold_font
        if italic:
            if italic_fontname:
                found = self._fitz_fonts.get(italic_fontname) or self._find_font_by_name(
                    italic_fontname
                )
                if found is not None:
                    return found
            return self._liberation_italic
        return self.get_fitz_font(base_fontname)

    def measure(
        self, text: str, fontsize: float, bold: bool = False, fontname: str | None = None,
    ) -> float:
        """Measure text width using font metrics.

        Met ``fontname`` wordt gemeten met het font waarmee ook getekend
        wordt (``get_fitz_font``); zonder valt het terug op book/bold.
        """
        if fontname:
            font = self.get_fitz_font(fontname)
        else:
            font = self._bold_font if bold else self._book_font
        return font.text_length(text, fontsize=fontsize)

    def wrap_text(
        self,
        text: str,
        fontsize: float,
        max_width: float,
        bold: bool = False,
        fontname: str | None = None,
    ) -> list[str]:
        """Word-wrap text to fit within max_width.

        Splits on spaces first, then on underscores/hyphens for long tokens,
        and finally breaks mid-character as last resort.
        """
        if not text:
            return [""]
        if max_width <= 0:
            return [text]

        # First split on spaces
        tokens = text.split()
        lines: list[str] = []
        current = ""

        for token in tokens:
            test = f"{current} {token}".strip()
            if self.measure(test, fontsize, bold, fontname) <= max_width:
                current = test
                continue

            # Token doesn't fit on current line
            if current:
                lines.append(current)
                current = ""

            # Check if single token fits on a fresh line
            if self.measure(token, fontsize, bold, fontname) <= max_width:
                current = token
                continue

            # Token too wide — split on underscores/hyphens first
            sub_parts = re.split(r"(?<=[_\-/.])", token)  # keep delimiter at end of part
            for sp in sub_parts:
                if not sp:
                    continue
                test = f"{current}{sp}" if current else sp
                if self.measure(test, fontsize, bold, fontname) <= max_width:
                    current = test
                else:
                    if current:
                        lines.append(current)
                    # If sub-part itself is too wide, break by character
                    if self.measure(sp, fontsize, bold, fontname) > max_width:
                        current = ""
                        for ch in sp:
                            test_ch = current + ch
                            if (
                                self.measure(test_ch, fontsize, bold, fontname) > max_width
                                and current
                            ):
                                lines.append(current)
                                current = ch
                            else:
                                current = test_ch
                    else:
                        current = sp

        if current:
            lines.append(current)
        return lines if lines else [""]


# ---------------------------------------------------------------------------
# Shared image resolver
# ---------------------------------------------------------------------------


def _is_svg(path: Path) -> bool:
    """SVG op extensie, of op inhoud als de extensie ontbreekt of niet klopt."""
    if path.suffix.lower() == ".svg":
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(512).lstrip()
    except OSError:
        return False
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _resolve_image(src) -> Path | None:
    """Resolve image source: file path, base64 dict, or None."""
    if not src:
        return None

    if isinstance(src, dict):
        # Base64 encoded image
        data = src.get("data", "")
        media_type = src.get("media_type", "image/png")
        if "svg" in media_type:
            ext = ".svg"
        else:
            ext = ".png" if "png" in media_type else ".jpg"
        try:
            raw = base64.b64decode(data)
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            tmp.write(raw)
            tmp.close()
            return Path(tmp.name)
        except (ValueError, OSError) as e:
            logger.warning("Base64 decode failed: %s", e)
            return None

    # File path
    p = Path(src)
    if p.exists():
        return p
    return None


# ---------------------------------------------------------------------------
# Cover Generator (ReportLab)
# ---------------------------------------------------------------------------
class CoverGenerator:
    """Generates cover page using ReportLab 3-layer approach."""

    def __init__(
        self, templates: TemplateSet, fonts: FontManager, brand_config: Any | None = None
    ):
        self.tpl = templates.cover
        self.fonts = fonts
        self._brand_config = brand_config

    def _color(self, style: dict, key: str, semantic: str, block: str) -> str:
        return _style_color(style, key, semantic, self._brand_config, block=block)

    def generate(
        self, data: dict, stationery: Path | None, output: Path, cover_is_pdf: bool = False,
    ) -> Path:
        """Generate cover PDF.

        Twee modi:
        - PNG overlay: ReportLab canvas met foto + PNG overlay + tekst
        - PDF stationery: PyMuPDF insert tekst op PDF stationery
        """
        if cover_is_pdf and stationery and stationery.exists():
            return self._generate_from_pdf(data, stationery, output)
        return self._generate_from_png(data, stationery, output)

    @staticmethod
    def _wrap_by_width(
        text: str, max_w: float, width_of: "callable[[str], float]"
    ) -> list[str]:
        """Woordterugloop op basis van een breedte-meetfunctie.

        ``width_of(s)`` geeft de renderbreedte van ``s`` in punten (canvas
        ``stringWidth`` of fitz ``Font.text_length``). Een enkel woord dat op
        zichzelf al te breed is blijft op een eigen regel staan (geen
        karakter-afbreken — voor titels zeldzaam en beter dan een lelijke split).
        """
        lines: list[str] = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if not current or width_of(candidate) <= max_w:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [text]

    def _draw_cover_title(
        self, c: "rl_canvas.Canvas", tf: dict, title_text: str,
        color_hex: str, page_w: float,
    ) -> float:
        """Teken de cover-titel met woordterugloop, naar BENEDEN uitgelijnd.

        De eerste regel blijft op de originele ``y_bl`` staan (de kicker zit er
        vlak boven en mag niet geraakt worden); extra regels stapelen eronder in
        de lege ruimte in het midden van de cover. Retourneert de ``shift`` (de
        afstand die de laatste regel onder de originele ``y_bl`` uitkomt) zodat
        de aanroeper de subtitel mee kan laten zakken. Een titel op één regel
        levert exact de oude ``drawString`` op + shift 0 — de pixel-baseline
        blijft gelijk. ``max_width`` en ``leading`` zijn per template
        overschrijfbaar.
        """
        font = tf.get("font", "Inter-Bold")
        size = tf.get("size", 28.9)
        x = tf.get("x", 54.3)
        y_bl = tf.get("y_bl", 120.5)
        max_w = tf.get("max_width", max(50.0, page_w - 2 * x))
        leading = tf.get("leading", size * 1.12)
        c.setFont(font, size)
        c.setFillColor(HexColor(color_hex))
        lines = self._wrap_by_width(
            title_text, max_w, lambda s: c.stringWidth(s, font, size)
        )
        for i, line in enumerate(lines):
            c.drawString(x, y_bl - i * leading, line)
        return (len(lines) - 1) * leading

    def _generate_from_pdf(self, data: dict, stationery_pdf: Path, output: Path) -> Path:
        """Cover via PyMuPDF: tekst op PDF stationery."""
        doc = fitz.open(str(stationery_pdf))
        page = doc[0]
        page.clean_contents()  # Normaliseer content stream voor TextWriter
        self.fonts.insert_into_page(page)

        fields = self.tpl.get("dynamic_fields", {})

        # Rapport type
        tf = fields.get("rapport_type", {})
        title_text = data.get("report_type", "")
        title_shift = 0.0
        if title_text:
            font_obj = self.fonts.get_fitz_font(tf.get("font", "LiberationSans-Bold"))
            size = tf.get("size", 28)
            x = tf.get("x", 54)
            y_bl = tf.get("y_bl", tf.get("y", 93))
            y_td = page.rect.height - y_bl
            # Woordterugloop, naar beneden uitgelijnd (eerste regel op y_bl;
            # in top-down coords = grotere y_td). ``title_shift`` laat de
            # subtitel mee zakken.
            max_w = tf.get("max_width", max(50.0, page.rect.width - 2 * x))
            leading = tf.get("leading", size * 1.12)
            lines = self._wrap_by_width(
                title_text, max_w, lambda s: font_obj.text_length(s, fontsize=size)
            )
            tw = fitz.TextWriter(page.rect)
            for i, line in enumerate(lines):
                tw.append((x, y_td + i * leading), line,
                          font=font_obj, fontsize=size)
            title_shift = (len(lines) - 1) * leading
            color = self._color(tf, "color", "primary", "cover.rapport_type(pdf)")
            tw.write_text(page, color=_hex_to_rgb(color))

        # Project naam
        sf = fields.get("project_naam", {})
        subtitle_text = data.get("project", "")
        if subtitle_text:
            font_obj = self.fonts.get_fitz_font(sf.get("font", "LiberationSans"))
            size = sf.get("size", 17)
            y_bl = sf.get("y_bl", sf.get("y", 63))
            y_td = page.rect.height - y_bl + title_shift
            tw = fitz.TextWriter(page.rect)
            tw.append((sf.get("x", 55), y_td), subtitle_text, font=font_obj, fontsize=size)
            color = self._color(sf, "color", "secondary", "cover.project_naam(pdf)")
            tw.write_text(page, color=_hex_to_rgb(color))

        # Cover image (als beschikbaar en er is een foto-zone in de template)
        photo_cfg = self.tpl.get("photo")
        cover_image_src = data.get("cover", {}).get("image")
        if photo_cfg and cover_image_src:
            cover_image_path = _resolve_image(cover_image_src)
            if cover_image_path:
                px = photo_cfg.get("x", 55.6)
                py_bl = photo_cfg.get("y", 161.7)
                pw = photo_cfg.get("width", 484.0)
                ph = photo_cfg.get("height", 560.8)
                py_td = page.rect.height - py_bl - ph
                rect = fitz.Rect(px, py_td, px + pw, py_td + ph)
                try:
                    page.insert_image(rect, filename=str(cover_image_path))
                except (OSError, ValueError, RuntimeError) as e:
                    logger.warning("Cover image insert failed: %s", e)

        doc.save(str(output))
        doc.close()
        return output

    def _static_elements_for_page(self, page_type: str) -> list[dict] | None:
        """Haal ``pages.<page_type>.static_elements`` op uit brand_config.

        Zie ``core/static_elements.py`` voor het element-formaat. Retourneert
        ``None`` als er geen brand_config is, of als de tenant dit
        paginatype niet als static_elements uitdrukt (backward compat: dan
        blijft de bestaande, 3BM-specifieke tekencode actief).
        """
        if self._brand_config is None:
            return None
        page_cfg = (self._brand_config.pages or {}).get(page_type, {})
        elements = page_cfg.get("static_elements")
        return elements if elements else None

    def _prepare_duotone_photo(
        self, image_path: Path, tint_hex: str, strength: float = 0.55
    ) -> Path:
        """Genereer een duotone (HSL-luminositeit + merk-tint) variant.

        CSS-bron (``coverblad.html``, klasse ``.a``)::

            .a .band { background: var(--petrol); }
            .a .band img { filter: grayscale(1) contrast(1.15);
                            mix-blend-mode: luminosity; opacity: .55; }

        ``mix-blend-mode: luminosity`` is GEEN lineaire interpolatie naar
        wit (dat was de vorige, systematisch te lichte implementatie —
        zie git-historie van dit bestand). Het is een HSL-compositie: de
        **hue + saturation van de onderlaag** (petrol) blijven staan, en
        alleen de **lightness** wordt overgenomen van de bovenlaag (de
        gefilterde foto). Dat resultaat wordt vervolgens met ``opacity``
        over de vlakke petrol-achtergrond gelegd.

        Implementatiestappen:

        1. **Lightness van de foto — bewuste HSL-keuze, niet Pillow's
           "L"-band.** We gebruiken ``L = (max(R,G,B) + min(R,G,B)) / 2``
           — dezelfde formule als ``colorsys.rgb_to_hls`` — in plaats van
           ``PIL.ImageOps.grayscale()`` (dat ITU-R BT.601 fotometrische
           luma gebruikt: ``0.299R + 0.587G + 0.114B``). Reden: CSS'
           ``luminosity``-blendmode hoort tot dezelfde HSL-kleurmodel-
           familie als ``hue``/``saturation``/``color`` (zie de CSS
           Compositing-spec, die overigens zelf wél Rec.601-gewichten
           gebruikt voor de exacte ``SetLum``-berekening — dit blijft dus
           een gedocumenteerde BENADERING, geen pixel-exacte W3C-
           reproductie, maar wel de HSL-conforme interpretatie die de
           opdracht vraagt in plaats van de vorige wit-interpolatie).
        2. Contrastversterking op die lightness (≈ CSS ``contrast(1.15)``
           in dezelfde filter-keten, vóór de blend).
        3. Hue en saturation van de petrol-tint (via ``colorsys.
           rgb_to_hls``) blijven constant over het hele beeld — petrol is
           een vlakke kleur, geen foto — en worden gecombineerd met de
           per-pixel lightness via ``colorsys.hls_to_rgb``. Vectorized met
           een 256-staps lookup-table op lightness (H/S zijn constant,
           dus goedkoop) i.p.v. een Python-loop per pixel.
        4. Composite met ``opacity=strength`` over het vlakke petrol-vlak
           (≈ CSS ``opacity: var(--duotone)`` over ``background: petrol``).

        Resultaat wordt als tijdelijke PNG weggeschreven; de aanroeper is
        verantwoordelijk voor cleanup (net als ``_resolve_image`` elders in
        dit bestand voor base64-afbeeldingen).
        """
        import colorsys

        import numpy as np
        from PIL import Image as PILImage

        img = PILImage.open(image_path).convert("RGB")
        arr = np.asarray(img).astype(np.float64) / 255.0  # (H, W, 3), 0..1

        # --- Stap 1+2: HSL-lightness van de foto + contrastversterking ---
        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        cmax = np.maximum(np.maximum(r, g), b)
        cmin = np.minimum(np.minimum(r, g), b)
        lum = (cmax + cmin) / 2.0
        lum = np.clip((lum - 0.5) * 1.15 + 0.5, 0.0, 1.0)  # contrast(1.15)

        # --- Stap 3: hue/saturation van petrol + lightness van de foto ---
        tint_hex_clean = tint_hex.lstrip("#")
        tint_rgb = tuple(
            int(tint_hex_clean[i : i + 2], 16) / 255.0 for i in (0, 2, 4)
        )
        tint_h, _tint_l, tint_s = colorsys.rgb_to_hls(*tint_rgb)

        lut = np.array(
            [
                colorsys.hls_to_rgb(tint_h, lightness / 255.0, tint_s)
                for lightness in range(256)
            ],
            dtype=np.float64,
        )  # (256, 3)
        lum_idx = np.clip((lum * 255.0).round().astype(np.int32), 0, 255)
        hsl_composite = lut[lum_idx]  # (H, W, 3), 0..1

        # --- Stap 4: opacity-blend over het vlakke petrol-vlak ---
        flat_tint = np.array(tint_rgb, dtype=np.float64)
        blended = flat_tint * (1.0 - strength) + hsl_composite * strength

        result_arr = np.clip(blended * 255.0, 0, 255).astype(np.uint8)
        result = PILImage.fromarray(result_arr, mode="RGB")
        tmp = tempfile.NamedTemporaryFile(suffix="_duotone.png", delete=False)
        result.save(tmp.name)
        tmp.close()
        return Path(tmp.name)

    def _generate_static_cover(
        self, data: dict, elements: list[dict], output: Path
    ) -> Path:
        """Render de cover volledig uit ``pages.cover.static_elements``.

        Gebruikt voor tenants (bijv. KBA variant a) die geen hand-getekende
        PNG-stationery hebben en hun ontwerp puur data-gedreven uitdrukken.
        De projectfoto ("{cover_photo}"-token) wordt, als het element
        ``duotone: true`` zet, eerst via :meth:`_prepare_duotone_photo`
        omgezet.
        """
        from openaec_reports.core.static_elements import render_static_elements

        self.fonts.register_reportlab()
        w, h = A4
        c = rl_canvas.Canvas(str(output), pagesize=A4)

        colofon_data = data.get("colofon", {})
        contact = getattr(self._brand_config, "contact", {}) or {}
        context: dict[str, str] = {
            "report_type": data.get("report_type", ""),
            "kicker": data.get("kicker", ""),
            "project": data.get("project", ""),
            "project_number": data.get("project_number", ""),
            "date": data.get("date", ""),
            "version": data.get("version", ""),
            "status": data.get("status", ""),
            "client": colofon_data.get("opdrachtgever_naam", data.get("client", "")),
            "author": colofon_data.get("adviseur_naam", data.get("author", "")),
            "company_name": contact.get("name", ""),
            "company_website": contact.get("website", ""),
            "company_email": contact.get("email", ""),
            "company_address": contact.get("address", ""),
        }

        cover_image_src = data.get("cover", {}).get("image")
        cover_image_path = _resolve_image(cover_image_src)
        tmp_duotone: Path | None = None
        if cover_image_path:
            wants_duotone = any(
                el.get("type") == "image"
                and el.get("src") == "{cover_photo}"
                and el.get("duotone")
                for el in elements
            )
            if wants_duotone:
                tint = None
                for el in elements:
                    if el.get("type") == "image" and el.get("duotone"):
                        tint = el.get("duotone_tint") or el.get("fill")
                        break
                tint = tint or "#000000"
                tmp_duotone = self._prepare_duotone_photo(
                    cover_image_path, tint,
                    strength=next(
                        (
                            float(el.get("duotone_strength", 0.55))
                            for el in elements
                            if el.get("type") == "image" and el.get("duotone")
                        ),
                        0.55,
                    ),
                )
                context["cover_photo"] = str(tmp_duotone)
            else:
                context["cover_photo"] = str(cover_image_path)
        elif cover_image_src:
            # Bron is opgegeven maar bestaat niet op disk (_resolve_image
            # gaf None). We zetten toch het RUWE pad in de context — niet
            # om het te laten renderen (dat gebeurt niet, want het bestand
            # bestaat niet), maar zodat static_elements._resolve_image_src
            # het werkelijk gezochte pad kan noemen in zijn "required:
            # true"-ValueError i.p.v. het generieke "geen waarde ingevuld".
            context["cover_photo"] = str(cover_image_src)

        tenant_dir = None
        tenant_label = ""
        if self._brand_config is not None:
            if self._brand_config.brand_dir:
                tenant_dir = self._brand_config.brand_dir
            tenant_label = (
                getattr(self._brand_config, "tenant", "")
                or getattr(self._brand_config, "slug", "")
            )

        try:
            render_static_elements(
                c, elements,
                page_height_pt=h,
                tenant_dir=tenant_dir,
                context=context,
                block="cover.static_elements",
                tenant=tenant_label,
            )

            # Dynamische titel/subtitel blijven via het bestaande
            # dynamic_fields-mechanisme lopen (title=report_type,
            # subtitle=project) — dat mechanisme was al data-gedreven en
            # hoeft niet gemigreerd te worden.
            fields = self.tpl.get("dynamic_fields", {})
            tf = fields.get("rapport_type", {})
            title_text = data.get("report_type", "")
            title_shift = 0.0
            if title_text:
                color = self._color(tf, "color", "primary", "cover.rapport_type(static)")
                title_shift = self._draw_cover_title(c, tf, title_text, color, A4[0])

            sf = fields.get("project_naam", {})
            subtitle_text = data.get("project", "")
            if subtitle_text:
                c.setFont(sf.get("font", "Inter-Regular"), sf.get("size", 17.8))
                color = self._color(sf, "color", "secondary", "cover.project_naam(static)")
                c.setFillColor(HexColor(color))
                c.drawString(sf.get("x", 55.0), sf.get("y_bl", 78.6) - title_shift, subtitle_text)

            c.save()
        finally:
            if tmp_duotone is not None:
                tmp_duotone.unlink(missing_ok=True)

        return output

    def _generate_from_png(self, data: dict, stationery_png: Path | None, output: Path) -> Path:
        """Legacy ReportLab PNG overlay cover."""
        static_elements = self._static_elements_for_page("cover")
        if static_elements:
            return self._generate_static_cover(data, static_elements, output)

        self.fonts.register_reportlab()
        w, h = A4
        c = rl_canvas.Canvas(str(output), pagesize=A4)

        # Layer 1: Project photo placeholder (or actual image)
        cover_image_src = data.get("cover", {}).get("image")
        cover_image_path = _resolve_image(cover_image_src)
        if cover_image_path:
            from reportlab.lib.utils import ImageReader

            img = ImageReader(str(cover_image_path))
            iw, ih = img.getSize()
            box_w, box_h = 484.0, 560.8
            box_x, box_y = 55.6, 161.7
            scale = max(box_w / iw, box_h / ih)
            draw_w = iw * scale
            draw_h = ih * scale
            draw_x = box_x - (draw_w - box_w) / 2
            draw_y = box_y - (draw_h - box_h) / 2
            c.saveState()
            p = c.beginPath()
            p.rect(box_x, box_y, box_w, box_h)
            c.clipPath(p, stroke=0)
            c.drawImage(img, draw_x, draw_y, width=draw_w, height=draw_h)
            c.restoreState()
        else:
            ph_cfg = self.tpl.get("placeholder", {})
            bg_color = self._color(ph_cfg, "background", "secondary", "cover.placeholder")
            text_color = self._color(ph_cfg, "text_color", "paper", "cover.placeholder.text")
            c.setFillColor(HexColor(bg_color))
            c.rect(55.6, 161.7, 484.0, 560.8, fill=1, stroke=0)
            c.setFillColor(Color(0.15, 0.35, 0.35, alpha=0.4))
            c.rect(55.6, 161.7, 484.0, 280, fill=1, stroke=0)
            c.setFillColor(HexColor(text_color))
            c.setFont("LiberationSans", 16)
            c.drawCentredString(w / 2, 440, ph_cfg.get("label", "[ PROJECTFOTO ]"))

        # Layer 2: Stationery overlay (with alpha channel)
        if stationery_png and stationery_png.exists():
            from reportlab.lib.utils import ImageReader

            img = ImageReader(str(stationery_png))
            c.drawImage(img, 0, 0, width=w, height=h, mask="auto", preserveAspectRatio=False)

        # Layer 3: Dynamic text
        fields = self.tpl.get("dynamic_fields", {})

        tf = fields.get("rapport_type", {})
        title_text = data.get("report_type", "")
        title_shift = 0.0
        if title_text:
            color = self._color(tf, "color", "primary", "cover.rapport_type(png)")
            title_shift = self._draw_cover_title(c, tf, title_text, color, A4[0])

        sf = fields.get("project_naam", {})
        subtitle_text = data.get("project", "")
        if subtitle_text:
            c.setFont(sf.get("font", "Inter-Regular"), sf.get("size", 17.8))
            color = self._color(sf, "color", "secondary", "cover.project_naam(png)")
            c.setFillColor(HexColor(color))
            c.drawString(sf.get("x", 55.0), sf.get("y_bl", 78.6) - title_shift, subtitle_text)

        c.save()
        return output


# ---------------------------------------------------------------------------
# Colofon Generator (PyMuPDF)
# ---------------------------------------------------------------------------
class ColofonGenerator:
    """Generates colofon page by inserting text onto stationery PDF."""

    def __init__(
        self, templates: TemplateSet, fonts: FontManager, brand_config: Any | None = None
    ):
        self.tpl = templates.colofon
        self.fonts = fonts
        self._brand_config = brand_config

    def _color(self, style: dict, key: str, semantic: str, block: str) -> str:
        return _style_color(style, key, semantic, self._brand_config, block=block)

    def generate(
        self, data: dict, stationery_pdf: Path, output: Path, page_number: int = 2
    ) -> Path:
        """Generate colofon PDF."""
        doc = fitz.open(str(stationery_pdf))
        page = doc[0]
        page.clean_contents()  # Normaliseer content stream voor TextWriter
        self.fonts.insert_into_page(page)

        # Pagina-breedte en rechter-margin bepalen max tekstbreedtes
        # (voorkomt dat lange velden over de rechter rand lopen — zie bug
        # "ISSO 51:2023 — Warmteverliesberekening voor woningen en
        # utiliteitsgebo" die visueel afgekapt werd).
        page_w = page.rect.width
        right_margin = 64.0

        # Title + Subtitle via gedeelde helper (voorheen copy-paste blok).
        self._render_dynamic_field(
            page,
            field_key="titel",
            value=data.get("report_type", ""),
            default_x=70.9,
            default_y_td=57.3,
            default_size=22,
            default_font="LiberationSans-Bold",
            color_semantic="primary",
            page_w=page_w,
            right_margin=right_margin,
        )
        self._render_dynamic_field(
            page,
            field_key="subtitel",
            value=data.get("project", ""),
            default_x=70.9,
            default_y_td=86.8,
            default_size=14,
            default_font="LiberationSans",
            color_semantic="secondary",
            page_w=page_w,
            right_margin=right_margin,
        )

        # Table values — map JSON fields to colofon positions
        colofon_data = data.get("colofon", {})
        field_map = self._build_field_map(data, colofon_data)
        table_cfg = self.tpl.get("table", {})
        value_x = table_cfg.get("value_x", 229.1)
        value_size = table_cfg.get("value_size", 10)
        value_font = table_cfg.get("value_font", "LiberationSans")
        value_color = _hex_to_rgb(
            self._color(table_cfg, "value_color", "primary", "colofon.table.value")
        )
        value_max_w = table_cfg.get(
            "value_max_width", max(50.0, page_w - value_x - right_margin)
        )
        value_bold = "Bold" in value_font

        for field_key, y_td in self._get_field_positions():
            text = field_map.get(field_key, "")
            if not text:
                continue
            font_obj = self.fonts.get_fitz_font(value_font)
            # Ondersteun expliciete newlines én auto-wrap bij te lange tekst
            logical_lines: list[str] = []
            for raw_line in text.split("\n"):
                wrapped = self.fonts.wrap_text(
                    raw_line, value_size, value_max_w, bold=value_bold, fontname=value_font
                )
                logical_lines.extend(wrapped if wrapped else [""])
            for i, line in enumerate(logical_lines):
                tw = fitz.TextWriter(page.rect)
                tw.append(
                    (value_x, y_td + i * 12.8 + value_size * 0.8),
                    line, font=font_obj, fontsize=value_size,
                )
                tw.write_text(page, color=value_color)

        # Revision history (optional)
        rev_cfg = self.tpl.get("revision_history", {})
        revisions = colofon_data.get("revision_history", [])
        current_y = rev_cfg.get("y_td", 670.0)
        if revisions:
            # Label
            lbl_font_name = rev_cfg.get("label_font", "LiberationSans-Bold")
            lbl_size = rev_cfg.get("label_size", 10.0)
            lbl_color = _hex_to_rgb(
                self._color(rev_cfg, "label_color", "secondary", "colofon.revision.label")
            )
            lbl_x = rev_cfg.get("label_x", 103.0)
            font_obj = self.fonts.get_fitz_font(lbl_font_name)
            tw = fitz.TextWriter(page.rect)
            tw.append(
                (lbl_x, current_y + lbl_size * 0.8),
                "Revisiehistorie", font=font_obj, fontsize=lbl_size,
            )
            tw.write_text(page, color=lbl_color)
            current_y += lbl_size * 1.8

            # Table
            tbl_x = rev_cfg.get("table_x", 103.0)
            tbl_end_x = rev_cfg.get("table_end_x", 420.0)
            hdr_font_name = rev_cfg.get("header_font", "LiberationSans-Bold")
            hdr_size = rev_cfg.get("header_size", 8.0)
            hdr_color = _hex_to_rgb(
                self._color(rev_cfg, "header_color", "primary", "colofon.revision.header")
            )
            body_font_name = rev_cfg.get("body_font", "LiberationSans")
            body_size = rev_cfg.get("body_size", 8.0)
            body_color = _hex_to_rgb(
                self._color(rev_cfg, "body_color", "text", "colofon.revision.body")
            )
            row_h = rev_cfg.get("row_height", 14.0)

            tbl_width = tbl_end_x - tbl_x
            col_ratios = rev_cfg.get("col_widths", [0.12, 0.18, 0.25, 0.45])
            col_xs = [tbl_x]
            for ratio in col_ratios[:-1]:
                col_xs.append(col_xs[-1] + tbl_width * ratio)

            # Header row
            headers = ["Versie", "Datum", "Auteur", "Omschrijving"]
            hdr_font = self.fonts.get_fitz_font(hdr_font_name)
            for i, header in enumerate(headers):
                tw = fitz.TextWriter(page.rect)
                tw.append(
                    (col_xs[i], current_y + hdr_size * 0.8),
                    header, font=hdr_font, fontsize=hdr_size,
                )
                tw.write_text(page, color=hdr_color)
            current_y += row_h

            # Separator line
            page.draw_line(
                fitz.Point(tbl_x, current_y - row_h * 0.3),
                fitz.Point(tbl_end_x, current_y - row_h * 0.3),
                color=hdr_color, width=0.5,
            )

            # Data rows
            body_font = self.fonts.get_fitz_font(body_font_name)
            for rev in revisions:
                cells = [
                    rev.get("version", ""),
                    rev.get("date", ""),
                    rev.get("author", ""),
                    rev.get("description", ""),
                ]
                for i, cell in enumerate(cells):
                    if cell:
                        tw = fitz.TextWriter(page.rect)
                        tw.append(
                            (col_xs[i], current_y + body_size * 0.8),
                            str(cell), font=body_font, fontsize=body_size,
                        )
                        tw.write_text(page, color=body_color)
                current_y += row_h

        # Disclaimer (optional)
        disclaimer = colofon_data.get("disclaimer", "")
        if disclaimer:
            discl_cfg = self.tpl.get("disclaimer", {})
            discl_x = discl_cfg.get("x", 103.0)
            discl_font_name = discl_cfg.get("font", "LiberationSans-Italic")
            discl_size = discl_cfg.get("size", 7.0)
            discl_color = _hex_to_rgb(
                self._color(discl_cfg, "color", "text_light", "colofon.disclaimer")
            )
            discl_y = current_y + 8.0

            discl_font = self.fonts.get_fitz_font(discl_font_name)
            for i, line in enumerate(disclaimer.split("\n")):
                tw = fitz.TextWriter(page.rect)
                tw.append(
                    (discl_x, discl_y + i * (discl_size * 1.6) + discl_size * 0.8),
                    line.strip(), font=discl_font, fontsize=discl_size,
                )
                tw.write_text(page, color=discl_color)

        # Page number
        pn_cfg = self.tpl.get("page_number", {})
        pn_size = pn_cfg.get("size", 8)
        font_obj = self.fonts.get_fitz_font(
            pn_cfg.get("font", "LiberationSans-Bold")
        )
        tw = fitz.TextWriter(page.rect)
        tw.append(
            (pn_cfg.get("x", 534.0), pn_cfg.get("y_td", 796.3) + pn_size * 0.8),
            str(page_number), font=font_obj, fontsize=pn_size,
        )
        tw.write_text(
            page,
            color=_hex_to_rgb(
                self._color(pn_cfg, "color", "secondary", "colofon.page_number")
            ),
        )

        doc.save(str(output))
        doc.close()
        return output

    def _render_dynamic_field(
        self,
        page: fitz.Page,
        *,
        field_key: str,
        value: str,
        default_x: float,
        default_y_td: float,
        default_size: float,
        default_font: str,
        color_semantic: str,
        page_w: float,
        right_margin: float,
    ) -> None:
        """Render één dynamic field (titel/subtitel) met word-wrap.

        Gedeelde helper voor de vrijwel identieke title- en subtitle
        render-blokken. Leest layout uit ``dynamic_fields[field_key]``
        in het colofon template, wrapt de waarde op basis van de
        beschikbare breedte (``page_w - x - right_margin``) en schrijft
        iedere regel via een aparte ``TextWriter``.

        Args:
            page: De PyMuPDF pagina waar op geschreven wordt.
            field_key: Template-key binnen ``dynamic_fields`` (bijv.
                ``"titel"`` of ``"subtitel"``).
            value: De weer te geven string. Lege strings worden
                overgeslagen.
            default_x: Fallback x-coördinaat als het template geen
                positie definieert.
            default_y_td: Fallback y (top-down) als fallback.
            default_size: Fallback font-size.
            default_font: Fallback font-naam.
            color_semantic: Semantische ``brand.colors``-sleutel om op
                terug te vallen als het template geen ``color`` zet
                (zie ``_style_color``).
            page_w: Breedte van de pagina (voor max-width berekening).
            right_margin: Rechtermarge (voor max-width berekening).
        """
        if not value:
            return
        cfg = self.tpl.get("dynamic_fields", {}).get(field_key, {})
        x = cfg.get("x", default_x)
        size = cfg.get("size", default_size)
        font_name = cfg.get("font", default_font)
        color = _hex_to_rgb(
            self._color(cfg, "color", color_semantic, f"colofon.{field_key}")
        )
        y_base = cfg.get("y_td", default_y_td)
        max_w = max(50.0, page_w - x - right_margin)
        bold = "Bold" in font_name
        font_obj = self.fonts.get_fitz_font(font_name)
        lines = self.fonts.wrap_text(value, size, max_w, bold=bold, fontname=font_name)
        for i, line in enumerate(lines):
            tw = fitz.TextWriter(page.rect)
            tw.append(
                (x, y_base + i * size * 1.15 + size * 0.8),
                line, font=font_obj, fontsize=size,
            )
            tw.write_text(page, color=color)

    def _build_field_map(self, data: dict, colofon: dict) -> dict[str, str]:
        """Build mapping from field keys to display values."""
        pn = data.get("project_number", "")
        pname = data.get("project", "")
        return {
            "project": f"{pn} - {pname}" if pn else pname,
            "opdrachtgever_contact": colofon.get("opdrachtgever_contact", ""),
            "opdrachtgever_naam": colofon.get("opdrachtgever_naam", data.get("client", "")),
            "opdrachtgever_adres": colofon.get("opdrachtgever_adres", ""),
            "adviseur_bedrijf": colofon.get(
                "adviseur_bedrijf", data.get("author", "")
            ),
            "adviseur_naam": colofon.get("adviseur_naam", ""),
            "adviseur_email": colofon.get("adviseur_email", ""),
            "adviseur_telefoon": colofon.get("adviseur_telefoon", ""),
            "adviseur_functie": colofon.get("adviseur_functie", ""),
            "adviseur_registratie": colofon.get("adviseur_registratie", ""),
            "normen": colofon.get("normen", ""),
            "documentgegevens": colofon.get("documentgegevens", ""),
            "datum": colofon.get("datum", data.get("date", "")),
            "fase": colofon.get("fase", ""),
            "status": colofon.get("status_colofon", data.get("status", "CONCEPT")),
            "kenmerk": colofon.get("kenmerk", ""),
        }

    def _get_field_positions(self) -> list[tuple[str, float]]:
        """Return (field_key, y_td) for each colofon table row."""
        rows = self.tpl.get("table", {}).get("rows", [])
        if rows:
            return [(r["key"], r["y_td"]) for r in rows]
        # Fallback: hardcoded from reference
        return [
            ("project", 321.1),
            ("opdrachtgever_naam", 369.1),
            ("opdrachtgever_contact", 381.8),
            ("opdrachtgever_adres", 394.6),
            ("adviseur_bedrijf", 489.1),
            ("adviseur_naam", 501.1),
            ("normen", 525.1),
            ("documentgegevens", 549.1),
            ("datum", 573.1),
            ("fase", 597.1),
            ("status", 621.1),
            ("kenmerk", 645.1),
        ]


# ---------------------------------------------------------------------------
# Content Renderer (PyMuPDF)
# ---------------------------------------------------------------------------
class ContentRenderer:
    """Renders dynamic content pages: TOC, chapters, appendices, backcover.

    Uses PyMuPDF to overlay text on stationery PDF pages with accurate
    Inter font measurements for word-wrapping.
    """

    def __init__(
        self,
        templates: TemplateSet,
        fonts: FontManager,
        stationery: dict[str, Path],
        brand_config=None,
    ):
        self.tpl = templates
        self.fonts = fonts
        self.stationery = stationery  # {"standaard": Path, "bijlagen": Path, "achterblad": Path}
        self.blocks = templates.blocks
        self._brand_config = brand_config

        # Y_max bepalen — bottom-grens voor content in top-down coords.
        # Voorkeursvolgorde:
        #   1. standaard.yaml `content_area.y_td_end` (top-down, semantisch
        #      correct voor renderer_v2 — voorkomt overlap met footer-zone).
        #   2. brand.yaml `stationery.content.content_frame` (legacy
        #      ReportLab-veld, bottom-up; werkt hier alleen via toeval).
        #   3. Hardcoded fallback (Y_MAX_PORTRAIT / Y_MAX_LANDSCAPE).
        content_area = templates.standaard.get("content_area") or {}
        y_td_end = content_area.get("y_td_end")
        if isinstance(y_td_end, (int, float)):
            self._default_y_max_portrait = float(y_td_end)
        elif brand_config and brand_config.stationery:
            content_spec = brand_config.stationery.get("content")
            if content_spec and content_spec.content_frame:
                cf = content_spec.content_frame
                self._default_y_max_portrait = cf.get("y_pt", 38.9) + cf.get("height_pt", 746.0)
            else:
                self._default_y_max_portrait = Y_MAX_PORTRAIT
        else:
            self._default_y_max_portrait = Y_MAX_PORTRAIT

        landscape_area = templates.standaard.get("content_area_landscape") or {}
        y_td_end_landscape = landscape_area.get("y_td_end")
        if isinstance(y_td_end_landscape, (int, float)):
            self._default_y_max_landscape = float(y_td_end_landscape)
        elif brand_config and brand_config.stationery:
            landscape_spec = brand_config.stationery.get("content_landscape")
            if landscape_spec and landscape_spec.content_frame:
                cf = landscape_spec.content_frame
                self._default_y_max_landscape = cf.get("y_pt", 38.9) + cf.get("height_pt", 517.5)
            else:
                self._default_y_max_landscape = Y_MAX_LANDSCAPE
        else:
            self._default_y_max_landscape = Y_MAX_LANDSCAPE

        self.doc = fitz.open()
        self.page: fitz.Page | None = None
        self.y = 0.0
        self.page_count = 0
        self.y_max = self._default_y_max_portrait
        self.current_page_nr = 3  # starts after cover (1) + colofon (2)
        self._orientation: str = "portrait"
        # Idempotency-flag: voorkomt dat dezelfde pagina meermaals een
        # paginanummer krijgt (anders ontstaat overlay zoals "16" + "17"
        # → "167" op de laatste pagina door cleanup-calls).
        self._page_number_written: bool = False
        # Log van werkelijk gerenderde headings → (level, number, title,
        # display_page_nr). Publieke attribuut (leesbaar door
        # ``ReportGeneratorV2._render_content``) zodat de TOC achteraf
        # gebouwd kan worden met correcte paginanummers in plaats van de
        # grove schatting uit ``_build_toc_entries``.
        self.heading_log: list[tuple[int, str, str, int]] = []
        # Cache van geopende stationery PDF documents, key = absoluut
        # pad. Voorkomt dat ``_new_page`` elk keer opnieuw de stationery
        # van disk leest (typisch 15+ reads per rapport).
        self._stationery_doc_cache: dict[str, fitz.Document] = {}
        # Auto-nummering van headings (1, 1.1, 1.2, 2, 2.1, ...). Kan via
        # ``data["toc"]["auto_number"]`` worden uitgezet voor rapporten
        # waarvan de titels zelf al een nummering bevatten (bijv. BBL-
        # toetsingen met "Afd. 4.3 — ..." als titel).
        self._auto_number_enabled: bool = True
        # Statuslabel rechtsboven op elke inhoudspagina ("CONCEPT"); gezet
        # door ReportGeneratorV2 vanuit data["header_label"]. Leeg = geen.
        self.header_label: str | dict = ""
        # Stationery-sleutel van de huidige pagina ("standaard", "bijlagen").
        self._page_template: str = ""
        self._section_counter: int = 0
        self._subsection_counter: int = 0

    def _color(self, style: dict, key: str, semantic: str, block: str) -> str:
        return _style_color(style, key, semantic, self._brand_config, block=block)

    # --- Low level ---

    def _get_stationery_doc(self, pdf_path: Path) -> fitz.Document:
        """Geeft een (gecachte) ``fitz.Document`` voor de stationery terug.

        PyMuPDF's ``insert_pdf`` kopieert pagina's uit de source-doc, dus
        we kunnen dezelfde ``fitz.Document`` meerdere keren hergebruiken
        voor verschillende doelpagina's.
        """
        key = str(pdf_path)
        cached = self._stationery_doc_cache.get(key)
        if cached is not None:
            return cached
        doc = fitz.open(key)
        self._stationery_doc_cache[key] = doc
        return doc

    def _new_page(self, template_key: str = "standaard") -> None:
        """Insert a new page from stationery template.

        Selecteert automatisch de juiste stationery en paginagrootte
        op basis van de huidige oriëntatie (``_orientation``).

        Args:
            template_key: Stationery key (bijv. 'standaard', 'bijlagen').
        """
        # Kies stationery op basis van oriëntatie
        if self._orientation == "landscape" and template_key == "standaard":
            landscape_key = "content_landscape"
            pdf_path = self.stationery.get(landscape_key)
        else:
            pdf_path = self.stationery.get(template_key)

        if pdf_path and pdf_path.exists():
            src = self._get_stationery_doc(pdf_path)
            self.doc.insert_pdf(src)
        elif self._orientation == "landscape":
            self.doc.new_page(
                width=A4_LANDSCAPE_WIDTH, height=A4_LANDSCAPE_HEIGHT
            )
        else:
            self.doc.new_page(
                width=A4_PORTRAIT_WIDTH, height=A4_PORTRAIT_HEIGHT
            )

        self.page_count += 1
        self.page = self.doc[-1]
        self._page_template = template_key
        self.page.clean_contents()  # Normaliseer content stream voor TextWriter
        self.fonts.insert_into_page(self.page)
        # Reset idempotency-flag: nieuwe pagina krijgt een nieuw nummer.
        self._page_number_written = False

        margins = self.tpl.standaard.get("margins", {})
        self.y = margins.get("top", 74.9)
        if self.header_label and template_key == "standaard":
            self._draw_header_label()

        # Y-max instellen op basis van oriëntatie
        if self._orientation == "landscape":
            self.y_max = self._default_y_max_landscape
        else:
            self.y_max = self._default_y_max_portrait

    def _draw_header_label(self) -> None:
        """Statuslabel (chip) rechtsboven in de kop van een inhoudspagina.

        Positie en soort via ``standaard.yaml`` ``header_label`` (``right_x``,
        ``y_td``, ``kind``, ``size``); standaard rechts uitgelijnd op de
        tekstkolom, halverwege de bovenmarge, als fout-label (rood omlijnd).
        """
        value = self.header_label
        text = str(value.get("text", "")) if isinstance(value, dict) else str(value)
        if not text:
            return
        cfg = self.tpl.standaard.get("header_label", {})
        kind = (value.get("kind") if isinstance(value, dict) else None) or cfg.get("kind", "fout")
        base = cfg.get("base_size", 11.0)
        style = self._label_style(kind, base)
        piece = label_piece(self._label_text(kind, text), style)
        line = layout_pieces([piece], piece.width + 1.0)[0]
        p = self.blocks.get("paragraph", {})
        if self._orientation == "landscape":
            margin = self.blocks.get("table", {}).get("right_margin_landscape", 54.0)
            right = cfg.get("right_x_landscape", A4_LANDSCAPE_WIDTH - margin)
        else:
            right = cfg.get("right_x", p.get("x", 125.4) + p.get("max_width", 393.0))
        top = self.tpl.standaard.get("margins", {}).get("top", 74.9)
        y_td = cfg.get("y_td", top / 2 - base * 0.47)
        self._draw_rich_line(line, right - line.width, y_td, base)

    def _check_overflow(self, needed: float) -> bool:
        """Check if content fits; if not, finalize page and start new one."""
        if self.y + needed > self.y_max:
            self._add_page_number()
            self._new_page()
            return True
        return False

    def _display_page_nr(self) -> int:
        """Paginanummer van de huidige pagina.

        ``_add_page_number`` hoogt de teller op zodra het nummer gestempeld
        is; loopt de inhoud daarna op dezelfde pagina door (een sectie zonder
        paginabreuk), dan is het nummer van deze pagina de teller min een.
        """
        if self._page_number_written:
            return self.current_page_nr - 1
        return self.current_page_nr

    def _add_page_number(self) -> None:
        """Add page number to current page (idempotent per page).

        Meerdere aanroepen op dezelfde pagina zijn no-ops. Dit voorkomt
        dat cleanup-code in ``_render_content`` het nummer nogmaals
        schrijft nadat ``render_section`` dit al had gedaan — anders
        ontstaat een overlay (bijv. "16" + "17" → "167").
        """
        if self._page_number_written:
            return
        pn = self.tpl.page_number
        if not pn or not self.page:
            return
        self._text(
            pn["x"], pn["y_td"],
            str(self.current_page_nr),
            pn.get("font", "Inter-Regular"),
            pn["size"],
            pn["color"],
        )
        self.current_page_nr += 1
        self._page_number_written = True

    def _text(
        self,
        x: float,
        y_td: float,
        text: str,
        fontname: str,
        size: float,
        color_hex: str,
    ) -> None:
        """Insert text met embedded font subset via TextWriter."""
        if not self.page or not text:
            return
        font_obj = self.fonts.get_fitz_font(fontname)
        tw = fitz.TextWriter(self.page.rect)
        tw.append((x, y_td + size * 0.8), text, font=font_obj, fontsize=size)
        tw.write_text(self.page, color=_hex_to_rgb(color_hex))

    # --- TOC ---

    def render_toc(self, entries: list[tuple]) -> None:
        """Render table of contents.

        Args:
            entries: List of (level, number, title, page_number) tuples.
        """
        self._new_page()
        toc_cfg = self.tpl.toc

        # Title
        title_cfg = toc_cfg.get("title", {})
        title_color = self._color(title_cfg, "color", "primary", "toc.title")
        self._text(
            title_cfg.get("x", 90.0),
            title_cfg.get("y_td", 74.9),
            title_cfg.get("text", "Inhoud"),
            title_cfg.get("font", "Inter-Regular"),
            title_cfg.get("size", 18.0),
            title_color,
        )

        self.y = toc_cfg.get("entries_start_y", 127.2)

        levels = toc_cfg.get("levels", {})
        lv1 = levels.get("1", {})
        lv2 = levels.get("2", {})
        lv1_color = self._color(lv1, "color", "text_accent", "toc.level1")
        lv2_color = self._color(lv2, "color", "primary", "toc.level2")

        part_cfg = toc_cfg.get("part", {})
        current_part = ""
        for entry in entries:
            level, number, title, pg = entry[:4]
            part = entry[4] if len(entry) > 4 else ""
            reference = entry[5] if len(entry) > 5 else ""
            if level == 1 and part and part != current_part:
                # Deelkop als groepsregel boven de hoofdstukken van dat deel.
                self.y += part_cfg.get("spacing_before", 17.0)
                self._check_overflow(20)
                self._text(
                    part_cfg.get("x", lv1.get("number_x", 90.0)), self.y, part,
                    part_cfg.get("font", lv1.get("font", "Inter-Regular")),
                    part_cfg.get("size", 9.0),
                    self._color(part_cfg, "color", "text_light", "toc.part"),
                )
                self.y += part_cfg.get("spacing_after", 0.0)
            if level == 1:
                current_part = part or current_part
            if reference:
                lv = lv1 if level == 1 else lv2
                ref_cfg = toc_cfg.get("reference", {})
                ref_size = ref_cfg.get("size", round(lv.get("size", 9.5) * 0.8, 2))
                pending_reference = (
                    ref_cfg.get("right_x", lv.get("page_x", 515.4) - ref_cfg.get("gap", 12.0)),
                    (lv.get("size", 9.5) - ref_size) * 0.8, reference,
                    ref_cfg.get("font", lv.get("font", "Inter-Regular")), ref_size,
                    self._color(ref_cfg, "color", "text_light", "toc.reference"),
                )
            else:
                pending_reference = None
            if level == 1:
                self.y += lv1.get("spacing_before", 17.0)
                self._check_overflow(20)
                self._text(
                    lv1.get("number_x", 90.0),
                    self.y,
                    number,
                    lv1.get("font", "Inter-Regular"),
                    lv1.get("size", 12.0),
                    lv1_color,
                )
                self._text(
                    lv1.get("title_x", 160.9),
                    self.y,
                    title,
                    lv1.get("font", "Inter-Regular"),
                    lv1.get("size", 12.0),
                    lv1_color,
                )
                self._text(
                    lv1.get("page_x", 515.4),
                    self.y,
                    str(pg),
                    lv1.get("font", "Inter-Regular"),
                    lv1.get("size", 12.0),
                    lv1_color,
                )
                if pending_reference:
                    self._text_right(
                        pending_reference[0], self.y + pending_reference[1], *pending_reference[2:]
                    )
                self.y += lv1.get("spacing_after", 20.0)
            else:
                self._check_overflow(17.3)
                self._text(
                    lv2.get("number_x", 90.0),
                    self.y,
                    number,
                    lv2.get("font", "Inter-Regular"),
                    lv2.get("size", 9.5),
                    lv2_color,
                )
                self._text(
                    lv2.get("title_x", 160.9),
                    self.y,
                    title,
                    lv2.get("font", "Inter-Regular"),
                    lv2.get("size", 9.5),
                    lv2_color,
                )
                self._text(
                    lv2.get("page_x", 515.4),
                    self.y,
                    str(pg),
                    lv2.get("font", "Inter-Regular"),
                    lv2.get("size", 9.5),
                    lv2_color,
                )
                if pending_reference:
                    self._text_right(
                        pending_reference[0], self.y + pending_reference[1], *pending_reference[2:]
                    )
                self.y += lv2.get("spacing_after", 17.3)

        self._add_page_number()

    def render_toc_to_fresh_doc(
        self,
        entries: list[tuple],
        toc_page_nr: int,
    ) -> fitz.Document:
        """Render de TOC in een gloednieuwe ``fitz.Document``.

        Voor deferred TOC-rendering: na het rendern van alle sections
        bevat ``self._heading_log`` de werkelijke paginanummers; de TOC
        moet dan ergens NA die render-fase gebouwd worden zonder dat de
        pagina tussen de bestaande content-pagina's belandt. Deze
        methode swapt de renderer-state tijdelijk naar een schone doc,
        roept ``render_toc`` aan en herstelt daarna alle mutaties.

        Args:
            entries: TOC entries ``(level, number, title, page_nr)`` met
                werkelijke paginanummers.
            toc_page_nr: Het paginanummer dat op de TOC-pagina zelf
                gedrukt wordt (bijv. 3 bij cover+colofon voorin).

        Returns:
            Nieuwe ``fitz.Document`` met uitsluitend de TOC-pagina(s).
            De caller is verantwoordelijk voor het mergen en sluiten.
        """
        # Alle muteerbare state die ``render_toc`` (via ``_new_page`` en
        # ``_add_page_number``) aanraakt, wordt hier vastgelegd.
        snapshot = {
            "doc": self.doc,
            "page": self.page,
            "y": self.y,
            "page_count": self.page_count,
            "current_page_nr": self.current_page_nr,
            "_page_number_written": self._page_number_written,
            "y_max": self.y_max,
            "_orientation": self._orientation,
        }

        toc_doc = fitz.open()
        self.doc = toc_doc
        self.page = None
        self.y = 0.0
        self.page_count = 0
        self.current_page_nr = toc_page_nr
        self._page_number_written = False
        self._orientation = "portrait"
        self.y_max = self._default_y_max_portrait

        try:
            self.render_toc(entries)
        finally:
            for attr, value in snapshot.items():
                setattr(self, attr, value)

        return toc_doc

    # --- Content blocks ---

    def _heading_title_x(self, style: dict, number: str) -> float:
        """X van de koptitel, opgeschoven als het nummer breder is dan de template aanneemt.

        De template-x van de titel gaat uit van een nummer met enkelcijferige delen
        ("4", "4.1"). Is het nummer breder ("12", "12.10"), dan schuift de titel
        precies het verschil op, zodat nummer en titel niet in elkaar lopen. Met
        ``number_gap`` (pt) in de template geldt in plaats daarvan
        ``max(titel-x, nummer-x + breedte(nummer) + number_gap)``.
        """
        n = style.get("number", {})
        t = style.get("title", {})
        if not number:
            return t["x"]
        font = self.fonts.get_fitz_font(n["font"])
        width = font.text_length(number, fontsize=n["size"])
        gap = style.get("number_gap")
        if gap is not None:
            return max(t["x"], n["x"] + width + float(gap))
        reference = re.sub(r"\d+", "0", number)
        extra = width - font.text_length(reference, fontsize=n["size"])
        return t["x"] + max(0.0, extra)

    def heading_1(
        self, number: str, title: str, part: str = "", reference: str = "",
    ) -> None:
        s = self.blocks.get("heading_1", {})
        n = s.get("number", {})
        t = s.get("title", {})
        part_s = s.get("part", {})
        part_h = (part_s.get("size", 9.0) + part_s.get("spacing_after", 8.0)) if part else 0.0
        self._check_overflow(part_h + n.get("size", 18) + s.get("spacing_after", 33.9))
        # Log AFTER overflow check: current_page_nr reflects the actual
        # page waarop de heading getekend wordt.
        self.heading_log.append(
            (1, number, title, self._display_page_nr(), part or "", reference or "")
        )
        if part:
            self._text(
                part_s.get("x", n["x"]), self.y, part,
                part_s.get("font", t["font"]), part_s.get("size", 9.0),
                self._color(part_s, "color", "text_light", "heading_1.part"),
            )
            self.y += part_h
        self._text(n["x"], self.y, number, n["font"], n["size"], n["color"])
        title_x = self._heading_title_x(s, number)
        self._text(title_x, self.y, title, t["font"], t["size"], t["color"])
        if reference:
            self._heading_reference(s, "heading_1", reference, self.y, t["size"])
        self.y += n["size"] + s.get("spacing_after", 33.9)

    def _heading_reference(
        self, style: dict, block: str, reference: str, y_td: float, title_size: float,
    ) -> None:
        """Verwijzing rechts in de kop, grijs, op de basislijn van de titel."""
        ref_s = style.get("reference", {})
        size = ref_s.get("size", 9.0)
        p = self.blocks.get("paragraph", {})
        right = ref_s.get("right_x", p.get("x", 125.4) + p.get("max_width", 393.0))
        baseline_td = y_td + title_size * 0.8 - size * 0.8
        self._text_right(
            right, baseline_td, reference,
            ref_s.get("font", style.get("title", {}).get("font", "Inter-Regular")), size,
            self._color(ref_s, "color", "text_light", f"{block}.reference"),
        )

    def _text_right(
        self, x_right: float, y_td: float, text: str, fontname: str, size: float, color: str,
    ) -> None:
        """Tekst rechts uitgelijnd op ``x_right``."""
        width = self.fonts.get_fitz_font(fontname).text_length(text, fontsize=size)
        self._text(x_right - width, y_td, text, fontname, size, color)

    def heading_2(self, number: str, title: str, reference: str = "") -> None:
        s = self.blocks.get("heading_2", {})
        n = s.get("number", {})
        t = s.get("title", {})
        spacing_before = s.get("spacing_before", 30.0)
        self._check_overflow(spacing_before + t.get("size", 13) + s.get("spacing_after", 20.5))
        # Log AFTER overflow check zodat het juiste paginanummer wordt
        # vastgelegd (ook als overflow een _new_page heeft getriggerd).
        self.heading_log.append((2, number, title, self._display_page_nr(), "", reference or ""))
        self.y += spacing_before
        self._text(n["x"], self.y, number, n["font"], n["size"], n["color"])
        y_title = self.y - (t["size"] - n["size"]) * 0.3
        self._text(
            self._heading_title_x(s, number), y_title, title, t["font"], t["size"], t["color"]
        )
        if reference:
            self._heading_reference(s, "heading_2", reference, y_title, t["size"])
        self.y += t["size"] + s.get("spacing_after", 20.5)

    # --- Opgemaakte tekst (runs) ---

    # Labelsoort -> (semantische randkleur, tekstkleur, vulkleur); None = geen.
    _LABEL_SEMANTICS: dict[str, tuple[str | None, str, str | None]] = {
        "ntb": ("text_light", "text_light", None),
        "bron": (None, "paper", "secondary"),
        "ok": ("secondary", "primary", None),
        "nvt": ("separator", "text_light", "surface"),
        "fout": ("warning", "warning", None),
    }
    # Nederlandse kleurnamen in runs -> semantische merkkleur.
    _COLOR_ALIASES: dict[str, str] = {
        "grijs": "text_light",
        "rood": "warning",
        "inkt": "text",
        "accent": "text_accent",
    }

    def _run_color(self, value: object, default: str) -> str:
        """Kleur van een run: #RRGGBB, merkkleur (``text_light``) of alias (``grijs``)."""
        if not value:
            return default
        name = str(value).strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", name):
            return name
        name = name.removeprefix("$colors.")
        name = self._COLOR_ALIASES.get(name, name)
        colors = getattr(self._brand_config, "colors", None) or {}
        if name in colors:
            return colors[name]
        logger.warning("Onbekende runkleur '%s'; basiskleur gebruikt", value)
        return default

    def _label_style(self, kind: str, base_size: float) -> LabelStyle:
        """Labelopmaak uit ``blocks.label`` met semantische merkkleuren als basis."""
        cfg = self.blocks.get("label", {})
        if kind not in self._LABEL_SEMANTICS:
            logger.warning("Onbekende labelsoort '%s'; 'ntb' gebruikt", kind)
            kind = "ntb"
        kind_cfg = {**cfg.get("kinds", {}).get(kind, {})}
        border_sem, text_sem, fill_sem = self._LABEL_SEMANTICS[kind]
        block = f"label.{kind}"
        border = (
            self._color(kind_cfg, "border", border_sem, f"{block}.border")
            if border_sem or kind_cfg.get("border") else None
        )
        fill = (
            self._color(kind_cfg, "fill", fill_sem, f"{block}.fill")
            if fill_sem or kind_cfg.get("fill") else None
        )
        text_color = self._color(kind_cfg, "text", text_sem, f"{block}.text")
        compact = kind == "bron"
        default_size = round(base_size * (0.65 if compact else 0.68), 2)
        size = kind_cfg.get("size", cfg.get("size", default_size))
        font_name = kind_cfg.get("font", cfg.get("font"))
        font = (
            self.fonts.get_fitz_font(font_name) if font_name
            else self.fonts.get_variant("", bold=True, bold_fontname=self._font_role("bold"))
        )
        return LabelStyle(
            font=font,
            size=float(size),
            text_color=text_color,
            border_color=border,
            fill_color=fill,
            pad_x=float(kind_cfg.get("pad_x", cfg.get("pad_x", 2.8 if compact else 3.7))),
            pad_y=float(kind_cfg.get("pad_y", cfg.get("pad_y", 0.4 if compact else 0.85))),
            radius=float(kind_cfg.get("radius", cfg.get("radius", 1.7))),
            line_width=float(kind_cfg.get("line_width", cfg.get("line_width", 0.7))),
        )

    def _label_text(self, kind: str, text: str) -> str:
        """Chips in hoofdletters (zoals de BVP-opmaak), bronlabels niet; instelbaar."""
        cfg = self.blocks.get("label", {})
        kind_cfg = cfg.get("kinds", {}).get(kind, {})
        upper = kind_cfg.get("uppercase", cfg.get("uppercase", kind != "bron"))
        return text.upper() if upper else text

    def _font_role(self, role: str) -> str | None:
        """Fontnaam uit ``content_styles.fonts`` (``bold``, ``book_italic``)."""
        return (self.tpl.content_styles.get("fonts") or {}).get(role)

    def _rich_items(
        self, runs: list[dict], fontname: str, size: float, color: str,
    ) -> list[Piece | Space | Break]:
        """Zet runs om in meetbare stukken voor ``layout_pieces``."""
        items: list[Piece | Space | Break] = []
        styles: dict[tuple, TextStyle] = {}
        for run in runs:
            if not isinstance(run, dict):
                run = {"text": str(run)}
            label = run.get("label")
            if label:
                if not isinstance(label, dict):
                    label = {"text": str(label)}
                kind = str(label.get("kind", "ntb"))
                text = self._label_text(kind, str(label.get("text", "")))
                items.append(label_piece(text, self._label_style(kind, size)))
                continue
            bold = bool(run.get("bold"))
            italic = bool(run.get("italic"))
            run_color = self._run_color(run.get("color"), color)
            key = (bold, italic, run_color)
            style = styles.get(key)
            if style is None:
                font = self.fonts.get_variant(
                    fontname, bold=bold, italic=italic,
                    bold_fontname=self._font_role("bold"),
                    italic_fontname=self._font_role("book_italic"),
                )
                style = styles[key] = TextStyle(font, size, run_color)
            items.extend(text_pieces(str(run.get("text", "")), style))
        return items

    def _layout_runs(
        self, runs: list[dict], fontname: str, size: float, color: str, max_width: float,
    ) -> list[Line]:
        return layout_pieces(self._rich_items(runs, fontname, size, color), max_width)

    def _draw_rich_line(self, line: Line, x: float, y_td: float, base_size: float) -> None:
        """Teken een opgemaakte regel; alle stukken delen de basislijn van de basistekst."""
        if not self.page:
            return
        baseline = y_td + base_size * 0.8
        for px, piece in line.pieces:
            if piece.label is not None:
                lab = piece.label
                box_h = lab.size * 1.2 + 2 * lab.pad_y
                center = baseline - base_size * 0.33
                rect = fitz.Rect(
                    x + px, center - box_h / 2, x + px + piece.width, center + box_h / 2
                )
                self.page.draw_rect(
                    rect,
                    color=_hex_to_rgb(lab.border_color) if lab.border_color else None,
                    fill=_hex_to_rgb(lab.fill_color) if lab.fill_color else None,
                    width=lab.line_width if lab.border_color else 0,
                    radius=min(0.5, lab.radius / max(box_h, 0.1)),
                )
                tw = fitz.TextWriter(self.page.rect)
                tw.append(
                    (x + px + lab.pad_x, center + lab.size * 0.35),
                    piece.text, font=lab.font, fontsize=lab.size,
                )
                tw.write_text(self.page, color=_hex_to_rgb(lab.text_color))
                continue
            st = piece.style
            tw = fitz.TextWriter(self.page.rect)
            tw.append((x + px, baseline), piece.text, font=st.font, fontsize=st.size)
            tw.write_text(self.page, color=_hex_to_rgb(st.color))

    def rich_paragraph(self, runs: list[dict]) -> None:
        """Paragraph met runs (vet, cursief, kleur, labels)."""
        s = self.blocks.get("paragraph", {})
        spacing_before = s.get("spacing_before", 12.0)
        lines = self._layout_runs(runs, s["font"], s["size"], s["color"], s["max_width"])
        self._check_overflow(spacing_before + len(lines) * s["line_height"])
        self.y += spacing_before
        for line in lines:
            self._draw_rich_line(line, s["x"], self.y, s["size"])
            self.y += s["line_height"]
        self.y += s.get("spacing_after", 12.0)

    def paragraph(self, text: str) -> None:
        s = self.blocks.get("paragraph", {})
        spacing_before = s.get("spacing_before", 12.0)
        text = _strip_html(text)
        lines = self.fonts.wrap_text(text, s["size"], s["max_width"], fontname=s["font"])
        self._check_overflow(spacing_before + len(lines) * s["line_height"])
        self.y += spacing_before
        for line in lines:
            self._text(s["x"], self.y, line, s["font"], s["size"], s["color"])
            self.y += s["line_height"]
        self.y += s.get("spacing_after", 12.0)

    def bullet_list(self, items: list[str]) -> None:
        sb = self.blocks.get("bullet_list", {})
        marker = sb.get("marker", {})
        text_s = sb.get("text", {})
        for item in items:
            runs = runs_of(item)
            if runs is None and isinstance(item, dict):
                item = str(item.get("text", ""))
            if runs is not None:
                lines = self._layout_runs(
                    runs, text_s["font"], text_s["size"], text_s["color"], text_s["max_width"]
                )
            else:
                item = _strip_html(item)
                lines = self.fonts.wrap_text(
                    item, text_s["size"], text_s["max_width"], fontname=text_s["font"]
                )
            needed = len(lines) * text_s["line_height"] + sb.get("spacing_between", 10.1)
            self._check_overflow(needed)
            # Bullet marker
            self._text(
                marker["x"], self.y,
                "\u2022",
                marker.get("font", "LiberationSans"),
                marker["size"],
                marker["color"],
            )
            for line in lines:
                if runs is not None:
                    self._draw_rich_line(line, text_s["x"], self.y, text_s["size"])
                else:
                    self._text(
                        text_s["x"], self.y, line, text_s["font"], text_s["size"], text_s["color"]
                    )
                self.y += text_s["line_height"]
            self.y += sb.get("spacing_between", 10.1)

    # --- Definitielijst ---

    def definition_list(self, block: dict) -> None:
        """Label-waarde-lijst zonder rasterlijnen: label grijs, waarde in inktkleur.

        ``rows: [{label, value | runs}]``; ``label_width_mm`` zet de breedte
        van de labelkolom. Labels en waarden lopen binnen hun kolom terug.
        """
        p = self.blocks.get("paragraph", {})
        ds = self.blocks.get("definition_list", {})
        x = ds.get("x", p["x"])
        max_w = ds.get("max_width", p["max_width"])
        fontname = ds.get("font", p["font"])
        size = ds.get("size", p["size"])
        line_h = ds.get("line_height", p["line_height"])
        label_color = self._color(ds, "label_color", "text_light", "definition_list.label")
        value_color = self._color(ds, "value_color", "text", "definition_list.value")
        label_w = float(block.get("label_width_mm", ds.get("label_width_mm", 45.0))) * _PT_PER_MM
        gap = ds.get("gap", 8.0)
        row_spacing = ds.get("row_spacing", 3.0)
        value_w = max(max_w - label_w - gap, 20.0)

        self.y += ds.get("spacing_before", p.get("spacing_before", 12.0))
        for row in block.get("rows", []):
            if not isinstance(row, dict):
                continue
            label_lines = self._layout_runs(
                [{"text": str(row.get("label", ""))}], fontname, size, label_color, label_w
            )
            value = row.get("value")
            value_runs = runs_of(row) or [{"text": "" if value is None else str(value)}]
            value_lines = self._layout_runs(value_runs, fontname, size, value_color, value_w)
            row_h = max(len(label_lines), len(value_lines)) * line_h
            self._check_overflow(row_h)
            for li, line in enumerate(label_lines):
                self._draw_rich_line(line, x, self.y + li * line_h, size)
            for li, line in enumerate(value_lines):
                self._draw_rich_line(line, x + label_w + gap, self.y + li * line_h, size)
            self.y += row_h + row_spacing
        self.y += ds.get("spacing_after", p.get("spacing_after", 12.0)) - row_spacing

    # --- Checklist ---

    def checklist(self, block: dict) -> None:
        """Aankruislijst: vak (aangevinkt, leeg, of leeg + n.t.b.) met tekst of runs.

        ``checked: true`` = vinkje, ``false`` = leeg vak, ``null``/ontbrekend =
        leeg vak met een ntb-label achter de tekst. Bij ``columns: 2`` worden
        de items rijsgewijs verdeeld (links, rechts, links, ...).
        """
        p = self.blocks.get("paragraph", {})
        cs = self.blocks.get("checklist", {})
        x = cs.get("x", p["x"])
        max_w = cs.get("max_width", p["max_width"])
        fontname = cs.get("font", p["font"])
        size = cs.get("size", p["size"])
        color = cs.get("color", p["color"])
        line_h = cs.get("line_height", p["line_height"])
        box = cs.get("box_size", 9.0)
        box_line = cs.get("box_line_width", 0.85)
        box_color = self._color(cs, "box_color", "primary", "checklist.box_color")
        check_color = self._color(cs, "check_color", "primary", "checklist.check_color")
        text_gap = cs.get("text_gap", 6.0)
        col_gap = cs.get("column_gap", 14.0)
        row_spacing = cs.get("row_spacing", 4.0)
        unknown = cs.get("unknown_label", "n.t.b.")

        columns = 2 if block.get("columns") == 2 else 1
        col_w = (max_w - col_gap * (columns - 1)) / columns
        text_w = col_w - box - text_gap

        laid_out: list[tuple[object, list[Line]]] = []
        for item in block.get("items", []):
            if not isinstance(item, dict):
                item = {"text": str(item)}
            runs = list(runs_of(item) or [{"text": str(item.get("text", ""))}])
            checked = item.get("checked")
            if checked is None:
                runs += [{"text": " "}, {"label": {"text": unknown, "kind": "ntb"}}]
            laid_out.append((checked, self._layout_runs(runs, fontname, size, color, text_w)))

        self.y += cs.get("spacing_before", p.get("spacing_before", 12.0))
        for start in range(0, len(laid_out), columns):
            row = laid_out[start:start + columns]
            row_h = max(len(lines) for _, lines in row) * line_h
            self._check_overflow(row_h)
            for col, (checked, lines) in enumerate(row):
                cx = x + col * (col_w + col_gap)
                center = self.y + size * 0.8 - size * 0.33
                rect = fitz.Rect(cx, center - box / 2, cx + box, center + box / 2)
                self.page.draw_rect(
                    rect, color=_hex_to_rgb(box_color), fill=(1, 1, 1),
                    width=box_line, radius=0.12,
                )
                if checked:
                    self.page.draw_polyline(
                        [
                            fitz.Point(rect.x0 + box * 0.22, rect.y0 + box * 0.52),
                            fitz.Point(rect.x0 + box * 0.42, rect.y0 + box * 0.72),
                            fitz.Point(rect.x0 + box * 0.78, rect.y0 + box * 0.28),
                        ],
                        color=_hex_to_rgb(check_color), width=box_line * 1.4,
                        lineCap=1, lineJoin=1,
                    )
                for li, line in enumerate(lines):
                    self._draw_rich_line(line, cx + box + text_gap, self.y + li * line_h, size)
            self.y += row_h + row_spacing
        self.y += cs.get("spacing_after", p.get("spacing_after", 12.0)) - row_spacing

    # --- Table ---

    def _cell_spec(
        self, value: object, row: int, col: int, cell_styles: dict,
    ) -> _CellSpec:
        """Normaliseer een tabelcel; ``cell_styles["r,c"]`` vult objectvelden aan."""
        extra = cell_styles.get(f"{row},{col}") if cell_styles else None
        if not isinstance(value, dict) and not extra:
            text, bold = _parse_cell(value)
            return _CellSpec(text, bold)
        obj = dict(extra or {})
        if isinstance(value, dict):
            obj.update(value)
        else:
            text, bold = _parse_cell(value)
            obj.setdefault("text", text)
            if bold:
                obj.setdefault("bold", True)
        runs = runs_of(obj)
        raw_text = obj.get("text")
        text = plain_text(runs) if runs else ("" if raw_text is None else str(raw_text))
        color = obj.get("color") or obj.get("text_color")
        bg = obj.get("bg_color")
        align = obj.get("align", "left")
        return _CellSpec(
            text=text,
            bold=bool(obj.get("bold")),
            italic=bool(obj.get("italic")),
            color=self._run_color(color, "") or None if color else None,
            bg_color=self._run_color(bg, "") or None if bg else None,
            align=align if align in ("left", "center", "right") else "left",
            runs=runs,
            rich=True,
        )

    def _table_group_row(
        self,
        row_specs: list[_CellSpec],
        x: float,
        max_w: float,
        cell_pad: float,
        cell_line_h: float,
        fontname: str,
        fontsize: float,
        group_s: dict,
        grid_color: tuple,
        render_header: Callable[[], None],
        header_h: float,
    ) -> None:
        """Groepsrij: tekst van de eerste gevulde cel over alle kolommen, vet op vlak."""
        spec = next((sp for sp in row_specs if sp.text or sp.runs), _CellSpec("", False))
        bold = group_s.get("bold", True)
        runs = [
            r if "label" in r else {**r, "bold": r.get("bold", bold)}
            for r in (spec.runs or [{"text": spec.text, "italic": spec.italic}])
        ]
        color = spec.color or self._color(group_s, "color", "primary", "table.group.color")
        lines = self._layout_runs(runs, fontname, fontsize, color, max_w - cell_pad * 2)
        row_h = max(len(lines) * cell_line_h + 6, cell_line_h + 6)
        if self._check_overflow(row_h):
            render_header()
            self.y += header_h
        bg = spec.bg_color or self._color(group_s, "background", "surface", "table.group.bg")
        self.page.draw_rect(
            fitz.Rect(x, self.y, x + max_w, self.y + row_h), color=None, fill=_hex_to_rgb(bg)
        )
        for li, line in enumerate(lines):
            self._draw_rich_line(line, x + cell_pad, self.y + 3 + li * cell_line_h, fontsize)
        self.page.draw_line(
            fitz.Point(x, self.y + row_h),
            fitz.Point(x + max_w, self.y + row_h),
            color=grid_color,
            width=0.3,
        )
        self.y += row_h

    def table(self, block: dict) -> None:
        """Render a table block with header and rows.

        Supports:
        - column_widths as proportional values (scaled to available width)
        - auto-fit when column_widths not provided
        - style "striped" for alternating row colors
        - vertical grid lines between columns
        - multi-line text wrapping in cells with dynamic row height
        """
        s = self.blocks.get("table", {})
        header_s = s.get("header", {})
        body_s = s.get("body", {})
        x = s.get("x", 125.4)
        max_w = s.get("max_width", 415.9)

        # In landscape is het portret-``max_width`` (~146mm) een te smal vak:
        # column_widths worden erbinnen geschaald, waardoor brede tabellen per
        # woord afbreken en de laatste kolom buiten beeld valt. Bereken de
        # breedte dan dynamisch uit de paginageometrie. De portret-linkermarge
        # (~125pt) zou in landscape links veel ruimte verspillen; gebruik in
        # plaats daarvan een symmetrische marge (default = portret-rechtermarge
        # 54pt) zodat de tabel breed en gecentreerd staat. Een template mag
        # ``x_landscape`` / ``max_width_landscape`` expliciet overschrijven.
        # Het portret-pad blijft ongemoeid — zo blijft de pixel-baseline gelijk.
        if self._orientation == "landscape":
            margin = s.get("right_margin_landscape", 54.0)
            x = s.get("x_landscape", margin)
            max_w = s.get(
                "max_width_landscape",
                max(50.0, A4_LANDSCAPE_WIDTH - x - margin),
            )

        headers_raw = block.get("headers", [])
        rows_raw = block.get("rows", [])
        raw_widths = block.get("column_widths")
        title = block.get("title", "")
        style = block.get("style", "")
        group_rows = {
            int(rs["row"])
            for rs in block.get("row_styles") or []
            if isinstance(rs, dict) and rs.get("style") == "group" and "row" in rs
        }
        cell_styles = block.get("cell_styles") or {}
        if headers_raw:
            num_cols = len(headers_raw)
        else:
            data_rows = [r for i, r in enumerate(rows_raw) if i not in group_rows]
            num_cols = max(
                (len(r) for r in data_rows), default=len(rows_raw[0]) if rows_raw else 0
            )
        if num_cols == 0:
            return

        # --- Normalize cells: strip HTML, detect inline bold ---
        # Headers zijn altijd bold; we slaan alleen de schoongemaakte tekst op.
        headers: list[str] = [_parse_cell(h)[0] for h in headers_raw]
        # Body cellen krijgen per cel een (text, is_bold) tuple zodat
        # ``<b>...</b>`` markup uit de payload bold gerenderd kan worden
        # zonder dat de tags letterlijk in de PDF terechtkomen.
        # Objectcellen ({runs} of {text, bold, ...}) en cell_styles geven een
        # opgemaakte cel; gewone cellen volgen ongewijzigd het oude pad.
        specs: list[list[_CellSpec]] = [
            [self._cell_spec(cell, r, c, cell_styles) for c, cell in enumerate(row)]
            for r, row in enumerate(rows_raw)
        ]
        # Groepsrijen tellen niet mee voor kolombreedtes (ze overspannen alles).
        rows: list[list[tuple[str, bool]]] = [
            [(sp.text, sp.bold) for sp in row]
            for r, row in enumerate(specs) if r not in group_rows
        ]

        # --- Resolve column widths ---
        # Meten met de fonts waarmee ook getekend wordt.
        h_font_m = header_s.get("font", "Inter-Bold")
        b_font_m = body_s.get("font", "Inter-Regular")
        b_bold_font_m = _derive_bold_fontname(b_font_m, fallback_bold=h_font_m)
        cell_pad = 5  # horizontal padding per side
        if raw_widths and len(raw_widths) >= num_cols:
            total = sum(raw_widths[:num_cols])
            if total > 0:
                col_widths_pt = [(w / total) * max_w for w in raw_widths[:num_cols]]
            else:
                col_widths_pt = [max_w / num_cols] * num_cols
        else:
            # Auto-fit: measure header + sample row data
            header_font_size = header_s.get("size", 9)
            measured = []
            for i in range(num_cols):
                h_text = headers[i] if i < len(headers) else ""
                max_cell_w = self.fonts.measure(
                    h_text, header_font_size, bold=True, fontname=h_font_m
                )
                body_font_size = body_s.get("size", 8)
                for row in rows[:10]:
                    if i < len(row):
                        cell_text, cell_bold = row[i]
                        cell_w = self.fonts.measure(
                            cell_text, body_font_size, bold=cell_bold,
                            fontname=b_bold_font_m if cell_bold else b_font_m,
                        )
                        max_cell_w = max(max_cell_w, cell_w)
                measured.append(max_cell_w + cell_pad * 2)
            total_measured = sum(measured)
            if total_measured > 0:
                col_widths_pt = [(m / total_measured) * max_w for m in measured]
            else:
                col_widths_pt = [max_w / num_cols] * num_cols

        # --- Enforce minimum column widths ---
        # Prevent narrow columns from becoming too small to display their
        # widest single word/token, which causes unwanted line breaks.
        body_font_size = body_s.get("size", 8)
        header_font_size = header_s.get("size", 9)
        min_widths = []
        for i in range(num_cols):
            # Widest single token in header
            h_text = headers[i] if i < len(headers) else ""
            h_tokens = h_text.split() or [""]
            min_w = max(
                self.fonts.measure(t, header_font_size, bold=True, fontname=h_font_m)
                for t in h_tokens
            )
            # Widest single token in body rows
            for row in rows:
                cell_text, cell_bold = row[i] if i < len(row) else ("", False)
                tokens = cell_text.split() or [""]
                token_max = max(
                    self.fonts.measure(
                        t, body_font_size, bold=cell_bold,
                        fontname=b_bold_font_m if cell_bold else b_font_m,
                    )
                    for t in tokens
                )
                min_w = max(min_w, token_max)
            min_widths.append(min_w + cell_pad * 2)

        # Redistribute: bump undersized columns, shrink oversized ones.
        # Herhaal zolang het krimpen een flexibele kolom onder zijn minimum
        # duwt (anders breekt een woord midden af, bijv. "Verlee|nd"). De
        # eerste ronde is gelijk aan de oude eenmalige herverdeling.
        locked = [col_widths_pt[i] < min_widths[i] for i in range(num_cols)]
        for _ in range(num_cols):
            deficit = sum(
                min_widths[i] - col_widths_pt[i]
                for i in range(num_cols)
                if locked[i] and col_widths_pt[i] < min_widths[i]
            )
            flexible_width = sum(
                col_widths_pt[i] for i in range(num_cols) if not locked[i]
            )
            if deficit <= 0 or flexible_width <= 0:
                break
            shrink_factor = max((flexible_width - deficit) / flexible_width, 0.5)
            for i in range(num_cols):
                if locked[i]:
                    col_widths_pt[i] = max(col_widths_pt[i], min_widths[i])
                else:
                    col_widths_pt[i] *= shrink_factor
            newly = [
                i for i in range(num_cols)
                if not locked[i] and col_widths_pt[i] < min_widths[i]
            ]
            if not newly or shrink_factor == 0.5:
                break
            for i in newly:
                locked[i] = True

        h_fontname = header_s.get("font", "Inter-Bold")
        h_fontsize = header_s.get("size", 9)
        h_color = _hex_to_rgb(self._color(header_s, "color", "paper", "table.header.text"))
        b_fontname = body_s.get("font", "Inter-Regular")
        b_fontsize = body_s.get("size", 8)
        b_color = _hex_to_rgb(self._color(body_s, "color", "primary", "table.body.text"))
        bg_color = _hex_to_rgb(
            self._color(header_s, "background", "text_accent", "table.header.background")
        )
        stripe_color = _hex_to_rgb(self._color(s, "stripe_color", "surface", "table.stripe"))
        grid_color = _hex_to_rgb(self._color(s, "grid_color", "separator", "table.grid"))
        line_color_hdr = _hex_to_rgb(
            self._color(s, "header_grid_color", "paper", "table.header_grid")
        )
        cell_line_h = b_fontsize * 1.35  # line height within cells

        # Pre-wrap all header cells
        header_wrapped: list[list[str]] = []
        for i, h in enumerate(headers):
            w = col_widths_pt[i] if i < len(col_widths_pt) else col_widths_pt[-1]
            lines = self.fonts.wrap_text(
                str(h), h_fontsize, w - cell_pad * 2, bold=True, fontname=h_fontname
            )
            header_wrapped.append(lines if lines else [""])
        header_max_lines = max((len(lines) for lines in header_wrapped), default=1)
        header_h = max(header_max_lines * (h_fontsize * 1.35) + 8, 20.0)
        # Zonder "headers"-sleutel geen kopregel; een expliciete lege lijst
        # houdt het oude gedrag (lege kopband) zodat bestaande invoer gelijk blijft.
        has_header = bool(headers_raw) or "headers" in block
        if not has_header:
            header_h = 0.0

        spacing_before = s.get("spacing_before", 20.0)

        # Title above table
        if title:
            title_s = s.get("title", {})
            self._check_overflow(spacing_before + 16 + header_h)
            self.y += spacing_before
            self._text(
                x, self.y, title,
                title_s.get("font", "Inter-Bold"),
                title_s.get("size", 9.5),
                self._color(title_s, "color", "primary", "table.title"),
            )
            self.y += 16
        else:
            self._check_overflow(spacing_before + header_h + cell_line_h + 8)
            self.y += spacing_before

        self._check_overflow(header_h + cell_line_h + 8)  # at least header + 1 row line

        # --- Render header helper ---
        def render_header():
            if not has_header:
                # Zonder kopregel sluit een bovenlijn de tabel af (ook na paginabreuk).
                self.page.draw_line(
                    fitz.Point(x, self.y), fitz.Point(x + max_w, self.y),
                    color=grid_color, width=0.5,
                )
                return
            header_rect = fitz.Rect(x, self.y, x + max_w, self.y + header_h)
            self.page.draw_rect(header_rect, color=None, fill=bg_color)
            cx = x
            for i, lines in enumerate(header_wrapped):
                w = col_widths_pt[i] if i < len(col_widths_pt) else col_widths_pt[-1]
                # Vertically center the wrapped text block
                text_block_h = len(lines) * (h_fontsize * 1.35)
                y_start = self.y + (header_h - text_block_h) / 2
                for li, line in enumerate(lines):
                    font_obj = self.fonts.get_fitz_font(h_fontname)
                    tw = fitz.TextWriter(self.page.rect)
                    tw.append(
                        (cx + cell_pad, y_start + h_fontsize * 0.8 + li * (h_fontsize * 1.35)),
                        line, font=font_obj, fontsize=h_fontsize,
                    )
                    tw.write_text(self.page, color=h_color)
                cx += w
            # Vertical grid lines in header
            cx = x
            for i in range(num_cols - 1):
                cx += col_widths_pt[i]
                self.page.draw_line(
                    fitz.Point(cx, self.y),
                    fitz.Point(cx, self.y + header_h),
                    color=line_color_hdr,
                    width=0.5,
                )

        render_header()
        self.y += header_h

        # Bold body font name (via centrale helper). Valt terug op de
        # header font wanneer die niet direct afgeleid kan worden.
        b_bold_fontname = _derive_bold_fontname(b_fontname, fallback_bold=h_fontname)
        b_color_hex = self._color(body_s, "color", "primary", "table.body.text")
        group_s = s.get("group", {})

        # --- Body rows ---
        for row_idx, row_specs in enumerate(specs):
            if row_idx in group_rows:
                self._table_group_row(
                    row_specs, x, max_w, cell_pad, cell_line_h, b_fontname, b_fontsize,
                    group_s, grid_color, render_header, header_h,
                )
                continue
            # Pre-wrap all cells to determine row height. Gewone cellen als
            # (text, is_bold)-regels, opgemaakte cellen als rich_text-regels.
            row_wrapped: list[list] = []
            for i in range(num_cols):
                spec = row_specs[i] if i < len(row_specs) else _CellSpec("", False)
                w = col_widths_pt[i] if i < len(col_widths_pt) else col_widths_pt[-1]
                if spec.rich:
                    row_wrapped.append(self._layout_runs(
                        spec.as_runs(), b_fontname, b_fontsize,
                        spec.color or b_color_hex, w - cell_pad * 2,
                    ))
                    continue
                lines = self.fonts.wrap_text(
                    spec.text, b_fontsize, w - cell_pad * 2, bold=spec.bold,
                    fontname=b_bold_fontname if spec.bold else b_fontname,
                )
                wrapped_lines = lines if lines else [""]
                row_wrapped.append([(line, spec.bold) for line in wrapped_lines])
            max_lines = max(len(lines) for lines in row_wrapped)
            row_h = max(max_lines * cell_line_h + 6, cell_line_h + 6)

            if self._check_overflow(row_h):
                # Re-render header on new page
                render_header()
                self.y += header_h

            # Striped background
            if style == "striped" and row_idx % 2 == 1:
                row_rect = fitz.Rect(x, self.y, x + max_w, self.y + row_h)
                self.page.draw_rect(row_rect, color=None, fill=stripe_color)

            # Cell text (wrapped)
            cx = x
            for i, lines in enumerate(row_wrapped):
                w = col_widths_pt[i] if i < len(col_widths_pt) else col_widths_pt[-1]
                spec = row_specs[i] if i < len(row_specs) else _CellSpec("", False)
                y_text = self.y + 3  # top padding
                if spec.bg_color:
                    self.page.draw_rect(
                        fitz.Rect(cx, self.y, cx + w, self.y + row_h),
                        color=None, fill=_hex_to_rgb(spec.bg_color),
                    )
                if spec.rich:
                    inner_w = w - cell_pad * 2
                    for li, line in enumerate(lines):
                        offset = 0.0
                        if spec.align == "center":
                            offset = (inner_w - line.width) / 2
                        elif spec.align == "right":
                            offset = inner_w - line.width
                        self._draw_rich_line(
                            line, cx + cell_pad + offset, y_text + li * cell_line_h, b_fontsize
                        )
                    cx += w
                    continue
                for li, (line, line_bold) in enumerate(lines):
                    font_name = b_bold_fontname if line_bold else b_fontname
                    font_obj = self.fonts.get_fitz_font(font_name)
                    tw = fitz.TextWriter(self.page.rect)
                    tw.append(
                        (cx + cell_pad, y_text + b_fontsize * 0.8 + li * cell_line_h),
                        line, font=font_obj, fontsize=b_fontsize,
                    )
                    tw.write_text(self.page, color=b_color)
                cx += w

            # Vertical grid lines between columns
            cx = x
            for i in range(num_cols - 1):
                cx += col_widths_pt[i]
                self.page.draw_line(
                    fitz.Point(cx, self.y),
                    fitz.Point(cx, self.y + row_h),
                    color=grid_color,
                    width=0.3,
                )

            # Bottom border
            self.page.draw_line(
                fitz.Point(x, self.y + row_h),
                fitz.Point(x + max_w, self.y + row_h),
                color=grid_color,
                width=0.3,
            )

            self.y += row_h

        # Final bottom border
        self.page.draw_line(
            fitz.Point(x, self.y),
            fitz.Point(x + max_w, self.y),
            color=grid_color,
            width=0.5,
        )

        self.y += s.get("spacing_after", 20.0)

    # --- Image ---

    def image(self, block: dict) -> None:
        """Render an image block with optional caption."""
        s = self.blocks.get("paragraph", {})  # use paragraph x/max_width
        img_s = self.blocks.get("image", {})
        caption_s = img_s.get("caption", {})
        error_s = img_s.get("error", {})
        x = s.get("x", 125.4)
        max_w = s.get("max_width", 393.0)

        src = block.get("src", "")
        caption = block.get("caption", "")
        width_mm = block.get("width_mm")

        # Resolve image source
        img_path = self._resolve_image(src)
        if not img_path:
            # Placeholder text
            self.y += 12
            self._text(
                x, self.y, f"[Image: {src or 'niet gevonden'}]",
                error_s.get("font", "Inter-Regular"),
                error_s.get("size", 9.5),
                self._color(error_s, "color", "warning", "image.error"),
            )
            self.y += 16
            return

        # Calculate dimensions
        target_w = (width_mm * 2.8346) if width_mm else max_w
        target_w = min(target_w, max_w)

        # SVG als vector: omzetten naar een PDF-pagina en die plaatsen.
        svg_doc: fitz.Document | None = None
        if _is_svg(img_path):
            try:
                with fitz.open(str(img_path), filetype="svg") as svg:
                    svg_doc = fitz.open("pdf", svg.convert_to_pdf())
                svg_rect = svg_doc[0].rect
                aspect = svg_rect.height / svg_rect.width
            except Exception as e:  # noqa: BLE001 - MuPDF-fouttypes verschillen per versie
                label = src if isinstance(src, str) else img_path.name
                logger.error("SVG kon niet worden geplaatst: %s (%s)", label, e)
                self.y += 12
                self._text(
                    x, self.y, f"[SVG kon niet worden geplaatst: {Path(label).name}]",
                    error_s.get("font", "Inter-Regular"),
                    error_s.get("size", 9.5),
                    self._color(error_s, "color", "warning", "image.error"),
                )
                self.y += 16
                return
        else:
            # Get image aspect ratio
            try:
                from PIL import Image as PILImage

                with PILImage.open(img_path) as im:
                    iw, ih = im.size
                aspect = ih / iw
            except (ImportError, OSError, ZeroDivisionError):
                aspect = 0.75  # fallback 4:3

        target_h = target_w * aspect
        max_h = self.y_max - self.y - 30  # leave room for caption
        if target_h > max_h and max_h > 50:
            target_h = max_h
            target_w = target_h / aspect

        self._check_overflow(target_h + 20)

        self.y += 8
        rect = fitz.Rect(x, self.y, x + target_w, self.y + target_h)
        try:
            if svg_doc is not None:
                try:
                    self.page.show_pdf_page(rect, svg_doc, 0)
                finally:
                    svg_doc.close()
            else:
                self.page.insert_image(rect, filename=str(img_path))
        except (OSError, ValueError, RuntimeError) as e:
            logger.warning("Image insert failed: %s", e)
            self._text(
                x, self.y, f"[Image error: {e}]",
                error_s.get("font", "Inter-Regular"),
                error_s.get("size", 9.5),
                self._color(error_s, "color", "warning", "image.error"),
            )

        self.y += target_h + 4

        if caption:
            self._text(
                x, self.y, caption,
                caption_s.get("font", "Inter-Regular"),
                caption_s.get("size", 8.0),
                self._color(caption_s, "color", "primary", "image.caption"),
            )
            self.y += 14

        self.y += 8

    def _resolve_image(self, src) -> Path | None:
        """Resolve image source: file path or base64 dict."""
        return _resolve_image(src)

    # --- Map ---

    def map_block(self, block: dict) -> None:
        """Render map images from PDOK services with POI marker.

        Accepts:
            address: Dutch address string (geocoded via PDOK)
            center: {lat, lon} coordinates (used if no address)
            zoom: Zoom level (default 16, ~1:5000)
            layers: List of layer keys: "brt", "brt_grijs", "luchtfoto", "kadastraal"
            width_mm: Image width in report (default: full content width)
            height_mm: Image height in report (default: auto from aspect)
            cadastral: Optional dict with kadastrale gegevens:
                - identificatie: "LDN03-H-8575"
                - gemeentecode: "LDN03"
                - gemeentenaam: "Loosduinen"
                - sectie: "H"
                - perceelnummer: 8575
                - grootte: area in m²
        """
        from openaec_reports.core.map_generator import MapGenerator

        s = self.blocks.get("paragraph", {})
        map_s = self.blocks.get("map", {})
        caption_s = map_s.get("caption", {})
        error_s = map_s.get("error", {})
        x = s.get("x", 125.4)
        max_w = s.get("max_width", 393.0)

        address = block.get("address", "")
        center = block.get("center", {})
        lat = center.get("lat")
        lon = center.get("lon")
        zoom = block.get("zoom", 16)
        layers = block.get("layers", ["brt"])
        width_mm = block.get("width_mm")
        height_mm = block.get("height_mm")
        caption = block.get("caption", "")
        cadastral = block.get("cadastral")

        # Normalize layer names
        layer_map = {
            "topografie": "brt",
            "topo": "brt",
            "standaard": "brt",
            "grijs": "brt_grijs",
            "luchtfoto": "luchtfoto",
            "satellite": "luchtfoto",
            "aerial": "luchtfoto",
            "kadastraal": "kadastraal",
            "kadaster": "kadastraal",
            "cadastral": "kadastraal",
        }
        normalized_layers = [layer_map.get(layer.lower(), layer.lower()) for layer in layers]

        # Calculate image dimensions in points
        target_w = (width_mm * 2.8346) if width_mm else max_w
        target_w = min(target_w, max_w)
        if height_mm:
            target_h = height_mm * 2.8346
        else:
            target_h = target_w * 0.667  # 3:2

        # WMS pixel dimensions (higher res for print quality)
        px_w = int(target_w * 2.5)
        px_h = int(target_h * 2.5)

        try:
            gen = MapGenerator(timeout=20)

            if address:
                maps = gen.generate_maps(
                    address,
                    layers=normalized_layers,
                    zoom=zoom,
                    width_px=px_w,
                    height_px=px_h,
                    show_poi=True,
                )
            elif lat is not None and lon is not None:
                maps = gen.generate_maps_from_coords(
                    lat,
                    lon,
                    layers=normalized_layers,
                    zoom=zoom,
                    width_px=px_w,
                    height_px=px_h,
                    show_poi=True,
                )
            else:
                self.y += 12
                self._text(
                    x,
                    self.y,
                    "[Kaart: geen adres of coördinaten opgegeven]",
                    error_s.get("font", "Inter-Regular"),
                    error_s.get("size", 9.5),
                    self._color(error_s, "color", "warning", "map.error"),
                )
                self.y += 20
                return

            if not maps:
                self.y += 12
                loc_str = address or f"{lat}, {lon}"
                self._text(
                    x,
                    self.y,
                    f"[Kaart kon niet worden opgehaald voor: {loc_str}]",
                    error_s.get("font", "Inter-Regular"),
                    error_s.get("size", 9.5),
                    self._color(error_s, "color", "warning", "map.error"),
                )
                self.y += 20
                return

            # Render each map layer as an image
            for map_result in maps:
                img_path = map_result["path"]
                map_caption = caption or map_result.get("caption", "")

                needed = target_h + 24
                self._check_overflow(needed)

                self.y += 8
                rect = fitz.Rect(x, self.y, x + target_w, self.y + target_h)
                try:
                    self.page.insert_image(rect, filename=str(img_path))
                except (OSError, ValueError, RuntimeError) as e:
                    logger.warning("Map image insert failed: %s", e)
                    self._text(
                        x, self.y + 10, f"[Kaart fout: {e}]",
                        error_s.get("font", "Inter-Regular"),
                        error_s.get("size", 9.5),
                        self._color(error_s, "color", "warning", "map.error"),
                    )
                self.y += target_h + 4

                if map_caption:
                    self._text(
                        x, self.y, map_caption,
                        caption_s.get("font", "Inter-Regular"),
                        caption_s.get("size", 8.0),
                        self._color(caption_s, "color", "primary", "map.caption"),
                    )
                    self.y += 14

                self.y += 8

                try:
                    img_path.unlink(missing_ok=True)
                except OSError:
                    pass

            # Render cadastral info below all maps
            if cadastral:
                self._render_cadastral_info(x, max_w, cadastral)

        except ImportError:
            logger.warning("Map generator dependencies not available")
            self.y += 12
            self._text(
                x,
                self.y,
                "[Kaartmodule niet beschikbaar — PIL/requests ontbreekt]",
                error_s.get("font", "Inter-Regular"),
                error_s.get("size", 9.5),
                self._color(error_s, "color", "warning", "map.error"),
            )
            self.y += 20
        except (OSError, ValueError, RuntimeError) as e:
            logger.error("Map generation failed: %s", e)
            self.y += 12
            self._text(
                x, self.y, f"[Kaart fout: {e}]",
                error_s.get("font", "Inter-Regular"),
                error_s.get("size", 9.5),
                self._color(error_s, "color", "warning", "map.error"),
            )
            self.y += 20

    def _render_cadastral_info(self, x: float, max_w: float, cadastral: dict) -> None:
        """Render cadastral parcel information below map images.

        Renders a styled info box with:
        - Kadastraal perceel: GEM-SECTIE-NUMMER
        - Gemeente: naam (code)
        - Oppervlakte: xxx m²
        """
        cad_s = self.blocks.get("cadastral", {})
        title_s = cad_s.get("title", {})
        body_s = cad_s.get("body", {})

        identificatie = cadastral.get("identificatie", "")
        gemeentenaam = cadastral.get("gemeentenaam", "")
        gemeentecode = cadastral.get("gemeentecode", "")
        grootte = cadastral.get("grootte", 0)

        if not identificatie:
            return

        # Calculate block height
        lines = 1  # perceel ID
        if gemeentenaam:
            lines += 1
        if grootte:
            lines += 1
        block_h = lines * 14 + 12  # line height + padding

        self._check_overflow(block_h + 8)

        # Light background box
        bg_color = _hex_to_rgb(
            self._color(cad_s, "background", "surface", "cadastral.background")
        )
        bg_rect = fitz.Rect(x, self.y, x + max_w, self.y + block_h)
        self.page.draw_rect(bg_rect, color=None, fill=bg_color)
        # Left accent bar
        accent_color = _hex_to_rgb(
            self._color(cad_s, "accent_color", "text_accent", "cadastral.accent")
        )
        accent_rect = fitz.Rect(x, self.y, x + 3, self.y + block_h)
        self.page.draw_rect(accent_rect, color=None, fill=accent_color)

        inner_x = x + 10
        y_line = self.y + 4

        # Perceel identification (bold)
        self._text(
            inner_x, y_line, f"Kadastraal perceel:  {identificatie}",
            title_s.get("font", "Inter-Bold"),
            title_s.get("size", 8.5),
            self._color(title_s, "color", "primary", "cadastral.title"),
        )
        y_line += 14

        cad_body_color = self._color(body_s, "color", "primary", "cadastral.body")

        # Gemeente
        if gemeentenaam:
            gem_str = f"Kadastrale gemeente:  {gemeentenaam}"
            if gemeentecode:
                gem_str += f" ({gemeentecode})"
            self._text(
                inner_x, y_line, gem_str,
                body_s.get("font", "Inter-Regular"),
                body_s.get("size", 8.5),
                cad_body_color,
            )
            y_line += 14

        # Oppervlakte
        if grootte:
            opp_str = f"Perceeloppervlakte:  {grootte:,.0f} m²".replace(",", ".")
            self._text(
                inner_x, y_line, opp_str,
                body_s.get("font", "Inter-Regular"),
                body_s.get("size", 8.5),
                cad_body_color,
            )
            y_line += 14

        self.y += block_h + 8

    # --- Spacer ---

    def spacer(self, block: dict) -> None:
        """Add vertical space."""
        height_mm = block.get("height_mm", 10)
        height_pt = height_mm * 2.8346
        self._check_overflow(height_pt)
        self.y += height_pt

    # --- Page break ---

    def page_break(self) -> None:
        """Force a page break."""
        self._add_page_number()
        self._new_page()

    # --- Calculation block ---

    def calculation(self, block: dict) -> None:
        """Render engineering calculation block with formula/result."""
        calc_s = self.blocks.get("calculation", {})
        title_s = calc_s.get("title", {})
        body_s = calc_s.get("body", {})
        result_s = calc_s.get("result", {})
        ref_s = calc_s.get("reference", {})

        title = block.get("title", "")
        formula = block.get("formula", "")
        substitution = block.get("substitution", "")
        result = block.get("result", "")
        unit = block.get("unit", "")
        reference = block.get("reference", "")

        x = self.blocks.get("paragraph", {}).get("x", 125.4)
        max_w = self.blocks.get("paragraph", {}).get("max_width", 393.0)

        # Calculate needed height
        lines_needed = 1  # title
        if formula:
            lines_needed += 1
        if substitution:
            lines_needed += 1
        lines_needed += 1  # result
        if reference:
            lines_needed += 1
        block_h = lines_needed * 16 + 16  # padding
        spacing_before = 12

        self._check_overflow(spacing_before + block_h)
        self.y += spacing_before

        # Background rect
        bg_color = _hex_to_rgb(
            self._color(calc_s, "background", "surface", "calculation.background")
        )
        bg_rect = fitz.Rect(x - 4, self.y - 2, x + max_w + 4, self.y + block_h - 8)
        self.page.draw_rect(bg_rect, color=None, fill=bg_color)

        # Title
        self._text(
            x, self.y, title,
            title_s.get("font", "Inter-Bold"),
            title_s.get("size", 9.5),
            self._color(title_s, "color", "primary", "calculation.title"),
        )
        self.y += 16

        calc_body_color = self._color(body_s, "color", "primary", "calculation.body")

        # Formula
        if formula:
            self._text(
                x + 8, self.y, formula,
                body_s.get("font", "Inter-Regular"),
                body_s.get("size", 9.5),
                calc_body_color,
            )
            self.y += 16

        # Substitution
        if substitution:
            self._text(
                x + 8, self.y, substitution,
                body_s.get("font", "Inter-Regular"),
                body_s.get("size", 9.5),
                calc_body_color,
            )
            self.y += 16

        # Result
        result_text = f"{result} {unit}".strip()
        self._text(
            x + 8, self.y, result_text,
            result_s.get("font", "Inter-Bold"),
            result_s.get("size", 9.5),
            self._color(result_s, "color", "primary", "calculation.result"),
        )
        self.y += 16

        # Reference
        if reference:
            self._text(
                x + 8, self.y, f"Ref: {reference}",
                ref_s.get("font", "Inter-Regular"),
                ref_s.get("size", 8.0),
                self._color(ref_s, "color", "text_accent", "calculation.reference"),
            )
            self.y += 14

        self.y += 12

    # --- Check block ---

    def check(self, block: dict) -> None:
        """Render engineering check block (voldoet/voldoet niet)."""
        chk_s = self.blocks.get("check", {})
        title_s = chk_s.get("title", {})
        body_s = chk_s.get("body", {})
        result_s = chk_s.get("result", {})

        description = block.get("description", "")
        required = block.get("required_value", "")
        calculated = block.get("calculated_value", "")
        unity = block.get("unity_check", 0)
        result = block.get("result", "VOLDOET")

        x = self.blocks.get("paragraph", {}).get("x", 125.4)
        max_w = self.blocks.get("paragraph", {}).get("max_width", 393.0)

        block_h = 80
        spacing_before = 12
        self._check_overflow(spacing_before + block_h)
        self.y += spacing_before

        # Background rect
        bg_color = _hex_to_rgb(
            self._color(chk_s, "background", "surface", "check.background")
        )
        bg_rect = fitz.Rect(x - 4, self.y - 2, x + max_w + 4, self.y + block_h - 16)
        self.page.draw_rect(bg_rect, color=None, fill=bg_color)

        chk_body_color = self._color(body_s, "color", "primary", "check.body")

        # Description
        self._text(
            x, self.y, description,
            title_s.get("font", "Inter-Bold"),
            title_s.get("size", 9.5),
            self._color(title_s, "color", "primary", "check.title"),
        )
        self.y += 16

        # Required vs calculated
        self._text(
            x + 8, self.y, f"Vereist: {required}",
            body_s.get("font", "Inter-Regular"),
            body_s.get("size", 9.5),
            chk_body_color,
        )
        self.y += 14
        self._text(
            x + 8, self.y, f"Berekend: {calculated}",
            body_s.get("font", "Inter-Regular"),
            body_s.get("size", 9.5),
            chk_body_color,
        )
        self.y += 14

        # Unity check
        uc_text = f"Unity check: {unity:.2f}"
        self._text(
            x + 8, self.y, uc_text,
            body_s.get("font", "Inter-Regular"),
            body_s.get("size", 9.5),
            chk_body_color,
        )

        # Result indicator
        is_ok = result.upper() == "VOLDOET"
        ok = self._color(chk_s, "ok_color", "text_accent", "check.ok_color")
        fail = self._color(chk_s, "fail_color", "warning", "check.fail_color")
        result_color = ok if is_ok else fail
        self._text(
            x + 200, self.y, result,
            result_s.get("font", "Inter-Bold"),
            result_s.get("size", 9.5),
            result_color,
        )
        self.y += 20

    # --- Section rendering ---

    def _next_auto_number(self, level: int) -> str:
        """Lever het volgende automatische heading-nummer voor ``level``.

        Houdt counters bij voor level-1 (``1``, ``2``, ...) en level-2
        (``1.1``, ``1.2``, ``2.1``, ...). Een nieuwe level-1 reset de
        level-2 counter. Levels >= 3 krijgen (voorlopig) geen nummer.
        """
        if level == 1:
            self._section_counter += 1
            self._subsection_counter = 0
            return str(self._section_counter)
        if level == 2:
            self._subsection_counter += 1
            return f"{self._section_counter}.{self._subsection_counter}"
        return ""

    def _resolve_heading_number(self, level: int, explicit: str) -> str:
        """Kies tussen expliciet nummer en auto-gegenereerd.

        Expliciete nummers blijven altijd leidend. Als er geen nummer
        is en ``_auto_number_enabled`` aan staat, wordt een automatisch
        nummer toegewezen — anders blijft het leeg.
        """
        if explicit:
            return explicit
        if self._auto_number_enabled:
            return self._next_auto_number(level)
        return ""

    def render_section(self, section: dict) -> None:
        """Render a single section (chapter) with its content blocks.

        Ondersteunt per-sectie oriëntatie via ``section["orientation"]``.
        Bij een oriëntatiewisseling wordt een nieuwe pagina gestart met
        de juiste paginagrootte en stationery. Na de sectie wordt de
        vorige oriëntatie hersteld.
        """
        level = section.get("level", 1)
        title = section.get("title", "")
        explicit_number = section.get("number") or ""

        # Per-sectie orientation switch
        section_orientation = section.get("orientation", self._orientation)
        previous_orientation = self._orientation
        orientation_changed = section_orientation != self._orientation

        if orientation_changed:
            # Finaliseer huidige pagina voor de wissel
            if self.page is not None:
                self._add_page_number()
            self._orientation = section_orientation

        # Level 1 begint altijd op een nieuwe pagina (page_break_before: false
        # verandert dat niet: clients sturen dat veld standaard mee).
        # continue_on_page: true laat een level-1-hoofdstuk doorlopen op de
        # huidige inhoudspagina (korte hoofdstukken). Een orientatiewissel,
        # een ontbrekende pagina of een niet-inhoudspagina (bijlage-
        # scheidingsblad) geeft altijd een nieuwe pagina.
        continue_on_page = (
            level == 1
            and section.get("continue_on_page") is True
            and not section.get("page_break_before", False)
            and self._page_template == "standaard"
        )
        page_break = section.get("page_break_before", False) or (
            level == 1 and not continue_on_page
        )
        if page_break or orientation_changed or self.page is None:
            self._new_page()
        elif continue_on_page:
            # Doorlopend hoofdstuk: witruimte boven de kop (bovenaan een
            # pagina is die er via de marge al). Past de kop met een paar
            # regels inhoud niet meer, dan toch een nieuwe pagina.
            h1 = self.blocks.get("heading_1", {})
            gap = h1.get("spacing_before_continued", 30.0)
            needed = (
                gap
                + h1.get("number", {}).get("size", 18.0)
                + h1.get("spacing_after", 33.9)
                + h1.get("keep_with_next", 40.0)
            )
            if not self._check_overflow(needed):
                self.y += gap

        number = self._resolve_heading_number(level, explicit_number)

        # Titel moet altijd getekend worden wanneer aanwezig, ook als
        # er geen (auto)nummer beschikbaar is.
        if title or number:
            reference = str(section.get("reference") or "")
            if level == 1:
                self.heading_1(number, title, str(section.get("part") or ""), reference)
            elif level == 2:
                self.heading_2(number, title, reference)

        for block in section.get("content", []):
            self._render_block(block)

        # Add page number if this was a top-level section start
        if level == 1:
            self._add_page_number()

        # Herstel vorige oriëntatie na de sectie
        if orientation_changed:
            self._orientation = previous_orientation
            self.y_max = (
                self._default_y_max_landscape
                if self._orientation == "landscape"
                else self._default_y_max_portrait
            )

    def _render_block(self, block: dict) -> None:
        """Dispatch a content block to the appropriate renderer."""
        block_type = block.get("type", "")
        if block_type == "paragraph":
            style = block.get("style", "")
            heading_text = block.get("text") or plain_text(runs_of(block) or [])
            if style in ("Heading1", "heading_1"):
                number = self._resolve_heading_number(1, block.get("number") or "")
                self.heading_1(number, heading_text)
            elif style in ("Heading2", "heading_2"):
                number = self._resolve_heading_number(2, block.get("number") or "")
                self.heading_2(number, heading_text)
            elif runs_of(block) is not None:
                self.rich_paragraph(block["runs"])
            else:
                self.paragraph(block.get("text", ""))
        elif block_type == "definition_list":
            self.definition_list(block)
        elif block_type == "checklist":
            self.checklist(block)
        elif block_type == "bullet_list":
            self.bullet_list(block.get("items", []))
        elif block_type == "heading_2":
            number = self._resolve_heading_number(2, block.get("number") or "")
            self.heading_2(number, block.get("title", ""))
        elif block_type == "heading_1":
            number = self._resolve_heading_number(1, block.get("number") or "")
            self.heading_1(number, block.get("title", ""))
        elif block_type == "table":
            self.table(block)
        elif block_type == "image":
            self.image(block)
        elif block_type == "spacer":
            self.spacer(block)
        elif block_type == "page_break":
            self.page_break()
        elif block_type == "calculation":
            self.calculation(block)
        elif block_type == "check":
            self.check(block)
        elif block_type == "map":
            self.map_block(block)
        else:
            logger.warning("Unsupported block type: %s", block_type)

    # --- Bijlage ---

    def render_bijlage_divider(self, nummer: str, titel: str) -> None:
        """Render appendix divider page."""
        self._new_page("bijlagen")
        # Number
        bijl_cfg = self.tpl.bijlage.get("dynamic_fields", {})
        nr_cfg = bijl_cfg.get("nummer", {})
        self._text(
            nr_cfg.get("x", 103.0),
            nr_cfg.get("y_td", 193.9),
            nummer,
            nr_cfg.get("font", "Inter-Bold"),
            nr_cfg.get("size", 41.4),
            self._color(nr_cfg, "color", "primary", "bijlage.nummer"),
        )

        # Title (fixed 20pt, multiline)
        ti_cfg = bijl_cfg.get("titel", {})
        fontsize = ti_cfg.get("size", 20.0)
        line_height = fontsize * 1.6
        ti_color = self._color(ti_cfg, "color", "paper", "bijlage.titel")
        for i, line in enumerate(titel.split("\n")):
            y_td = ti_cfg.get("y_td", 262.2) + i * line_height
            self._text(
                ti_cfg.get("x", 136.1),
                y_td,
                line,
                ti_cfg.get("font", "Inter-Regular"),
                fontsize,
                ti_color,
            )
        # No page number on divider, but increment counter
        self.current_page_nr += 1

    def render_achterblad(self) -> None:
        """Append static backcover PDF."""
        path = self.stationery.get("achterblad")
        if path and path.exists():
            src = fitz.open(str(path))
            self.doc.insert_pdf(src)
            src.close()

    def save(self, output_path: Path) -> None:
        """Save the assembled PyMuPDF document."""
        self.doc.save(str(output_path))
        self.doc.close()


# ---------------------------------------------------------------------------
# Main Generator — orchestrates all parts
# ---------------------------------------------------------------------------
class ReportGeneratorV2:
    """Top-level report generator. Accepts JSON, produces PDF.

    Usage:
        gen = ReportGeneratorV2(brand="default")
        gen.generate(data, stationery_dir, output_pdf)

        # Multi-tenant: per-request tenant (Authentik forward_auth header):
        gen = ReportGeneratorV2(brand="3bm", tenant_slug="3bm")

    Or from JSON file:
        gen = ReportGeneratorV2.from_json("report.json", stationery_dir, output)
    """

    def __init__(
        self,
        brand: str | None = None,
        tenant_slug: str | None = None,
        tenant_config: TenantConfig | None = None,
    ):
        self.brand_name = brand or os.environ.get("OPENAEC_DEFAULT_BRAND", "default")

        # Resolve tenant_config: expliciet > slug-lookup > env-var > None.
        # Env-var (OPENAEC_TENANT_DIR) blijft ondersteund voor legacy CLI /
        # examples scripts die geen tenant_slug meegeven.
        if tenant_config is None and tenant_slug:
            try:
                from openaec_reports.core.tenant_resolver import (
                    get_tenant_config,
                )
                tenant_config = get_tenant_config(tenant_slug)
            except Exception:  # pragma: no cover — defensief
                logger.exception(
                    "Kon tenant_config niet resolven voor slug '%s' — "
                    "fallback naar package defaults", tenant_slug,
                )
                tenant_config = None
        elif tenant_config is None and os.environ.get("OPENAEC_TENANT_DIR"):
            try:
                from openaec_reports.core.tenant import TenantConfig

                tenant_config = TenantConfig()
            except Exception:  # pragma: no cover — defensief
                tenant_config = None
        self._tenant_config = tenant_config

        # Laad brand config vóór de templates: TemplateSet gebruikt hem om
        # $colors./$fonts.-refs in de template-YAML's te resolven bij het
        # laden (zie core/refs.py). Zo hoeft geen enkele call-site verderop
        # in de renderer iets van refs te weten — de YAML-dicts die uit
        # TemplateSet komen zijn al volledig resolved.
        from openaec_reports.core.brand import BrandLoader

        try:
            loader = BrandLoader(
                tenant_config=tenant_config,
                tenant_slug=tenant_slug or "",
            )
            self.brand_config = loader.load(brand)
        except FileNotFoundError:
            self.brand_config = None
            if tenant_config is not None:
                logger.warning(
                    "Brand config '%s' niet gevonden in tenant '%s' of package — "
                    "fallback naar defaults",
                    brand, tenant_slug or tenant_config.tenant_dir,
                )
            else:
                logger.warning(
                    "Brand config niet gevonden voor '%s', gebruik defaults", brand,
                )

        self.templates = TemplateSet(
            brand, tenant_config=tenant_config, brand_config=self.brand_config,
        )

        # FontManager met brand font info + tenant cascade
        brand_fonts = self.brand_config.fonts if self.brand_config else None
        font_dir = None
        if self.brand_config and self.brand_config.brand_dir:
            candidate = self.brand_config.brand_dir / "fonts"
            if candidate.exists() and any(candidate.glob("*.ttf")):
                font_dir = candidate
        self.fonts = FontManager(
            font_dir=font_dir,
            brand_fonts=brand_fonts,
            tenant_config=tenant_config,
        )

    def _resolve_stationery(self, stationery_dir: Path) -> dict[str, Path]:
        """Resolve stationery bestandspaden uit brand config of fallback naar conventie."""
        result = {}
        stationery_mapping = {
            "cover": {
                "brand_keys": ["cover"],
                "fallbacks": ["cover.pdf", "cover_stationery.pdf"],
            },
            "colofon": {
                "brand_keys": ["colofon"],
                "fallbacks": ["colofon.pdf", "colofon_stationery.pdf"],
            },
            "standaard": {
                "brand_keys": ["content", "content_portrait"],
                "fallbacks": [
                    "standaard.pdf",
                    "content_portrait_stationery.pdf",
                    "content_portrait.pdf",
                ],
            },
            "content_landscape": {
                "brand_keys": ["content_landscape"],
                "fallbacks": [
                    "standaard_landscape.pdf",
                    "content_landscape_stationery.pdf",
                    "content_landscape.pdf",
                ],
            },
            "bijlagen": {
                "brand_keys": ["appendix", "bijlagen"],
                "fallbacks": ["bijlagen.pdf", "bijlagen_stationery.pdf"],
            },
            "achterblad": {
                "brand_keys": ["backcover", "achterblad"],
                "fallbacks": [
                    "achterblad.pdf",
                    "backcover_stationery.pdf",
                    "backcover.pdf",
                ],
            },
        }

        for key, config in stationery_mapping.items():
            resolved = None

            # 1. Probeer brand config
            if self.brand_config and self.brand_config.stationery:
                for brand_key in config["brand_keys"]:
                    spec = self.brand_config.stationery.get(brand_key)
                    if spec and spec.source:
                        # Relatief t.o.v. brand_dir
                        if self.brand_config.brand_dir:
                            candidate = self.brand_config.brand_dir / spec.source
                            if candidate.exists():
                                resolved = candidate
                                break
                        # Zoek op bestandsnaam in stationery_dir
                        candidate = stationery_dir / Path(spec.source).name
                        if candidate.exists():
                            resolved = candidate
                            break

            # 2. Fallback: zoek in stationery_dir op conventie-naam
            if resolved is None:
                for fallback_name in config["fallbacks"]:
                    candidate = stationery_dir / fallback_name
                    if candidate.exists():
                        resolved = candidate
                        break

            if resolved:
                result[key] = resolved

        return result

    def generate(
        self,
        data: dict[str, Any],
        stationery_dir: Path,
        output_path: Path,
    ) -> Path:
        """Generate a complete PDF report.

        Args:
            data: Report data dict (conforming to report.schema.json).
            stationery_dir: Directory with stationery PDFs and PNG overlay.
            output_path: Output PDF path.

        Returns:
            Path to generated PDF.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve stationery via brand config
        stationery = self._resolve_stationery(stationery_dir)

        # Cover: bepaal modus (PDF of PNG)
        cover_stationery = stationery.get("cover")
        cover_is_pdf = bool(cover_stationery and cover_stationery.suffix.lower() == ".pdf")

        # PNG overlay (legacy cover mode)
        if not cover_is_pdf:
            overlay_name = self.templates.cover.get("overlay", {}).get("file")
            stationery_png = (stationery_dir / overlay_name) if overlay_name else None
            # Fallback: zoek bekende PNG overlay
            if (not stationery_png or not stationery_png.exists()):
                legacy_png = stationery_dir / "2707_BBLrapportage_v01_1.png"
                if legacy_png.exists():
                    stationery_png = legacy_png
        else:
            stationery_png = None

        tmp_dir = output_path.parent
        tmp_cover = tmp_dir / "_tmp_cover.pdf"
        tmp_colofon = tmp_dir / "_tmp_colofon.pdf"
        tmp_content = tmp_dir / "_tmp_content.pdf"

        try:
            parts: list[Path] = []

            # 1. Cover (optioneel)
            if data.get("cover", {}).get("enabled", True):
                logger.info("Generating cover...")
                cover_gen = CoverGenerator(
                    self.templates, self.fonts, brand_config=self.brand_config
                )
                if cover_is_pdf:
                    cover_gen.generate(data, cover_stationery, tmp_cover, cover_is_pdf=True)
                else:
                    cover_gen.generate(data, stationery_png, tmp_cover, cover_is_pdf=False)
                parts.append(tmp_cover)

            # 2. Colofon (optioneel)
            colofon_stationery = stationery.get("colofon")
            colofon_enabled = data.get("colofon", {}).get("enabled", True)
            if colofon_enabled and colofon_stationery and colofon_stationery.exists():
                logger.info("Generating colofon...")
                colofon_gen = ColofonGenerator(
                    self.templates, self.fonts, brand_config=self.brand_config
                )
                colofon_gen.generate(data, colofon_stationery, tmp_colofon)
                parts.append(tmp_colofon)

            # 3. Content (TOC + sections + appendices + backcover)
            logger.info("Generating content...")
            content = ContentRenderer(
                self.templates, self.fonts, stationery,
                brand_config=self.brand_config,
            )
            # Adjust page numbering based on which parts are included
            content.current_page_nr = len(parts) + 1
            self._render_content(content, data)
            content.save(tmp_content)
            parts.append(tmp_content)

            # 4. Merge
            logger.info("Merging PDF...")
            self._merge_pdfs(parts, output_path)

            # Report stats
            result = fitz.open(str(output_path))
            page_count = result.page_count
            result.close()
            logger.info("Report ready: %s (%d pages)", output_path, page_count)
            return output_path

        finally:
            # Cleanup temp files
            for tmp in [tmp_cover, tmp_colofon, tmp_content]:
                tmp.unlink(missing_ok=True)

    def _render_content(self, renderer: ContentRenderer, data: dict) -> None:
        """Orchestrate rendering of TOC, sections, appendices, backcover.

        Twee-pass TOC-rendering: de TOC wordt ALS LAATSTE gerenderd in
        een aparte PyMuPDF-doc en vervolgens vooraan in de content-doc
        ingevoegd. Zo krijgen TOC-entries de werkelijke paginanummers
        op basis van het ``_heading_log`` in plaats van de grove
        schatting uit ``_build_toc_entries`` (die alle entries naar
        dezelfde pagina liet wijzen).
        """
        renderer.header_label = data.get("header_label") or ""
        toc_cfg = data.get("toc", {})
        toc_enabled = toc_cfg.get("enabled", True)
        # Auto-nummering default aan — rapporten met eigen nummering in
        # de titels kunnen dit via ``toc.auto_number = false`` uitzetten.
        renderer._auto_number_enabled = bool(
            toc_cfg.get("auto_number", True)
        )
        renderer._section_counter = 0
        renderer._subsection_counter = 0

        # Paginanummer voor de TOC zelf (één positie gereserveerd vóór de
        # sections). Sections starten daardoor één hoger dan de huidige
        # renderer.current_page_nr.
        toc_page_nr = renderer.current_page_nr
        if toc_enabled:
            renderer.current_page_nr += 1  # reserveer 1 pagina voor TOC

        # Document-brede oriëntatie: een top-level ``orientation`` (of
        # ``format`` met "landscape") maakt álle content-secties landscape,
        # tenzij een sectie het zelf via ``section["orientation"]``
        # overschrijft. Cover/colofon (portret-ontworpen) blijven portret.
        # Zonder deze velden blijft alles portret → bestaande rapporten en de
        # pixel-baseline ongewijzigd. (Voorheen werd oriëntatie ALLEEN
        # per-sectie gelezen; een top-level veld werd stil genegeerd, waardoor
        # een "landscape"-rapport toch portret bleef — gemeld door ypsilon.)
        _doc_or = str(data.get("orientation") or "").lower()
        _fmt = str(data.get("format") or "").lower()
        if _doc_or == "landscape" or "landscape" in _fmt:
            renderer._orientation = "landscape"
        elif _doc_or == "portrait":
            renderer._orientation = "portrait"

        # Sections
        for section in data.get("sections", []):
            renderer.render_section(section)

        # Finalize last content page number if not already done
        if renderer.page is not None:
            renderer._add_page_number()

        # Appendices
        for appendix in data.get("appendices", []):
            nummer = appendix.get("label", f"Bijlage {appendix.get('number', 1)}")
            titel = appendix.get("title", "")
            renderer.render_bijlage_divider(nummer, titel)

            # Appendix content pages
            for section in appendix.get("content_sections", []):
                renderer.render_section(section)
            if appendix.get("content_sections"):
                renderer._add_page_number()

        # Backcover
        if data.get("backcover", {}).get("enabled", True):
            renderer.render_achterblad()

        # TOC — deferred rendering met werkelijke paginanummers
        if toc_enabled:
            toc_entries = self._build_toc_entries_from_log(
                renderer.heading_log, data
            )
            toc_doc = renderer.render_toc_to_fresh_doc(
                toc_entries, toc_page_nr
            )
            try:
                # Voeg TOC pagina(s) in op positie 0 van de content-doc
                renderer.doc.insert_pdf(toc_doc, start_at=0)
            finally:
                toc_doc.close()

    def _build_toc_entries(self, data: dict) -> list[tuple]:
        """Build TOC entries from sections data.

        Returns list of (level, number, title, estimated_page).
        """
        entries = []
        # Start page estimation: cover(1) + colofon(2) + toc(3) = content starts at 4
        page_est = 4

        for section in data.get("sections", []):
            level = section.get("level", 1)
            number = section.get("number", "")
            title = section.get("title", "")
            entries.append((level, number, title, page_est))

            # Rough page estimation based on content volume
            if level == 1:
                page_est += 1

            # Sub-sections
            for block in section.get("content", []):
                if block.get("type") == "heading_2":
                    entries.append(
                        (
                            2,
                            block.get("number", ""),
                            block.get("title", ""),
                            page_est,
                        )
                    )

        return entries

    def _build_toc_entries_from_log(
        self,
        heading_log: list[tuple[int, str, str, int]],
        data: dict,
    ) -> list[tuple]:
        """Build TOC entries from the actual heading render log.

        Het ``heading_log`` bevat tuples van ``(level, number, title,
        display_page_nr)`` die door ``heading_1`` / ``heading_2`` worden
        vastgelegd op het moment dat ze daadwerkelijk getekend worden
        (na eventuele page-overflow). Dit levert de juiste TOC-nummers,
        in tegenstelling tot ``_build_toc_entries`` die een grove
        schatting maakt en in de praktijk alle entries naar dezelfde
        pagina laat wijzen.

        Args:
            heading_log: Door de renderer opgebouwde log tijdens de
                sections-pass.
            data: Rapport-data (gebruikt als fallback wanneer het log
                leeg blijkt — bijv. tests die direct op de TOC richten).

        Returns:
            Lijst van ``(level, number, title, page_nr)`` tuples.
        """
        if heading_log:
            return list(heading_log)
        # Fallback: gebruik de oude heuristische builder als het log
        # leeg is (bijv. bij lege rapporten in tests).
        return self._build_toc_entries(data)

    @staticmethod
    def _merge_pdfs(input_paths: list[Path], output_path: Path) -> None:
        """Merge multiple PDFs into one."""
        final = fitz.open()
        for path in input_paths:
            if path.exists():
                src = fitz.open(str(path))
                final.insert_pdf(src)
                src.close()
        final.save(str(output_path))
        final.close()

    @classmethod
    def from_json(
        cls,
        json_path: str | Path,
        stationery_dir: str | Path,
        output_path: str | Path,
        brand: str | None = None,
    ) -> Path:
        """Generate report directly from JSON file.

        Args:
            json_path: Path to JSON report definition.
            stationery_dir: Directory with stationery PDFs.
            output_path: Output PDF path.
            brand: Template brand name.

        Returns:
            Path to generated PDF.
        """
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        gen = cls(brand=brand)
        return gen.generate(data, Path(stationery_dir), Path(output_path))
