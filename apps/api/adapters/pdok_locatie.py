"""
PDOK Locatieserver adapter.

Geocoding NL: tekst-input -> BAG verblijfsobject-id + WGS84 coordinaten + buurtcode.

Twee-traps flow (zoals aanbevolen door PDOK):
1. Suggest API -> kandidaten met type + id (fuzzy matching, typefout-tolerant)
2. Lookup API -> volledige details van gekozen kandidaat (incl. centroide + BAG-id)

Gratis, geen API-key nodig, response in milliseconden.
Docs: https://www.pdok.nl/restful-api/-/article/pdok-locatieserver
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

SUGGEST_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/suggest"
LOOKUP_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/lookup"

# Timeout laag houden: PDOK hoort in <100 ms te antwoorden op adres-lookups.
# Als het langer duurt is er iets mis; geen zin in minutenlang wachten.
TIMEOUT_S = 3.0


@dataclass
class AddressMatch:
    """Resultaat van een geocoding-lookup.

    Alle velden die verdere adapters nodig hebben om door te queryen.
    """

    display_name: str  # "Damrak 1, 1012LG Amsterdam"
    bag_verblijfsobject_id: Optional[str]  # sleutel voor BAG-adapter
    bag_pand_id: Optional[str]  # sleutel voor 3D/hoogte-adapter
    buurtcode: Optional[str]  # sleutel voor CBS-adapter (bv. "BU03630000")
    wijkcode: Optional[str]
    gemeentecode: Optional[str]
    postcode: Optional[str]
    huisnummer: Optional[str]
    lat: float  # WGS84
    lon: float  # WGS84
    rd_x: float  # Rijksdriehoek (EPSG:28992) voor RIVM/Klimaat-WMS
    rd_y: float


async def suggest(query: str, rows: int = 5) -> list[dict]:
    """Haal lijst van adres-kandidaten op via Suggest API.

    Returnt de raw 'docs' uit Solr — elke doc heeft minimaal 'id', 'type', 'weergavenaam'.
    We filteren op type='adres' omdat we postcodes/wegen/wijken niet als eindpunt willen.
    """
    params = {
        "q": query,
        "rows": rows,
        "fq": "type:adres",  # alleen adres-hits, geen woonplaats of postcode
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(SUGGEST_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data.get("response", {}).get("docs", [])


async def lookup(address_id: str) -> AddressMatch:
    """Haal volledige details op voor een specifieke adres-id uit Suggest.

    Bevat de centroide (punt-geometrie), BAG-id's en alle administratieve codes
    die we nodig hebben om door te ketenen naar CBS/BAG/RIVM-adapters.
    """
    params = {"id": address_id, "fl": "*"}  # fl=* : alle beschikbare velden
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(LOOKUP_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    docs = data.get("response", {}).get("docs", [])
    if not docs:
        raise ValueError(f"Adres-id niet gevonden: {address_id}")
    doc = docs[0]

    # centroide_ll is "POINT(lon lat)" in WGS84; parse ruwweg.
    # centroide_rd is "POINT(x y)" in RD (EPSG:28992).
    lon, lat = _parse_point(doc.get("centroide_ll", ""))
    rd_x, rd_y = _parse_point(doc.get("centroide_rd", ""))

    return AddressMatch(
        display_name=doc.get("weergavenaam", ""),
        bag_verblijfsobject_id=doc.get("adresseerbaarobject_id"),
        bag_pand_id=doc.get("nummeraanduiding_id"),  # TODO: via BAG naar pand-id
        buurtcode=doc.get("buurtcode"),
        wijkcode=doc.get("wijkcode"),
        gemeentecode=doc.get("gemeentecode"),
        postcode=doc.get("postcode"),
        huisnummer=str(doc.get("huisnummer", "")) if doc.get("huisnummer") else None,
        lat=lat,
        lon=lon,
        rd_x=rd_x,
        rd_y=rd_y,
    )


def _parse_point(wkt: str) -> tuple[float, float]:
    """Parse WKT 'POINT(x y)' naar (x, y) floats.

    PDOK levert geometrie als WKT-strings in plaats van losse velden.
    Accepteert ook lege strings (retourneert 0.0, 0.0 — caller moet valideren).
    """
    if not wkt or "(" not in wkt:
        return 0.0, 0.0
    inside = wkt.split("(", 1)[1].rstrip(")")
    parts = inside.split()
    if len(parts) < 2:
        return 0.0, 0.0
    return float(parts[0]), float(parts[1])


async def geocode(query: str) -> Optional[AddressMatch]:
    """One-shot: tekst -> AddressMatch (eerste beste hit).

    Handig voor server-side gebruik waar de gebruiker al heeft geklikt op een
    specifiek suggest-resultaat. Voor de UI-autocomplete is suggest() beter
    omdat je dan de hele lijst aan de frontend wilt tonen.
    """
    candidates = await suggest(query, rows=1)
    if not candidates:
        return None
    return await lookup(candidates[0]["id"])
