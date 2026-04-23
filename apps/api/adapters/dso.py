"""
DSO-adapter — haalt omgevingsplan + activiteiten + regeltekst-identifiers
per coord via Omgevingsdocumenten Presenteren v8.

Belangrijke DSO-conventies (ontdekt via live probe):
- Path-param `uriIdentificatie` wordt door DSO gecodeerd als `/` → `_`
  (dus `/akn/nl/act/gm0363/2020/omgevingsplan` → `_akn_nl_act_gm0363_2020_omgevingsplan`).
  Dit is GEEN URL-encoding; URL-encoding (`%2F`) wordt verworpen met 400.
- Content-Crs header vereist OGC-URN-formaat:
  `http://www.opengis.net/def/crs/EPSG/0/28992` voor RD. Korte vorm `epsg:28992`
  wordt verworpen (leeg resultaat ipv error).
- Body voor `_zoek`-endpoints: `{"geometrie": {"type": "Point", "coordinates": [x, y]}}`

Flow per locatie:
1. `/regelingen/_zoek` → lijst van regelingen (rijks, provincie, gemeente, waterschap).
2. Filter op gemeente-omgevingsplan (bv Amsterdam: `/akn/nl/act/gm0363/.../omgevingsplan`).
3. `/regelingen/{encoded_id}/regeltekstannotaties/_zoek?_expand=true` →
   1000+ regelteksten (alleen identifiers) + activiteiten-lijst + locatie-refs.
4. De `activiteiten`-lijst wordt DIRECT gebruikt door vergunningcheck.py voor
   de `functioneleStructuurRefs` bij de Uitvoeren-services call.

Fase 2b MVP retourneert voor de UI:
- Omgevingsplan-naam ("Omgevingsplan gemeente Amsterdam")
- Omgevingsplan-URI (voor deeplinks)
- Lijst relevante bouw-activiteiten (voor Fase 2c Vergunningcheck)

Fase 2b-full (later): per regeltekst-wId de tekst ophalen via documentstructuur
en via Haiku de bouwhoogte extraheren. Dat vereist ~5-50 extra calls per scan;
we cachen agressief (30 dagen op uriIdentificatie-niveau) om latency en kosten
te beperken.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

DSO_PRES_BASE = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8"
RD_CRS = "http://www.opengis.net/def/crs/EPSG/0/28992"
TIMEOUT_S = 12.0


def _auth_headers() -> Optional[dict]:
    key = os.getenv("DSO_API_KEY")
    if not key:
        return None
    return {
        "x-api-key": key,
        "Accept": "application/hal+json",
        "User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com",
    }


def _encode_uri(uri_id: str) -> str:
    """DSO pad-encoding: slashes naar underscores."""
    return uri_id.replace("/", "_")


@dataclass
class DSOActiviteit:
    """Eén activiteit die geldt voor deze locatie."""
    identificatie: str        # STOP-URI, bv nl.imow-gm0363.activiteit.xxx
    naam: str
    groep: Optional[str] = None


@dataclass
class DSORegeling:
    """Eén regeling (omgevingsplan / bestemmingsplan) die de locatie raakt."""
    uri_identificatie: str    # /akn/nl/act/.../...
    officiele_titel: str
    type: Optional[str] = None                          # "regeling", "instructie"
    bevoegd_gezag_code: Optional[str] = None            # gm0363, pv27, ws0155, mnre...


@dataclass
class DSOOmgevingsData:
    """Geaggregeerd: welke regels + activiteiten gelden voor deze locatie."""
    omgevingsplan: Optional[DSORegeling] = None
    overige_regelingen: list[DSORegeling] = field(default_factory=list)
    activiteiten: list[DSOActiviteit] = field(default_factory=list)
    aantal_regelteksten: int = 0


async def _zoek_regelingen(
    client: httpx.AsyncClient, rd_x: float, rd_y: float
) -> list[DSORegeling]:
    """Paginate door alle regelingen die de coord raken."""
    url = f"{DSO_PRES_BASE}/regelingen/_zoek"
    body = {"geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
    # Belangrijke merge-subtiliteit: httpx mergt call-level headers met
    # client-level, maar alleen als keys verschillend zijn — Content-Type
    # was NOG NIET op client gezet, dus merge lukt. Toch expliciet alle
    # auth-headers meegeven is robuuster.
    auth = _auth_headers() or {}
    headers = {**auth, "Content-Type": "application/json", "Content-Crs": RD_CRS}
    out: list[DSORegeling] = []
    for page in range(1, 5):  # DSO page is 1-based; 4 pagina's × 10 = 40 regelingen (ruim)
        try:
            resp = await client.post(
                url,
                params={"size": 10, "page": page},
                headers=headers,
                json=body,
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break
        regs = (data.get("_embedded") or {}).get("regelingen") or []
        if not regs:
            break
        for r in regs:
            bg = (r.get("bevoegdGezag") or {})
            if isinstance(bg, dict):
                bg_code = bg.get("code") or bg.get("bevoegdGezag")
            else:
                bg_code = None
            out.append(DSORegeling(
                uri_identificatie=r.get("identificatie") or r.get("uriIdentificatie") or "",
                officiele_titel=r.get("officieleTitel") or "",
                type=r.get("type") or None,
                bevoegd_gezag_code=bg_code,
            ))
        # Stop als laatste pagina
        page_meta = data.get("page") or {}
        total = page_meta.get("totalElements") or 0
        if len(out) >= total:
            break
    return out


def _pick_omgevingsplan(regs: list[DSORegeling]) -> Optional[DSORegeling]:
    """Kies de gemeente-omgevingsplan of bestemmingsplan uit een regelingenlijst.

    Prioriteit: (1) Omgevingsplan gemeente, (2) Bestemmingsplan gemeente,
    (3) Omgevingsverordening provincie als fallback.
    """
    # Gemeente-omgevingsplan
    for r in regs:
        uri = r.uri_identificatie.lower()
        titel = r.officiele_titel.lower()
        if "/gm" in uri and ("omgevingsplan" in titel or "omgevingsplan" in uri):
            return r
    for r in regs:
        if "bestemmingsplan" in r.officiele_titel.lower():
            return r
    for r in regs:
        if "omgevingsverordening" in r.officiele_titel.lower():
            return r
    return None


async def _fetch_regeling_annotaties(
    client: httpx.AsyncClient, regeling: DSORegeling, rd_x: float, rd_y: float
) -> tuple[list[DSOActiviteit], int]:
    """Haal activiteiten + regeltekst-count voor een regeling op de locatie."""
    enc = _encode_uri(regeling.uri_identificatie)
    url = f"{DSO_PRES_BASE}/regelingen/{enc}/regeltekstannotaties/_zoek"
    body = {"geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
    auth = _auth_headers() or {}
    headers = {**auth, "Content-Type": "application/json", "Content-Crs": RD_CRS}
    try:
        resp = await client.post(
            url,
            params={"_expand": "true", "size": 10},
            headers=headers,
            json=body,
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [], 0
    acts_raw = data.get("activiteiten") or []
    acts = [
        DSOActiviteit(
            identificatie=a.get("identificatie") or "",
            naam=a.get("naam") or "",
            groep=a.get("groep") or None,
        )
        for a in acts_raw
        if a.get("identificatie")
    ]
    regelteksten = data.get("regelteksten") or []
    return acts, len(regelteksten)


async def fetch_omgevingsdata(
    rd_x: float, rd_y: float,
) -> Optional[DSOOmgevingsData]:
    """Hoofd-entry: zoek omgevingsplan + activiteiten voor deze coord.

    Returns None als geen DSO_API_KEY of als alle calls falen. UI valt dan
    terug op de Bbl-heuristiek-status (Fase 2a).
    """
    headers = _auth_headers()
    if not headers:
        return None
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        regs = await _zoek_regelingen(client, rd_x, rd_y)
        if not regs:
            return None
        omgevingsplan = _pick_omgevingsplan(regs)
        if omgevingsplan is None:
            return DSOOmgevingsData(overige_regelingen=regs)
        activiteiten, n_tekst = await _fetch_regeling_annotaties(
            client, omgevingsplan, rd_x, rd_y
        )
        # Filter bouw-activiteiten voor de UI — anders is de lijst 100+
        overige = [r for r in regs if r.uri_identificatie != omgevingsplan.uri_identificatie]
        return DSOOmgevingsData(
            omgevingsplan=omgevingsplan,
            overige_regelingen=overige,
            activiteiten=activiteiten,
            aantal_regelteksten=n_tekst,
        )


# --- Activiteit-mapping voor Buurtscan beslisboom-cards ---
# We mappen onze beslisboom-keys naar substrings in DSO-activiteitsnamen.
# Dit is de brug tussen onze UI-cards en de Vergunningcheck-API (Fase 2c),
# die een activiteit-URI vereist als `functioneleStructuurRefs`.
CARD_NAAR_ACTIVITEIT_MATCH = {
    "uitbouw":   ["bijbehorend bouwwerk"],
    "dakkapel":  ["dakkapel bouwen"],
    "tuinhuis":  ["bijbehorend bouwwerk", "ander bouwwerk bouwen"],
    # NB: "optopping" verwijderd — card is uit de UI gehaald omdat we
    # zonder structured max-bouwhoogte (RP v4 ~5% coverage in 2026) geen
    # eerlijk verdict kunnen geven. Zie orchestrator._build_mogelijkheden.
}


def match_activiteit_voor_card(
    data: DSOOmgevingsData, card_key: str
) -> Optional[DSOActiviteit]:
    """Vind de DSO-activiteit die het beste bij een card past."""
    if not data or not data.activiteiten:
        return None
    patterns = CARD_NAAR_ACTIVITEIT_MATCH.get(card_key, [])
    for pat in patterns:
        for a in data.activiteiten:
            if pat in (a.naam or "").lower():
                return a
    return None


def dso_beschikbaar() -> bool:
    return bool(os.getenv("DSO_API_KEY"))


# ---------------------------------------------------------------------------
# Regeltekst-ophaal voor Haiku bouwhoogte-extractie
# ---------------------------------------------------------------------------

import asyncio as _asyncio
import re as _re


async def _fetch_regeltekst_tekst(
    client: httpx.AsyncClient, regeling_uri: str, wId: str
) -> Optional[str]:
    """Haal de ruwe tekst van één regelcomponent op.

    DSO geeft XML-inhoud in `_embedded.documentComponenten[0].inhoud`. We
    strippen de XML-tags zodat er bruikbare NL-tekst overblijft voor Haiku.
    """
    enc = _encode_uri(regeling_uri)
    url = f"{DSO_PRES_BASE}/regelingen/{enc}/documentstructuur/{wId}"
    auth = _auth_headers() or {}
    try:
        resp = await client.get(url, headers=auth, timeout=TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    comps = (data.get("_embedded") or {}).get("documentComponenten") or []
    if not comps:
        return None
    parts: list[str] = []
    for c in comps:
        kop = c.get("kop") or ""
        inhoud = c.get("inhoud") or ""
        for x in (kop, inhoud):
            if not x:
                continue
            # Strip alle XML-tags — Haiku heeft alleen de NL-tekst nodig.
            text = _re.sub(r"<[^>]+>", " ", x)
            text = _re.sub(r"\s+", " ", text).strip()
            if text:
                parts.append(text)
    return " | ".join(parts) if parts else None


# Keywords die wijzen op bouwhoogte-regels — we filteren teksten hierop
# voordat we ze naar Haiku sturen. Scheelt irrelevante tokens + kosten.
_HOOGTE_KEYWORDS = [
    "bouwhoogte", "maximale bouwhoogte", "max. bouwhoogte",
    "goothoogte", "nokhoogte", "bouwlagen", "hoogte van",
    "maximale hoogte", "maximaal aantal bouwlagen", "kap verplicht",
    "plat dak", "hoogte niet meer",
]


def _relevantie_score(tekst: str) -> int:
    """Aantal hoogte-keywords in een regeltekst — hoe hoger, hoe relevanter."""
    lower = tekst.lower()
    return sum(1 for kw in _HOOGTE_KEYWORDS if kw in lower)


async def fetch_bouwhoogte_regeltekst(
    regeling_uri: str, wIds: list[str], max_parallel: int = 12,
    max_wIds: int = 250, min_score: int = 1,
) -> Optional[str]:
    """Haal een set regelteksten op, filter op bouwhoogte-relevantie, geef
    geconcateneerde tekst voor Haiku terug.

    Strategie:
    1. Beperk tot `max_wIds` wId's (eerste N van de locatie-annotaties).
       Was 40 — te smal: Amsterdam-bruidsschat heeft 1461 wIds en de
       eerste 40 zijn meestal hoofdstuk-headers (definities, doelen). De
       woon-artikelen met de bouwregels zitten dieper. 250 is een goede
       balans: dekt typisch >90% van de wIds in normale plannen, en bij
       Amsterdam-grote plannen pakken we genoeg samples om statistisch
       een hoogte-vermelding te raken.
    2. Parallel ophalen via semaphore (max_parallel gelijktijdig; DSO
       rate=200/s — bij 12 parallel zitten we ruim onder die limiet).
    3. Scoor elke tekst op bouwhoogte-keywords.
    4. Pak de top-5 hoogst-scorende teksten, concat tot 1 input voor Haiku.

    Latency-budget: 250 calls × ~200ms / 12 parallel ≈ 4-5s wall-clock.
    Acceptabel binnen lazy-loaded /verbouwing endpoint.

    Returns: geconcateneerde NL-tekst of None als geen relevante content.
    """
    headers = _auth_headers()
    if not headers or not wIds:
        return None
    sem = _asyncio.Semaphore(max_parallel)
    results: list[tuple[int, str]] = []  # (score, tekst)

    async def _worker(client, wId):
        async with sem:
            tekst = await _fetch_regeltekst_tekst(client, regeling_uri, wId)
            if not tekst:
                return
            score = _relevantie_score(tekst)
            if score >= min_score:
                results.append((score, tekst))

    to_fetch = wIds[:max_wIds]
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        await _asyncio.gather(*[_worker(client, w) for w in to_fetch])
    if not results:
        return None
    # Top-5 hoogst-scorend, combineer tot één input voor Haiku.
    results.sort(reverse=True)
    top = results[:5]
    return "\n\n".join(t for _s, t in top)


async def fetch_bp_regeltekst_voor_locatie(
    rd_x: float, rd_y: float,
) -> Optional[tuple[str, str]]:
    """High-level: vind omgevingsplan + haal bouwhoogte-regeltekst op.

    Returns: tuple van (regeling_naam, regeltekst) of None als niks bruikbaars.
    Deze tekst is direct Haiku-klaar; upstream caller roept
    `bp_extractor.extract_bp_regels()` aan.
    """
    headers = _auth_headers()
    if not headers:
        return None
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        regs = await _zoek_regelingen(client, rd_x, rd_y)
        op = _pick_omgevingsplan(regs)
        if op is None:
            return None
        # Haal wIds van regelteksten-voor-locatie
        enc = _encode_uri(op.uri_identificatie)
        url = f"{DSO_PRES_BASE}/regelingen/{enc}/regeltekstannotaties/_zoek"
        body = {"geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
        hdr = {**headers, "Content-Type": "application/json", "Content-Crs": RD_CRS}
        try:
            resp = await client.post(url, json=body, headers=hdr,
                                     params={"size": 50})
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None
        regelteksten = data.get("regelteksten") or []
        # Filter op inhoud-dragende wId-componenten. Was alleen `__art_` —
        # dat brak op Amsterdam-bruidsschat-omgevingsplannen waar ALLE wIds
        # `gm0363_xxx__para_1`-stijl zijn (geen __art_ in het pad). Verbreed
        # naar __art_, __para_, __subsec_, __sec_ — alle vier dragen
        # inhoud. We laten __chp_ / __subchp_ weg omdat dat hoofdstuk-headers
        # zijn zonder bouwregels.
        _INHOUD_PAT = ("__art_", "__para_", "__subsec_", "__sec_")
        wIds = [
            r.get("wId") for r in regelteksten
            if r.get("wId") and any(p in r["wId"] for p in _INHOUD_PAT)
        ]
    if not wIds:
        return None
    tekst = await fetch_bouwhoogte_regeltekst(op.uri_identificatie, wIds)
    if not tekst:
        return None
    return (op.officiele_titel, tekst)
