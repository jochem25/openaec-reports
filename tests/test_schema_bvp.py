"""Schema-tests voor de BVP-uitbreidingen (E2-E9) in report.schema.json.

Valideert zoals ``/api/validate``: Draft7 tegen ``schemas/report.schema.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "report.schema.json"


@pytest.fixture(scope="module")
def validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    return jsonschema.Draft7Validator(schema)


def report(*blocks: dict) -> dict:
    return {
        "project": "Test",
        "template": "standaard",
        "sections": [{"title": "Hoofdstuk", "content": list(blocks)}],
    }


def errors(validator, data: dict) -> list[str]:
    return [e.message for e in validator.iter_errors(data)]


class TestRuns:
    def test_paragraph_with_runs_only(self, validator):
        block = {
            "type": "paragraph",
            "runs": [
                {"text": "Monitoringsplan: "},
                {"text": "grootheden", "italic": True, "color": "grijs"},
                {"label": {"text": "n.t.b.", "kind": "ntb"}},
            ],
        }
        assert errors(validator, report(block)) == []

    def test_paragraph_text_still_valid(self, validator):
        assert errors(validator, report({"type": "paragraph", "text": "a"})) == []

    def test_paragraph_needs_text_or_runs(self, validator):
        assert errors(validator, report({"type": "paragraph"}))

    def test_run_needs_text_or_label(self, validator):
        block = {"type": "paragraph", "runs": [{"bold": True}]}
        assert errors(validator, report(block))

    def test_unknown_label_kind_rejected(self, validator):
        block = {"type": "paragraph", "runs": [{"label": {"text": "x", "kind": "zzz"}}]}
        assert errors(validator, report(block))

    def test_bullet_items_mixed(self, validator):
        block = {
            "type": "bullet_list",
            "items": ["tekst", {"text": "object"}, {"runs": [{"text": "runs", "bold": True}]}],
        }
        assert errors(validator, report(block)) == []


class TestTable:
    def test_table_without_headers(self, validator):
        assert errors(validator, report({"type": "table", "rows": [["a", "b"]]})) == []

    def test_table_cells_rows_and_cell_styles(self, validator):
        block = {
            "type": "table",
            "headers": ["Aspect", "Score"],
            "rows": [
                ["Groep", ""],
                ["Bouwput", {"text": "3", "bg_color": "#FAD7B5", "align": "center"}],
                [{"runs": [{"text": "x"}, {"label": {"text": "n.t.b."}}]}, 2, None],
            ],
            "row_styles": [{"row": 0, "style": "group"}],
            "cell_styles": {"1,0": {"italic": True, "color": "grijs"}},
        }
        assert errors(validator, report(block)) == []

    def test_unknown_row_style_rejected(self, validator):
        block = {"type": "table", "rows": [["a"]], "row_styles": [{"row": 0, "style": "x"}]}
        assert errors(validator, report(block))

    def test_bad_align_rejected(self, validator):
        block = {"type": "table", "rows": [[{"text": "a", "align": "justify"}]]}
        assert errors(validator, report(block))


class TestChecklist:
    def test_checklist(self, validator):
        block = {
            "type": "checklist",
            "columns": 2,
            "items": [
                {"text": "Vooropname", "checked": True},
                {"text": "Nulmeting", "checked": False},
                {"runs": [{"text": "24/7 bereikbaar"}], "checked": None},
            ],
        }
        assert errors(validator, report(block)) == []

    def test_three_columns_rejected(self, validator):
        block = {"type": "checklist", "columns": 3, "items": [{"text": "a"}]}
        assert errors(validator, report(block))

    def test_item_needs_text_or_runs(self, validator):
        block = {"type": "checklist", "items": [{"checked": True}]}
        assert errors(validator, report(block))


class TestDefinitionList:
    def test_definition_list(self, validator):
        block = {
            "type": "definition_list",
            "label_width_mm": 40,
            "rows": [
                {"label": "Project", "value": "Parkview"},
                {"label": "Aannemer", "runs": [{"label": {"text": "n.t.b."}}]},
                {"label": "Leeg", "value": None},
            ],
        }
        assert errors(validator, report(block)) == []

    def test_row_needs_label(self, validator):
        block = {"type": "definition_list", "rows": [{"value": "x"}]}
        assert errors(validator, report(block))
