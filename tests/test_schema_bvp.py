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
