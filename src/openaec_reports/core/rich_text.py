"""Opgemaakte tekst als runs: regelopbouw zonder HTML te parsen.

Een run is een dict met ``text`` plus optioneel ``bold``, ``italic`` en
``color``, of een dict met ``label`` (een chip zoals "n.t.b."). Deze module
doet alleen de regelopbouw: stukken meten en over regels verdelen. Fonts,
kleuren en tekenen horen bij de renderer, die de stukken aanlevert.

Gebruikt door renderer_v2 voor ``paragraph``, ``bullet_list``-items,
tabelcellen en checklist-items. De Rust-engine hoort dezelfde regels te
volgen (zie ``layout_pieces``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

LABEL_KINDS = ("ntb", "bron", "ok", "nvt", "fout")

_RE_WHITESPACE_SPLIT = re.compile(r"(\s+)")


def runs_of(value: Any) -> list[dict] | None:
    """Geef de runs van een blok, item of cel, of ``None`` als er geen zijn.

    ``runs`` wint van ``text`` als beide aanwezig zijn.
    """
    if isinstance(value, dict):
        runs = value.get("runs")
        if isinstance(runs, list) and runs:
            return runs
    return None


def plain_text(runs: list[dict]) -> str:
    """Platte tekst van runs (labels als hun tekst), voor meten en fallback."""
    parts: list[str] = []
    for run in runs:
        label = run.get("label")
        if "check" in run and not run.get("text") and not label:
            parts.append("[x]" if run["check"] else "[ ]")
        elif isinstance(label, dict):
            parts.append(str(label.get("text", "")))
        elif label:
            parts.append(str(label))
        else:
            parts.append(str(run.get("text", "")))
    return "".join(parts)


@dataclass
class TextStyle:
    """Font, grootte en kleur van een tekststuk. ``font`` heeft ``text_length``."""

    font: Any
    size: float
    color: str


@dataclass
class LabelStyle:
    """Opmaak van een label (chip): omlijnd en/of gevuld, kleine tekst."""

    font: Any
    size: float
    text_color: str
    border_color: str | None
    fill_color: str | None
    pad_x: float
    pad_y: float
    radius: float
    line_width: float


@dataclass
class BoxStyle:
    """Aankruisvak in lopende tekst of een tabelcel."""

    size: float
    line_width: float
    color: str
    check_color: str


@dataclass
class Piece:
    """Een ondeelbaar stuk: een woord(deel) in een stijl, een label of een vak."""

    text: str
    width: float
    style: TextStyle | None = None
    label: LabelStyle | None = None
    box: BoxStyle | None = None
    checked: bool = False


@dataclass
class Space:
    """Witruimte tussen stukken; ``width`` is de spatiebreedte in die stijl."""

    width: float


@dataclass
class Break:
    """Harde regelafbreking (``\\n`` in de tekst)."""


@dataclass
class Line:
    """Een opgemaakte regel: stukken met hun x-offset vanaf de linkerkant."""

    pieces: list[tuple[float, Piece]] = field(default_factory=list)
    width: float = 0.0


def text_pieces(text: str, style: TextStyle) -> list[Piece | Space | Break]:
    """Splits tekst in woorden, spaties en harde afbrekingen."""
    out: list[Piece | Space | Break] = []
    space_w = style.font.text_length(" ", fontsize=style.size)
    for part in _RE_WHITESPACE_SPLIT.split(text):
        if not part:
            continue
        if part.isspace():
            breaks = part.count("\n")
            if breaks:
                out.extend(Break() for _ in range(breaks))
            else:
                out.append(Space(space_w))
        else:
            width = style.font.text_length(part, fontsize=style.size)
            out.append(Piece(part, width, style=style))
    return out


def label_piece(text: str, style: LabelStyle) -> Piece:
    """Een label als ondeelbaar stuk; breedte inclusief binnenmarge."""
    width = style.font.text_length(text, fontsize=style.size) + 2 * style.pad_x
    return Piece(text, width, label=style)


def box_piece(checked: bool, style: BoxStyle) -> Piece:
    """Een aankruisvak als ondeelbaar stuk (leeg of met vinkje)."""
    return Piece("", style.size, box=style, checked=checked)


def _split_wide(piece: Piece, max_width: float) -> list[Piece]:
    """Knip een te breed tekststuk per teken op (laatste redmiddel)."""
    if piece.style is None:
        return [piece]
    font, size = piece.style.font, piece.style.size
    chunks: list[Piece] = []
    current = ""
    for ch in piece.text:
        trial = current + ch
        if current and font.text_length(trial, fontsize=size) > max_width:
            chunks.append(Piece(current, font.text_length(current, fontsize=size), piece.style))
            current = ch
        else:
            current = trial
    if current:
        chunks.append(Piece(current, font.text_length(current, fontsize=size), piece.style))
    return chunks


def _append(line: Line, piece: Piece, lead: float) -> None:
    """Zet een stuk achter op de regel; voeg samen met gelijke tekststijl."""
    if line.pieces:
        last_x, last = line.pieces[-1]
        if (
            piece.style is not None
            and last.label is None
            and piece.label is None
            and last.style is piece.style
        ):
            joiner = " " if lead > 0 else ""
            last.text = last.text + joiner + piece.text
            last.width = last.width + lead + piece.width
            line.width = last_x + last.width
            return
    x = line.width + (lead if line.pieces else 0.0)
    line.pieces.append(
        (x, Piece(piece.text, piece.width, piece.style, piece.label, piece.box, piece.checked))
    )
    line.width = x + piece.width


def layout_pieces(items: list[Piece | Space | Break], max_width: float) -> list[Line]:
    """Verdeel stukken over regels (greedy, zoals ``FontManager.wrap_text``).

    Stukken zonder witruimte ertussen vormen samen een woord en worden niet
    gesplitst (bijv. "Monitoringsplan:" gevolgd door een label zonder spatie).
    Meerdere spaties tellen als een. Een woord dat breder is dan de regel
    wordt per teken geknipt.

    Returns:
        Altijd minstens een (mogelijk lege) regel.
    """
    # 1. Groepeer tot woorden: (spatie ervoor, stukken) of een harde afbreking.
    groups: list[tuple[float, list[Piece]] | None] = []
    current: list[Piece] = []
    lead = 0.0
    pending = 0.0
    for item in items:
        if isinstance(item, Piece):
            if not current:
                lead = pending
                pending = 0.0
            current.append(item)
        elif isinstance(item, Space):
            if current:
                groups.append((lead, current))
                current = []
            pending = max(pending, item.width)
        else:
            if current:
                groups.append((lead, current))
                current = []
            groups.append(None)
            pending = 0.0
    if current:
        groups.append((lead, current))

    # 2. Vul regels.
    lines = [Line()]
    for group in groups:
        if group is None:
            lines.append(Line())
            continue
        lead, pieces = group
        width = sum(p.width for p in pieces)
        line = lines[-1]
        if line.pieces and line.width + lead + width > max_width:
            line = Line()
            lines.append(line)
        if not line.pieces and width > max_width:
            for piece in pieces:
                for chunk in _split_wide(piece, max_width):
                    if line.pieces and line.width + chunk.width > max_width:
                        line = Line()
                        lines.append(line)
                    _append(line, chunk, 0.0)
            continue
        for i, piece in enumerate(pieces):
            _append(line, piece, lead if i == 0 else 0.0)
    return lines
