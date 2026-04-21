"""
Onderwijs + kinderopvang adapter.

Input  : WGS84 coordinaat (lat/lon)
Output : top-N kinderopvang + scholen binnen radius, met afstand in meters

Bronnen (via sync_onderwijs.py gebundeld in apps/api/data/onderwijs.json):
  - LRK (Landelijk Register Kinderopvang) — naam, type, kindplaatsen
  - DUO basisonderwijs vestigingen — naam, BRIN, denominatie
  - Onderwijsinspectie — oordeel (Voldoende / Onvoldoende / Zeer zwak)

Laad-strategie:
  - JSON wordt één keer in-memory geladen bij module-import (lazy).
  - Query-tijd: lineaire scan (~35K entries, <50ms voor haversine).
  - Optimalisatie later mogelijk via grid-bucketing indien nodig.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Data-bestand (geproduceerd door scripts/sync_onderwijs.py)
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "onderwijs.json"

# Defaults: binnen deze radius zoeken. 1 km is een realistische schaal
# voor kinderopvang en basisscholen ("loopafstand ouders").
DEFAULT_RADIUS_M = 1500
# Maximaal aantal hits per categorie in output
TOP_N = 5


@dataclass
class OnderwijsItem:
    """Eén kinderopvang-locatie of school met afstand tot query-punt."""
    categorie: str         # 'kinderopvang' | 'school'
    type: Optional[str]    # KDV / BSO / VGO / GO voor kinderopvang; None voor school
    naam: str
    adres: str
    gemeente: str
    meters: int
    lat: float
    lon: float
    # Kinderopvang-specifiek
    kindplaatsen: Optional[int] = None
    # School-specifiek
    denominatie: Optional[str] = None      # Openbaar / PC / RK / Islamitisch / ...
    inspectie_oordeel: Optional[str] = None  # Voldoende / Onvoldoende / Zeer zwak
    inspectie_peildatum: Optional[str] = None
    brin: Optional[str] = None
    url: Optional[str] = None


# In-memory cache van de JSON-data. Wordt lazy geladen bij eerste query.
_DATA: Optional[dict] = None


def _load() -> dict:
    """Lazy load het onderwijs.json bestand in een dict."""
    global _DATA
    if _DATA is not None:
        return _DATA
    if not DATA_PATH.exists():
        _DATA = {"kinderopvang": [], "scholen": [], "peildatum": None}
        return _DATA
    try:
        with DATA_PATH.open(encoding="utf-8") as f:
            _DATA = json.load(f)
    except Exception:
        _DATA = {"kinderopvang": [], "scholen": [], "peildatum": None}
    return _DATA


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Afstand in meters tussen twee WGS84-punten."""
    r = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def fetch_onderwijs(
    lat: float,
    lon: float,
    radius_m: int = DEFAULT_RADIUS_M,
    top_n: int = TOP_N,
) -> dict:
    """Retourneer aggregaten + top-N dichtstbijzijnde per categorie.

    Output-structuur past 1-op-1 op wat de orchestrator in de /scan-response
    wil. Compleet voorbeeld:
        {
            "available": True,
            "peildatum": "2024-xx",
            "radius_m": 1500,
            "kinderopvang": {
                "aantal_locaties": 12,
                "totaal_kindplaatsen": 450,
                "per_type": {"KDV": 6, "BSO": 4, "VGO": 2},
                "top": [{naam, type, meters, kindplaatsen, adres}, ...]
            },
            "scholen": {
                "aantal": 4,
                "oordelen": {"Voldoende": 3, "Onvoldoende": 0, ...},
                "top": [{naam, denominatie, meters, inspectie_oordeel, adres}, ...]
            }
        }
    """
    data = _load()
    # Rough bbox filter — vermijd haversine op heel NL; pak eerst
    # kandidaten binnen ~0.02 graden (~2km) en doe daarna haversine.
    deg = (radius_m / 111_000) * 1.5  # ruim marge, haversine is beslissend

    # --- Kinderopvang ---
    ko_hits: list[OnderwijsItem] = []
    for row in data.get("kinderopvang", []):
        if abs(row["lat"] - lat) > deg or abs(row["lon"] - lon) > deg:
            continue
        d = _haversine_m(lat, lon, row["lat"], row["lon"])
        if d > radius_m:
            continue
        ko_hits.append(OnderwijsItem(
            categorie="kinderopvang",
            type=row.get("type"),
            naam=row.get("naam") or "",
            adres=row.get("adres") or "",
            gemeente=row.get("gemeente") or "",
            meters=int(d),
            lat=row["lat"],
            lon=row["lon"],
            kindplaatsen=row.get("kindplaatsen") or 0,
            url=row.get("url"),
        ))
    ko_hits.sort(key=lambda x: x.meters)

    # --- Scholen ---
    sch_hits: list[OnderwijsItem] = []
    for row in data.get("scholen", []):
        if abs(row["lat"] - lat) > deg or abs(row["lon"] - lon) > deg:
            continue
        d = _haversine_m(lat, lon, row["lat"], row["lon"])
        if d > radius_m:
            continue
        sch_hits.append(OnderwijsItem(
            categorie="school",
            type=None,
            naam=row.get("naam") or "",
            adres=row.get("adres") or "",
            gemeente=row.get("gemeente") or "",
            meters=int(d),
            lat=row["lat"],
            lon=row["lon"],
            denominatie=row.get("denominatie"),
            inspectie_oordeel=row.get("inspectie_oordeel"),
            inspectie_peildatum=row.get("inspectie_peildatum"),
            brin=row.get("brin"),
            url=row.get("url"),
        ))
    sch_hits.sort(key=lambda x: x.meters)

    # --- Aggregatie kinderopvang ---
    per_type: dict[str, int] = {}
    totaal_kindplaatsen = 0
    for it in ko_hits:
        per_type[it.type or "ANDERS"] = per_type.get(it.type or "ANDERS", 0) + 1
        totaal_kindplaatsen += it.kindplaatsen or 0

    # --- Aggregatie scholen ---
    oordelen: dict[str, int] = {}
    for it in sch_hits:
        if it.inspectie_oordeel:
            oordelen[it.inspectie_oordeel] = oordelen.get(it.inspectie_oordeel, 0) + 1

    return {
        "available": True,
        "peildatum": data.get("peildatum"),
        "radius_m": radius_m,
        "kinderopvang": {
            "aantal_locaties": len(ko_hits),
            "totaal_kindplaatsen": totaal_kindplaatsen,
            "per_type": per_type,
            "top": [_item_to_dict(it) for it in ko_hits[:top_n]],
        },
        "scholen": {
            "aantal": len(sch_hits),
            "oordelen": oordelen,
            "top": [_item_to_dict(it) for it in sch_hits[:top_n]],
        },
    }


def _item_to_dict(it: OnderwijsItem) -> dict:
    """Serialize naar compact dict (alleen velden die de UI nodig heeft)."""
    out = {
        "naam": it.naam,
        "adres": it.adres,
        "meters": it.meters,
    }
    if it.type:
        out["type"] = it.type
    if it.kindplaatsen is not None:
        out["kindplaatsen"] = it.kindplaatsen
    if it.denominatie:
        out["denominatie"] = it.denominatie
    if it.inspectie_oordeel:
        out["inspectie_oordeel"] = it.inspectie_oordeel
        out["inspectie_peildatum"] = it.inspectie_peildatum
    if it.url:
        out["url"] = it.url
    return out
