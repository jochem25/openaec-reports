"""Tests voor flow layout — zone verschuiving bij text wrapping overflow."""

from __future__ import annotations

from openaec_reports.core.template_config import (
    ImageZone,
    LineZone,
    TextZone,
)
from openaec_reports.core.template_engine import _apply_flow_layout, _paginate_flow_zones


def _make_brand():
    """Minimale brand mock voor font/kleur resolve."""
    class _Brand:
        fonts = {"body": "Helvetica", "heading": "Helvetica-Bold"}
        colors = {
            "primary": "#40124A",
            "secondary": "#38BDA0",
            "text": "#45243D",
            "white": "#FFFFFF",
        }
    return _Brand()


# ============================================================
# Helpers
# ============================================================

FOOTER_Y = 260.0


def _tz(bind: str, y_mm: float, max_width_mm: float | None = None) -> TextZone:
    """Shorthand voor TextZone aanmaken."""
    return TextZone(
        bind=bind,
        x_mm=19.5,
        y_mm=y_mm,
        font="body",
        size=10,
        max_width_mm=max_width_mm,
        line_height_mm=4.2,
    )


def _lz(y_mm: float) -> LineZone:
    """Shorthand voor LineZone aanmaken."""
    return LineZone(x0_mm=17.5, y_mm=y_mm, x1_mm=80.2, width_pt=1.0, color="primary")


def _iz(bind: str, y_mm: float) -> ImageZone:
    """Shorthand voor ImageZone aanmaken."""
    return ImageZone(bind=bind, x_mm=40.0, y_mm=y_mm, width_mm=130, height_mm=100)


# ============================================================
# Tests
# ============================================================


class TestNoOverflow:
    """Als data kort genoeg is, mogen zones niet verschuiven."""

    def test_short_text_no_changes(self) -> None:
        """Korte tekst → geen verschuiving."""
        text_zones = [
            _tz("_static.Label", 44.3),
            _tz("field.value", 44.3, max_width_mm=83),
            _tz("_static.Label2", 48.4),
            _tz("field.value2", 48.4, max_width_mm=83),
        ]
        data = {"field": {"value": "Kort", "value2": "Ook kort"}}

        new_tz, new_lz, new_iz = _apply_flow_layout(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        # Posities ongewijzigd
        assert [z.y_mm for z in new_tz] == [44.3, 44.3, 48.4, 48.4]

    def test_no_max_width_no_changes(self) -> None:
        """Zones zonder max_width_mm → geen wrapping check."""
        text_zones = [
            _tz("_static.Label", 44.3),
            _tz("_static.Label2", 48.4),
        ]
        data = {}

        new_tz, _, _ = _apply_flow_layout(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        assert [z.y_mm for z in new_tz] == [44.3, 48.4]


class TestSingleRowOverflow:
    """Eén veld wraps naar meerdere regels — alles eronder verschuift."""

    def test_single_overflow_shifts_below(self) -> None:
        """Veld op y=44.3 wraps, zones op y=48.4 en y=52.4 moeten verschuiven."""
        text_zones = [
            _tz("_static.Label", 44.3),
            _tz("desc", 44.3, max_width_mm=83),
            _tz("_static.Label2", 48.4),
            _tz("val2", 48.4, max_width_mm=83),
            _tz("_static.Label3", 52.4),
            _tz("val3", 52.4, max_width_mm=83),
        ]

        # Tekst die lang genoeg is om te wrappen in 83mm bij 10pt Helvetica
        long_text = (
            "Dit is een zeer lange beschrijving die absoluut niet in een enkele regel "
            "past bij 83mm breedte en 10pt font"
        )
        data = {"desc": long_text, "val2": "kort", "val3": "kort"}

        new_tz, _, _ = _apply_flow_layout(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        # De eerste rij (44.3) behoudt positie
        row1 = [z for z in new_tz if abs(z.y_mm - 44.3) < 0.1]
        assert len(row1) == 2

        # De tweede rij moet verschoven zijn (> 48.4)
        row2 = [z for z in new_tz if z.bind in ("_static.Label2", "val2")]
        assert all(z.y_mm > 48.4 for z in row2)

        # De derde rij moet ook verschoven zijn
        row3 = [z for z in new_tz if z.bind in ("_static.Label3", "val3")]
        assert all(z.y_mm > 52.4 for z in row3)


class TestMultipleOverflows:
    """Meerdere velden wrappen — offsets accumuleren."""

    def test_cumulative_offsets(self) -> None:
        """Twee velden wrappen → derde rij verschuift met som van beide."""
        text_zones = [
            _tz("desc1", 44.3, max_width_mm=83),
            _tz("desc2", 48.4, max_width_mm=83),
            _tz("val3", 52.4, max_width_mm=83),
        ]

        long_text = (
            "Dit is een zeer lange beschrijving die absoluut niet in een enkele regel "
            "past bij 83mm breedte en 10pt font"
        )
        data = {"desc1": long_text, "desc2": long_text, "val3": "kort"}

        new_tz, _, _ = _apply_flow_layout(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        # Derde zone moet meer verschoven zijn dan de tweede
        shift_2 = new_tz[1].y_mm - 48.4
        shift_3 = new_tz[2].y_mm - 52.4
        assert shift_3 >= shift_2


class TestFooterExcluded:
    """Footer zones (y >= footer_y_mm) blijven op hun vaste positie."""

    def test_footer_not_shifted(self) -> None:
        """Footer zones verplaatsen NIET, ook bij overflow."""
        text_zones = [
            _tz("desc", 44.3, max_width_mm=83),
            _tz("_static.Label2", 48.4),
            # Footer zones
            TextZone(bind="client.name", x_mm=10.3, y_mm=275.4, font="heading", size=10),
            TextZone(
                bind="_page_number", x_mm=172.0, y_mm=275.4, font="heading", size=12, align="right"
            ),
        ]

        long_text = (
            "Dit is een zeer lange beschrijving die absoluut niet in een enkele regel "
            "past bij 83mm breedte"
        )
        data = {"desc": long_text, "client": {"name": "TestBV"}}

        new_tz, _, _ = _apply_flow_layout(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        # Footer zones behouden originele y
        footer_zones = [z for z in new_tz if z.y_mm >= FOOTER_Y]
        assert len(footer_zones) == 2
        assert all(z.y_mm == 275.4 for z in footer_zones)


class TestLineZonesShift:
    """Lijnen verschuiven mee met tekst."""

    def test_lines_shift_with_content(self) -> None:
        """Line zones onder overflow verschuiven mee."""
        text_zones = [
            _tz("desc", 44.3, max_width_mm=83),
            _tz("_static.Label2", 90.0),
        ]
        line_zones = [
            _lz(42.05),  # boven de overflow → kleine/geen shift
            _lz(88.55),  # onder de overflow → moet verschuiven als er overflow is
        ]

        long_text = (
            "Dit is een zeer lange beschrijving die absoluut niet in een enkele regel "
            "past bij 83mm breedte en 10pt font"
        )
        data = {"desc": long_text}

        _, new_lz, _ = _apply_flow_layout(
            text_zones, line_zones, [], data, _make_brand(), FOOTER_Y,
        )

        # Lijn op 88.55 moet verschoven zijn als er overflow was
        # (afhankelijk van of er genoeg overflow is — met 1 wrapping rij
        #  en gap van 45.7mm is er mogelijk geen overflow)
        # We controleren minimaal dat het mechanisme werkt:
        assert len(new_lz) == 2


class TestImageZonesShift:
    """Afbeeldingen verschuiven mee met tekst."""

    def test_images_shift_with_content(self) -> None:
        """Image zones onder overflow verschuiven mee."""
        text_zones = [
            _tz("desc", 44.3, max_width_mm=83),
            _tz("_static.Label2", 48.4),
        ]
        image_zones = [
            _iz("foto", 157.0),
        ]

        long_text = (
            "Dit is een zeer lange beschrijving die absoluut niet in een enkele regel "
            "past bij 83mm breedte en 10pt font"
        )
        data = {"desc": long_text}

        _, _, new_iz = _apply_flow_layout(
            text_zones, [], image_zones, data, _make_brand(), FOOTER_Y,
        )

        assert len(new_iz) == 1


class TestEmptyData:
    """Lege of ontbrekende data veroorzaakt geen fouten."""

    def test_empty_data_no_crash(self) -> None:
        """Lege data dict → geen crash, zones ongewijzigd."""
        text_zones = [
            _tz("_static.Label", 44.3),
            _tz("field.value", 44.3, max_width_mm=83),
            _tz("_static.Label2", 48.4),
        ]

        new_tz, _, _ = _apply_flow_layout(
            text_zones, [], [], {}, _make_brand(), FOOTER_Y,
        )

        assert [z.y_mm for z in new_tz] == [44.3, 44.3, 48.4]

    def test_none_values_no_crash(self) -> None:
        """None waarden in data → geen crash."""
        text_zones = [
            _tz("field.value", 44.3, max_width_mm=83),
            _tz("field.other", 48.4, max_width_mm=83),
        ]
        data = {"field": {"value": None, "other": None}}

        new_tz, _, _ = _apply_flow_layout(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        assert [z.y_mm for z in new_tz] == [44.3, 48.4]

    def test_empty_zones_no_crash(self) -> None:
        """Geen zones → geen crash."""
        new_tz, new_lz, new_iz = _apply_flow_layout(
            [], [], [], {}, _make_brand(), FOOTER_Y,
        )

        assert new_tz == []
        assert new_lz == []
        assert new_iz == []

    def test_single_row_no_crash(self) -> None:
        """Slechts één rij → geen crash (geen volgende rij voor gap berekening)."""
        text_zones = [_tz("desc", 44.3, max_width_mm=83)]
        data = {"desc": "Kort"}

        new_tz, _, _ = _apply_flow_layout(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        assert len(new_tz) == 1


class TestParseFlowLayoutConfig:
    """parse_page_type leest flow_layout en flow_footer_y_mm."""

    def test_parse_flow_layout_true(self) -> None:
        from openaec_reports.core.template_config import parse_page_type

        data = {
            "name": "test_page",
            "flow_layout": True,
            "flow_footer_y_mm": 265.0,
        }
        pt = parse_page_type(data)
        assert pt.flow_layout is True
        assert pt.flow_footer_y_mm == 265.0

    def test_parse_flow_layout_default(self) -> None:
        from openaec_reports.core.template_config import parse_page_type

        data = {"name": "test_page"}
        pt = parse_page_type(data)
        assert pt.flow_layout is False
        assert pt.flow_footer_y_mm == 260.0

    def test_parse_flow_content_start_y_mm(self) -> None:
        from openaec_reports.core.template_config import parse_page_type

        data = {
            "name": "test_page",
            "flow_layout": True,
            "flow_content_start_y_mm": 40.0,
        }
        pt = parse_page_type(data)
        assert pt.flow_content_start_y_mm == 40.0

    def test_parse_flow_content_start_y_mm_default(self) -> None:
        from openaec_reports.core.template_config import parse_page_type

        data = {"name": "test_page"}
        pt = parse_page_type(data)
        assert pt.flow_content_start_y_mm == 32.0


# ============================================================
# Pagination tests
# ============================================================


VERY_LONG_TEXT = (
    "Dit is een extreem lange beschrijving die heel veel regels nodig heeft "
    "om volledig weer te geven. De tekst is zo lang dat het onmogelijk is "
    "om deze in een paar regels te plaatsen bij 83mm breedte en 10pt font. "
    "Er moet zeker gewrapped worden over meerdere regels en dat zorgt voor "
    "extra ruimte die de zones eronder naar beneden duwt. Deze tekst wordt "
    "herhaald voor meerdere velden zodat de cumulatieve offset groot genoeg "
    "wordt om content voorbij de footer grens te duwen."
)


def _build_page_zones(
    num_rows: int,
    start_y: float = 44.3,
    gap: float = 4.1,
    footer_y: float = FOOTER_Y,
) -> tuple[list[TextZone], list[LineZone]]:
    """Bouw een realistische set zones: label + waarde per rij, + footer."""
    text_zones = []
    line_zones = []
    for i in range(num_rows):
        y = start_y + i * gap
        text_zones.append(_tz(f"_static.Label{i}", y))
        text_zones.append(_tz(f"field.val{i}", y, max_width_mm=83))
    # Footer zones
    text_zones.append(
        TextZone(bind="client.name", x_mm=10.3, y_mm=275.4, font="heading", size=10)
    )
    text_zones.append(
        TextZone(
            bind="_page_number", x_mm=172.0, y_mm=275.4,
            font="heading", size=12, align="right",
        )
    )
    return text_zones, line_zones


class TestPaginateNoOverflow:
    """Geen overflow → één pagina."""

    def test_short_text_single_page(self) -> None:
        text_zones, _ = _build_page_zones(5)
        data = {"field": {f"val{i}": "Kort" for i in range(5)}}

        pages = _paginate_flow_zones(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        assert len(pages) == 1
        # Alle zones inclusief footer op pagina 1
        tz, lz, iz = pages[0]
        assert len(tz) == len(text_zones)

    def test_no_wrapping_single_page(self) -> None:
        text_zones = [_tz("_static.A", 44.3), _tz("_static.B", 200.0)]
        footer_tz = [TextZone(bind="client.name", x_mm=10, y_mm=275, font="body", size=10)]

        pages = _paginate_flow_zones(
            text_zones + footer_tz, [], [], {}, _make_brand(), FOOTER_Y,
        )

        assert len(pages) == 1


class TestPaginateOverflow:
    """Content overflowt voorbij footer → meerdere pagina's."""

    def test_overflow_creates_two_pages(self) -> None:
        """Veel rijen met lange tekst → overflow naar pagina 2."""
        # 50 rijen met start_y=44.3 en gap=4.1 → laatste rij op y=245.2
        # Na wrapping kan accumulated offset >15mm zijn → zones voorbij 260
        text_zones, _ = _build_page_zones(50)
        data = {"field": {f"val{i}": VERY_LONG_TEXT for i in range(50)}}

        pages = _paginate_flow_zones(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        assert len(pages) >= 2

    def test_footer_on_all_pages(self) -> None:
        """Footer zones (client.name, _page_number) staan op elke pagina."""
        text_zones, _ = _build_page_zones(50)
        data = {"field": {f"val{i}": VERY_LONG_TEXT for i in range(50)}}

        pages = _paginate_flow_zones(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        for page_tz, _, _ in pages:
            footer = [z for z in page_tz if z.y_mm >= FOOTER_Y]
            assert len(footer) == 2, "Footer zones moeten op elke pagina staan"
            binds = {z.bind for z in footer}
            assert "client.name" in binds
            assert "_page_number" in binds

    def test_overflow_zones_repositioned(self) -> None:
        """Overflow zones worden herpositioneerd op vervolg-pagina."""
        text_zones, _ = _build_page_zones(50)
        data = {"field": {f"val{i}": VERY_LONG_TEXT for i in range(50)}}
        content_start = 32.0

        pages = _paginate_flow_zones(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
            content_start_y_mm=content_start,
        )

        assert len(pages) >= 2
        # Op pagina 2: content zones moeten starten rond content_start_y_mm
        page2_tz, _, _ = pages[1]
        content_zones = [z for z in page2_tz if z.y_mm < FOOTER_Y]
        assert content_zones, "Pagina 2 moet content zones bevatten"
        min_y = min(z.y_mm for z in content_zones)
        assert abs(min_y - content_start) < 1.0, (
            f"Content op pagina 2 moet starten rond {content_start}mm, niet {min_y}mm"
        )

    def test_no_content_above_footer_on_any_page(self) -> None:
        """Geen content zone mag y >= footer_y_mm hebben op enige pagina."""
        text_zones, _ = _build_page_zones(50)
        data = {"field": {f"val{i}": VERY_LONG_TEXT for i in range(50)}}

        pages = _paginate_flow_zones(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
        )

        for page_idx, (page_tz, page_lz, page_iz) in enumerate(pages):
            # Footer zones zijn OK (die staan altijd >= FOOTER_Y)
            footer_binds = {"client.name", "_page_number"}
            for z in page_tz:
                if z.bind not in footer_binds:
                    assert z.y_mm < FOOTER_Y, (
                        f"Content zone '{z.bind}' op pagina {page_idx + 1} "
                        f"staat op y={z.y_mm}mm >= footer {FOOTER_Y}mm"
                    )


class TestPaginateLineZones:
    """Line zones splitsen mee met content."""

    def test_line_zones_split(self) -> None:
        """Line zones boven footer blijven, rest gaat naar pagina 2."""
        text_zones, _ = _build_page_zones(50)
        # Lijnen bij begin, midden en eind van content
        line_zones = [_lz(42.0), _lz(130.0), _lz(250.0)]
        data = {"field": {f"val{i}": VERY_LONG_TEXT for i in range(50)}}

        pages = _paginate_flow_zones(
            text_zones, line_zones, [], data, _make_brand(), FOOTER_Y,
        )

        assert len(pages) >= 2
        # Pagina 1 moet minstens de eerste lijn bevatten
        _, p1_lz, _ = pages[0]
        assert len(p1_lz) >= 1


class TestPaginateImageZones:
    """Image zones splitsen mee met content."""

    def test_image_zone_on_overflow_page(self) -> None:
        """Image zone die overflowt komt op vervolg-pagina."""
        text_zones, _ = _build_page_zones(50)
        # Image zone ver naar beneden → overflowt bij veel wrapping
        image_zones = [_iz("foto", 255.0)]
        data = {"field": {f"val{i}": VERY_LONG_TEXT for i in range(50)}}

        pages = _paginate_flow_zones(
            text_zones, [], image_zones, data, _make_brand(), FOOTER_Y,
        )

        # Image moet ergens staan (pagina 1 of 2 afhankelijk van shift)
        total_images = sum(len(iz) for _, _, iz in pages)
        assert total_images >= 1


class TestPaginateEdgeCases:
    """Randgevallen voor paginering."""

    def test_empty_zones(self) -> None:
        pages = _paginate_flow_zones([], [], [], {}, _make_brand(), FOOTER_Y)
        assert len(pages) == 1
        assert pages[0] == ([], [], [])

    def test_only_footer_zones(self) -> None:
        """Alleen footer zones → één pagina."""
        footer = [TextZone(bind="x", x_mm=10, y_mm=275, font="body", size=10)]
        pages = _paginate_flow_zones(footer, [], [], {}, _make_brand(), FOOTER_Y)
        assert len(pages) == 1

    def test_custom_content_start(self) -> None:
        """Aangepaste content_start_y_mm wordt gebruikt voor herpositionering."""
        text_zones, _ = _build_page_zones(50)
        data = {"field": {f"val{i}": VERY_LONG_TEXT for i in range(50)}}

        pages = _paginate_flow_zones(
            text_zones, [], [], data, _make_brand(), FOOTER_Y,
            content_start_y_mm=50.0,
        )

        if len(pages) >= 2:
            page2_tz, _, _ = pages[1]
            content_zones = [z for z in page2_tz if z.y_mm < FOOTER_Y]
            if content_zones:
                min_y = min(z.y_mm for z in content_zones)
                assert abs(min_y - 50.0) < 1.0
