"""
BAG-verblijfsobjecten per pand — detecteert stapeling en de verdieping van
een specifiek verblijfsobject.

Waarom dit bestaat:
Voor Paramaribostraat 7-H (Amsterdam) weten we uit de display-naam alleen
"H" (huis) — maar BAG registreert dat met huisnummer 303 (Amsterdams
souterrain-nummering). Simpele toevoeging-parsing ("is numeriek?") faalt
bij exotische patronen (7-A, 7-B, 7-K, …). De robuuste oplossing is: haal
ALLE VBO's van het pand op en bepaal de positie van de specifieke VBO
binnen de gesorteerde lijst.

Flow:
  1. BAG-WFS: filter `bag:verblijfsobject` op pandidentificatie
  2. Groepeer per huisnummer+huisletter (= "één woonadres-range")
  3. Sorteer op toevoeging met bewuste ordening:
       - "" of None                 → positie 0 (alleen-adres)
       - "H", "hs", "bg", "huis", "0" → positie 0 (begane grond)
       - Numeriek 1..N              → positie N (verdieping)
       - Letter A..Z                → positie ord(letter) - ord('A')
         (consistent alfabetisch; dekt 7-A/B/C en Rotterdam-stijl patronen)
  4. Positie van user-VBO = verdieping (0-based)
  5. is_bovenste = (positie == max_positie_in_pand)

We vergelijken positie met het AANTAL unieke etages in dit pand, NIET met
3D BAG bouwlagen — want een pand kan 4 etages tellen maar bv. de begane
grond als bedrijf zijn zonder dat die als VBO meetelt. We tellen alleen de
VBO's met woonfunctie.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
TIMEOUT_S = 10.0
HEADERS = {"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"}


@dataclass
class VboInfo:
    """Één verblijfsobject in een pand."""
    identificatie: str
    huisnummer: Optional[int]
    huisletter: Optional[str]
    toevoeging: Optional[str]
    oppervlakte: Optional[int]
    gebruiksdoel: Optional[str] = None


@dataclass
class PandStapelingInfo:
    """Resultaat: hoe is dit pand gestapeld en waar zit deze specifieke VBO?"""
    aantal_vbos: int
    aantal_wonen: int
    is_gestapeld: bool                  # >1 woon-VBO in dit pand
    eigen_verdieping: Optional[int]     # 0-based; 0 = begane grond
    totaal_etages: Optional[int]        # aantal unieke etages
    is_bovenste: Optional[bool]         # True als eigen == totaal - 1
    etage_ordening: list[str]           # de gesorteerde toevoegingen (debug)


def _etage_key(toevoeging: Optional[str], huisletter: Optional[str]) -> tuple:
    """Sorteerkey: lagere waarde = lagere etage. Begane grond eerst."""
    hl = (huisletter or "").strip().lower()
    t = (toevoeging or "").strip().lower()
    # Stap 1: begane grond-aanduidingen
    if t in ("h", "hs", "hs.", "huis", "bg", ""):
        # "" = geen toevoeging → hoofdadres = begane grond
        base = 0
    elif t.isdigit():
        base = int(t)  # 1 → 1, 2 → 2, etc.
    elif len(t) == 1 and t.isalpha():
        # letter-ordening: A=0, B=1, C=2 ...
        base = ord(t) - ord("a")
    else:
        # Onbekend patroon — zet achter alles anders met hoge score
        base = 99
    # Tie-break: huisletter — zelden relevant maar voorkomt
    # instabiele sortering bij duplicaten.
    return (base, hl, t)


async def fetch_pand_stapeling(
    pand_id: str, eigen_vbo_id: Optional[str] = None,
) -> Optional[PandStapelingInfo]:
    """Hoofdentry: analyseer het pand op stapeling en eigen positie.

    `eigen_vbo_id` is het BAG-verblijfsobject-ID van de user. Als meegegeven,
    rekenen we de verdieping van die VBO uit t.o.v. alle andere wonende
    VBO's in hetzelfde pand.
    """
    if not pand_id:
        return None
    ogc_filter = (
        "<Filter>"
        "<PropertyIsEqualTo>"
        "<PropertyName>pandidentificatie</PropertyName>"
        f"<Literal>{pand_id}</Literal>"
        "</PropertyIsEqualTo>"
        "</Filter>"
    )
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "bag:verblijfsobject",
        "filter": ogc_filter,
        "outputFormat": "application/json",
        "count": "100",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=HEADERS) as client:
            resp = await client.get(BAG_WFS, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    feats = data.get("features") or []
    vbos: list[VboInfo] = []
    for f in feats:
        p = f.get("properties") or {}
        g = p.get("gebruiksdoel") or ""
        if isinstance(g, list):
            g = ",".join(g)
        vbos.append(VboInfo(
            identificatie=p.get("identificatie") or "",
            huisnummer=p.get("huisnummer"),
            huisletter=p.get("huisletter"),
            toevoeging=p.get("toevoeging"),
            oppervlakte=p.get("oppervlakte"),
            gebruiksdoel=g,
        ))
    if not vbos:
        return None
    # Alleen woon-VBO's meetellen voor verdieping-berekening (bedrijfsruimtes
    # op de begane grond tellen niet als woonlaag)
    wonen = [v for v in vbos if "woon" in (v.gebruiksdoel or "").lower()]
    if not wonen:
        wonen = vbos  # geen wonen-VBO's; val terug op alle

    # Kijk of eigen VBO in wonen-subset zit. Zo niet, pak 'm uit de volledige
    # lijst (bv. 7-H Amsterdam is soms als bedrijfsruimte geregistreerd).
    eigen_vbo = next((v for v in wonen if v.identificatie == eigen_vbo_id), None)
    if eigen_vbo is None:
        eigen_vbo = next((v for v in vbos if v.identificatie == eigen_vbo_id), None)
    if eigen_vbo is None:
        # User-VBO staat niet in pand; dan kunnen we alleen "gestapeld"-vlag
        # bepalen (er zijn meerdere VBO's) maar geen verdieping.
        return PandStapelingInfo(
            aantal_vbos=len(vbos),
            aantal_wonen=len(wonen),
            is_gestapeld=len(wonen) > 1,
            eigen_verdieping=None,
            totaal_etages=None,
            is_bovenste=None,
            etage_ordening=[],
        )
    # Zoek de verticale kolom: zelfde huisnummer+huisletter. Als eigen_vbo
    # niet in wonen-subset zit (bedrijfsruimte), combineer wonen + eigen_vbo
    # zelf zodat we de positie wel kunnen bepalen.
    kolom_basis = wonen if eigen_vbo in wonen else (wonen + [eigen_vbo])
    kolom = [
        v for v in kolom_basis
        if v.huisnummer == eigen_vbo.huisnummer
        and (v.huisletter or None) == (eigen_vbo.huisletter or None)
    ]
    kolom.sort(key=lambda v: _etage_key(v.toevoeging, v.huisletter))
    toevs = [(v.toevoeging or "") for v in kolom]
    try:
        idx = next(i for i, v in enumerate(kolom) if v.identificatie == eigen_vbo.identificatie)
    except StopIteration:
        idx = None
    is_bovenste = (idx == len(kolom) - 1) if idx is not None else None
    return PandStapelingInfo(
        aantal_vbos=len(vbos),
        aantal_wonen=len(wonen),
        is_gestapeld=len(wonen) > 1,
        eigen_verdieping=idx,
        totaal_etages=len(kolom),
        is_bovenste=is_bovenste,
        etage_ordening=toevs,
    )
