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
    """Eén meetpunt onder een categorie."""

    label: str           # "Woninginbraken in buurt"
    value_text: str      # "4.0 per 1.000 inwoners"
    level: str           # 'good' / 'neutral' / 'warn'


@dataclass
class Categorie:
    """Een bakje dat meerdere losse factoren samenvat tot één verdict.

    Naast de raw factoren heeft een categorie:
      - score_5: 1-5 schaal voor de progress-bar
      - label: kwalitatief oordeel ('Zwak'/'Matig'/'Gemiddeld'/'Goed'/'Sterk')
      - samenvatting: 1 korte regel met concrete getallen
    """

    naam: str
    icoon: str
    verdict: str         # 'good' / 'neutral' / 'warn' / 'mixed'
    score_5: int         # 1-5 voor progress-bar
    label: str           # 'Zwak' | 'Matig' | 'Gemiddeld' | 'Goed' | 'Sterk'
    samenvatting: str
    factoren: list[Factor]


@dataclass
class SocialeVraag:
    vraag: str
    icoon: str
    verdict: str
    score_label: str     # legacy: 'Goed gedekt', etc.
    score_10: int        # 0-10 samengestelde score voor deze vraag
    label: str           # 'Niet geschikt' .. 'Zeer geschikt'
    samenvatting: str
    advies: str          # concreet advies-blok, 1-2 zinnen
    categorieen: list[Categorie]


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

def _score_5_and_label(factoren: list[Factor]) -> tuple[int, str]:
    """Bereken 1-5 score + kwalitatief label uit factoren.

    Schaal:
      - good  telt als +1
      - neutral 0
      - warn  -1
    Score_5 = 3 + (gemiddelde · 2), clamped 1-5.
    Label-grenzen zijn kalibreerd zodat 'Sterk' alleen bij overwegend goods,
    'Zwak' alleen bij overwegend warns.
    """
    if not factoren:
        return 3, "Onbekend"
    vals = []
    for f in factoren:
        if f.level == "good":
            vals.append(1)
        elif f.level == "warn":
            vals.append(-1)
        else:
            vals.append(0)
    avg = sum(vals) / len(vals)
    raw = 3 + avg * 2  # 1 (alle warns) tot 5 (alle goods)
    score = max(1, min(5, round(raw)))
    # Label kiezen: kwalitatief, niet lineair — mensen denken niet in "3/5"
    if score >= 5:
        return 5, "Sterk"
    if score >= 4:
        return 4, "Goed"
    if score >= 3:
        return 3, "Gemiddeld"
    if score >= 2:
        return 2, "Matig"
    return 1, "Zwak"


def _cat_from(naam: str, icoon: str, factoren: list[Factor]) -> Categorie:
    """Bouw een Categorie-bakje uit een lijst factoren.

    De samenvatting noemt max 3 factoren — warns eerst, dan goods.
    Dubbele emojis uit factor-labels worden weggehaald (de categorie-
    icoon is al zichtbaar; labels als '🏫 basisschool' worden 'basisschool').
    """
    factoren = [f for f in factoren if f]
    if not factoren:
        return Categorie(
            naam=naam, icoon=icoon, verdict="neutral",
            score_5=3, label="Onbekend",
            samenvatting="Geen data voor deze categorie.",
            factoren=[],
        )
    levels = [f.level for f in factoren]
    verdict, _ = _aggregate(levels)
    score_5, label = _score_5_and_label(factoren)

    # Samenvatting: warns eerst met concrete getallen, dan goods
    warns = [f for f in factoren if f.level == "warn"]
    goods = [f for f in factoren if f.level == "good"]
    parts: list[str] = []
    for f in warns[:2]:
        parts.append(f"{_clean_label(f.label)} {f.value_text}")
    for f in goods[:1]:
        parts.append(f"{_clean_label(f.label)} {f.value_text}")
    samenvatting = " · ".join(parts) if parts else "Gemeten waarden rond NL-gemiddelde."

    return Categorie(
        naam=naam, icoon=icoon, verdict=verdict,
        score_5=score_5, label=label,
        samenvatting=samenvatting, factoren=factoren,
    )


def _clean_label(label: str) -> str:
    """Strip emoji's en parenthetische uitleg — houdt 't kort in samenvatting."""
    # Verwijder alles tussen haakjes en trim
    import re
    s = re.sub(r"\s*\(.*?\)", "", label).strip()
    # Verwijder emoji-achtige karakters aan het begin
    while s and not s[0].isalnum():
        s = s[1:].lstrip()
    return s.lower() if s else label.lower()


def _vraag_score_10(categorieen: list[Categorie]) -> tuple[int, str]:
    """Aggregeer categorie-scores naar 0-10 + kwalitatief label per vraag.

    Neemt het gemiddelde van categorie-scores (1-5) en schaalt naar 0-10.
    Minder gewicht voor 'Onbekend'-bakjes.
    """
    valid = [c.score_5 for c in categorieen if c.label != "Onbekend"]
    if not valid:
        return 5, "Onbekend"
    avg = sum(valid) / len(valid)  # 1-5
    score_10 = round((avg - 1) / 4 * 10)  # 0-10
    if score_10 >= 8:
        return score_10, "Zeer geschikt"
    if score_10 >= 6:
        return score_10, "Geschikt"
    if score_10 >= 4:
        return score_10, "Wisselend"
    if score_10 >= 2:
        return score_10, "Matig geschikt"
    return score_10, "Niet geschikt"


def vraag_kinderen(scan: dict) -> SocialeVraag:
    """'Is het hier veilig voor mijn kinderen?' gegroepeerd in 3 bakjes:
       Veiligheid · Gezondheid · Kindervoorzieningen.

    Voor kinderen strenger meten op lucht dan bij volwassenen:
      - PM2.5 boven WHO-advies (5 µg/m³) = warn, niet neutral
      - NO2 vooral verkeersblootstelling (astma-risico)
      - Geluid boven 60 dB = warn (slaap/concentratie)
    """
    # === BAKJE 1: Veiligheid ===
    veilig: list[Factor] = []
    inbr_v = _safe(scan, "veiligheid", "woninginbraak", "value")
    inbr_lvl = _safe(scan, "veiligheid", "woninginbraak", "ref", "chip_level")
    if inbr_v is not None:
        veilig.append(Factor("Woninginbraken", f"{inbr_v} per 1.000 inw (12 mnd)", inbr_lvl or "neutral"))
    geweld_v = _safe(scan, "veiligheid", "geweld_12m")
    if geweld_v is not None:
        gl = "good" if geweld_v < 20 else "neutral" if geweld_v < 60 else "warn"
        veilig.append(Factor("Geweldsmisdrijven", f"{geweld_v} incidenten (12 mnd)", gl))
    # Leefbaarometer sub-score overlast = de meest overkoepelende indicator
    dims = _safe(scan, "cover", "dimensies") or []
    onv = next((d for d in dims if d.get("key") == "onv"), None)
    if onv and onv.get("score") is not None:
        s = onv["score"]
        ol = "good" if s >= 7 else "neutral" if s >= 5 else "warn"
        veilig.append(Factor("Overlast & veiligheid (BZK)", f"{s}/9", ol))

    # === BAKJE 2: Gezondheid (kinderen zijn strenger) ===
    gezond: list[Factor] = []
    pm25_v = _safe(scan, "leefkwaliteit", "pm25", "value")
    if pm25_v is not None:
        # Kinder-drempels: WHO 5 = good, 5-10 = neutral, >10 = warn
        pl = "good" if pm25_v <= 5 else "neutral" if pm25_v <= 10 else "warn"
        gezond.append(Factor("Fijnstof PM2.5", f"{pm25_v} µg/m³", pl))
    no2_v = _safe(scan, "leefkwaliteit", "no2", "value")
    if no2_v is not None:
        nl = "good" if no2_v <= 10 else "neutral" if no2_v <= 20 else "warn"
        gezond.append(Factor("Stikstofdioxide NO₂", f"{no2_v} µg/m³", nl))
    db = _safe(scan, "leefkwaliteit", "geluid", "value")
    if db is not None:
        gl = "good" if db < 50 else "neutral" if db < 60 else "warn"
        gezond.append(Factor("Geluid op gevel", f"{db} dB Lden", gl))

    # === BAKJE 3: Kindervoorzieningen ===
    voorz = (scan.get("voorzieningen") or {}).get("items") or []
    kinder: list[Factor] = []
    school = next((v for v in voorz if v.get("type") == "basisschool"), None)
    if school and school.get("km") is not None:
        km = school["km"]
        sl = "good" if km <= 0.5 else "neutral" if km <= 1.5 else "warn"
        kinder.append(Factor("Basisschool", f"{km} km", sl))
    kdv = next((v for v in voorz if v.get("type") == "kinderdagverblijf"), None)
    if kdv and kdv.get("km") is not None:
        km = kdv["km"]
        sl = "good" if km <= 0.5 else "neutral" if km <= 1.5 else "warn"
        kinder.append(Factor("Kinderdagverblijf", f"{km} km", sl))
    bso = next((v for v in voorz if v.get("type") == "buitenschoolse_opvang"), None)
    if bso and bso.get("km") is not None:
        km = bso["km"]
        sl = "good" if km <= 0.5 else "neutral" if km <= 1.5 else "warn"
        kinder.append(Factor("BSO", f"{km} km", sl))
    huisarts = next((v for v in voorz if v.get("type") == "huisarts"), None)
    if huisarts and huisarts.get("km") is not None:
        km = huisarts["km"]
        hl = "good" if km <= 1.0 else "neutral" if km <= 2.0 else "warn"
        kinder.append(Factor("Huisarts", f"{km} km", hl))

    # === BAKJE 4: Sociaal weefsel (andere gezinnen / speelmaatjes) ===
    # Een buurt met veel gezinnen + hoge sociale-samenhang = meer speelmaatjes,
    # actiever school-netwerk, en kinderen die elkaar opzoeken. Relevant voor
    # vooral jonge gezinnen.
    sociaal: list[Factor] = []
    kind_pct = _safe(scan, "buren", "met_kinderen", "value")
    if kind_pct is not None:
        # NL-gemiddelde 2024: ~34% huishoudens met kinderen
        # >40% = good (kinderrijke buurt), 25-40 = neutral, <25 = warn
        sl = "good" if kind_pct >= 40 else "neutral" if kind_pct >= 25 else "warn"
        sociaal.append(Factor(
            "Huishoudens met kinderen",
            f"{kind_pct}% (NL-gem ~34%)",
            sl,
        ))
    h_grootte = _safe(scan, "buren", "huishoudensgrootte")
    if h_grootte is not None:
        # Grote huishoudens = meestal gezinnen. >2.3 = good (gezinsbuurt)
        sl = "good" if h_grootte >= 2.4 else "neutral" if h_grootte >= 2.0 else "warn"
        sociaal.append(Factor(
            "Gemiddelde huishoudensgrootte",
            f"{h_grootte} personen",
            sl,
        ))
    soc_dim = next((d for d in dims if d.get("key") == "soc"), None)
    if soc_dim and soc_dim.get("score") is not None:
        s = soc_dim["score"]
        sl = "good" if s >= 7 else "neutral" if s >= 5 else "warn"
        sociaal.append(Factor(
            "Sociale samenhang buurt (BZK)",
            f"{s}/9",
            sl,
        ))

    categorieen = [
        _cat_from("Veiligheid", "🛡️", veilig),
        _cat_from("Gezondheid", "🫁", gezond),
        _cat_from("Kindervoorzieningen", "🏫", kinder),
        _cat_from("Sociaal weefsel", "👪", sociaal),
    ]

    cat_levels = [c.verdict if c.verdict != "mixed" else "warn" for c in categorieen]
    verdict, score_label = _aggregate(cat_levels)
    score_10, label = _vraag_score_10(categorieen)
    samenvatting = _vraag_samenvatting(categorieen)
    advies = _advies_kinderen(categorieen)

    return SocialeVraag(
        vraag="Is het hier veilig voor mijn kinderen?",
        icoon="👶",
        verdict=verdict,
        score_label=score_label,
        score_10=score_10,
        label=label,
        samenvatting=samenvatting,
        advies=advies,
        categorieen=categorieen,
    )


def _vraag_samenvatting(cats: list[Categorie]) -> str:
    """1 regel samenvatting over alle categorieën."""
    goed = [c.naam.lower() for c in cats if c.score_5 >= 4]
    warn = [c.naam.lower() for c in cats if c.score_5 <= 2]
    if goed and warn:
        return f"Sterk op {' en '.join(goed)}; aandacht op {' en '.join(warn)}."
    if goed:
        return f"Sterk op alle vlakken: {', '.join(goed)}."
    if warn:
        return f"Aandachtspunten: {', '.join(warn)}."
    return "Overwegend gemiddeld Nederlands profiel."


def _advies_kinderen(cats: list[Categorie]) -> str:
    """Concreet advies voor gezin-vraag op basis van de 4 categorie-scores."""
    d = {c.naam: c.score_5 for c in cats}
    veiligheid = d.get("Veiligheid", 3)
    gezondheid = d.get("Gezondheid", 3)
    voorzieningen = d.get("Kindervoorzieningen", 3)
    sociaal = d.get("Sociaal weefsel", 3)

    # Dominant sterk op alles
    if all(s >= 4 for s in (veiligheid, gezondheid, voorzieningen, sociaal)):
        return (
            "Uitstekend geschikt voor een gezin: veilig, gezonde lucht, "
            "voorzieningen dichtbij en veel andere gezinnen in de buurt."
        )
    # Meerdere zware problemen
    zwak_veilig = veiligheid <= 2
    zwak_gezond = gezondheid <= 2
    zwak_soc = sociaal <= 2
    zwak_voorz = voorzieningen <= 2

    if zwak_veilig and zwak_gezond:
        return (
            "Dit adres past niet goed bij een gezin met jonge kinderen. "
            "Vervuiling en overlast zijn structureel; voor oudere kinderen "
            "of tieners is dat minder bezwaarlijk."
        )
    if zwak_gezond and voorzieningen >= 4:
        return (
            "Voorzieningen zitten op loopafstand, maar de lucht- en geluidsbelasting "
            "is hoog — voor kleine kinderen die veel buiten spelen een serieus minpunt."
        )
    if zwak_veilig and voorzieningen >= 4:
        return (
            "Voorzieningen zijn top, maar veiligheid/overlast in de buurt is onder de maat. "
            "Voor tieners werkbaar; voor jonge kinderen minder geschikt."
        )
    if zwak_soc and voorzieningen >= 4 and veiligheid >= 3:
        return (
            "Prima buurt qua veiligheid en voorzieningen, maar er wonen weinig andere gezinnen — "
            "minder speelmaatjes of sociale context voor kinderen."
        )
    if all(s >= 3 for s in (veiligheid, gezondheid, voorzieningen, sociaal)):
        return "Passend voor een gezin; nergens uitgesproken slecht, nergens uitgesproken beter dan gemiddeld."
    if zwak_voorz and veiligheid >= 3 and gezondheid >= 3:
        return (
            "Veilig en gezond, maar voorzieningen voor kinderen liggen op afstand — "
            "reken op meer auto-/fiets-ritjes naar school en opvang."
        )
    return (
        "Een gemengd beeld. Bekijk welke categorieën voor jouw gezin zwaar wegen — "
        "daar zit het echte antwoord."
    )


def vraag_kosten(scan: dict) -> SocialeVraag:
    """'Wat kost wonen hier?' in 3 bakjes:
       Aanschaf/waardebehoud · Verduurzaming · Risico-investeringen.
    """
    # === BAKJE 1: Aanschaf & waardebehoud ===
    aanschaf: list[Factor] = []
    woz_adres = _safe(scan, "woning", "woz_adres", "value")
    woz_buurt = _safe(scan, "wijk_economie", "woz", "value")
    woz_v = woz_adres or woz_buurt
    if woz_v is not None:
        bron = "dit pand" if woz_adres else "buurt"
        # Niveau als context; geen good/warn op absoluut bedrag
        aanschaf.append(Factor(
            f"WOZ-waarde ({bron})", _eur(woz_v), "neutral",
        ))
    trend = _safe(scan, "wijk_economie", "woz", "trend_pct_per_jaar")
    if trend is not None:
        tl = "good" if trend >= 3 else "neutral" if trend >= 0 else "warn"
        aanschaf.append(Factor(
            "WOZ-trend per jaar", f"{trend:+.1f}%", tl,
        ))

    # === BAKJE 2: Verduurzaming (oud huis = hoge investering) ===
    verduurzamen: list[Factor] = []
    label = _safe(scan, "woning", "energielabel", "value")
    label_lvl = _safe(scan, "woning", "energielabel", "ref", "chip_level")
    if label:
        verduurzamen.append(Factor("Energielabel", label, label_lvl or "neutral"))
    bouwjaar = _safe(scan, "woning", "bouwjaar", "value")
    if bouwjaar is not None:
        # Ouder dan 1980 + slecht label = reële renovatiekosten
        if bouwjaar < 1945:
            bl = "warn" if label in ("E", "F", "G") else "neutral"
        elif bouwjaar < 1992:
            bl = "neutral"
        else:
            bl = "good"
        verduurzamen.append(Factor(
            "Bouwjaar (isolatiestandaard)", str(bouwjaar), bl,
        ))
    # Geluid komt hier ook terug — gevelisolatie wordt duurder bij veel geluid
    db = _safe(scan, "leefkwaliteit", "geluid", "value")
    if db is not None:
        gl = "good" if db < 55 else "neutral" if db < 65 else "warn"
        verduurzamen.append(Factor(
            "Geluid (gevelisolatie-kost)", f"{db} dB Lden", gl,
        ))

    # === BAKJE 3: Risico-investeringen (klimaat + bouwfysiek) ===
    risico: list[Factor] = []
    paalrot_v = _safe(scan, "klimaat", "paalrot", "value")
    paalrot_lvl = _safe(scan, "klimaat", "paalrot", "ref", "chip_level")
    if paalrot_v is not None:
        risico.append(Factor(
            "Funderingsrisico (buurt)",
            f"{paalrot_v}% van panden",
            paalrot_lvl or "neutral",
        ))
    hitte = _safe(scan, "klimaat", "hittestress", "value")
    if hitte is not None:
        hl = "good" if hitte <= 2 else "neutral" if hitte <= 3 else "warn"
        risico.append(Factor(
            "Hittestress (koeling/airco-kost)",
            f"klasse {hitte}/5",
            hl,
        ))
    # Waterdiepte alleen als er daadwerkelijk overlast is
    waterdiepte = _safe(scan, "klimaat", "waterdiepte_cm")
    if waterdiepte and waterdiepte > 0:
        wl = "warn" if waterdiepte >= 20 else "neutral"
        risico.append(Factor(
            "Wateroverlast bij piekneerslag",
            f"{waterdiepte} cm",
            wl,
        ))

    categorieen = [
        _cat_from("Aanschaf & waardebehoud", "🏷️", aanschaf),
        _cat_from("Verduurzaming", "🔋", verduurzamen),
        _cat_from("Risico-investeringen", "⚠️", risico),
    ]

    cat_levels = [c.verdict if c.verdict != "mixed" else "warn" for c in categorieen]
    verdict, score_label = _aggregate(cat_levels)
    score_10, label = _vraag_score_10(categorieen)
    samenvatting = _vraag_samenvatting(categorieen)
    advies = _advies_kosten(categorieen)

    return SocialeVraag(
        vraag="Wat kost wonen hier?",
        icoon="💶",
        verdict=verdict,
        score_label=score_label,
        score_10=score_10,
        label=label,
        samenvatting=samenvatting,
        advies=advies,
        categorieen=categorieen,
    )


def _advies_kosten(cats: list[Categorie]) -> str:
    d = {c.naam: c.score_5 for c in cats}
    aanschaf = d.get("Aanschaf & waardebehoud", 3)
    verduur = d.get("Verduurzaming", 3)
    risico = d.get("Risico-investeringen", 3)

    if all(s >= 4 for s in (aanschaf, verduur, risico)):
        return "Lage totale woonlasten verwacht: stijgende waarde, zuinig pand, geen grote klimaatrisico's."
    if risico <= 2 and verduur <= 2:
        return (
            "Reken op fors extra kapitaal: funderingsherstel (€40-100k) plus verduurzaming (€20-60k). "
            "Alleen doen als je budget hebt — dit is geen instapper."
        )
    if risico <= 2 and verduur >= 4:
        return (
            "Het huis zelf is energetisch op orde, maar klimaatrisico's (fundering of water) "
            "zijn reëel. Laat vóór aankoop een funderingsonderzoek doen."
        )
    if verduur <= 2 and risico >= 4:
        return (
            "Stabiele ondergrond en lage klimaatrisico's, maar het huis is zelf onzuinig. "
            "Reken op €30-60k verduurzaming voor G→A+ in 10 jaar."
        )
    if aanschaf <= 2 and verduur >= 4 and risico >= 4:
        return (
            "Het pand is goed maar de buurt verliest waarde. Op lange termijn "
            "kan het lastig zijn je investering terug te verdienen."
        )
    return "Gemengd kostenbeeld. Belangrijkste check: doe een bouwkundige keuring vóór aankoop."


def vraag_investering(scan: dict) -> SocialeVraag:
    """'Is dit een goede langetermijn-investering?' in 3 bakjes:
       Waarde-ontwikkeling · Wijk-vitaliteit · Klimaat-robuustheid.
    """
    # === BAKJE 1: Waarde-ontwikkeling ===
    waarde: list[Factor] = []
    trend = _safe(scan, "wijk_economie", "woz", "trend_pct_per_jaar")
    if trend is not None:
        tl = "good" if trend >= 3 else "neutral" if trend >= 0 else "warn"
        waarde.append(Factor("WOZ-trend", f"{trend:+.1f}% per jaar", tl))
    leef_score = _safe(scan, "cover", "score")
    buurt_score = _safe(scan, "cover", "buurt_score")
    if leef_score is not None:
        ll = "good" if leef_score >= 7 else "neutral" if leef_score >= 4 else "warn"
        waarde.append(Factor("Leefbaarheid (100m)", f"{leef_score}/9", ll))
    if buurt_score is not None and leef_score != buurt_score:
        bl = "good" if buurt_score >= 7 else "neutral" if buurt_score >= 4 else "warn"
        waarde.append(Factor("Leefbaarheid buurt-breed", f"{buurt_score}/9", bl))

    # === BAKJE 2: Wijk-vitaliteit (economie + mensen) ===
    vitaal: list[Factor] = []
    arbeid_v = _safe(scan, "wijk_economie", "arbeidsparticipatie", "value")
    arbeid_lvl = _safe(scan, "wijk_economie", "arbeidsparticipatie", "ref", "chip_level")
    if arbeid_v is not None:
        vitaal.append(Factor("Arbeidsparticipatie", f"{arbeid_v}%", arbeid_lvl or "neutral"))
    opl_v = _safe(scan, "wijk_economie", "opleiding_hoog", "value")
    opl_lvl = _safe(scan, "wijk_economie", "opleiding_hoog", "ref", "chip_level")
    if opl_v is not None:
        vitaal.append(Factor("Hoogopgeleid", f"{opl_v}%", opl_lvl or "neutral"))
    dims = _safe(scan, "cover", "dimensies") or []
    won_dim = next((d for d in dims if d.get("key") == "won"), None)
    if won_dim and won_dim.get("score") is not None:
        s = won_dim["score"]
        wl = "good" if s >= 7 else "neutral" if s >= 5 else "warn"
        vitaal.append(Factor("Woningvoorraad-kwaliteit", f"{s}/9", wl))

    # === BAKJE 3: Klimaat-robuustheid (de grote onzekerheid) ===
    klimaat: list[Factor] = []
    paalrot = _safe(scan, "klimaat", "paalrot", "value")
    if paalrot is not None:
        kl = "warn" if paalrot >= 40 else "neutral" if paalrot >= 10 else "good"
        klimaat.append(Factor("Funderingsrisico 2050", f"{paalrot}% panden", kl))
    hitte = _safe(scan, "klimaat", "hittestress", "value")
    if hitte is not None:
        hl = "good" if hitte <= 2 else "neutral" if hitte <= 3 else "warn"
        klimaat.append(Factor("Hittestress 2050", f"klasse {hitte}/5", hl))

    categorieen = [
        _cat_from("Waarde-ontwikkeling", "📈", waarde),
        _cat_from("Wijk-vitaliteit", "💼", vitaal),
        _cat_from("Klimaat-robuustheid", "🌍", klimaat),
    ]

    cat_levels = [c.verdict if c.verdict != "mixed" else "warn" for c in categorieen]
    verdict, score_label = _aggregate(cat_levels)
    score_10, label = _vraag_score_10(categorieen)
    samenvatting = _vraag_samenvatting(categorieen)
    advies = _advies_investering(categorieen)

    return SocialeVraag(
        vraag="Is dit een goede langetermijn-investering?",
        icoon="📈",
        verdict=verdict,
        score_label=score_label,
        score_10=score_10,
        label=label,
        samenvatting=samenvatting,
        advies=advies,
        categorieen=categorieen,
    )


def _advies_investering(cats: list[Categorie]) -> str:
    d = {c.naam: c.score_5 for c in cats}
    waarde = d.get("Waarde-ontwikkeling", 3)
    vitaal = d.get("Wijk-vitaliteit", 3)
    klimaat = d.get("Klimaat-robuustheid", 3)

    if all(s >= 4 for s in (waarde, vitaal, klimaat)):
        return "Soort buurt waar mensen instromen: stijgende waarde, economisch sterk, klimaatbestendig. Lange-termijn investeringsmagneet."
    if klimaat <= 2 and waarde >= 4:
        return (
            "Huidige waarde-ontwikkeling is sterk, maar klimaatrisico's (fundering of hitte) "
            "kunnen vanaf 2030-2040 gaan drukken op verzekerbaarheid en hypotheek. "
            "Blijvende monitoring van funderingsschade aangeraden."
        )
    if waarde <= 2 and klimaat >= 4:
        return (
            "Stabiel ondergrond en klimaatbestendig, maar de wijk zakt qua waarde. "
            "Mogelijk een kooptijdstip als je gelooft in revitalisatie, anders riskant."
        )
    if waarde <= 2 and klimaat <= 2:
        return (
            "Meerdere risicofactoren samen: dalende waarde plus structurele klimaatblootstelling. "
            "Voor investeerders minder aantrekkelijk; bekijk nabijgelegen alternatieven."
        )
    if vitaal >= 4 and waarde >= 4:
        return "Economische vitaliteit en waardeontwikkeling gaan hand in hand — kenmerk van een 'kansrijke' buurt."
    return (
        "Doorsnee investeringsprofiel. Waarde ontwikkelt gemiddeld mee met NL — "
        "geen uitschieter, maar ook geen rode vlaggen."
    )


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
