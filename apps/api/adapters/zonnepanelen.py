"""
Zonnepanelen-schatting voor Sectie 10 — pure rekenfunctie.

Input:
  - pand-footprint polygoon (Shapely, RD/EPSG:28992 in meters)
  - dak-type uit 3D BAG ('flat', 'slanted', 'multiple horizontal' enz.)
  - monument-vlag (rijks of gemeentelijk → strenger)
  - beschermd-stadsgezicht-vlag
  - is_appartement (gedeeld dak → VvE-besluit)

Output:
  - aantal panelen (range, integer)
  - jaarlijkse opbrengst (range, kWh)
  - verhouding tot gemiddeld huishoud-verbruik (~2.700 kWh/jr)
  - verdict-level (good/neutral/warn) + samenvattende tekst

Belangrijke uitgangspunten (april 2026):
  - Saldering wordt 1-1-2027 afgeschaft → we tonen GEEN terugverdientijd,
    alleen kWh-opbrengst + verhouding tot huishoudverbruik.
  - Standaard zonnepaneel ~1,75 m² met montage-ruimte ~2,0 m².
  - Modern paneel-vermogen ~425 Wp.
  - NL-gemiddelde opbrengst per kWp:
      hellend zuid (30°)        : 950 kWh
      hellend oost+west gespreid: 800 kWh (per dakvlak ~80% van zuid)
      plat dak (10° opstelling) : 850 kWh
  - Schaduw-onzekerheid → we tonen ALTIJD een range (laag = -25%, hoog = nominaal).
  - Bruikbaar dakopp:
      plat: 65 % (na randzone, AC, dakkapel, schoorsteen)
      hellend: 50 % per dakvlak (idem) × 1,18 (helling-correctie 25°)

Voor monument (rijks én gemeentelijk én beschermd stadsgezicht):
  - Fysiek mogelijk = achterkant met vergunning → 50% van bruikbaar
  - Het verschil tussen rijks- en gemeentelijk monument zit in de KANS op
    goedkeuring en de zwaarte van de procedure (RCE+welstand vs alleen
    welstand), niet in het fysiek mogelijke aantal panelen. Dat verschil
    verwoorden we in de detail-tekst, niet in de aantallen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from shapely.geometry import Polygon


@dataclass
class ZonnepanelenSchatting:
    """Schatting voor zonnepanelen op een pand."""
    aantal_panelen_min: int        # ondergrens (na schaduw-correctie)
    aantal_panelen_max: int        # bovengrens
    kwh_per_jaar_min: int          # ondergrens jaaropbrengst
    kwh_per_jaar_max: int          # bovengrens
    pct_huishoudverbruik_min: int  # % van NL-gemiddeld huishouden
    pct_huishoudverbruik_max: int
    config_beschrijving: str       # 'plat dak', 'zuid-dak', 'oost+west-daken' etc.
    bruikbaar_dakopp_m2: int       # info: hoeveel m² dakvlak bruikbaar
    beperking: Optional[str] = None  # 'monument' / 'appartement' / None


# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------

GEM_HUISHOUD_KWH_JAAR = 2700      # NL-gemiddeld huishouden (CBS)
PANEEL_OPP_M2 = 2.0                # incl. montage-ruimte
PANEEL_WP = 425                    # modern paneel-vermogen

# Bruikbaarheid van het dakoppervlak na obstakels:
PLAT_BRUIKBAAR_FR = 0.50           # 50% verlies (rand 1.5m + AC + dakkapellen + lichtkoepels + paden)
HELLEND_PER_VLAK_FR = 0.55         # 55% bruikbaar per dakvlak (dakkapellen, schoorsteen, randzone)
HELLEND_HELLING_CORR = 1.18        # 25° helling: dakopp = footprint × 1.18

# NB: bij hellend dak telt PER DAKVLAK slechts de HALVE footprint (zadeldak
# bestaat uit 2 vlakken die samen de footprint dekken). Dus voor zuid-only
# is bruikbaar = (footprint/2) × HELLING_CORR × PER_VLAK_FR.

# Opbrengst per kWp per jaar (NL-gemiddelde):
KWH_PER_KWP_ZUID = 950             # hellend zuidgericht 30°
KWH_PER_KWP_OOSTWEST = 800         # gespreid over oost + west
KWH_PER_KWP_PLAT = 850             # plat dak met 10° opstelling

# Schaduw-onzekerheid: ondergrens = nominaal × (1 - x)
SCHADUW_ONZEKERHEID = 0.25         # ondergrens 25% lager dan nominaal


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def _hoofd_orientatie_oost_west(poly: Polygon) -> bool:
    """True als de pand-hoofdas (lange zijde) oost-west loopt.

    We bepalen dit via de minimum-rotated-rectangle: dat geeft de geroteerde
    bounding-box die de polygoon strak omvat. De hoek tussen de lange zijde
    en de oost-west-as bepalt de oriëntatie.

    Voor onze toepassing:
      - Lange zijde oost-west → pand 'breed' richting zuid: meestal 1 zuid-dak
        en 1 noord-dak (asymmetrisch rendement; we pakken alleen zuid).
      - Lange zijde noord-zuid → pand 'breed' richting oost: oost- en west-dak
        beide bruikbaar (gespreid rendement).

    Bij vrijwel-vierkante panden of complexe vormen: default we naar
    oost-west (zuid-dak aanname), wat conservatiever is qua dakopp.
    """
    try:
        mrr = poly.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
    except Exception:
        return True
    if len(coords) < 4:
        return True
    import math
    p0, p1, p2 = coords[0], coords[1], coords[2]
    len01 = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    len12 = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if max(len01, len12) == 0:
        return True
    # Vrijwel-vierkant pand: oriëntatie is onbepaald → default naar
    # oost-west-as (= conservatieve zuid-dak schatting). Drempel: minder
    # dan 20% verschil tussen lange en korte zijde.
    short, long = sorted([len01, len12])
    if short / long > 0.80:
        return True  # conservatief: alleen zuid-dak
    # Niet-vierkant: pak hoek van de LANGE zijde
    if len01 >= len12:
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    else:
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    # Hoek t.o.v. oost-west (X-as in RD)
    hoek_deg = abs(math.degrees(math.atan2(dy, dx))) % 180
    # Lange zijde oost-west als hoek dichter bij 0° of 180° dan bij 90°
    afwijking_oost_west = min(hoek_deg, 180 - hoek_deg)
    return afwijking_oost_west < 45


def _is_plat_dak(daktype: Optional[str]) -> bool:
    """3D BAG dak-type → plat / hellend.

    Mogelijke waardes: 'horizontal', 'slanted', 'multiple horizontal',
    'pitched', 'flat'. 'horizontal'/'flat'/'multiple horizontal' = plat.
    """
    if not daktype:
        return False
    return any(s in daktype.lower() for s in ("horizontal", "flat"))


# ---------------------------------------------------------------------------
# Hoofd-rekenfunctie
# ---------------------------------------------------------------------------

def schat_zonnepanelen(
    pand_op_perceel_poly: Optional[Polygon],
    daktype: Optional[str],
    is_rijksmonument: bool = False,
    is_gem_monument: bool = False,
    is_beschermd_gezicht: bool = False,
    is_appartement: bool = False,
) -> Optional[ZonnepanelenSchatting]:
    """Bereken een eerlijke range van panelen + opbrengst.

    Returns None als we niet eens een footprint hebben (geen pand-polygoon).
    Voor appartement zetten we beperking='appartement' en geven 0-schatting
    terug — de UI toont dan alleen de tekst, niet de getallen.
    """
    if pand_op_perceel_poly is None or pand_op_perceel_poly.is_empty:
        return None

    footprint_m2 = pand_op_perceel_poly.area
    if footprint_m2 <= 0:
        return None

    # Appartement: dak hoort bij VvE — geen individuele installatie
    if is_appartement:
        return ZonnepanelenSchatting(
            aantal_panelen_min=0, aantal_panelen_max=0,
            kwh_per_jaar_min=0, kwh_per_jaar_max=0,
            pct_huishoudverbruik_min=0, pct_huishoudverbruik_max=0,
            config_beschrijving="gedeeld dak",
            bruikbaar_dakopp_m2=0,
            beperking="appartement",
        )

    plat = _is_plat_dak(daktype)

    # Stap 1: bruikbaar dakoppervlak
    if plat:
        bruikbaar = footprint_m2 * PLAT_BRUIKBAAR_FR
        config = "plat dak"
        kwh_per_kwp = KWH_PER_KWP_PLAT
    else:
        # Hellend zadeldak: één dakvlak = (footprint/2) × helling-correctie.
        # Beide vlakken samen ≈ footprint × helling-correctie (1.18).
        if _hoofd_orientatie_oost_west(pand_op_perceel_poly):
            # Pand-as oost-west → 1 zuid-dak en 1 noord-dak.
            # We tellen alleen het zuid-dak (noord-rendement < 50%).
            bruikbaar = (footprint_m2 / 2) * HELLEND_HELLING_CORR * HELLEND_PER_VLAK_FR
            config = "zuid-dak (hellend)"
            kwh_per_kwp = KWH_PER_KWP_ZUID
        else:
            # Pand-as noord-zuid → 1 oost-dak en 1 west-dak.
            # Beide bruikbaar (samen ≈ volledige dakopp).
            bruikbaar = footprint_m2 * HELLEND_HELLING_CORR * HELLEND_PER_VLAK_FR
            config = "oost+west-daken (hellend)"
            kwh_per_kwp = KWH_PER_KWP_OOSTWEST

    # Stap 2: monument-reductie — bij ALLE monument-types geldt dezelfde
    # fysieke beperking: alleen het achter-dakvlak komt in aanmerking
    # (× 0.5). Het verschil zit in de toetsingsprocedure, niet in het
    # mogelijke aantal panelen — dat communiceren we in de detail-tekst.
    beperking = None
    if is_rijksmonument:
        bruikbaar = bruikbaar * 0.5
        config = f"alleen achterkant ({config})"
        beperking = "rijksmonument"
    elif is_gem_monument:
        bruikbaar = bruikbaar * 0.5
        config = f"alleen achterkant ({config})"
        beperking = "gemeentelijk monument"
    elif is_beschermd_gezicht:
        bruikbaar = bruikbaar * 0.5
        config = f"alleen achterkant ({config})"
        beperking = "beschermd gezicht"

    # Stap 3: aantal panelen + vermogen
    if bruikbaar < PANEEL_OPP_M2:
        # Minder dan 1 paneel mogelijk → wel teruggeven met expliciete 0
        return ZonnepanelenSchatting(
            aantal_panelen_min=0, aantal_panelen_max=0,
            kwh_per_jaar_min=0, kwh_per_jaar_max=0,
            pct_huishoudverbruik_min=0, pct_huishoudverbruik_max=0,
            config_beschrijving=config,
            bruikbaar_dakopp_m2=int(bruikbaar),
            beperking=beperking or "te weinig dakopp",
        )

    aantal_max = int(bruikbaar / PANEEL_OPP_M2)
    aantal_min = max(1, int(aantal_max * (1 - SCHADUW_ONZEKERHEID)))
    # Geen aparte rijksmonument-override op aantallen meer — fysieke
    # mogelijkheid is gelijk aan gem.monument; de strengere kans staat
    # in card_detail().

    kwp_max = aantal_max * PANEEL_WP / 1000.0
    kwp_min = aantal_min * PANEEL_WP / 1000.0
    kwh_max = int(kwp_max * kwh_per_kwp)
    kwh_min = int(kwp_min * kwh_per_kwp)

    pct_max = int(kwh_max / GEM_HUISHOUD_KWH_JAAR * 100)
    pct_min = int(kwh_min / GEM_HUISHOUD_KWH_JAAR * 100)

    return ZonnepanelenSchatting(
        aantal_panelen_min=aantal_min,
        aantal_panelen_max=aantal_max,
        kwh_per_jaar_min=kwh_min,
        kwh_per_jaar_max=kwh_max,
        pct_huishoudverbruik_min=pct_min,
        pct_huishoudverbruik_max=pct_max,
        config_beschrijving=config,
        bruikbaar_dakopp_m2=int(bruikbaar),
        beperking=beperking,
    )


# ---------------------------------------------------------------------------
# UI-tekst-builders (gebruikt door orchestrator)
# ---------------------------------------------------------------------------

def card_samenvatting(s: ZonnepanelenSchatting) -> str:
    """Korte tekst voor de card-header."""
    if s.beperking == "appartement":
        return "Dak hoort bij VvE — complex-besluit"
    if s.aantal_panelen_max == 0:
        return "Onvoldoende dakoppervlak"
    # Bij ALLE monumenten: zelfde fysieke beperking (achterkant alleen)
    if s.beperking in ("rijksmonument", "gemeentelijk monument", "beschermd gezicht"):
        return f"Achterkant: ~{s.aantal_panelen_min}–{s.aantal_panelen_max} panelen"
    return f"~{s.aantal_panelen_min}–{s.aantal_panelen_max} panelen mogelijk"


def card_detail(s: ZonnepanelenSchatting) -> str:
    """Uitgebreide tekst voor de card-detail."""
    if s.beperking == "appartement":
        return (
            "Het dak hoort bij het hele complex en valt onder de VvE — "
            "een installatie is een collectief besluit, niet individueel "
            "te regelen. Bij een dakopstelling deelt de VvE meestal de "
            "kosten en opbrengsten naar rato van eigendom."
        )

    if s.aantal_panelen_max == 0:
        return (
            f"Bruikbaar dakoppervlak na obstakels en oriëntatie is te klein "
            f"(~{s.bruikbaar_dakopp_m2} m²). Een paneel vraagt circa 2 m² "
            f"effectief."
        )

    # Standaard rapport
    kwh_range = f"~{s.kwh_per_jaar_min:,}–{s.kwh_per_jaar_max:,} kWh per jaar".replace(",", ".")
    pct_range = f"{s.pct_huishoudverbruik_min}–{s.pct_huishoudverbruik_max}%"
    config_tekst = s.config_beschrijving

    if s.beperking == "rijksmonument":
        prefix = (
            f"Alleen het achter-dakvlak komt in aanmerking. Voor een "
            f"rijksmonument toetst de RCE samen met de welstandscommissie "
            f"streng op zicht-criterium en omkeerbaarheid; goedkeuring is "
            f"minder vanzelfsprekend dan bij een gemeentelijk monument. "
            f"Fysiek mogelijk op de achterkant: "
        )
    elif s.beperking == "gemeentelijk monument":
        prefix = (
            f"Alleen het achter-dakvlak komt in aanmerking; de "
            f"welstandscommissie toetst per dakvlak. "
            f"Fysiek mogelijk op de achterkant: "
        )
    elif s.beperking == "beschermd gezicht":
        prefix = (
            f"Alleen het achter-dakvlak komt in aanmerking; in een "
            f"beschermd stadsgezicht weegt de welstand het straatbeeld "
            f"zwaar mee. Fysiek mogelijk op de achterkant: "
        )
    else:
        prefix = f"Op dit pand ({config_tekst}) is plek voor "

    return (
        f"{prefix}circa {s.aantal_panelen_min}–{s.aantal_panelen_max} panelen, "
        f"goed voor {kwh_range}. "
        f"Dat is {pct_range} van een gemiddeld huishoud-verbruik "
        f"(~{GEM_HUISHOUD_KWH_JAAR:,} kWh/jaar).".replace(",", ".") +
        " Schaduw van bomen of buurpanden is niet meegerekend en kan "
        "het rendement met ±25 % verlagen. "
        "NB: per 1-1-2027 vervalt de saldering — eigen verbruik wordt "
        "dan financieel meer waard dan terugleveren."
    )


def card_level(s: ZonnepanelenSchatting) -> str:
    """Card-level voor UI: good/neutral/warn."""
    if s.beperking == "appartement":
        return "neutral"
    if s.aantal_panelen_max == 0:
        return "warn"
    # Bij elke monument-vorm: 'neutral' (kan met vergunning, geen blocker).
    # Het verschil in goedkeurings­kans staat in card_detail.
    if s.beperking in ("rijksmonument", "gemeentelijk monument", "beschermd gezicht"):
        return "neutral"
    return "good"
