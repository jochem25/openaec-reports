"""Integratietests voor Report.from_json() en Report.from_dict()."""

from __future__ import annotations

from pathlib import Path

from reportlab.platypus import Flowable

from openaec_reports import Report

EXAMPLE_JSON = Path(__file__).parent.parent / "schemas" / "example_structural.json"


class TestFromJson:
    """Tests voor Report.from_json() met het bestaande example bestand."""

    def test_from_json_example(self):
        """Parse example_structural.json naar een Report object."""
        report = Report.from_json(EXAMPLE_JSON)
        assert isinstance(report, Report)

    def test_from_json_metadata(self):
        """Project metadata wordt correct ingelezen."""
        report = Report.from_json(EXAMPLE_JSON)
        assert report.document.config.project == "Kijkduin Reddingspost"
        assert report.document.config.project_number == "2024-031"
        assert report.document.config.client == "Gemeente Den Haag"
        assert report.document.config.author == "Default Author"

    def test_from_json_extra_metadata(self):
        """Extra metadata velden (date, version, status) worden ingelezen."""
        report = Report.from_json(EXAMPLE_JSON)
        assert report.date == "2026-02-18"
        assert report.version == "1.0"
        assert report.status == "CONCEPT"
        assert report._metadata.get("software") == "OpenAEC Report Generator"

    def test_from_json_cover(self):
        """Cover is ingesteld met subtitle."""
        report = Report.from_json(EXAMPLE_JSON)
        assert report._has_cover is True
        assert report.document.config.subtitle == "Constructieve berekening hoofddraagconstructie"

    def test_from_json_sections_count(self):
        """Juiste aantal secties wordt aangemaakt."""
        report = Report.from_json(EXAMPLE_JSON)
        # example_structural.json heeft 6 secties
        assert len(report._sections) == 6

    def test_from_json_content_blocks(self):
        """Secties bevatten Flowable objecten, geen raw dicts."""
        report = Report.from_json(EXAMPLE_JSON)
        for section in report._sections:
            for item in section["content"]:
                assert isinstance(item, Flowable), (
                    f"Content item in sectie '{section['title']}' is {type(item)}, "
                    f"verwacht Flowable"
                )

    def test_from_json_page_break_before(self):
        """page_break_before wordt correct opgeslagen."""
        report = Report.from_json(EXAMPLE_JSON)
        # Sectie "Staalligger L1 — HEA 200" heeft page_break_before=true
        staal_section = next(
            s for s in report._sections if "Staalligger" in s["title"]
        )
        assert staal_section["page_break_before"] is True

        # Eerste sectie heeft geen page_break_before
        assert report._sections[0].get("page_break_before", False) is False

    def test_from_json_backcover(self):
        """Backcover is enabled."""
        report = Report.from_json(EXAMPLE_JSON)
        assert report._has_backcover is True

    def test_from_json_toc_config(self):
        """TOC configuratie wordt correct ingelezen."""
        report = Report.from_json(EXAMPLE_JSON)
        assert report._has_toc is True
        assert report._toc_max_depth == 3

    def test_from_json_colofon_stored(self):
        """Colofon data wordt opgeslagen."""
        report = Report.from_json(EXAMPLE_JSON)
        assert report._colofon is not None
        assert report._colofon.get("enabled") is True
        assert len(report._colofon.get("revision_history", [])) == 2

    def test_from_json_builds_pdf(self, tmp_path):
        """from_json → build → geldige PDF."""
        report = Report.from_json(EXAMPLE_JSON)
        output = tmp_path / "from_json_test.pdf"
        result = report.build(output)

        assert result.exists()
        assert result.stat().st_size > 1000
        # Check PDF magic bytes
        with open(result, "rb") as f:
            assert f.read(4) == b"%PDF"


class TestFromDict:
    """Tests voor Report.from_dict() — directe dict input (API use case)."""

    def test_from_dict(self):
        """Directe dict input creëert correct Report object."""
        data = {
            "template": "structural",
            "project": "Test Project",
            "project_number": "2026-099",
            "client": "Test Client",
            "sections": [
                {
                    "title": "Sectie 1",
                    "content": [
                        {"type": "paragraph", "text": "Tekst in sectie."},
                    ],
                },
            ],
        }
        report = Report.from_dict(data)
        assert report.document.config.project == "Test Project"
        assert len(report._sections) == 1
        assert len(report._sections[0]["content"]) == 1

    def test_from_dict_minimal(self):
        """Minimale dict (alleen template + project) werkt."""
        data = {
            "template": "custom",
            "project": "Minimaal Project",
        }
        report = Report.from_dict(data)
        assert report.document.config.project == "Minimaal Project"
        assert len(report._sections) == 0
        assert report._has_cover is False
        assert report._has_backcover is False

    def test_from_dict_builds_pdf(self, tmp_path):
        """from_dict met secties bouwt valide PDF."""
        data = {
            "template": "structural",
            "project": "Dict Test",
            "sections": [
                {
                    "title": "Test Sectie",
                    "content": [
                        {"type": "paragraph", "text": "Hallo wereld."},
                        {"type": "spacer", "height_mm": 10},
                        {"type": "paragraph", "text": "Tweede paragraaf."},
                    ],
                },
            ],
        }
        report = Report.from_dict(data)
        output = tmp_path / "from_dict_test.pdf"
        result = report.build(output)

        assert result.exists()
        assert result.stat().st_size > 500

    def test_from_dict_a3_format(self):
        """A3 format wordt correct verwerkt."""
        data = {
            "template": "custom",
            "project": "A3 Project",
            "format": "A3",
        }
        report = Report.from_dict(data)
        assert report.document.config.format.name == "A3"

    def test_from_dict_cover_and_backcover(self):
        """Cover en backcover worden correct verwerkt vanuit dict."""
        data = {
            "template": "custom",
            "project": "Cover Test",
            "cover": {
                "subtitle": "Test Subtitle",
            },
            "backcover": {"enabled": True},
        }
        report = Report.from_dict(data)
        assert report._has_cover is True
        assert report.document.config.subtitle == "Test Subtitle"
        assert report._has_backcover is True

    def test_from_dict_toc_disabled(self):
        """TOC kan uitgeschakeld worden."""
        data = {
            "template": "custom",
            "project": "No TOC",
            "toc": {"enabled": False},
        }
        report = Report.from_dict(data)
        assert report._has_toc is False

    def test_from_dict_toc_custom_title(self):
        """TOC titel kan aangepast worden."""
        data = {
            "template": "custom",
            "project": "Custom TOC",
            "toc": {"enabled": True, "title": "Table of Contents"},
        }
        report = Report.from_dict(data)
        assert report._toc_title == "Table of Contents"

    def test_from_dict_backcover_disabled(self):
        """Backcover disabled wordt gerespecteerd."""
        data = {
            "template": "custom",
            "project": "No Backcover",
            "backcover": {"enabled": False},
        }
        report = Report.from_dict(data)
        assert report._has_backcover is False
