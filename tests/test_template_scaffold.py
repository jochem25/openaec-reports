"""Tests voor TemplateLoader.to_scaffold() — template → JSON scaffold."""

from __future__ import annotations

from datetime import date

import pytest

from openaec_reports.core.template_loader import TemplateLoader


class TestScaffoldStructuralReport:
    """Tests voor scaffold van het structural_report template."""

    def test_scaffold_has_required_fields(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["template"] == "structural_report"
        assert "project" in scaffold
        assert "sections" in scaffold
        assert isinstance(scaffold["sections"], list)

    def test_scaffold_has_cover(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert "cover" in scaffold
        assert "subtitle" in scaffold["cover"]

    def test_scaffold_has_colofon_with_disclaimer(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["colofon"]["enabled"] is True
        assert len(scaffold["colofon"]["disclaimer"]) > 0

    def test_scaffold_has_revision_history(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        revisions = scaffold["colofon"]["revision_history"]
        assert len(revisions) == 1
        assert revisions[0]["version"] == "0.1"
        assert revisions[0]["description"] == "Eerste opzet"

    def test_scaffold_has_toc(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["toc"]["enabled"] is True
        assert scaffold["toc"]["title"] == "Inhoudsopgave"
        assert scaffold["toc"]["max_depth"] == 3

    def test_scaffold_has_backcover(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["backcover"]["enabled"] is True

    def test_scaffold_has_today_date(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["date"] == date.today().isoformat()

    def test_scaffold_has_metadata_defaults(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["project"] == ""
        assert scaffold["project_number"] == ""
        assert scaffold["client"] == ""
        assert scaffold["author"] == ""
        assert scaffold["version"] == "1.0"
        assert scaffold["status"] == "CONCEPT"

    def test_scaffold_sections_empty(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["sections"] == []

    def test_scaffold_report_type(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["report_type"] == "structural"

    def test_scaffold_format_and_orientation(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("structural_report")
        assert scaffold["format"] == "A4"
        assert scaffold["orientation"] == "portrait"


class TestScaffoldBlank:
    """Tests voor scaffold van het blank template."""

    def test_scaffold_blank_colofon_disabled(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("blank")
        assert scaffold["colofon"]["enabled"] is False

    def test_scaffold_blank_toc_disabled(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("blank")
        assert scaffold["toc"]["enabled"] is False

    def test_scaffold_blank_backcover_disabled(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("blank")
        assert scaffold["backcover"]["enabled"] is False

    def test_scaffold_blank_report_type(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("blank")
        assert scaffold["report_type"] == "custom"


class TestScaffoldDaylight:
    """Tests voor scaffold van het daylight template."""

    def test_scaffold_daylight_report_type(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("daylight")
        assert scaffold["report_type"] == "daylight"

    def test_scaffold_daylight_toc_max_depth(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("daylight")
        assert scaffold["toc"]["max_depth"] == 2

    def test_scaffold_daylight_subtitle_hint(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("daylight")
        assert scaffold["cover"]["subtitle"] == "Daglichtberekening"


class TestScaffoldBuildingCode:
    """Tests voor scaffold van het building_code template."""

    def test_scaffold_building_code_report_type(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("building_code")
        assert scaffold["report_type"] == "building_code"

    def test_scaffold_building_code_colofon_enabled(self):
        loader = TemplateLoader()
        scaffold = loader.to_scaffold("building_code")
        assert scaffold["colofon"]["enabled"] is True


class TestScaffoldErrors:
    """Tests voor foutafhandeling."""

    def test_scaffold_nonexistent_template(self):
        loader = TemplateLoader()
        with pytest.raises(FileNotFoundError):
            loader.to_scaffold("does_not_exist")
