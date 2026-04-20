"""
Kadaster WOZ-bevragen adapter — WOZ-waarde per adres.

Bron: Haal Centraal WOZ bevragen API (github.com/kadaster/WOZ-bevragen)
Endpoint: GET /wozobjecten?postcode=XXXXYY&huisnummer=N
          of     /wozobjecten?adresseerbaarObjectIdentificatie=<BAG-VBO>

**Aanmelden API-key (gratis, ~500 calls/dag)**

  1. Ga naar https://www.kadaster.nl/zakelijk/producten/adressen-en-gebouwen/woz-api-bevragen
  2. Klik "Aanmelden WOZ API Bevragen" (formulieren.kadaster.nl/aanmelden_lv_woz)
  3. Vul KvK-nummer + doel in ("data-ontsluiting MVP" is prima)
  4. Kadaster activeert doorgaans binnen 1-5 werkdagen en stuurt een key
  5. Zet `KADASTER_WOZ_API_KEY=...` in apps/api/.env
  6. Herstart de server — de adapter pikt het automatisch op

**Zonder key** retourneert de adapter None en toont de UI het
buurtgemiddelde uit CBS als fallback (al aanwezig).

Docs: https://kadaster.github.io/WOZ-bevragen/getting-started
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

# Productie-endpoint (vereist API-key). Swagger-mock werkt ook zonder key
# maar geeft statische voorbeelddata — handig voor ontwikkeling.
BASE_URL = os.environ.get(
    "KADASTER_WOZ_BASE_URL",
    "https://api.kadaster.nl/lv/woz/v1",  # officieel; key vereist
)
TIMEOUT_S = 4.0


@dataclass
class WozWaarde:
    """Eén WOZ-registratie voor een specifiek pand.

    Kadaster kan meerdere waardepeildata teruggeven (jaargangen). We tonen
    alleen de meest recente; de volledige reeks zit in `historie`.
    """

    wozobjectnummer: str
    adres: Optional[str]
    gebruiksdoel: Optional[str]  # 'Woning' / 'Niet-woning'
    oppervlakte_m2: Optional[int]
    huidige_waarde_eur: Optional[int]
    peildatum: Optional[str]  # bv. '2024-01-01'
    historie: list[dict]  # [{peildatum, waarde_eur}]


async def fetch_woz_by_bag(bag_vbo_id: str) -> Optional[WozWaarde]:
    """WOZ opvragen via BAG verblijfsobject-id (preciesst pad)."""
    api_key = os.environ.get("KADASTER_WOZ_API_KEY", "").strip()
    if not api_key:
        return None
    params = {"adresseerbaarObjectIdentificatie": bag_vbo_id}
    return await _query(params, api_key)


async def fetch_woz_by_adres(
    postcode: str, huisnummer: str, huisnummertoevoeging: str = ""
) -> Optional[WozWaarde]:
    """WOZ opvragen via postcode + huisnummer."""
    api_key = os.environ.get("KADASTER_WOZ_API_KEY", "").strip()
    if not api_key:
        return None
    pc = (postcode or "").replace(" ", "").upper()
    if not pc or not huisnummer:
        return None
    params: dict = {"postcode": pc, "huisnummer": str(huisnummer)}
    if huisnummertoevoeging:
        params["huisnummertoevoeging"] = huisnummertoevoeging
    return await _query(params, api_key)


async def _query(params: dict, api_key: str) -> Optional[WozWaarde]:
    """Generic GET /wozobjecten en parse eerste match."""
    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/hal+json",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(
                f"{BASE_URL}/wozobjecten", params=params, headers=headers
            )
            if resp.status_code == 401:
                return None  # key invalid / niet-geactiveerd
            if resp.status_code == 404:
                return None  # geen WOZ voor dit adres
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    embedded = (data.get("_embedded") or {}).get("wozobjecten") or []
    if not embedded:
        return None
    obj = embedded[0]

    # Waarden-historie kan onder 'wozWaarden' staan (HAL-style)
    waarden_raw = obj.get("wozWaarden") or []
    historie = []
    for w in waarden_raw:
        peildatum = w.get("waardepeildatum") or w.get("peildatum")
        vastgestelde = w.get("vastgesteldeWaarde")
        if peildatum and vastgestelde is not None:
            historie.append(
                {"peildatum": peildatum, "waarde_eur": int(vastgestelde)}
            )
    historie.sort(key=lambda x: x["peildatum"], reverse=True)

    huidige = historie[0] if historie else {}
    return WozWaarde(
        wozobjectnummer=str(obj.get("wozObjectNummer") or obj.get("identificatie") or ""),
        adres=obj.get("aanduiding") or None,
        gebruiksdoel=obj.get("wozObjectSoortgebouwdObject") or obj.get("gebruiksdoel"),
        oppervlakte_m2=obj.get("gebruiksoppervlakte") or None,
        huidige_waarde_eur=huidige.get("waarde_eur"),
        peildatum=huidige.get("peildatum"),
        historie=historie,
    )
