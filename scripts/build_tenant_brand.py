"""Genereer ``tenants/<tenant>/brand.yaml`` uit de canonieke huisstijlbron.

Voor KBA is de enige bron van waarheid ``kba-brand.json`` (kleuren,
typografie, bedrijfsgegevens) in de huisstijl-repo op de share
(``X:\\10_3BM_bouwkunde\\10_huisstijl\\KBA\\brand\\kba-brand.json``). Dat pad
staat *niet* hardcoded in deze repo — geef het mee via ``--source`` of zet de
omgevingsvariabele ``KBA_BRAND_SOURCE``.

Niet elk veld dat renderer_v2 verwacht is afleidbaar uit die bron (pagina-
geometrie, stationery-referenties, module-styling). Die secties staan
handmatig onderhouden in ``tenants/<tenant>/brand.base.yaml`` en worden
hier ongewijzigd overgenomen ("gemerged") in de output.

Usage:
    # eenmalig, of in PowerShell-profiel:
    export KBA_BRAND_SOURCE="X:/10_3BM_bouwkunde/10_huisstijl/KBA/brand/kba-brand.json"

    python scripts/build_tenant_brand.py --tenant kba
    python scripts/build_tenant_brand.py --tenant kba --source <pad>
    python scripts/build_tenant_brand.py --tenant kba --check   # CI-guard, exit 1 bij drift
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TENANTS_DIR = REPO_ROOT / "tenants"

# Secties die 1-op-1 uit brand.base.yaml worden overgenomen (niet afleidbaar
# uit kba-brand.json — layout-geometrie, stationery-referenties, assets).
_BASE_SECTIONS = (
    "logos",
    "header",
    "footer",
    "styles",
    "pages",
    "stationery",
    "modules",
    "tenant_modules",
    "module_config",
    "font_files",
)

_GENERATED_HEADER = """\
# ============================================================
# GEGENEREERD BESTAND — NIET MET DE HAND BEWERKEN
# ============================================================
# Bron:          {source}
# Regenereren:   python scripts/build_tenant_brand.py --tenant {tenant} --source {source}
# Niet-afleidbare secties ({base_sections}) komen uit
# tenants/{tenant}/brand.base.yaml — dat bestand blijft wél handmatig
# onderhouden. Zie ook: X:\\10_3BM_bouwkunde\\10_huisstijl\\KBA\\README.md
# ============================================================

"""


def load_json_source(path: Path) -> dict[str, Any]:
    """Laad kba-brand.json. Faalt hard met een duidelijke melding."""
    if not path.exists():
        raise FileNotFoundError(
            f"Huisstijlbron niet gevonden: {path}\n"
            "Geef --source mee, of zet de omgevingsvariabele KBA_BRAND_SOURCE."
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_base(tenant_dir: Path) -> dict[str, Any]:
    """Laad de handmatig onderhouden brand.base.yaml (niet-afleidbare secties)."""
    base_path = tenant_dir / "brand.base.yaml"
    if not base_path.exists():
        return {}
    with base_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_colors(kleuren: dict[str, str]) -> dict[str, str]:
    """Map kba-brand.json 'kleuren' naar het colors:-schema van renderer_v2.

    'warning' (fail-kleur voor check-blocks) is sinds 2026-07-10 afgeleid
    van kba-brand.json's ``kleuren.rood`` — de canonieke bron kreeg die
    tint toegevoegd nadat gebleken was dat een niet-canonieke placeholder
    ("#E74C3C") in ``tenants/kba/templates/content_styles.yaml`` stond te
    wachten op precies deze aanvulling (zie git-historie van dat bestand
    voor de oude placeholder-noot).

    'table_header_bg' / 'table_footer_bg' hebben geen 1-op-1 bron in
    kba-brand.json — de HTML-referentietemplates (coverblad.html,
    rapport.html) kennen geen gevulde tabelheader-balk, alleen een petrol
    onderrand. Petrol/teal zijn hier gekozen als dichtstbijzijnde
    band-kleuren, consistent met de gevulde petrol/teal-vlakken die de
    templates wél gebruiken (bv. ``.a footer``, ``.c .balk``, colofon-footer).
    """
    return {
        "primary": kleuren["petrol"],
        "secondary": kleuren["teal"],
        "accent": kleuren["mint"],
        "text": kleuren["inkt"],
        "text_accent": kleuren["petrol"],
        "text_light": kleuren["grijs"],
        "separator": kleuren["lijn"],
        "surface": kleuren["vlak"],
        "paper": kleuren["papier"],
        "warning": kleuren["rood"],
        "table_header_bg": kleuren["petrol"],
        "table_header_text": kleuren["papier"],
        "table_footer_bg": kleuren["teal"],
    }


def build_fonts(_typografie: dict[str, Any]) -> dict[str, str]:
    """Map naar ReportLab-fontnamen die matchen met tenants/<tenant>/fonts/*.ttf.

    kba-brand.json geeft CSS font-stacks (bv. "'Segoe UI', Arial,
    sans-serif"), geen ReportLab-fontnamen. Die worden hier vast gemapt naar
    de bestandsstammen in de fonts-map, zodat renderer_v2's auto-register
    (op bestandsnaam) ze oppikt.

    'semibold' (SegoeUI-Semibold) dekt de CSS font-weight:600 uit de
    referentie (".meta .kop", ".kicker", "footer strong"). Het .ttf komt uit
    Windows (seguisb.ttf); het bestand leeft in tenants/kba/fonts/ maar niet
    in git (tenants/* is gitignored), zodat de Segoe UI-licentiekwestie de
    publieke repo niet raakt. 'medium' (SegoeUI-Semilight) blijft beschikbaar
    als lichter tussengewicht maar wordt momenteel nergens gebruikt.
    """
    return {
        "heading": "SegoeUI-Bold",
        "body": "SegoeUI",
        "semibold": "SegoeUI-Semibold",
        "medium": "SegoeUI-Semilight",
        "italic": "SegoeUI-Italic",
    }


def build_contact(bedrijf: dict[str, str]) -> dict[str, str]:
    """Map kba-brand.json 'bedrijf' naar het contact:-schema.

    renderer_v2 (special_pages.py backcover) leest vandaag alleen
    ``name``/``address``/``website`` — ``address`` blijft daarom een enkele
    samengestelde regel (zelfde formaat als tenants/3bm/brand.yaml).
    De overige velden (email, phone, kvk, btw, iban) zijn extra, ongebruikte
    maar correcte metadata voor toekomstig gebruik (bv. een echte
    contact-kolom op het achterblad) — geen bestaande consument breekt op
    extra dict-keys.
    """
    adres_regel = (
        f"{bedrijf['adres']}  |  {bedrijf['postcode']} {bedrijf['plaats']}"
        f"  |  T. {bedrijf['telefoon']}"
    )
    return {
        "name": bedrijf["naam"],
        "address": adres_regel,
        "website": bedrijf["website"],
        "email": bedrijf["email"],
        "phone": bedrijf["telefoon"],
        "kvk": bedrijf["kvk"],
        "btw": bedrijf["btw"],
        "iban": bedrijf["iban"],
    }


def build_brand_yaml(
    source_data: dict[str, Any], tenant: str, base: dict[str, Any]
) -> dict[str, Any]:
    """Bouw de volledige brand.yaml-datastructuur (afgeleid + base gemerged)."""
    kleuren = source_data["kleuren"]
    typografie = source_data["typografie"]
    bedrijf = source_data["bedrijf"]

    result: dict[str, Any] = {
        "brand": {"name": bedrijf["naam"], "slug": tenant},
        "colors": build_colors(kleuren),
        "fonts": build_fonts(typografie),
    }
    if "logos" in base:
        result["logos"] = base["logos"]
    result["contact"] = build_contact(bedrijf)
    for key in _BASE_SECTIONS:
        if key == "logos":
            continue  # al hierboven geplaatst (vóór contact:)
        if key in base:
            result[key] = base[key]
    return result


def render_yaml(data: dict[str, Any], source: Path, tenant: str) -> str:
    """Render de datastructuur naar YAML-tekst met een gegenereerd-bestand-banner."""
    base_sections = ", ".join(
        k for k in _BASE_SECTIONS if k != "logos"
    )
    header = _GENERATED_HEADER.format(
        source=source, tenant=tenant, base_sections=f"logos, {base_sections}"
    )
    body = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
    )
    return header + body


def resolve_source(cli_value: str | None) -> Path:
    """Bepaal het bronpad: CLI-argument > KBA_BRAND_SOURCE env var > harde fout."""
    if cli_value:
        return Path(cli_value)
    env_value = os.environ.get("KBA_BRAND_SOURCE")
    if env_value:
        return Path(env_value)
    raise SystemExit(
        "Geen bron opgegeven. Gebruik --source <pad naar kba-brand.json>, "
        "of zet de omgevingsvariabele KBA_BRAND_SOURCE."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Genereer tenants/<tenant>/brand.yaml uit de canonieke huisstijlbron."
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Pad naar kba-brand.json (default: env var KBA_BRAND_SOURCE).",
    )
    parser.add_argument("--tenant", required=True, help="Tenant-slug, bv. 'kba'.")
    parser.add_argument(
        "--output",
        default=None,
        help="Output-pad (default: tenants/<tenant>/brand.yaml).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: genereer in geheugen, diff tegen bestaand bestand, exit 1 bij verschil.",
    )
    args = parser.parse_args(argv)

    source_path = resolve_source(args.source)
    tenant_dir = TENANTS_DIR / args.tenant
    output_path = Path(args.output) if args.output else tenant_dir / "brand.yaml"

    source_data = load_json_source(source_path)
    base = load_base(tenant_dir)
    data = build_brand_yaml(source_data, args.tenant, base)
    rendered = render_yaml(data, source_path, args.tenant)

    if args.check:
        if not output_path.exists():
            print(f"FOUT: {output_path} bestaat nog niet.", file=sys.stderr)
            return 1
        existing = output_path.read_text(encoding="utf-8")
        if existing != rendered:
            diff = difflib.unified_diff(
                existing.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=str(output_path),
                tofile="<opnieuw gegenereerd>",
            )
            sys.stdout.writelines(diff)
            print(
                f"\nFOUT: {output_path} wijkt af van een verse generatie. "
                "Draai het script zonder --check om bij te werken.",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {output_path} is actueel.")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    print(f"Geschreven: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
