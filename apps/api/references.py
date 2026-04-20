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
    """Funderingsrisico — meest financieel-kritieke parameter voor huizenbezitters.

    Gemiddeld NL: ~15% panden hebben paalrot-risico onder 'sterk' klimaat-
    scenario. In veengebieden (West-NL) vaak 50-100%, op zandgrond (Oost-NL) ~0%.
    """
    if pct_sterk is None and pct_mild is None:
        return None
    pct = pct_sterk if pct_sterk is not None else (pct_mild or 0)
    nl_gem = 15
    if pct == 0:
        level, chip, msg = "good", "geen risico", "Stabiele ondergrond (meestal zandgrond)."
    elif pct < 10:
        level, chip, msg = "good", "onder NL-gemiddelde", "Enkele panden in buurt potentieel kwetsbaar."
    elif pct < 40:
        level, chip, msg = "warn", "boven NL-gemiddelde", (
            "Moeilijk te voorspellen per individueel pand; laat funderingsinspectie overwegen "
            "bij aankoop of verbouwing."
        )
    elif pct < 80:
        level, chip, msg = "warn", "verhoogd buurt-risico", (
            "Groot deel panden kan funderingsschade oplopen. Hypotheekverstrekkers kijken hier "
            "scherp naar. Herstel kost doorgaans €40-100k per woning."
        )
    else:
        level, chip, msg = "warn", "zeer hoog buurt-risico", (
            "Bijna alle panden in buurt staan op houten palen in uitdrogende veengrond. "
            "Fundering-onderzoek en eventueel herstel-reservering sterk aangeraden."
        )
    return Reference(
        chip_level=level,
        chip_text=chip,
        nl_gemiddelde=f"{nl_gem}%",
        norm="bij sterk klimaatscenario 2050",
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
