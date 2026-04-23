"""
Bijgebouwen-detectie via BAG: zoek andere panden binnen hetzelfde perceel.

Voor Buurtscan's verbouwings-sectie willen we weten: staat er al een
aanbouw/schuur/carport op het achtererf? Bbl beperkt de totale oppervlakte
aanbouwen+bijgebouwen (staffel van 50%/50+20%/90+10%), en als er al iets
staat telt dat mee.

Aanpak:
1. Haal alle BAG-panden binnen de bbox van het perceel op.
2. Filter: pand-polygoon moet significant met perceel-polygoon overlappen.
3. Exclude hoofdpand (adres-pand-id).
4. Resultaat: lijst van bijgebouw-panden met oppervlakte + bouwjaar.

Beperkingen:
- BAG registreert alleen panden met verblijfsobject of groter dan ~10 m²
  met zelfstandige bouwkundige relevantie. Houten tuinhuisjes zonder fundering,
  open carports en kleine schuurtjes zitten vaak NIET in BAG. Voor die laatste
  zou BGT (Basisregistratie Grootschalige Topografie) completer zijn; dat is
  een volgende uitbreiding.
- Garages onder hoofdpand tellen als onderdeel van hoofdpand (één pand-id).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx
from shapely.geometry import shape, Polygon, MultiPolygon

BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
TIMEOUT_S = 10.0
HEADERS = {"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"}

# Minimale overlap met perceel (in m²) om een pand als bijgebouw te markeren.
# <3 m² is meestal een randje van buur-pand dat minimaal over de erfgrens valt.
MIN_OVERLAP_M2 = 3.0


@dataclass
class Bijgebouw:
    """Een BAG-pand dat op hetzelfde perceel staat als het hoofdpand."""
    pand_id: str
    oppervlakte_m2: int          # intersect-oppervlak binnen perceel
    totale_pand_m2: int          # volledige BAG-pand-footprint
    bouwjaar: Optional[int] = None
    status: Optional[str] = None


async def _fetch_panden_in_bbox(
    client: httpx.AsyncClient,
    min_x: float, min_y: float, max_x: float, max_y: float,
) -> list[dict]:
    """BAG-panden binnen een RD-bbox. Returnt raw GeoJSON features."""
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "bag:pand",
        "bbox": f"{min_x},{min_y},{max_x},{max_y},EPSG:28992",
        "count": "30",
        "outputFormat": "application/json",
        "srsName": "EPSG:28992",
    }
    try:
        resp = await client.get(BAG_WFS, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    return data.get("features") or []


async def fetch_bijgebouwen(
    perceel_polygon_rd: Polygon,
    hoofdpand_id: str,
) -> list[Bijgebouw]:
    """Vind alle BAG-panden (≠ hoofdpand) die op hetzelfde perceel staan.

    Input:
      perceel_polygon_rd — Shapely Polygon in EPSG:28992
      hoofdpand_id       — BAG pand-id van de woning zelf (uitgesloten)

    Output: lijst Bijgebouw-objecten, gesorteerd op oppervlakte-desc.
    """
    if not perceel_polygon_rd or perceel_polygon_rd.is_empty:
        return []
    min_x, min_y, max_x, max_y = perceel_polygon_rd.bounds
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=HEADERS) as client:
        feats = await _fetch_panden_in_bbox(client, min_x, min_y, max_x, max_y)

    out: list[Bijgebouw] = []
    for f in feats:
        props = f.get("properties") or {}
        pid = props.get("identificatie") or ""
        if not pid or pid == hoofdpand_id:
            continue
        try:
            g = shape(f.get("geometry") or {})
        except Exception:
            continue
        if isinstance(g, MultiPolygon):
            # Grootste deel-polygoon nemen
            g = max(g.geoms, key=lambda p: p.area)
        if not isinstance(g, Polygon):
            continue
        # Intersect met perceel — alleen het deel binnen het perceel telt
        try:
            overlap = perceel_polygon_rd.intersection(g)
        except Exception:
            continue
        overlap_m2 = overlap.area if overlap and not overlap.is_empty else 0.0
        if overlap_m2 < MIN_OVERLAP_M2:
            continue
        # Parse bouwjaar (soms None of string)
        bj = props.get("oorspronkelijk_bouwjaar")
        try:
            bj = int(bj) if bj is not None else None
        except (TypeError, ValueError):
            bj = None
        out.append(Bijgebouw(
            pand_id=pid,
            oppervlakte_m2=int(round(overlap_m2)),
            totale_pand_m2=int(round(g.area)),
            bouwjaar=bj,
            status=props.get("status"),
        ))
    # Sorteer op grootte (grootste eerst)
    out.sort(key=lambda b: -b.oppervlakte_m2)
    return out
