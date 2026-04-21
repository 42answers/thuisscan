"""
Leefbaarometer adapter — samengestelde leefbaarheidsscore + 5 sub-dimensies.

Input  : RD-coordinaten (EPSG:28992)
Output : totaalscore (1-9) + sub-scores per dimensie:
           - won : Woningen (voorraad, eigendom, leegstand)
           - fys : Fysieke Omgeving (geluid, lucht, dichtheid)
           - vrz : Voorzieningen (nabijheid van dagelijks + recreatief)
           - soc : Sociale Samenhang (demografie, huishoudens)
           - onv : Overlast & Onveiligheid (criminaliteit, overlast)

Bron: Leefbaarometer 3.0 (BZK), peiljaar 2024, 100m-grid.
Eén WMS GetFeatureInfo-call op 'lbm3:clippedgridscore24' levert alle
sub-scores tegelijk, geen 6 aparte calls nodig.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

# Leefbaarometer 3.0 (peiljaar 2024) op geo.leefbaarometer.nl.
# Hier zit de RECENTSTE publieke data; RIVM ALO heeft nog 2018 (oude 2.0).
WMS_URL = "https://geo.leefbaarometer.nl/wms"
# 'clippedgridscore24' = 100m-grid direct rondom het adres (fijnste detail).
# 'buurtscore24'       = geaggregeerd gemiddelde over de hele CBS-buurt.
# Beide halen we op — de verschil tussen de twee is diagnostisch:
# als de grid-score hoger is dan de buurt, zit dit adres in een betere
# uithoek binnen een gemiddelde buurt (of andersom).
LAYER_GRID = "lbm3:clippedgridscore24"
LAYER_BUURT = "lbm3:buurtscore24"
# Trend-layers — zelfde 100m-grid, maar i.p.v. absolute score de ONTWIKKELING
# (klasse 1-9; 1=sterk verslechterd, 5=geen verandering, 9=sterk verbeterd).
# We halen een 2-jaars en 10-jaars variant — kort termijn zegt iets over de
# huidige dynamiek (gentrification, verval), lang termijn over de structurele
# baan van de buurt.
LAYER_DEV_RECENT = "lbm3:clippedgridontwikkeling22_24"  # 2-jaars (2022→2024)
LAYER_DEV_LANG = "lbm3:clippedgridontwikkeling14_24"    # 10-jaars (2014→2024)
TIMEOUT_S = 4.0


@dataclass
class Dimensiescore:
    """Eén van de 5 onderliggende dimensies, schaal 1-9."""
    key: str       # 'won' / 'fys' / 'vrz' / 'soc' / 'onv'
    label: str     # menselijke naam
    score: int     # 1-9
    beschrijving: str  # wat valt er in deze dimensie


@dataclass
class Ontwikkeling:
    """Leefbaarheids-trend over een periode (bv. 2014→2024).

    Klasse-schaal 1-9: 1=sterk verslechterd, 5=geen verandering, 9=sterk verbeterd.
    Raw continuous 'score' is de afwijking; negatief = achteruit, positief = vooruit.
    """
    periode: str                 # "2014-2024"
    score: int                   # 1-9 totaal
    label: str                   # "verbeterd" / "stabiel" / "verslechterd"
    raw_delta: float             # continuous afwijking (positief=vooruit)
    per_dimensie: dict           # {key: klasse} voor won/fys/vrz/soc/onv


@dataclass
class LeefbaarheidScore:
    """Samengestelde leefbaarheidsscore op adres-niveau.

    We bewaren BEIDE granulariteiten zodat de UI transparant kan zijn:
      - score          : grid-cel (100m direct rondom adres, meest specifiek)
      - buurt_score    : hele CBS-buurt geaggregeerd (gemiddeld)

    De buurt-score is meestal wat een makelaar of Funda toont; de grid-score
    is preciezer maar kan afwijken als je aan de rand van een 'betere' plek
    binnen een minder sterke buurt woont.
    """

    score: int  # 1-9 grid (direct rondom adres, 100m)
    label: str
    vs_nl_gem: str
    betekenis: str
    dimensies: list[Dimensiescore] = field(default_factory=list)
    # Optioneel: hele buurt ter vergelijking
    buurt_score: Optional[int] = None
    buurt_label: Optional[str] = None
    buurt_naam: Optional[str] = None  # bv. "Oranjebuurt"
    # Trends over tijd — key = periode-string, waarde = Ontwikkeling
    ontwikkeling_recent: Optional[Ontwikkeling] = None  # 2 jaar
    ontwikkeling_lang: Optional[Ontwikkeling] = None    # 10 jaar


# Schaal 1-9 conform officiele Leefbaarometer-categorieën
SCHAAL = {
    1: ("zeer onvoldoende",       "warn",    "Grote achterstandsproblematiek: criminaliteit, leegstand, lage sociale samenhang."),
    2: ("ruim onvoldoende",       "warn",    "Meerdere leefbaarheidsproblemen; aandachtsgebied in het gemeentelijk beleid."),
    3: ("onvoldoende",            "warn",    "Onder het NL-gemiddelde; typische sociaal-economisch zwakkere wijk."),
    4: ("zwak",                   "neutral", "Licht onder gemiddeld; hoor je 'wijk-in-ontwikkeling' spreken."),
    5: ("voldoende",              "neutral", "Precies op het Nederlandse gemiddelde."),
    6: ("ruim voldoende",         "neutral", "Iets boven gemiddeld; prettige, stabiele wijk."),
    7: ("goed",                   "good",    "Duidelijk bovengemiddelde leefbaarheid."),
    8: ("zeer goed",              "good",    "Sterke samenhang van woningen, voorzieningen, sociale mix."),
    9: ("uitstekend",             "good",    "Top-percentiel van Nederland; gewilde woonomgeving."),
}


DIMENSIES = [
    ("won", "Woningen",            "Bouwjaar-mix, koop/huur-verhouding, leegstand, staat woningvoorraad."),
    ("fys", "Fysieke omgeving",    "Geluid, luchtkwaliteit, bebouwingsdichtheid, groen/grijs verhouding."),
    ("vrz", "Voorzieningen",       "Nabijheid winkels, horeca, zorg, OV, recreatie."),
    ("soc", "Sociale samenhang",   "Demografische mix, huishoudens, inkomensverdeling, kwetsbare groepen."),
    ("onv", "Overlast & veiligheid", "Criminaliteit, objectieve overlast, veiligheidsgevoel."),
]


import asyncio


async def fetch_leefbaarheid(rd_x: float, rd_y: float) -> Optional[LeefbaarheidScore]:
    """Haal grid-score (100m) + buurt-score + trends parallel op.

    Vier WMS GetFeatureInfo-calls tegelijk; totale latency ≈ max van alle
    (~200-400ms) in plaats van sum. De trends (2-jaar + 10-jaar) zijn
    optioneel — als ze falen, valt de app terug op alleen de huidige score.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        grid_task = _fetch_layer(client, LAYER_GRID, rd_x, rd_y)
        buurt_task = _fetch_layer(client, LAYER_BUURT, rd_x, rd_y)
        dev_recent_task = _fetch_layer(client, LAYER_DEV_RECENT, rd_x, rd_y)
        dev_lang_task = _fetch_layer(client, LAYER_DEV_LANG, rd_x, rd_y)
        grid_props, buurt_props, dev_recent_props, dev_lang_props = await asyncio.gather(
            grid_task, buurt_task, dev_recent_task, dev_lang_task
        )

    if not grid_props:
        return None
    score = _to_score(grid_props.get("kscore"))
    if score is None:
        return None

    label, _level, betekenis = SCHAAL.get(score, ("onbekend", "neutral", ""))
    if score < 5:
        vs_nl = "onder"
    elif score == 5:
        vs_nl = "rond"
    else:
        vs_nl = "boven"

    # Sub-dimensies uit de grid-cel (preciest)
    dims: list[Dimensiescore] = []
    for key, naam, beschr in DIMENSIES:
        s = _to_score(grid_props.get(f"k{key}"))
        if s is not None:
            dims.append(Dimensiescore(key=key, label=naam, score=s, beschrijving=beschr))

    # Buurt-score + buurtnaam ter vergelijking. De buurtscore-layer levert
    # 'name' = buurtnaam (bv. 'Oranjebuurt'), 'id' = CBS-buurtcode.
    buurt_score = None
    buurt_label = None
    buurt_naam = None
    if buurt_props:
        buurt_score = _to_score(buurt_props.get("kscore"))
        if buurt_score is not None:
            buurt_label = SCHAAL.get(buurt_score, ("onbekend",))[0]
        bn = buurt_props.get("name") or buurt_props.get("buurtnaam")
        if isinstance(bn, str) and bn.strip():
            buurt_naam = bn.strip()

    # Ontwikkeling (optioneel — faalt silently als WMS-layer niet beschikbaar)
    ontw_recent = _parse_ontwikkeling(dev_recent_props, "2022-2024")
    ontw_lang = _parse_ontwikkeling(dev_lang_props, "2014-2024")

    return LeefbaarheidScore(
        score=score,
        label=label,
        vs_nl_gem=vs_nl,
        betekenis=betekenis,
        dimensies=dims,
        buurt_score=buurt_score,
        buurt_label=buurt_label,
        buurt_naam=buurt_naam,
        ontwikkeling_recent=ontw_recent,
        ontwikkeling_lang=ontw_lang,
    )


def _parse_ontwikkeling(props: Optional[dict], periode: str) -> Optional[Ontwikkeling]:
    """Bouw een Ontwikkeling uit een ontwikkelings-grid properties-dict.

    De ontwikkelings-layer gebruikt dezelfde 1-9 klasse als de score-layer,
    maar hier betekent:
      1 = sterk verslechterd
      5 = geen verandering
      9 = sterk verbeterd
    Raw waarde 'score' is de continue afwijking (positief = vooruit).
    """
    if not props:
        return None
    klasse = _to_score(props.get("kscore"))
    if klasse is None:
        return None
    # Continue waarde voor nuance (bv. +0.12 = lichte verbetering)
    try:
        raw = float(props.get("score")) if props.get("score") is not None else 0.0
    except (TypeError, ValueError):
        raw = 0.0
    # Label-logica op basis van de 1-9 klasse
    if klasse <= 3:
        lab = "verslechterd"
    elif klasse >= 7:
        lab = "verbeterd"
    else:
        lab = "stabiel"
    # Per-dimensie ontwikkeling (zelfde keys als de score-layer)
    per_dim: dict = {}
    for key, _naam, _beschr in DIMENSIES:
        k = _to_score(props.get(f"k{key}"))
        if k is not None:
            per_dim[key] = k
    return Ontwikkeling(
        periode=periode,
        score=klasse,
        label=lab,
        raw_delta=raw,
        per_dimensie=per_dim,
    )


async def _fetch_layer(
    client: httpx.AsyncClient, layer: str, rd_x: float, rd_y: float
) -> Optional[dict]:
    """WMS GetFeatureInfo voor één layer; returnt properties-dict."""
    half = 25.0
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetFeatureInfo",
        "layers": layer,
        "query_layers": layer,
        "bbox": f"{rd_x - half},{rd_y - half},{rd_x + half},{rd_y + half}",
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
    return feats[0].get("properties", {}) if feats else None


def _to_score(raw) -> Optional[int]:
    if raw is None:
        return None
    try:
        s = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return s if 1 <= s <= 9 else None
