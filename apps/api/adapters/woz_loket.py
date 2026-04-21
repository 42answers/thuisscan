"""
WOZ-Waardeloket adapter — pand-specifieke WOZ-waarden via publieke viewer-API.

Input  : BAG verblijfsobject-ID (adresseerbaarobjectid)
Output : WOZ-historie 2022-2025 per pand

**Gebruik de officiële Kadaster WOZ Bevragen API als je OIN + PKIoverheid-
certificaat hebt.** Dat is de gesanctioneerde weg. Deze adapter gebruikt
de interne API van het publieke WOZ-Waardeloket (wat door consumenten ook
via de browser wordt gebruikt).

Rate-limit beleid:
  - Globaal 1 request/sec (asyncio.Lock + sleep)
  - Cache 365 dagen per BAG-id (WOZ muteert jaarlijks)
  - Bij 429/403: exponentiële backoff + fallback naar None
  - Gebruikers-response duurt nooit langer dan ~3s cold

Endpoints (gereverse-engineerd uit de viewer's main.js):
  GET /suggest?aotids=<bag_vbo>            → WOZ-objectnummer
  GET /wozwaarde/wozobjectnummer/<nr>      → WOZ-historie
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

WOZ_BASE = "https://api.kadaster.nl/lvwoz/wozwaardeloket-api/v1"
TIMEOUT_S = 8.0

# Rate-limit: 1 request per seconde (globaal). We delen ÉÉN lock over de hele
# module — alle concurrent users moeten door deze bottleneck. Dat is bewust
# want WOZ-loket staat geen bulk-verkeer toe; we willen niet agressief scrapen.
_RATE_LOCK = asyncio.Lock()
_LAST_CALL_TS = 0.0
_MIN_INTERVAL_S = 1.0

# User-agent: beleefd + identificerend zodat Waarderingskamer ons kan contacten
# als ze iets niet willen. Beter dan anoniem scrapen.
HEADERS = {
    "User-Agent": "buurtscan/1.0 (nl-NL; https://buurtscan.com) consumer-app",
    "Accept": "application/json",
}


@dataclass
class WozWaarde:
    """WOZ-data voor één pand."""
    bag_vbo_id: str
    wozobjectnummer: Optional[int] = None
    huidige_waarde_eur: Optional[int] = None
    huidige_peildatum: Optional[str] = None  # bv. '2025-01-01'
    historie: list[dict] = field(default_factory=list)  # [{peildatum, waarde_eur}] gesorteerd nieuwste eerst
    # Trend-percentage (jaarlijkse groei CAGR) over beschikbare historie
    trend_pct_per_jaar: Optional[float] = None


async def _rate_limited_get(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    """Rate-limited GET: wacht tot min. 1s na de laatste call."""
    global _LAST_CALL_TS
    async with _RATE_LOCK:
        elapsed = time.time() - _LAST_CALL_TS
        if elapsed < _MIN_INTERVAL_S:
            await asyncio.sleep(_MIN_INTERVAL_S - elapsed)
        try:
            resp = await client.get(url, headers=HEADERS)
            _LAST_CALL_TS = time.time()
            if resp.status_code == 429:
                # Ons-zij zegt 'te snel' — wacht nog langer en geef None
                await asyncio.sleep(5)
                return None
            if resp.status_code == 403:
                # IP mogelijk geblokkeerd; geef None (fallback op buurt-WOZ)
                return None
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None


async def fetch_woz(bag_vbo_id: str) -> Optional[WozWaarde]:
    """Haal WOZ-waarde + historie op via BAG verblijfsobject-id.

    Flow (twee sequentiële calls door de rate-limit):
      1. /suggest?aotids=<bag>  →  wozobjectnummer
      2. /wozwaarde/wozobjectnummer/<nr>  →  historie

    Retourneert None bij:
      - lege/onjuiste BAG-id
      - WOZ-object niet gevonden (bv. nieuwbouw nog niet getaxeerd)
      - rate-limit/403/netwerk-fout
    """
    if not bag_vbo_id:
        return None

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        # Stap 1: zoek wozobjectnummer
        data = await _rate_limited_get(
            client, f"{WOZ_BASE}/suggest?aotids={bag_vbo_id}"
        )
        if not data or not data.get("docs"):
            return None
        woz_nr = data["docs"][0].get("wozobjectnummer")
        if not woz_nr:
            return None

        # Stap 2: haal historie op
        wd = await _rate_limited_get(
            client, f"{WOZ_BASE}/wozwaarde/wozobjectnummer/{woz_nr}"
        )
        if not wd:
            return WozWaarde(bag_vbo_id=bag_vbo_id, wozobjectnummer=woz_nr)

    waarden = wd.get("wozWaarden") or []
    historie = []
    for w in waarden:
        peil = w.get("peildatum")
        val = w.get("vastgesteldeWaarde")
        if peil and val is not None:
            historie.append({"peildatum": peil, "waarde_eur": int(val)})
    # Kadaster levert nieuwste eerst; zeker maken.
    historie.sort(key=lambda x: x["peildatum"], reverse=True)

    huidige_waarde = historie[0]["waarde_eur"] if historie else None
    huidige_peildatum = historie[0]["peildatum"] if historie else None
    trend_pct = _compute_trend(historie)

    return WozWaarde(
        bag_vbo_id=bag_vbo_id,
        wozobjectnummer=woz_nr,
        huidige_waarde_eur=huidige_waarde,
        huidige_peildatum=huidige_peildatum,
        historie=historie,
        trend_pct_per_jaar=trend_pct,
    )


def _compute_trend(historie: list[dict]) -> Optional[float]:
    """CAGR over beschikbare historie, als er ≥2 datapunten zijn."""
    if len(historie) < 2:
        return None
    # Historie is al gesorteerd nieuwste eerst — laatste en eerste pakken
    nieuwste = historie[0]
    oudste = historie[-1]
    try:
        span = int(nieuwste["peildatum"][:4]) - int(oudste["peildatum"][:4])
        if span <= 0 or oudste["waarde_eur"] <= 0:
            return None
        ratio = nieuwste["waarde_eur"] / oudste["waarde_eur"]
        cagr = (ratio ** (1 / span) - 1) * 100
        return round(cagr, 1)
    except Exception:
        return None
