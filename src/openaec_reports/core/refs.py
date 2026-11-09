"""Refs — resolveert ``$colors.<key>`` / ``$fonts.<key>`` placeholders in
template-YAML tegen een :class:`~openaec_reports.core.brand.BrandConfig`.

Achtergrond: tenant-templates bevatten hardcoded hexes ipv verwijzingen naar
het merk-palet. Twee renderpaden hadden elk hun eigen, kleine substitutie-
implementatie:

- ``brand_renderer.py`` (``_resolve_color`` / ``_resolve_font``) — syntax
  ``"$primary"`` (één ``$``, geen ``colors.``-prefix), gebruikt voor
  header/footer ``ElementConfig``.
- ``page_templates.py`` (``_draw_text_zones``) — syntax ``"$colors.<key>"`` /
  ``"$fonts.<key>"``, maar alleen voor de twee velden ``color``/``font`` van
  een text-zone; geen generieke doorloop van geneste structuren.

Dit module is de derde, generieke variant: recursief over dict/list/str,
met een expliciete ``$colors.``/``$fonts.``-prefix (zoals ``page_templates.py``
al gebruikte — dat is de vorm die de tenant-YAML's al bevatten, bijv.
``brand.yaml``'s ``modules.check.fail_color: "$colors.warning"``). Bestaande
call-sites in ``brand_renderer.py``/``page_templates.py`` zijn NIET aangepast
(buiten scope van deze wijziging) — dit module is bedoeld om nieuwe
call-sites (``renderer_v2.TemplateSet``) op te laten hangen, en kan later als
die twee bestaande implementaties ook naar deze vorm migreren.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openaec_reports.core.brand import BrandConfig

# Matcht ALLEEN als de VOLLEDIGE string exact "$colors.<key>" of
# "$fonts.<key>" is — geen omliggende tekst, geen whitespace. Een string als
# "$colors.primary " (spatie) of "prefix $colors.primary" matcht niet en
# wordt dus ongewijzigd doorgegeven als literal — zie ``resolve_refs``
# docstring voor de motivatie.
_REF_RE = re.compile(r"^\$(colors|fonts)\.([A-Za-z0-9_]+)$")


def resolve_refs(
    value: Any,
    brand: BrandConfig,
    *,
    tenant: str = "",
    source: str = "",
) -> Any:
    """Resolveer ``$colors.<key>`` / ``$fonts.<key>`` refs recursief.

    Loopt door geneste ``dict``/``list``-structuren (templates zijn diep
    genest) en vervangt elke string die EXACT ``$colors.<key>`` of
    ``$fonts.<key>`` is door de bijbehorende waarde uit ``brand.colors``
    resp. ``brand.fonts``.

    Gedrag bij randgevallen:
    - Een letterlijke hex (``"#40124A"``) of willekeurige andere string
      matcht de ref-regex niet en blijft ongewijzigd. Achterwaarts
      compatibel met templates die nog geen refs gebruiken.
    - Een string die *lijkt* op een ref maar niet exact matcht (bv.
      ``"$colors.primary "`` met een spatie, of ``"zie $colors.primary"``)
      wordt NIET als ref herkend en dus ook niet als fout gerapporteerd —
      hij wordt behandeld als een gewone literal string. Dit is een
      bewuste keuze: refs worden in de templates altijd als volledige
      veldwaarde gebruikt (nooit ingebed in doorlopende tekst), dus
      partial-matching zou onnodige complexiteit toevoegen zonder een
      echt use-case te dienen.
    - Een sleutel die WEL de ``$colors.``/``$fonts.``-vorm heeft maar niet
      bestaat in ``brand.colors``/``brand.fonts`` faalt luid met een
      ``ValueError`` die de tenant, het bronbestand en de sleutel noemt.
      Er is bewust GEEN stille fallback naar een default-kleur — dat is
      precies het gedrag dat deze refactor moet uitroeien.

    Args:
        value: Ruwe YAML-waarde: dict, list, str, of scalar (int/float/bool/None).
        brand: BrandConfig met de ``colors``/``fonts`` dicts van het merk.
        tenant: Tenant-slug, uitsluitend voor de foutmelding.
        source: Bestandsnaam/pad van de template, uitsluitend voor de foutmelding.

    Returns:
        Dezelfde structuur met refs vervangen door hun waarde.

    Raises:
        ValueError: Als een ``$colors.<key>``/``$fonts.<key>`` ref een
            sleutel gebruikt die niet in het merk-palet voorkomt.
    """
    if isinstance(value, dict):
        return {
            key: resolve_refs(val, brand, tenant=tenant, source=source)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_refs(item, brand, tenant=tenant, source=source)
            for item in value
        ]
    if isinstance(value, str):
        match = _REF_RE.match(value)
        if not match:
            return value
        namespace, ref_key = match.groups()
        table = brand.colors if namespace == "colors" else brand.fonts
        if ref_key not in table:
            tenant_label = tenant or getattr(brand, "tenant", "") or getattr(
                brand, "slug", "?"
            )
            raise ValueError(
                f"Onbekende ${namespace}.{ref_key} referentie in tenant "
                f"'{tenant_label}', bestand '{source or '?'}': sleutel "
                f"'{ref_key}' bestaat niet in brand.{namespace}. "
                f"Beschikbare sleutels: {sorted(table)}"
            )
        return table[ref_key]
    return value
