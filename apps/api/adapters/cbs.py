"""
CBS OData v4 adapter — Kerncijfers Wijken en Buurten.

Input  : CBS buurtcode (bv. 'BU0363AD03')
Output : demografie, inkomen, WOZ, voorzieningen-afstanden, laadpalen

Default dataset: 85984NED (jaargang 2024). Voor actualisatie naar 2025
  vervang DATASET_ID door '86165NED' (identieke schema's).

Gebruikt datasets.cbs.nl (de 'nieuwe' OData v4 omgeving, cell-based).
Alle data is kosteloos, geen key. Docs:
  https://www.cbs.nl/nl-nl/onze-diensten/open-data/statline-als-open-data/snelstartgids-odata-v4
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

DATASET_ID = "85984NED"  # Kerncijfers wijken en buurten 2024
DATASET_NABIJHEID = "84718NED"  # Nabijheid voorzieningen — uitgebreide afstanden
BASE_URL = f"https://datasets.cbs.nl/odata/v1/CBS/{DATASET_ID}"
BASE_URL_NABIJHEID = f"https://datasets.cbs.nl/odata/v1/CBS/{DATASET_NABIJHEID}"
TIMEOUT_S = 6.0

# Historische jaargangen van "Kerncijfers Wijken en Buurten" voor WOZ-trend.
# LET OP: er zit een breuk in de buurt-codering rond 2023 — oude datasets
# gebruiken numerieke codes (BU03630000), nieuwe alfanumeriek (BU0363AD03).
# Voor trend tonen we alleen de datasets die de nieuwe code kennen, tenzij
# we een goede mapping hebben. Voor MVP = 2023+2024 (twee-jaars delta).
WOZ_TREND_DATASETS = [
    ("2024", "85984NED"),
    ("2023", "85618NED"),
]

# Uitgebreide voorzieningen-codes uit 84718NED. Veel voorzieningen zijn er
# alleen op gemeente-niveau betrouwbaar; voor MVP nemen we gemeentecode.
# Elke tuple: (output_name, measure_code, emoji).
VOORZIENINGEN_CODES = [
    # Boodschappen & dagelijks
    ("supermarkt",          "D000025", "🛒"),
    ("dagelijkse_levensmiddelen", "D000038", "🏪"),
    # Zorg
    ("huisarts",            "D000028", "🏥"),
    ("huisartsenpost",      "D000027", "🚑"),
    ("apotheek",            "D000010", "💊"),
    ("fysiotherapeut",      "D000024", "🧘"),
    ("ziekenhuis",          "D000056_1", "🏥"),
    # Onderwijs & kinderen
    ("basisschool",         "D000045_1", "🏫"),
    ("kinderdagverblijf",   "D000029", "👶"),
    ("buitenschoolse_opvang", "D000019", "🎒"),
    # Horeca & uitgaan
    ("restaurant",          "D000043", "🍴"),
    ("cafe",                "D000020", "🍺"),
    ("cafetaria",           "D000021", "🍟"),
    ("hotel",               "D000026", "🏨"),
    # Groen & recreatie
    ("park",                "D000039", "🌳"),
    ("bos",                 "D000017", "🌲"),
    ("sportterrein",        "D000051", "⚽"),
    ("zwembad",             "D000058", "🏊"),
    # Mobiliteit
    ("treinstation",        "D000052", "🚆"),
    ("overstapstation",     "D000014", "🚉"),
    ("oprit_snelweg",       "D000037", "🛣️"),
    # Cultuur
    ("bibliotheek",         "D000015", "📚"),
    ("museum",              "D000032", "🎨"),
    ("bioscoop",            "D000016", "🎬"),
]

# Measure-codes die we direct gebruiken in de 6 secties van de app.
# Alle codes komen uit GET /MeasureCodes van dataset 85984NED.
# Comments = UI-sectie die de indicator voedt.
MEASURES = {
    # --- Sectie 1 "De woning" (aangevuld met BAG-data) ---
    # ... geen CBS-codes nodig; alles uit BAG ---

    # --- Sectie 2 "Waarde & wijk-economie" ---
    "woz_gemiddeld": "M001642",             # Gemiddelde WOZ-waarde van woningen
    "inkomen_per_inwoner": "M000224",       # Gem inkomen per inwoner
    "arbeidsparticipatie": "M001796_2",     # Nettoarbeidsparticipatie (%)

    # --- Sectie 3 "De buren (sociaal weefsel)" ---
    "inwoners": "T001036",                  # Aantal inwoners
    "bevolkingsdichtheid": "M000100",       # per km^2
    "huishoudens": "1050010_2",             # totaal huishoudens
    "eenpersoonshuishoudens": "1050015",
    "huishoudens_met_kinderen": "1016030",
    "huishoudensgrootte": "M000114",        # gemiddeld

    # --- Sectie 2 uitbreiding — opleidingsniveau ---
    "opleiding_laag": "2018700",            # Basisonderwijs, vmbo, mbo1
    "opleiding_midden": "2018740",          # Havo, vwo, mbo2-4
    "opleiding_hoog": "2018790",            # Hbo, wo

    # --- Sectie 5 "Leefkwaliteit hier & nu" — proxy voor modernisering ---
    "laadpalen": "M008299",                 # publieke laadpalen in buurt

    # --- Voorzieningen-ringen (v1, buurt-gemiddelde) ---
    "afstand_huisarts": "D000028",
    "afstand_supermarkt": "D000025",
    "afstand_kinderdagverblijf": "D000029",
    "afstand_school": "D000045",
}

# Speciale missing-value codes van CBS OData (worden ValueAttribute, niet Value)
# We behandelen ze allemaal als None in onze output.
MISSING_ATTRIBUTES = {"None", "Missing", "Imputed", "Unknown"}


@dataclass
class BuurtStats:
    """Resultaat van CBS-lookup op buurtcode.

    Alle velden zijn Optional — CBS publiceert niet elk cijfer voor elke buurt
    (geheimhouding bij te kleine aantallen, peiljaar-verschuivingen). None = UI
    toont 'onbekend' i.p.v. een verzonnen 0.
    """

    buurtcode: str
    # Sectie 2
    woz_gemiddeld_x1000_eur: Optional[float]  # CBS publiceert in eenheden van 1.000 euro
    inkomen_per_inwoner_x1000_eur: Optional[float]
    arbeidsparticipatie_pct: Optional[float]
    # Opleidingsniveau (absolute aantallen; frontend/orchestrator maakt % ervan)
    opleiding_laag: Optional[int]
    opleiding_midden: Optional[int]
    opleiding_hoog: Optional[int]
    # Sectie 3
    inwoners: Optional[int]
    bevolkingsdichtheid_per_km2: Optional[int]
    huishoudens: Optional[int]
    eenpersoonshuishoudens: Optional[int]
    huishoudens_met_kinderen: Optional[int]
    huishoudensgrootte: Optional[float]
    # Sectie 5
    laadpalen: Optional[int]
    # Voorzieningen-ringen (afstanden in km)
    afstand_huisarts_km: Optional[float]
    afstand_supermarkt_km: Optional[float]
    afstand_kinderdagverblijf_km: Optional[float]
    afstand_school_km: Optional[float]


async def fetch_buurt(buurtcode: str) -> BuurtStats:
    """Haal alle relevante indicatoren op voor één buurt via één OData-call.

    We vragen alle measures in één $filter met 'in'-operator op Measure,
    zodat we niet 15x round-trippen. Eén HTTP-call is tientallen ms;
    15 calls zou >1s aan latency toevoegen.
    """
    measure_codes = list(MEASURES.values())
    # OData 'in' expressie: Measure in ('M001642','M000224',...)
    in_list = ",".join(f"'{c}'" for c in measure_codes)
    filter_expr = f"WijkenEnBuurten eq '{buurtcode}' and Measure in ({in_list})"

    params = {
        "$filter": filter_expr,
        "$select": "Measure,Value,StringValue,ValueAttribute",
        "$top": str(len(measure_codes)),
    }

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(f"{BASE_URL}/Observations", params=params)
        resp.raise_for_status()
        data = resp.json()

    # Invert MEASURES: code -> veldnaam
    code_to_field = {v: k for k, v in MEASURES.items()}

    # Parse observations: één rij per measure voor deze buurt
    parsed: dict[str, Optional[float]] = {k: None for k in MEASURES}
    for obs in data.get("value", []):
        code = obs.get("Measure")
        field = code_to_field.get(code)
        if not field:
            continue
        if obs.get("ValueAttribute") in MISSING_ATTRIBUTES and obs.get("Value") is None:
            # Expliciet ontbrekende waarde (geheimhouding of n.v.t.)
            continue
        parsed[field] = obs.get("Value")

    return _build_buurtstats(buurtcode, parsed)


async def fetch_voorzieningen(buurtcode: str, gemeentecode: str) -> list[dict]:
    """Uitgebreide voorzieningen-afstanden uit dataset 84718NED.

    Flow:
      1. Probeer buurtcode (werkt alleen als het de OUDE numerieke code is;
         PDOK levert nieuwe alfanumerieke codes zoals BU0363AD03 die hier
         vaak niet bestaan).
      2. Fall back op gemeentecode (GM + 4 cijfers) — geeft gemeente-gemiddelde,
         ~60% nauwkeurigheid van buurt maar dekt alle voorzieningen.

    Retourneert lijst van dicts: [{type, km, emoji}, ...] gesorteerd op afstand.
    """
    codes = [c for _, c, _ in VOORZIENINGEN_CODES]
    in_list = ",".join(f"'{c}'" for c in codes)

    # Stap 1: buurt-probeer (kan 0 resultaten opleveren bij nieuwe codes)
    results: dict[str, float] = {}
    if buurtcode:
        results = await _query_voorzieningen(buurtcode, in_list)

    # Stap 2: gemeente-fallback. PDOK levert de gemeentecode als 4 cijfers
    # ('0363'), maar CBS verwacht de 'GM'-prefix.
    if gemeentecode:
        gm_code = gemeentecode if gemeentecode.startswith("GM") else f"GM{gemeentecode}"
        gem_results = await _query_voorzieningen(gm_code, in_list)
        for code, km in gem_results.items():
            if code not in results:
                results[code] = km

    # Zet om naar lijst gesorteerd op afstand
    items = []
    for name, code, emoji in VOORZIENINGEN_CODES:
        km = results.get(code)
        if km is None:
            continue
        items.append({"type": name, "km": km, "emoji": emoji})
    items.sort(key=lambda x: x["km"])
    return items


async def fetch_woz_trend(buurtcode: str) -> list[dict]:
    """WOZ-waarde over meerdere jaargangen — simpele 2-puntsreeks voor MVP.

    Retourneert lijst [{year, woz_eur}, ...] gesorteerd op jaar oplopend.
    Lege waarden (onbekende buurt in die jaargang) worden overgeslagen.
    """
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        # Alle jaargangen parallel (2 calls is niets)
        import asyncio as _asyncio

        async def _fetch_one(year: str, dataset: str) -> Optional[dict]:
            params = {
                "$filter": f"WijkenEnBuurten eq '{buurtcode}' and Measure eq 'M001642'",
                "$select": "Value",
                "$top": "1",
            }
            try:
                resp = await client.get(
                    f"https://datasets.cbs.nl/odata/v1/CBS/{dataset}/Observations",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                return None
            rows = data.get("value", [])
            if not rows or rows[0].get("Value") is None:
                return None
            return {"year": year, "woz_eur": int(rows[0]["Value"] * 1000)}

        tasks = [_fetch_one(y, ds) for y, ds in WOZ_TREND_DATASETS]
        raw = await _asyncio.gather(*tasks)

    for r in raw:
        if r is not None:
            results.append(r)
    results.sort(key=lambda r: r["year"])
    return results


async def _query_voorzieningen(wnb_code: str, in_list: str) -> dict[str, float]:
    """Eén OData-call op 84718NED voor alle voorzieningen-codes.

    Retourneert dict code -> km. Lege waarden (geheimhouding) worden
    overgeslagen.
    """
    filter_expr = f"WijkenEnBuurten eq '{wnb_code}' and Measure in ({in_list})"
    params = {
        "$filter": filter_expr,
        "$select": "Measure,Value,ValueAttribute",
        "$top": "100",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(f"{BASE_URL_NABIJHEID}/Observations", params=params)
        resp.raise_for_status()
        data = resp.json()

    out: dict[str, float] = {}
    for obs in data.get("value", []):
        code = obs.get("Measure")
        if obs.get("ValueAttribute") in MISSING_ATTRIBUTES and obs.get("Value") is None:
            continue
        v = obs.get("Value")
        if isinstance(v, (int, float)):
            out[code] = float(v)
    return out


def _build_buurtstats(buurtcode: str, parsed: dict) -> BuurtStats:
    """Helper; blijft bestaan voor compatibiliteit met de oude signatuur."""
    def as_int(v: Optional[float]) -> Optional[int]:
        return int(v) if v is not None else None

    return BuurtStats(
        buurtcode=buurtcode,
        woz_gemiddeld_x1000_eur=parsed["woz_gemiddeld"],
        inkomen_per_inwoner_x1000_eur=parsed["inkomen_per_inwoner"],
        arbeidsparticipatie_pct=parsed["arbeidsparticipatie"],
        opleiding_laag=as_int(parsed["opleiding_laag"]),
        opleiding_midden=as_int(parsed["opleiding_midden"]),
        opleiding_hoog=as_int(parsed["opleiding_hoog"]),
        inwoners=as_int(parsed["inwoners"]),
        bevolkingsdichtheid_per_km2=as_int(parsed["bevolkingsdichtheid"]),
        huishoudens=as_int(parsed["huishoudens"]),
        eenpersoonshuishoudens=as_int(parsed["eenpersoonshuishoudens"]),
        huishoudens_met_kinderen=as_int(parsed["huishoudens_met_kinderen"]),
        huishoudensgrootte=parsed["huishoudensgrootte"],
        laadpalen=as_int(parsed["laadpalen"]),
        afstand_huisarts_km=parsed["afstand_huisarts"],
        afstand_supermarkt_km=parsed["afstand_supermarkt"],
        afstand_kinderdagverblijf_km=parsed["afstand_kinderdagverblijf"],
        afstand_school_km=parsed["afstand_school"],
    )
