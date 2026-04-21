"""
Sociale betekenis-laag — hergroepeert de 6 datasecties rondom 3 menselijke
vragen die mensen écht stellen bij een woningkeuze:

  1. "Is het hier veilig voor mijn kinderen?"
  2. "Wat kost wonen hier?"
  3. "Is dit een goede langetermijn-investering?"

Elke vraag combineert 3-5 indicatoren uit de bestaande scan en geeft:
  - verdict: good / neutral / warn
  - samenvatting (één zin in mensentaal)
  - factoren-lijst (de individuele waarden die het verdict dragen)

Pure data-transformatie; geen extra API-calls.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class Factor:
    """Eén bouwsteen onder een sociale vraag."""

    label: str           # "Inbraken in buurt"
    value_text: str      # "4.0 per 1.000 inwoners"
    level: str           # 'good' / 'neutral' / 'warn'


@dataclass
class SocialeVraag:
    vraag: str
    icoon: str           # emoji om de vraag visueel te ankeren
    verdict: str         # 'good' / 'neutral' / 'warn' / 'mixed'
    score_label: str     # 'Goed gedekt', 'Aandacht nodig', etc.
    samenvatting: str    # 1 zin
    factoren: list[Factor]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _level_to_score(level: Optional[str]) -> int:
    """good=+1 · neutral=0 · warn=-1 · None=0."""
    if level == "good":
        return 1
    if level == "warn":
        return -1
    return 0


def _aggregate(levels: list[Optional[str]]) -> tuple[str, str]:
    """Bepaal verdict + label uit de set van factor-levels.

    Volgorde van checks (eerste match wint):
      1. >=2 warns altijd mixed/warn — die mogen nooit verstopt raken
      2. Alle goods (geen warns) -> good
      3. Score-gebaseerde fallback voor de rest
    """
    warns = sum(1 for l in levels if l == "warn")
    goods = sum(1 for l in levels if l == "good")
    score = sum(_level_to_score(l) for l in levels)

    # Meerdere aandachtspunten: altijd zichtbaar maken
    if warns >= 2:
        return ("mixed", "Wisselend beeld") if goods >= 1 else ("warn", "Aandacht nodig")
    if warns == 1 and goods == 0:
        return "warn", "Aandacht nodig"
    if warns == 0 and goods >= 2:
        return "good", "Goed gedekt"
    if score >= 2:
        return "good", "Sterk"
    if score <= -2:
        return "warn", "Aandacht nodig"
    return "neutral", "Gemiddeld"


def _safe(d: dict, *keys: str) -> Any:
    """Veilige nested dict-get; None als één van de keys ontbreekt."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or cur.get(k) is None:
            return None
        cur = cur[k]
    return cur


def _eur(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return f"€{int(n):,}".replace(",", ".")


# ---------------------------------------------------------------------------
# De drie vragen
# ---------------------------------------------------------------------------

def vraag_kinderen(scan: dict) -> SocialeVraag:
    """'Is het hier veilig voor mijn kinderen?'

    Combineert: woninginbraken, geweld, luchtkwaliteit (PM2.5/NO2),
    afstand tot basisschool en huisarts.
    """
    factoren: list[Factor] = []
    levels: list[Optional[str]] = []

    inbr = _safe(scan, "veiligheid", "woninginbraak", "ref")
    inbr_v = _safe(scan, "veiligheid", "woninginbraak", "value")
    if inbr_v is not None:
        factoren.append(Factor(
            label="Woninginbraken in buurt",
            value_text=f"{inbr_v} per 1.000 inwoners (12 mnd)",
            level=(inbr or {}).get("chip_level", "neutral"),
        ))
        levels.append((inbr or {}).get("chip_level"))

    geweld_v = _safe(scan, "veiligheid", "geweld_12m")
    if geweld_v is not None:
        # Drempel: <20 = good, <60 = neutral, ≥60 = warn (rough heuristiek)
        gl = "good" if geweld_v < 20 else "neutral" if geweld_v < 60 else "warn"
        factoren.append(Factor(
            label="Geweldsmisdrijven (12 mnd)",
            value_text=f"{geweld_v} incidenten",
            level=gl,
        ))
        levels.append(gl)

    pm25 = _safe(scan, "leefkwaliteit", "pm25", "ref")
    pm25_v = _safe(scan, "leefkwaliteit", "pm25", "value")
    if pm25_v is not None:
        factoren.append(Factor(
            label="Fijnstof (PM2.5)",
            value_text=f"{pm25_v} µg/m³",
            level=(pm25 or {}).get("chip_level", "neutral"),
        ))
        levels.append((pm25 or {}).get("chip_level"))

    # Voorzieningen: pak afstand tot basisschool uit voorzieningen-lijst
    voorz = (scan.get("voorzieningen") or {}).get("items") or []
    school = next((v for v in voorz if v.get("type") == "basisschool"), None)
    if school and school.get("km") is not None:
        km = school["km"]
        sl = "good" if km <= 0.5 else "neutral" if km <= 1.5 else "warn"
        factoren.append(Factor(
            label="Basisschool dichtstbij",
            value_text=f"{km} km",
            level=sl,
        ))
        levels.append(sl)

    huisarts = next((v for v in voorz if v.get("type") == "huisarts"), None)
    if huisarts and huisarts.get("km") is not None:
        km = huisarts["km"]
        hl = "good" if km <= 1.0 else "neutral" if km <= 2.0 else "warn"
        factoren.append(Factor(
            label="Huisarts dichtstbij",
            value_text=f"{km} km",
            level=hl,
        ))
        levels.append(hl)

    verdict, label = _aggregate(levels)
    samenvatting = _samenvatting_kinderen(verdict, factoren)

    return SocialeVraag(
        vraag="Is het hier veilig voor mijn kinderen?",
        icoon="👶",
        verdict=verdict,
        score_label=label,
        samenvatting=samenvatting,
        factoren=factoren,
    )


def _samenvatting_kinderen(verdict: str, factoren: list[Factor]) -> str:
    if verdict == "good":
        return "Lage criminaliteit, schone lucht en voorzieningen op loop-/fietsafstand. Goed startgebied voor een gezin."
    if verdict == "warn":
        return "Meerdere aandachtspunten: lucht, criminaliteit of voorzieningen op afstand. Bekijk de factoren hieronder."
    if verdict == "mixed":
        return "Sterk op het ene aspect, zwak op het andere — afhankelijk van wat voor jouw gezin telt."
    return "Een typisch Nederlands profiel: niets uitgesproken slecht, niets uitgesproken goed."


def vraag_kosten(scan: dict) -> SocialeVraag:
    """'Wat kost wonen hier?'

    Combineert: WOZ (per pand of buurt), energielabel, paalrot-risico,
    geluid (proxy voor gevelisolatie-investering).
    """
    factoren: list[Factor] = []
    levels: list[Optional[str]] = []

    woz_adres = _safe(scan, "woning", "woz_adres", "value")
    woz_buurt = _safe(scan, "wijk_economie", "woz", "value")
    woz_bron = "dit pand" if woz_adres else "buurtgemiddelde"
    woz_v = woz_adres or woz_buurt
    if woz_v is not None:
        ref = _safe(scan, "woning", "woz_adres", "ref") or _safe(scan, "wijk_economie", "woz", "ref")
        factoren.append(Factor(
            label=f"WOZ-waarde ({woz_bron})",
            value_text=_eur(woz_v),
            level=(ref or {}).get("chip_level", "neutral"),
        ))
        # WOZ-niveau zelf is geen kostensignaal (hoog WOZ = duurder huis maar
        # dat is jouw keuze); enkel meenemen als context, niet in verdict.

    label = _safe(scan, "woning", "energielabel", "value")
    label_ref = _safe(scan, "woning", "energielabel", "ref")
    if label:
        ll = (label_ref or {}).get("chip_level", "neutral")
        factoren.append(Factor(
            label="Energielabel",
            value_text=label,
            level=ll,
        ))
        levels.append(ll)

    paalrot = _safe(scan, "klimaat", "paalrot", "value")
    paalrot_ref = _safe(scan, "klimaat", "paalrot", "ref")
    if paalrot is not None:
        pl = (paalrot_ref or {}).get("chip_level", "neutral")
        factoren.append(Factor(
            label="Funderingsrisico (buurt)",
            value_text=f"{paalrot}% van panden",
            level=pl,
        ))
        levels.append(pl)

    geluid = _safe(scan, "leefkwaliteit", "geluid", "value")
    geluid_ref = _safe(scan, "leefkwaliteit", "geluid", "ref")
    if geluid is not None:
        gl = (geluid_ref or {}).get("chip_level", "neutral")
        factoren.append(Factor(
            label="Geluidsbelasting op gevel",
            value_text=f"{geluid} dB Lden",
            level=gl,
        ))
        levels.append(gl)

    bouwjaar = _safe(scan, "woning", "bouwjaar", "value")
    if bouwjaar is not None:
        # Oud + slecht label = hoge verbouwkosten verwacht
        bl = "warn" if (bouwjaar < 1980 and label in ("E", "F", "G")) else (
            "good" if bouwjaar >= 2010 else "neutral"
        )
        factoren.append(Factor(
            label="Bouwjaar (renovatie-indicator)",
            value_text=str(bouwjaar),
            level=bl,
        ))
        levels.append(bl)

    verdict, score_label = _aggregate(levels)
    samenvatting = _samenvatting_kosten(verdict, label, paalrot)

    return SocialeVraag(
        vraag="Wat kost wonen hier?",
        icoon="💶",
        verdict=verdict,
        score_label=score_label,
        samenvatting=samenvatting,
        factoren=factoren,
    )


def _samenvatting_kosten(verdict: str, label, paalrot) -> str:
    if verdict == "good":
        return "Energiezuinige woning, stabiele ondergrond, weinig geluid. Lage maandelijkse + onderhoudskosten verwacht."
    if verdict == "warn":
        bits = []
        if label in ("E", "F", "G"):
            bits.append("verduurzaming nodig (G→A is €30-60k)")
        if paalrot and paalrot >= 40:
            bits.append("funderingsrisico (mogelijk €40-100k)")
        if bits:
            return "Reken op extra investeringen: " + " · ".join(bits) + "."
        return "Meerdere kostenposten kunnen op de loer liggen — zie de factoren."
    if verdict == "mixed":
        return "Lage maandlasten op het ene vlak, mogelijk forse renovatie of risico's op het andere."
    return "Standaard Nederlands kostenprofiel; geen rode vlaggen, geen uitschieters."


def vraag_investering(scan: dict) -> SocialeVraag:
    """'Is dit een goede langetermijn-investering?'

    Combineert: WOZ-trend, Leefbaarometer-totaal, hittestress, paalrot,
    arbeidsparticipatie buurt (proxy economische vitaliteit).
    """
    factoren: list[Factor] = []
    levels: list[Optional[str]] = []

    trend = _safe(scan, "wijk_economie", "woz", "trend_pct_per_jaar")
    if trend is not None:
        tl = "good" if trend >= 3 else "neutral" if trend >= 0 else "warn"
        factoren.append(Factor(
            label="WOZ-trend per jaar",
            value_text=f"{trend:+.1f}%",
            level=tl,
        ))
        levels.append(tl)

    leef_score = _safe(scan, "cover", "score")
    if leef_score is not None:
        ll = "good" if leef_score >= 7 else "neutral" if leef_score >= 4 else "warn"
        factoren.append(Factor(
            label="Leefbaarheid (Leefbaarometer)",
            value_text=f"{leef_score}/9",
            level=ll,
        ))
        levels.append(ll)

    arbeid = _safe(scan, "wijk_economie", "arbeidsparticipatie", "ref")
    arbeid_v = _safe(scan, "wijk_economie", "arbeidsparticipatie", "value")
    if arbeid_v is not None:
        al = (arbeid or {}).get("chip_level", "neutral")
        factoren.append(Factor(
            label="Economische vitaliteit (arbeidsparticipatie)",
            value_text=f"{arbeid_v}%",
            level=al,
        ))
        levels.append(al)

    paalrot = _safe(scan, "klimaat", "paalrot", "value")
    if paalrot is not None and paalrot >= 40:
        factoren.append(Factor(
            label="Klimaatrisico funderingsschade",
            value_text=f"{paalrot}% panden in buurt",
            level="warn",
        ))
        levels.append("warn")
    elif paalrot is not None:
        factoren.append(Factor(
            label="Klimaatrisico funderingsschade",
            value_text=f"{paalrot}% panden in buurt",
            level="good",
        ))
        levels.append("good")

    hitte_klasse = _safe(scan, "klimaat", "hittestress", "value")
    if hitte_klasse is not None:
        hl = "good" if hitte_klasse <= 2 else "neutral" if hitte_klasse <= 3 else "warn"
        factoren.append(Factor(
            label="Hittestress 2050 (klimaatrisico)",
            value_text=f"klasse {hitte_klasse}/5",
            level=hl,
        ))
        levels.append(hl)

    verdict, score_label = _aggregate(levels)
    samenvatting = _samenvatting_investering(verdict, trend)

    return SocialeVraag(
        vraag="Is dit een goede langetermijn-investering?",
        icoon="📈",
        verdict=verdict,
        score_label=score_label,
        samenvatting=samenvatting,
        factoren=factoren,
    )


def _samenvatting_investering(verdict: str, trend) -> str:
    if verdict == "good":
        if trend and trend > 0:
            return "Stijgende WOZ, sterke leefbaarheid, lage klimaatrisico's — het soort buurt waar mensen instromen."
        return "Stabiele wijk met economische vitaliteit en beperkte langetermijn-risico's."
    if verdict == "warn":
        return "Reken op tegenwind: dalende waarde, klimaatrisico's of een wijk die statistisch verzwakt."
    if verdict == "mixed":
        return "Sterke fundamenten op de ene as, klimaat- of marktrisico's op de andere. Inschatten waar jij op gokt."
    return "Doorsnee Nederlandse buurt qua waardeontwikkeling — geen winnaar, geen verliezer."


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def build(scan: dict) -> list[dict]:
    """Bouw de drie vragen op basis van een complete scan-response."""
    return [asdict(q) for q in (
        vraag_kinderen(scan),
        vraag_kosten(scan),
        vraag_investering(scan),
    )]
