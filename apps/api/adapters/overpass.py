"""
OpenStreetMap POI-adapter via Overpass API.

Input  : WGS84 coordinaat (lat/lon) + radius in meters
Output : lijst POI's per categorie, met NAAM + exacte afstand in meters

Waarom deze bestaat naast cbs.fetch_voorzieningen:
  CBS 84718NED publiceert nabijheids-afstanden als buurt-, wijk- of
  gemeentegemiddelde. Voor Damrak 1 betekent dat bv. 'overstapstation =
  4 km' (gemiddelde van heel Amsterdam), terwijl Amsterdam Centraal
  letterlijk 85 m verderop staat. Onbruikbaar als naburigheids-signaal.

OSM biedt:
  - POI-punten met naam ("Amsterdam Centraal", "AH Beursstraat")
  - Exacte coordinaten → haversine-afstand in meters
  - Eén Overpass-call levert alle categorieën tegelijk (~200-500ms)
  - Gratis, geen API-key

Tradeoffs:
  - OSM heeft gaten in privé-POI's (huisartsen, kinderdagverblijven minder
    compleet dan winkels/stations). Voor die categorieën vallen we in de
    orchestrator terug op CBS-gemeente-gemiddelde.
  - Rate-limit op publieke Overpass instance. Aggressieve cache (coord-100m,
    TTL 7d) is voldoende voor normale traffic.
"""
from __future__ import annotations

import asyncio
import math
import sys
from dataclasses import dataclass
from typing import Optional

import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Fallback-endpoints: bij rate-limit / timeout van hoofdserver proberen we
# deze. Zelfde Overpass-QL specificatie, andere servers (meer capaciteit
# wereldwijd). Volgorde = prio. Gemixt: hoofdserver → EU-mirrors →
# wereldwijd → community.
OVERPASS_FALLBACKS = [
    "https://overpass-api.de/api/interpreter",          # hoofdserver (DE)
    "https://overpass.kumi.systems/api/interpreter",    # EU-mirror (DE)
    "https://overpass.osm.ch/api/interpreter",          # CH-mirror (Zwitserland)
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",  # RU-mirror
    "https://overpass.private.coffee/api/interpreter",  # community (DE)
]
TIMEOUT_S = 18.0  # per endpoint; totaal max 5×18s = 90s worst case.
# Verhoogd van 12s naar 18s na observatie dat bij cold-start (Fly machine
# net wakker, OSM Overpass-server net warmgelopen) 12s soms te kort is →
# fallback naar CBS-buurtgemiddelden zonder POI-namen, wat het rapport
# vervolgens als "Treinstation 2,5 km" zonder naam toont. 18s is comfort.

# Exponential backoff bij rate-limit (429/503/506) op ZELFDE endpoint.
# Strategie: 1× retry op zelfde URL met groeiende wait; pas bij blijvend
# rate-limit door naar volgende endpoint. Voorkomt dat we 5 servers
# tegelijk hameren bij een algemene Overpass-piek.
RATE_LIMIT_BACKOFF_S = (2, 6)  # 2s, 6s — totaal max 8s wachten per endpoint

# Cache: per (lat-grid, lon-grid) op 100m precisie, TTL 7 dagen.
# POI's muteren nauwelijks (winkel sluit, kerk verhuist niet). Cache hit
# bespaart een Overpass-call EN beschermt tegen rate-limit-issues.
# In-memory dict — overleeft niet deploys, maar dat is OK want bij deploy
# is alles toch wakker te krijgen via natuurlijk verkeer.
_POI_CACHE: dict[str, tuple[float, list]] = {}   # key → (timestamp, POI-lijst)
_POI_CACHE_TTL_S = 7 * 24 * 3600
_GRID_M = 100  # 100m precisie = ~0.001° = lat afronden op 3 decimalen

# POI-definities: wat we zoeken in OSM + hoe we het in de UI tonen.
# Elk tuple:
#   (internal_key, label, categorie, emoji, radius_m, osm_filter)
#   osm_filter: Overpass-QL selector die ACHTER 'node' / 'way' komt, bv.
#               '["amenity"="supermarket"]'
#   Bij radius kiezen we pragmatisch:
#     dagelijkse dingen (supermarkt, huisarts)  ~ 1000 m
#     publieke voorzieningen (school, park)     ~ 1500 m
#     transport / stations / uitgaan            ~ 2500-3000 m
POI_SPECS = [
    # Boodschappen
    ("supermarkt",             "Supermarkt",             "boodschappen",  "🛒", 1000, '["shop"="supermarket"]'),
    ("dagelijkse_levensmiddelen", "Buurtsuper / dagwinkel", "boodschappen", "🏪", 800,  '["shop"="convenience"]'),
    ("bakker",                 "Bakker",                 "boodschappen",  "🥐", 800,  '["shop"="bakery"]'),
    # Zorg
    ("huisarts",               "Huisarts",               "zorg",          "🏥", 1500, '["amenity"="doctors"]'),
    ("apotheek",               "Apotheek",               "zorg",          "💊", 1500, '["amenity"="pharmacy"]'),
    ("tandarts",               "Tandarts",               "zorg",          "🦷", 1500, '["amenity"="dentist"]'),
    ("ziekenhuis",             "Ziekenhuis",             "zorg",          "🏥", 5000, '["amenity"="hospital"]'),
    # Kinderen / onderwijs
    ("basisschool",            "Basisschool",            "kinderen",      "🏫", 1500, '["amenity"="school"]'),
    ("kinderdagverblijf",      "Kinderdagverblijf",      "kinderen",      "👶", 1500, '["amenity"="kindergarten"]'),
    ("speeltuin",              "Speeltuin",              "kinderen",      "🛝", 800,  '["leisure"="playground"]'),
    # Entertainment / horeca
    ("restaurant",             "Restaurant",             "entertainment", "🍴", 1000, '["amenity"="restaurant"]'),
    ("cafe",                   "Café",                   "entertainment", "☕", 1000, '["amenity"="cafe"]'),
    ("bar_pub",                "Bar / Pub",              "entertainment", "🍺", 1000, '["amenity"~"^(bar|pub)$"]'),
    ("cafetaria",              "Cafetaria / snackbar",   "entertainment", "🍟", 1000, '["amenity"="fast_food"]'),
    ("hotel",                  "Hotel",                  "entertainment", "🏨", 2500, '["tourism"="hotel"]'),
    # Groen / sport
    ("park",                   "Park",                   "sport",         "🌳", 1500, '["leisure"="park"]'),
    ("bos",                    "Bos",                    "sport",         "🌲", 5000, '["landuse"="forest"]'),
    ("sportcentrum",           "Sportcentrum",           "sport",         "⚽", 2500, '["leisure"="sports_centre"]'),
    ("zwembad",                "Zwembad",                "sport",         "🏊", 5000, '["leisure"="swimming_pool"]'),
    ("fitness",                "Fitnesscentrum",         "sport",         "💪", 2000, '["leisure"="fitness_centre"]'),
    # Transport — we queriën ALLE railway=station en splitsen in code op station=subway/light_rail
    ("treinstation",           "Treinstation",           "transport",     "🚆", 5000, '["railway"="station"]'),
    ("tramhalte",              "Tramhalte",              "transport",     "🚋", 800,  '["railway"="tram_stop"]'),
    ("bushalte",               "Bushalte",               "transport",     "🚌", 500,  '["highway"="bus_stop"]'),
    ("oprit_snelweg",          "Oprit snelweg",          "transport",     "🛣️", 5000, '["highway"="motorway_junction"]'),
    # Cultuur
    ("bibliotheek",            "Bibliotheek",            "cultuur",       "📚", 2000, '["amenity"="library"]'),
    ("museum",                 "Museum",                 "cultuur",       "🎨", 3000, '["tourism"="museum"]'),
    ("bioscoop",               "Bioscoop",               "cultuur",       "🎬", 3000, '["amenity"="cinema"]'),
    ("theater",                "Theater",                "cultuur",       "🎭", 3000, '["amenity"="theatre"]'),
]

# POI-types die standaard ook in ways voorkomen (polygonen), niet alleen nodes.
# Voor deze voegen we expliciet 'way' toe aan de query. 'out center' geeft
# dan de centroïde die we kunnen gebruiken voor afstandberekening.
WAY_TYPES = {"park", "bos", "sportcentrum", "zwembad"}


@dataclass
class POI:
    """Eén Point-of-Interest gevonden via Overpass."""
    key: str              # intern type: 'supermarkt', 'treinstation', ...
    label: str            # UI-label: 'Supermarkt', 'Treinstation'
    categorie: str        # filter-groep: 'boodschappen', 'transport', ...
    emoji: str
    naam: Optional[str]   # OSM 'name' tag — None als ongenaamd
    meters: int           # haversine-afstand in meters
    km: float             # meters / 1000, afgerond op 1 decimaal
    lat: float
    lon: float


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Afstand in meters tussen twee WGS84-coördinaten (bol-aarde)."""
    r = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _build_query(lat: float, lon: float) -> str:
    """Bouw één Overpass-QL query voor ALLE POI-types tegelijk.

    Per type één (of twee, bij WAY_TYPES) regels in de union. Overpass
    retourneert dan één JSON met alle gevonden elementen. 'out tags center'
    levert tags + een center-coördinaat (voor ways/relations), wat we nodig
    hebben voor afstandberekening.
    """
    lines: list[str] = []
    for _key, _label, _cat, _emoji, radius, osm_filter in POI_SPECS:
        lines.append(f'  node{osm_filter}(around:{radius},{lat},{lon});')
        if _key in WAY_TYPES:
            lines.append(f'  way{osm_filter}(around:{radius},{lat},{lon});')
            lines.append(f'  relation{osm_filter}(around:{radius},{lat},{lon});')
    body = "\n".join(lines)
    # [out:json] = JSON-respons; timeout in ZIP-query = serverside budget
    return f"[out:json][timeout:10];\n(\n{body}\n);\nout tags center;"


async def _overpass_post_with_retry(query: str) -> Optional[dict]:
    """Robuuste Overpass-POST met endpoint-fallback + per-endpoint backoff.

    Strategie:
      1. Voor elk endpoint: probeer 1×, bij rate-limit (429/503/506) wacht
         RATE_LIMIT_BACKOFF_S[0] sec en probeer 1× opnieuw, dan nog 1×
         na BACKOFF_S[1] sec. Pas dan door naar volgende endpoint.
         → geeft de server adem voor we 'm afschrijven (vermijdt cascade-
         falen waarbij we 5 servers tegelijk hameren).
      2. Bij netwerk-timeout/connect-error: meteen volgend endpoint.
      3. Bij ALLE endpoints fail: None → caller valt terug op CBS.
    """
    headers = {
        "User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com",
        "Accept": "application/json",
    }
    last_error = None
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        for attempt, url in enumerate(OVERPASS_FALLBACKS):
            # Maximaal 1 + len(BACKOFF_S) pogingen op zelfde URL
            backoff_schedule = [0.0] + list(RATE_LIMIT_BACKOFF_S)
            for sub_attempt, wait_s in enumerate(backoff_schedule):
                if wait_s > 0:
                    print(f"[overpass] {url} → rate-limit, wacht {wait_s}s en retry…", file=sys.stderr)
                    await asyncio.sleep(wait_s)
                try:
                    resp = await client.post(url, data={"data": query})
                except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                    last_error = f"{type(e).__name__}:{e}"
                    print(f"[overpass] attempt {attempt+1}.{sub_attempt+1} ({url}): network {last_error}", file=sys.stderr)
                    break  # netwerk-issue → meteen volgend endpoint
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except Exception as e:
                        last_error = f"parse:{e}"
                        print(f"[overpass] attempt {attempt+1}: 200 but parse fail {e}", file=sys.stderr)
                        break  # parse-fail → volgend endpoint
                # 429/503/506 = rate-limit / quota; retry met backoff op zelfde endpoint
                if resp.status_code in (429, 503, 506):
                    last_error = f"status {resp.status_code}"
                    print(f"[overpass] attempt {attempt+1}.{sub_attempt+1} ({url}): {resp.status_code} (rate-limit)", file=sys.stderr)
                    continue  # → wacht in volgende iteratie van backoff_schedule
                # 406/502/504 = niet-rate-limit-error: meteen door naar volgend endpoint
                if resp.status_code in (406, 502, 504):
                    last_error = f"status {resp.status_code}"
                    print(f"[overpass] attempt {attempt+1} ({url}): {resp.status_code} (gateway/server)", file=sys.stderr)
                    break
                # Andere status-codes: log en volgend endpoint
                last_error = f"status {resp.status_code}: {resp.text[:100]}"
                print(f"[overpass] attempt {attempt+1} ({url}): {last_error}", file=sys.stderr)
                break
    print(f"[overpass] ALL {len(OVERPASS_FALLBACKS)} endpoints failed: {last_error}", file=sys.stderr)
    return None


def _grid_key(lat: float, lon: float) -> str:
    """Cache-sleutel: lat/lon afgerond op ~100m grid.

    Zelfde grid-cel = zelfde POI's (POI's verhuizen niet binnen 100m).
    1 graad lat ≈ 111km, dus 100m = 0.0009° → afronden op 3 decimalen.
    """
    return f"{round(lat, 3)},{round(lon, 3)}"


def _cache_get(lat: float, lon: float) -> Optional[list]:
    """In-memory cache lookup, returnt list[POI] of None als verlopen/leeg."""
    import time
    key = _grid_key(lat, lon)
    hit = _POI_CACHE.get(key)
    if not hit:
        return None
    ts, pois = hit
    if time.time() - ts > _POI_CACHE_TTL_S:
        _POI_CACHE.pop(key, None)
        return None
    return pois


def _cache_set(lat: float, lon: float, pois: list) -> None:
    """Schrijf naar cache. Geen size-limit; bij honderden grids = paar MB."""
    import time
    _POI_CACHE[_grid_key(lat, lon)] = (time.time(), pois)


async def fetch_poi_nearby(lat: float, lon: float) -> list[POI]:
    """Haal alle POI-types op in een bounding-box rond (lat, lon).

    Retourneert een lijst POI's, gesorteerd op afstand (dichtbij → ver).
    Per type tonen we NIET alle hits — de orchestrator kiest de dichtstbijzijnde
    per type (anders zie je bv. 20 cafes).

    Met 7-dagen-cache per 100m-grid: tweede scan binnen dezelfde
    grid-cel = instant return zonder Overpass-call. Beschermt tegen
    rate-limit én bespaart latency.
    """
    # Cache hit?
    cached = _cache_get(lat, lon)
    if cached is not None:
        # Re-bereken afstanden met EXACTE coords (cache is per-grid maar
        # adres is een specifiek punt binnen die grid — afstanden kunnen
        # paar tientallen meters verschillen tussen 2 adressen in zelfde cel).
        # POI-coords zijn al opgeslagen, dus haversine opnieuw is goedkoop.
        out: list[POI] = []
        for p in cached:
            new_m = int(_haversine_m(lat, lon, p.lat, p.lon))
            out.append(POI(
                key=p.key, label=p.label, categorie=p.categorie, emoji=p.emoji,
                naam=p.naam, meters=new_m, km=round(new_m / 1000, 1),
                lat=p.lat, lon=p.lon,
            ))
        out.sort(key=lambda x: x.meters)
        print(f"[overpass] cache HIT voor grid {_grid_key(lat, lon)} ({len(out)} POIs)", file=sys.stderr)
        return out

    query = _build_query(lat, lon)
    data = await _overpass_post_with_retry(query)
    if data is None:
        return []

    # Map: (filter → spec) voor matching op terug-gekomen tags.
    # We itereren per element over alle specs en zoeken welke overeenkomt.
    # Niet ideaal qua efficiency maar lijst is klein (~25 specs).
    specs = POI_SPECS

    raw: list[POI] = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        # coord: node heeft lat/lon direct, way/relation hebben center
        if el.get("type") == "node":
            elat, elon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            elat, elon = c.get("lat"), c.get("lon")
        if elat is None or elon is None:
            continue

        # Match op eerste spec die qua tags past. OSM elements kunnen meerdere
        # classificaties hebben (bv. een supermarkt met tourism=hotel — zelden
        # maar mogelijk). We pakken de eerste match.
        matched = None
        for key, label, cat, emoji, _radius, _osm_filter in specs:
            if _tags_match(tags, key):
                matched = (key, label, cat, emoji)
                break
        if matched is None:
            continue

        key, label, cat, emoji = matched
        d_m = _haversine_m(lat, lon, elat, elon)
        naam = tags.get("name")
        raw.append(POI(
            key=key,
            label=label,
            categorie=cat,
            emoji=emoji,
            naam=naam if isinstance(naam, str) and naam.strip() else None,
            meters=int(round(d_m)),
            km=round(d_m / 1000.0, 2),
            lat=elat,
            lon=elon,
        ))

    # Sorteer op afstand + dedupliceer per type: pak dichtstbijzijnde per key,
    # plus evt. 1-2 extra als ze substantieel verder zijn (bv. volgend treinstation).
    raw.sort(key=lambda p: p.meters)
    # Dichtstbijzijnde per type als default-lijst
    seen_keys: set[str] = set()
    out: list[POI] = []
    for p in raw:
        if p.key in seen_keys:
            continue
        seen_keys.add(p.key)
        out.append(p)

    # Schrijf naar cache (7d TTL per 100m grid). Volgende scan in dezelfde
    # cel = instant return zonder Overpass-call. Beschermt rate-limit én
    # versnelt vervolg-scans van dezelfde buurt.
    _cache_set(lat, lon, out)
    print(f"[overpass] cache STORE voor grid {_grid_key(lat, lon)} ({len(out)} POIs)", file=sys.stderr)
    return out


def _tags_match(tags: dict, key: str) -> bool:
    """Check of een OSM-element (met z'n tag-dict) matcht met ons POI-key.

    We kunnen het OSM-filter-string niet direct hergebruiken voor matching
    (dat is Overpass QL); dus duplicatie hier. Houd in sync met POI_SPECS.
    """
    # Eenvoudige 1-key=waarde match voor de meeste types
    simple_map = {
        "supermarkt": ("shop", "supermarket"),
        "dagelijkse_levensmiddelen": ("shop", "convenience"),
        "bakker": ("shop", "bakery"),
        "huisarts": ("amenity", "doctors"),
        "apotheek": ("amenity", "pharmacy"),
        "tandarts": ("amenity", "dentist"),
        "ziekenhuis": ("amenity", "hospital"),
        "basisschool": ("amenity", "school"),
        "kinderdagverblijf": ("amenity", "kindergarten"),
        "speeltuin": ("leisure", "playground"),
        "restaurant": ("amenity", "restaurant"),
        "cafe": ("amenity", "cafe"),
        "cafetaria": ("amenity", "fast_food"),
        "hotel": ("tourism", "hotel"),
        "park": ("leisure", "park"),
        "bos": ("landuse", "forest"),
        "sportcentrum": ("leisure", "sports_centre"),
        "zwembad": ("leisure", "swimming_pool"),
        "fitness": ("leisure", "fitness_centre"),
        "tramhalte": ("railway", "tram_stop"),
        "bushalte": ("highway", "bus_stop"),
        "oprit_snelweg": ("highway", "motorway_junction"),
        "bibliotheek": ("amenity", "library"),
        "museum": ("tourism", "museum"),
        "bioscoop": ("amenity", "cinema"),
        "theater": ("amenity", "theatre"),
    }
    if key in simple_map:
        k, v = simple_map[key]
        return tags.get(k) == v
    if key == "bar_pub":
        return tags.get("amenity") in ("bar", "pub")
    if key == "treinstation":
        # alle railway=station EXCLUSIEF metro/sneltram (die vaak ook
        # railway=station + station=subway/light_rail hebben)
        if tags.get("railway") != "station":
            return False
        sub = tags.get("station")
        return sub not in ("subway", "light_rail", "monorail")
    return False
