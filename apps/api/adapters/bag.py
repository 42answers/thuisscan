"""
BAG (Basisregistratie Adressen en Gebouwen) adapter.

Input  : BAG verblijfsobject-id (uit PDOK Locatieserver)
Output : bouwjaar, oppervlakte, gebruiksdoel, status, pand-id

Gebruikt PDOK BAG WFS v2.0 (gratis, geen API-key).
De modernere "OGC API Features" variant van de BAG op api.pdok.nl levert alleen
Vector Tiles, geen attributen — en de Kadaster BAG individuele bevragingen API
vereist een PKIO-certificaat. Daarom WFS.

Docs: https://www.pdok.nl/introductie/-/article/basisregistratie-adressen-en-gebouwen-ba-1
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

WFS_URL = "https://service.pdok.nl/lv/bag/wfs/v2_0"
TIMEOUT_S = 5.0


@dataclass
class PandDetails:
    """Resultaat van een BAG-lookup op een verblijfsobject.

    None-waardes = BAG kende het veld niet (bv. pand in aanbouw of gesloopt).
    """

    verblijfsobject_id: str
    pand_id: Optional[str]
    bouwjaar: Optional[int]
    oppervlakte_m2: Optional[int]  # gebruiksoppervlakte
    gebruiksdoel: list[str]  # bv. ["woonfunctie"]
    status_verblijfsobject: Optional[str]
    status_pand: Optional[str]
    straat: Optional[str]
    huisnummer: Optional[str]


async def fetch_pand(verblijfsobject_id: str) -> PandDetails:
    """Haal pand-details op via BAG WFS.

    Twee sequentiele WFS-calls:
    1. verblijfsobject -> oppervlakte, gebruiksdoel, status, pand_id
    2. pand -> bouwjaar, pand-status

    Allebei PropertyIsEqualTo op 'identificatie'.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        vbo = await _get_feature(client, "verblijfsobject", verblijfsobject_id)
        if vbo is None:
            return PandDetails(
                verblijfsobject_id=verblijfsobject_id,
                pand_id=None,
                bouwjaar=None,
                oppervlakte_m2=None,
                gebruiksdoel=[],
                status_verblijfsobject=None,
                status_pand=None,
                straat=None,
                huisnummer=None,
            )

        # BAG oppervlakte wordt in hele m^2 geleverd (niet dm^2 zoals eerder
        # gedacht); wel controleren op onzinnig hoge waarden.
        oppervlakte = vbo.get("oppervlakte")
        if isinstance(oppervlakte, (int, float)):
            oppervlakte = int(oppervlakte)
        else:
            oppervlakte = None

        raw_gebruiksdoel = vbo.get("gebruiksdoel") or ""
        gebruiksdoel = [g.strip() for g in str(raw_gebruiksdoel).split(",") if g.strip()]

        # PDOK BAG WFS geeft 'pandidentificatie' (single), soms met meerdere
        # pand-ids comma-separated voor hoekhuizen / dubbelhuizen. We pakken
        # de eerste — voor 99% van de woningen is er precies 1.
        pand_field = vbo.get("pandidentificatie")
        pand_id: Optional[str] = None
        if isinstance(pand_field, str) and pand_field:
            pand_id = pand_field.split(",")[0].strip()

        bouwjaar: Optional[int] = None
        status_pand: Optional[str] = None
        if pand_id:
            pand = await _get_feature(client, "pand", pand_id)
            if pand:
                bj = pand.get("bouwjaar")
                if isinstance(bj, int):
                    bouwjaar = bj
                elif isinstance(bj, str) and bj.isdigit():
                    bouwjaar = int(bj)
                status_pand = pand.get("status")

        # Huisnummer komt al uit Locatieserver, maar is ook in BAG aanwezig —
        # handig voor verificatie + de 'huisletter' die Locatieserver soms mist.
        huisnummer = vbo.get("huisnummer")
        if huisnummer is not None:
            hl = vbo.get("huisletter") or ""
            tv = vbo.get("toevoeging") or ""
            huisnummer = f"{huisnummer}{hl}{('-' + tv) if tv else ''}"

    return PandDetails(
        verblijfsobject_id=verblijfsobject_id,
        pand_id=pand_id,
        bouwjaar=bouwjaar,
        oppervlakte_m2=oppervlakte,
        gebruiksdoel=gebruiksdoel,
        status_verblijfsobject=vbo.get("status"),
        status_pand=status_pand,
        straat=vbo.get("openbare_ruimte"),
        huisnummer=huisnummer,
    )


async def _get_feature(
    client: httpx.AsyncClient, type_name: str, identificatie: str
) -> Optional[dict]:
    """WFS GetFeature met OGC Filter op 'identificatie'.

    Retourneert de 'properties' van het eerste feature, of None als niets gevonden.
    """
    # OGC filter — URL-encoded XML. Houden we minimaal om debugging mogelijk te
    # maken; geen fancy FES-builder dependency nodig.
    ogc_filter = (
        "<Filter>"
        "<PropertyIsEqualTo>"
        "<PropertyName>identificatie</PropertyName>"
        f"<Literal>{identificatie}</Literal>"
        "</PropertyIsEqualTo>"
        "</Filter>"
    )
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"bag:{type_name}",
        "outputFormat": "application/json",
        "filter": ogc_filter,
    }
    resp = await client.get(WFS_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features", [])
    if not features:
        return None
    return features[0].get("properties", {})
