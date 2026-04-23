"""
3D BAG adapter — haalt pand-hoogte + bouwlagen uit het 3D BAG bestand.

3D BAG is een TU Delft + Kadaster project dat alle Nederlandse panden in 3D
reconstrueert op basis van BAG-voetprinten + AHN4/5/6 hoogtebestand. Per pand
levert het onder meer:

    b3_bouwlagen       - aantal bouwlagen (geschat uit hoogte / 3 m)
    b3_h_nok           - nokhoogte (hoogste punt) in meters boven maaiveld
    b3_h_dak_max       - max dakhoogte
    b3_h_dak_50p       - mediaan dakhoogte (≈ goothoogte bij schuin dak)
    b3_h_dak_70p       - 70-percentiel dakhoogte
    b3_dak_type        - 'flat', 'slanted', of 'multiple'
    b3_h_maaiveld      - maaiveld-NAP-hoogte

Voor de Optopping-card in Sectie 10 combineren we dit met de BP-bouwhoogte
(als die bekend is via DSO) om te bepalen of een extra bouwlaag past.

Endpoint: https://api.3dbag.nl/collections/pand/items
CRS: EPSG:7415 (= RD/Amersfoort + NAP hoogte). bbox-query in x,y,z of alleen x,y.
Directe item-lookup werkt niet betrouwbaar; we gebruiken een kleine bbox +
filter op pand-identificatie.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

BAG3D_BASE = "https://api.3dbag.nl/collections/pand/items"
# 3D BAG API is soms traag (wereldwijd grote dataset); 20s is ruim genoeg.
TIMEOUT_S = 20.0
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com",
}
BBOX_CRS = "http://www.opengis.net/def/crs/EPSG/0/7415"


@dataclass
class PandHoogte:
    """3D-geometrie metadata van een pand."""
    bouwlagen: Optional[int] = None
    nokhoogte_m: Optional[float] = None       # hoogste punt boven maaiveld
    dakhoogte_max_m: Optional[float] = None
    dakhoogte_50p_m: Optional[float] = None   # mediaan, ≈ goothoogte schuin dak
    goothoogte_m: Optional[float] = None      # = dakhoogte_50p bij schuin dak
    daktype: Optional[str] = None             # 'flat', 'slanted', 'multiple'
    maaiveld_nap_m: Optional[float] = None


def _parse_city_object(obj: dict) -> PandHoogte:
    """Trek hoogte-attributen uit één CityObject.

    KRITIEKE CORRECTIE: 3D BAG's `b3_h_*`-velden zijn **absolute NAP-hoogtes**,
    niet hoogte-boven-maaiveld. We trekken b3_h_maaiveld af om de echte
    pand-hoogte boven maaiveld te krijgen. Zonder deze correctie krijg je op
    de Veluwe (maaiveld ~17 m NAP) voor een normaal pand 26 m nokhoogte,
    in Amsterdam (maaiveld ~0 m NAP) wél correct 9 m.
    """
    a = (obj or {}).get("attributes") or {}
    daktype = a.get("b3_dak_type")
    mv = a.get("b3_h_maaiveld") or 0.0  # default 0 als ontbreekt

    def _boven_mv(nap: Optional[float]) -> Optional[float]:
        if nap is None:
            return None
        return max(0.0, nap - mv)

    nok_bvm = _boven_mv(a.get("b3_h_nok"))
    dak_max_bvm = _boven_mv(a.get("b3_h_dak_max"))
    dak_50p_bvm = _boven_mv(a.get("b3_h_dak_50p"))
    dak_min_bvm = _boven_mv(a.get("b3_h_dak_min"))
    # Goothoogte (bij schuin dak ≈ mediaan dakhoogte; plat dak ≈ dak-min)
    goot = None
    if daktype == "slanted":
        goot = dak_50p_bvm
    elif daktype == "flat":
        goot = dak_min_bvm or dak_50p_bvm
    return PandHoogte(
        bouwlagen=int(a["b3_bouwlagen"]) if a.get("b3_bouwlagen") is not None else None,
        nokhoogte_m=nok_bvm,
        dakhoogte_max_m=dak_max_bvm,
        dakhoogte_50p_m=dak_50p_bvm,
        goothoogte_m=goot,
        daktype=daktype,
        maaiveld_nap_m=a.get("b3_h_maaiveld"),
    )


async def fetch_pand_hoogte(
    rd_x: float, rd_y: float, bag_pand_id: Optional[str] = None,
) -> Optional[PandHoogte]:
    """Haal pand-hoogte op via een kleine bbox rond de coord.

    Als `bag_pand_id` meegegeven wordt, filteren we de response op die ID
    (meerdere panden kunnen in de bbox liggen bij rijtjeshuizen). Zonder
    ID pakken we de eerste feature.
    """
    if not (rd_x and rd_y):
        return None
    # Bbox 30 m half — groot genoeg om ook grote panden (landhuizen,
    # scholen) volledig te pakken. 10 m was te klein: voor Hindenhoek 20
    # Vaassen lag het juiste pand NET buiten, waardoor adapter terugviel
    # op een buurpand (silo/kerk) met onrealistische hoogte van 26 m.
    half = 30
    bbox = f"{rd_x - half},{rd_y - half},{rd_x + half},{rd_y + half}"
    params = {
        "bbox": bbox,
        "bbox-crs": BBOX_CRS,
        "limit": "30",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=HEADERS) as client:
            resp = await client.get(BAG3D_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    # De features zijn van vorm {CityObjects: {id: {attributes: {...}}}}
    want = None
    if bag_pand_id:
        want = f"NL.IMBAG.Pand.{bag_pand_id}"
    # Zoek de specifieke pand als gevraagd
    for f in feats:
        co = f.get("CityObjects") or {}
        for pid, obj in co.items():
            if want is None or pid == want:
                ph = _parse_city_object(obj)
                if ph.nokhoogte_m is not None or ph.bouwlagen is not None:
                    return ph
    # Geen fallback meer op een willekeurig buurpand — liever None dan een
    # onrealistisch getal (Hindenhoek 20 Vaassen gaf eerder 26 m door silo-
    # buurpand). Als de juiste pand niet in de bbox zit, retourneren we None
    # en toont UI een net generieker bericht.
    return None
