"""
Klimaateffectatlas adapter — bodem-aware klimaatrisico's.

Input  : RD- of WGS84-coordinaten
Output : lijst relevante risico's gegeven het bodemtype

Kernidee: klimaatrisico's zijn zwaar locatie-afhankelijk. Paalrot is
alleen relevant in veenweide-gebieden; verschilzetting speelt vooral op
zand. Overstroming is essentieel in rivier-/kustgebieden. Hittestress is
overal relevant.

We fetchen alle risico-layers PARALLEL (de ArcGIS-calls zijn snel
individueel), maar tonen in de UI alleen het relevante subset op basis
van de bodem-typologie uit de Klimaateffectatlas Basiskaart.

Bodem-typologie (gridcode -> beschrijving):
    1  Strandwallen en binnenduinrand
    2  Zeekleipolders
    3  Laagveen                         <- paalrot-gebied
    4  Droogmakerijen en IJsselmeerpolders
    5  Rivierengebied                   <- overstromingsgebied
    6  Rivierterrassen                  <- zand, verschilzetting
    7  Stuwwallen
    8  Keileemgebied
    9  Dekzandgebied                    <- zand, droogte
    10 Voormalige hoogvenen
    11 Heuvelland en lossgebied
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

import httpx

# FeatureServer base (polygon-per-buurt met %-panden velden)
ARCGIS_FS_BASE = (
    "https://services.arcgis.com/nSZVuSZjHpEZZbRo/arcgis/rest/services"
)
# ImageServer base (raster met pixel-waarden per risico-klasse)
ARCGIS_IS_BASE = "https://image.arcgisonline.nl/arcgis/rest/services/KEA"

# Bodemtype (essentieel voor relevance-logic)
BODEMTYPE_URL = f"{ARCGIS_FS_BASE}/Klimaateffectatlas_Basiskaart_natuurlijk_systeem_hoofdtypen/FeatureServer/0"

# Funderingsrisico — 2 varianten; beiden %-panden per buurt
PAALROT_URL = f"{ARCGIS_FS_BASE}/Klimaateffectatlas_Risico_paalrot/FeatureServer/3"
VERSCHILZETTING_URL = f"{ARCGIS_FS_BASE}/Klimaateffectatlas_Risico_verschilzetting/FeatureServer/2"

# Raster-lagen (ImageServer identify)
HITTE_URL = f"{ARCGIS_IS_BASE}/Hittestress_door_warme_nachten_huidig/ImageServer"
WATEROVERLAST_URL = f"{ARCGIS_IS_BASE}/Waterdiepte_bij_intense_neerslag/ImageServer"
OVERSTROMING_KANS_URL = f"{ARCGIS_IS_BASE}/Plaatsgebonden_overstromingskans/ImageServer"
OVERSTROMING_DIEPTE_URL = f"{ARCGIS_IS_BASE}/Maximale_overstromingsdiepte/ImageServer"
DROOGTE_URL = f"{ARCGIS_IS_BASE}/Risico_droogtestress/ImageServer"
BODEMDALING_URL = f"{ARCGIS_IS_BASE}/Bodemdaling/ImageServer"

TIMEOUT_S = 8.0

# Hittestress labels voor legacy backwards-compat
HITTE_LABELS = {
    1: "zeer laag",
    2: "laag",
    3: "middel",
    4: "hoog",
    5: "zeer hoog",
}

# Bodemtype-beschrijvingen en welke risico's er typisch spelen.
# Wordt gebruikt in orchestrator om irrelevante cijfers weg te filteren.
BODEMTYPE_LABELS = {
    1:  "Strandwallen / binnenduinrand",
    2:  "Zeekleipolders",
    3:  "Laagveen",
    4:  "Droogmakerijen / IJsselmeerpolders",
    5:  "Rivierengebied",
    6:  "Rivierterrassen",
    7:  "Stuwwallen",
    8:  "Keileemgebied",
    9:  "Dekzandgebied",
    10: "Voormalige hoogvenen",
    11: "Heuvelland / lossgebied",
}

# Per bodemtype welke risico-keys structureel relevant zijn.
# Hittestress + overstromingskans zijn UNIVERSEEL (altijd checken).
# Paalrot & bodemdaling: alleen in veen-gerelateerde gronden.
# Verschilzetting: zand/klei/leem.
# Droogte: zand/löss/rivierterrassen (zanderige bodems).
RELEVANT_RISKS_PER_BODEM = {
    1:  {"verschilzetting", "droogte"},
    2:  {"paalrot", "verschilzetting"},                 # oude kleipolder-bebouwing
    3:  {"paalrot", "bodemdaling"},                     # klassiek veen-gebied
    4:  {"paalrot", "bodemdaling", "verschilzetting"},
    5:  {"verschilzetting", "paalrot"},                 # klei-op-rivierzand
    6:  {"verschilzetting", "droogte"},                 # zanderige rivierterras (Grave!)
    7:  {"droogte"},
    8:  {"verschilzetting", "droogte"},
    9:  {"verschilzetting", "droogte"},
    10: {"paalrot", "bodemdaling", "droogte"},
    11: {"verschilzetting", "droogte"},
}
# Universeel — altijd tonen (ook buiten boven-lijst)
UNIVERSAL_RISKS = {"hittestress", "overstroming", "wateroverlast"}


@dataclass
class Risico:
    """Eén individueel klimaatrisico met score + rauwe waarden."""
    key: str                      # 'paalrot' / 'verschilzetting' / 'overstroming' / ...
    label: str                    # menselijk label voor UI
    relevant: bool                # hoort dit risico bij dit bodemtype?
    klasse: Optional[int] = None  # 1-5 schaal waar van toepassing
    pct: Optional[float] = None   # percentage waar van toepassing
    waarde: Optional[float] = None  # rauwe metric (cm, mm/jr, etc.)
    eenheid: Optional[str] = None
    aantal_panden: Optional[int] = None  # voor buurt-gebaseerde risico's
    buurtnaam: Optional[str] = None
    # Betekenis en chip_level worden in orchestrator toegevoegd op basis
    # van references.py — houd deze adapter puur.


@dataclass
class Klimaatrisico:
    """Samengesteld klimaat-profiel van een adres.

    Stap 1: bodemtype (Rivierterrassen, Laagveen, ...).
    Stap 2: alle potentiële risico's parallel ophalen.
    Stap 3: per risico markeren of 't structureel relevant is (via
            RELEVANT_RISKS_PER_BODEM). De orchestrator kiest wat te tonen.
    """

    bodemtype_code: Optional[int]
    bodemtype_label: Optional[str]
    risicos: list[Risico] = field(default_factory=list)


async def fetch_klimaat(
    lat: float, lon: float, rd_x: float, rd_y: float
) -> Klimaatrisico:
    """Haal alle klimaatrisico-layers parallel op + bodemtype.

    FeatureServers (bodemtype, paalrot, verschilzetting) accepteren WGS84.
    ImageServers (hittestress, overstroming, droogte, bodemdaling) zijn
    native EPSG:28992 en werken het betrouwbaarst met RD-coordinaten.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        tasks = {
            "bodem":        _fetch_bodemtype(client, lat, lon),
            "paalrot":      _fetch_fs_panden(client, PAALROT_URL, lat, lon),
            "verschilzet":  _fetch_fs_panden(client, VERSCHILZETTING_URL, lat, lon),
            "hittestress":  _fetch_image_value_rd(client, HITTE_URL, rd_x, rd_y),
            "wateroverlast":_fetch_image_value_rd(client, WATEROVERLAST_URL, rd_x, rd_y),
            "oversr_kans":  _fetch_image_value_rd(client, OVERSTROMING_KANS_URL, rd_x, rd_y),
            "oversr_diepte":_fetch_image_value_rd(client, OVERSTROMING_DIEPTE_URL, rd_x, rd_y),
            "droogte":      _fetch_image_value_rd(client, DROOGTE_URL, rd_x, rd_y),
            "bodemdaling":  _fetch_image_value_rd(client, BODEMDALING_URL, rd_x, rd_y),
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        res = dict(zip(tasks.keys(), results))

    # Bodemtype: None als niet gevonden
    bodem_code = None
    if isinstance(res["bodem"], int):
        bodem_code = res["bodem"]
    bodem_label = BODEMTYPE_LABELS.get(bodem_code) if bodem_code else None
    relevant_keys = RELEVANT_RISKS_PER_BODEM.get(bodem_code or 0, set()) | UNIVERSAL_RISKS

    risicos: list[Risico] = []

    # --- Funderingsrisico (paalrot + verschilzetting) ---
    # NB: de ArcGIS-services `Klimaateffectatlas_Risico_paalrot` en
    # `Klimaateffectatlas_Risico_verschilzetting` retourneren IDENTIEKE
    # data — ze wijzen beide naar dezelfde funderingsrisico-tabel per
    # CBS-buurt (zelfde OBJECTID, zelfde percentages, zelfde aantal_pan).
    # We combineren ze daarom tot één rij "Funderingsrisico" en laten het
    # bodemtype de INTERPRETATIE bepalen:
    #   - Veen/slappe bodem → paalrot-narrative (houten palen in uitdrogend veen)
    #   - Klei/zand-overgang → verschilzetting-narrative (ongelijk zakken)
    #   - Rivierengebied / stedelijk → beide van toepassing
    p = res["paalrot"] if isinstance(res["paalrot"], dict) else {}
    if not p:
        p = res["verschilzet"] if isinstance(res["verschilzet"], dict) else {}
    if p:
        # Bepaal het dominant-type op basis van bodem_code
        if bodem_code in (3, 4, 10):        # echte veen-gebieden
            fund_key, fund_label = "paalrot", "Paalrot-risico (houten palen in veen)"
        elif bodem_code in (6, 7, 8, 9, 11):  # zand/klei-overgang
            fund_key, fund_label = "verschilzetting", "Verschilzetting (zand/klei-zakkingen)"
        else:
            fund_key, fund_label = "funderingsrisico", "Funderingsrisico (paalrot + verschilzetting)"
        risicos.append(Risico(
            key=fund_key,
            label=fund_label,
            relevant=("paalrot" in relevant_keys) or ("verschilzetting" in relevant_keys),
            pct=p.get("pct_sterk") or p.get("pct_mild"),
            aantal_panden=p.get("aantal_panden"),
            buurtnaam=p.get("buurtnaam"),
        ))

    # --- Hittestress (ImageServer, klasse 1-5) ---
    h = res["hittestress"] if isinstance(res["hittestress"], (int, float)) else None
    if h is not None and h > 0:
        risicos.append(Risico(
            key="hittestress",
            label="Hittestress (warme nachten)",
            relevant=True,  # universeel
            klasse=int(round(h)),
        ))

    # --- Wateroverlast intense neerslag (ImageServer, cm) ---
    w = res["wateroverlast"] if isinstance(res["wateroverlast"], (int, float)) else None
    if w is not None and w > 0:
        risicos.append(Risico(
            key="wateroverlast",
            label="Wateroverlast bij stortbui",
            relevant=True,
            waarde=float(w),
            eenheid="cm",
        ))

    # --- Overstromingskans (plaatsgebonden, van rivier/zee) ---
    # We tonen ALTIJD een overstromings-record, ook bij NoData, zodat de
    # gebruiker ziet dat we hebben gekeken. NoData in de CAS-raster = locatie
    # wordt door het systeem beschouwd als beschermd (achter dijk/wal) of
    # hoger gelegen → dus 'geen risico'.
    ok = res["oversr_kans"] if isinstance(res["oversr_kans"], (int, float)) else None
    if ok is not None and ok > 0:
        risicos.append(Risico(
            key="overstroming",
            label="Overstromingskans (rivier/zee)",
            relevant=True,
            klasse=int(round(ok)),
        ))
    else:
        # NoData fallback: 'praktisch geen risico' (achter dijk, hoger gelegen,
        # of CAS heeft geen data voor dit punt). Klasse 0 signaleert 'geen'.
        risicos.append(Risico(
            key="overstroming",
            label="Overstromingskans (rivier/zee)",
            relevant=True,
            klasse=0,
        ))

    # --- Maximale overstromingsdiepte bij rampscenario ---
    # Raster levert waarde in METERS (F32, max ~100+ voor rampscenario's).
    # Bij NoData tonen we 'geen risico' i.p.v. het veld te verbergen —
    # anders denkt de gebruiker dat we het niet gecheckt hebben.
    od = res["oversr_diepte"] if isinstance(res["oversr_diepte"], (int, float)) else None
    if od is not None and od > 0.01:  # <1cm = ruis
        risicos.append(Risico(
            key="overstroming_diepte",
            label="Maximale overstromingsdiepte (rampscenario)",
            relevant=True,
            waarde=round(float(od) * 100, 0),  # m → cm
            eenheid="cm",
        ))
    else:
        risicos.append(Risico(
            key="overstroming_diepte",
            label="Maximale overstromingsdiepte (rampscenario)",
            relevant=True,
            waarde=0,  # 0 = geen risico
            eenheid="cm",
        ))

    # --- Droogtestress (zandgrond kwetsbaar) ---
    # Bron-raster heeft schaal 0-42 (beschrijft droge dagen/indexscore).
    # Normaliseren naar 1-5 klasse voor consistente UI:
    #   0-8:   klasse 1 (zeer laag)
    #   8-16:  klasse 2 (laag)
    #   16-24: klasse 3 (middel)
    #   24-32: klasse 4 (hoog)
    #   32+:   klasse 5 (zeer hoog)
    dr = res["droogte"] if isinstance(res["droogte"], (int, float)) else None
    if dr is not None and dr > 0:
        # Normalize 0-42 → 1-5
        raw = float(dr)
        if raw < 8:
            dr_klasse = 1
        elif raw < 16:
            dr_klasse = 2
        elif raw < 24:
            dr_klasse = 3
        elif raw < 32:
            dr_klasse = 4
        else:
            dr_klasse = 5
        risicos.append(Risico(
            key="droogte",
            label="Droogtestress (bomen, tuin, ondergrond)",
            relevant="droogte" in relevant_keys,
            klasse=dr_klasse,
            waarde=raw,  # ruwe bron-waarde bewaard voor debugging
        ))

    # --- Bodemdaling (veen-gebieden) ---
    bd = res["bodemdaling"] if isinstance(res["bodemdaling"], (int, float)) else None
    if bd is not None and bd > 0:
        risicos.append(Risico(
            key="bodemdaling",
            label="Bodemdaling (veen)",
            relevant="bodemdaling" in relevant_keys,
            waarde=float(bd),
            eenheid="mm/jaar",
        ))

    return Klimaatrisico(
        bodemtype_code=bodem_code,
        bodemtype_label=bodem_label,
        risicos=risicos,
    )


# --- Helpers ---------------------------------------------------------------

async def _fetch_bodemtype(
    client: httpx.AsyncClient, lat: float, lon: float
) -> Optional[int]:
    """Bodemtype gridcode (1-11) uit Klimaateffectatlas Basiskaart."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "gridcode",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = await client.get(f"{BODEMTYPE_URL}/query", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    feats = data.get("features", [])
    if not feats:
        return None
    code = feats[0].get("attributes", {}).get("gridcode")
    if code is None:
        return None
    # gridcode is 1000, 2000, ..., 11000; deel door 1000 voor 1-11
    try:
        return int(code) // 1000
    except (TypeError, ValueError):
        return None


async def _fetch_fs_panden(
    client: httpx.AsyncClient, fs_url: str, lat: float, lon: float
) -> dict:
    """Query FeatureServer met %-panden-velden (paalrot/verschilzetting)."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "buurtnaam,aantal_pan,percentage,percenta_1,sterke_c_1",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = await client.get(f"{fs_url}/query", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}
    feats = data.get("features", [])
    if not feats:
        return {}
    a = feats[0].get("attributes", {})
    return {
        "buurtnaam": a.get("buurtnaam"),
        "aantal_panden": a.get("aantal_pan"),
        "pct_mild": _to_pct(a.get("percentage")),
        "pct_sterk": _to_pct(a.get("sterke_c_1")),
    }


async def _fetch_image_value_rd(
    client: httpx.AsyncClient, base_url: str, rd_x: float, rd_y: float
) -> Optional[float]:
    """Identify-call op ImageServer met RD-coordinaat (EPSG:28992).

    Sommige CAS-services werken alleen met native RD — WGS84-input geeft
    stille NoData-response (bv. overstromingskans voor Grave teruggeeft
    niets met 4326, maar wel klasse 4 met 28992).
    """
    if not rd_x or not rd_y:
        return None
    geom = f'{{"x":{rd_x},"y":{rd_y},"spatialReference":{{"wkid":28992}}}}'
    params = {
        "geometry": geom,
        "geometryType": "esriGeometryPoint",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = await client.get(f"{base_url}/identify", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    v = data.get("value")
    if v in (None, "NoData", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_pct(v) -> Optional[float]:
    """Normaliseer percentage naar 0-100 (bron soms 0-1, soms 0-100)."""
    if v is None:
        return None
    try:
        n = float(v)
        if 0 <= n <= 1:
            return round(n * 100, 1)
        return round(n, 1)
    except (TypeError, ValueError):
        return None
