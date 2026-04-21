"""
Orchestrator — combineert alle adapters tot één Thuisscan-response.

Flow voor GET /scan?q=<adres>:
  1. PDOK Locatieserver           -> BAG-id + buurtcode + coordinaten
  2. PARALLEL:
       - BAG WFS                  -> bouwjaar, oppervlakte, gebruiksdoel
       - CBS OData                -> demografie, inkomen, WOZ, voorzieningen
  3. Samenvoegen tot 'ScanResult' dat de frontend 1-op-1 kan renderen.

Cache-strategie (MVP = in-memory LRU):
  - key = buurtcode, ttl = 24u   (CBS-data verandert jaarlijks)
  - key = bag_vbo_id, ttl = 7d   (BAG muteert dagelijks, maar per VBO zelden)
  - Redis komt in v2 — in-memory is genoeg voor 1 instance / <10k daily users.

De adapter-modules blijven bewust state-less; alle cache + orchestratie zit hier.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Optional

import references
import social_questions
from adapters import bag, cbs, kadaster_woz, klimaat, leefbaarometer, onderwijs, overpass, pdok_locatie, politie, rivm_geluid, rivm_lki, rvo_ep, verkiezingen


def _as_ref(r) -> Optional[dict]:
    """Serialize Reference dataclass naar dict voor JSON-response.

    Geeft None terug als de adapter geen waarde had (dan hoeft de UI geen
    lege referentie-regel te tonen).
    """
    if r is None:
        return None
    return {
        "chip_level": r.chip_level,
        "chip_text": r.chip_text,
        "nl_gemiddelde": r.nl_gemiddelde,
        "norm": r.norm,
        "betekenis": r.betekenis,
    }

# Simpele TTL-cache. Voor MVP meer dan genoeg; Redis later.
_cache: dict[str, tuple[float, object]] = {}
_BUURT_TTL_S = 24 * 3600  # CBS-data jaarcohort, 24u cache is ruim
_BAG_TTL_S = 7 * 24 * 3600  # BAG muteert per pand zelden
_POLITIE_TTL_S = 12 * 3600  # Politie publiceert maandelijks — 12u is strak genoeg
_RIVM_TTL_S = 30 * 24 * 3600  # Luchtkwaliteit-jaargemiddelden wijzigen jaarlijks
_KLIMAAT_TTL_S = 30 * 24 * 3600  # Klimaateffectatlas wordt jaarlijks geactualiseerd
_OSM_TTL_S = 7 * 24 * 3600  # POI's verhuizen zelden; 7 dagen is ruim


def _cache_get(key: str, ttl_s: int) -> Optional[object]:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, value = hit
    if time.time() - ts > ttl_s:
        _cache.pop(key, None)  # lazy eviction
        return None
    return value


def _cache_set(key: str, value: object) -> None:
    _cache[key] = (time.time(), value)


@dataclass
class ScanResult:
    """De volledige Thuisscan-response voor één adres."""

    adres: dict
    cover: dict  # Leefbaarometer-score — bovenaan pagina
    woning: dict  # Sectie 1
    wijk_economie: dict  # Sectie 2
    buren: dict  # Sectie 3
    voorzieningen: dict
    veiligheid: dict  # Sectie 4
    leefkwaliteit: dict  # Sectie 5
    klimaat: dict  # Sectie 6
    onderwijs: dict  # Sectie 7 — kinderopvang + scholen + inspectie
    sociale_vragen: list[dict]  # 3 menselijke vragen (post-processing)
    provenance: list[dict]  # bronvermelding per sectie voor UI-tags


async def scan(query: str) -> ScanResult:
    """Main entry point — adres-tekst naar volledige Thuisscan-response."""
    # Stap 1: geocoding (niet cached per query — Locatieserver is al <100ms)
    match = await pdok_locatie.geocode(query)
    if match is None:
        raise ValueError(f"Geen adres gevonden voor: {query!r}")

    # Stap 2a: parallel BAG + CBS
    # BAG + CBS zijn los van elkaar; we doen ze tegelijk.
    bag_task = _cached_fetch_bag(match.bag_verblijfsobject_id or "")
    cbs_task = _cached_fetch_cbs(
        match.buurtcode or "",
        wijkcode=match.wijkcode,
        gemeentecode=match.gemeentecode,
    )
    pand, buurt = await asyncio.gather(bag_task, cbs_task)

    # Stap 2b: Politie + WOZ-trend + migratieachtergrond parallel.
    # Voorzieningen worden NIET hier opgehaald — OSM via Overpass is de
    # traagste externe dep (~3-6s cold). In plaats daarvan biedt de API
    # een aparte /voorzieningen endpoint die de frontend na render aanroept.
    inwoners = buurt.inwoners if buurt else None
    politie_task = _cached_fetch_politie(match.buurtcode or "", inwoners)
    woz_trend_task = _cached_fetch_woz_trend(match.buurtcode or "")
    migratie_task = _cached_fetch_migratie(
        match.buurtcode or "", match.wijkcode, match.gemeentecode
    )
    misdrijven, woz_trend, migratie = await asyncio.gather(
        politie_task, woz_trend_task, migratie_task
    )

    # Stap 2c: RVO EP-Online + TK2025-uitslag (beide synchroon, geen I/O)
    # + Kadaster WOZ (async — vereist API-key, retourneert None zonder).
    energielabel = rvo_ep.fetch_label(
        match.postcode or "", match.huisnummer or ""
    )
    tk_uitslag = verkiezingen.fetch_top3(match.gemeentecode or "")
    woz_adres = await _cached_fetch_woz_adres(
        match.bag_verblijfsobject_id or "",
        match.postcode or "",
        match.huisnummer or "",
    )

    # Stap 2d: RIVM luchtkwaliteit + Klimaateffectatlas + Leefbaarometer
    # (allemaal punt-queries op externe services; parallel).
    lucht_task = _cached_fetch_lucht(match.rd_x, match.rd_y)
    klimaat_task = _cached_fetch_klimaat(
        match.lat, match.lon, match.rd_x, match.rd_y
    )
    leef_task = _cached_fetch_leefbaarheid(match.rd_x, match.rd_y)
    geluid_task = _cached_fetch_geluid(match.rd_x, match.rd_y)
    lucht, klimaatrisico, leefbaarheid, geluid = await asyncio.gather(
        lucht_task, klimaat_task, leef_task, geluid_task
    )

    # Buurtnaam uit Leefbaarometer: handig voor het adres-kopje
    buurt_naam = leefbaarheid.buurt_naam if leefbaarheid else None

    # Stap 3: samenstellen — volgorde = volgorde in UI
    return ScanResult(
        cover=_build_cover(leefbaarheid),
        adres={
            "display_name": match.display_name,
            "postcode": match.postcode,
            "huisnummer": match.huisnummer,
            "buurtcode": match.buurtcode,
            "buurt_naam": buurt_naam,
            "wijkcode": match.wijkcode,
            "gemeentecode": match.gemeentecode,
            "wgs84": {"lat": match.lat, "lon": match.lon},
            "rd": {"x": match.rd_x, "y": match.rd_y},
        },
        woning=_build_woning(pand, energielabel, woz_adres),
        wijk_economie=_build_wijk_economie(buurt, woz_trend),

        buren=_build_buren(buurt, tk_uitslag, migratie),
        # Voorzieningen worden apart geladen via /voorzieningen endpoint
        # (Overpass-call duurt 3-6s cold; frontend haalt deze async op nadat
        # de hoofdpagina is gerenderd). Hier een 'pending' placeholder zodat
        # de frontend weet dat het nog komt.
        voorzieningen={"available": False, "pending": True},
        veiligheid=_build_veiligheid(misdrijven),
        leefkwaliteit=_build_leefkwaliteit(lucht, geluid),
        klimaat=_build_klimaat(klimaatrisico),
        onderwijs=_build_onderwijs(match.lat, match.lon),
        sociale_vragen=[],  # gevuld in result_as_dict na serialisatie
        provenance=_provenance(match.buurtcode or ""),
    )


# --- Cached fetch wrappers -------------------------------------------------

async def _cached_fetch_bag(vbo_id: str) -> Optional[bag.PandDetails]:
    if not vbo_id:
        return None
    key = f"bag:{vbo_id}"
    hit = _cache_get(key, _BAG_TTL_S)
    if isinstance(hit, bag.PandDetails):
        return hit
    result = await bag.fetch_pand(vbo_id)
    _cache_set(key, result)
    return result


async def _cached_fetch_cbs(
    buurtcode: str,
    wijkcode: Optional[str] = None,
    gemeentecode: Optional[str] = None,
) -> Optional[cbs.BuurtStats]:
    """Cached CBS-fetch met hiërarchische fallback buurt → wijk → gemeente."""
    if not buurtcode:
        return None
    # Cache-key bevat de fallback-dimensies zodat we niet door elkaar halen
    key = f"cbs:{buurtcode}:{wijkcode}:{gemeentecode}"
    hit = _cache_get(key, _BUURT_TTL_S)
    if isinstance(hit, cbs.BuurtStats):
        return hit
    result = await cbs.fetch_buurt(buurtcode, wijkcode, gemeentecode)
    _cache_set(key, result)
    return result


async def _cached_fetch_migratie(
    buurtcode: str,
    wijkcode: Optional[str],
    gemeentecode: Optional[str],
) -> Optional[dict]:
    """Migratieachtergrond uit KWB 2020 met hierarchische fallback.

    KWB 2020 is de laatste CBS-dataset met dit veld op buurt-niveau. Voor
    Amsterdam (nieuwe buurtcodering sinds 2021) valt het terug op wijk
    of gemeente. 30-dagen cache — data is dated (2020), verandert niet.
    """
    key = f"migratie:{buurtcode}:{wijkcode}:{gemeentecode}"
    hit = _cache_get(key, 30 * 24 * 3600)
    if isinstance(hit, dict):
        return hit
    try:
        result = await cbs.fetch_migratieachtergrond(
            buurtcode=buurtcode or None,
            wijkcode=wijkcode,
            gemeentecode=gemeentecode,
        )
    except Exception:
        return None
    if result is not None:
        _cache_set(key, result)
    return result


async def _cached_fetch_woz_trend(buurtcode: str) -> list[dict]:
    """WOZ-trend — 2+ jaargangen gesorteerd oplopend."""
    if not buurtcode:
        return []
    key = f"woz_trend:{buurtcode}"
    hit = _cache_get(key, _BUURT_TTL_S)
    if isinstance(hit, list):
        return hit
    try:
        result = await cbs.fetch_woz_trend(buurtcode)
    except Exception:
        return []
    _cache_set(key, result)
    return result


async def _cached_fetch_woz_adres(
    bag_vbo: str, postcode: str, huisnummer: str
) -> Optional[kadaster_woz.WozWaarde]:
    """WOZ per adres via Kadaster bevragingen-API; vereist API-key.

    Cache 30 dagen — WOZ-waarden worden jaarlijks vastgesteld, dus langere
    TTL is veilig. Retourneert None als geen key gezet of geen WOZ voor
    dit adres (bv. bij nieuwbouw nog niet getaxeerd).
    """
    if not (bag_vbo or (postcode and huisnummer)):
        return None
    key = f"wozadres:{bag_vbo}:{postcode}:{huisnummer}"
    hit = _cache_get(key, 30 * 24 * 3600)
    if isinstance(hit, kadaster_woz.WozWaarde):
        return hit
    try:
        # Prefer BAG VBO-id (preciest); fallback op postcode+huisnummer
        if bag_vbo:
            result = await kadaster_woz.fetch_woz_by_bag(bag_vbo)
        else:
            result = await kadaster_woz.fetch_woz_by_adres(postcode, huisnummer)
    except Exception:
        return None
    if result is not None:
        _cache_set(key, result)
    return result


async def fetch_voorzieningen(
    lat: float,
    lon: float,
    buurtcode: str = "",
    gemeentecode: str = "",
) -> dict:
    """Los endpoint: haal OSM + CBS voorzieningen parallel en merge.

    Wordt door /voorzieningen in main.py aangeroepen — niet door scan().
    Dit is de dure call (~3-6s cold, ~100ms warm) die we uit het hoofd-
    scan-pad hebben gehaald zodat de pagina snel toont.
    """
    osm_task = _cached_fetch_overpass(lat, lon)
    cbs_task = _cached_fetch_voorzieningen(buurtcode, gemeentecode)
    osm_pois, cbs_voorz = await asyncio.gather(osm_task, cbs_task)
    merged = _merge_voorzieningen(osm_pois, cbs_voorz)
    return _build_voorzieningen(merged)


async def _cached_fetch_overpass(lat: float, lon: float) -> list[overpass.POI]:
    """OSM POI's rond (lat, lon) via Overpass API, met cache.

    Cache-key gebruikt coord-100m rounding — POI's binnen hetzelfde 100m-grid
    delen dezelfde POI-lijst (kleine afwijkingen in afstand zijn acceptabel).
    """
    if not (lat and lon):
        return []
    # 100m rounding via lat*1000 (≈111m) / lon*1000 (iets korter in NL)
    key = f"osm:{round(lat * 1000)}_{round(lon * 1000)}"
    hit = _cache_get(key, _OSM_TTL_S)
    if isinstance(hit, list):
        return hit
    try:
        result = await overpass.fetch_poi_nearby(lat, lon)
    except Exception:
        return []
    if result:
        _cache_set(key, result)
    return result


def _merge_voorzieningen(
    osm_pois: list[overpass.POI], cbs_voorz: list[dict]
) -> list[dict]:
    """Combineer OSM POI's en CBS-gemeente-gemiddeldes tot één lijst.

    Strategie:
      1. OSM POI's → altijd meenemen (met naam + precieze meters)
      2. CBS-types die NIET in OSM zitten → als fallback (gemeente-gemiddelde)
         met chip die duidelijk maakt dat het een gemiddelde is, niet specifiek.

    Output-formaat gelijk aan het oorspronkelijke fetch_voorzieningen
    (type/km/emoji) + extra velden (naam, meters, source).
    """
    out: list[dict] = []
    osm_types = {p.key for p in osm_pois}
    # CBS-categorieën die we onderdrukken als OSM een equivalent heeft.
    # 'overstapstation' (intercity) is in NL vrijwel synoniem met 'treinstation'
    # voor de meeste mensen — de OSM-entry geeft al naam + precieze afstand.
    # Het CBS-gemeente-gemiddelde zou verwarrend naast de concrete waarde staan.
    # Key = CBS-type, value = set van OSM-types die het vervangen.
    cbs_suppression = {
        "overstapstation": {"treinstation"},
    }
    # 1. OSM POIs → primary lijst
    for p in osm_pois:
        out.append({
            "type": p.key,
            "label": p.label,          # bv 'Supermarkt' (overschrijft CBS-label)
            "categorie": p.categorie,
            "emoji": p.emoji,
            "naam": p.naam,            # bv 'Amsterdam Centraal'
            "meters": p.meters,
            "km": round(p.meters / 1000.0, 2),
            "source": "osm",
        })
    # 2. CBS-categorieën die OSM niet heeft → fallback
    # (huisartsenpost, buitenschoolse opvang, fysiotherapeut, etc.)
    for v in cbs_voorz:
        if v["type"] in osm_types:
            continue  # OSM heeft het al preciezer
        # Ook onderdrukken als een OSM-equivalent al aanwezig is
        equivalents = cbs_suppression.get(v["type"], set())
        if equivalents & osm_types:
            continue
        out.append({
            "type": v["type"],
            "emoji": v.get("emoji", "•"),
            "km": v["km"],
            "meters": int(round(v["km"] * 1000)),
            "naam": None,
            "source": "cbs",
        })
    out.sort(key=lambda x: x.get("meters") or x.get("km", 0) * 1000)
    return out


async def _cached_fetch_voorzieningen(
    buurtcode: str, gemeentecode: str
) -> list[dict]:
    """Uitgebreide voorzieningen-lijst (20+ items) via CBS 84718NED.

    Cache op (buurtcode, gemeentecode) tuple; beide wijzigen zelden.
    """
    key = f"voorz:{buurtcode}:{gemeentecode}"
    hit = _cache_get(key, _BUURT_TTL_S)
    if isinstance(hit, list):
        return hit
    try:
        result = await cbs.fetch_voorzieningen(buurtcode, gemeentecode)
    except Exception:
        return []
    _cache_set(key, result)
    return result


async def _cached_fetch_politie(
    buurtcode: str, inwoners: Optional[int]
) -> Optional[politie.Misdrijven]:
    if not buurtcode:
        return None
    # Cache-key bevat inwonersaantal zodat de per-1000-normering blijft kloppen
    # als CBS een nieuwe jaargang met andere inwonersaantallen publiceert.
    key = f"politie:{buurtcode}:{inwoners or 0}"
    hit = _cache_get(key, _POLITIE_TTL_S)
    if isinstance(hit, politie.Misdrijven):
        return hit
    try:
        result = await politie.fetch_misdrijven(buurtcode, inwoners=inwoners)
    except Exception:
        # Politie OData valt vaker uit dan CBS of PDOK. We failen niet de
        # hele scan; de sectie toont gewoon 'geen data beschikbaar'.
        return None
    _cache_set(key, result)
    return result


# Cache-keys voor RIVM/Klimaat gebruiken de RD-coordinaat afgerond op 25m
# (het RIVM-grid is 25m); dat levert een praktisch "per-buurt" cache op zonder
# een aparte buurtcode-lookup.
def _coord_key(rd_x: float, rd_y: float) -> str:
    return f"{int(rd_x // 25)}_{int(rd_y // 25)}"


async def _cached_fetch_lucht(
    rd_x: float, rd_y: float
) -> Optional[rivm_lki.Luchtkwaliteit]:
    if rd_x == 0 or rd_y == 0:
        return None
    key = f"lucht:{_coord_key(rd_x, rd_y)}"
    hit = _cache_get(key, _RIVM_TTL_S)
    if isinstance(hit, rivm_lki.Luchtkwaliteit):
        return hit
    try:
        result = await rivm_lki.fetch_luchtkwaliteit(rd_x, rd_y)
    except Exception:
        return None
    _cache_set(key, result)
    return result


async def _cached_fetch_geluid(
    rd_x: float, rd_y: float
) -> Optional[rivm_geluid.GeluidOpGevel]:
    if rd_x == 0 or rd_y == 0:
        return None
    key = f"geluid:{_coord_key(rd_x, rd_y)}"
    hit = _cache_get(key, _RIVM_TTL_S)
    if isinstance(hit, rivm_geluid.GeluidOpGevel):
        return hit
    try:
        result = await rivm_geluid.fetch_geluid(rd_x, rd_y)
    except Exception:
        return None
    if result is not None:
        _cache_set(key, result)
    return result


async def _cached_fetch_leefbaarheid(
    rd_x: float, rd_y: float
) -> Optional[leefbaarometer.LeefbaarheidScore]:
    if rd_x == 0 or rd_y == 0:
        return None
    key = f"leefbaar:{_coord_key(rd_x, rd_y)}"
    hit = _cache_get(key, _RIVM_TTL_S)
    if isinstance(hit, leefbaarometer.LeefbaarheidScore):
        return hit
    result = await leefbaarometer.fetch_leefbaarheid(rd_x, rd_y)
    if result is not None:
        _cache_set(key, result)
    return result


async def _cached_fetch_klimaat(
    lat: float, lon: float, rd_x: float, rd_y: float
) -> Optional[klimaat.Klimaatrisico]:
    if not (lat and lon):
        return None
    key = f"klimaat:{_coord_key(rd_x, rd_y)}"
    hit = _cache_get(key, _KLIMAAT_TTL_S)
    if isinstance(hit, klimaat.Klimaatrisico):
        return hit
    try:
        result = await klimaat.fetch_klimaat(lat, lon, rd_x, rd_y)
    except Exception:
        return None
    _cache_set(key, result)
    return result


# --- Section builders ------------------------------------------------------
# Houd deze functies puur (alleen data-transformatie, geen I/O).
# Ze bepalen de uiteindelijke contract met de frontend.

def _build_woning(
    pand: Optional[bag.PandDetails],
    energielabel: Optional[rvo_ep.Energielabel],
    woz_adres: Optional[kadaster_woz.WozWaarde] = None,
) -> dict:
    if pand is None:
        return {"available": False}
    is_woning = "woonfunctie" in pand.gebruiksdoel
    label = energielabel.label_klasse if energielabel else None
    out = {
        "available": True,
        "bouwjaar": {
            "value": pand.bouwjaar,
            "unit": None,
            "ref": _as_ref(references.ref_bouwjaar(pand.bouwjaar)),
        },
        "oppervlakte": {
            "value": pand.oppervlakte_m2,
            "unit": "m²",
            "ref": _as_ref(references.ref_oppervlakte(pand.oppervlakte_m2, is_woning)),
        },
        "gebruiksdoel": pand.gebruiksdoel,
        "is_woning": is_woning,
        "status": pand.status_pand or pand.status_verblijfsobject,
        "energielabel": {
            "value": label,
            "datum": energielabel.registratiedatum if energielabel else None,
            "ref": _as_ref(references.ref_energielabel(label)),
        },
        "bag_pand_id": pand.pand_id,
    }
    # WOZ-per-adres: alleen beschikbaar als Kadaster-key is gezet. Wordt
    # getoond boven het buurt-gemiddelde. Frontend kiest welke dominant is.
    if woz_adres is not None and woz_adres.huidige_waarde_eur:
        out["woz_adres"] = {
            "value": woz_adres.huidige_waarde_eur,
            "unit": "€",
            "peildatum": woz_adres.peildatum,
            "historie": woz_adres.historie,
            "ref": _as_ref(references.ref_woz(woz_adres.huidige_waarde_eur)),
        }
    return out


def _build_wijk_economie(
    buurt: Optional[cbs.BuurtStats], woz_trend: list[dict]
) -> dict:
    if buurt is None:
        return {"available": False}
    woz_eur = (
        int(buurt.woz_gemiddeld_x1000_eur * 1000)
        if buurt.woz_gemiddeld_x1000_eur is not None
        else None
    )
    inkomen_eur = (
        int(buurt.inkomen_per_inwoner_x1000_eur * 1000)
        if buurt.inkomen_per_inwoner_x1000_eur is not None
        else None
    )
    # Bereken jaar-op-jaar groei als er minimaal 2 datapunten zijn
    trend_pct = None
    if len(woz_trend) >= 2:
        oldest = woz_trend[0]["woz_eur"]
        newest = woz_trend[-1]["woz_eur"]
        if oldest > 0:
            span_years = int(woz_trend[-1]["year"]) - int(woz_trend[0]["year"])
            total_change = (newest - oldest) / oldest
            # CAGR: jaarlijkse groei
            if span_years > 0:
                trend_pct = round(((1 + total_change) ** (1 / span_years) - 1) * 100, 1)
            else:
                trend_pct = round(total_change * 100, 1)
    # Opleidingsniveau: bereken % hoogopgeleid (hbo + wo) van het totaal.
    # CBS publiceert absolute aantallen in x1000 inwoners; de som is de volwassen
    # bevolking 15-75 jaar. Percentages zijn intuïtiever voor bewoners.
    opl_hoog_pct = None
    opl_laag_pct = None
    opl_midden_pct = None
    opl_totaal = sum(
        v for v in (buurt.opleiding_laag, buurt.opleiding_midden, buurt.opleiding_hoog)
        if v is not None
    )
    if opl_totaal > 0:
        if buurt.opleiding_hoog is not None:
            opl_hoog_pct = round(100 * buurt.opleiding_hoog / opl_totaal, 1)
        if buurt.opleiding_laag is not None:
            opl_laag_pct = round(100 * buurt.opleiding_laag / opl_totaal, 1)
        if buurt.opleiding_midden is not None:
            opl_midden_pct = round(100 * buurt.opleiding_midden / opl_totaal, 1)

    scope = buurt.scope or {}
    # Opleiding-scope = laagste beschikbare; als alle 3 opleidings-velden
    # van wijk komen, is het percentage ook op wijk-niveau
    opl_scopes = [scope.get(k) for k in ("opleiding_laag", "opleiding_midden", "opleiding_hoog")]
    opl_scope = next((s for s in opl_scopes if s), None)

    # Eigendomsverhouding — scope is laagste niveau waar minstens 1 veld
    # beschikbaar is. Sommatie rond meestal 100% af (rounding tot gehele %).
    eig_scopes = [scope.get(k) for k in ("koop_pct", "sociale_huur_pct", "particuliere_huur_pct")]
    eig_scope = next((s for s in eig_scopes if s), None)

    return {
        "available": True,
        "woz": {
            "value": woz_eur,
            "unit": "€",
            "ref": _as_ref(references.ref_woz(woz_eur)),
            "trend_pct_per_jaar": trend_pct,
            "trend_series": woz_trend,
            "scope": scope.get("woz_gemiddeld"),
        },
        "inkomen_per_inwoner": {
            "value": inkomen_eur,
            "unit": "€",
            "ref": _as_ref(references.ref_inkomen(inkomen_eur)),
            "scope": scope.get("inkomen_per_inwoner"),
        },
        "arbeidsparticipatie": {
            "value": buurt.arbeidsparticipatie_pct,
            "unit": "%",
            "ref": _as_ref(references.ref_arbeidsparticipatie(buurt.arbeidsparticipatie_pct)),
            "scope": scope.get("arbeidsparticipatie"),
        },
        "opleiding_hoog": {
            "value": opl_hoog_pct,
            "unit": "%",
            "ref": _as_ref(references.ref_opleiding_hoog(opl_hoog_pct)),
            "scope": opl_scope,
            "breakdown": {
                "laag_pct": opl_laag_pct,
                "midden_pct": opl_midden_pct,
                "hoog_pct": opl_hoog_pct,
            },
        },
        # Eigendomsverhouding — stacked-bar visualisatie in frontend.
        # We tonen dit als "field-fullwidth" onder de grid, zodat de 3
        # categorieën lekker breed kunnen ademen.
        "eigendomsverhouding": {
            "koop_pct": buurt.koop_pct,
            "sociale_huur_pct": buurt.sociale_huur_pct,
            "particuliere_huur_pct": buurt.particuliere_huur_pct,
            "ref": _as_ref(references.ref_eigendomsverhouding(
                buurt.koop_pct,
                buurt.sociale_huur_pct,
                buurt.particuliere_huur_pct,
            )),
            "scope": eig_scope,
        },
    }


def _build_buren(
    buurt: Optional[cbs.BuurtStats],
    verkiezing: Optional[verkiezingen.VerkiezingsUitslag],
    migratie: Optional[dict] = None,
) -> dict:
    if buurt is None:
        return {"available": False}
    h_tot = buurt.huishoudens or 0
    pct_eenpersoons = (
        round(100 * buurt.eenpersoonshuishoudens / h_tot, 1)
        if buurt.eenpersoonshuishoudens is not None and h_tot > 0
        else None
    )
    pct_met_kinderen = (
        round(100 * buurt.huishoudens_met_kinderen / h_tot, 1)
        if buurt.huishoudens_met_kinderen is not None and h_tot > 0
        else None
    )
    scope = buurt.scope or {}

    # Leeftijdsprofiel: absolute aantallen → percentages van totaal.
    # We tonen 3 klassen in UI (jong/volwassen/oud) door de 5 CBS-klassen
    # te hergroeperen: 0-15 = kinderen, 15-65 = werkzame leeftijd, 65+ = oud.
    leef_raw = {
        "0-15":   buurt.leeftijd_0_15,
        "15-25":  buurt.leeftijd_15_25,
        "25-45":  buurt.leeftijd_25_45,
        "45-65":  buurt.leeftijd_45_65,
        "65+":    buurt.leeftijd_65plus,
    }
    leef_totaal = sum(v for v in leef_raw.values() if v is not None)
    leeftijdsprofiel = None
    if leef_totaal > 0:
        def _pct(key: str) -> Optional[float]:
            v = leef_raw.get(key)
            return round(100 * v / leef_totaal, 1) if v is not None else None
        pct_0_15 = _pct("0-15")
        pct_15_25 = _pct("15-25")
        pct_25_45 = _pct("25-45")
        pct_45_65 = _pct("45-65")
        pct_65plus = _pct("65+")
        # Aggregeer tot 3 klassen voor compactere UI-weergave
        pct_jong = pct_0_15
        pct_midden = round(
            sum(p for p in (pct_15_25, pct_25_45, pct_45_65) if p is not None), 1
        ) if any(p is not None for p in (pct_15_25, pct_25_45, pct_45_65)) else None
        pct_oud = pct_65plus
        leef_scope = next(
            (scope.get(k) for k in (
                "leeftijd_0_15", "leeftijd_15_25", "leeftijd_25_45",
                "leeftijd_45_65", "leeftijd_65plus",
            ) if scope.get(k)),
            None,
        )
        leeftijdsprofiel = {
            "pct_jong": pct_jong,          # 0-15
            "pct_midden": pct_midden,      # 15-65
            "pct_oud": pct_oud,            # 65+
            # Fijngranulair voor tooltip / gedetailleerde weergave
            "fijn": {
                "0-15": pct_0_15,
                "15-25": pct_15_25,
                "25-45": pct_25_45,
                "45-65": pct_45_65,
                "65+": pct_65plus,
            },
            "ref": _as_ref(references.ref_leeftijdsprofiel(pct_jong, pct_midden, pct_oud)),
            "scope": leef_scope,
        }

    out = {
        "available": True,
        "eenpersoons": {
            "value": pct_eenpersoons,
            "unit": "%",
            "ref": _as_ref(references.ref_eenpersoons(pct_eenpersoons)),
            "scope": scope.get("eenpersoonshuishoudens") or scope.get("huishoudens"),
        },
        "met_kinderen": {
            "value": pct_met_kinderen,
            "unit": "%",
            "ref": _as_ref(references.ref_met_kinderen(pct_met_kinderen)),
            "scope": scope.get("huishoudens_met_kinderen"),
        },
        # Gemiddelde huishoudensgrootte: 1.5 = singles, 2.0-2.4 = gemengd,
        # 2.5+ = gezinsbuurt. Data was al aanwezig, nu zichtbaar.
        "huishoudensgrootte": {
            "value": buurt.huishoudensgrootte,
            "unit": "personen",
            "ref": _as_ref(references.ref_huishoudensgrootte(buurt.huishoudensgrootte)),
            "scope": scope.get("huishoudensgrootte"),
        },
        "leeftijdsprofiel": leeftijdsprofiel,
        # Migratieachtergrond uit KWB 2020 (laatste buurt-beschikbare jaargang).
        # Orchestrator retourneert ook 'None' als niets gevonden — frontend
        # rendert alleen als er echt data is.
        "migratieachtergrond": _migratie_to_dict(migratie) if migratie else None,
        # Legacy fields voor backwards-compat (frontend rendert ze niet meer,
        # maar evt. oude clients of caches zouden breken zonder).
        "inwoners": {
            "value": buurt.inwoners,
            "unit": None,
            "ref": _as_ref(references.ref_inwoners(buurt.inwoners)),
            "scope": scope.get("inwoners"),
        },
        "dichtheid": {
            "value": buurt.bevolkingsdichtheid_per_km2,
            "unit": "per km²",
            "ref": _as_ref(references.ref_dichtheid(buurt.bevolkingsdichtheid_per_km2)),
            "scope": scope.get("bevolkingsdichtheid"),
        },
    }
    if verkiezing is not None:
        out["verkiezing_tk2023"] = {  # key behouden voor frontend-compatibiliteit
            "election": verkiezing.election,
            "date": verkiezing.date,
            "top3": verkiezing.top3,
            "per_gemeente_beschikbaar": verkiezing.per_gemeente_beschikbaar,
        }
    return out


def _migratie_to_dict(m: dict) -> dict:
    """Serialize migratie-data naar UI-klaar dict met ref + karakter."""
    ref = references.ref_migratieachtergrond(
        pct_nederlands=m.get("pct_nederlands"),
        pct_westers=m.get("pct_westers"),
        pct_niet_westers=m.get("pct_niet_westers"),
    )
    return {
        "pct_nederlands":   m.get("pct_nederlands"),
        "pct_westers":      m.get("pct_westers"),
        "pct_niet_westers": m.get("pct_niet_westers"),
        "totaal_inwoners":  m.get("totaal_inwoners"),
        "scope":            m.get("scope"),
        "peiljaar":         m.get("peiljaar"),
        "ref":              _as_ref(ref),
    }


def _build_voorzieningen(lijst: list[dict]) -> dict:
    """Voorzieningen-lijst gesorteerd van dichtbij naar ver.

    Per item:
      - type, label, categorie, emoji (voor filter-chips in UI)
      - km + meters (meters wint bij OSM, bij CBS is meters afgeleid)
      - naam (OSM) of None (CBS-gemeente-gemiddelde)
      - source ('osm' / 'cbs') zodat de UI kan tonen "gemeente-gemiddelde"
        als disclaimer bij CBS-items
    """
    if not lijst:
        return {"available": False}
    # Fallback-labels voor types die alleen uit CBS komen (niet uit OSM);
    # OSM zet al label+categorie direct in het dict.
    cbs_labels = {
        "supermarkt":              ("Supermarkt",              "boodschappen"),
        "dagelijkse_levensmiddelen":("Buurtsuper / dagwinkel", "boodschappen"),
        "huisarts":                ("Huisarts",                "zorg"),
        "huisartsenpost":          ("Huisartsenpost",          "zorg"),
        "apotheek":                ("Apotheek",                "zorg"),
        "fysiotherapeut":          ("Fysiotherapeut",          "zorg"),
        "ziekenhuis":              ("Ziekenhuis",              "zorg"),
        "basisschool":             ("Basisschool",             "kinderen"),
        "kinderdagverblijf":       ("Kinderdagverblijf",       "kinderen"),
        "buitenschoolse_opvang":   ("Buitenschoolse opvang",   "kinderen"),
        "restaurant":              ("Restaurant",              "entertainment"),
        "cafe":                    ("Café",                    "entertainment"),
        "cafetaria":               ("Cafetaria",               "entertainment"),
        "hotel":                   ("Hotel",                   "entertainment"),
        "park":                    ("Park",                    "sport"),
        "bos":                     ("Bos",                     "sport"),
        "sportterrein":            ("Sportterrein",            "sport"),
        "zwembad":                 ("Zwembad",                 "sport"),
        "treinstation":            ("Treinstation",            "transport"),
        "overstapstation":         ("Intercity-station",       "transport"),
        "oprit_snelweg":           ("Oprit snelweg",           "transport"),
        "bibliotheek":             ("Bibliotheek",             "cultuur"),
        "museum":                  ("Museum",                  "cultuur"),
        "bioscoop":                ("Bioscoop",                "cultuur"),
    }
    items = []
    for v in lijst:
        # OSM levert label + categorie al mee; CBS-entries nog niet.
        label = v.get("label")
        cat = v.get("categorie")
        if not label:
            label, cat = cbs_labels.get(
                v["type"], (v["type"].replace("_", " ").title(), "overig")
            )
        items.append({
            "type": v["type"],
            "label": label,
            "categorie": cat,
            "emoji": v.get("emoji", "•"),
            "km": v["km"],
            "meters": v.get("meters"),
            "naam": v.get("naam"),
            "source": v.get("source", "cbs"),
        })
    return {"available": True, "items": items}


def _build_veiligheid(m: Optional[politie.Misdrijven]) -> dict:
    if m is None:
        return {"available": False}
    return {
        "available": True,
        "periode": f"{m.periode_van} – {m.periode_tot}",
        "woninginbraak": {
            "value": m.woninginbraak_per_1000_inwoners,
            "unit": "per 1.000 inw",
            "absoluut_12m": m.woninginbraak_12m,
            "ref": _as_ref(references.ref_woninginbraak(m.woninginbraak_per_1000_inwoners)),
        },
        # Geweldsmisdrijven per 1.000 inw (mishandeling + bedreiging + straatroof
        # + openlijk geweld + overval). Distinct van totaal: vertelt over
        # persoonlijke veiligheid op straat, niet over tourist-delicten.
        "geweld": {
            "value": m.geweld_per_1000_inwoners,
            "unit": "per 1.000 inw",
            "absoluut_12m": m.geweld_12m,
            "ref": _as_ref(references.ref_geweld(m.geweld_per_1000_inwoners)),
        },
        # Fietsendiefstal — dagelijks leven indicator + proxy voor sociale controle
        "fietsendiefstal": {
            "value": m.fietsendiefstal_per_1000_inwoners,
            "unit": "per 1.000 inw",
            "absoluut_12m": m.fietsendiefstal_12m,
            "ref": _as_ref(references.ref_fietsendiefstal(m.fietsendiefstal_per_1000_inwoners)),
        },
        # Legacy: absoluut geweld 12m (voor evt. frontend die nog dit veld leest)
        "geweld_12m": m.geweld_12m,
        "totaal": {
            "value": m.totaal_per_1000_inwoners,
            "unit": "per 1.000 inw",
            "absoluut_12m": m.totaal_12m,
            "ref": _as_ref(references.ref_totaal_misdrijven(m.totaal_per_1000_inwoners)),
        },
    }


def _build_cover(l: Optional[leefbaarometer.LeefbaarheidScore]) -> dict:
    """Leefbaarometer cover: totaal-score + 5 gewogen sub-dimensies.

    Waarschuwingslogica: de Leefbaarometer 3.0 berekent totaalscore door
    dimensies te **wegen** naar hun invloed op huizenprijzen en bewoners-
    perceptie (multivariate regressie, geen rekenkundig gemiddelde).
    Daardoor kan totaal = 9 terwijl sub-dimensies sterk variëren.
    We detecteren dat en tonen het expliciet — anders voelt de UI oneerlijk.
    """
    if l is None:
        return {"available": False}
    percentile = round((l.score - 1) / 8 * 100)

    # Spread-analyse: waarschuw als er een dimensie is die duidelijk lager
    # scoort dan de totaalscore.
    dim_scores = [d.score for d in l.dimensies] if l.dimensies else []
    zwakste_dim = None
    waarschuwing = None
    if dim_scores:
        min_dim = min(l.dimensies, key=lambda d: d.score)
        spread = l.score - min_dim.score
        if spread >= 4:  # significant gat
            zwakste_dim = min_dim.label.lower()
            waarschuwing = (
                f"Let op: '{min_dim.label}' scoort duidelijk lager "
                f"({min_dim.score}/9). De Leefbaarometer weegt dimensies naar "
                f"hun invloed op vastgoedwaarde — een hoge voorzieningen- of "
                f"ligging-score kan statistisch de totaalscore naar boven "
                f"trekken ondanks zwakkere sub-aspecten."
            )

    # Genuanceerde betekenis: vervang de generieke tekst als er grote spread is
    betekenis = l.betekenis
    if waarschuwing:
        betekenis = (
            "Hoge totaalscore, maar niet alle aspecten zijn even sterk. "
            "Zie de uitsplitsing hieronder."
        )

    # Grid-vs-buurt verschil, helder verwoord voor UI.
    # Gebruikt buurtnaam zodat de context concreet wordt
    # (bv. "de Oranjebuurt" i.p.v. "de buurt als geheel").
    grid_vs_buurt = None
    if l.buurt_score is not None:
        # Gebruik de buurtnaam zoals BZK hem levert (bv. 'Weerestein');
        # alleen de eerste letter van de ZIN hoofdletter maken, niet van
        # de buurtnaam zelf.
        buurt_phrase = (
            f"de {l.buurt_naam}" if l.buurt_naam else "de hele buurt"
        )
        delta = l.score - l.buurt_score
        if delta >= 2:
            # Zin begint met 'Direct...' — buurt_phrase blijft middenin
            grid_vs_buurt = (
                f"Direct om dit huis (straal ~100 m): {l.score}/9. "
                f"In {buurt_phrase} als geheel: {l.buurt_score}/9. "
                f"Dit adres zit in een betere uithoek dan de buurt gemiddeld."
            )
        elif delta <= -2:
            grid_vs_buurt = (
                f"Direct om dit huis (straal ~100 m): {l.score}/9. "
                f"In {buurt_phrase} als geheel: {l.buurt_score}/9. "
                f"Dit specifieke plekje trekt het buurtgemiddelde omlaag."
            )
        elif delta == 0:
            grid_vs_buurt = (
                f"Direct om dit huis én {buurt_phrase} als geheel scoren "
                f"gelijk ({l.score}/9)."
            )
        else:
            richting = "iets beter" if delta > 0 else "iets minder"
            grid_vs_buurt = (
                f"Direct om dit huis (100 m): {l.score}/9, {richting} dan "
                f"{buurt_phrase} als geheel ({l.buurt_score}/9)."
            )

    # Ontwikkeling (trend over tijd) — optioneel, beide perioden parallel
    ontwikkeling_recent = _serialize_ontwikkeling(l.ontwikkeling_recent, "2 jaar")
    ontwikkeling_lang = _serialize_ontwikkeling(l.ontwikkeling_lang, "10 jaar")

    return {
        "available": True,
        "score": l.score,
        "max": 9,
        "percentile_nl": percentile,
        "label": l.label,
        "vs_nl_gem": l.vs_nl_gem,
        "betekenis": betekenis,
        "waarschuwing": waarschuwing,
        "zwakste_dimensie": zwakste_dim,
        "buurt_score": l.buurt_score,
        "buurt_label": l.buurt_label,
        "buurt_naam": l.buurt_naam,
        "grid_vs_buurt": grid_vs_buurt,
        "dimensies": [
            {
                "key": d.key,
                "label": d.label,
                "score": d.score,
                "percentile_nl": round((d.score - 1) / 8 * 100),
                "beschrijving": d.beschrijving,
                # Relatief label: hoe verhoudt deze dimensie zich tot de totaal?
                "vs_totaal": _relatief_label(d.score, l.score),
            }
            for d in l.dimensies
        ],
        # Trends over tijd — de frontend rendert hiermee een compacte
        # "hoe ontwikkelt deze buurt zich?"-sectie.
        "ontwikkeling": {
            "recent": ontwikkeling_recent,
            "lang": ontwikkeling_lang,
        },
    }


# Dimensie-labels hergebruiken voor de trend-weergave
_DIM_LABELS = {key: label for key, label, _ in leefbaarometer.DIMENSIES}


def _serialize_ontwikkeling(
    o: Optional[leefbaarometer.Ontwikkeling], horizon_label: str
) -> Optional[dict]:
    """Serialiseer een Ontwikkeling naar UI-klaar dict.

    - `chip_level` kleurt de chip rood/grijs/groen
    - `beschrijving` is een menselijke duiding
    - `veranderingen` = lijst van significante dimensie-veranderingen,
       zowel positief als negatief (threshold: |klasse-5| ≥ 2).

    Belangrijk: we tonen BEIDE kanten wanneer er tegelijkertijd verbetering
    en verslechtering is. Een 10-jaars trend kan bv. voorzieningen (+) én
    overlast (-) laten zien — beide horen zichtbaar te zijn, anders verdwijnt
    het negatieve signaal in het positieve (wat bij gelijke delta gebeurde).
    """
    if o is None:
        return None
    # Chip-niveau op totaal
    if o.label == "verbeterd":
        chip = "good"
    elif o.label == "verslechterd":
        chip = "warn"
    else:
        chip = "neutral"

    # Alle dimensies met enige afwijking (≥ 1 van stabiel=5) verzamelen.
    # Threshold = 1 i.p.v. 2 omdat de Leefbaarometer-klasse discreet is (1-9):
    # bij klasse 6 of 4 is er al een reële verandering die past bij een totaal
    # dat 'verbeterd' of 'verslechterd' scoort. Threshold 2 verborg te veel.
    verbeteringen: list[dict] = []
    verslechteringen: list[dict] = []
    for key, klasse in (o.per_dimensie or {}).items():
        if klasse is None:
            continue
        delta = klasse - 5
        if delta == 0:
            continue
        # Gradatie aan de hand van de absolute afwijking van 5
        if abs(delta) >= 3:
            gradatie = "sterk"
        elif abs(delta) == 2:
            gradatie = "matig"
        else:  # abs(delta) == 1
            gradatie = "licht"
        richting = "verbeterd" if delta > 0 else "verslechterd"
        entry = {
            "key": key,
            "label": _DIM_LABELS.get(key, key),
            "klasse": klasse,
            "richting": richting,
            "gradatie": gradatie,                         # 'licht' / 'matig' / 'sterk'
            "richting_tekst": f"{gradatie} {richting}",   # 'licht verbeterd', 'matig verslechterd'
            "delta": delta,
        }
        (verbeteringen if delta > 0 else verslechteringen).append(entry)
    verbeteringen.sort(key=lambda e: -e["delta"])
    verslechteringen.sort(key=lambda e: e["delta"])  # meest-negatief eerst

    # Veranderingen-lijst: top 2 verbeteringen + top 2 verslechteringen (max).
    # Meestal zijn er hooguit 1-2 dimensies in beweging; we tonen ze allemaal
    # als ze echt afwijken. Zo zie je op Paramaribo 10j bv. "Overlast &
    # veiligheid: licht verbeterd (6/9)" i.p.v. stilte.
    veranderingen: list[dict] = []
    veranderingen.extend(verbeteringen[:2])
    veranderingen.extend(verslechteringen[:2])

    # Backwards-compat: 'sterkste_verandering' pakt de grootste (absolute) —
    # bij gelijke delta geeft de warn de voorkeur (meer actionable).
    sterkste = None
    if verslechteringen and verbeteringen:
        if abs(verslechteringen[0]["delta"]) >= verbeteringen[0]["delta"]:
            sterkste = verslechteringen[0]
        else:
            sterkste = verbeteringen[0]
    elif verslechteringen:
        sterkste = verslechteringen[0]
    elif verbeteringen:
        sterkste = verbeteringen[0]

    # Compacte menselijke beschrijving — geeft direct context
    if o.score <= 2:
        beschrijving = f"Sterk verslechterd over {horizon_label}."
    elif o.score == 3:
        beschrijving = f"Licht verslechterd over {horizon_label}."
    elif o.score == 5:
        beschrijving = f"Stabiel over {horizon_label}."
    elif o.score <= 6:
        beschrijving = f"Licht verbeterd over {horizon_label}."
    elif o.score >= 8:
        beschrijving = f"Sterk verbeterd over {horizon_label}."
    else:
        beschrijving = f"Verbeterd over {horizon_label}."

    # Bij totaalscore 'stabiel' maar onderliggend wél beweging: nuanceren.
    # Anders lijkt "stabiel over 10 jaar" in tegenspraak met "voorzieningen
    # verbeterd + overlast verslechterd" eronder.
    if o.score == 5 and veranderingen:
        beschrijving = (
            f"Totaal stabiel over {horizon_label}, maar onder de motorkap "
            f"wél beweging:"
        )
    # Tegengesteld scenario: totaal BEWOOG (verbeterd of verslechterd) maar
    # geen enkele dimensie sprong naar een andere klasse. Dit gebeurt wanneer
    # alle dimensies licht (<1 klasse) veranderden — de som was genoeg om het
    # totaal omhoog te duwen, maar individueel te klein voor klasse-rounding.
    # Zonder uitleg lijkt "Licht verbeterd" in tegenspraak met 5 keer "stabiel".
    elif o.score != 5 and not veranderingen:
        richting = "verbetering" if o.score > 5 else "verslechtering"
        beschrijving += (
            f" Kleine {richting} verspreid over alle dimensies — "
            f"niet één specifieke dimensie sprong naar een andere klasse."
        )

    return {
        "periode": o.periode,
        "horizon": horizon_label,
        "klasse": o.score,
        "max": 9,
        "label": o.label,
        "chip_level": chip,
        "raw_delta": o.raw_delta,
        "beschrijving": beschrijving,
        "sterkste_verandering": sterkste,     # legacy veld, 1 item
        "veranderingen": veranderingen,       # nieuwe: lijst van 1-2 items
        "per_dimensie": [
            {
                "key": k,
                "label": _DIM_LABELS.get(k, k),
                "klasse": v,
                "richting": (
                    "verbeterd" if v > 5 else "verslechterd" if v < 5 else "stabiel"
                ),
            }
            for k, v in (o.per_dimensie or {}).items()
        ],
    }


def _relatief_label(sub_score: int, totaal_score: int) -> str:
    """Eén woord over de sub-score t.o.v. totaal: 'onder'/'gelijk'/'boven'."""
    delta = sub_score - totaal_score
    if delta <= -3:
        return "sterk onder totaal"
    if delta <= -1:
        return "onder totaal"
    if delta >= 3:
        return "sterk boven totaal"
    if delta >= 1:
        return "boven totaal"
    return "op niveau"


def _build_leefkwaliteit(
    l: Optional[rivm_lki.Luchtkwaliteit],
    g: Optional[rivm_geluid.GeluidOpGevel],
) -> dict:
    """Sectie 5 — Leefkwaliteit. Luchtkwaliteit + geluid."""
    if l is None and g is None:
        return {"available": False}
    out: dict = {"available": True}
    if l is not None:
        out["pm25"] = {"value": l.pm25_ug_m3, "unit": "µg/m³", "ref": _as_ref(references.ref_pm25(l.pm25_ug_m3))}
        out["no2"]  = {"value": l.no2_ug_m3,  "unit": "µg/m³", "ref": _as_ref(references.ref_no2(l.no2_ug_m3))}
        out["pm10"] = {"value": l.pm10_ug_m3, "unit": "µg/m³", "ref": _as_ref(references.ref_pm10(l.pm10_ug_m3))}
    if g is not None and g.lden_totaal_db is not None:
        out["geluid"] = {
            "value": g.lden_totaal_db,
            "unit": "dB Lden",
            "dominante_bron": g.dominante_bron,
            "per_bron": g.per_bron,
            "ref": _build_geluid_ref(g),
        }
    return out


def _build_geluid_ref(g: rivm_geluid.GeluidOpGevel) -> dict:
    """Interpretatie-context voor Lden-waarde. Inline (niet in references.py)
    omdat de betekenis sterk afhangt van de bron (trein 45 dB is al hinder,
    wegverkeer 45 dB niet)."""
    db = g.lden_totaal_db
    if g.hinder_niveau == "geen":
        level, chip = "good", "geen hinder"
        msg = "Rustige locatie. Lden onder EU-hinderdrempel van 55 dB."
    elif g.hinder_niveau == "matig":
        level, chip = "warn", "matige hinder"
        msg = f"Merkbare {g.dominante_bron or 'omgevings'}-geluid. Boven WHO-advies."
    else:
        level, chip = "warn", "ernstige hinder"
        msg = (
            f"Hoge cumulatieve {g.dominante_bron or 'omgevings'}-belasting. "
            "EU classificeert Lden > 65 dB als ernstige hinder; "
            "gevelisolatie kan nodig zijn."
        )
    return {
        "chip_level": level,
        "chip_text": chip,
        "nl_gemiddelde": "~55 dB in stedelijk gebied",
        "norm": "WHO 53 · EU-hinder 55 · ernstig 65 dB",
        "betekenis": msg,
    }


def _build_klimaat(k: Optional[klimaat.Klimaatrisico]) -> dict:
    """Bodem-aware klimaat-output.

    De adapter retourneert nu een lijst Risico-objecten plus het bodemtype.
    Wij serialiseren alleen het SUBSET dat relevant is voor dit bodemtype
    plus universele risico's (hittestress, overstroming, wateroverlast).

    Legacy-velden (paalrot {value, unit, ref, ...}, hittestress, waterdiepte_cm)
    blijven gevuld voor backwards-compat met frontend-code die deze leest —
    maar de NIEUWE `risicos` lijst is wat de UI primair rendert.
    """
    if k is None:
        return {"available": False}

    # Map voor snelle lookup op key
    by_key = {r.key: r for r in (k.risicos or [])}

    # Filter op relevantie + bouw UI-klare dicts met referenties
    relevante: list[dict] = []
    for r in (k.risicos or []):
        # Filter 1: alleen relevante risico's voor dit bodemtype (+universeel)
        if not r.relevant:
            continue
        # Filter 2: skip bodemdaling < 1mm/jaar (praktisch geen signaal)
        if r.key == "bodemdaling" and (r.waarde or 0) < 1.0:
            continue
        # Overstroming + overstromings-diepte: ALTIJD tonen, ook bij 0.
        # Klasse=0 of waarde=0 betekenen expliciet 'geen risico' (achter dijk,
        # hoger gelegen) — dat is zelf een waardevol antwoord voor de
        # gebruiker die zich anders afvraagt of we überhaupt hebben gekeken.

        ref = _risico_ref(r)
        entry = {
            "key": r.key,
            "label": r.label,
            "klasse": r.klasse,
            "pct": r.pct,
            "waarde": r.waarde,
            "eenheid": r.eenheid,
            "aantal_panden": r.aantal_panden,
            "buurtnaam": r.buurtnaam,
            "ref": _as_ref(ref),
        }
        relevante.append(entry)

    # Sorteer: warn eerst (voor zichtbaarheid), dan neutral, dan good
    level_rank = {"warn": 0, "neutral": 1, "good": 2, None: 3}
    relevante.sort(key=lambda x: level_rank.get((x.get("ref") or {}).get("chip_level"), 3))

    # Legacy-velden voor backwards-compat met oude frontend-code
    paalrot = by_key.get("paalrot")
    hitte = by_key.get("hittestress")
    water = by_key.get("wateroverlast")

    out: dict = {
        "available": True,
        "bodemtype_code": k.bodemtype_code,
        "bodemtype_label": k.bodemtype_label,
        "risicos": relevante,

        # --- Legacy velden (oude renderer leest deze) ---
        "paalrot": {
            "value": paalrot.pct if paalrot else None,
            "unit": "%",
            "buurt": paalrot.buurtnaam if paalrot else None,
            "aantal_panden": paalrot.aantal_panden if paalrot else None,
            "pct_sterk": paalrot.pct if paalrot else None,
            "pct_mild": paalrot.pct if paalrot else None,
            "ref": _as_ref(references.ref_paalrot(paalrot.pct if paalrot else None, None)),
        } if paalrot else None,
        "hittestress": {
            "value": hitte.klasse if hitte else None,
            "label": klimaat.HITTE_LABELS.get(hitte.klasse) if hitte else None,
            "ref": _as_ref(references.ref_hittestress(hitte.klasse if hitte else None)),
        } if hitte else None,
    }
    if water and water.waarde:
        out["waterdiepte_cm"] = int(round(water.waarde))
    return out


def _build_onderwijs(lat: float, lon: float) -> dict:
    """Sectie 7 — kinderopvang + scholen binnen 1.5 km.

    Geen netwerk-IO: adapter leest in-memory JSON (geladen uit
    apps/api/data/onderwijs.json bij eerste gebruik) en doet haversine.
    """
    if not (lat and lon):
        return {"available": False}
    try:
        result = onderwijs.fetch_onderwijs(lat, lon)
    except Exception:
        return {"available": False}
    return result


def _risico_ref(r: "klimaat.Risico"):  # type: ignore[name-defined]
    """Map Risico naar de juiste reference-functie op basis van key."""
    if r.key == "paalrot":
        return references.ref_paalrot(r.pct, None)
    if r.key == "verschilzetting":
        return references.ref_verschilzetting(r.pct)
    if r.key == "hittestress":
        return references.ref_hittestress(r.klasse)
    if r.key == "wateroverlast":
        return references.ref_wateroverlast_neerslag(r.waarde)
    if r.key == "overstroming":
        return references.ref_overstromingskans(r.klasse)
    if r.key == "overstroming_diepte":
        return references.ref_overstromingsdiepte(r.waarde)
    if r.key == "droogte":
        return references.ref_droogtestress(r.klasse)
    if r.key == "bodemdaling":
        return references.ref_bodemdaling(r.waarde)
    return None


def _provenance(buurtcode: str) -> list[dict]:
    """Welke bronnen zijn geraadpleegd voor deze respons, met peildatum.

    De UI toont dit als kleine tag onder elk cijfer ("Bron: CBS 2024 · 20 apr").
    Zonder provenance wordt de app opaque — essentieel voor vertrouwen.
    """
    return [
        {
            "section": "woning",
            "source": "BAG (Kadaster) via PDOK WFS v2.0",
            "peildatum": "dagelijks geactualiseerd",
        },
        {
            "section": "wijk_economie,buren,voorzieningen",
            "source": f"CBS Kerncijfers Wijken en Buurten ({cbs.DATASET_ID})",
            "peildatum": "2024",
            "buurtcode": buurtcode,
        },
        {
            "section": "adres",
            "source": "PDOK Locatieserver v3.1",
            "peildatum": "realtime",
        },
        {
            "section": "veiligheid",
            "source": "Politie Open Data via CBS (47022NED)",
            "peildatum": "maandcijfers, laatste 12 afgesloten maanden",
            "buurtcode": buurtcode,
        },
        {
            "section": "energielabel",
            "source": "RVO EP-Online (lokale SQLite-cache)",
            "peildatum": "maandelijkse bulk-sync",
        },
        {
            "section": "leefkwaliteit",
            "source": "RIVM Atlas Leefomgeving WMS (GetFeatureInfo)",
            "peildatum": "jaargemiddelde meetjaar",
        },
        {
            "section": "klimaat",
            "source": "Klimaateffectatlas (ArcGIS Online, CAS)",
            "peildatum": "huidig scenario + 2050",
        },
        {
            "section": "cover",
            "source": "Leefbaarometer 2.0 (BZK) via RIVM ALO WMS",
            "peildatum": "2018 (100m-grid)",
        },
        {
            "section": "leefkwaliteit_geluid",
            "source": "RIVM geluid-Lden 2022 via ALO WMS (peiljaar 2020)",
            "peildatum": "2020 cumulatieve belasting",
        },
        {
            "section": "onderwijs",
            "source": "LRK (kinderopvang) + DUO (basisscholen) + Onderwijsinspectie",
            "peildatum": "maandelijkse sync — bronnen actueel",
        },
    ]


def result_as_dict(r: ScanResult) -> dict:
    """Serialize + bouw de sociale vragen + cover-highlights (post-processing)."""
    data = asdict(r)
    # Sociale vragen hergebruiken de reeds-opgebouwde dicts — doen we hier
    # zodat de vragen-module niet af hoeft te weten van de dataclass-structuur.
    data["sociale_vragen"] = social_questions.build(data)
    # Cijferrapport-highlights: 2-3 korte signalen voor onder de cover-score.
    if data.get("cover") and data["cover"].get("available"):
        data["cover"]["highlights"] = _build_highlights(data)
    return data


def _build_highlights(data: dict) -> list[dict]:
    """Scan alle secties en destilleer 2-3 *opvallende* signalen.

    Geen dubbeling van de samengestelde score zelf. We pakken:
      - Beste aspect (sterkste good)
      - Zwakste aspect (sterkste warn)
      - Evt. een opvallend 'mixed' signaal (bv. hoge lucht maar veel geluid)

    Elk highlight: {label, value, level, scope} — frontend rendert als chips.
    """
    candidates: list[dict] = []

    # --- Woning ---
    label = (data.get("woning") or {}).get("energielabel") or {}
    if label.get("value") and (label.get("ref") or {}).get("chip_level"):
        lvl = label["ref"]["chip_level"]
        candidates.append({
            "label": f"Energielabel {label['value']}",
            "value": (label["ref"] or {}).get("chip_text", ""),
            "level": lvl,
        })

    # --- Wijk-economie: WOZ-trend ---
    woz = (data.get("wijk_economie") or {}).get("woz") or {}
    trend = woz.get("trend_pct_per_jaar")
    if trend is not None:
        if trend >= 3:
            candidates.append({"label": "WOZ stijgt", "value": f"+{trend}%/jaar", "level": "good"})
        elif trend <= -2:
            candidates.append({"label": "WOZ daalt", "value": f"{trend}%/jaar", "level": "warn"})

    # --- Klimaat: paalrot ---
    paalrot = (data.get("klimaat") or {}).get("paalrot") or {}
    p_lvl = (paalrot.get("ref") or {}).get("chip_level")
    p_val = paalrot.get("value")
    if p_val is not None and p_lvl == "warn" and p_val >= 40:
        candidates.append({
            "label": "Funderingsrisico",
            "value": f"{p_val}% panden",
            "level": "warn",
        })
    elif p_val is not None and p_lvl == "good" and p_val < 10:
        candidates.append({"label": "Stevige ondergrond", "value": "laag paalrotrisico", "level": "good"})

    # --- Leefkwaliteit: geluid + PM2.5 ---
    geluid = (data.get("leefkwaliteit") or {}).get("geluid") or {}
    g_lvl = (geluid.get("ref") or {}).get("chip_level")
    g_val = geluid.get("value")
    if g_val is not None and g_lvl == "warn":
        candidates.append({"label": "Geluid op gevel", "value": f"{g_val} dB", "level": "warn"})
    elif g_val is not None and g_lvl == "good" and g_val < 50:
        candidates.append({"label": "Stille locatie", "value": f"{g_val} dB", "level": "good"})

    pm = (data.get("leefkwaliteit") or {}).get("pm25") or {}
    pm_lvl = (pm.get("ref") or {}).get("chip_level")
    if pm_lvl == "warn":
        candidates.append({"label": "Fijnstof boven NL", "value": f"{pm['value']} µg/m³", "level": "warn"})
    elif pm_lvl == "good" and pm.get("value") and pm["value"] <= 5:
        candidates.append({"label": "Lucht zeer schoon", "value": f"{pm['value']} µg/m³", "level": "good"})

    # --- Veiligheid ---
    inbr = (data.get("veiligheid") or {}).get("woninginbraak") or {}
    inbr_lvl = (inbr.get("ref") or {}).get("chip_level")
    if inbr_lvl == "warn" and inbr.get("value"):
        candidates.append({"label": "Inbraken bovengem.", "value": f"{inbr['value']} /1.000", "level": "warn"})
    elif inbr_lvl == "good" and inbr.get("value") is not None and inbr["value"] < 1:
        candidates.append({"label": "Weinig inbraken", "value": f"{inbr['value']} /1.000", "level": "good"})

    # --- Buren: dichtheid als 'karakter' ---
    dichtheid = (data.get("buren") or {}).get("dichtheid") or {}
    d_val = dichtheid.get("value")
    if d_val is not None and d_val >= 10000:
        candidates.append({"label": "Zeer stedelijk", "value": f"{d_val:,}/km²".replace(",", "."), "level": "neutral"})
    elif d_val is not None and d_val < 500:
        candidates.append({"label": "Landelijk", "value": f"{d_val:,}/km²".replace(",", "."), "level": "neutral"})

    # --- Oppervlakte als karakter ---
    opp = (data.get("woning") or {}).get("oppervlakte") or {}
    opp_v = opp.get("value")
    if opp_v is not None and opp_v >= 180:
        candidates.append({"label": "Zeer ruim", "value": f"{opp_v} m²", "level": "good"})

    # Selecteer max 3: bij voorkeur 1 good + 1 warn + 1 overig (neutral/tweede).
    # Voorkomt saaie 3x-good of 3x-warn rijen; toont wat écht opvalt.
    goods = [c for c in candidates if c["level"] == "good"]
    warns = [c for c in candidates if c["level"] == "warn"]
    neutrals = [c for c in candidates if c["level"] == "neutral"]

    out: list[dict] = []
    if goods:
        out.append(goods[0])
    if warns:
        out.append(warns[0])
    # Derde slot: 2e warn (als aandacht nodig) of 2e good of neutral
    if len(warns) >= 2:
        out.append(warns[1])
    elif len(goods) >= 2 and len(out) < 3:
        out.append(goods[1])
    elif neutrals and len(out) < 3:
        out.append(neutrals[0])

    return out[:3]
