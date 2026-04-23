"""
Ruimtelijke Plannen v4 adapter — haalt `maximum bouwhoogte` en
`maximum goothoogte` per locatie uit bestemmingsplannen (oude Wro).

Achtergrond: DSO omgevingsplan-API levert voor bruidsschat-regelingen GEEN
gestructureerde `Norm`-objecten (alleen vrije tekst). De Ruimtelijke Plannen
API (apart endpoint, eigen API-key) heeft wél gestructureerde `maatvoeringen`
op bestemmingsplannen uit de Wro-periode, inclusief expliciete
"maximum bouwhoogte (m)" en "maximum goothoogte (m)" waarden.

Flow:
  1. POST /plannen/_zoek met {geo: Point} → lijst van plannen die de
     locatie raken (meerdere typen: bestemmingsplannen, provinciale
     verordeningen, structuurvisies).
  2. Filter op type='bestemmingsplan' (alleen die hebben harde maatvoeringen).
  3. Per BP: GET /plannen/{id}/maatvoeringen → alle maatvoeringen in plan.
  4. Filter op naam bevat 'bouwhoogte' of 'goothoogte'.
  5. Meerdere waarden → pak de conservatiefste (= laagste max).

URL: https://ruimte.omgevingswet.overheid.nl/ruimtelijke-plannen/api/opvragen/v4
Header: `x-api-key: <RUIMTELIJKE_PLANNEN_API_KEY>`
Coord-systeem: EPSG:28992 (RD), Content-Crs-header vereist (formaat 'epsg:28992').
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx

RP_BASE = "https://ruimte.omgevingswet.overheid.nl/ruimtelijke-plannen/api/opvragen/v4"
TIMEOUT_S = 12.0


@dataclass
class RPMaatvoeringen:
    """Bouw-maatvoeringen uit het geldende bestemmingsplan."""
    max_bouwhoogte_m: Optional[float] = None
    max_goothoogte_m: Optional[float] = None
    max_bouwlagen: Optional[int] = None
    max_wooneenheden: Optional[int] = None
    # Plan waaruit deze waarden komen
    plan_id: Optional[str] = None
    plan_naam: Optional[str] = None


def _auth_headers() -> Optional[dict]:
    key = os.getenv("RUIMTELIJKE_PLANNEN_API_KEY")
    if not key:
        return None
    # BELANGRIJK: géén Accept-header meegeven — deze API geeft 406 bij
    # expliciet "Accept: application/json". Default (*/*) is OK.
    return {
        "x-api-key": key,
        "User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com",
    }


async def _zoek_plannen(
    client: httpx.AsyncClient, rd_x: float, rd_y: float
) -> list[dict]:
    """Alle plannen die de coord raken (bestemmingsplan, verordening, enz.)."""
    auth = _auth_headers() or {}
    headers = {**auth, "Content-Type": "application/json", "Content-Crs": "epsg:28992"}
    body = {"_geo": {"intersects": {"type": "Point", "coordinates": [rd_x, rd_y]}}}
    try:
        resp = await client.post(
            f"{RP_BASE}/plannen/_zoek",
            params={"size": 50},
            headers=headers,
            json=body,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    return (data.get("_embedded") or {}).get("plannen") or []


async def _fetch_maatvoeringen(
    client: httpx.AsyncClient, plan_id: str
) -> list[dict]:
    """Alle maatvoeringen in een plan (zonder geo-filter; snel)."""
    auth = _auth_headers() or {}
    try:
        resp = await client.get(
            f"{RP_BASE}/plannen/{plan_id}/maatvoeringen",
            params={"size": 100},
            headers=auth,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    return (data.get("_embedded") or {}).get("maatvoeringen") or []


async def _fetch_maatvoeringen_geo(
    client: httpx.AsyncClient, plan_id: str, rd_x: float, rd_y: float
) -> list[dict]:
    """Maatvoeringen van een plan GEFILTERD op locatie (bouwvlak-intersect).

    Geeft de precieze maatvoeringen voor deze coord. Soms 0 omdat het
    bouwvlak nét niet over de coord valt; dan fallen we terug op alle
    maatvoeringen in het plan.
    """
    auth = _auth_headers() or {}
    headers = {**auth, "Content-Type": "application/json", "Content-Crs": "epsg:28992"}
    body = {"_geo": {"intersects": {"type": "Point", "coordinates": [rd_x, rd_y]}}}
    try:
        resp = await client.post(
            f"{RP_BASE}/plannen/{plan_id}/maatvoeringen/_zoek",
            params={"size": 50},
            headers=headers,
            json=body,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    return (data.get("_embedded") or {}).get("maatvoeringen") or []


_NUM_RE = re.compile(r"[0-9]+(?:[.,][0-9]+)?")


def _parse_waarde(raw) -> Optional[float]:
    """Trek numerieke waarde uit een maatvoering-omvang."""
    if raw is None:
        return None
    s = str(raw).replace(",", ".")
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _extract_hoogtes(maatvoeringen: list[dict]) -> dict:
    """Reduceer een lijst maatvoeringen naar beste schatting per type.

    Als meerdere waarden voor hetzelfde type → pak de strengste
    (laagste max). Een bestemmingsplan kan meerdere bouwvlakken hebben
    met verschillende hoogtes; voor ons "wat mag hier" pakken we de
    conservatiefste om veilige indicaties te geven.
    """
    out = {
        "max_bouwhoogte_m": None,
        "max_goothoogte_m": None,
        "max_bouwlagen": None,
        "max_wooneenheden": None,
    }
    for m in maatvoeringen:
        omvang_list = m.get("omvang") or []
        for o in omvang_list:
            naam = (o.get("naam") or "").lower()
            w = _parse_waarde(o.get("waarde"))
            if w is None:
                continue
            if "bouwhoogte" in naam:
                cur = out["max_bouwhoogte_m"]
                out["max_bouwhoogte_m"] = w if cur is None else min(cur, w)
            elif "goothoogte" in naam:
                cur = out["max_goothoogte_m"]
                out["max_goothoogte_m"] = w if cur is None else min(cur, w)
            elif "bouwlagen" in naam or "aantal lagen" in naam:
                cur = out["max_bouwlagen"]
                out["max_bouwlagen"] = int(w) if cur is None else min(cur, int(w))
            elif "wooneenheden" in naam:
                cur = out["max_wooneenheden"]
                out["max_wooneenheden"] = int(w) if cur is None else min(cur, int(w))
    return out


async def fetch_maatvoeringen(
    rd_x: float, rd_y: float,
) -> Optional[RPMaatvoeringen]:
    """Hoofdentry: vind bestemmingsplan + beste maatvoeringen voor coord.

    Returns None als geen key, geen BP gevonden, of geen hoogte-maatvoering.
    """
    headers = _auth_headers()
    if not headers:
        return None
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        plannen = await _zoek_plannen(client, rd_x, rd_y)
        if not plannen:
            return None
        # Alleen bestemmingsplannen en beheersverordeningen (Wro) hebben
        # maatvoeringen. Provinciale verordeningen + structuurvisies niet.
        bp_candidates = [
            p for p in plannen
            if p.get("type") in ("bestemmingsplan", "beheersverordening")
        ]
        if not bp_candidates:
            return None
        # We proberen plannen één voor één tot er maatvoeringen met hoogte
        # zijn gevonden. Voorkeur: plannen die op specifiek perceel slaan
        # (kleine plannen zoals "Molenstraat 24") boven grote kaderplannen.
        for p in bp_candidates:
            plan_id = p.get("id")
            if not plan_id:
                continue
            # Probeer eerst geo-filter (strikt) — vaak nauwkeuriger
            mv = await _fetch_maatvoeringen_geo(client, plan_id, rd_x, rd_y)
            if not mv:
                # Fallback: alle maatvoeringen in plan (geeft ook bruikbare
                # indicatie, zij het op plan-niveau ipv perceel-niveau)
                mv = await _fetch_maatvoeringen(client, plan_id)
            if not mv:
                continue
            hoogtes = _extract_hoogtes(mv)
            if any(hoogtes[k] is not None for k in
                   ("max_bouwhoogte_m", "max_goothoogte_m")):
                return RPMaatvoeringen(
                    max_bouwhoogte_m=hoogtes["max_bouwhoogte_m"],
                    max_goothoogte_m=hoogtes["max_goothoogte_m"],
                    max_bouwlagen=hoogtes["max_bouwlagen"],
                    max_wooneenheden=hoogtes["max_wooneenheden"],
                    plan_id=plan_id,
                    plan_naam=p.get("naam"),
                )
    return None


def rp_beschikbaar() -> bool:
    return bool(os.getenv("RUIMTELIJKE_PLANNEN_API_KEY"))
