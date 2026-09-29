"""Brand systeem — YAML-configureerbare merken met kleuren, fonts, header/footer.

Multi-tenant brand resolution:
    BrandLoader zoekt brands in volgorde:
    1. Tenant brand.yaml (via TenantConfig)
    2. Tenants root: ``<tenants_root>/<name>/brand.yaml``
    3. Package brands: ``assets/brands/<name>.yaml``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from openaec_reports.core.tenant import TenantConfig

logger = logging.getLogger(__name__)

# Standaard locatie van brand bestanden binnen het package
BRANDS_DIR = Path(__file__).parent.parent / "assets" / "brands"


@dataclass
class ElementConfig:
    """Enkel tekenbaar element in header/footer.

    Attrs:
        type: Element type ("rect", "text", "image", "line").
        x: Positie in mm vanaf linker paginarand.
        y: Positie in mm vanaf onderkant van de zone.
        width: Breedte in mm.
        height: Hoogte in mm.
        fill: Vulkleur — "$primary" of "#38BDA0".
        color: Tekst/lijn kleur — "$text" of hex.
        stroke: Outline kleur.
        stroke_width: Lijndikte in points.
        content: Tekst met {variabelen} (bijv. "{page}").
        src: Afbeelding pad relatief t.o.v. assets/.
        font: Font referentie — "$heading" of "Helvetica-Bold".
        size: Font grootte in points.
        align: Tekst uitlijning ("left", "center", "right").
    """

    type: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    fill: str = ""
    color: str = ""
    stroke: str = ""
    stroke_width: float = 0.5
    content: str = ""
    src: str = ""
    font: str = ""
    size: float = 0.0
    align: str = "left"


@dataclass
class ZoneConfig:
    """Header of footer zone configuratie.

    Attrs:
        height: Hoogte in mm. 0 betekent geen zone (wordt overgeslagen).
        elements: Lijst van tekenbare elementen.
    """

    height: float = 0.0
    elements: list[ElementConfig] = field(default_factory=list)


@dataclass
class StationeryPageConfig:
    """Stationery configuratie per paginatype."""

    source: str = ""
    header_footer: str | None = None
    text_zones: list[dict] = field(default_factory=list)
    content_frame: dict | None = None


@dataclass
class BrandConfig:
    """Volledige brand configuratie.

    Attrs:
        name: Weergavenaam van het merk.
        slug: Machine-leesbare identifier.
        tenant: Tenant identifier voor module registry lookup.
        tagline: Merk tagline.
        colors: Kleurenpalet (naam → hex waarde).
        fonts: Font mapping (naam → font naam).
        header: Header zone configuratie.
        footer: Footer zone configuratie.
        logos: Logo mapping (naam → relatief pad).
        stationery: Achtergrond-templates per paginatype.
        modules: Module-specifieke styling (table, calculation, etc.).
        tenant_modules: Beschikbare tenant-specifieke module types.
        module_config: Configuratie voor tenant modules (kleuren, sizes).
        brand_dir: Directory van het YAML bestand (voor relatieve paden).
    """

    name: str = "Default"
    slug: str = "default"
    tenant: str = ""
    tagline: str = ""
    colors: dict[str, str] = field(default_factory=dict)
    fonts: dict[str, str] = field(default_factory=dict)
    header: ZoneConfig = field(default_factory=ZoneConfig)
    footer: ZoneConfig = field(default_factory=ZoneConfig)
    logos: dict[str, str] = field(default_factory=dict)
    contact: dict[str, str] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    styles: dict[str, dict] = field(default_factory=dict)
    pages: dict[str, dict] = field(default_factory=dict)
    stationery: dict[str, StationeryPageConfig] = field(default_factory=dict)
    modules: dict[str, dict] = field(default_factory=dict)
    tenant_modules: list[str] = field(default_factory=list)
    module_config: dict = field(default_factory=dict)
    font_files: dict[str, str] = field(default_factory=dict)
    brand_dir: Path | None = None


def _parse_elements(raw_elements: list[dict] | None) -> list[ElementConfig]:
    """Parseer een lijst van ruwe element dicts naar ElementConfig objecten."""
    if not raw_elements:
        return []
    elements = []
    for elem_data in raw_elements:
        elements.append(
            ElementConfig(
                type=elem_data.get("type", ""),
                x=float(elem_data.get("x", 0)),
                y=float(elem_data.get("y", 0)),
                width=float(elem_data.get("width", 0)),
                height=float(elem_data.get("height", 0)),
                fill=str(elem_data.get("fill", "")),
                color=str(elem_data.get("color", "")),
                stroke=str(elem_data.get("stroke", "")),
                stroke_width=float(elem_data.get("stroke_width", 0.5)),
                content=str(elem_data.get("content", "")),
                src=str(elem_data.get("src", "")),
                font=str(elem_data.get("font", "")),
                size=float(elem_data.get("size", 0)),
                align=str(elem_data.get("align", "left")),
            )
        )
    return elements


def _parse_zone(raw_zone: dict | None) -> ZoneConfig:
    """Parseer een ruwe zone dict naar ZoneConfig."""
    if not raw_zone:
        return ZoneConfig()
    return ZoneConfig(
        height=float(raw_zone.get("height", 0)),
        elements=_parse_elements(raw_zone.get("elements")),
    )


class BrandLoader:
    """Laad en parseer YAML brand configuraties.

    Multi-tenant brand resolution zoekt in volgorde:
    1. Tenant brand (via TenantConfig.brand_path, als slug matcht)
    2. Tenants root: ``<tenants_root>/<name>/brand.yaml``
    3. Package brands: ``assets/brands/<name>.yaml``

    De tenants_root wordt automatisch gedetecteerd via:
    - Expliciete ``tenants_root`` parameter
    - ``TenantConfig.tenants_root``
    - ``OPENAEC_TENANTS_ROOT`` environment variable
    - Parent van ``OPENAEC_TENANT_DIR``

    Usage:
        loader = BrandLoader()
        brand = loader.load("default")   # vindt tenants/default/brand.yaml
        available = loader.list_brands()
    """

    def __init__(
        self,
        brands_dir: Path | None = None,
        tenant_config: TenantConfig | None = None,
        tenants_root: Path | None = None,
        tenant_slug: str = "",
    ):
        self._tenant_config = tenant_config
        self._tenant_slug = tenant_slug
        self.brands_dir = brands_dir or BRANDS_DIR

        # Tenants root: explicit → tenant_config → env var detectie
        if tenants_root:
            self._tenants_root = tenants_root
        elif tenant_config and hasattr(tenant_config, "tenants_root"):
            self._tenants_root = tenant_config.tenants_root
        else:
            from openaec_reports.core.tenant import detect_tenants_root

            self._tenants_root = detect_tenants_root()

    def load(self, name: str | None = None) -> BrandConfig:
        """Laad een brand configuratie op naam.

        Bij tenant_config: als name niet opgegeven, laad tenant brand.yaml.
        Anders zoek op naam in brands_dir.

        Args:
            name: Brand naam (zonder .yaml extensie). None = tenant default.

        Returns:
            BrandConfig met de geparseerde brand data.

        Raises:
            FileNotFoundError: Als het brand bestand niet gevonden wordt.
        """
        # Als tenant config beschikbaar en geen specifieke naam → tenant brand
        if name is None and self._tenant_config:
            path = self._tenant_config.brand_path
        elif name is None:
            path = self.brands_dir / "default.yaml"
        else:
            # Check tenant brand path eerst als naam overeenkomt
            if self._tenant_config:
                tenant_brand = self._tenant_config.brand_path
                if tenant_brand.exists():
                    # Lees slug uit tenant brand om te matchen
                    try:
                        with tenant_brand.open("r", encoding="utf-8") as f:
                            data = yaml.safe_load(f)
                        brand_info = data.get("brand", {}) if data else {}
                        slug = brand_info.get("slug", "")
                        if slug == name or tenant_brand.stem == name or name == "brand":
                            path = tenant_brand
                        else:
                            path = self._resolve_path(name)
                    except (yaml.YAMLError, OSError):
                        path = self._resolve_path(name)
                else:
                    path = self._resolve_path(name)
            else:
                path = self._resolve_path(name)

        if not path.exists():
            raise FileNotFoundError(f"Brand '{name}' niet gevonden in {self.brands_dir}")

        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if data is None:
            data = {}

        brand_info = data.get("brand", {})

        # Stationery parsen
        stationery = {}
        for page_type, spec in data.get("stationery", {}).items():
            stationery[page_type] = StationeryPageConfig(
                source=spec.get("source", ""),
                header_footer=spec.get("header_footer"),
                text_zones=spec.get("text_zones", []),
                content_frame=spec.get("content_frame"),
            )

        # Modules: dict (styling config) of list (tenant module types)
        raw_modules = data.get("modules", {})
        if isinstance(raw_modules, list):
            tenant_modules = raw_modules
            modules_dict: dict[str, dict] = {}
        else:
            tenant_modules = data.get("tenant_modules", [])
            modules_dict = raw_modules

        # $colors./$fonts.-refs in "pages" (bijv. pages.cover.static_elements,
        # zie core/static_elements.py) resolven vóórdat BrandConfig gebouwd
        # wordt. TemplateSet._load doet dit al voor template-YAML's (zie
        # core/refs.py); "pages" leeft in brand.yaml zelf en liep tot nu toe
        # NIET door die resolver — static_elements zou dus "$colors.primary"
        # als letterlijke string doorgeven i.p.v. de hex-waarde. colors/fonts
        # zijn op dit punt al bekend (regels hierboven), dus we resolven
        # tegen een lichte stand-in met diezelfde twee attributen.
        raw_pages = data.get("pages", {})
        if raw_pages:
            from openaec_reports.core.refs import resolve_refs

            _colors_fonts_ns = SimpleNamespace(
                colors=data.get("colors", {}), fonts=data.get("fonts", {})
            )
            raw_pages = resolve_refs(
                raw_pages,
                _colors_fonts_ns,
                tenant=brand_info.get("tenant", "") or brand_info.get("slug", name or ""),
                source="brand.yaml#pages",
            )

        return BrandConfig(
            name=brand_info.get("name", name),
            slug=brand_info.get("slug", name),
            tenant=brand_info.get("tenant", data.get("tenant", "")),
            tagline=brand_info.get("tagline", data.get("tagline", "")),
            colors=data.get("colors", {}),
            fonts=data.get("fonts", {}),
            header=_parse_zone(data.get("header")),
            footer=_parse_zone(data.get("footer")),
            logos=data.get("logos", {}),
            contact=data.get("contact", {}),
            status=data.get("status", {}) or {},
            styles=data.get("styles", {}),
            pages=raw_pages,
            stationery=stationery,
            modules=modules_dict,
            tenant_modules=tenant_modules,
            module_config=data.get("module_config", {}),
            font_files=data.get("font_files", {}),
            brand_dir=path.parent,
        )

    def load_default(self) -> BrandConfig:
        """Laad de standaard brand configuratie.

        Returns:
            BrandConfig voor het 'default' merk.
        """
        return self.load("default")

    def list_brands(self) -> list[dict[str, str]]:
        """Lijst beschikbare brands, strikt per tenant.

        Als een tenant_slug is ingesteld, wordt ALLEEN de eigen tenant
        brand getoond. Andere tenants' brands zijn nooit zichtbaar.

        Zonder tenant_slug worden alle brands gescand (backward compat).

        Returns:
            Lijst van dicts met 'name' en 'slug' per brand.
        """
        brands: list[dict[str, str]] = []
        seen_slugs: set[str] = set()

        # 1. Primaire tenant brand (altijd)
        if self._tenant_config:
            tenant_brand = self._tenant_config.brand_path
            if tenant_brand.exists():
                self._add_brand_from_file(
                    tenant_brand, brands, seen_slugs,
                )

        # Met tenant: alleen eigen brand, geen cross-tenant scan
        if self._tenant_slug:
            return brands

        # Zonder tenant (backward compat): scan alles
        if self._tenants_root and self._tenants_root.exists():
            for tenant_dir in sorted(self._tenants_root.iterdir()):
                brand_file = tenant_dir / "brand.yaml"
                if not tenant_dir.is_dir() or not brand_file.exists():
                    continue
                if tenant_dir.name.startswith("test_"):
                    continue
                self._add_brand_from_file(brand_file, brands, seen_slugs)

        if self.brands_dir.exists():
            for path in sorted(self.brands_dir.glob("*.yaml")):
                self._add_brand_from_file(path, brands, seen_slugs)

        return brands

    @staticmethod
    def _add_brand_from_file(
        path: Path,
        brands: list[dict[str, str]],
        seen_slugs: set[str],
    ) -> None:
        """Voeg brand info toe uit een YAML bestand (dedup op slug)."""
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            brand_info = data.get("brand", {}) if data else {}
            slug = brand_info.get("slug", path.stem)
            if slug in seen_slugs:
                return
            seen_slugs.add(slug)
            brands.append(
                {
                    "name": brand_info.get("name", path.stem),
                    "slug": slug,
                }
            )
        except (yaml.YAMLError, OSError):
            slug = path.stem
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                brands.append({"name": path.stem, "slug": slug})

    def _resolve_path(self, name: str) -> Path:
        """Resolve brand naam naar bestandspad.

        Met tenant_slug: zoekt ALLEEN eigen tenant brand in tenants_root.
        Zonder tenant_slug: zoekt in alle locaties (backward compat).

        Zoekt in volgorde:
        1. Tenants root: ``<tenants_root>/<name>/brand.yaml``
        2. Package brands directory layout: ``brands/<name>/brand.yaml``
        3. Package brands single file: ``brands/<name>.yaml``

        Args:
            name: Brand naam (slug) of bestandsnaam met .yaml extensie.

        Returns:
            Pad naar het brand YAML bestand.
        """
        if name.endswith(".yaml"):
            return self.brands_dir / name

        # Met tenant: alleen eigen tenant brand toestaan
        if self._tenant_slug:
            if name != self._tenant_slug:
                # Probeer niet eens andermans brand te laden
                return self.brands_dir / f"{name}.yaml"
            # Eigen tenant brand uit tenants_root
            if self._tenants_root:
                tenant_brand = self._tenants_root / name / "brand.yaml"
                if tenant_brand.exists():
                    return tenant_brand
            return self.brands_dir / f"{name}.yaml"

        # Zonder tenant (backward compat): zoek overal
        if self._tenants_root:
            tenant_brand = self._tenants_root / name / "brand.yaml"
            if tenant_brand.exists():
                return tenant_brand
        # Package brands directory layout
        dir_path = self.brands_dir / name / "brand.yaml"
        if dir_path.exists():
            return dir_path
        # Fallback naar single file
        return self.brands_dir / f"{name}.yaml"
