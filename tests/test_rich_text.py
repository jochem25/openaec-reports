"""Tests voor core/rich_text: regelopbouw van runs (BVP E2)."""

from __future__ import annotations

from openaec_reports.core.rich_text import (
    Break,
    LabelStyle,
    Piece,
    Space,
    TextStyle,
    label_piece,
    layout_pieces,
    plain_text,
    runs_of,
    text_pieces,
)


class FixedFont:
    """Font met vaste tekenbreedte: 1 pt per teken per pt fontgrootte / 10."""

    def text_length(self, text: str, fontsize: float) -> float:
        return len(text) * fontsize / 10


FONT = FixedFont()
BASE = TextStyle(FONT, 10.0, "#000000")  # 1 pt per teken
OTHER = TextStyle(FONT, 10.0, "#FF0000")
LABEL = LabelStyle(FONT, 10.0, "#000000", "#000000", None, 2.0, 1.0, 1.0, 0.5)


def texts(lines):
    return [[p.text for _, p in line.pieces] for line in lines]


class TestRunsOf:
    def test_runs_win_over_text(self):
        assert runs_of({"text": "a", "runs": [{"text": "b"}]}) == [{"text": "b"}]

    def test_no_runs(self):
        assert runs_of({"text": "a"}) is None
        assert runs_of("a") is None
        assert runs_of({"runs": []}) is None

    def test_plain_text(self):
        runs = [{"text": "Stand: "}, {"label": {"text": "n.t.b.", "kind": "ntb"}}]
        assert plain_text(runs) == "Stand: n.t.b."


class TestTextPieces:
    def test_words_spaces_breaks(self):
        items = text_pieces("ab  cd\nef", BASE)
        kinds = [type(i).__name__ for i in items]
        assert kinds == ["Piece", "Space", "Piece", "Break", "Piece"]
        assert items[0].width == 2.0

    def test_label_width_includes_padding(self):
        assert label_piece("ntb", LABEL).width == 3.0 + 2 * 2.0


class TestLayout:
    def test_fits_on_one_line_and_merges_same_style(self):
        lines = layout_pieces(text_pieces("aa bb cc", BASE), 100)
        assert texts(lines) == [["aa bb cc"]]
        assert lines[0].width == 8.0

    def test_wraps_greedy(self):
        lines = layout_pieces(text_pieces("aaaa bbbb cccc", BASE), 9)
        assert texts(lines) == [["aaaa bbbb"], ["cccc"]]

    def test_style_change_keeps_positions(self):
        items = text_pieces("ab ", BASE) + text_pieces("cd", OTHER)
        lines = layout_pieces(items, 100)
        assert [(x, p.text) for x, p in lines[0].pieces] == [(0.0, "ab"), (3.0, "cd")]

    def test_glued_runs_do_not_split(self):
        # "ab" + "cd" zonder spatie is een woord: gaat samen naar de volgende regel.
        items = text_pieces("xxxxxx ab", BASE) + text_pieces("cd", OTHER)
        lines = layout_pieces(items, 8)
        assert texts(lines) == [["xxxxxx"], ["ab", "cd"]]

    def test_label_is_atomic(self):
        items = [*text_pieces("abc ", BASE), label_piece("ntb", LABEL)]
        lines = layout_pieces(items, 8)
        assert len(lines) == 2
        assert lines[1].pieces[0][1].label is LABEL

    def test_hard_break(self):
        lines = layout_pieces(text_pieces("ab\ncd", BASE), 100)
        assert texts(lines) == [["ab"], ["cd"]]

    def test_too_wide_word_split_per_char(self):
        lines = layout_pieces(text_pieces("abcdefghij", BASE), 4)
        assert texts(lines) == [["abcd"], ["efgh"], ["ij"]]

    def test_empty_gives_one_line(self):
        assert len(layout_pieces([], 10)) == 1

    def test_multiple_spaces_collapse(self):
        items: list = [Piece("a", 1.0, BASE), Space(1.0), Space(1.0), Piece("b", 1.0, BASE)]
        lines = layout_pieces(items, 100)
        assert lines[0].width == 3.0

    def test_break_resets_pending_space(self):
        items: list = [Piece("a", 1.0, BASE), Space(1.0), Break(), Piece("b", 1.0, BASE)]
        lines = layout_pieces(items, 100)
        assert lines[1].pieces[0][0] == 0.0


class TestBoxPiece:
    """F6: aankruisvak als ondeelbaar stuk; nooit samengevoegd met tekst of een ander vak."""

    def test_boxes_not_merged(self):
        from openaec_reports.core.rich_text import BoxStyle, box_piece

        box = BoxStyle(9.0, 0.85, "#000000", "#000000")
        items = [box_piece(True, box), Space(2.5), box_piece(False, box), *text_pieces(" ab", BASE)]
        line = layout_pieces(items, 100)[0]
        kinds = [(p.box is not None, p.checked, p.text) for _, p in line.pieces]
        assert kinds == [(True, True, ""), (True, False, ""), (False, False, "ab")]

    def test_plain_text_of_check(self):
        assert plain_text([{"check": True}, {"text": " klaar"}]) == "[x] klaar"
