"""
Vergunningcheck-adapter — officiële DSO Toepasbare Regels / Uitvoeren Services v3.

Correcte endpoint (na OpenAPI-spec-inspectie):
    POST /conclusie/_bepaal

Request-body vereist twee velden:
- `functioneleStructuurRefs`: lijst van werkzaamheid-URI's van vorm
  `http://toepasbare-regels.omgevingswet.overheid.nl/werkzaamheden/id/concept/{urn}`
  (deze URI's komen uit `/zoekinterface/v2/werkzaamheden/_zoek`).
- `_geo`: `{"intersects": GeoJSON-geometry}` — Point in WGS84 of RD.

Response: lijst van werkzaamheid-resultaten, elk met `activiteiten` en
`vraaggroepen`. Zonder antwoorden retourneert DSO de nog-te-beantwoorden
vragen (~5-15 per werkzaamheid). Voor een définitief verdict moet de user
doorklikken op Omgevingsloket en de vragen handmatig beantwoorden.

Voor Buurtscan-beslisboom-cards gebruiken we deze call om:
1. Te bevestigen dat de activiteit op deze locatie relevant is.
2. Te tonen welke aantal vragen een user moet beantwoorden.
3. Een officieel gemeentelijk bestuursorgaan ('bestuursorgaan.oin') te tonen.

Mapping van onze 4 cards naar DSO-werkzaamheden (hardcoded; stabiel binnen
de DSO Registratie Toepasbare Regels):
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

import httpx

VC_BASE = "https://service.omgevingswet.overheid.nl/publiek/toepasbare-regels/api/toepasbareregelsuitvoerenservices/v3"
TIMEOUT_S = 15.0

WERKZAAMHEDEN_BASE = "http://toepasbare-regels.omgevingswet.overheid.nl/werkzaamheden/id/concept"

# Card-key → werkzaamheid-URN (stabiel in DSO RTR-catalogus).
CARD_NAAR_WERKZAAMHEID = {
    "uitbouw":   "AanbouwPlaatsen",             # Aanbouw, uitbouw of bijgebouw bouwen
    "dakkapel":  "DakkapelPlaatsen",            # Dakkapel plaatsen/vervangen/veranderen
    "tuinhuis":  "AanbouwPlaatsen",             # Idem; 'bijgebouw' valt onder deze werkzaamheid
    # NB: "optopping" verwijderd uit cards — zie orchestrator._build_mogelijkheden
}


@dataclass
class VCResultaat:
    """Vergunningcheck-uitkomst voor één card/activiteit."""
    card: str                                   # onze card-key
    werkzaamheid_urn: str                       # DSO-URN (bv DakkapelPlaatsen)
    werkzaamheid_omschrijving: str              # menselijke tekst
    aantal_activiteiten: int = 0                # hoeveel sub-activiteiten van toepassing
    aantal_vragen: int = 0                      # aantal open vragen voor definitieve conclusie
    bestuursorgaan_bestuurslaag: Optional[str] = None  # 'gemeente', 'provincie', 'waterschap'
    bestuursorgaan_oin: Optional[str] = None


def _auth_headers() -> Optional[dict]:
    key = os.getenv("DSO_API_KEY")
    if not key:
        return None
    return {
        "x-api-key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com",
    }


async def _conclusie_bepaal(
    client: httpx.AsyncClient,
    card_key: str,
    urn: str,
    rd_x: float, rd_y: float,
) -> Optional[VCResultaat]:
    """Eén POST /conclusie/_bepaal voor één werkzaamheid.

    Zonder antwoorden retourneert de API de vragenlijst die de user zou moeten
    beantwoorden. We tellen ze + retourneren bestuursorgaan-info.
    """
    ref = f"{WERKZAAMHEDEN_BASE}/{urn}"
    body = {
        "functioneleStructuurRefs": [{"functioneleStructuurRef": ref}],
        "_geo": {"intersects": {"type": "Point", "coordinates": [rd_x, rd_y]}},
    }
    try:
        resp = await client.post(
            f"{VC_BASE}/conclusie/_bepaal",
            json=body,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    r = data[0]  # één werkzaamheid gevraagd, dus één resultaat
    acts = r.get("activiteiten") or []
    vragen_total = 0
    bestuursorgaan_laag = None
    bestuursorgaan_oin = None
    for a in acts:
        for vg in (a.get("vraaggroepen") or []):
            vragen_total += len(vg.get("vragen") or [])
        bg = a.get("bestuursorgaan") or {}
        if bg.get("bestuurslaag"):
            bestuursorgaan_laag = bestuursorgaan_laag or bg.get("bestuurslaag")
            bestuursorgaan_oin = bestuursorgaan_oin or bg.get("oin")
    omschrijving = r.get("omschrijving") or urn
    return VCResultaat(
        card=card_key,
        werkzaamheid_urn=urn,
        werkzaamheid_omschrijving=omschrijving,
        aantal_activiteiten=len(acts),
        aantal_vragen=vragen_total,
        bestuursorgaan_bestuurslaag=bestuursorgaan_laag,
        bestuursorgaan_oin=bestuursorgaan_oin,
    )


async def check_alle_werkzaamheden(
    rd_x: float, rd_y: float,
) -> dict[str, VCResultaat]:
    """Parallel 4 conclusie-calls, één per beslisboom-card.

    Returns: dict card_key → VCResultaat. Leeg bij ontbrekende DSO-key.
    Let op: we de-dupliceren — uitbouw en tuinhuis delen `AanbouwPlaatsen`.
    """
    headers = _auth_headers()
    if not headers:
        return {}
    # De-dupliceren op URN om dubbele API-calls te voorkomen
    uniq: dict[str, list[str]] = {}  # URN → [card-keys]
    for card, urn in CARD_NAAR_WERKZAAMHEID.items():
        uniq.setdefault(urn, []).append(card)
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        tasks = {
            urn: _conclusie_bepaal(client, cards[0], urn, rd_x, rd_y)
            for urn, cards in uniq.items()
        }
        results_list = await asyncio.gather(*tasks.values())
    out: dict[str, VCResultaat] = {}
    for (urn, cards), res in zip(uniq.items(), results_list):
        if res is None:
            continue
        # Propageer naar alle cards die deze URN delen
        for card in cards:
            out[card] = VCResultaat(
                card=card,
                werkzaamheid_urn=res.werkzaamheid_urn,
                werkzaamheid_omschrijving=res.werkzaamheid_omschrijving,
                aantal_activiteiten=res.aantal_activiteiten,
                aantal_vragen=res.aantal_vragen,
                bestuursorgaan_bestuurslaag=res.bestuursorgaan_bestuurslaag,
                bestuursorgaan_oin=res.bestuursorgaan_oin,
            )
    return out


def vc_beschikbaar() -> bool:
    return bool(os.getenv("DSO_API_KEY"))
