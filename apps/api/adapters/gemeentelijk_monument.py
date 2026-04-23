"""
Gemeentelijk monument — per-gemeente aanpak (geen landelijk register).

Rijksmonumenten zitten in één centraal register (RCE) — gemeentelijke
monumenten NIET. Elke gemeente voert z'n eigen lijst. Enkele grote
gemeenten publiceren dit als open data; de meeste niet.

Deze adapter:
1. **Amsterdam (GM0363)** → directe API-call naar data.amsterdam.nl
   (`/v1/monumenten/monumenten/?betreftBagPand.identificatie={pand_id}`)
   Status-veld kan 'Gemeentelijk monument', 'Rijksmonument', 'Orde 2', etc.
2. **Overige gemeenten** → géén data beschikbaar. We retourneren `checked=False`
   en de frontend toont een neutrale chip "Check ook bij gemeente" met een
   deeplink (Google-search zodat de user het register vindt zonder dat wij
   elke gemeente-URL moeten onderhouden).

Uitbreiden per gemeente: voeg een async fetch-functie toe + registreer in
FETCHERS dict. Rotterdam, Utrecht, Den Haag, Eindhoven, Groningen hebben
allemaal een eigen open-data portaal; PR-waardig werk, niet MVP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

TIMEOUT_S = 8.0
HEADERS = {"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"}


@dataclass
class GemMonument:
    """Gemeentelijk monument-status (of 'unknown' voor gemeenten zonder API)."""
    checked: bool                    # hebben we daadwerkelijk gecontroleerd?
    is_monument: Optional[bool]      # None als checked=False; True/False bij Amsterdam
    status: Optional[str] = None     # bv 'Gemeentelijk monument', 'Orde 2'
    naam: Optional[str] = None       # bv 'Tweede Weteringdwarsstraat 71'
    deeplink: Optional[str] = None   # naar gemeentelijk/nationaal register


async def _fetch_amsterdam(
    client: httpx.AsyncClient, bag_pand_id: str
) -> Optional[GemMonument]:
    """Amsterdam monumenten-dataset: BAG-pand → monument-registratie.

    Bevat ZOWEL rijksmonumenten als gemeentelijke monumenten als 'orde-
    panden' (Amsterdamse orde-1/2 beschermingsclassificatie uit de welstand).
    """
    if not bag_pand_id:
        return None
    url = "https://api.data.amsterdam.nl/v1/monumenten/monumenten/"
    params = {
        "betreftBagPand.identificatie": bag_pand_id,
        "_format": "json",
        "_pageSize": "3",
    }
    try:
        resp = await client.get(url, params=params, timeout=TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    monumenten = (data.get("_embedded") or {}).get("monumenten") or []
    if not monumenten:
        return GemMonument(checked=True, is_monument=False)
    # We kunnen meerdere hits hebben (bv pand is deels rijksmonument); pak de
    # eerste die een status heeft, maar geef rijksmonument voorrang (want dat
    # is zwaarder beschermd dan gemeentelijk).
    gem = None
    rijks = None
    orde = None
    for m in monumenten:
        status = (m.get("status") or "").strip()
        if not status:
            continue
        if "rijks" in status.lower():
            rijks = m
        elif "gemeentelijk" in status.lower():
            gem = m
        else:
            orde = m  # bv Amsterdamse orde 1/2
    chosen = rijks or gem or orde or monumenten[0]
    status = chosen.get("status") or "onbekende status"
    adres = chosen.get("adressering") or ""
    nr = chosen.get("monumentnummer")
    deeplink = None
    if nr:
        deeplink = f"https://api.data.amsterdam.nl/v1/monumenten/monumenten/{chosen.get('identificatie')}?_format=json"
    return GemMonument(
        checked=True,
        is_monument=True,
        status=status,
        naam=adres or None,
        deeplink=deeplink,
    )


# Gemeentecode (CBS, 4-cijferig) → async fetcher
FETCHERS = {
    "0363": _fetch_amsterdam,
}


async def fetch_gemeentelijk_monument(
    gemeentecode: Optional[str], bag_pand_id: Optional[str],
    gemeente_naam: Optional[str] = None,
) -> GemMonument:
    """Dispatcher: kies juiste fetcher op basis van gemeentecode.

    Retourneert altijd een GemMonument; voor niet-geïmplementeerde gemeenten
    `checked=False` zodat de frontend een "onbekend — check bij gemeente" chip
    kan tonen met een Google-search-deeplink (geen maintenance op URL-tabel).
    """
    fetcher = FETCHERS.get((gemeentecode or "").lstrip("GM").zfill(4))
    if fetcher is None:
        # Fallback: generiek deeplink
        q_parts = ["gemeente"]
        if gemeente_naam:
            q_parts.append(gemeente_naam)
        q_parts.extend(["monumentenlijst", "register"])
        q = "+".join(q_parts)
        return GemMonument(
            checked=False,
            is_monument=None,
            deeplink=f"https://www.google.com/search?q={q}",
        )
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=HEADERS) as client:
        try:
            result = await fetcher(client, bag_pand_id or "")
        except Exception:
            result = None
    return result or GemMonument(checked=True, is_monument=False)
