"""
Woning-extras adapter — monumentenstatus, erfpacht-prevalentie, groen-nabijheid.

Drie losse features die samen in sectie 1 'De woning' getoond worden:

1. **Rijksmonument-check** via RCE WFS (Rijksdienst Cultureel Erfgoed).
   Bbox-query rond pand-coord; returnt monument-nummer + categorie indien
   het pand een rijksmonument is. Geen treffer = "geen rijksmonument".

2. **Erfpacht-prevalentie** per gemeente (hardcoded, want individuele pand-
   erfpacht zit in BRK-Kadaster zonder publieke open data).
   Toont: "Deze gemeente heeft veel erfpacht — check bij notaris/taxateur".
   Gebaseerd op bekende statistieken van de grote erfpachtgemeenten.

3. **Groen in de buurt** via OpenStreetMap Overpass. Som van leisure=park,
   landuse=forest/grass/recreation, natural=wood polygonen binnen 300m.
   Geeft oppervlakte-schatting + vergelijking met cirkel-oppervlakte.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Optional

import httpx

# --- Rijksmonumenten (RCE WFS) ---
RCE_WFS = "https://services.rce.geovoorziening.nl/rce/wfs"

# --- Overpass voor groen ---
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

TIMEOUT_S = 10.0
HEADERS = {"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"}

# --- Erfpacht-prevalentie (hardcoded, publiekelijk bekend) ---
# Bron: diverse onderzoeken + NVM/Kadaster rapportages. Pand-specifieke
# erfpacht-data zit achter de BRK (Basisregistratie Kadaster) die alleen
# met OIN+PKI te bevragen is. Voor deze indicator volstaat gemeente-niveau.
ERFPACHT_PREVALENTIE = {
    # gemeentecode (zonder 'GM'): (niveau, % ongeveer, toelichting)
    "0363": ("hoog", 85, "Amsterdam: ca. 85% van de grond is erfpacht van de gemeente."),
    "0518": ("middel", 30, "Den Haag: ongeveer 1/3 van de woningen op gemeentelijke erfpacht."),
    "0344": ("middel-laag", 15, "Utrecht: erfpacht komt voor, vooral in de binnenstad."),
    "0599": ("laag", 5, "Rotterdam: erfpacht is zeldzaam."),
    "0855": ("middel", 20, "Vlissingen: erfpacht komt regelmatig voor."),
    "0014": ("laag", 5, "Groningen: erfpacht komt sporadisch voor."),
    "0546": ("laag", 5, "Leiden: erfpacht in enkele specifieke gebieden."),
    "0222": ("middel", 25, "Delft: oude stadskern vaak op erfpacht."),
}


@dataclass
class Rijksmonument:
    """Info over een rijksmonument-hit."""
    monument_nummer: int
    hoofdcategorie: Optional[str]     # bv 'Woningen en woningbouwcomplexen'
    subcategorie: Optional[str]
    aard_monument: Optional[str]      # bv 'onroerend gebouwd'
    url: Optional[str]                # rijksmonumenten.nl link


@dataclass
class Erfpacht:
    """Erfpacht-prevalentie op gemeente-niveau (geen pand-specifiek)."""
    niveau: str                       # 'hoog' / 'middel' / 'laag'
    pct_schatting: int                # ~% gemeentegrondoppervlak
    toelichting: str


@dataclass
class GroenNabij:
    """Groen-polygonen oppervlakte binnen N meter."""
    straal_m: int
    groen_m2: int                     # totale groene oppervlakte
    cirkel_m2: int                    # cirkel-oppervlakte voor referentie
    groen_pct: float                  # groen / cirkel * 100
    aantal_elementen: int             # hoeveel polygonen


@dataclass
class WoningExtras:
    """Alle 3 extra-info items voor sectie 1 Woning."""
    rijksmonument: Optional[Rijksmonument] = None
    erfpacht: Optional[Erfpacht] = None
    groen: Optional[GroenNabij] = None


# ---------------------------------------------------------------------------
# 1. Rijksmonument-check
# ---------------------------------------------------------------------------

async def _fetch_rijksmonument(
    client: httpx.AsyncClient, rd_x: float, rd_y: float
) -> Optional[Rijksmonument]:
    """Bbox-query op RCE WFS (punten-laag) rond RD-coord.

    25m-radius is ruim genoeg om de pand-centroid te koppelen aan een
    monument-punt (die vaak op de ingang of centroid staan).
    """
    if not (rd_x and rd_y):
        return None
    half = 25
    bbox = f"{rd_x - half},{rd_y - half},{rd_x + half},{rd_y + half},EPSG:28992"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": "rce:NationalListedMonumentPoints",
        "bbox": bbox,
        "maxFeatures": "5",
        "outputFormat": "application/json",
    }
    try:
        resp = await client.get(RCE_WFS, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    feats = data.get("features", [])
    if not feats:
        return None
    # Pak het eerste monument als hit
    p = feats[0].get("properties", {}) or {}
    try:
        num = int(p.get("rijksmonument_nummer") or 0)
    except (TypeError, ValueError):
        num = 0
    if not num:
        return None
    return Rijksmonument(
        monument_nummer=num,
        hoofdcategorie=p.get("hoofdcategorie"),
        subcategorie=p.get("subcategorie"),
        aard_monument=p.get("aard_monument"),
        url=p.get("rijksmonumenturl"),
    )


# ---------------------------------------------------------------------------
# 2. Erfpacht-prevalentie (hardcoded lookup)
# ---------------------------------------------------------------------------

def lookup_erfpacht(gemeentecode: Optional[str]) -> Optional[Erfpacht]:
    """Zoek gemeentecode in ERFPACHT_PREVALENTIE.

    Retourneert None voor gemeenten waar erfpacht statistisch onbeduidend
    is (>90% NL valt hieronder).
    """
    if not gemeentecode:
        return None
    key = gemeentecode.replace("GM", "").zfill(4)
    if key in ERFPACHT_PREVALENTIE:
        niveau, pct, txt = ERFPACHT_PREVALENTIE[key]
        return Erfpacht(niveau=niveau, pct_schatting=pct, toelichting=txt)
    return None


# ---------------------------------------------------------------------------
# 3. Groen in de buurt (Overpass)
# ---------------------------------------------------------------------------

GROEN_RADIUS_M = 300  # loopafstand voor 'groen direct om je heen'


async def _fetch_groen_nabij(
    client: httpx.AsyncClient, lat: float, lon: float
) -> Optional[GroenNabij]:
    """Haal groen-polygonen op binnen GROEN_RADIUS_M en som hun oppervlakte.

    Overpass levert de geometry (lon/lat nodes per way). We berekenen
    oppervlakte via de shoelace-formule in een lokaal equirectangular
    projectie (nauwkeurig genoeg voor 300m-schaal in NL).
    """
    query = f"""
[out:json][timeout:15];
(
  way["leisure"="park"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["leisure"="garden"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["leisure"="recreation_ground"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["landuse"="forest"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["landuse"="grass"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["landuse"="recreation_ground"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["landuse"="meadow"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["natural"="wood"](around:{GROEN_RADIUS_M},{lat},{lon});
  way["natural"="scrub"](around:{GROEN_RADIUS_M},{lat},{lon});
);
out tags geom;
""".strip()
    try:
        resp = await client.post(
            OVERPASS_URL, data={"data": query}, headers=HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    # Shoelace area in lokale equirectangular projectie (meters)
    lat_m = 111_320.0  # meters per graad lat op NL-breedte
    total_area = 0.0
    elements = data.get("elements", [])
    for e in elements:
        geom = e.get("geometry") or []
        if len(geom) < 3:
            continue
        pts = [
            (p["lon"] * lat_m * math.cos(math.radians(p["lat"])),
             p["lat"] * lat_m)
            for p in geom
        ]
        s = 0.0
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        total_area += abs(s) / 2

    cirkel_m2 = int(math.pi * GROEN_RADIUS_M * GROEN_RADIUS_M)
    groen_m2 = int(total_area)
    pct = round(100 * groen_m2 / cirkel_m2, 1) if cirkel_m2 else 0.0
    return GroenNabij(
        straal_m=GROEN_RADIUS_M,
        groen_m2=groen_m2,
        cirkel_m2=cirkel_m2,
        groen_pct=pct,
        aantal_elementen=len(elements),
    )


# ---------------------------------------------------------------------------
# Orchestrator entry-point: alle 3 parallel
# ---------------------------------------------------------------------------

async def fetch_woning_extras(
    lat: float, lon: float, rd_x: float, rd_y: float,
    gemeentecode: Optional[str],
) -> WoningExtras:
    """Drie losse calls parallel; geen van allen is blokkerend.

    - Rijksmonument (RCE WFS) — 200-500ms
    - Groen (Overpass) — 500-1500ms cold
    - Erfpacht — direct lookup (geen I/O)
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=HEADERS) as client:
        rijk_task = _fetch_rijksmonument(client, rd_x, rd_y)
        groen_task = _fetch_groen_nabij(client, lat, lon)
        rijk, groen = await asyncio.gather(rijk_task, groen_task)
    erfpacht = lookup_erfpacht(gemeentecode)
    return WoningExtras(
        rijksmonument=rijk,
        erfpacht=erfpacht,
        groen=groen,
    )
