"""Tests voor de renderer_v2-uitbreidingen voor het bouwveiligheidsplan (E1-E9)."""

from __future__ import annotations

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
