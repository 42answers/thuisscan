"""
Referentiewaarden & interpretatie-tekst per indicator.

Kerngedachte: een kaal getal ("9.7 µg/m³") is waardeloos zonder context.
We leveren per indicator drie lagen context:

  1. chip      — ↑ goed / → gemiddeld / ↓ aandachtspunt  (kleurcode)
  2. referentie — 'NL-gemiddelde: X · Norm: Y'  (voor objectieve vergelijking)
  3. betekenis  — één zin in mensentaal over de consequentie

Alle referentiewaarden komen uit:
  - CBS Nederland 2024 (inkomen, WOZ, misdrijven, demografie)
  - RIVM GCN 2024 (luchtkwaliteit jaargemiddelden)
  - WHO 2021 Global Air Quality Guidelines
  - EU Richtlijn 2008/50/EG (luchtkwaliteitsnormen)

Dit module wordt bewust **niet** in de adapters gepropt; scheiden houdt
de interpretatie-logica los van de data-ophaal-logica (makkelijker te
updaten als normen veranderen, en testbaar zonder API-calls).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Reference:
    """Context om bij een waarde te tonen in de UI."""

    chip_level: str  # 'good' / 'neutral' / 'warn'
    chip_text: str  # bv. "boven WHO, binnen EU-norm"
    nl_gemiddelde: Optional[str]  # human-readable NL-referentie
    norm: Optional[str]  # WHO/EU/CBS-norm indien relevant
    betekenis: str  # één zin interpretatie


# ---------------------------------------------------------------------------
# Luchtkwaliteit (sectie 5)
# ---------------------------------------------------------------------------

def ref_bouwjaar(jaar: Optional[int]) -> Optional[Reference]:
    """Contextualiseer bouwjaar naar bouwperiode en implicaties.

    Periodes volgen gangbare Nederlandse bouwtechnische onderverdeling,
    met implicaties voor isolatie, funderingsrisico en monumentstatus.
    """
    if jaar is None:
        return None
    if jaar < 1900:
        level, chip, msg = "neutral", "monumentaal", "19e-eeuws of ouder; vaak houten fundering, meestal beschermd. Karakter maar onderhoudskosten."
    elif jaar < 1945:
        level, chip, msg = "neutral", "vooroorlogs", "Typisch Nederlands jaren-'20/'30-bouw; nauwelijks geïsoleerd zonder renovatie."
    elif jaar < 1970:
        level, chip, msg = "neutral", "wederopbouw", "Snel en sober gebouwd; vaak enkel glas en minimale isolatie in originele staat."
    elif jaar < 1992:
        level, chip, msg = "neutral", "jaren '70-'80", "Eerste isolatienormen; spouwmuur vaak aanwezig, dubbel glas wisselend."
    elif jaar < 2012:
        level, chip, msg = "good", "moderne bouw", "Bouwbesluit 1992+; redelijke isolatie en HR-beglazing standaard."
    else:
        level, chip, msg = "good", "nieuwbouw", "Moderne eisen, vaak bijna-energie-neutraal (BENG 2020+)."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=None,
        norm=None,
        betekenis=msg,
    )


def ref_oppervlakte(m2: Optional[int], is_woning: bool = True) -> Optional[Reference]:
    """Gebruiksoppervlakte in m². NL-gem 2024: 120 m² gemiddelde woning."""
    if m2 is None:
        return None
    if not is_woning:
        return Reference(
            chip_level="neutral",
            chip_text="niet-woning",
            nl_gemiddelde=None,
            norm=None,
            betekenis="Object is geen woonfunctie; vergelijking met woningen niet zinvol.",
        )
    nl = 120
    if m2 < 60:
        level, chip, msg = "neutral", "compact", "Studio of klein appartement; veel in binnensteden."
    elif m2 < 100:
        level, chip, msg = "neutral", "klein", "Onder NL-gemiddelde; typisch tussenwoning of stadsappartement."
    elif m2 < 140:
        level, chip, msg = "neutral", "gemiddeld", "Rond NL-gemiddelde voor een gezinswoning."
    elif m2 < 200:
        level, chip, msg = "good", "ruim", "Boven gemiddeld; veel woonruimte."
    else:
        level, chip, msg = "good", "zeer ruim", "Fors vastgoed; vrijstaand of karakteristiek groot pand."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"{nl} m² (NL-gemiddelde)",
        norm=None,
        betekenis=msg,
    )


def ref_energielabel(klasse: Optional[str]) -> Optional[Reference]:
    """Energielabel A+++++ .. G. NL-gem 2024: dichtbij C."""
    if not klasse:
        return None
    # Map label -> level; hoe meer + hoe groener
    plusses = klasse.count("+")
    if klasse.startswith("A") and plusses >= 3:
        level, chip, msg = "good", "topklasse", "BENG/nul-op-de-meter niveau; zeer lage energielasten."
    elif klasse.startswith("A"):
        level, chip, msg = "good", "zeer zuinig", "Goed geïsoleerd, lage energierekening."
    elif klasse == "B":
        level, chip, msg = "good", "zuinig", "Boven gemiddelde isolatie; acceptabel voor modern gebruik."
    elif klasse == "C":
        level, chip, msg = "neutral", "gemiddeld", "Typisch label voor NL-woningvoorraad; basisisolatie aanwezig."
    elif klasse == "D":
        level, chip, msg = "neutral", "matig", "Verbeterpotentieel; verduurzaming vaak rendabel."
    elif klasse == "E":
        level, chip, msg = "warn", "onzuinig", "Slechte isolatie; hoge energiekosten en hypotheekcorrectie mogelijk."
    elif klasse in ("F", "G"):
        level, chip, msg = "warn", "zeer onzuinig", "Ongeïsoleerd; niet-woningen mogen na 2023 verboden zijn. Verduurzaming urgent."
    else:
        level, chip, msg = "neutral", klasse, ""
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="NL-gemiddelde ≈ C",
        norm="A+++++ (zeer zuinig) tot G (zeer onzuinig)",
        betekenis=msg,
    )


def ref_pm25(ug_m3: Optional[float]) -> Optional[Reference]:
    """PM2.5 — 'meer = slechter'. WHO 2021: 5. EU: 25. NL-gem 2024: ~10.

    Chip-kleur volgt gezondheidsimpact: lager = groen, boven NL-gem = rood.
    """
    if ug_m3 is None:
        return None
    who, eu, nl = 5, 25, 10
    if ug_m3 <= who:
        level, chip = "good", "binnen WHO-advies"
        betekenis = "Uitstekende luchtkwaliteit; zeldzaam in NL."
    elif ug_m3 <= nl:
        level, chip = "good", "onder of rond NL-gemiddelde"
        betekenis = (
            "Vergelijkbaar met grote delen van Nederland. Boven WHO-advies, "
            "maar ruim onder EU-norm."
        )
    elif ug_m3 <= eu:
        level, chip = "warn", "boven NL-gemiddelde"
        betekenis = (
            "Verhoogde blootstelling. Langdurig effect: verhoogd risico op "
            "long- en hartaandoeningen."
        )
    else:
        level, chip = "warn", "boven EU-norm"
        betekenis = "Structureel te hoge blootstelling; gezondheidseffect fors."
    return Reference(
        chip_level=level, chip_text=chip,
        nl_gemiddelde=f"{nl} µg/m³",
        norm=f"WHO {who} · EU {eu} µg/m³",
        betekenis=betekenis,
    )


def ref_no2(ug_m3: Optional[float]) -> Optional[Reference]:
    """NO2 — meer = slechter, vooral verkeer-gerelateerd."""
    if ug_m3 is None:
        return None
    who, eu, nl = 10, 40, 15
    if ug_m3 <= who:
        level, chip = "good", "binnen WHO-advies"
        betekenis = "Weinig verkeersinvloed; karakteristiek voor platteland."
    elif ug_m3 <= nl:
        level, chip = "good", "onder of rond NL-gemiddelde"
        betekenis = "Normale stedelijke achtergrond; geen directe actie nodig."
    elif ug_m3 <= eu:
        level, chip = "warn", "boven NL-gemiddelde"
        betekenis = (
            "Typisch voor locaties dicht bij een drukke weg. Binnen de EU-norm "
            "maar ruim boven WHO-advies."
        )
    else:
        level, chip = "warn", "boven EU-norm"
        betekenis = "Structureel te hoog; meestal vlakbij zware verkeersader."
    return Reference(
        chip_level=level, chip_text=chip,
        nl_gemiddelde=f"{nl} µg/m³",
        norm=f"WHO {who} · EU {eu} µg/m³",
        betekenis=betekenis,
    )


def ref_pm10(ug_m3: Optional[float]) -> Optional[Reference]:
    """PM10 — meer = slechter."""
    if ug_m3 is None:
        return None
    who, eu, nl = 15, 40, 17
    if ug_m3 <= who:
        level, chip, msg = "good", "binnen WHO-advies", "Zeldzaam goed voor NL."
    elif ug_m3 <= nl:
        level, chip, msg = "good", "onder of rond NL-gemiddelde", "Gebruikelijk voor NL."
    elif ug_m3 <= eu:
        level, chip, msg = "warn", "boven NL-gemiddelde", "Hoger dan gemiddeld."
    else:
        level, chip, msg = "warn", "boven EU-norm", "Structureel verhoogd."
    return Reference(
        chip_level=level, chip_text=chip,
        nl_gemiddelde=f"{nl} µg/m³",
        norm=f"WHO {who} · EU {eu} µg/m³",
        betekenis=msg,
    )


# ---------------------------------------------------------------------------
# Klimaatrisico (sectie 6)
# ---------------------------------------------------------------------------

def ref_paalrot(pct_sterk: Optional[float], pct_mild: Optional[float]) -> Optional[Reference]:
    """Paalrot — uitdrogen van houten palen in veengrond."""
    if pct_sterk is None and pct_mild is None:
        return None
    pct = pct_sterk if pct_sterk is not None else (pct_mild or 0)
    if pct == 0:
        level, chip, msg = "good", "geen risico", "Geen houten palen in veen."
    elif pct < 10:
        level, chip, msg = "good", "laag", "Weinig panden kwetsbaar."
    elif pct < 40:
        level, chip, msg = "warn", "verhoogd", (
            "Houten palen drogen uit door dalend grondwater — kans op "
            "verzakking. Funderingsinspectie bij aankoop verstandig."
        )
    elif pct < 80:
        level, chip, msg = "warn", "hoog", (
            "Veel panden op houten palen in veen. Uitdroging = verzakking. "
            "Herstel €40-100k per woning."
        )
    else:
        level, chip, msg = "warn", "zeer hoog", (
            "Bijna alle panden op houten palen in uitdrogende veengrond. "
            "Funderingsonderzoek sterk aangeraden."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="~15% (NL)",
        norm="bij sterk klimaatscenario 2050",
        betekenis=msg,
    )


def ref_verschilzetting(pct: Optional[float]) -> Optional[Reference]:
    """Verschilzetting — ongelijkmatige zakking op zand/klei-overgangen."""
    if pct is None:
        return None
    if pct == 0:
        level, chip, msg = "good", "geen risico", "Stabiele ondergrond."
    elif pct < 10:
        level, chip, msg = "good", "laag", "Weinig panden kwetsbaar."
    elif pct < 40:
        level, chip, msg = "warn", "verhoogd", (
            "Ondergrond zakt ongelijkmatig — kans op scheuren in muren "
            "en scheve vloeren. Bouwtechnisch onderzoek bij aankoop."
        )
    elif pct < 80:
        level, chip, msg = "warn", "hoog", (
            "Veel panden op onstabiele ondergrond die ongelijkmatig zakt. "
            "Scheuren en verzakkingen komen voor. Herstel €30-80k."
        )
    else:
        level, chip, msg = "warn", "zeer hoog", (
            "Bijna alle panden zakken ongelijkmatig door bodemverschillen. "
            "Let op scheuren en klemmende deuren."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="~10% (NL)",
        norm="bij sterk klimaatscenario 2050",
        betekenis=msg,
    )


def ref_overstromingskans(klasse: Optional[int]) -> Optional[Reference]:
    """Plaatsgebonden overstromingskans (rivier/zee/dijkdoorbraak)."""
    if klasse is None:
        return None
    teksten = {
        1: ("good", "zeer laag", "Praktisch geen kans op overstroming."),
        2: ("good", "laag", "Overstroming is een extreem zeldzaam scenario."),
        3: ("neutral", "middel", "Gemiddeld voor NL; dijk- of duinbescherming volstaat."),
        4: ("warn", "verhoogd", "Rivier-uiterwaard of laaggelegen polder. Check opstalverzekering."),
        5: ("warn", "zeer hoog", "Hoogrisico-gebied; verzekering vaak beperkt."),
    }
    level, chip, msg = teksten.get(klasse, ("neutral", "onbekend", ""))
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="klasse 2-3 voor meeste woningen",
        norm="1=zeer laag · 5=zeer hoog",
        betekenis=msg,
    )


def ref_overstromingsdiepte(cm: Optional[float]) -> Optional[Reference]:
    """Maximale overstromingsdiepte bij rampscenario.

    NL-context (CAS):
      - ~60% van NL: <10 cm (geen risico, achter dijk/hoog)
      - ~20% NL: 10-50 cm (ondiepe polders)
      - ~15% NL: 0.5-2 m (diepere polders, sommige uiterwaarden)
      - ~5% NL: >2 m (diepe polders, Maas/IJssel uiterwaarden)
    """
    if cm is None or cm <= 0:
        return None
    m = cm / 100
    if m < 0.1:
        level, chip, msg = "good", "droog", "Huis blijft praktisch droog."
        nl = "onder NL-gemiddelde (meeste NL <10 cm)"
    elif m < 0.5:
        level, chip, msg = "neutral", "ondiep", (
            f"Tot ~{m:.1f} m water op straat; woning meestal gespaard."
        )
        nl = "rond NL-gemiddelde voor lager gelegen gebied"
    elif m < 1.5:
        level, chip, msg = "warn", "middelhoog", (
            f"Tot ~{m:.1f} m water. Begane grond onder water; flinke schade."
        )
        nl = "boven NL-gemiddelde (top ~25% diepste)"
    else:
        level, chip, msg = "warn", "diep", (
            f"Tot ~{m:.1f} m water. Hele begane grond onder water."
        )
        nl = "ver boven NL-gemiddelde (top ~5% diepste)"
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=nl,
        norm="bij rampscenario",
        betekenis=msg,
    )


def ref_droogtestress(klasse: Optional[int]) -> Optional[Reference]:
    """Droogtestress-klasse 1-5."""
    if klasse is None:
        return None
    teksten = {
        1: ("good", "zeer laag", "Voldoende grondwater; tuin en bomen gezond."),
        2: ("good", "laag", "Marginale droogte alleen in extreme zomers."),
        3: ("neutral", "middel", "Tuin extra water geven in droge zomer."),
        4: ("warn", "verhoogd", "Bomen en tuin kwetsbaar in droge zomers; op zand ook verzakkingsrisico."),
        5: ("warn", "zeer hoog", "Bodem droogt sterk uit — schade aan groen en fundering mogelijk."),
    }
    level, chip, msg = teksten.get(klasse, ("neutral", "onbekend", ""))
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="klasse 2-3 voor meeste gebieden",
        norm="1=zeer laag · 5=zeer hoog",
        betekenis=msg,
    )


def ref_bodemdaling(mm_per_jaar: Optional[float]) -> Optional[Reference]:
    """Bodemdaling in mm/jaar (vooral veenweide-gebieden)."""
    if mm_per_jaar is None or mm_per_jaar <= 0:
        return None
    per_jaar = mm_per_jaar
    if per_jaar < 1:
        level, chip, msg = "good", "stabiel", "Geen noemenswaardige daling."
    elif per_jaar < 3:
        level, chip, msg = "neutral", "licht", f"~{per_jaar:.1f} mm/jaar; merkbaar op lange termijn."
    elif per_jaar < 6:
        level, chip, msg = "warn", "merkbaar", (
            f"~{per_jaar:.1f} mm/jaar (~{per_jaar*10:.0f} cm/10 jaar). "
            "Scheve vloeren, druk op fundering en riool."
        )
    else:
        level, chip, msg = "warn", "sterk", (
            f">{per_jaar:.1f} mm/jaar. Snelle daling — structurele impact."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="<1 mm/jaar buiten veen",
        norm=None,
        betekenis=msg,
    )


def ref_wateroverlast_neerslag(cm: Optional[float]) -> Optional[Reference]:
    """Waterdiepte op straat bij T=100 stortbui."""
    if cm is None or cm <= 0:
        return None
    if cm < 5:
        level, chip, msg = "good", "droog", "Straat blijft droog bij piek-regen."
    elif cm < 15:
        level, chip, msg = "neutral", "licht", f"Tot ~{cm:.0f} cm op straat bij stortbui."
    elif cm < 30:
        level, chip, msg = "warn", "matig", f"Tot ~{cm:.0f} cm; water kan kruipruimte of voordeur bereiken."
    else:
        level, chip, msg = "warn", "hoog", f"Tot ~{cm:.0f} cm; hoog risico op binnendringen."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="~5 cm stedelijk",
        norm="bij T=100 piek-regen",
        betekenis=msg,
    )


def ref_hittestress(klasse: Optional[int]) -> Optional[Reference]:
    """Hittestress klasse 1-5 (Klimaateffectatlas).

    Klasse meet aantal 'warme nachten' (>20°C) per zomer. Klimaat-NL 2024:
    gemiddeld 5 tropische nachten per zomer in de Randstad.
    """
    if klasse is None:
        return None
    teksten = {
        1: ("good", "zeer laag", "Koele buurt — veel groen, weinig bebouwing. Tropische nachten zeldzaam."),
        2: ("good", "laag", "Aangename zomernachten; meer groen dan gemiddeld."),
        3: ("neutral", "rond NL-gemiddelde", "Gemiddelde hitte-stress. In zomers 5-10 tropische nachten (>20°C)."),
        4: ("warn", "verhoogd", "Sterk urban heat island-effect. Airco of goede ventilatie aan te raden."),
        5: ("warn", "zeer hoog", "Extreme warmte-opbouw. Zwakke of oudere bewoners krijgen serieuze last in zomers."),
    }
    level, chip, msg = teksten.get(klasse, ("neutral", "onbekend", ""))
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="klasse 3 (5-10 tropische nachten/zomer)",
        norm="1=zeer laag · 5=zeer hoog",
        betekenis=msg,
    )


# ---------------------------------------------------------------------------
# Wijk-economie (sectie 2)
# ---------------------------------------------------------------------------

def ref_woz(eur: Optional[int]) -> Optional[Reference]:
    """WOZ-waarde. NL-gemiddelde 2024: €404.000 per woning (CBS)."""
    if eur is None:
        return None
    nl = 404_000
    ratio = eur / nl
    if ratio < 0.7:
        level, chip = "neutral", "onder NL-gemiddelde"
        msg = "Lagere instapprijs, vaak rustigere wijken of kleinere woningen."
    elif ratio < 1.2:
        level, chip = "neutral", "rond NL-gemiddelde"
        msg = "Typische NL-woningwaarde."
    elif ratio < 1.7:
        level, chip = "good", "boven NL-gemiddelde"
        msg = "Bovengemiddelde waardebuurt; vaak gewilde ligging."
    else:
        level, chip = "good", "ver boven NL-gemiddelde"
        msg = "Kapitaalkrachtige buurt; doorgaans hoge voorzieningen-dichtheid."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"€{nl:,}".replace(",", "."),
        norm=None,
        betekenis=msg,
    )


def ref_inkomen(eur: Optional[int]) -> Optional[Reference]:
    """Gemiddeld inkomen per inwoner. NL-gemiddelde 2024: €34.100 (CBS)."""
    if eur is None:
        return None
    nl = 34_100
    ratio = eur / nl
    if ratio < 0.7:
        level, chip = "warn", "onder NL-gemiddelde"
        msg = "Lager inkomensniveau; vaker financieel kwetsbare huishoudens."
    elif ratio < 1.15:
        level, chip = "neutral", "rond NL-gemiddelde"
        msg = "Typisch Nederlands inkomensniveau."
    elif ratio < 1.5:
        level, chip = "good", "boven NL-gemiddelde"
        msg = "Bovengemiddeld inkomen; meer koopkracht en bestedingsruimte."
    else:
        level, chip = "good", "ver boven NL-gemiddelde"
        msg = "Welvarende buurt met kapitaalkrachtige bewoners."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"€{nl:,}".replace(",", "."),
        norm=None,
        betekenis=msg,
    )


def ref_opleiding_hoog(pct: Optional[float]) -> Optional[Reference]:
    """Percentage hbo/wo-opgeleiden van volwassen bevolking (15-75 jaar).

    NL-gemiddelde 2024: ~34% (CBS Statistiek van het onderwijsniveau).
    """
    if pct is None:
        return None
    nl = 34
    if pct < nl - 10:
        level, chip = "neutral", "onder NL-gemiddelde"
        msg = "Voornamelijk praktijkgericht opgeleide bewoners (vmbo/mbo)."
    elif pct < nl + 10:
        level, chip = "neutral", "rond NL-gemiddelde"
        msg = "Gemengd opleidingsprofiel, typerend voor Nederlandse wijken."
    elif pct < nl + 25:
        level, chip = "good", "boven NL-gemiddelde"
        msg = "Relatief veel hbo/wo-opgeleiden; vaak academisch/kennis-georiënteerde wijk."
    else:
        level, chip = "good", "ver boven NL-gemiddelde"
        msg = "Zeer hoog aandeel hoger opgeleiden — typerend voor universiteitssteden en kapitaalkrachtige wijken."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"~{nl}%",
        norm="hbo + wo samen",
        betekenis=msg,
    )


def ref_arbeidsparticipatie(pct: Optional[float]) -> Optional[Reference]:
    """Netto arbeidsparticipatie. NL-gemiddelde 2024: 73.4% (CBS)."""
    if pct is None:
        return None
    nl = 73.4
    if pct < nl - 5:
        level, chip = "warn", "onder NL-gemiddelde"
        msg = "Lagere arbeidsparticipatie; vaker mensen met uitkering of pensioen."
    elif pct < nl + 5:
        level, chip = "neutral", "rond NL-gemiddelde"
        msg = "Gemengde wijk qua economische vitaliteit."
    else:
        level, chip = "good", "boven NL-gemiddelde"
        msg = "Economisch vitale buurt; veel werkende bewoners."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"{nl}%",
        norm=None,
        betekenis=msg,
    )


# ---------------------------------------------------------------------------
# Veiligheid (sectie 4)
# ---------------------------------------------------------------------------

def ref_woninginbraak(per_1000: Optional[float]) -> Optional[Reference]:
    """Woninginbraken per 1.000 inwoners — meer = slechter.

    NL-gemiddelde 2024: ~2.3 woninginbraken per 1.000 inwoners per jaar.
    """
    if per_1000 is None:
        return None
    nl = 2.3
    if per_1000 < nl * 0.5:
        level, chip, msg = "good", "ruim onder NL-gemiddelde", "Veilige buurt qua inbraakcijfers."
    elif per_1000 < nl * 1.2:
        level, chip, msg = "good", "rond NL-gemiddelde", "Typisch inbraakcijfer voor NL."
    elif per_1000 < nl * 2:
        level, chip, msg = "warn", "boven NL-gemiddelde", (
            "Verhoogde inbraakkans; goede sloten en eventueel alarm aan te raden."
        )
    else:
        level, chip, msg = "warn", "ver boven NL-gemiddelde", (
            "Zeer hoog inbraakcijfer; wijk-preventieplan of buurtapp vaak actief."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"{nl} per 1.000 inw/jaar",
        norm=None,
        betekenis=msg,
    )


def ref_eigendomsverhouding(
    koop_pct: Optional[float],
    sociale_pct: Optional[float],
    particulier_pct: Optional[float],
) -> Optional[Reference]:
    """Karakterisering van de wijk op basis van eigendomsverhouding.

    Geen goed/slecht — we kenschetsen het type buurt:
      - Koop-dominant (>60%): stabiele bewoners, lange doorlooptijd
      - Corporatie-dominant (>50%): gemengde doelgroepen, sociale woningmix
      - Particuliere-huur-dominant (>40%): expats/studenten/flex, hoge doorstroom
      - Anders: gemengde wijk

    Daarom chip_level = 'neutral'; het label beschrijft het karakter.
    """
    if koop_pct is None and sociale_pct is None and particulier_pct is None:
        return None
    k = koop_pct or 0
    s = sociale_pct or 0
    p = particulier_pct or 0
    if k >= 60:
        chip, msg = "koop-dominant", (
            "Dominant koopbuurt — stabiele bewoners, vaak lange verblijfsduur "
            "en actieve VvE's. Lage doorstroom."
        )
    elif s >= 50:
        chip, msg = "corporatie-wijk", (
            "Vooral sociale huur (corporaties). Gemengde doelgroepen en vaak "
            "meer stedelijke diversiteit in inkomen en achtergrond."
        )
    elif p >= 40:
        chip, msg = "particuliere-huur-hotspot", (
            "Veel particuliere verhuur — typerend voor centrumgebieden met "
            "expats, studenten en tijdelijke bewoners. Hoge doorstroom."
        )
    elif k >= 45 and s + p >= 35:
        chip, msg = "gemengd koop + huur", (
            "Gebalanceerde mix van koop en huur — gemengde wijk met zowel "
            "stabiele bewoners als doorstroming."
        )
    else:
        chip, msg = "gemengd", (
            "Gemengde eigendomssamenstelling zonder één dominante vorm."
        )
    return Reference(
        chip_level="neutral",
        chip_text=chip,
        nl_gemiddelde="NL: 58% koop · 28% sociale huur · 14% particulier",
        norm=None,
        betekenis=msg,
    )


def ref_geweld(per_1000: Optional[float]) -> Optional[Reference]:
    """Geweldsmisdrijven per 1.000 inw (mishandeling + bedreiging + straatroof
    + openlijk geweld + overval) over 12 maanden.

    NL-gemiddelde 2024: ~5 per 1.000 inw/jaar. Dit is de 'persoonlijke
    veiligheid'-indicator — zegt iets over hoe veilig je je op straat voelt,
    in tegenstelling tot totaal-misdrijven dat vervuild is door tourist-delicten.
    """
    if per_1000 is None:
        return None
    nl = 5.0
    if per_1000 < nl * 0.6:
        level, chip, msg = "good", "ruim onder NL-gemiddelde", (
            "Lage geweldsdruk — rustige wijk qua persoonlijke veiligheid."
        )
    elif per_1000 < nl * 1.3:
        level, chip, msg = "good", "rond NL-gemiddelde", (
            "Gemiddeld geweldsniveau — typisch voor stedelijk NL."
        )
    elif per_1000 < nl * 2.2:
        level, chip, msg = "warn", "boven NL-gemiddelde", (
            "Verhoogd geweldsniveau — aandachtspunt voor persoonlijke veiligheid, "
            "vooral 's avonds."
        )
    else:
        level, chip, msg = "warn", "ver boven NL-gemiddelde", (
            "Hoog geweldsniveau — niet vanzelfsprekend veilig op straat, "
            "met name rond uitgaans- of stationsgebieden."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"~{nl} per 1.000 inw/jaar",
        norm=None,
        betekenis=msg,
    )


def ref_fietsendiefstal(per_1000: Optional[float]) -> Optional[Reference]:
    """Fietsen- en bromfietsendiefstal per 1.000 inw (12 maanden).

    NL-gemiddelde 2024: ~20 per 1.000 inw/jaar (~500K fietsen/jaar ÷ 17.9M
    inwoners × 1.000, afgerond met correctie voor onderrapportage). Sterk
    plaats-afhankelijk: centrumgebieden / stations zitten structureel hoger.

    Deze indicator is vooral een proxy voor sociale controle — hoge cijfers
    signaleren doorgangswijken zonder betrokken bewoners.
    """
    if per_1000 is None:
        return None
    nl = 20.0
    if per_1000 < nl * 0.5:
        level, chip, msg = "good", "ruim onder NL-gemiddelde", (
            "Weinig fietsendiefstal — je fiets kan hier met één goed slot rustig buiten."
        )
    elif per_1000 < nl * 1.3:
        level, chip, msg = "good", "rond NL-gemiddelde", (
            "Typisch stedelijk — één goed slot volstaat meestal."
        )
    elif per_1000 < nl * 2.5:
        level, chip, msg = "warn", "boven NL-gemiddelde", (
            "Verhoogd risico — twee sloten zijn hier de norm, geen dure fiets buiten laten staan."
        )
    else:
        level, chip, msg = "warn", "ver boven NL-gemiddelde", (
            "Zeer hoog risico — typerend voor station-, centrum- of uitgaansgebied. "
            "Ook een signaal van lage sociale controle in de buurt."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"~{nl} per 1.000 inw/jaar",
        norm=None,
        betekenis=msg,
    )


def ref_totaal_misdrijven(per_1000: Optional[float]) -> Optional[Reference]:
    """Totaal misdrijven per 1.000 inwoners over 12 maanden.

    LET OP: winkel- en horecagebieden hebben structureel veel hogere cijfers
    per-inwoner omdat er dagbezoekers worden meegeteld maar niet als inwoner.
    Damrak bv. = ~900/1000 inw, want 500 bewoners vs. miljoenen bezoekers.
    """
    if per_1000 is None:
        return None
    nl = 40  # NL-gem 2024, ruwweg
    if per_1000 < nl * 0.5:
        level, chip, msg = "good", "ruim onder NL-gemiddelde", "Rustige residentiële wijk."
    elif per_1000 < nl * 1.5:
        level, chip, msg = "good", "rond NL-gemiddelde", "Normaal stedelijk niveau."
    elif per_1000 < nl * 3:
        level, chip, msg = "warn", "boven NL-gemiddelde", (
            "Hoger dan gemiddeld; vaak door nabijheid van uitgaansgebied of station."
        )
    else:
        level, chip, msg = "warn", "ver boven NL-gemiddelde", (
            "Waarschijnlijk toeristisch / uitgaansgebied (bezoekers tellen mee, bewoners niet). "
            "Cijfer zegt minder over persoonlijke veiligheid dan je denkt."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"~{nl} per 1.000 inw/jaar",
        norm=None,
        betekenis=msg,
    )


# ---------------------------------------------------------------------------
# Buren (sectie 3)
# ---------------------------------------------------------------------------

def ref_met_kinderen(pct: Optional[float]) -> Optional[Reference]:
    """Percentage huishoudens met kinderen. NL-gem 2024: ~34% (CBS)."""
    if pct is None:
        return None
    nl = 34
    if pct < nl - 15:
        level, chip = "neutral", "onder NL-gemiddelde"
        msg = "Weinig gezinnen met kinderen; stadscentrum- of seniorenwijk."
    elif pct < nl - 5:
        level, chip = "neutral", "iets onder NL-gemiddelde"
        msg = "Minder gezinnen dan gemiddeld."
    elif pct < nl + 10:
        level, chip = "neutral", "rond NL-gemiddelde"
        msg = "Gemengde samenstelling; normaal NL-profiel."
    else:
        level, chip = "neutral", "boven NL-gemiddelde"
        msg = "Gezinsvriendelijke buurt met veel kinderen."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"~{nl}%",
        norm=None,
        betekenis=msg,
    )


def ref_huishoudensgrootte(grootte: Optional[float]) -> Optional[Reference]:
    """Gemiddelde huishoudensgrootte.

    Karakterisering, geen goed/slecht:
      ≤ 1.5 : dominant singles — stadswijk of seniorenflat
      1.5-2.0 : gemengd — veel stellen zonder kinderen en singles
      2.0-2.4 : gezinnen + andere types gemengd — typische NL-wijk
      > 2.4 : dominant gezinnen met kinderen — suburbane / landelijke gezinsbuurt
    NL-gemiddelde 2024: ~2.1 personen.
    """
    if grootte is None:
        return None
    if grootte <= 1.5:
        chip, msg = "veel singles", (
            "Kleine huishoudens domineren — stadswijk, studentenbuurt of "
            "appartementen voor ouderen. Weinig gezinscultuur."
        )
    elif grootte <= 2.0:
        chip, msg = "singles + stellen", (
            "Mix van singles en stellen; beperkt aantal gezinnen."
        )
    elif grootte <= 2.4:
        chip, msg = "gemengde samenstelling", (
            "Typische NL-mix van gezinnen, stellen en singles."
        )
    else:
        chip, msg = "gezinsbuurt", (
            "Dominant gezinnen met kinderen — suburbaan of landelijk "
            "gezinsprofiel."
        )
    return Reference(
        chip_level="neutral",
        chip_text=chip,
        nl_gemiddelde="2.1 personen (NL-gemiddelde)",
        norm=None,
        betekenis=msg,
    )


def ref_migratieachtergrond(
    pct_nederlands: Optional[float],
    pct_westers: Optional[float],
    pct_niet_westers: Optional[float],
) -> Optional[Reference]:
    """Karakterisering van culturele samenstelling van de wijk (peiljaar 2020).

    Geen goed/slecht oordeel — we beschrijven hoe gemengd de wijk is.
    NL-gemiddelde 2020: ~76% Nederlands, ~10% westers, ~14% niet-westers.

    Categorieën:
      - Homogeen NL-achtergrond (>85% NL)
      - Gemengd met internationale inslag (NL 65-85%, westers hoog)
      - Sterk gemengde wijk (NL < 60%, hoog aandeel niet-westers)
      - Dominant niet-westers (niet_westers > 45%)
    """
    if pct_nederlands is None and pct_westers is None and pct_niet_westers is None:
        return None
    nl = pct_nederlands or 0
    w = pct_westers or 0
    nw = pct_niet_westers or 0

    if nw >= 45:
        chip, msg = "overwegend niet-westerse mix", (
            "Grote groep bewoners met niet-westerse migratieachtergrond. "
            "Kenmerkend voor sommige wijken in de grote steden."
        )
    elif nl >= 85:
        chip, msg = "overwegend Nederlandse achtergrond", (
            "Weinig culturele diversiteit. Typisch voor kleinere gemeenten "
            "en dorpen buiten de Randstad."
        )
    elif nl >= 70 and w >= 10:
        chip, msg = "Nederlands met internationale inslag", (
            "Overwegend Nederlandse bevolking met een internationale "
            "component (expats, EU-migranten)."
        )
    elif nl < 55 and nw >= 25:
        chip, msg = "sterk cultureel gemengd", (
            "Gemengde wijk met veel verschillende achtergronden. Typisch "
            "voor stadswijken met grote internationale aanwezigheid."
        )
    else:
        chip, msg = "gemengde samenstelling", (
            "Mix van Nederlandse, westerse en niet-westerse achtergronden "
            "zonder één dominante groep."
        )
    return Reference(
        chip_level="neutral",
        chip_text=chip,
        nl_gemiddelde="NL: ~76% Nederlands · ~10% westers · ~14% niet-westers",
        norm="peiljaar 2020",
        betekenis=msg,
    )


def ref_leeftijdsprofiel(
    pct_jong: Optional[float],
    pct_midden: Optional[float],
    pct_oud: Optional[float],
) -> Optional[Reference]:
    """Karakterisering op basis van 3 leeftijdsklassen (0-15 / 15-65 / 65+).

    Proxyt voor het type buurt:
      - Hoog % jong (>20%) met veel gezinnen → gezinsbuurt
      - Hoog % midden (>70%) → jongvolwassen stadswijk
      - Hoog % 65+ (>25%) → vergrijsde wijk
      - Anders → gemengd

    NL-gemiddelde (2024): ~16% jong, ~64% midden, ~20% 65+.
    """
    if pct_jong is None and pct_midden is None and pct_oud is None:
        return None
    j, m, o = (pct_jong or 0), (pct_midden or 0), (pct_oud or 0)
    if o >= 25 and j < 14:
        chip, msg = "vergrijsde wijk", (
            "Aanzienlijk ouderenaandeel — rustiger buurt, vaak "
            "seniorencomplexen of van-oudsher gevestigde bewoners."
        )
    elif j >= 20 and m >= 55:
        chip, msg = "gezinsbuurt", (
            "Veel jonge kinderen — typisch voor suburbane wijken met "
            "scholen, speelplekken en gezinswoningen."
        )
    elif m >= 72 and j < 12:
        chip, msg = "jongvolwassen stadswijk", (
            "Dominante 25-45 groep; stad-centrum-kenmerken met singles, "
            "stellen en weinig kinderen."
        )
    elif abs(j - 16) <= 4 and abs(o - 20) <= 5:
        chip, msg = "NL-gemiddeld leeftijdsprofiel", (
            "Gevarieerde mix van generaties, dicht bij het landelijk "
            "gemiddelde."
        )
    else:
        chip, msg = "gemengd leeftijdsprofiel", (
            "Mix van generaties zonder één dominante groep."
        )
    return Reference(
        chip_level="neutral",
        chip_text=chip,
        nl_gemiddelde="NL: ~16% jong · ~64% midden · ~20% 65+",
        norm=None,
        betekenis=msg,
    )


def ref_dichtheid(per_km2: Optional[int]) -> Optional[Reference]:
    """Bevolkingsdichtheid per km². NL-gem 2024: 527/km². Grote steden >5000."""
    if per_km2 is None:
        return None
    if per_km2 < 200:
        level, chip, msg = "neutral", "landelijk", "Platteland of dorp; veel open ruimte, weinig bebouwing."
    elif per_km2 < 1500:
        level, chip, msg = "neutral", "voorstedelijk", "Buitenwijk of kleine stad; rustiger tempo."
    elif per_km2 < 5000:
        level, chip, msg = "neutral", "stedelijk", "Regulier stedelijk milieu."
    elif per_km2 < 10000:
        level, chip, msg = "neutral", "sterk stedelijk", "Dichte stedelijke bebouwing, veel voorzieningen dichtbij."
    else:
        level, chip, msg = "neutral", "zeer sterk stedelijk", "Binnenstad of grootstedelijk centrum; maximale nabijheid + bruis."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde="527 per km² (NL-gemiddelde)",
        norm=None,
        betekenis=msg,
    )


def ref_inwoners(n: Optional[int]) -> Optional[Reference]:
    """Aantal inwoners in de buurt. Contextualiseert schaal: klein/middel/groot."""
    if n is None:
        return None
    if n < 500:
        level, chip, msg = "neutral", "kleine buurt", "Intiem schaalniveau; buren kennen elkaar vaak persoonlijk."
    elif n < 2000:
        level, chip, msg = "neutral", "middelgrote buurt", "Typische NL-buurtomvang met lokale betrokkenheid."
    elif n < 5000:
        level, chip, msg = "neutral", "grote buurt", "Meerdere straten; vaak met eigen voorzieningen."
    else:
        level, chip, msg = "neutral", "zeer grote buurt", "Omvang van een kleine wijk; meerdere bevolkingsgroepen."
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=None,
        norm="klassen: <500 klein · 500-2000 middel · 2000+ groot",
        betekenis=msg,
    )


def ref_eenpersoons(pct: Optional[float]) -> Optional[Reference]:
    """Percentage eenpersoonshuishoudens. NL-gem 2024: 38%."""
    if pct is None:
        return None
    nl = 38
    if pct < nl - 10:
        level, chip, msg = "neutral", "onder NL-gemiddelde", "Overwegend gezinnen en stellen."
    elif pct < nl + 10:
        level, chip, msg = "neutral", "rond NL-gemiddelde", "Gemengde samenstelling huishoudens."
    else:
        level, chip, msg = "neutral", "boven NL-gemiddelde", (
            "Veel singles, stadscentrum-kenmerken. Meestal dynamisch maar minder familiefocus."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"{nl}%",
        norm=None,
        betekenis=msg,
    )
