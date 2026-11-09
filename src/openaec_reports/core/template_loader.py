"""Template loader — YAML rapport templates inlezen en parsen."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Standaard locatie van templates binnen het package
TEMPLATES_DIR = Path(__file__).parent.parent / "assets" / "templates"


@dataclass
class TemplateConfig:
    """Geparseerde template configuratie.

    Attrs:
        name: Template bestandsnaam (zonder extensie).
        report_type: Type rapport (bijv. 'structural', 'daylight').
        format: Paginaformaat ('A4' of 'A3').
        orientation: Oriëntatie ('portrait' of 'landscape').
        margins: Marges in mm (dict met top, bottom, left, right).
        header: Header configuratie.
        footer: Footer configuratie.
        cover: Voorblad configuratie.
        colofon: Colofon configuratie.
        toc: Inhoudsopgave configuratie.
        backcover: Achterblad configuratie.
        structure: Volgorde van secties.
        raw: Volledige ongeparseerde YAML data.
    """

    name: str = ""
    report_type: str = ""
    tenant: str = ""
    format: str = "A4"
    orientation: str = "portrait"
    margins: dict[str, float] = field(
        default_factory=lambda: {
            "top": 25.0,
            "bottom": 20.0,
            "left": 20.0,
            "right": 15.0,
        }
    )
    header: dict[str, Any] = field(default_factory=dict)
    footer: dict[str, Any] = field(default_factory=dict)
    cover: dict[str, Any] = field(default_factory=dict)
    colofon: dict[str, Any] = field(default_factory=dict)
    toc: dict[str, Any] = field(default_factory=dict)
    backcover: dict[str, Any] = field(default_factory=dict)
    structure: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class TemplateLoader:
    """Laad en parseer YAML rapport templates.

    Zoekt templates in de standaard assets/templates/ directory,
    of in een custom directory. Ondersteunt meerdere directories
    (tenant-specifiek + package defaults).

    Usage:
        loader = TemplateLoader()
        config = loader.load("structural_report")
        available = loader.list_templates()

        # Multi-tenant: meerdere directories
        loader = TemplateLoader(templates_dirs=[tenant_dir, package_dir])
    """

    def __init__(
        self,
        templates_dir: Path | None = None,
        templates_dirs: list[Path] | None = None,
        tenant_slug: str = "",
    ):
        if templates_dirs:
            self._templates_dirs = templates_dirs
        else:
            self._templates_dirs = [templates_dir or TEMPLATES_DIR]
        # Backward compat: eerste directory is de "primaire"
        self.templates_dir = self._templates_dirs[0]
        self._tenant_slug = tenant_slug

    def load(self, name: str) -> TemplateConfig:
        """Laad een template op naam.

        Zoekt in alle geconfigureerde directories (tenant eerst, dan package).

        Args:
            name: Template naam (zonder .yaml extensie).

        Returns:
            TemplateConfig met de geparseerde template data.

        Raises:
            FileNotFoundError: Als het template niet gevonden wordt.
            yaml.YAMLError: Als het YAML bestand ongeldig is.
        """
        path = self._resolve_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Template '{name}' niet gevonden in {self._templates_dirs}")

        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if data is None:
            data = {}

        return TemplateConfig(
            name=name,
            report_type=data.get("report_type", ""),
            tenant=data.get("tenant", ""),
            format=data.get("format", "A4"),
            orientation=data.get("orientation", "portrait"),
            margins=data.get(
                "margins",
                {
                    "top": 25.0,
                    "bottom": 20.0,
                    "left": 20.0,
                    "right": 15.0,
                },
            ),
            header=data.get("header", {}),
            footer=data.get("footer", {}),
            cover=data.get("cover", {}),
            colofon=data.get("colofon", {}),
            toc=data.get("toc", {}),
            backcover=data.get("backcover", {}),
            structure=data.get("structure", []),
            raw=data,
        )

    def list_templates(self) -> list[dict[str, str]]:
        """Lijst beschikbare templates, strikt per tenant.

        Als een tenant_slug is ingesteld, worden ALLEEN templates uit de
        tenant-directory getoond (eerste dir in ``_templates_dirs``).
        Fallback-directories worden niet gescand — een tenant ziet
        uitsluitend eigen templates.

        Zonder tenant_slug worden alle directories gescand (backward compat).

        Returns:
            Lijst van dicts met 'name' en 'report_type' per template.
        """
        seen: set[str] = set()
        templates = []

        # Met tenant: alleen eerste directory (= tenant templates)
        dirs_to_scan = self._templates_dirs
        if self._tenant_slug and len(self._templates_dirs) > 1:
            dirs_to_scan = self._templates_dirs[:1]

        for tdir in dirs_to_scan:
            if not tdir.exists():
                continue
            for path in sorted(tdir.glob("*.yaml")):
                if path.stem in seen:
                    continue
                seen.add(path.stem)
                try:
                    with path.open("r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    templates.append(
                        {
                            "name": path.stem,
                            "report_type": data.get("report_type", "") if data else "",
                        }
                    )
                except yaml.YAMLError:
                    templates.append(
                        {
                            "name": path.stem,
                            "report_type": "(ongeldig YAML)",
                        }
                    )

        return templates

    def to_scaffold(self, name: str) -> dict[str, Any]:
        """Genereer een leeg JSON scaffold vanuit een template.

        Het scaffold bevat alle metadata velden met defaults uit het template,
        cover/colofon/toc/backcover configuratie, en een lege sections array.
        Geschikt als startpunt voor de frontend.

        Args:
            name: Template naam.

        Returns:
            Dict conform report.schema.json, klaar voor de frontend.

        Raises:
            FileNotFoundError: Als het template niet gevonden wordt.
        """
        config = self.load(name)
        today = date.today().isoformat()

        default_disclaimer = (
            "Dit rapport is uitsluitend bedoeld voor de opdrachtgever. "
            "Verspreiding aan derden is niet toegestaan zonder schriftelijke "
            "toestemming van de auteur."
        )

        # Colofon
        colofon_raw = config.colofon
        colofon_enabled = colofon_raw.get("enabled", True) if colofon_raw else False
        disclaimer = colofon_raw.get("disclaimer", "").strip() if colofon_raw else ""
        if not disclaimer:
            disclaimer = default_disclaimer

        colofon: dict[str, Any] = {
            "enabled": colofon_enabled,
            "opdrachtgever_naam": "",
            "opdrachtgever_contact": "",
            "opdrachtgever_adres": "",
            "adviseur_bedrijf": "",
            "adviseur_naam": "",
            "adviseur_email": "",
            "adviseur_telefoon": "",
            "adviseur_functie": "",
            "adviseur_registratie": "",
            "normen": "",
            "documentgegevens": "",
            "datum": today,
            "fase": "",
            "status_colofon": "CONCEPT",
            "kenmerk": "",
            "extra_fields": {},
            "revision_history": [
                {
                    "version": "0.1",
                    "date": today,
                    "author": "",
                    "description": "Eerste opzet",
                },
            ],
            "disclaimer": disclaimer,
        }

        # TOC
        toc_raw = config.toc
        toc: dict[str, Any] = {
            "enabled": toc_raw.get("enabled", True) if toc_raw else False,
            "title": toc_raw.get("title", "Inhoudsopgave") if toc_raw else "Inhoudsopgave",
            "max_depth": toc_raw.get("max_depth", 3) if toc_raw else 3,
        }

        # Cover
        cover_raw = config.cover
        cover: dict[str, Any] = {
            "subtitle": cover_raw.get("subtitle_hint", "") if cover_raw else "",
        }

        # Backcover
        backcover_raw = config.backcover
        backcover: dict[str, Any] = {
            "enabled": backcover_raw.get("enabled", True) if backcover_raw else False,
        }

        sections = self._build_scaffold_sections(config)

        scaffold: dict[str, Any] = {
            "template": name,
            "format": config.format,
            "orientation": config.orientation,
            "project": "",
            "project_number": "",
            "client": "",
            "author": "",
            "brand": (
                config.tenant
                if config.tenant
                else os.environ.get("OPENAEC_DEFAULT_BRAND", "default")
            ),
            "date": today,
            "version": "1.0",
            "status": "CONCEPT",
            "report_type": config.report_type,
            "cover": cover,
            "colofon": colofon,
            "toc": toc,
            "sections": sections,
            "backcover": backcover,
            "metadata": {},
        }

        # Template engine templates: extract field groups uit page_types
        # Detectie: pages is lijst van dicts (V3), niet strings (legacy)
        raw_pages = config.raw.get("pages", [])
        is_template_engine = (
            raw_pages
            and isinstance(raw_pages, list)
            and isinstance(raw_pages[0], dict)
        )
        if is_template_engine:
            field_groups, flat_data = self._extract_field_groups(
                name, config.tenant,
            )
            if field_groups:
                scaffold["field_groups"] = field_groups
                scaffold.update(flat_data)

        return scaffold

    def _build_scaffold_sections(
        self, config: TemplateConfig
    ) -> list[dict[str, Any]]:
        """Genereer default secties op basis van het template type.

        BIC Rapport templates gebruiken field_groups (formulier-velden),
        niet content-block secties. Retourneert een lege lijst.
        Overige templates retourneren ook een lege lijst
        (gebruiker voegt zelf secties toe).
        """
        return []

    def _extract_field_groups(
        self,
        template_name: str,
        tenant: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Extraheer veldgroepen uit page_type YAML's en genereer sample data.

        Scant alle page_types in de template, extraheert data_bind velden,
        groepeert per prefix, en bepaalt input_type heuristisch.

        Args:
            template_name: Template naam.
            tenant: Tenant identifier.

        Returns:
            Tuple van (field_groups metadata, flat_data dicts met sample waarden).
        """
        from openaec_reports.core.template_resolver import TemplateResolver

        # Resolve tenants dir vanuit templates_dirs
        tenants_dir = self._resolve_tenants_dir()
        if not tenants_dir:
            logger.warning("Kon tenants dir niet resolven voor field group extraction")
            return [], {}

        resolver = TemplateResolver(tenants_dir=tenants_dir)
        try:
            template_config = resolver.load_template(template_name, tenant)
        except FileNotFoundError:
            logger.warning("Template %s niet gevonden voor tenant %s", template_name, tenant)
            return [], {}

        page_types = resolver.load_all_page_types(template_config)

        # Laad sample data uit example JSON (indien aanwezig)
        sample_data = self._load_sample_data(template_name)

        # Verzamel alle data_binds per page_type
        all_binds: list[tuple[str, str, str]] = []  # (page_type_name, bind, source)
        table_binds: list[tuple[str, dict[str, Any]]] = []  # (page_type_name, table_info)

        for pt_name, pt in page_types.items():
            # Skip special pages (voorblad, achterblad)
            page_def = next(
                (p for p in template_config.pages if p.page_type == pt_name),
                None,
            )

            for tz in pt.text_zones:
                bind = tz.bind
                # Skip static labels en page numbers
                if bind.startswith("_static.") or bind == "_page_number":
                    continue
                # Skip footer zones (y > 265mm portrait, y > 185mm landscape)
                orientation = page_def.orientation if page_def else "portrait"
                footer_threshold = 185.0 if orientation == "landscape" else 265.0
                if tz.y_mm > footer_threshold:
                    # Maar bewaar client.name en meta.rapportkop_locatie NIET als footer
                    if bind in ("client.name", "meta.rapportkop_locatie", "_page_number"):
                        continue
                all_binds.append((pt_name, bind, "text"))

            for iz in pt.image_zones:
                all_binds.append((pt_name, iz.bind, "image"))

            if pt.table:
                table_binds.append((pt_name, {
                    "data_bind": pt.table.data_bind,
                    "columns": [
                        {
                            "field": col.field,
                            "width_mm": col.width_mm,
                            "header": col.header or col.field,
                        }
                        for col in pt.table.columns
                    ],
                }))

        # Groepeer per prefix
        groups_by_prefix: dict[str, list[dict[str, Any]]] = defaultdict(list)
        group_page_type: dict[str, str] = {}  # prefix → eerste page_type

        for pt_name, bind, source in all_binds:
            parts = bind.split(".", 1)
            if len(parts) == 2:
                prefix, field_name = parts
            else:
                prefix = bind
                field_name = bind

            if prefix not in group_page_type:
                group_page_type[prefix] = pt_name

            # Deduplicatie
            if not any(f["bind"] == bind for f in groups_by_prefix[prefix]):
                groups_by_prefix[prefix].append({
                    "bind": bind,
                    "field": field_name,
                    "label": _humanize_field_name(field_name),
                    "input_type": _infer_input_type(bind, source),
                })

        # Bouw field_groups lijst
        field_groups: list[dict[str, Any]] = []

        # Vaste volgorde voor BIC rapport groepen
        group_order = [
            "meta", "client", "location", "voorziening", "object", "vvv",
            "controleur", "bic", "reiniging", "additioneel", "samenvatting",
            "herstel", "herstel_reiniging", "herstel_additioneel",
            "heading", "subheading", "tekening",
            "controlelijst", "inspecties", "foto_bijlage",
        ]
        # Voeg groepen toe die niet in de vaste volgorde staan
        remaining = [k for k in groups_by_prefix if k not in group_order and k != "toc"]
        ordered_prefixes = [p for p in group_order if p in groups_by_prefix] + remaining

        for prefix in ordered_prefixes:
            fields = groups_by_prefix[prefix]
            field_groups.append({
                "key": prefix,
                "label": _GROUP_LABELS.get(prefix, _humanize_field_name(prefix)),
                "page_type": group_page_type.get(prefix, ""),
                "fields": fields,
            })

        # Voeg tabel-groepen toe
        for pt_name, table_info in table_binds:
            table_key = table_info["data_bind"]
            field_groups.append({
                "key": table_key,
                "label": _GROUP_LABELS.get(table_key, _humanize_field_name(table_key)),
                "page_type": pt_name,
                "type": "table",
                "columns": table_info["columns"],
            })

        # Bouw flat data dicts met sample waarden
        flat_data = self._build_flat_data_from_groups(field_groups, sample_data)

        return field_groups, flat_data

    def _resolve_tenants_dir(self) -> Path | None:
        """Resolve de tenants root directory."""
        import os

        # Check env var
        env_root = os.environ.get("OPENAEC_TENANTS_ROOT")
        if env_root:
            p = Path(env_root)
            if p.is_dir():
                return p

        env_dir = os.environ.get("OPENAEC_TENANT_DIR")
        if env_dir:
            p = Path(env_dir).parent
            if p.is_dir():
                return p

        # Probeer vanuit templates_dir naar tenants dir te navigeren
        # templates_dir = tenants/{slug}/templates → parent.parent = tenants/
        for tdir in self._templates_dirs:
            candidate = tdir.parent.parent
            if candidate.name == "tenants" and candidate.is_dir():
                return candidate
            # Source tree fallback
            candidate = tdir.parent.parent.parent.parent / "tenants"
            if candidate.is_dir():
                return candidate

        return None

    @staticmethod
    def _load_sample_data(template_name: str) -> dict[str, Any]:
        """Laad voorbeeld data uit schemas/ directory.

        Zoekt in meerdere locaties om zowel source tree als
        geïnstalleerd package (Docker) te ondersteunen.
        """
        import json as _json

        search_dirs = [
            # Source tree (lokaal development)
            Path(__file__).parent.parent.parent.parent / "schemas",
            # Docker: /app/schemas/
            Path("/app/schemas"),
            # CWD fallback
            Path.cwd() / "schemas",
        ]

        filenames = [
            f"example_{template_name}.json",
            f"example_customer_{template_name}.json",
        ]

        for schemas_dir in search_dirs:
            if not schemas_dir.is_dir():
                continue
            for filename in filenames:
                path = schemas_dir / filename
                if path.exists():
                    try:
                        with path.open("r", encoding="utf-8") as f:
                            return _json.load(f)
                    except (_json.JSONDecodeError, OSError):
                        pass
        return {}

    @staticmethod
    def _build_flat_data_from_groups(
        field_groups: list[dict[str, Any]],
        sample_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Bouw flat data dicts op basis van field_groups met sample waarden."""
        flat_data: dict[str, Any] = {}

        for group in field_groups:
            key = group["key"]

            if group.get("type") == "table":
                # Tabel: neem sample data als array, of lege lijst
                flat_data[key] = sample_data.get(key, [])
                continue

            # Scalar groep: bouw dict met velden
            group_data: dict[str, Any] = {}
            sample_group = sample_data.get(key, {})

            if isinstance(sample_group, dict):
                for field_def in group.get("fields", []):
                    field_name = field_def["field"]
                    group_data[field_name] = sample_group.get(field_name, "")
            else:
                # Fallback: lege strings voor alle velden
                for field_def in group.get("fields", []):
                    group_data[field_def["field"]] = ""

            flat_data[key] = group_data

        # Voeg TOC data toe indien aanwezig in sample
        if "toc" in sample_data and isinstance(sample_data["toc"], dict):
            flat_data["toc"] = sample_data["toc"]

        return flat_data

    def _resolve_path(self, name: str) -> Path:
        """Resolve template naam naar bestandspad.

        Met tenant_slug: zoekt ALLEEN in de eerste directory (tenant dir).
        Zonder tenant_slug: zoekt in alle directories (backward compat).

        Bij niet gevonden, retourneer het pad in de eerste directory
        (voor foutmelding).
        """
        filename = name if name.endswith(".yaml") else f"{name}.yaml"

        # Met tenant: alleen eigen tenant directory (= eerste dir)
        dirs_to_search = self._templates_dirs
        if self._tenant_slug and len(self._templates_dirs) > 1:
            dirs_to_search = self._templates_dirs[:1]

        for tdir in dirs_to_search:
            candidate = tdir / filename
            if candidate.exists():
                return candidate
        # Niet gevonden — retourneer pad in eerste dir voor foutmelding
        return self._templates_dirs[0] / filename


# ============================================================
# Helper functies voor field group extraction
# ============================================================

# Display labels per groep-prefix
_GROUP_LABELS: dict[str, str] = {
    "meta": "Voorblad",
    "client": "Opdrachtgever",
    "location": "Locatie",
    "voorziening": "Voorziening",
    "object": "Object",
    "vvv": "Verklaring Vloeistofdichte Voorziening",
    "controleur": "Controleur",
    "bic": "BIC Details",
    "reiniging": "Reiniging",
    "additioneel": "Additioneel",
    "samenvatting": "Kostenoverzicht",
    "herstel": "Herstelwerkzaamheden",
    "herstel_reiniging": "Herstel — Reiniging",
    "herstel_additioneel": "Herstel — Additioneel",
    "heading": "Tekening kop",
    "subheading": "Tekening subkop",
    "tekening": "Tekening",
    "controlelijst": "Controlelijst",
    "controlelijst_items": "Controlelijst BIC",
    "inspecties": "Onderhoudsinspecties",
    "inspecties_items": "Inspectie historie",
    "historie_bic_items": "Historie BIC controles",
    "historie_herstel_items": "Historie herstelwerkzaamheden",
    "foto_bijlage": "Fotobijlage",
    "toc": "Inhoudsopgave",
}


def _humanize_field_name(name: str) -> str:
    """Maak een veldnaam leesbaar: underscore → spatie, capitalize."""
    return name.replace("_", " ").capitalize()


def _infer_input_type(bind: str, source: str) -> str:
    """Bepaal input type op basis van veldnaam heuristiek."""
    if source == "image":
        return "image"

    lower = bind.lower()
    if any(kw in lower for kw in ("datum", "date", "geldigheid")):
        return "text"  # Datums zijn vrij formaat in BIC (bijv. "Maart 2026")
    if any(kw in lower for kw in ("kosten", "subtotaal", "totaal", "aantal")):
        return "number"
    if any(kw in lower for kw in ("notitie", "opmerkingen", "bijzonderheden", "context")):
        return "textarea"
    if any(kw in lower for kw in ("foto", "photo", "image")):
        return "image"
    return "text"
