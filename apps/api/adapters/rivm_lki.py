"""
RIVM Atlas Leefomgeving adapter — luchtkwaliteit op adres-niveau.

Input  : RD-coordinaten (EPSG:28992) — uit PDOK Locatieserver
Output : jaargemiddelden NO2, PM10, PM2.5 (microgram/m3) + WHO-vergelijking

Gebruikt de Atlas Leefomgeving WMS via GetFeatureInfo (pixel-point query
op de gerenderde rasterlaag). Dit is geen typische REST-API maar WFS zou
per-punt queries veel complexer maken; GetFeatureInfo op 1x1-pixel BBOX
levert dezelfde waarde ~50ms sneller.

Layers gebruikt:
  - rivm_jaargemiddeld_NO2_actueel
  - rivm_jaargemiddeld_PM25_actueel  (dominant voor gezondheid, sterkste indicator)
  - rivm_jaargemiddeld_PM10_actueel

Referentiewaarden:
  - WHO 2021 advies: NO2 ≤10, PM2.5 ≤5, PM10 ≤15 μg/m3
  - EU-norm: NO2 ≤40, PM2.5 ≤25, PM10 ≤40 μg/m3
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

WMS_URL = "https://data.rivm.nl/geo/alo/wms"
TIMEOUT_S = 5.0

# WMS-layers — de 'actueel' varianten worden jaarlijks door RIVM vernieuwd
# met het meest recente complete meetjaar.
LAYERS = {
    "NO2": "rivm_jaargemiddeld_NO2_actueel",
    "PM10": "rivm_jaargemiddeld_PM10_actueel",
    "PM25": "rivm_jaargemiddeld_PM25_actueel",
}

# WHO 2021 air-quality guidelines (μg/m3, jaargemiddeld)
WHO_LIMITS = {"NO2": 10, "PM10": 15, "PM25": 5}
# EU-normen — hoger dan WHO; een adres dat binnen EU-norm zit maar boven
# WHO kan nog steeds serieuze gezondheidsimpact hebben.
EU_LIMITS = {"NO2": 40, "PM10": 40, "PM25": 25}


@dataclass
class Luchtkwaliteit:
    """Jaargemiddelde waarden voor één adres + vergelijking met normen."""

    no2_ug_m3: Optional[float]
    pm10_ug_m3: Optional[float]
    pm25_ug_m3: Optional[float]  # dominante indicator (WHO zegt: geen veilige drempel)
    pm25_vs_who: Optional[str]  # 'binnen' / 'boven' WHO-advies
    pm25_vs_eu: Optional[str]


async def fetch_luchtkwaliteit(rd_x: float, rd_y: float) -> Luchtkwaliteit:
    """Query alle 3 layers parallel.

    Voor fijnstof is adres-precisie belangrijk: op 50m kan PM2.5 al 20%
    verschillen door afstand tot drukke straten. RIVM grid = 25m.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        results = await asyncio.gather(
            *[_get_feature_info(client, layer, rd_x, rd_y) for layer in LAYERS.values()],
            return_exceptions=True,
        )

    # results volgorde = LAYERS.values() volgorde; map terug naar keys
    values: dict[str, Optional[float]] = {}
    for key, result in zip(LAYERS.keys(), results):
        values[key] = result if isinstance(result, (int, float)) else None

    pm25 = values.get("PM25")
    return Luchtkwaliteit(
        no2_ug_m3=_round(values.get("NO2")),
        pm10_ug_m3=_round(values.get("PM10")),
        pm25_ug_m3=_round(pm25),
        pm25_vs_who=_compare(pm25, WHO_LIMITS["PM25"]),
        pm25_vs_eu=_compare(pm25, EU_LIMITS["PM25"]),
    )


async def _get_feature_info(
    client: httpx.AsyncClient, layer: str, rd_x: float, rd_y: float
) -> Optional[float]:
    """Eén GetFeatureInfo-call: query de layer op exact 1 pixel.

    Kleine BBOX rond het punt (50m vierkant); we zetten width=height=3 en
    query op middelste pixel (x=y=1). Resultaat is de GRAY_INDEX property
    die de jaargemiddelde waarde bevat.
    """
    half = 25.0  # 50m x 50m BBOX
    bbox = f"{rd_x - half},{rd_y - half},{rd_x + half},{rd_y + half}"
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetFeatureInfo",
        "layers": layer,
        "query_layers": layer,
        "bbox": bbox,
        "width": "3",
        "height": "3",
        "srs": "EPSG:28992",
        "x": "1",
        "y": "1",
        "info_format": "application/json",
    }
    try:
        resp = await client.get(WMS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    feats = data.get("features", [])
    if not feats:
        return None
    props = feats[0].get("properties", {})
    # Verschillende layers gebruiken verschillende property-namen; we
    # pakken de eerste numerieke.
    for v in props.values():
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _round(v: Optional[float]) -> Optional[float]:
    return round(v, 1) if v is not None else None


def _compare(value: Optional[float], limit: float) -> Optional[str]:
    if value is None:
        return None
    return "binnen" if value <= limit else "boven"
