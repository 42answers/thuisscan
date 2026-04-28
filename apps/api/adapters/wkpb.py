"""
Wkpb-adapter — publiekrechtelijke beperkingen via PDOK BRK-PB WFS.

Doel: detecteer **gemeentelijke monumenten** (en verwante publiekrechtelijke
beperkingen) voor een pand. De Wkpb (Wet kenbaarheid publiekrechtelijke
beperkingen) dwingt gemeenten om monument-aanwijzingen te registreren in
de BRK; Kadaster publiceert dit als gratis WFS via PDOK.

Belangrijkste grondslagen (grondslagCode → betekenis):
  GWA — Gemeentewet: Aanwijzing gemeentelijk monument
  EWR — Erfgoedwet: Aanwijzing rijksmonument
  EWA — Erfgoedwet: Archeologisch rijksmonument
  EWS — Erfgoedwet: Voorbescherming stads- of dorpsgezicht

Input: RD-coord.
Output: lijst beperkingen met grondslag, datum, type.

Endpoint: https://service.pdok.nl/kadaster/wkpb/wfs/v1_0
Geen API-key nodig.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

WKPB_WFS = "https://service.pdok.nl/kadaster/wkpb/wfs/v1_0"
TIMEOUT_S = 8.0
HEADERS = {"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"}

# Monument-relevante Wkpb-grondslagen (Kadaster 3-letter codes).
# EWE is de feitelijke inschrijvingscode voor rijksmonumenten — EWR bestaat
# niet in de praktijk (verwarring in documentatie).
MONUMENT_GRONDSLAGEN = {
    "GWA": "gemeentelijk monument",
    "EWE": "rijksmonument",
    "EWA": "archeologisch rijksmonument",
    "EWS": "beschermd stads- of dorpsgezicht",
}


@dataclass
class WkpbBeperking:
    """Eén publiekrechtelijke beperking op het pand/perceel."""
    grondslag_code: str           # bv 'GWA'
    grondslag_omschrijving: str   # menselijke beschrijving
    monument_type: Optional[str] = None  # bv 'gemeentelijk monument' (via mapping)
    datum_in_werking: Optional[str] = None
    type_gebied: Optional[str] = None   # 'BAG', 'Perceel', etc.
    identificatie: Optional[str] = None


async def fetch_wkpb_monumenten(
    rd_x: float, rd_y: float, half_m: int = 10,
) -> list[WkpbBeperking]:
    """Zoek Wkpb-beperkingen in een kleine bbox rond coord.

    Filter op monument-gerelateerde grondslagen. `half_m` is de halve-
    breedte van de zoek-bbox; 10 m is ruim genoeg om het pand te pakken
    zonder buurpand-overlap.
    """
    if not (rd_x and rd_y):
        return []
    bbox = f"{rd_x - half_m},{rd_y - half_m},{rd_x + half_m},{rd_y + half_m},EPSG:28992"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "wkpb:pb_multipolygon",
        "bbox": bbox,
        "count": "10",
        "outputFormat": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=HEADERS) as client:
            resp = await client.get(WKPB_WFS, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []
    out: list[WkpbBeperking] = []
    seen_codes: set[str] = set()
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        gc = (p.get("grondslagCode") or "").strip().upper()
        if gc not in MONUMENT_GRONDSLAGEN:
            continue
        # Dedup op grondslag+identificatie (zelfde beperking kan meerdere
        # geometrie-delen hebben).
        key = f"{gc}:{p.get('identificatie','')}"
        if key in seen_codes:
            continue
        seen_codes.add(key)
        out.append(WkpbBeperking(
            grondslag_code=gc,
            grondslag_omschrijving=p.get("grondslagOmschrijving", ""),
            monument_type=MONUMENT_GRONDSLAGEN[gc],
            datum_in_werking=p.get("datumInWerking"),
            type_gebied=p.get("typeBeperkingsgebied"),
            identificatie=p.get("identificatie"),
        ))
    return out


def is_gemeentelijk_monument(beperkingen: list[WkpbBeperking]) -> bool:
    """GWA = gemeentewet-aanwijzing gemeentelijk monument (pand-niveau)."""
    return any(b.grondslag_code == "GWA" for b in beperkingen)


def is_rijksmonument(beperkingen: list[WkpbBeperking]) -> bool:
    """Strict: alleen EWE (Erfgoedwet rijksmonument PAND).

    EWA = archeologisch rijksmonument — dat gaat over de BODEM onder een
    groter gebied, niet over of dit pand zelf monument is. Inclusie van
    EWA gaf false positives op b.v. Vondelstraat/Steenstraat/Driebergen
    (gewone woningen op archeologisch beschermde grond).
    """
    return any(b.grondslag_code == "EWE" for b in beperkingen)


def has_archeologisch_monument(beperkingen: list[WkpbBeperking]) -> bool:
    """Aparte query — relevant voor graven/grondwerken, niet voor pand-bouw."""
    return any(b.grondslag_code == "EWA" for b in beperkingen)


def has_beschermd_gezicht_wkpb(beperkingen: list[WkpbBeperking]) -> bool:
    """EWS = beschermd stads-/dorpsgezicht (gebied-niveau).
    Backup voor RCE-detectie als die faalt."""
    return any(b.grondslag_code == "EWS" for b in beperkingen)
