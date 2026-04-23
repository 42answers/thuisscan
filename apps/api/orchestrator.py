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
from adapters import bag, bereikbaarheid, cbs, klimaat, leefbaarometer, onderwijs, overpass, pdok_locatie, politie, rivm_geluid, rivm_lki, rvo_ep, verbouwing, verkiezingen, woning_extras, woz_loket, wkpb, zonnepanelen


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
    bereikbaarheid: dict  # Sectie 9 — OV + auto
    verbouwing: dict  # Sectie 10 — verbouwingsmogelijkheden (lazy-loaded)
    sociale_vragen: list[dict]  # 3 menselijke vragen (post-processing, deprecated)
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
    # + WOZ-Waardeloket per pand (publieke viewer-API, rate-limited 1/s).
    energielabel = rvo_ep.fetch_label(
        match.postcode or "", match.huisnummer or ""
    )
    tk_uitslag = verkiezingen.fetch_top3(match.gemeentecode or "")
    woz_adres = await _cached_fetch_woz_adres(
        match.bag_verblijfsobject_id or "",
    )

    # Stap 2d: RIVM luchtkwaliteit + Klimaateffectatlas + Leefbaarometer
    # (allemaal punt-queries op externe services; parallel).
    # Klimaat (CAS — 8 parallel sub-calls, ~500-1500ms), bereikbaarheid
    # (Overpass — 2-5s cold) en woning-extras (RCE WFS + Overpass-groen,
    # 500-1500ms) zijn de zwaarste externe deps. Verplaats naar aparte
    # /klimaat, /bereikbaarheid en /woning-extras endpoints zodat de hoofd-
    # scan niet wacht. Hier alleen lucht + leefbaarheid + geluid (sneller).
    lucht_task = _cached_fetch_lucht(match.rd_x, match.rd_y)
    leef_task = _cached_fetch_leefbaarheid(match.rd_x, match.rd_y)
    geluid_task = _cached_fetch_geluid(match.rd_x, match.rd_y)
    lucht, leefbaarheid, geluid = await asyncio.gather(
        lucht_task, leef_task, geluid_task
    )
    extras = None  # lazy geladen via /woning-extras endpoint

    # Buurtnaam uit Leefbaarometer: handig voor het adres-kopje
    buurt_naam = leefbaarheid.buurt_naam if leefbaarheid else None

    # Stap 3: samenstellen — volgorde = volgorde in UI
    return ScanResult(
        cover=_build_cover(leefbaarheid),
        adres={
            "display_name": match.display_name,
            "postcode": match.postcode,
            "huisnummer": match.huisnummer,
            "huisnummertoevoeging": match.huisnummertoevoeging,
            "huisletter": match.huisletter,
            "buurtcode": match.buurtcode,
            "buurt_naam": buurt_naam,
            "wijkcode": match.wijkcode,
            "gemeentecode": match.gemeentecode,
            "bag_verblijfsobject_id": match.bag_verblijfsobject_id,
            "wgs84": {"lat": match.lat, "lon": match.lon},
            "rd": {"x": match.rd_x, "y": match.rd_y},
        },
        woning=_build_woning(pand, energielabel, woz_adres, extras),
        wijk_economie=_build_wijk_economie(buurt, woz_trend),

        buren=_build_buren(buurt, tk_uitslag, migratie),
        # Voorzieningen worden apart geladen via /voorzieningen endpoint
        # (Overpass-call duurt 3-6s cold; frontend haalt deze async op nadat
        # de hoofdpagina is gerenderd). Hier een 'pending' placeholder zodat
        # de frontend weet dat het nog komt.
        voorzieningen={"available": False, "pending": True},
        veiligheid=_build_veiligheid(misdrijven),
        leefkwaliteit=_build_leefkwaliteit(lucht, geluid),
        # Klimaat + bereikbaarheid worden lazy geladen via aparte endpoints.
        # Frontend toont skeleton tot data binnen is.
        klimaat={"available": False, "pending": True},
        onderwijs=_build_onderwijs(match.lat, match.lon),
        bereikbaarheid={"available": False, "pending": True},
        # Verbouwing (Sectie 10): lazy geladen via /verbouwing endpoint.
        # 3 WFS-calls (BRK + RCE + BAG-geom) + Shapely ~600-900ms.
        verbouwing={"available": False, "pending": True},
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
    bag_vbo: str,
) -> Optional[woz_loket.WozWaarde]:
    """Pand-WOZ via WOZ-Waardeloket viewer-API (geen key, rate-limited 1/s).

    We delen deze cache met fetch_woz_pand() (`/woz` endpoint) zodat een
    request van de hoofd-scan ook de losse endpoint-call goedkoop maakt.
    Cache 365 dagen — WOZ-peildata muteren jaarlijks, dus ruim TTL is veilig.

    Retourneert None bij missing BAG-VBO of fail; UI valt dan terug op
    buurt-WOZ (CBS) die in dezelfde scan al wordt opgehaald.
    """
    if not bag_vbo:
        return None
    key = f"woz_pand:{bag_vbo}"
    hit = _cache_get(key, 365 * 24 * 3600)
    # Cache kan zowel WozWaarde (van deze functie) als dict (van fetch_woz_pand)
    # zijn — we lezen alleen WozWaarde-vorm uit; dict laten we de andere route.
    if isinstance(hit, woz_loket.WozWaarde):
        return hit
    try:
        result = await woz_loket.fetch_woz(bag_vbo)
    except Exception:
        return None
    if result is not None and result.huidige_waarde_eur is not None:
        _cache_set(key, result)
    return result


async def fetch_woz_pand(bag_vbo_id: str) -> dict:
    """Los endpoint: pand-specifieke WOZ-waarde via WOZ-loket.

    Rate-limited (1/sec globaal, beleefd) + 365d cache per BAG-id.
    Retourneert een compact dict voor de UI; bij geen data:
    {"available": False}.
    """
    if not bag_vbo_id:
        return {"available": False}
    key = f"woz_pand:{bag_vbo_id}"
    hit = _cache_get(key, 365 * 24 * 3600)
    if isinstance(hit, dict):
        return hit
    try:
        result = await woz_loket.fetch_woz(bag_vbo_id)
    except Exception:
        return {"available": False}
    if result is None or result.huidige_waarde_eur is None:
        out = {"available": False}
    else:
        out = {
            "available": True,
            "wozobjectnummer": result.wozobjectnummer,
            "huidige_waarde_eur": result.huidige_waarde_eur,
            "huidige_peildatum": result.huidige_peildatum,
            "trend_pct_per_jaar": result.trend_pct_per_jaar,
            "historie": result.historie,
        }
    _cache_set(key, out)
    return out


async def fetch_klimaat_section(
    lat: float, lon: float, rd_x: float, rd_y: float
) -> dict:
    """Los endpoint: klimaatrisico bodem-aware (8 CAS calls).

    Wordt door /klimaat in main.py aangeroepen — niet door scan().
    Deze call is duur (~500-1500ms cold) en hoort dus achter een aparte
    endpoint zodat de hoofdpagina snel toont.
    """
    k = await _cached_fetch_klimaat(lat, lon, rd_x, rd_y)
    return _build_klimaat(k)


async def fetch_bereikbaarheid_section(lat: float, lon: float) -> dict:
    """Los endpoint: bereikbaarheid (Overpass route-relations + werkcentra).

    Overpass cold-call duurt 2-5s. Cached na 100ms.
    """
    b = await _cached_fetch_bereikbaarheid(lat, lon)
    return _build_bereikbaarheid(b)


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


async def _cached_fetch_woning_extras(
    lat: float, lon: float, rd_x: float, rd_y: float,
    gemeentecode: Optional[str],
) -> Optional[woning_extras.WoningExtras]:
    """Rijksmonument + erfpacht + groen-nabij parallel.

    Alle 3 zijn goedkoop genoeg voor main /scan flow (~300-800ms totaal).
    Cache 30 dagen — data wijzigt zelden (monumenten-register,
    erfpacht-prevalentie, groen in buurt).
    """
    if not (lat and lon and rd_x and rd_y):
        return None
    key = f"woning_extras:{round(lat*1000)}_{round(lon*1000)}:{gemeentecode}"
    hit = _cache_get(key, 30 * 24 * 3600)
    if isinstance(hit, woning_extras.WoningExtras):
        return hit
    try:
        result = await woning_extras.fetch_woning_extras(
            lat=lat, lon=lon, rd_x=rd_x, rd_y=rd_y,
            gemeentecode=gemeentecode,
        )
    except Exception:
        return None
    if result:
        _cache_set(key, result)
    return result


async def _cached_fetch_verbouwing(
    lat: float, lon: float, rd_x: float, rd_y: float,
    bag_pand_id: Optional[str],
    gemeentecode: Optional[str] = None,
    gemeente_naam: Optional[str] = None,
    eigen_vbo_id: Optional[str] = None,
) -> Optional[verbouwing.VerbouwingsInfo]:
    """Verbouwing: BRK-perceel + RCE-gezicht + Shapely achtererf-analyse +
    gemeentelijk-monument-check (per-gemeente dispatch).

    Cache 30 dagen — percelen en beschermde gezichten veranderen bijna nooit.
    Cache-key versie 'v2': inclusief pand_op_perceel + gemeentelijk monument.
    """
    if not (lat and lon):
        return None
    # v3: inclusief stapeling-analyse (VBO's per pand voor verdieping).
    key = f"verbouwing:v3:{round(lat*10000)}_{round(lon*10000)}:{bag_pand_id or ''}:{eigen_vbo_id or ''}:{gemeentecode or ''}"
    hit = _cache_get(key, 30 * 24 * 3600)
    if isinstance(hit, verbouwing.VerbouwingsInfo):
        return hit
    try:
        result = await verbouwing.fetch_verbouwing(
            lat=lat, lon=lon, rd_x=rd_x, rd_y=rd_y,
            bag_pand_id=bag_pand_id,
            gemeentecode=gemeentecode,
            gemeente_naam=gemeente_naam,
            eigen_vbo_id=eigen_vbo_id,
        )
    except Exception:
        return None
    if result:
        _cache_set(key, result)
    return result


async def fetch_verbouwing_section(
    lat: float, lon: float, rd_x: float, rd_y: float,
    bag_pand_id: Optional[str],
    gemeentecode: Optional[str] = None,
    gemeente_naam: Optional[str] = None,
    huisnummertoevoeging: Optional[str] = None,
    eigen_vbo_id: Optional[str] = None,
) -> dict:
    """Los endpoint voor Sectie 10 Verbouwingsmogelijkheden."""
    v = await _cached_fetch_verbouwing(
        lat, lon, rd_x, rd_y, bag_pand_id,
        gemeentecode=gemeentecode, gemeente_naam=gemeente_naam,
        eigen_vbo_id=eigen_vbo_id,
    )
    return _build_verbouwing(v, huisnummertoevoeging=huisnummertoevoeging)


def _build_verbouwing(
    v: Optional[verbouwing.VerbouwingsInfo],
    huisnummertoevoeging: Optional[str] = None,
) -> dict:
    """Serialize VerbouwingsInfo naar JSON-response voor Sectie 10."""
    if v is None:
        return {"available": False}
    out: dict = {"available": True, "pending": False}
    if v.perceel is not None:
        out["perceel"] = {
            "perceelnummer": v.perceel.perceelnummer,
            "gemeente_code": v.perceel.gemeente_code,
            "oppervlakte_m2": v.perceel.oppervlakte_m2,
        }
    if v.pand_op_perceel_m2 is not None:
        out["pand_op_perceel_m2"] = v.pand_op_perceel_m2
    if v.pand_totaal_m2 is not None:
        out["pand_totaal_m2"] = v.pand_totaal_m2
    out["woning_type_hint"] = v.woning_type_hint
    if v.achtererf is not None:
        out["achtererf"] = {
            "onbebouwd_m2": v.achtererf.onbebouwd_m2,
            "onbebouwd_pct": v.achtererf.onbebouwd_pct,
            "achtererf_m2": v.achtererf.achtererf_m2,
            "uitbouw_diepte_max_m": v.achtererf.uitbouw_diepte_max_m,
        }
    if v.beschermd_gezicht is not None:
        out["beschermd_gezicht"] = {
            "naam": v.beschermd_gezicht.naam,
            "status": v.beschermd_gezicht.status,
        }
    if v.gem_monument is not None:
        out["gem_monument"] = {
            "checked": v.gem_monument.checked,
            "is_monument": v.gem_monument.is_monument,
            "status": v.gem_monument.status,
            "naam": v.gem_monument.naam,
            "deeplink": v.gem_monument.deeplink,
        }
    # 3D BAG pand-hoogte
    if v.pand_hoogte is not None and v.pand_hoogte.nokhoogte_m is not None:
        ph = v.pand_hoogte
        out["pand_hoogte"] = {
            "bouwlagen": ph.bouwlagen,
            "nokhoogte_m": round(ph.nokhoogte_m, 1) if ph.nokhoogte_m else None,
            "goothoogte_m": round(ph.goothoogte_m, 1) if ph.goothoogte_m else None,
            "daktype": ph.daktype,
        }
    # Stapeling-info (BAG-VBO's per pand) — kernsignaal voor verdieping
    if v.stapeling:
        out["stapeling"] = {
            "is_gestapeld": v.stapeling.is_gestapeld,
            "aantal_wonen": v.stapeling.aantal_wonen,
            "eigen_verdieping": v.stapeling.eigen_verdieping,
            "totaal_etages": v.stapeling.totaal_etages,
            "is_bovenste": v.stapeling.is_bovenste,
        }
    # Publiekrechtelijke beperkingen (monument-status landelijk dekkend)
    if v.wkpb_beperkingen:
        out["wkpb"] = [
            {
                "grondslag_code": b.grondslag_code,
                "monument_type": b.monument_type,
                "datum_in_werking": b.datum_in_werking,
            }
            for b in v.wkpb_beperkingen
        ]
    # Bijgebouwen (andere BAG-panden op hetzelfde perceel — schuren, aanbouwen)
    if v.bijgebouwen:
        out["bijgebouwen"] = [
            {
                "pand_id": b.pand_id,
                "oppervlakte_m2": b.oppervlakte_m2,
                "totale_pand_m2": b.totale_pand_m2,
                "bouwjaar": b.bouwjaar,
            }
            for b in v.bijgebouwen
        ]
    # BP-regels uit Haiku-extractie
    if v.bp_regels is not None:
        out["bp_regels"] = {
            "max_bouwhoogte_m": v.bp_regels.max_bouwhoogte_m,
            "max_goothoogte_m": v.bp_regels.max_goothoogte_m,
            "max_bouwlagen": v.bp_regels.max_bouwlagen,
            "kap_verplicht": v.bp_regels.kap_verplicht,
            "plat_dak_toegestaan": v.bp_regels.plat_dak_toegestaan,
            "bestemming": v.bp_regels.bestemming,
            "toelichting": v.bp_regels.toelichting,
        }
    # DSO omgevingsdata — alleen als key gezet (anders None).
    if v.omgevingsdata is not None and v.omgevingsdata.omgevingsplan is not None:
        op = v.omgevingsdata.omgevingsplan
        out["omgevingsplan"] = {
            "naam": _vertaal_omgevingsplan_naam(op.officiele_titel),
            "officiele_titel": op.officiele_titel,  # behouden voor debug
            "uri": op.uri_identificatie,
            "bevoegd_gezag": op.bevoegd_gezag_code,
            "aantal_activiteiten": len(v.omgevingsdata.activiteiten),
            "aantal_regelteksten": v.omgevingsdata.aantal_regelteksten,
            "overige_regelingen": len(v.omgevingsdata.overige_regelingen),
        }
    if v.ruimtelijkeplannen_url:
        out["ruimtelijkeplannen_url"] = v.ruimtelijkeplannen_url
    if v.omgevingsloket_url:
        out["omgevingsloket_url"] = v.omgevingsloket_url
    # Fase 2: beslisboom — 4 cards met concrete mogelijkheden.
    out["mogelijkheden"] = _build_mogelijkheden(
        v, huisnummertoevoeging=huisnummertoevoeging
    )
    return out


def _build_uitbouw_criteria(
    v: verbouwing.VerbouwingsInfo, ach
) -> list[dict]:
    """Bouw de Bbl-criteria-checklist voor 'uitbouw achter' vergunningvrij.

    Bbl art. 2.29 stelt 8 voorwaarden. 7 kunnen we zelf verifiëren met
    beschikbare data (monument, achtererf, woonfunctie, oppervlakte-cap).
    1 moet user zelf bevestigen (geen bestaande andere aanbouw).

    Elke criterium: {label, status ('pass'/'fail'/'unknown'), detail}.
    """
    crits: list[dict] = []

    # Monument-check (al gedaan in voorafgaande hoofd-flow, maar expliciet tonen)
    is_rijks = (v.gem_monument is not None and v.gem_monument.checked
                and v.gem_monument.is_monument
                and (v.gem_monument.status or "").lower().find("rijks") >= 0)
    is_gem_mon = (v.gem_monument is not None and v.gem_monument.checked
                  and v.gem_monument.is_monument)
    beschermd = v.beschermd_gezicht is not None

    # Wkpb-check: landelijk dekkend voor zowel rijks- als gemeentelijke monumenten.
    wkpb_rijks = wkpb.is_rijksmonument(v.wkpb_beperkingen)
    wkpb_gem = wkpb.is_gemeentelijk_monument(v.wkpb_beperkingen)
    # Combineer met bestaande RCE-rijksmonument-check (dubbele verificatie)
    is_rijks_all = is_rijks or wkpb_rijks
    is_gem_mon_all = is_gem_mon or wkpb_gem

    crits.append({
        "label": "Geen rijksmonument",
        "status": "fail" if is_rijks_all else "pass",
        "detail": "Niet in het landelijk monumentenregister"
            if not is_rijks_all else "Monumentenvergunning verplicht",
    })
    crits.append({
        "label": "Geen gemeentelijk monument",
        "status": "fail" if (is_gem_mon_all and not is_rijks_all) else "pass",
        "detail": "Gecontroleerd via Kadaster Wkpb-register (landelijk dekkend)"
            if not is_gem_mon_all
            else "Staat in gemeentelijk monumentenregister",
    })
    crits.append({
        "label": "Niet in beschermd stadsgezicht",
        "status": "fail" if beschermd else "pass",
        "detail": ("Binnen " + v.beschermd_gezicht.naam) if beschermd else "Geen bijzondere gebiedsbescherming",
    })

    # Achtertuin + oppervlaktestaffel
    if ach and ach.achtererf_m2 and ach.achtererf_m2 > 10:
        crits.append({
            "label": "Achtertuin aanwezig",
            "status": "pass",
            "detail": f"{ach.achtererf_m2} m² tuin achter het huis",
        })
        max_bouw, uitleg = _bbl_max_bijbouw(ach.achtererf_m2)
        # Bestaande bijgebouwen tellen mee — netto beschikbaar is wat overblijft
        reeds = sum(b.oppervlakte_m2 for b in v.bijgebouwen)
        if reeds > 0:
            netto = max(0, round(max_bouw - reeds, 1))
            crits.append({
                "label": "Oppervlakte-limiet aanbouw",
                "status": "pass" if netto >= 10 else "fail",
                "detail": f"max {max_bouw} m² totaal − {reeds} m² bestaand = nog {netto} m² beschikbaar · {uitleg}",
            })
        else:
            crits.append({
                "label": "Oppervlakte-limiet aanbouw",
                "status": "pass",
                "detail": f"max {max_bouw} m² aanbouwen en bijgebouwen samen · {uitleg}",
            })
    else:
        crits.append({
            "label": "Achtertuin aanwezig",
            "status": "fail",
            "detail": "Onvoldoende open terrein achter het huis",
        })

    # Locatie: achter de woning (niet aan voorkant of openbare zijgevel)
    if ach and ach.uitbouw_diepte_max_m and ach.uitbouw_diepte_max_m > 0:
        crits.append({
            "label": "Bouwlocatie achter de woning",
            "status": "pass",
            "detail": "Automatisch bepaald via voor-/achterkant-analyse",
        })

    # Woonfunctie
    crits.append({
        "label": "Het is een woning",
        "status": "pass",
        "detail": "Aanbouw moet functioneel verbonden zijn met de woning",
    })

    # Uitbouw tot 4 m diep
    if ach and ach.uitbouw_diepte_max_m and ach.uitbouw_diepte_max_m >= 4:
        crits.append({
            "label": "Genoeg ruimte voor 4 m diepe uitbouw",
            "status": "pass",
            "detail": f"{ach.uitbouw_diepte_max_m:.1f} m beschikbaar achter de gevel",
        })

    # Plat dak ≤ 3 m (we adviseren dit als default)
    crits.append({
        "label": "Plat dak ≤ 3 m hoog",
        "status": "pass",
        "detail": "Met plat dak van 3 m blijf je binnen de regels",
    })

    # Dakrand ≤ 0,3 m boven 1e verdiepingsvloer — we gaan uit van een
    # normale aanbouw-uitvoering die hieraan voldoet.
    crits.append({
        "label": "Aanbouw-dak past onder 1e verdiepingsvloer",
        "status": "pass",
        "detail": "Standaard uitvoering voldoet automatisch",
    })

    # Bestaande aanbouw/schuur: check of er andere BAG-panden op hetzelfde
    # perceel staan. Als ja → m² telt mee in de Bbl-staffel (al verrekend
    # hierboven onder Oppervlakte-limiet). Status hangt af van de NETTO
    # beschikbare ruimte; alleen als er onvoldoende ruimte overblijft wordt
    # dit een fail. Anders is het een info-regel ("X m² bestaand verrekend").
    if v.bijgebouwen and ach and ach.achtererf_m2:
        reeds = sum(b.oppervlakte_m2 for b in v.bijgebouwen)
        max_bouw_cap, _ = _bbl_max_bijbouw(ach.achtererf_m2)
        netto = max(0, round(max_bouw_cap - reeds, 1))
        namen = ", ".join(f"{b.oppervlakte_m2} m²" for b in v.bijgebouwen[:3])
        if netto >= 10:
            crits.append({
                "label": "Bestaande aanbouw/schuur verrekend",
                "status": "pass",
                "detail": f"{len(v.bijgebouwen)} in BAG ({namen}) · {reeds} m² al verrekend in oppervlakte-limiet, {netto} m² blijft beschikbaar",
            })
        else:
            crits.append({
                "label": "Geen ruimte meer voor uitbouw",
                "status": "fail",
                "detail": f"{reeds} m² aan bijgebouwen verbruikt de Bbl-limiet bijna volledig ({netto} m² over)",
            })
    else:
        crits.append({
            "label": "Geen bestaande aanbouw of schuur",
            "status": "pass",
            "detail": "Geen extra pand op dit perceel geregistreerd · check zelf op kleine tuinhuisjes of carports",
        })

    return crits


def _bbl_max_bijbouw(achtererf_m2: int) -> tuple[float, str]:
    """Bbl art. 2.29 — maximum oppervlakte aanbouwen + bijgebouwen SAMEN.

    Staffel:
      achtererf < 100 m²  → 50 % van het erf
      achtererf 100-300   → 50 m² + 20 % van (erf − 100)
      achtererf > 300     → 90 m² + 10 % van (erf − 300), max 150 m²

    Returns: (max_m², menselijke uitleg van de formule).
    """
    if achtererf_m2 < 100:
        m = round(achtererf_m2 * 0.5, 1)
        return m, f"tuin <100 m² → 50 % bebouwbaar"
    if achtererf_m2 <= 300:
        m = round(50 + 0.2 * (achtererf_m2 - 100), 1)
        return m, f"tuin 100-300 m² → 50 m² plus 20 % van het meerdere"
    m = min(150.0, round(90 + 0.1 * (achtererf_m2 - 300), 1))
    cap = " (bovengrens)" if m >= 150 else ""
    return m, f"tuin >300 m² → 90 m² plus 10 % van het meerdere{cap}"


def _schat_uitbouw_breedte(pand_m2: Optional[int]) -> float:
    """Schat realistische uitbouw-breedte op basis van pand-footprint.

    We nemen aan: pand-rechthoek aspect ratio ~1.3 (huizen zijn iets dieper
    dan breed in NL-straten). Lange kant = √(opp · 1.3), korte = opp / lange.
    De achtergevel-breedte is meestal de KORTE kant. Voor uitbouw pakken we
    ~80% daarvan (erfgrens-marge + praktische bouwkunde).
    """
    if not pand_m2 or pand_m2 < 20:
        return 3.5  # minimum vuistregel
    lang = (pand_m2 * 1.3) ** 0.5
    kort = pand_m2 / lang
    return round(kort * 0.8, 1)


def _vertaal_omgevingsplan_naam(officiele_titel: str) -> str:
    """Vertaal officiële DSO-regelingsnaam naar begrijpelijke tekst voor kopers.

    DSO-titels zoals "Technisch in beheer nemen van de bruidsschat in het
    Omgevingsplan gemeente Hillegom" bevatten juridisch jargon dat
    transitieregels-status aanduidt. Voor een koper volstaat "Omgevingsplan
    gemeente Hillegom". We zoeken naar het "Omgevingsplan ..." deel en
    gebruiken dat; bij ontbreken val terug op origineel.
    """
    if not officiele_titel:
        return "het omgevingsplan"
    t = officiele_titel.strip()
    # Pak alles vanaf "Omgevingsplan" — dat is altijd het kern-concept.
    idx = t.lower().find("omgevingsplan")
    if idx >= 0:
        return "het " + t[idx:].rstrip(".")
    # Fallback voor bestemmingsplannen of overig
    if "bestemmingsplan" in t.lower():
        return t
    return "het geldende omgevingsplan"


def _apply_vergunningcheck(cards: list[dict], vc_per_card: dict) -> list[dict]:
    """Verrijk elke beslisboom-card met Vergunningcheck-meta indien beschikbaar.

    Toont bij de card: aantal officiële activiteiten gevonden + aantal vragen
    dat beantwoord moet worden op Omgevingsloket voor een definitief verdict.
    Verandert de card-level NIET (nog); dat vereist default-antwoorden en
    per-activiteit analyse in een toekomstige uitbreiding.
    """
    for c in cards:
        res = vc_per_card.get(c["key"])
        if res is None:
            continue
        c["vergunningcheck"] = {
            "werkzaamheid_urn": res.werkzaamheid_urn,
            "aantal_activiteiten": res.aantal_activiteiten,
            "aantal_vragen": res.aantal_vragen,
            "bestuurslaag": res.bestuursorgaan_bestuurslaag,
        }
    return cards


def _verdieping_uit_toevoeging(
    toevoeging: Optional[str], bouwlagen: Optional[int]
) -> Optional[tuple[int, bool]]:
    """Pak verdieping uit de huisnummertoevoeging, check of 't de bovenste is.

    Amsterdams/stedelijke patroon:
      "7-1" → 1e verdieping, "7-2" → 2e etc. (numeriek)
      "H", "hs", "hs.", "huis", "bg", "0" → begane grond-etage van gestapeld pand
    Enkele letters als "A", "B" zijn vaak adres-splits, niet etage — die
    behandelen we niet als etage-aanduiding.

    Returns: (verdieping_nr, is_bovenste) of None als we 't niet weten.
    """
    if not toevoeging or bouwlagen is None or bouwlagen < 2:
        return None
    t = str(toevoeging).strip().lower()
    if t in ("h", "hs", "hs.", "huis", "bg", "0"):
        return (0, bouwlagen <= 1)
    if t.isdigit():
        v = int(t)
        if v < 0 or v >= 20:
            return None
        return (v, v >= bouwlagen - 1)
    return None


def _is_etage_toevoeging(toevoeging: Optional[str]) -> bool:
    """True als de toevoeging een etage-aanduiding is (vs. adres-split).

    Onafhankelijk van 3D BAG, want dit signaal is alleen gebaseerd op de
    huisnummer-conventie: "7-H" / "7-1" / "7-2" zijn etages in een gestapeld
    pand, ongeacht of we bouwlagen kennen.
    """
    if not toevoeging:
        return False
    t = str(toevoeging).strip().lower()
    if t in ("h", "hs", "hs.", "huis", "bg"):
        return True
    return t.isdigit()


def _build_mogelijkheden(
    v: verbouwing.VerbouwingsInfo,
    huisnummertoevoeging: Optional[str] = None,
) -> list[dict]:
    """Beslisboom: bepaal per mogelijkheid of het kan, met toelichting.

    Retourneert 4 cards: uitbouw-achter, dakkapel, tuinhuis, optopping.
    Level: 'good' (groen/ja), 'neutral' (oranje/voorwaardelijk), 'warn'
    (rood/nee of vergunning-plicht), 'unknown' (grijs/BP-data ontbreekt).
    """
    cards: list[dict] = []

    beschermd = v.beschermd_gezicht is not None
    # Rijks- en gemeentelijk-monument: combineer RCE/Amsterdam + landelijke
    # Wkpb-check. Die laatste is door heel NL dekkend en vangt o.a. Hillegom
    # gemeentelijke monumenten die voorheen onzichtbaar waren.
    is_rijks_rce = (
        v.gem_monument is not None
        and v.gem_monument.checked
        and v.gem_monument.is_monument
        and (v.gem_monument.status or "").lower().find("rijks") >= 0
    )
    is_gem_mon_city = (
        v.gem_monument is not None
        and v.gem_monument.checked
        and v.gem_monument.is_monument
    )
    is_rijks = is_rijks_rce or wkpb.is_rijksmonument(v.wkpb_beperkingen)
    is_gem_mon = is_gem_mon_city or wkpb.is_gemeentelijk_monument(v.wkpb_beperkingen)
    # Alle monument-statussen in één vlag; dakkapel/optopping/tuinhuis
    # moeten vergunning vragen bij elk monumenttype.
    is_monument = is_rijks or is_gem_mon

    # Appartement-detectie — BAG-aantal-verblijfsobjecten is de DEFINITIEVE
    # autoriteit (Kadaster bron). Ratio-heuristics komen alleen in beeld
    # als die data ontbreekt, want ze produceren false positives op
    # vrijstaande woningen met groot perceel (bv. boerderij-kavel).
    is_appartement = False
    complex_signal = ""
    stap = v.stapeling
    # Ground truth: BAG-stapeling-analyse kent het EXACTE aantal woningen
    # in dit pand. ≥2 = gestapeld = appartement. =1 = geen appartement
    # (ongeacht perceel-grootte of pand/perceel-ratio).
    if stap is not None and stap.aantal_wonen is not None:
        if stap.is_gestapeld or stap.aantal_wonen >= 2:
            is_appartement = True
            complex_signal = (
                f"gestapeld pand ({stap.aantal_wonen} woningen in 1 BAG-pand)"
            )
        # Als stap.aantal_wonen == 1, dan is het GEEN appartement — punt.
        # Geen heuristic meer die dit overruled.
    else:
        # Stapeling-fetch faalde (bv. geen VBO-data) — val terug op
        # zwakkere signalen in deze volgorde van betrouwbaarheid.
        if _is_etage_toevoeging(huisnummertoevoeging):
            is_appartement = True
            complex_signal = "etage-woning (huisnummertoevoeging)"
        elif v.perceel and v.pand_totaal_m2 and v.perceel.oppervlakte_m2:
            ratio_groot = v.pand_totaal_m2 / max(1, v.perceel.oppervlakte_m2)
            if ratio_groot > 3:
                # BAG-pand loopt over meerdere percelen → waarschijnlijk
                # appartementencomplex (of rijwoning, maar die behandelen
                # we hetzelfde qua bouw-beperkingen).
                is_appartement = True
                complex_signal = "BAG-pand loopt over meerdere percelen"
            # Geen "kleine woning op groot perceel"-heuristic meer: die
            # gaf false positives op landelijke vrijstaande woningen
            # (bv. 's-Gravenschanslaan 1 Slochteren: 94 m² pand op 835 m²
            # perceel = 11%, maar is gewoon vrijstaande woning).
    ach = v.achtererf
    onbebouwd_pct = ach.onbebouwd_pct if ach else None
    diepte = ach.uitbouw_diepte_max_m if ach else None

    # 1. Uitbouw achter -----------------------------------------------------
    uitbouw: dict = {"key": "uitbouw", "titel": "Uitbouw achter", "icon": "↔"}
    if is_rijks:
        uitbouw.update(level="warn",
            samenvatting="Vergunning voor rijksmonument nodig",
            detail="Dit is een rijksmonument — voor elke wijziging aan de "
                   "buitenkant heb je een monumentenvergunning nodig en de "
                   "welstandscommissie beoordeelt de uitvoering. Binnen "
                   "verbouwen mag vaak wel zonder vergunning.")
    elif is_gem_mon:
        uitbouw.update(level="warn",
            samenvatting="Vergunning voor gemeentelijk monument nodig",
            detail="Dit is een gemeentelijk monument — een uitbouw is nooit "
                   "vergunningvrij. Altijd een omgevingsvergunning en "
                   "welstandstoets; de gemeente beoordeelt of de uitbouw de "
                   "monumentale waarde aantast.")
    elif beschermd:
        uitbouw.update(level="warn",
            samenvatting="Altijd met vergunning",
            detail="Deze woning ligt in een beschermd stadsgezicht. Voor elke uitbouw "
                   "heb je een vergunning nodig en oordeelt de welstandscommissie "
                   "over materiaal, kleur en vormgeving.")
    elif is_appartement:
        uitbouw.update(level="warn",
            samenvatting="Toestemming VvE nodig",
            detail="Dit is een appartementencomplex. Uitbouwen kan alleen met "
                   "toestemming van de Vereniging van Eigenaren (VvE) en de "
                   "mede-eigenaren. In de praktijk lastig, behalve voor woningen "
                   "op de begane grond.")
    elif diepte is None or diepte <= 0:
        uitbouw.update(level="warn",
            samenvatting="Geen ruimte achter het huis",
            detail="Er is geen of te weinig open terrein achter de woning om "
                   "een uitbouw te plaatsen. Binnen verbouwen kan natuurlijk wel.")
    elif diepte >= 4:
        # Schat praktische aanbouw-grootte: 4 m diep × pand-breedte × 80 %.
        # Landelijke oppervlakte-limiet beperkt totaal aanbouw + bijgebouwen.
        breedte = _schat_uitbouw_breedte(v.pand_op_perceel_m2)
        geometrisch = min(4, diepte) * breedte
        bbl_max, _ = _bbl_max_bijbouw(ach.achtererf_m2) if ach else (geometrisch, "")
        realistisch = round(min(geometrisch, bbl_max), 1)
        uitbouw.update(level="good",
            samenvatting=f"~{realistisch} m² waarschijnlijk zonder vergunning",
            detail=f"Er is ruimte voor een uitbouw van 4 m diep × ~{breedte} m "
                   f"breed = {round(geometrisch, 1)} m². De landelijke regels "
                   f"staan maximaal {bbl_max} m² aanbouw en bijgebouwen samen toe. "
                   f"Met een plat dak van 3 m hoog voldoe je aan de standaardregels."
        )
    else:
        breedte = _schat_uitbouw_breedte(v.pand_op_perceel_m2)
        geometrisch = diepte * breedte
        bbl_max, _ = _bbl_max_bijbouw(ach.achtererf_m2) if ach else (geometrisch, "")
        realistisch = round(min(geometrisch, bbl_max), 1)
        uitbouw.update(level="neutral",
            samenvatting=f"Krap ({diepte} m diep)",
            detail=f"Ruimte voor ~{realistisch} m² ({diepte} m diep × "
                   f"{breedte} m breed), minder dan de gebruikelijke 4 m die "
                   f"zonder vergunning is toegestaan. Met vergunning vaak mogelijk."
        )
    # Criteria-checklist alleen als er realistisch iets vergunningvrij kan
    # (dus geen monument/beschermd/appartement — daar is de conclusie al
    # "altijd vergunning" of "niet zelf te beslissen").
    if not (is_rijks or is_gem_mon or beschermd or is_appartement):
        uitbouw["criteria"] = _build_uitbouw_criteria(v, ach)
    cards.append(uitbouw)

    # 2. Dakkapel -----------------------------------------------------------
    dakkapel: dict = {"key": "dakkapel", "titel": "Dakkapel", "icon": "🪟"}
    # Verdieping-check via BAG-stapeling: we sorteren alle woon-VBO's in het
    # pand en bepalen de positie van deze woning. is_bovenste is definitief,
    # ongeacht welk toevoeging-patroon de gemeente gebruikt (H, 1-3, A-K).
    stap = v.stapeling
    niet_bovenste = (stap is not None and stap.is_gestapeld
                     and stap.is_bovenste is False)
    if niet_bovenste:
        verd = stap.eigen_verdieping
        totaal = stap.totaal_etages
        verd_label = "begane grond" if verd == 0 else f"{verd}e verdieping"
        dakkapel.update(level="warn",
            samenvatting="Niet van toepassing — geen eigen dak",
            detail=f"Jouw woning is de {verd_label} van een gestapeld pand met "
                   f"{totaal} woningen boven elkaar. Een dakkapel vraag je aan "
                   f"op het dak; alleen de bewoner van de bovenste woning kan "
                   f"dit — met VvE-toestemming.")
    elif is_rijks:
        dakkapel.update(level="warn",
            samenvatting="Vergunning voor rijksmonument nodig",
            detail="Bij een rijksmonument heeft elke wijziging aan het dak "
                   "een monumentenvergunning nodig; de welstandscommissie "
                   "beoordeelt het ontwerp streng.")
    elif is_gem_mon:
        dakkapel.update(level="warn",
            samenvatting="Vergunning voor gemeentelijk monument nodig",
            detail="Bij een gemeentelijk monument is een dakkapel nooit "
                   "vergunningvrij — altijd een omgevingsvergunning en "
                   "welstandstoets, ook aan de achterkant.")
    elif beschermd:
        dakkapel.update(level="neutral",
            samenvatting="Altijd met vergunning",
            detail="In een beschermd stadsgezicht heb je altijd een vergunning "
                   "nodig voor een dakkapel. De welstandscommissie beoordeelt "
                   "materiaal, kleur en plaatsing.")
    elif is_appartement:
        dakkapel.update(level="warn",
            samenvatting="Toestemming VvE nodig",
            detail="Dakwijzigingen bij een appartementencomplex mogen alleen "
                   "via de Vereniging van Eigenaren (VvE).")
    else:
        dakkapel.update(level="good",
            samenvatting="Achterkant meestal zonder vergunning",
            detail="Een dakkapel op de achterkant mag zonder vergunning als hij "
                   "hooguit 1,75 m hoog is en voldoende afstand houdt tot dakrand "
                   "en nok. Aan de voorkant heb je altijd een vergunning nodig.")
    cards.append(dakkapel)

    # 3. Tuinhuis -----------------------------------------------------------
    tuinhuis: dict = {"key": "tuinhuis", "titel": "Tuinhuis", "icon": "🏡"}
    # Appartement-eerst: het "onbebouwd m²"-getal slaat dan op het complex-
    # terrein, niet op een eigen tuin. Een VvE-lid heeft geen eigen rechten
    # om een tuinhuis te plaatsen op gedeelde grond.
    if is_appartement:
        tuinhuis.update(level="warn",
            samenvatting="Geen eigen tuin (appartement)",
            detail=f"Het onbebouwd terrein rond dit pand "
                   f"({'; '.join(x for x in [complex_signal] if x)}) is complex-"
                   f"terrein dat bij de VvE of mede-eigenaren hoort — geen eigen "
                   f"tuin om een tuinhuis op te plaatsen.")
    elif is_rijks or is_gem_mon or beschermd:
        tuinhuis.update(level="neutral",
            samenvatting="Altijd met vergunning",
            detail="Bij een monument of in een beschermd stadsgezicht heb je "
                   "altijd een vergunning nodig voor een tuinhuis. Vaak zijn "
                   "alleen traditionele materialen toegestaan.")
    elif (ach is None) or (ach.onbebouwd_m2 < 5):
        tuinhuis.update(level="warn",
            samenvatting="Geen eigen tuin",
            detail="Er is te weinig open terrein om een tuinhuis te plaatsen.")
    elif onbebouwd_pct is not None and onbebouwd_pct >= 50:
        tuinhuis.update(level="good",
            samenvatting="Tot ~30 m² zonder vergunning",
            detail=f"Je hebt {ach.achtererf_m2} m² tuin achter het huis "
                   f"({onbebouwd_pct}% van je grond is onbebouwd). Je mag er tot "
                   f"ongeveer 30 m² aan tuinhuizen of schuren bouwen zonder "
                   f"vergunning — maximaal 3 m hoog en niet aan de voorkant."
        )
    elif onbebouwd_pct is not None and onbebouwd_pct >= 25:
        tuinhuis.update(level="neutral",
            samenvatting="Tot ~4 m² zonder vergunning",
            detail=f"Beperkte achtertuin van {ach.achtererf_m2} m². Kleine "
                   f"bijgebouwen tot ~4 m² mogen zonder vergunning; groter "
                   f"altijd met vergunning.")
    else:
        tuinhuis.update(level="warn",
            samenvatting="Tuin bijna volgebouwd",
            detail="Te weinig open ruimte voor een tuinhuis. Aan de voorkant "
                   "mag sowieso geen tuinhuis staan.")
    cards.append(tuinhuis)

    # 4. Zonnepanelen ------------------------------------------------------
    # Schatting o.b.v. pand-footprint, dak-type (3D BAG), oriëntatie
    # (Shapely PCA op pand-polygoon) en monument-/appartement-vlaggen.
    # Ranges (geen puntwaarde) want schaduw is altijd onbekende factor.
    zonne: dict = {"key": "zonnepanelen", "titel": "Zonnepanelen", "icon": "☀️"}
    daktype = v.pand_hoogte.daktype if v.pand_hoogte else None
    schatting = zonnepanelen.schat_zonnepanelen(
        pand_op_perceel_poly=v.pand_op_perceel_poly,
        daktype=daktype,
        is_rijksmonument=is_rijks,
        is_gem_monument=is_gem_mon,
        is_beschermd_gezicht=beschermd,
        is_appartement=is_appartement,
    )
    if schatting is None:
        zonne.update(level="unknown",
            samenvatting="Onbekend — geen pand-geometrie",
            detail="Zonder pand-polygoon van BAG kunnen we geen "
                   "dakoppervlak schatten. Probeer het exacte huisnummer.")
    else:
        zonne.update(
            level=zonnepanelen.card_level(schatting),
            samenvatting=zonnepanelen.card_samenvatting(schatting),
            detail=zonnepanelen.card_detail(schatting),
        )
        # Structured velden voor UI-grafieken/badges (frontend negeert ze als
        # er nu nog niets mee gedaan wordt).
        if schatting.aantal_panelen_max > 0:
            zonne["schatting"] = {
                "panelen_min": schatting.aantal_panelen_min,
                "panelen_max": schatting.aantal_panelen_max,
                "kwh_jaar_min": schatting.kwh_per_jaar_min,
                "kwh_jaar_max": schatting.kwh_per_jaar_max,
                "pct_huishoudverbruik_min": schatting.pct_huishoudverbruik_min,
                "pct_huishoudverbruik_max": schatting.pct_huishoudverbruik_max,
                "config": schatting.config_beschrijving,
            }
    cards.append(zonne)

    # NB: Optopping-card was eerder de 4e card — verwijderd omdat max
    # bouwhoogte via open data niet beschikbaar is sinds bruidsschat
    # 1-1-2024 (DSO regelteksten = 0/1365 getallen voor Amsterdam, RP v4
    # 5% coverage). Vervangen door Zonnepanelen — die we wél kunnen
    # onderbouwen met footprint + dak-type + monument-status.

    # Verrijk cards met Vergunningcheck-resultaten (indien beschikbaar)
    cards = _apply_vergunningcheck(cards, v.vergunningcheck_per_card)

    return cards


async def _cached_fetch_bereikbaarheid(
    lat: float, lon: float
) -> Optional[bereikbaarheid.Bereikbaarheid]:
    """Bereikbaarheid via Overpass (OV-halten + route-relations).

    Dezelfde cachestrategie als andere Overpass-calls: coord-100m grid,
    7 dagen TTL (halten verhuizen zelden, routes veranderen af en toe).
    """
    if not (lat and lon):
        return None
    # v2: OV-reistijd-formule gekalibreerd tegen 9292 (was systematisch te krap)
    key = f"bereik:v2:{round(lat * 1000)}_{round(lon * 1000)}"
    hit = _cache_get(key, _OSM_TTL_S)
    if isinstance(hit, bereikbaarheid.Bereikbaarheid):
        return hit
    try:
        result = await bereikbaarheid.fetch_bereikbaarheid(lat, lon)
    except Exception:
        return None
    if result is not None:
        _cache_set(key, result)
    return result


async def _cached_fetch_overpass(lat: float, lon: float) -> list[overpass.POI]:
    """OSM POI's rond (lat, lon) via Overpass API, met cache.

    Cache-key gebruikt coord-100m rounding — POI's binnen hetzelfde 100m-grid
    delen dezelfde POI-lijst (kleine afwijkingen in afstand zijn acceptabel).
    """
    if not (lat and lon):
        return []
    # 100m rounding via lat*1000 (≈111m) / lon*1000 (iets korter in NL)
    # v2: na Overpass retry+fallback fix. Oude key (v1) had stille 0-POI
    # resultaten door rate-limit; die cachen we niet meer.
    key = f"osm:v2:{round(lat * 1000)}_{round(lon * 1000)}"
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
    woz_adres: Optional[woz_loket.WozWaarde] = None,
    extras: Optional[woning_extras.WoningExtras] = None,
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
    # WOZ-per-pand uit WOZ-Waardeloket. Komt nu in de eerste /scan-response
    # (vroeger lazy via /woz endpoint en frontend-injectie). Frontend toont
    # deze boven het buurt-gemiddelde uit CBS — pand-niveau is preciezer.
    if woz_adres is not None and woz_adres.huidige_waarde_eur:
        out["woz_adres"] = {
            "value": woz_adres.huidige_waarde_eur,
            "unit": "€",
            "peildatum": woz_adres.huidige_peildatum,
            "trend_pct_per_jaar": woz_adres.trend_pct_per_jaar,
            "historie": woz_adres.historie,
            "ref": _as_ref(references.ref_woz(woz_adres.huidige_waarde_eur)),
        }

    # Woning-extras: Rijksmonument (RCE WFS) + Groen (Overpass, 500-1500ms
    # cold) worden lazy geladen via /woning-extras endpoint. Hier alleen een
    # pending-flag zodat de frontend weet dat er nog een patch komt.
    if extras is None:
        out["extras_pending"] = True
    else:
        out["extras_pending"] = False
        if extras.rijksmonument is not None:
            rm = extras.rijksmonument
            out["rijksmonument"] = {
                "monument_nummer": rm.monument_nummer,
                "hoofdcategorie": rm.hoofdcategorie,
                "subcategorie": rm.subcategorie,
                "aard_monument": rm.aard_monument,
                "url": rm.url,
            }
        if extras.groen is not None:
            g = extras.groen
            out["groen"] = {
                "straal_m": g.straal_m,
                "groen_m2": g.groen_m2,
                "cirkel_m2": g.cirkel_m2,
                "groen_pct": g.groen_pct,
                "aantal_elementen": g.aantal_elementen,
            }
    return out


async def fetch_woning_extras_section(
    lat: float, lon: float, rd_x: float, rd_y: float,
    gemeentecode: Optional[str],
) -> dict:
    """Los endpoint: Rijksmonument-check + Groen in straat.

    Wordt door /woning-extras in main.py aangeroepen — niet door scan().
    RCE WFS ~200-500ms, Overpass-groen ~500-1500ms cold. Samen too traag
    voor de main /scan. Returnt alleen de velden die de frontend moet
    bijpatchen in de woning-sectie.
    """
    extras = await _cached_fetch_woning_extras(lat, lon, rd_x, rd_y, gemeentecode)
    if extras is None:
        return {"available": False}
    out: dict = {"available": True}
    if extras.rijksmonument is not None:
        rm = extras.rijksmonument
        out["rijksmonument"] = {
            "monument_nummer": rm.monument_nummer,
            "hoofdcategorie": rm.hoofdcategorie,
            "subcategorie": rm.subcategorie,
            "aard_monument": rm.aard_monument,
            "url": rm.url,
        }
    if extras.groen is not None:
        g = extras.groen
        out["groen"] = {
            "straal_m": g.straal_m,
            "groen_m2": g.groen_m2,
            "cirkel_m2": g.cirkel_m2,
            "groen_pct": g.groen_pct,
            "aantal_elementen": g.aantal_elementen,
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
                f"'{min_dim.label}' scoort veel lager ({min_dim.score}/9). "
                f"Sterkere dimensies compenseren dat in de totaalscore."
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

    # Compacte menselijke beschrijving — lijnt consistent met het label/chip.
    # Leefbaarometer klasse 4-5-6 = 'stabiel' (chip=STABIEL); klasse 3 of 7
    # zijn de eerste echte klassen buiten stabiel. We gebruiken DEZELFDE
    # grensen zodat chip en tekst nooit tegenstrijdig zijn (anders krijg je
    # bv. "STABIEL" + "Licht verbeterd over 2 jaar" zoals vóór deze fix).
    if o.score <= 2:
        beschrijving = f"Sterk verslechterd over {horizon_label}."
    elif o.score == 3:
        beschrijving = f"Licht verslechterd over {horizon_label}."
    elif 4 <= o.score <= 6:
        beschrijving = f"Stabiel over {horizon_label}."
    elif o.score == 7:
        beschrijving = f"Licht verbeterd over {horizon_label}."
    else:  # 8-9
        beschrijving = f"Sterk verbeterd over {horizon_label}."

    # Binnen de stabiele range (4-6) kan er onderliggend wél beweging zijn.
    # Dan maken we dat expliciet zodat de chip STABIEL niet in tegenspraak
    # oogt met een dimensie die bv. licht verslechterd is.
    if 4 <= o.score <= 6 and veranderingen:
        beschrijving = (
            f"Totaal stabiel over {horizon_label}, maar onder de motorkap "
            f"wél beweging:"
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


def _build_bereikbaarheid(
    b: Optional["bereikbaarheid.Bereikbaarheid"],  # type: ignore[name-defined]
) -> dict:
    """Sectie 8 — OV + auto-ontsluiting.

    OSM-data voor NL OV-routes is niet 100% compleet; lijn-aantallen zijn
    een onderkant (kan meer zijn in werkelijkheid). Dat vermelden we in
    de UI via de hint.
    """
    if b is None:
        return {"available": False}

    def _halte_to_dict(h) -> Optional[dict]:
        if h is None:
            return None
        out = {
            "naam": h.naam,
            "type": h.type,
            "meters": h.meters,
            "lijnen": h.lijnen or [],
            "aantal_lijnen": len(h.lijnen or []),
        }
        # Voor treinen: rijkere info i.p.v. interne NS-trajectnummers
        if h.type == "trein":
            out["bestemmingen"] = h.bestemmingen or []
            out["aantal_ic"] = h.aantal_ic
            out["aantal_sprinter"] = h.aantal_sprinter
        return out

    # Heeft deze locatie überhaupt OV-ontsluiting?
    has_ov = any([b.trein, b.metro, b.tram, b.bus])
    werkcentra = [
        {"stad": w.stad, "station": w.station, "km": w.km, "ov_min": w.ov_min}
        for w in (b.werkcentra or [])
    ]
    return {
        "available": has_ov or bool(b.snelweg_oprit_meters) or bool(werkcentra),
        "trein": _halte_to_dict(b.trein),
        "metro": _halte_to_dict(b.metro),
        "tram": _halte_to_dict(b.tram),
        "bus": _halte_to_dict(b.bus),
        "snelweg": {
            "meters": b.snelweg_oprit_meters,
            "naam": b.snelweg_oprit_naam,
        } if b.snelweg_oprit_meters else None,
        "werkcentra": werkcentra,
    }


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
        {
            "section": "bereikbaarheid",
            "source": "OpenStreetMap route-relations (Overpass) · OV-reistijd schatting uit afstand (geen routing-API)",
            "peildatum": "OSM realtime",
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
