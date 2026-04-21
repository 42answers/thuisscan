"""
Politie Open Data adapter — geregistreerde misdrijven per buurt per maand.

Input  : CBS buurtcode (bv. 'BU0363AD03')
Output : laatste 12 maanden totaal misdrijven + woninginbraak + geweld

Dataset: 47022NED (Maandcijfers; wijk/buurt; soort misdrijf).

**Let op** — deze dataset zit op een ander OData-endpoint dan de demografie.
De Politie-data woont op 'dataderden.cbs.nl' met OData **v3** (TypedDataSet
met kolommen als directe properties), niet v4 (cell-based). Consequentie:
andere query-syntax, geen 'in'-operator, 'Key' en 'SoortMisdrijf' bevatten
trailing spaces die we moeten respecteren in filters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import httpx

BASE_URL = "https://dataderden.cbs.nl/ODataApi/OData/47022NED"
TIMEOUT_S = 8.0

# Soort-misdrijf-codes uit /SoortMisdrijf. De Politie/CBS levert deze
# codes met een trailing space; dat MOET letterlijk zo mee in filters,
# anders krijg je 0 resultaten terug (en geen foutmelding).
CODE_TOTAAL = "0.0.0 "
CODE_WONINGINBRAAK = "1.1.1 "
# Fietsendiefstal (incl. brom-/snorfietsen) — meest voorkomend misdrijf in NL.
# Sterke proxy voor sociale controle: buurten met veel fietsendiefstal zijn
# vaak doorgangswijken waar niemand zich verantwoordelijk voelt voor buiten.
CODE_FIETSENDIEFSTAL = "1.2.3 "
# Geweldscluster — bedreiging, mishandeling, straatroof, openlijk geweld, overval.
# Zedendelicten + moord zouden strikt ook meetellen maar die maken relevant
# nieuws; we willen hier juist de 'sfeer-op-straat' score.
GEWELD_CODES = [
    "1.4.3 ",  # Openlijk geweld (persoon)
    "1.4.4 ",  # Bedreiging
    "1.4.5 ",  # Mishandeling
    "1.4.6 ",  # Straatroof
    "1.4.7 ",  # Overval
]


@dataclass
class Misdrijven:
    buurtcode: str
    periode_van: str  # 'YYYYMMNN'
    periode_tot: str  # 'YYYYMMNN' (inclusief)
    totaal_12m: Optional[int]  # alle misdrijven in 12 maanden
    woninginbraak_12m: Optional[int]  # sterkste indicator voor sectie 4
    geweld_12m: Optional[int]  # samengesteld: bedreiging/mishandeling/etc.
    fietsendiefstal_12m: Optional[int]
    # Per 1000 inwoners voor vergelijkbaarheid tussen kleine/grote buurten.
    # None als inwonersaantal onbekend of 0.
    totaal_per_1000_inwoners: Optional[float]
    woninginbraak_per_1000_inwoners: Optional[float]
    geweld_per_1000_inwoners: Optional[float]
    fietsendiefstal_per_1000_inwoners: Optional[float]


def _month_range(today: date, months: int = 12) -> tuple[str, str]:
    """Laatste 'months' **afgesloten** maanden in 'YYYYMM##'-formaat.

    We pakken altijd tot en met de *vorige* maand, omdat de lopende maand
    nog onvolledig is en Politie-data ~4-6 weken achterloopt. Voor 20-04-2026
    met months=12: 2025MM04 t/m 2026MM03.
    """
    first_of_current = today.replace(day=1)
    last_month_end = first_of_current - timedelta(days=1)
    last_year, last_m = last_month_end.year, last_month_end.month

    # Reken in 'absolute maanden sinds jaar 0' om off-by-one-fouten te vermijden
    # die je krijgt bij naieve divmod op year/month-paren.
    end_abs = last_year * 12 + (last_m - 1)  # 0-indexed months
    start_abs = end_abs - (months - 1)
    start_year, start_m0 = divmod(start_abs, 12)
    van = f"{start_year:04d}MM{start_m0 + 1:02d}"
    tot = f"{last_year:04d}MM{last_m:02d}"
    return van, tot


async def fetch_misdrijven(
    buurtcode: str, inwoners: Optional[int] = None, today: Optional[date] = None
) -> Misdrijven:
    """Haal 12 maanden misdrijf-cijfers op voor één buurt.

    Args:
        buurtcode: CBS buurtcode uit Locatieserver (bv. 'BU0363AD03').
        inwoners: optioneel aantal inwoners uit CBS-adapter. Wordt gebruikt
            om per-1000 te berekenen. Als None, laten we die velden leeg.
        today: alleen voor tests; default is datetime.date.today().

    Eén OData-query met filter op WijkenEnBuurten + alle relevante codes +
    periode-range. Dat is een grote filter-string maar nog steeds 1 HTTP call.
    """
    if today is None:
        today = date.today()
    van, tot = _month_range(today, months=12)

    # OData v3 filter — 'in' bestaat niet, dus 'or'-ladder op SoortMisdrijf.
    all_codes = [CODE_TOTAAL, CODE_WONINGINBRAAK, CODE_FIETSENDIEFSTAL, *GEWELD_CODES]
    code_clauses = " or ".join(f"SoortMisdrijf eq '{c}'" for c in all_codes)
    filter_expr = (
        f"WijkenEnBuurten eq '{buurtcode}'"
        f" and Perioden ge '{van}' and Perioden le '{tot}'"
        f" and ({code_clauses})"
    )

    params = {
        "$filter": filter_expr,
        "$select": "SoortMisdrijf,Perioden,GeregistreerdeMisdrijven_1",
        "$top": "500",  # 12 mnd * 7 codes = 84 rijen, ruim binnen limiet
    }

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(f"{BASE_URL}/TypedDataSet", params=params)
        resp.raise_for_status()
        data = resp.json()

    # Aggregate: som per soort misdrijf over alle maanden
    totaal = 0
    inbraak = 0
    geweld = 0
    fietsen = 0
    any_seen = False
    for row in data.get("value", []):
        any_seen = True
        code = row.get("SoortMisdrijf")
        n = row.get("GeregistreerdeMisdrijven_1") or 0
        if code == CODE_TOTAAL:
            totaal += n
        elif code == CODE_WONINGINBRAAK:
            inbraak += n
        elif code == CODE_FIETSENDIEFSTAL:
            fietsen += n
        elif code in GEWELD_CODES:
            geweld += n

    # Als de dataset geen enkele rij voor deze buurt kent (bv. heel kleine
    # buurt met geheimhouding of net opgeheven buurtcode), laten we alle
    # velden None; de UI toont dan 'geen data' i.p.v. misleidende nullen.
    if not any_seen:
        return Misdrijven(
            buurtcode=buurtcode,
            periode_van=van,
            periode_tot=tot,
            totaal_12m=None,
            woninginbraak_12m=None,
            geweld_12m=None,
            fietsendiefstal_12m=None,
            totaal_per_1000_inwoners=None,
            woninginbraak_per_1000_inwoners=None,
            geweld_per_1000_inwoners=None,
            fietsendiefstal_per_1000_inwoners=None,
        )

    def per_1000(n: int) -> Optional[float]:
        if inwoners and inwoners > 0:
            return round(1000 * n / inwoners, 1)
        return None

    return Misdrijven(
        buurtcode=buurtcode,
        periode_van=van,
        periode_tot=tot,
        totaal_12m=totaal,
        woninginbraak_12m=inbraak,
        geweld_12m=geweld,
        fietsendiefstal_12m=fietsen,
        totaal_per_1000_inwoners=per_1000(totaal),
        woninginbraak_per_1000_inwoners=per_1000(inbraak),
        geweld_per_1000_inwoners=per_1000(geweld),
        fietsendiefstal_per_1000_inwoners=per_1000(fietsen),
    )
