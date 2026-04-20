"""
Klimaateffectatlas adapter — funderingsrisico + hittestress.

Input  : RD- of WGS84-coordinaten
Output : paalrot-risico (% panden in buurt), hittestress-score, wateroverlast

De Klimaateffectatlas publiceert via ArcGIS Online. Twee typen services:
  - FeatureServer (polygonen per buurt)    -> point-in-polygon query
  - ImageServer   (raster van risicoscores) -> identify op pixel

Voor de 2 sterkste indicatoren uit het product-ontwerp:
  1. **Funderingsrisico (paalrot)** — meest kritieke financiële parameter;
     hypotheekverstrekkers kijken hier naar.
  2. **Hittestress (warme nachten)** — comfort + gezondheid; meest tastbaar
     voor bewoners.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

# Feature + Image services bij ArcGIS Online Nederland
PAALROT_URL = (
    "https://services.arcgis.com/nSZVuSZjHpEZZbRo/arcgis/rest/services/"
    "Klimaateffectatlas_Risico_paalrot/FeatureServer/3"  # Layer 3 = huidig
)
HITTE_URL = (
    "https://image.arcgisonline.nl/arcgis/rest/services/"
    "KEA/Hittestress_door_warme_nachten_huidig/ImageServer"
)
WATEROVERLAST_URL = (
    "https://image.arcgisonline.nl/arcgis/rest/services/"
    "KEA/Waterdiepte_bij_intense_neerslag/ImageServer"
)
TIMEOUT_S = 6.0

# Hittestress-waarden (1-5): betekenis volgens Klimaateffectatlas-legenda
HITTE_LABELS = {
    1: "zeer laag",
    2: "laag",
    3: "middel",
    4: "hoog",
    5: "zeer hoog",
}


@dataclass
class Klimaatrisico:
    """Samengesteld risicoprofiel op adres-niveau."""

    # Funderingsrisico (paalrot)
    paalrot_aantal_panden_in_buurt: Optional[int]
    paalrot_pct_sterk_risico: Optional[float]  # % panden met 'sterk' klimaatscenario risico
    paalrot_pct_mild_risico: Optional[float]
    paalrot_buurtnaam: Optional[str]

    # Hittestress (klasse 1-5)
    hittestress_klasse: Optional[int]
    hittestress_label: Optional[str]

    # Wateroverlast bij extreme neerslag (cm waterdiepte; 0 = geen overlast)
    waterdiepte_cm: Optional[int]


async def fetch_klimaat(lat: float, lon: float, rd_x: float, rd_y: float) -> Klimaatrisico:
    """Query de 3 klimaat-datasets parallel.

    We gebruiken WGS84 voor alle queries; ArcGIS transformeert intern naar
    hun native EPSG:28992 waar nodig.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        paalrot_task = _fetch_paalrot(client, lat, lon)
        hitte_task = _fetch_image_value(client, HITTE_URL, lat, lon)
        water_task = _fetch_image_value(client, WATEROVERLAST_URL, lat, lon)
        paalrot, hitte_val, water_val = await asyncio.gather(
            paalrot_task, hitte_task, water_task, return_exceptions=False
        )

    hittestress_klasse = None
    hittestress_label = None
    if isinstance(hitte_val, (int, float)) and hitte_val > 0:
        hittestress_klasse = int(round(hitte_val))
        hittestress_label = HITTE_LABELS.get(hittestress_klasse)

    # Wateroverlast: ImageServer geeft pixel-waarde in de raster; voor deze
    # laag is de eenheid cm waterdiepte bij T=100 piek-neerslag.
    waterdiepte = (
        int(round(water_val)) if isinstance(water_val, (int, float)) and water_val > 0 else 0
    )

    return Klimaatrisico(
        paalrot_aantal_panden_in_buurt=paalrot.get("aantal_panden"),
        paalrot_pct_sterk_risico=paalrot.get("pct_sterk"),
        paalrot_pct_mild_risico=paalrot.get("pct_mild"),
        paalrot_buurtnaam=paalrot.get("buurtnaam"),
        hittestress_klasse=hittestress_klasse,
        hittestress_label=hittestress_label,
        waterdiepte_cm=waterdiepte,
    )


async def _fetch_paalrot(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Point-in-polygon query op paalrot FeatureServer.

    Returnt een dict met aantal_panden, pct_sterk, pct_mild, buurtnaam.
    Als geen buurt hit: alle velden None.
    """
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "buurtnaam,aantal_pan,percentage,percenta_1,sterke_c_1",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = await client.get(f"{PAALROT_URL}/query", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    feats = data.get("features", [])
    if not feats:
        return {}
    attrs = feats[0].get("attributes", {})

    # Veldnamen zijn afgekort in het bronbestand (10-char ArcGIS-limit):
    #   percentage  = % panden met mild risico (huidig klimaatscenario)
    #   percenta_1  = % panden in risicoklasse (mild scenario, alt def)
    #   sterke_c_1  = % panden met risico onder sterk klimaatscenario
    #
    # Voor de gebruiker = simpel 'huidig' (%) en 'worst case' (%).
    return {
        "buurtnaam": attrs.get("buurtnaam"),
        "aantal_panden": attrs.get("aantal_pan"),
        "pct_mild": _to_pct(attrs.get("percentage")),
        "pct_sterk": _to_pct(attrs.get("sterke_c_1")),
    }


async def _fetch_image_value(
    client: httpx.AsyncClient, base_url: str, lat: float, lon: float
) -> Optional[float]:
    """Identify-call op ImageServer: pixel-waarde op WGS84 punt.

    Voor hittestress: integer 1-5 (risicoklasse).
    Voor wateroverlast: integer cm waterdiepte (0-100+).
    """
    geom = f'{{"x":{lon},"y":{lat},"spatialReference":{{"wkid":4326}}}}'
    params = {
        "geometry": geom,
        "geometryType": "esriGeometryPoint",
        "f": "json",
    }
    try:
        resp = await client.get(f"{base_url}/identify", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    v = data.get("value")
    if v in (None, "NoData", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_pct(v) -> Optional[float]:
    """FeatureServer-percentages staan al op 0-100 schaal; None als leeg."""
    if v is None:
        return None
    try:
        n = float(v)
        # Sommige FeatureServer-layers gebruiken 0-1 ipv 0-100; we normaliseren
        # door te kijken of de waarde <=1 is.
        if 0 <= n <= 1:
            return round(n * 100, 1)
        return round(n, 1)
    except (TypeError, ValueError):
        return None
