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
from adapters import bag, cbs, kadaster_woz, klimaat, leefbaarometer, pdok_locatie, politie, rivm_geluid, rivm_lki, rvo_ep, verkiezingen


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
    cbs_task = _cached_fetch_cbs(match.buurtcode or "")
    pand, buurt = await asyncio.gather(bag_task, cbs_task)

    # Stap 2b: Politie + uitgebreide voorzieningen (parallel).
    # Politie hangt af van inwoners uit CBS; voorzieningen is onafhankelijk.
    inwoners = buurt.inwoners if buurt else None
    politie_task = _cached_fetch_politie(match.buurtcode or "", inwoners)
    voorz_task = _cached_fetch_voorzieningen(
        match.buurtcode or "", match.gemeentecode or ""
    )
    woz_trend_task = _cached_fetch_woz_trend(match.buurtcode or "")
    misdrijven, voorzieningen_lijst, woz_trend = await asyncio.gather(
        politie_task, voorz_task, woz_trend_task
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

        buren=_build_buren(buurt, tk_uitslag),
        voorzieningen=_build_voorzieningen(voorzieningen_lijst),
        veiligheid=_build_veiligheid(misdrijven),
        leefkwaliteit=_build_leefkwaliteit(lucht, geluid),
        klimaat=_build_klimaat(klimaatrisico),
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


async def _cached_fetch_cbs(buurtcode: str) -> Optional[cbs.BuurtStats]:
    if not buurtcode:
        return None
    key = f"cbs:{buurtcode}"
    hit = _cache_get(key, _BUURT_TTL_S)
    if isinstance(hit, cbs.BuurtStats):
        return hit
    result = await cbs.fetch_buurt(buurtcode)
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

    return {
        "available": True,
        "woz": {
            "value": woz_eur,
            "unit": "€",
            "ref": _as_ref(references.ref_woz(woz_eur)),
            "trend_pct_per_jaar": trend_pct,
            "trend_series": woz_trend,
        },
        "inkomen_per_inwoner": {
            "value": inkomen_eur,
            "unit": "€",
            "ref": _as_ref(references.ref_inkomen(inkomen_eur)),
        },
        "arbeidsparticipatie": {
            "value": buurt.arbeidsparticipatie_pct,
            "unit": "%",
            "ref": _as_ref(references.ref_arbeidsparticipatie(buurt.arbeidsparticipatie_pct)),
        },
        "opleiding_hoog": {
            "value": opl_hoog_pct,
            "unit": "%",
            "ref": _as_ref(references.ref_opleiding_hoog(opl_hoog_pct)),
            "breakdown": {
                "laag_pct": opl_laag_pct,
                "midden_pct": opl_midden_pct,
                "hoog_pct": opl_hoog_pct,
            },
        },
    }


def _build_buren(
    buurt: Optional[cbs.BuurtStats],
    verkiezing: Optional[verkiezingen.VerkiezingsUitslag],
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
    out = {
        "available": True,
        "eenpersoons": {
            "value": pct_eenpersoons,
            "unit": "%",
            "ref": _as_ref(references.ref_eenpersoons(pct_eenpersoons)),
        },
        "met_kinderen": {
            "value": pct_met_kinderen,
            "unit": "%",
            "ref": _as_ref(references.ref_met_kinderen(pct_met_kinderen)),
        },
        "inwoners": {
            "value": buurt.inwoners,
            "unit": None,
            "ref": _as_ref(references.ref_inwoners(buurt.inwoners)),
        },
        "dichtheid": {
            "value": buurt.bevolkingsdichtheid_per_km2,
            "unit": "per km²",
            "ref": _as_ref(references.ref_dichtheid(buurt.bevolkingsdichtheid_per_km2)),
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


def _build_voorzieningen(lijst: list[dict]) -> dict:
    """Voorzieningen-lijst gesorteerd van dichtbij naar ver.

    Verving de ringen-visualisatie — een lijst met 20+ items is voor de
    gebruiker veel informatiever dan 4 iconen op concentrische cirkels.
    """
    if not lijst:
        return {"available": False}
    # Pretty labels voor de frontend (snake_case -> Menselijke naam)
    labels = {
        "supermarkt": "Supermarkt",
        "dagelijkse_levensmiddelen": "Buurtsuper / dagwinkel",
        "huisarts": "Huisarts",
        "huisartsenpost": "Huisartsenpost",
        "apotheek": "Apotheek",
        "fysiotherapeut": "Fysiotherapeut",
        "ziekenhuis": "Ziekenhuis",
        "basisschool": "Basisschool",
        "kinderdagverblijf": "Kinderdagverblijf",
        "buitenschoolse_opvang": "Buitenschoolse opvang",
        "restaurant": "Restaurant",
        "cafe": "Café",
        "cafetaria": "Cafetaria",
        "hotel": "Hotel",
        "park": "Park",
        "bos": "Bos",
        "sportterrein": "Sportterrein",
        "zwembad": "Zwembad",
        "treinstation": "Treinstation",
        "overstapstation": "Intercity-station",
        "oprit_snelweg": "Oprit snelweg",
        "bibliotheek": "Bibliotheek",
        "museum": "Museum",
        "bioscoop": "Bioscoop",
    }
    items = []
    for v in lijst:
        items.append({
            "type": v["type"],
            "label": labels.get(v["type"], v["type"].replace("_", " ").title()),
            "emoji": v.get("emoji", "•"),
            "km": v["km"],
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
    if k is None:
        return {"available": False}
    paalrot_pct = k.paalrot_pct_sterk_risico if k.paalrot_pct_sterk_risico is not None else k.paalrot_pct_mild_risico
    out = {
        "available": True,
        "paalrot": {
            "value": paalrot_pct,
            "unit": "%",
            "buurt": k.paalrot_buurtnaam,
            "aantal_panden": k.paalrot_aantal_panden_in_buurt,
            "pct_sterk": k.paalrot_pct_sterk_risico,
            "pct_mild": k.paalrot_pct_mild_risico,
            "ref": _as_ref(references.ref_paalrot(k.paalrot_pct_sterk_risico, k.paalrot_pct_mild_risico)),
        },
        "hittestress": {
            "value": k.hittestress_klasse,
            "label": k.hittestress_label,
            "ref": _as_ref(references.ref_hittestress(k.hittestress_klasse)),
        },
    }
    if k.waterdiepte_cm and k.waterdiepte_cm > 0:
        out["waterdiepte_cm"] = k.waterdiepte_cm
    return out


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
    ]


def result_as_dict(r: ScanResult) -> dict:
    return asdict(r)
