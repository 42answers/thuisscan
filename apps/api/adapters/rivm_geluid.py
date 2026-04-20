"""
RIVM 3D Geluid adapter — Lden op gevel (decibel).

Input  : RD-coordinaten (EPSG:28992)
Output : totale geluidsbelasting Lden in dB + dominante bron (weg/trein/vlieg)

Lden = "Level day-evening-night" — gewogen 24-uursgemiddelde met +5 dB
straf voor avond (19-23h) en +10 dB straf voor nacht (23-07h). De EU-norm
voor 'ernstig gehinderd' ligt op Lden > 55 dB. WHO 2018 adviseert max
53 dB voor wegverkeer.

Datasets uit ALO-WMS (rivm_20220601_Geluid_lden_*_2020):
  - allebronnen  — totale cumulatieve belasting (sterkste indicator)
  - wegverkeer   — meest dominant in stedelijk NL
  - treinverkeer — relevant nabij spoorlijnen
  - vliegverkeer — relevant onder aanvliegroutes

Voor MVP pakken we 'allebronnen' + determineren we dominante bron door
de 3 bron-layers naast elkaar te vergelijken.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

WMS_URL = "https://data.rivm.nl/geo/alo/wms"
TIMEOUT_S = 5.0

# Meest recente publicatie (juni 2022, peiljaar 2020). Deze layers klasseren
# de Lden in 5-dB-buckets — de GRAY_INDEX is dus een representatieve waarde
# in dB (bv. 66 = 65-70 dB bucket).
LAYERS = {
    "allebronnen": "rivm_20220601_Geluid_lden_allebronnen_2020",
    "wegverkeer":  "rivm_20220601_Geluid_lden_wegverkeer_2020",
    "treinverkeer": "rivm_20220601_Geluid_lden_treinverkeer_2020",
    "vliegverkeer": "rivm_20220601_Geluid_lden_vliegverkeer_2020",
}

# WHO 2018 advies: ≤53 dB wegverkeer, ≤45 dB treinverkeer, ≤45 dB vliegverkeer
# EU-hinderdrempel: Lden > 55 dB = ernstige hinder
WHO_LDEN = 53
EU_HINDER = 55
EU_ERNSTIG = 65  # EU 'ernstig gehinderd' drempel


@dataclass
class GeluidOpGevel:
    """Geluidsbelasting Lden op adres-niveau."""

    lden_totaal_db: Optional[int]  # alle bronnen gecombineerd
    dominante_bron: Optional[str]  # 'wegverkeer' / 'treinverkeer' / 'vliegverkeer'
    per_bron: dict  # {bron: dB, ...}
    vs_who: Optional[str]  # 'binnen' / 'boven'
    hinder_niveau: Optional[str]  # 'geen' / 'matig' / 'ernstig'


async def fetch_geluid(rd_x: float, rd_y: float) -> Optional[GeluidOpGevel]:
    """Query alle 4 geluid-layers parallel; determineer dominante bron."""
    if rd_x == 0 or rd_y == 0:
        return None

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        tasks = [
            _get_feature_info(client, layer, rd_x, rd_y)
            for layer in LAYERS.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    values: dict[str, Optional[int]] = {}
    for key, res in zip(LAYERS.keys(), results):
        if isinstance(res, (int, float)):
            values[key] = int(round(res))
        else:
            values[key] = None

    totaal = values.get("allebronnen")
    # Bepaal dominante bron door hoogste waarde van de 3 bron-layers
    bronnen = {k: v for k, v in values.items() if k != "allebronnen" and v is not None and v > 0}
    dominant = max(bronnen, key=bronnen.get) if bronnen else None

    vs_who = None
    hinder = None
    if totaal is not None:
        vs_who = "binnen" if totaal <= WHO_LDEN else "boven"
        if totaal < EU_HINDER:
            hinder = "geen"
        elif totaal < EU_ERNSTIG:
            hinder = "matig"
        else:
            hinder = "ernstig"

    return GeluidOpGevel(
        lden_totaal_db=totaal,
        dominante_bron=dominant,
        per_bron={k: v for k, v in values.items() if k != "allebronnen"},
        vs_who=vs_who,
        hinder_niveau=hinder,
    )


async def _get_feature_info(
    client: httpx.AsyncClient, layer: str, rd_x: float, rd_y: float
) -> Optional[float]:
    """WMS GetFeatureInfo voor één layer op exact dit punt."""
    # Kleine BBOX rondom het punt — RIVM raster = ~10m, 20m vierkant is ruim
    half = 10.0
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetFeatureInfo",
        "layers": layer,
        "query_layers": layer,
        "bbox": f"{rd_x - half},{rd_y - half},{rd_x + half},{rd_y + half}",
        "width": "3", "height": "3",
        "srs": "EPSG:28992",
        "x": "1", "y": "1",
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
    for v in props.values():
        if isinstance(v, (int, float)):
            return float(v)
    return None
