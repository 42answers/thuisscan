"""
Bereikbaarheid-adapter — OV + auto-ontsluiting.

Input  : WGS84 coordinaat
Output : dichtstbijzijnde OV-halten per modaliteit + aantal lijnen + afstand
         dichtstbijzijnde oprit snelweg

We combineren 2 Overpass-bewerkingen in één call:
  1. Halten binnen radius per modaliteit (trein / metro / tram / bus)
  2. Route-relations die deze halten bevatten → telling aantal lijnen

Kernidee: *afstand tot halte* zegt weinig zonder context. Een halte op
500 m met 1 buurtbus per uur is slechter dan een halte op 800 m met
metro + tram + 8 buslijnen. Door het aantal lijnen per halte te tonen
geven we die nuance.

Geen echte reistijd-berekening — dat vereist GTFS + OpenTripPlanner
self-hosted, wat buiten scope is. Wel: afstand naar dichtstbijzijnde
snelwegoprit voor auto-ontsluiting.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TIMEOUT_S = 12.0

# Zoekradii per modaliteit: pragmatische balans tussen 'bereikbaar zonder fiets'
# en 'relevant nog beschouwd als ontsluiting'.
#   Bushalte      — 500 m  (loopafstand)
#   Tramhalte     — 800 m  (iets verder prima)
#   Metro-ingang  — 800 m  (idem tram)
#   Treinstation  — 3000 m (fiets-afstand, cruciaal punt)
#   Oprit snelweg — 5000 m (auto-afstand)
RADIUS_BUS = 500
RADIUS_TRAM = 800
RADIUS_METRO = 800
RADIUS_TREIN = 3000
RADIUS_SNELWEG = 5000


@dataclass
class Halte:
    """Een OV-halte met context over hoeveel lijnen erlangs komen."""
    naam: Optional[str]
    type: str                    # 'trein' / 'metro' / 'tram' / 'bus'
    meters: int
    lat: float
    lon: float
    # Unieke lijnen die deze halte bedienen, bv. ['1', '2', 'IC']
    lijnen: list[str] = field(default_factory=list)


# Grote werkcentra (intercity-stations) in NL voor hemelsbrede afstandsmeting.
# Deze geven een ruwe 'afstand tot de grote stad' maatstaf. We geven GEEN
# reistijd-claim — dat vereist OpenTripPlanner + GTFS. Hemelsbrede afstand
# is correleert goed met pendelbereikbaarheid voor de grote corridors.
GROTE_WERKCENTRA = [
    ("Amsterdam",  "Amsterdam Centraal",  52.37906, 4.90011),
    ("Rotterdam",  "Rotterdam Centraal",  51.92511, 4.46931),
    ("Den Haag",   "Den Haag Centraal",   52.08062, 4.32453),
    ("Utrecht",    "Utrecht Centraal",    52.08935, 5.11002),
    ("Eindhoven",  "Eindhoven Centraal",  51.44311, 5.48130),
    ("Groningen",  "Groningen",           53.21101, 6.56413),
    ("Zwolle",     "Zwolle",              52.50461, 6.09252),
    ("Arnhem",     "Arnhem Centraal",     51.98486, 5.89898),
    ("Nijmegen",   "Nijmegen",            51.84360, 5.85249),
    ("'s-Hertogenbosch", "'s-Hertogenbosch",  51.69020, 5.29291),
    ("Breda",      "Breda",               51.59535, 4.77983),
    ("Tilburg",    "Tilburg",             51.56051, 5.08356),
    ("Leeuwarden", "Leeuwarden",          53.19604, 5.79273),
    ("Maastricht", "Maastricht",          50.84910, 5.70539),
]


@dataclass
class Werkcentrum:
    """Afstand + OV-reistijd-schatting naar een intercity-station."""
    stad: str
    station: str
    km: float        # hemelsbrede afstand, afgerond
    ov_min: int      # ruwe OV-reistijd in minuten (schatting)


@dataclass
class Bereikbaarheid:
    """Samengestelde bereikbaarheid-score voor een adres."""
    # Dichtstbijzijnde halte per type (of None)
    trein: Optional[Halte] = None
    metro: Optional[Halte] = None
    tram: Optional[Halte] = None
    bus: Optional[Halte] = None
    # Auto — alleen afstand, geen lijnen
    snelweg_oprit_meters: Optional[int] = None
    snelweg_oprit_naam: Optional[str] = None
    # Top-3 dichtstbijzijnde grote werkcentra (hemelsbreed)
    werkcentra: list[Werkcentrum] = field(default_factory=list)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


async def fetch_bereikbaarheid(lat: float, lon: float) -> Bereikbaarheid:
    """Eén Overpass-call haalt haltes + routes op, Python groepeert het.

    Query-structuur:
      1. Haltes per type (node-query met `around`)
      2. Route-relations die deze haltes bevatten (`rel(bn.halten)`)

    Het output-format 'out body' geeft zowel de nodes (met coord + name)
    als de relations (met tags + member-list waarmee we nodes → lijnen
    kunnen koppelen).
    """
    query = _build_query(lat, lon)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return Bereikbaarheid()

    elements = data.get("elements", [])
    # Split: nodes vs relations
    nodes = {e["id"]: e for e in elements if e.get("type") == "node"}
    relations = [e for e in elements if e.get("type") == "relation"]

    # node_id → list of line-references (ref tag in route-relation)
    node_to_lines: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for rel in relations:
        tags = rel.get("tags") or {}
        route_type = tags.get("route")
        if route_type not in ("bus", "tram", "subway", "light_rail", "train"):
            continue
        ref = tags.get("ref") or tags.get("name") or "?"
        for m in rel.get("members") or []:
            if m.get("type") == "node":
                node_to_lines[m["ref"]].append((route_type, ref))

    # Nu per categorie de dichtstbijzijnde halte kiezen
    result = Bereikbaarheid()

    def _best_halte(
        cat: str,
        node_filter,
        radius: int,
    ) -> Optional[Halte]:
        candidates: list[Halte] = []
        for n_id, n in nodes.items():
            tags = n.get("tags") or {}
            if not node_filter(tags):
                continue
            nlat = n.get("lat")
            nlon = n.get("lon")
            if nlat is None or nlon is None:
                continue
            d = _haversine_m(lat, lon, nlat, nlon)
            if d > radius:
                continue
            # Unique lines through this node (per category)
            lines_for_type = sorted({
                ref for (rt, ref) in node_to_lines.get(n_id, [])
                if _line_matches_cat(rt, cat)
            })
            candidates.append(Halte(
                naam=tags.get("name"),
                type=cat,
                meters=int(round(d)),
                lat=nlat,
                lon=nlon,
                lijnen=lines_for_type,
            ))
        if not candidates:
            return None
        candidates.sort(key=lambda h: h.meters)
        return candidates[0]

    # Trein-filter: pas op voor OSM-tag-verwarring. 'railway=stop' wordt
    # in OSM óók op tram-stops gebruikt; we moeten dus expliciet train=yes
    # hebben, of een railway=station zonder tram/subway/light_rail station-tag.
    def _is_trein(t: dict) -> bool:
        if t.get("railway") == "station":
            return t.get("station") not in ("subway", "light_rail", "tram")
        # Bij stop-positions of railway=stop: alleen als expliciet train=yes
        # EN niet gemarkeerd als tram.
        if t.get("public_transport") == "stop_position" or t.get("railway") == "stop":
            return t.get("train") == "yes" and t.get("tram") != "yes"
        return False

    result.trein = _best_halte("trein", _is_trein, RADIUS_TREIN)
    result.metro = _best_halte(
        "metro",
        lambda t: (
            t.get("railway") == "station"
            and t.get("station") in ("subway", "light_rail")
        )
        or t.get("railway") == "subway_entrance"
        or (
            t.get("public_transport") == "stop_position"
            and (t.get("subway") == "yes" or t.get("light_rail") == "yes")
        ),
        RADIUS_METRO,
    )
    result.tram = _best_halte(
        "tram",
        lambda t: t.get("railway") == "tram_stop"
        or (t.get("public_transport") == "stop_position" and t.get("tram") == "yes"),
        RADIUS_TRAM,
    )
    result.bus = _best_halte(
        "bus",
        lambda t: t.get("highway") == "bus_stop"
        or (t.get("public_transport") == "stop_position" and t.get("bus") == "yes"),
        RADIUS_BUS,
    )

    # Oprit snelweg — apart, zonder lijnen-info
    snelweg_candidates: list[tuple[int, Optional[str], float, float]] = []
    for n in nodes.values():
        tags = n.get("tags") or {}
        if tags.get("highway") != "motorway_junction":
            continue
        nlat, nlon = n.get("lat"), n.get("lon")
        if nlat is None or nlon is None:
            continue
        d = _haversine_m(lat, lon, nlat, nlon)
        if d > RADIUS_SNELWEG:
            continue
        snelweg_candidates.append((int(round(d)), tags.get("name") or tags.get("ref"), nlat, nlon))
    if snelweg_candidates:
        snelweg_candidates.sort(key=lambda x: x[0])
        d, nm, _, _ = snelweg_candidates[0]
        result.snelweg_oprit_meters = d
        result.snelweg_oprit_naam = nm

    # Top-3 werkcentra op hemelsbrede afstand (lokaal, geen extra API-call).
    # Plus een RUWE OV-reistijdschatting op basis van NL-kenmerken:
    #   - Lokaal OV (tram/bus, <5 km): ~18 km/h gem = 3.3 min/km
    #   - Stedelijk + overstap (5-20 km): ~22 km/h gem = 2.7 min/km
    #   - Intercity (>20 km): ~55 km/h gem = 1.1 min/km, +10 min voor
    #     overstap + lopen naar/van station
    # Geen echte routing — voor exacte reistijd verwijzen we naar 9292.
    wc_ranked = sorted(
        (
            (stad, station, _haversine_m(lat, lon, clat, clon) / 1000.0)
            for stad, station, clat, clon in GROTE_WERKCENTRA
        ),
        key=lambda x: x[2],
    )
    result.werkcentra = [
        Werkcentrum(
            stad=s,
            station=st,
            km=round(km, 1),
            ov_min=_schat_ov_min(km),
        )
        for s, st, km in wc_ranked[:3]
    ]

    return result


def _schat_ov_min(km: float) -> int:
    """Ruwe schatting van reistijd met OV in minuten.

    Gebaseerd op gemiddelde-snelheid-heuristiek per afstandsklasse:
      <5 km   : lokaal OV (18 km/h), geen overstap
      5-20 km : stedelijk + overstap (22 km/h) + 5 min overstap-opslag
      >20 km  : intercity (55 km/h) + 10 min heen-&-weer naar station

    NL-realiteit: Amsterdam Zuid → Utrecht CS (35 km) ≈ 28 min IC + 15 min
    lopen/fietsen = 43 min totaal. Onze schatting: 35 / 55 * 60 + 10 = 48 min.
    Realistisch binnen ±15%.
    """
    if km < 5:
        return round(km * 3.3)
    if km < 20:
        return round(km * 2.7 + 5)
    # >20 km: intercity-route
    return round(km / 55 * 60 + 10)


def _line_matches_cat(route_type: str, cat: str) -> bool:
    """Match een OSM route-type met onze categorie."""
    if cat == "trein":
        return route_type == "train"
    if cat == "metro":
        return route_type in ("subway", "light_rail")
    if cat == "tram":
        return route_type == "tram"
    if cat == "bus":
        return route_type == "bus"
    return False


def _build_query(lat: float, lon: float) -> str:
    """Bouw Overpass-QL query — 1 roundtrip voor haltes + relations.

    We nemen zowel de 'anchor' nodes (bv. railway=station voor de naam)
    als de 'stop_position' nodes (die in OSM route-relations zitten met
    tags als bus=yes/tram=yes/train=yes). Die stop_positions zijn vaak
    verschillend van de station-node zelf. Zonder dat mis je de lijnen.
    """
    return f"""
[out:json][timeout:15];
(
  // Anchor-nodes (voor halte-naam + afstand)
  node["railway"="station"](around:{RADIUS_TREIN},{lat},{lon});
  node["railway"="tram_stop"](around:{RADIUS_TRAM},{lat},{lon});
  node["railway"="subway_entrance"](around:{RADIUS_METRO},{lat},{lon});
  node["highway"="bus_stop"](around:{RADIUS_BUS},{lat},{lon});
  // Stop-positions (zitten in route-relations)
  node["public_transport"="stop_position"]["train"="yes"](around:{RADIUS_TREIN},{lat},{lon});
  node["public_transport"="stop_position"]["subway"="yes"](around:{RADIUS_METRO},{lat},{lon});
  node["public_transport"="stop_position"]["light_rail"="yes"](around:{RADIUS_METRO},{lat},{lon});
  node["public_transport"="stop_position"]["tram"="yes"](around:{RADIUS_TRAM},{lat},{lon});
  node["public_transport"="stop_position"]["bus"="yes"](around:{RADIUS_BUS},{lat},{lon});
  // Railway stop (oudere tagging)
  node["railway"="stop"](around:{RADIUS_TREIN},{lat},{lon});
  // Oprit snelweg
  node["highway"="motorway_junction"](around:{RADIUS_SNELWEG},{lat},{lon});
)->.halten;
(
  .halten;
  rel(bn.halten)["type"="route"]["route"~"^(bus|tram|subway|light_rail|train)$"];
);
out body;
""".strip()
