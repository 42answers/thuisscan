"""
Buurtscan v2.0-rapport — render-engine.

Twee gebruiksvormen:
  A. Standalone (lokaal testen):    python3 rapport_template.py
     Leest /tmp/p72_*.json, schrijft /tmp/rapport_v2.html
  B. Als module in backend:
       from rapport_template import render_html
       html_str = await render_html_async(adres_query)
"""
from __future__ import annotations

import base64
import json
import html
import os
import re
from pathlib import Path
from datetime import datetime, timedelta

# Module-globals — worden door _set_globals() gevuld voor elke render.
SCAN: dict = {}
WOZ: dict = {}
VOORZ: dict = {}
KLIM: dict = {}
BER: dict = {}
EXTRAS: dict = {}
VERB: dict = {}
A: dict = {}
W: dict = {}
WE: dict = {}
B: dict = {}
V: dict = {}
LK: dict = {}
ON: dict = {}
COVER: dict = {}
STREETMAP_SRC: str = ""   # <img src=...> waarde
PERCEEL_SRC: str = ""

NU = datetime.now()
RAPPORTNR = f"BS-{NU.strftime('%Y')}-{NU.strftime('%m%d')}"


def _set_globals(data: dict) -> None:
    """Vul module-globals vóór render."""
    global SCAN, WOZ, VOORZ, KLIM, BER, EXTRAS, VERB
    global A, W, WE, B, V, LK, ON, COVER
    global STREETMAP_SRC, PERCEEL_SRC
    SCAN = data.get("scan") or {}
    WOZ = data.get("woz") or {}
    VOORZ = data.get("voorz") or {}
    KLIM = data.get("klim") or {}
    BER = data.get("ber") or {}
    EXTRAS = data.get("extras") or {}
    VERB = data.get("verb") or {}
    A = SCAN.get("adres", {})
    W = SCAN.get("woning", {})
    WE = SCAN.get("wijk_economie", {})
    B = SCAN.get("buren", {})
    V = SCAN.get("veiligheid", {})
    LK = SCAN.get("leefkwaliteit", {})
    ON = SCAN.get("onderwijs", {})
    COVER = SCAN.get("cover", {})
    sm = data.get("streetmap_png")  # bytes of None
    pc = data.get("perceel_png")
    if sm:
        STREETMAP_SRC = "data:image/png;base64," + base64.b64encode(sm).decode("ascii")
    elif Path("/tmp/p72_streetmap.png").exists():
        STREETMAP_SRC = "{STREETMAP_SRC}"
    else:
        STREETMAP_SRC = ""
    if pc:
        PERCEEL_SRC = "data:image/png;base64," + base64.b64encode(pc).decode("ascii")
    elif Path("/tmp/p72_perceel.png").exists():
        PERCEEL_SRC = "{PERCEEL_SRC}"
    else:
        PERCEEL_SRC = ""

NL_MAANDEN = {
    "January": "januari", "February": "februari", "March": "maart",
    "April": "april", "May": "mei", "June": "juni",
    "July": "juli", "August": "augustus", "September": "september",
    "October": "oktober", "November": "november", "December": "december",
}
def nl_datum(d, fmt="%d %B %Y"):
    s = d.strftime(fmt)
    for en, nl in NL_MAANDEN.items(): s = s.replace(en, nl)
    return s

TIJD = nl_datum(NU)
TIJD_KORT = nl_datum(NU, "%d %B %Y, %H:%M")
GELDIG_TOT = nl_datum(NU + timedelta(days=7))


# =============================================================================
# HELPERS
# =============================================================================
def euro(v):
    if v is None: return "—"
    return f"€&nbsp;{int(v):,}".replace(",", ".")


def _fmt_top_pct(top_pct):
    """Empirisch percentiel → leesbare 'Top X% van Nederland qua leefbaarheid'
    (boven gem.) of 'Onderste X% van Nederland qua leefbaarheid' (onder gem.).

    Spiegel van formatTopPct() in apps/web/app.js — expliciet 'qua leefbaarheid'
    zodat duidelijk is waar het percentiel naar refereert.
    """
    if top_pct is None:
        return ""
    try:
        p = float(top_pct)
    except (TypeError, ValueError):
        return ""
    suffix = " van Nederland qua leefbaarheid"
    if p < 0.5:
        return "Top &lt;1%" + suffix
    if p < 1:
        return "Top 1%" + suffix
    if p <= 50:
        return f"Top {round(p)}%" + suffix
    onderste = 100 - p
    if onderste < 0.5:
        return "Onderste &lt;1%" + suffix
    if onderste < 1:
        return "Onderste 1%" + suffix
    return f"Onderste {round(onderste)}%" + suffix


def _render_balans_waarschuwing(text, severity):
    """Render de sub-balans-waarschuwing in PDF — twee severity-stijlen."""
    if not text:
        return ""
    import html as _h
    icon = "⚠" if severity == "strong" else "ℹ"
    cls = "callout-strong" if severity == "strong" else "callout-mild"
    label = "Let op" if severity == "strong" else "Nuance"
    return (
        f'<div class="callout {cls}">'
        f'<strong>{icon} {label}:</strong> {_h.escape(text)}'
        f'</div>'
    )


def _render_percentile_chip(top_pct):
    """Render de empirisch-percentiel chip onder de grote score in leef-hero.

    Spiegel van renderPercentileBadge() in apps/web/app.js — drie kleur-niveaus
    (good/neutral/warn) op basis van top_pct positie, en compacte tekst zoals
    'Top 7% NL' / 'Bottom 3% NL'. Returnt lege string bij geen data.
    """
    if top_pct is None:
        return ""
    try:
        p = float(top_pct)
    except (TypeError, ValueError):
        return ""
    if p < 0.5:
        text, level = "Top &lt;1% NL", "good"
    elif p < 10:
        text, level = f"Top {round(p)}% NL", "good"
    elif p <= 50:
        text, level = f"Top {round(p)}% NL", "neutral"
    else:
        # Onderkant — spiegel ('Onderste X% NL'), geen Engelse 'Bottom'.
        onderste = 100 - p
        if onderste < 0.5:
            text = "Onderste &lt;1% NL"
        elif onderste < 1:
            text = "Onderste 1% NL"
        else:
            text = f"Onderste {round(onderste)}% NL"
        level = "warn"
    # Mini-label onder de chip — dezelfde rol als in app.js (cover-percentile-caption):
    # maakt expliciet dat het percentiel over leefbaarheid gaat.
    return (f'<div class="leef-pct leef-pct-{level}">{text}</div>'
            f'<div class="leef-pct-caption">leefbaarheid</div>')

def fmt_pct(v, signed=True):
    if v is None: return "—"
    sign = "+" if v > 0 and signed else ""
    return f"{sign}{v:.1f}%".replace(".",",")

def fmt_afstand(meters, *, in_km=False):
    """Afstand altijd in km met 1 decimaal (100m nauwkeurig)."""
    if meters is None or meters == "—": return "—"
    try:
        km = float(meters) / 1000.0
    except (TypeError, ValueError):
        return str(meters)
    return f"{km:.1f}".replace(".", ",") + "&nbsp;km"

def vertaal_daktype(t):
    """3D BAG technische daktype-codes naar NL."""
    if not t: return "—"
    mapping = {
        "horizontal": "Plat dak",
        "flat": "Plat dak",
        "multiple horizontal": "Meerdere platte daken",
        "slanted": "Hellend dak (zadel/lessenaar)",
        "pitched": "Hellend dak",
        "sloped": "Hellend dak",
    }
    return mapping.get(t.lower(), t.capitalize())

def chip(text, level="neutral"):
    if not text: return ""
    return f'<span class="chip chip-{level}">{html.escape(str(text))}</span>'

def src(text):
    if not text: return ""
    return f'<div class="source">{html.escape(text)}</div>'

def stat(label, value, *, unit=None, source=None, badge=None, badge_level="neutral"):
    val = str(value) if value is not None else "—"
    if unit and value not in (None, "—"):
        val = f'{val}<span class="unit">&nbsp;{unit}</span>'
    badge_html = f' {chip(badge, badge_level)}' if badge else ""
    return f"""
    <div class="stat">
      <div class="label">{html.escape(label)}</div>
      <div class="value">{val}{badge_html}</div>
      {src(source)}
    </div>
    """

def section_h(num, kicker, title_main, title_accent, intro=""):
    """intro mag <strong>/<em> HTML bevatten — caller zorgt voor escape van user-data."""
    intro_html = f'<p class="intro">{intro}</p>' if intro else ""
    return f"""
    <div class="section-head">
      <div class="kicker">{num} — {html.escape(kicker.upper())}</div>
      <h2>{html.escape(title_main)} <em>{html.escape(title_accent)}</em>.</h2>
      {intro_html}
    </div>
    """

def link(url, label):
    if not url or not str(url).strip(): return html.escape(label)
    u = str(url).strip()
    if not (u.startswith("http://") or u.startswith("https://") or u.startswith("mailto:")):
        u = "https://" + u
    return f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(label)}</a>'

def gmaps_link(query):
    """Google Maps deeplink voor een POI op basis van naam + adres."""
    q = re.sub(r'\s+', '+', f"{query} {A.get('display_name','').split(',')[-1].strip()}")
    return f"https://www.google.com/maps/search/{q}"

def eigendom_context(eig):
    """Korte interpretatie van de eigendomsverhouding vs NL-gem."""
    koop = eig.get("koop_pct", 0)
    huur = eig.get("sociale_huur_pct", 0) + eig.get("particuliere_huur_pct", 0)
    if koop >= 70:
        return "Sterk koop-dominant — bewoners blijven typisch langer wonen, weinig doorstroom, actieve VvE's."
    if koop >= 55:
        return "Koop-overheersend zoals NL-gemiddelde (58%) — gematigde doorstroom."
    if huur >= 50:
        return f"Huur-overheersend ({huur:.0f}% versus NL ~42%) — meer doorstroom, vluchtigere bewoners-mix, kleinere markt voor familie-koophuizen."
    return "Mix van koop en huur — gemiddelde doorstroom."


def leeftijd_context(fijn):
    """Interpreteer leeftijdsopbouw vs NL (24/56/20)."""
    jong = fijn.get("0-15", 0) + fijn.get("15-25", 0)
    midden = fijn.get("25-45", 0) + fijn.get("45-65", 0)
    oud = fijn.get("65+", 0)
    delen = []
    # Vs NL gemiddelden
    if jong >= 30: delen.append(f"veel jongeren ({jong:.0f}% versus NL ~24%) — gezinsbuurt met druk op scholen en opvang")
    elif jong <= 15: delen.append(f"weinig jongeren ({jong:.0f}% versus NL ~24%) — minder gezinnen")
    if midden >= 70: delen.append(f"hoog aandeel werkenden ({midden:.0f}% van 25-65 jaar versus NL ~56%) — typisch starters/young-professionals-buurt")
    if oud >= 25: delen.append(f"vergrijsd ({oud:.0f}% 65+ versus NL ~20%)")
    elif oud <= 10: delen.append(f"weinig 65-plussers ({oud:.0f}% versus NL ~20%)")
    if not delen:
        return f"Brede leeftijdsmix rond het Nederlandse gemiddelde (24/56/20%)."
    return "Buurt heeft " + " · ".join(delen) + "."


def _geluid_stat(label, db_value, kind, default_msg):
    """Geluid-stat met interpretatie obv dB-waarde en bron-type.
    kind = 'trein' | 'vlieg' (verschillende drempels per bron)."""
    if db_value is None or db_value == "—":
        return stat(label, "—", source="RIVM · GEEN DATA")
    try:
        v = float(db_value)
    except (TypeError, ValueError):
        return stat(label, db_value, unit="dB Lden", source="RIVM")
    # Drempels: < 40 dB = niet gemodelleerd (te laag voor RIVM-grid)
    if v < 40:
        chip_text = "geen hinder"
        chip_lvl = "good"
        bet = default_msg
    elif v < 50:
        chip_text = "nauwelijks hinder"
        chip_lvl = "good"
        bet = "Onder de typische hinder-drempel."
    elif v < 55:
        chip_text = "lichte hinder"
        chip_lvl = "neutral"
        bet = "Vergelijkbaar met rustig stedelijk gebied."
    elif v < 65:
        chip_text = "matige hinder"
        chip_lvl = "warn"
        bet = "Boven WHO-advies (53 dB) en EU-hinderdrempel (55 dB)."
    else:
        chip_text = "ernstige hinder"
        chip_lvl = "warn"
        bet = "Boven 65 dB — significante slaap- en gezondheidsoverlast."
    fake_ref = {
        "chip_text": chip_text,
        "chip_level": chip_lvl,
        "nl_gemiddelde": "23 dB (vlieg) · 35 dB (trein) · landelijk gem.",
        "betekenis": bet,
    }
    return stat_with_ref(label, db_value, fake_ref, unit="dB Lden", source="RIVM · WHO 53 · EU 55")


def opleiding_context(b):
    """Vergelijk wijk-opleidingsniveau met NL-gemiddelde (32/38/30)."""
    if not b: return ""
    laag = b.get("laag_pct", 0)
    hoog = b.get("hoog_pct", 0)
    if hoog >= 50:
        return f"<strong>{hoog:.0f}% hoogopgeleid</strong> — duidelijk boven NL-gemiddelde van ~30%. Typisch voor stedelijk-academisch milieu."
    if hoog >= 38:
        return f"{hoog:.0f}% hoogopgeleid — boven NL-gemiddelde (~30%)."
    if laag >= 45:
        return f"{laag:.0f}% laagopgeleid — boven NL-gemiddelde (~32%); minder vraag naar hoge-segment-vastgoed."
    return f"Opleidingsniveau rond NL-gemiddelde (32/38/30%)."


def herkomst_context(migratie):
    """Interpreteer herkomst vs NL (76/10/14)."""
    nl = migratie.get("pct_nederlands", 0)
    westers = migratie.get("pct_westers", 0)
    nw = migratie.get("pct_niet_westers", 0)
    if nw >= 25:
        return f"Sterke internationale samenstelling: {nw:.0f}% niet-westerse en {westers:.0f}% westerse migratie­achtergrond — duidelijk diverser dan het Nederlandse gemiddelde van 14% niet-westers en 10% westers."
    if westers >= 18:
        return f"Veel internationale bewoners, vooral westerse migratie­achtergrond ({westers:.0f}% versus NL 10%) — typisch expat-buurt of oude Europese migratiestromen."
    if nl >= 85:
        return f"Overwegend Nederlandse bevolking ({nl:.0f}% versus NL-gemiddelde van 76%) — homogenere buurt."
    return f"Bevolkingssamenstelling vergelijkbaar met Nederlandse gemiddelde (76% Nederlands, 10% westers, 14% niet-westers)."


def school_url(s):
    """Forceer Scholen op de Kaart link voor consistentie.

    Als de oorspronkelijke URL al naar scholenopdekaart.nl wijst → gebruiken.
    Anders: maak een zoek-deeplink met de schoolnaam (geeft betrouwbaar resultaat).
    """
    url = (s.get("url") or "").strip()
    if "scholenopdekaart.nl" in url:
        return url
    naam = s.get("naam", "")
    if naam:
        from urllib.parse import quote
        return f"https://scholenopdekaart.nl/zoeken/?zoekterm={quote(naam)}"
    return None

# ---------- ref-velden formatter ----------
def stat_with_ref(label, value, ref, *, unit=None, source=None):
    """Stat met alle ref-velden uit de API (chip_text, nl_gemiddelde, betekenis)."""
    val = str(value) if value not in (None, "—") else "—"
    if unit and value not in (None, "—"):
        val = f'{val}<span class="unit">&nbsp;{unit}</span>'

    chip_html = ""
    nlg_html = ""
    bet_html = ""
    if ref:
        if ref.get("chip_text"):
            chip_html = f' {chip(ref["chip_text"], ref.get("chip_level","neutral"))}'
        if ref.get("nl_gemiddelde"):
            nlg_html = f'<div class="ref-nl">vs NL: {html.escape(ref["nl_gemiddelde"])}</div>'
        if ref.get("betekenis"):
            bet_html = f'<div class="ref-bet">{html.escape(ref["betekenis"])}</div>'

    return f"""
    <div class="stat">
      <div class="label">{html.escape(label)}</div>
      <div class="value">{val}{chip_html}</div>
      {nlg_html}
      {bet_html}
      {src(source)}
    </div>
    """


# =============================================================================
# PAGE HEADER / FOOTER
# =============================================================================
def phead(right_text):
    adres = A.get('display_name','')
    return f"""
    <header class="phead">
      <span><span class="logo">B</span> Buurtscan{f' · {html.escape(adres)}' if adres else ''}</span>
      <span class="phead-right">{html.escape(right_text)}</span>
    </header>
    """

def pfoot(num=None, total=None, copyright=False):
    """Pagina-footer. Pagina-nummering komt automatisch via @page CSS counter
    rechtsonder; pfoot toont alleen rapport-id (links) en buurtscan-merk (rechts)."""
    left = f"© {NU.year} BUURTSCAN · KVK 98765432" if copyright else f"RAPPORT {RAPPORTNR} · BUURTSCAN.NL"
    extra_class = " copyright" if copyright else ""
    return f"""
    <footer class="pfoot{extra_class}">
      <span>{left}</span>
      <span>VERTROUWELIJK · ENKEL VOOR HET GENOEMDE ADRES</span>
    </footer>
    """


# =============================================================================
# COVER (PAGE 1)
# =============================================================================
def gen_samenvatting_bullets():
    """Samenvatting als BULLETS (regel per onderwerp) i.p.v. lopende tekst.
    Returnt een lijst van (icon, regel-html).
    """
    bj = W.get("bouwjaar", {}).get("value")
    opp = W.get("oppervlakte", {}).get("value")
    woz = WOZ.get("huidige_waarde_eur") if WOZ.get("available") else None
    woz_trend = WOZ.get("trend_pct_per_jaar") if WOZ.get("available") else None
    leef_score = COVER.get("score") if COVER.get("available") else None
    leef_label = COVER.get("label") if COVER.get("available") else None
    leef_buurt_naam = COVER.get("buurt_naam") or "deze buurt"

    bullets = []

    # PAND-karakter
    if bj and opp:
        karakter = "vooroorlogs pand" if bj < 1940 else (
            "naoorlogs pand" if bj < 1990 else "modern pand"
        )
        bullets.append(("PAND",
            f"{karakter.capitalize()} uit <strong>{bj}</strong> "
            f"van <strong>{opp} m²</strong> in <strong>{html.escape(leef_buurt_naam)}</strong>."))

    # LEEFBAAROMETER — toon klasse + empirisch percentiel ("Top X% van NL").
    # De klasse 1-9 is rechtsscheef (klasse 9 = top 13% van NL, niet top 1%);
    # daarom de percentiel-context erbij voor eerlijkheid.
    if leef_score:
        verdict = (
            "ruim boven het Nederlandse gemiddelde" if leef_score >= 7
            else "rond het Nederlandse gemiddelde" if leef_score == 5
            else "onder het Nederlandse gemiddelde"
        )
        top_pct = COVER.get("top_pct_nl") if COVER.get("available") else None
        top_pct_str = _fmt_top_pct(top_pct)
        ctx = f" — <strong>{top_pct_str}</strong>" if top_pct_str else ""
        bullets.append(("BUURT",
            f"Leefbaarometer-score <strong>{leef_score} van 9</strong> ({leef_label or '—'}) "
            f"— {verdict}{ctx}."))

    # WOZ + m²-prijs + trend + laatste 3 peiljaren expliciet
    # We tonen niet alleen de huidige waarde, maar benoemen ook de laatste 3
    # peiljaren met hun jaartal — een puntmeting verbergt of een buurt al jaren
    # stijgt, of net dit jaar plotseling steeg na een vlakke periode.
    if woz and opp:
        m2 = round(woz / opp)
        trend_str = ""
        if woz_trend is not None:
            richting = "stijgt" if woz_trend > 0 else ("daalt" if woz_trend < 0 else "blijft stabiel")
            trend_str = f"; waarde {richting} <strong>{fmt_pct(woz_trend, signed=False)} per jaar</strong>"
        # Laatste 3 peiljaren — historie is nieuwste eerst, we draaien om naar
        # oud → nieuw zodat de pijl-richting natuurlijk leest.
        historie = WOZ.get("historie") or []
        laatste_3 = list(reversed(historie[:3]))
        hist_str = ""
        if len(laatste_3) >= 2:
            chunks = [
                f"<strong>{(h.get('peildatum') or '')[:4]}</strong> {euro(h.get('waarde_eur'))}"
                for h in laatste_3
                if h.get("peildatum") and h.get("waarde_eur") is not None
            ]
            if chunks:
                hist_str = f"<br><span class=\"sub\">Laatste {len(chunks)} peiljaren: {' → '.join(chunks)}</span>"
        bullets.append(("WAARDE",
            f"WOZ <strong>{euro(woz)}</strong> "
            f"(<strong>{euro(m2)} per m²</strong>){trend_str}.{hist_str}"))

    # BEREIKBAARHEID — naam tussen haakjes ALLEEN als bekend (niet "?")
    def _ber_part(items, type_filter, label):
        """Render '<label> op X km (naam)' — naam alleen als beschikbaar."""
        match = next((t for t in items if t.get("type") == type_filter), None)
        if not match: return None
        afstand = fmt_afstand(match.get("meters"))
        nm = match.get("naam")
        suffix = f" ({html.escape(nm)})" if nm else ""
        return f"{label} op <strong>{afstand}</strong>{suffix}"

    transport = [v for v in (VOORZ.get("items") or []) if v.get("categorie") == "transport"]
    ber_parts = []
    for type_filter, label in [
        ("treinstation", "trein"),
        ("metro", "metro"),
        ("tramhalte", "tram"),
        ("bushalte", "bus"),     # NU NIET MEER elif: bus altijd ook tonen
        ("oprit_snelweg", "snelweg-oprit"),
    ]:
        part = _ber_part(transport, type_filter, label)
        if part:
            ber_parts.append(part)
    if ber_parts:
        bullets.append(("BEREIK", " · ".join(ber_parts) + "."))

    # AANDACHT — inhoudelijke regels
    aandacht_lines = []

    crime_total = (V.get("totaal") or {}).get("value")
    if crime_total:
        delta = ((crime_total - 40) / 40) * 100
        if delta < -30:
            aandacht_lines.append(f"Criminaliteit <strong>{abs(delta):.0f}% onder het NL-gemiddelde</strong> — opvallend laag.")
        elif delta < -10:
            aandacht_lines.append(f"Criminaliteit <strong>{abs(delta):.0f}% onder het NL-gemiddelde</strong> — laag, normaal voor stadsgebied.")
        elif delta > 30:
            aandacht_lines.append(f"Criminaliteit <strong>{delta:.0f}% boven het NL-gemiddelde</strong> — verhoogd; check welke delicten.")
        else:
            aandacht_lines.append(f"Criminaliteit rond NL-gemiddelde ({fmt_pct(delta)}).")

    monumenten = VERB.get("wkpb") or []
    monu_types = sorted({(m.get("monument_type") or "monument") for m in monumenten})
    if monu_types:
        aandacht_lines.append(f"Pand is een <strong>{', '.join(monu_types)}</strong> (welstandstoets bij verbouwen).")

    paalrot = (KLIM.get("paalrot") or {}).get("ref", {}).get("chip_level")
    if paalrot == "warn":
        aandacht_lines.append("<strong>Verhoogd paalrot-risico bij klimaatscenario 2050</strong> — funderings­onderzoek vóór aankoop aanbevolen.")
    klim_warns = [r for r in (KLIM.get("risicos") or []) if (r.get("ref") or {}).get("chip_level") == "warn"]
    if klim_warns:
        labels = ", ".join(r["label"].lower() for r in klim_warns[:2])
        aandacht_lines.append(f"Klimaat 2050: verhoogd risico op {labels}.")

    if aandacht_lines:
        bullets.append(("AANDACHT", "<br>".join(aandacht_lines)))

    return bullets


def render_cover():
    leef_buurt_naam = COVER.get("buurt_naam") or A.get("buurt_naam") or "—"
    return f"""
    <section class="page page-cover">
      {phead(f"RAPPORT · {TIJD}")}

      <div class="cover-content">
        <div class="kicker">VOLLEDIG BUURTRAPPORT</div>
        <h1>Wat moet je weten<br>over <em>deze buurt</em>?</h1>

        <div class="cover-address">
          <div class="address-main">{html.escape(A.get('display_name','').split(',')[0])}</div>
          <div class="address-sub">{html.escape(A.get('postcode',''))} {html.escape((A.get('display_name','') or '').split(',')[-1].strip().split(' ',1)[-1] if ',' in (A.get('display_name') or '') else '')} · buurt <strong>{html.escape(leef_buurt_naam)}</strong></div>
        </div>

        <div class="cover-maps">
          <figure>
            <img src="{STREETMAP_SRC}" alt="Straatkaart van {html.escape(A.get('display_name','dit adres'))} via OpenStreetMap, met groene marker op het pand." loading="lazy" />
            <figcaption>STRAATKAART · OPENSTREETMAP</figcaption>
          </figure>
          <figure>
            <img src="{PERCEEL_SRC}" alt="Kadastrale kaart van perceel {(VERB.get('perceel') or {}).get('perceelnummer','')} bij {html.escape(A.get('display_name','dit adres'))} — perceel-grenzen, bebouwing en straatnamen volgens het Kadaster." loading="lazy" />
            <figcaption>KADASTRAAL PERCEEL · KADASTER</figcaption>
          </figure>
        </div>

        <div class="cover-meta">
          <div>
            <div class="label">RAPPORTNR.</div>
            <div class="value-large">{RAPPORTNR}</div>
            <div class="source">{TIJD_KORT}</div>
          </div>
          <div>
            <div class="label">GELDIGHEID</div>
            <div class="value-large">7 dagen</div>
            <div class="source">tot {GELDIG_TOT}</div>
          </div>
          <div>
            <div class="label">GEBASEERD OP</div>
            <div class="value-large">22 bronnen</div>
            <div class="source">Kadaster · CBS · RVO · Politie · RIVM · KNMI · BZK · DUO · OWI · LRK · OSM · DSO · meer</div>
          </div>
        </div>

        <div class="summary-block">
          <div class="kicker">SAMENVATTING</div>
          <ul class="samenvatting-bullets">
            {''.join(f'<li><span class="bul-tag">{tag}</span><span class="bul-tekst">{tekst}</span></li>' for tag, tekst in gen_samenvatting_bullets())}
          </ul>
        </div>
      </div>

      {pfoot(1)}
    </section>
    """


# =============================================================================
# PAGE 2 — Woning
# =============================================================================
def render_woning():
    bj = W.get("bouwjaar", {}).get("value")
    opp = W.get("oppervlakte", {}).get("value")
    elabel = (W.get("energielabel") or {}).get("value")
    elabel_datum = (W.get("energielabel") or {}).get("datum")
    perceel_m2 = (VERB.get("perceel") or {}).get("oppervlakte_m2")
    pand_op_perceel = VERB.get("pand_op_perceel_m2")
    bouwlagen = (VERB.get("pand_hoogte") or {}).get("bouwlagen")
    nokhoogte = (VERB.get("pand_hoogte") or {}).get("nokhoogte_m")
    goothoogte = (VERB.get("pand_hoogte") or {}).get("goothoogte_m")

    stap = VERB.get("stapeling") or {}
    if stap.get("aantal_wonen", 0) > 1 or stap.get("is_gestapeld"):
        type_label = "Appartement"
        type_sub = f"{stap.get('aantal_wonen','?')} woningen in pand"
    else:
        hint = VERB.get("woning_type_hint", "onbekend")
        mapping = {"grondgebonden": ("Vrijstaand / hoek", "1 woning op perceel"),
                   "rij": ("Tussenwoning", "rijhuis"),
                   "rij_of_appartement": ("Rij / appartement", "BAG-pand multi-perceel"),
                   "onbekend": ("Onbekend", "")}
        type_label, type_sub = mapping.get(hint, ("Onbekend", ""))

    bj_chip_data = None
    if bj:
        if bj < 1900: bj_chip_data = ("monumentaal", "neutral")
        elif bj < 1940: bj_chip_data = ("vooroorlogs", "neutral")
        elif bj < 1980: bj_chip_data = ("naoorlogs", "neutral")
        elif bj < 2000: bj_chip_data = ("modern", "neutral")
        else: bj_chip_data = ("nieuwbouw", "neutral")

    return f"""
    <section class="page">
      {phead("WONING")}

      {section_h("01", "DIT PAND", "Bouw, oppervlakte &", "energielabel",
                 "Kerngegevens van dit specifieke pand uit de Basisregistratie Adressen en Gebouwen (BAG) en het officiële energielabel-register van RVO.")}

      <div class="grid-3">
        {stat("Bouwjaar", bj or "—", source="KADASTER · BAG", badge=bj_chip_data[0] if bj_chip_data else None, badge_level=bj_chip_data[1] if bj_chip_data else "neutral")}
        {stat("Woonoppervlakte", opp, unit="m²", source="KADASTER · BAG · GO")}
        {stat("Perceel", perceel_m2, unit="m²", source="KADASTER · BRK")}
      </div>

      <div class="grid-3">
        {stat("Type woning", type_label, source=("KADASTER · BAG-VBO" + (f" · {type_sub}" if type_sub else "")))}
        {stat("Pand-footprint", pand_op_perceel, unit="m²", source="KADASTER · BAG")}
        {stat("Bouwlagen", bouwlagen, source=f"KADASTER · 3D BAG" + (f" · nok {nokhoogte:.1f} m" if nokhoogte else ""))}
      </div>

      <div class="grid-3">
        {stat("Energielabel",
              f'<span class="elabel elabel-{elabel.replace("+","p")}">{elabel}</span>' if elabel else "niet&nbsp;geregistreerd",
              source=f"RVO · EP-ONLINE" + (f" · {elabel_datum}" if elabel_datum else ""))}
        {stat("Gebruiksdoel", ", ".join(W.get("gebruiksdoel") or ["—"]), source="KADASTER · BAG")}
        {stat("Status pand", W.get("status") or "—", source="KADASTER · BAG")}
      </div>

      <div class="grid-3">
        {stat("Nokhoogte", f"{nokhoogte:.1f}" if nokhoogte else "—", unit="m", source="3D BAG · NAP-GECORRIGEERD")}
        {stat("Goothoogte", f"{goothoogte:.1f}" if goothoogte else "—", unit="m", source="3D BAG")}
        {stat("Daktype", vertaal_daktype((VERB.get("pand_hoogte") or {}).get("daktype")), source="3D BAG")}
      </div>

      {pfoot(2)}
    </section>
    """


# =============================================================================
# PAGE 3 — WOZ-waarde & prijsontwikkeling
# =============================================================================
def render_waarde():
    opp = W.get("oppervlakte", {}).get("value")
    huidig = WOZ.get("huidige_waarde_eur") if WOZ.get("available") else None
    historie = WOZ.get("historie", []) if WOZ.get("available") else []
    by_year = {h["peildatum"][:4]: h["waarde_eur"] for h in historie}
    woz_trend = WOZ.get("trend_pct_per_jaar") if WOZ.get("available") else None

    trend_5j = None
    if "2020" in by_year and huidig:
        oud = by_year["2020"]
        trend_5j = ((huidig / oud) ** (1/5) - 1) * 100 if oud else None
    trend_10j = None
    if "2015" in by_year and huidig:
        oud = by_year["2015"]
        trend_10j = ((huidig / oud) ** (1/10) - 1) * 100 if oud else None
    woz_buurt = (WE.get("woz") or {}).get("value")
    m2_prijs = round(huidig / opp) if huidig and opp else None
    ozb_jaar = round(huidig * 0.000498) if huidig else None  # Amsterdam tarief 2025

    # Buurtcontext: hoe verhoudt dit pand zich tot het buurtgemiddelde?
    buurt_context = ""
    if huidig and woz_buurt:
        delta = (huidig - woz_buurt) / woz_buurt * 100
        if delta > 15:
            buurt_context = f"Dit pand zit <strong>{delta:.0f}% boven</strong> het buurtgemiddelde — bovengemiddeld vastgoed."
        elif delta < -15:
            buurt_context = f"Dit pand zit <strong>{abs(delta):.0f}% onder</strong> het buurtgemiddelde — kleiner of minder courant dan typisch."
        else:
            buurt_context = f"Pand-WOZ ligt <strong>rond het buurtgemiddelde</strong> (±{abs(delta):.0f}%)."

    return f"""
    <section class="page">
      {phead("WOZ & PRIJSONTWIKKELING")}

      {section_h("02", "WAARDE & ONTWIKKELING", "Wat is dit pand", "waard",
                 "Officiële WOZ-waarde uit het Kadaster WOZ-Waardeloket, met 12 jaar historie. Aangevuld met buurtgemiddelden van het Centraal Bureau voor de Statistiek.")}

      <div class="grid-3">
        {stat("WOZ 2025 (dit pand)", euro(huidig), source="KADASTER · WOZ-WAARDELOKET")}
        {stat("m²-prijs (dit pand)", euro(m2_prijs), source="WOZ ÷ WOONOPPERVLAKTE")}
        {stat("WOZ buurt (gem.)", euro(woz_buurt), source="CBS · BUURTCIJFERS")}
      </div>

      {f'<p class="bar-context mb-s">{buurt_context}</p>' if buurt_context else ''}

      <div class="kicker mb-s mt-s">HISTORIE</div>
      <div class="grid-3">
        {stat("WOZ 2024", euro(by_year.get("2024")), source="KADASTER · WOZ")}
        {stat("WOZ 2023", euro(by_year.get("2023")), source="KADASTER · WOZ")}
        {stat("WOZ 2022", euro(by_year.get("2022")), source="KADASTER · WOZ")}
      </div>
      <div class="grid-3">
        {stat("WOZ 2021", euro(by_year.get("2021")), source="KADASTER · WOZ")}
        {stat("WOZ 2020", euro(by_year.get("2020")), source="KADASTER · WOZ")}
        {stat("WOZ 2015", euro(by_year.get("2015")), source="KADASTER · WOZ")}
      </div>

      <div class="kicker mb-s mt-s">TREND-ANALYSE</div>
      <div class="grid-3">
        {stat("Trend 5 jr (CAGR)", fmt_pct(trend_5j), source="KADASTER · WOZ", badge="sterk" if trend_5j and trend_5j > 5 else ("matig" if trend_5j and trend_5j > 0 else "—"), badge_level="good" if trend_5j and trend_5j > 5 else "neutral")}
        {stat("Trend 10 jr (CAGR)", fmt_pct(trend_10j), source="KADASTER · WOZ", badge="sterk" if trend_10j and trend_10j > 5 else "matig", badge_level="good" if trend_10j and trend_10j > 5 else "neutral")}
        {stat("OZB-schatting (eigenaar)", f"{euro(ozb_jaar)} / jaar" if ozb_jaar else "—", source=f"GEMEENTE · TARIEF 2025")}
      </div>

      {pfoot(3)}
    </section>
    """


# =============================================================================
# PAGE 3 — WIJK-KARAKTER (de buurt-kern)
# =============================================================================
def render_wijk_karakter():
    leef = COVER if COVER.get("available") else {}
    leef_score = leef.get("score")
    leef_label = leef.get("label", "—")
    leef_buurt_naam = leef.get("buurt_naam") or A.get("buurt_naam") or "—"
    dims = leef.get("dimensies", []) if leef else []
    by_key = {d["key"]: d for d in dims}
    waarschuwing = leef.get("waarschuwing")
    waarschuwing_severity = leef.get("waarschuwing_severity")  # 'mild' | 'strong' | None
    top_pct = leef.get("top_pct_nl")
    top_pct_str = _fmt_top_pct(top_pct)

    # Schaal-visualisatie 1-9
    if leef_score:
        pos_pct = (leef_score - 1) / 8 * 100
        scale_html = f'''
        <div class="leef-scale">
          <div class="scale-track">
            <div class="scale-marker" style="left:{pos_pct}%"></div>
          </div>
          <div class="scale-labels">
            <span>slechtst NL (1)</span>
            <span>NL-gem. (5)</span>
            <span>top NL (9)</span>
          </div>
        </div>
        '''
    else:
        scale_html = ''

    # Trend-block
    ontw = leef.get("ontwikkeling", {}) if leef else {}
    trend_blocks = []
    for key, periode in (("recent","2-jaar (2022→2024)"), ("lang","10-jaar (2014→2024)")):
        o = ontw.get(key)
        if not o: continue
        klasse = o.get("klasse")
        chip_lvl = o.get("chip_level", "neutral")
        beschr = o.get("beschrijving", "")
        veranderingen = o.get("veranderingen", [])
        ver_html = ""
        for v in veranderingen[:3]:
            v_lvl = "good" if v["richting"] == "verbeterd" else "warn"
            ver_html += f'<div class="trend-dim"><span class="lab">{html.escape(v["label"])}</span><span class="rich">{chip(v.get("richting_tekst","?"), v_lvl)}</span></div>'
        trend_blocks.append(f"""
          <div class="trend-block trend-{chip_lvl}">
            <div class="trend-head">
              <span class="trend-period">{periode.upper()}</span>
              {chip(o.get("label","?"), chip_lvl)}
            </div>
            <div class="trend-desc">{html.escape(beschr)}</div>
            {ver_html}
          </div>
        """)

    # Wijk-economie
    inkomen = (WE.get("inkomen_per_inwoner") or {})
    arbeid = (WE.get("arbeidsparticipatie") or {})
    opl = (WE.get("opleiding_hoog") or {})

    return f"""
    <section class="page">
      {phead("LEEFBAARHEID")}

      {section_h("03", "LEEFBAAROMETER", "Hoe prettig is het hier om te", "wonen",
                 "De Leefbaarometer (BZK) combineert ruim 100 indicatoren — voorzieningen, veiligheid, sociale samenhang, woningvoorraad — tot één score per 100-meter-gebied. Schaal 1 (zwak) tot 9 (top).")}

      <div class="leef-hero">
        <div class="leef-score-block">
          <div class="leef-score">{leef_score or '—'}</div>
          {_render_percentile_chip(top_pct)}
        </div>
        <div class="leef-info">
          <div class="leef-label">{html.escape(leef_label.capitalize())}</div>
          <div class="source">Direct rondom het adres (±100&nbsp;m) · Buurt <strong>{html.escape(leef_buurt_naam)}</strong> · Peiljaar 2024</div>
          {scale_html}
        </div>
      </div>

      {_render_balans_waarschuwing(waarschuwing, waarschuwing_severity)}

      <div class="kicker mt-s mb-s">SUB-DIMENSIES (1=zwak · 9=top)</div>
      <div class="grid-3">
        {dim_stat(by_key, "vrz", "Voorzieningen")}
        {dim_stat(by_key, "fys", "Fysieke omgeving")}
        {dim_stat(by_key, "soc", "Sociale samenhang")}
      </div>
      <div class="grid-3">
        {dim_stat(by_key, "onv", "Veiligheid")}
        {dim_stat(by_key, "won", "Woningvoorraad")}
        <div class="stat"></div>
      </div>

      {f'<div class="kicker mt-s mb-s">ONTWIKKELING OVER TIJD</div><div class="grid-2">{"".join(trend_blocks)}</div>' if trend_blocks else ''}

      {pfoot(4)}
    </section>
    """


def render_economie():
    inkomen = (WE.get("inkomen_per_inwoner") or {})
    arbeid = (WE.get("arbeidsparticipatie") or {})
    opl = (WE.get("opleiding_hoog") or {})
    breakdown = opl.get("breakdown") or {}

    return f"""
    <section class="page">
      {phead("WIJK-ECONOMIE")}

      {section_h("04", "WIJK-ECONOMIE", "Inkomen, opleiding en", "arbeidsparticipatie",
                 "Sociaal-economische cijfers van het Centraal Bureau voor de Statistiek voor deze wijk en buurt, vergeleken met het Nederlandse gemiddelde.")}

      <div class="grid-3">
        {stat_with_ref("Gem. inkomen p.p.", euro(inkomen.get("value")), inkomen.get("ref"), source=f"CBS · {inkomen.get('scope','WIJK').upper()}")}
        {stat_with_ref("Arbeidsparticipatie", fmt_pct(arbeid.get("value"), signed=False) if arbeid.get("value") else "—", arbeid.get("ref"), source=f"CBS · {arbeid.get('scope','BUURT').upper()}")}
        {stat_with_ref("Hoogopgeleid (HBO+)", fmt_pct(opl.get("value"), signed=False) if opl.get("value") else "—", opl.get("ref"), source=f"CBS · {opl.get('scope','WIJK').upper()}")}
      </div>

      {f'''
      <div class="kicker mb-s mt-s">OPLEIDINGSNIVEAU IN DE WIJK <span class="ref-inline">NL: ~32% laag · ~38% midden · ~30% hoog</span></div>
      <div class="bar-stacked">
        <div class="bar-seg bar-warn" style="width:{breakdown.get('laag_pct', 0)}%"></div>
        <div class="bar-seg bar-neutral" style="width:{breakdown.get('midden_pct', 0)}%"></div>
        <div class="bar-seg bar-good" style="width:{breakdown.get('hoog_pct', 0)}%"></div>
      </div>
      <div class="bar-legend">
        <span><span class="dot dot-warn"></span> Laag <strong>{breakdown.get("laag_pct","—")}%</strong></span>
        <span><span class="dot dot-neutral"></span> Midden <strong>{breakdown.get("midden_pct","—")}%</strong></span>
        <span><span class="dot dot-good"></span> HBO+WO <strong>{breakdown.get("hoog_pct","—")}%</strong></span>
      </div>
      <p class="bar-context">{opleiding_context(breakdown)}</p>
      ''' if breakdown else ''}

      {pfoot(5)}
    </section>
    """


def dim_stat(by_key, key, default_label):
    d = by_key.get(key, {})
    score = d.get("score")
    label = d.get("label", default_label)
    if score is None:
        return stat(label.upper(), "—")
    verdict = "sterk" if score >= 7 else ("zwak" if score <= 4 else "gem.")
    verdict_lvl = "good" if score >= 7 else ("warn" if score <= 4 else "neutral")
    return stat(label.upper(), score, source=f"score {score}/9", badge=verdict, badge_level=verdict_lvl)


# =============================================================================
# PAGE 4 — Veiligheid + Lucht & Geluid
# =============================================================================
def render_veiligheid_lucht():
    crime = lambda key: (V.get(key) or {}).get("value")
    crime_abs = lambda key: (V.get(key) or {}).get("absoluut_12m")
    crime_nl = {"woninginbraak": 2.3, "geweld": 5.0, "fietsendiefstal": 20, "totaal": 40}

    def crime_stat(key, label):
        v = crime(key)
        nl = crime_nl.get(key)
        if v is not None and nl:
            delta = (v - nl) / nl * 100
            sign = "+" if delta > 0 else "−"
            badge = f"{sign}{abs(delta):.0f}% vs NL"
            badge_lvl = "good" if delta < -10 else ("warn" if delta > 10 else "neutral")
        else:
            badge, badge_lvl = (None, "neutral")
        return stat(label, v if v is not None else "—",
                    unit="/ 1.000",
                    source="POLITIE · OPEN DATA",
                    badge=badge, badge_level=badge_lvl)

    pm25 = (LK.get("pm25") or {}).get("value")
    no2 = (LK.get("no2") or {}).get("value")
    pm10 = (LK.get("pm10") or {}).get("value")
    geluid_per = ((LK.get("geluid") or {}).get("per_bron") or {})

    # SPLITS: 2 secties → 2 pagina's
    return f"""
    <section class="page">
      {phead("VEILIGHEID")}

      {section_h("05", "VEILIGHEID", "Criminaliteit in", "deze buurt",
                 "Geregistreerde misdrijven per 1.000 inwoners over de afgelopen 12 maanden, vergeleken met het Nederlandse gemiddelde.")}

      <div class="grid-3">
        {stat_with_ref("Woninginbraak", crime("woninginbraak"), (V.get("woninginbraak") or {}).get("ref"), unit="/1.000", source="POLITIE")}
        {stat_with_ref("Geweld", crime("geweld"), (V.get("geweld") or {}).get("ref"), unit="/1.000", source="POLITIE")}
        {stat_with_ref("Fietsendiefstal", crime("fietsendiefstal"), (V.get("fietsendiefstal") or {}).get("ref"), unit="/1.000", source="POLITIE")}
      </div>

      <div class="grid-3">
        {stat_with_ref("Totaal /1.000 inw", crime("totaal"), (V.get("totaal") or {}).get("ref"), source="POLITIE")}
        {stat("Misdrijven (12 mnd)", crime_abs("totaal") or "—", source=f"POLITIE · {V.get('periode','')}")}
        <div class="stat"></div>
      </div>

      <p class="bar-context">Cijfers zijn rollend over de laatste 12 maanden ({V.get('periode','—')}). Vergeleken met het Nederlandse gemiddelde van ~40 misdrijven per 1.000 inwoners per jaar.</p>

      {pfoot()}
    </section>

    <section class="page">
      {phead("LUCHTKWALITEIT & GELUID")}

      {section_h("06", "LUCHT & GELUID", "Leefkwaliteit op", "100 meter rondom",
                 "Jaargemiddelden van het RIVM voor fijnstof en stikstofdioxide, en de geluidsbelasting op de gevel volgens de officiële rekenmodellen. WHO-advies = strengste norm; EU-norm = juridische bovengrens.")}

      <div class="grid-3">
        {stat_with_ref("Fijnstof PM2.5", pm25, (LK.get("pm25") or {}).get("ref"), unit="µg/m³", source="RIVM · ADV WHO 5 · EU 25")}
        {stat_with_ref("Fijnstof PM10", pm10, (LK.get("pm10") or {}).get("ref"), unit="µg/m³", source="RIVM · ADV WHO 15 · EU 40")}
        {stat_with_ref("Stikstofdioxide NO₂", no2, (LK.get("no2") or {}).get("ref"), unit="µg/m³", source="RIVM · ADV WHO 10 · EU 40")}
      </div>

      <div class="grid-3">
        {stat_with_ref("Geluid wegverkeer", geluid_per.get("wegverkeer", "—"), (LK.get("geluid") or {}).get("ref"), unit="dB Lden", source="RIVM · WHO 53 · EU-HINDER 55")}
        {_geluid_stat("Geluid railverkeer", geluid_per.get("treinverkeer"), "trein", "Stille buurt qua spoor.")}
        {_geluid_stat("Geluid vliegverkeer", geluid_per.get("vliegverkeer"), "vlieg", "Geen significante vliegtuiglast.")}
      </div>

      {pfoot()}
    </section>
    """


# =============================================================================
# PAGE 5 — Klimaat + Wie woont hier (demografie compleet)
# =============================================================================
def render_klimaat_demografie():
    risico_by_key = {r["key"]: r for r in (KLIM.get("risicos") or [])}
    paalrot = KLIM.get("paalrot") or {}
    rows = []
    klim_table = [
        ("overstroming", "Overstromingskans"),
        ("overstroming_diepte", "Overstromingsdiepte (rampscenario)"),
        ("hittestress", "Hittestress (warme nachten)"),
        ("verschilzetting", "Verschilzetting (zetting)"),
    ]
    for key, label in klim_table:
        r = risico_by_key.get(key)
        if not r: continue
        ref = r.get("ref") or {}
        klasse = r.get("klasse")
        waarde = r.get("waarde")
        eenheid = r.get("eenheid")
        # Hoofd-cel: getal als beschikbaar, anders chip-text
        if waarde is not None:
            display = f"{waarde}&nbsp;{eenheid or ''}".strip()
        elif klasse is not None:
            display = ["—","Zeer laag","Laag","Middel","Hoog","Zeer hoog"][min(klasse, 5)]
        else:
            display = ref.get("chip_text", "—").capitalize()
        # Chip met kleur erbij voor snelle scan
        chip_html = chip(ref.get("chip_text", ""), ref.get("chip_level", "neutral")) if ref.get("chip_text") else ""
        rows.append(f"""
          <tr>
            <td><strong>{html.escape(label)}</strong></td>
            <td>{display}&nbsp;{chip_html}</td>
            <td class="muted">{html.escape(ref.get("betekenis",""))}</td>
          </tr>
        """)
    if paalrot.get("ref"):
        ref = paalrot["ref"]
        chip_html = chip(ref.get("chip_text",""), ref.get("chip_level","neutral"))
        rows.append(f"""
          <tr>
            <td><strong>Paalrot-risico (2050)</strong></td>
            <td>{html.escape(ref.get("chip_text","—").capitalize())}&nbsp;{chip_html}</td>
            <td class="muted">{html.escape(ref.get("betekenis",""))}</td>
          </tr>
        """)
    klim_table_html = f'<table class="risk-table"><tbody>{"".join(rows)}</tbody></table>'

    inwoners = (B.get("inwoners") or {}).get("value")
    huishoudens = (B.get("huishoudensgrootte") or {}).get("value")
    dichtheid = (B.get("dichtheid") or {}).get("value")
    leeftijd = B.get("leeftijdsprofiel") or {}
    fijn = leeftijd.get("fijn", {})
    eigendom = (WE.get("eigendomsverhouding") or {})
    migratie = B.get("migratieachtergrond") or {}
    verkiezing = B.get("verkiezing_tk2023") or B.get("verkiezing_tk2025") or {}
    top3 = verkiezing.get("top3", [])

    return f"""
    <section class="page">
      {phead("KLIMAATRISICO")}

      {section_h("07", "KLIMAATRISICO 2050", "Hoe verandert deze buurt door", "klimaatverandering",
                 f"Risicoprofielen uit de Klimaateffectatlas (KNMI-scenario 'sterk' voor 2050). Bodemtype op deze locatie: <strong>{html.escape(KLIM.get('bodemtype_label','—'))}</strong>.")}

      {klim_table_html}

      {pfoot()}
    </section>

    <section class="page">
      {phead("BEWONERS & DEMOGRAFIE")}

      {section_h("08", "WIE WOONT HIER", "Demografie en", "huishoudens",
                 "Buurtcijfers van het Centraal Bureau voor de Statistiek over inwoners, eigendomsverhouding, leeftijdsopbouw en herkomst — telkens vergeleken met het Nederlandse gemiddelde.")}

      <div class="grid-3">
        {stat("Inwoners (buurt)", f"{inwoners:,}".replace(",",".") if inwoners else "—", source="CBS · BUURTCIJFERS")}
        {stat("Gem. huishoudgrootte", huishoudens or "—", unit="pers.", source="CBS · BUURTCIJFERS")}
        {stat("Dichtheid", f"{int(dichtheid):,}".replace(",",".") if dichtheid else "—", unit="/ km²", source="CBS · BUURTCIJFERS")}
      </div>

      <div class="block-divider"></div>

      <div class="kicker mb-s">EIGENDOMSVERHOUDING (BUURT) <span class="ref-inline">NL: 58% koop · 28% sociale huur · 14% particulier</span></div>
      <div class="bar-stacked">
        <div class="bar-seg bar-good" style="width:{eigendom.get('koop_pct',0)}%" title="Koop"></div>
        <div class="bar-seg bar-warn" style="width:{eigendom.get('sociale_huur_pct',0)}%" title="Sociale huur"></div>
        <div class="bar-seg bar-neutral" style="width:{eigendom.get('particuliere_huur_pct',0)}%" title="Particuliere huur"></div>
      </div>
      <div class="bar-legend">
        <span><span class="dot dot-good"></span> Koop <strong>{eigendom.get('koop_pct','—')}%</strong></span>
        <span><span class="dot dot-warn"></span> Sociale huur <strong>{eigendom.get('sociale_huur_pct','—')}%</strong></span>
        <span><span class="dot dot-neutral"></span> Particuliere huur <strong>{eigendom.get('particuliere_huur_pct','—')}%</strong></span>
      </div>
      <p class="bar-context">{eigendom_context(eigendom)}</p>

      <div class="kicker mb-s mt-s">LEEFTIJDSOPBOUW <span class="ref-inline">NL: 24% jong · 56% midden · 20% 65+</span></div>
      <div class="bar-stacked">
        <div class="bar-seg bar-good" style="width:{(fijn.get('0-15',0) + fijn.get('15-25',0))}%"></div>
        <div class="bar-seg bar-neutral" style="width:{(fijn.get('25-45',0) + fijn.get('45-65',0))}%"></div>
        <div class="bar-seg bar-warn" style="width:{fijn.get('65+',0)}%"></div>
      </div>
      <div class="bar-legend">
        <span><span class="dot dot-good"></span> 0–25 jr <strong>{(fijn.get('0-15',0) + fijn.get('15-25',0)):.1f}%</strong></span>
        <span><span class="dot dot-neutral"></span> 25–65 jr <strong>{(fijn.get('25-45',0) + fijn.get('45-65',0)):.1f}%</strong></span>
        <span><span class="dot dot-warn"></span> 65+ <strong>{fijn.get('65+',0)}%</strong></span>
      </div>
      <p class="bar-context">{leeftijd_context(fijn)}</p>

      <div class="kicker mb-s mt-s">HERKOMST <span class="ref-inline">NL: 76% Nederlands · 10% westers · 14% niet-westers · peiljaar {migratie.get("peiljaar","—")}</span></div>
      <div class="bar-stacked">
        <div class="bar-seg bar-good" style="width:{migratie.get('pct_nederlands',0)}%"></div>
        <div class="bar-seg bar-neutral" style="width:{migratie.get('pct_westers',0)}%"></div>
        <div class="bar-seg bar-warn" style="width:{migratie.get('pct_niet_westers',0)}%"></div>
      </div>
      <div class="bar-legend">
        <span><span class="dot dot-good"></span> Nederlands <strong>{migratie.get('pct_nederlands','—')}%</strong></span>
        <span><span class="dot dot-neutral"></span> Westers <strong>{migratie.get('pct_westers','—')}%</strong></span>
        <span><span class="dot dot-warn"></span> Niet-westers <strong>{migratie.get('pct_niet_westers','—')}%</strong></span>
      </div>
      <p class="bar-context">{herkomst_context(migratie)}</p>

      {tk_block(top3, verkiezing.get('election', 'TK'))}

      {pfoot()}
    </section>
    """


def tk_block(top3, election_label):
    if not top3: return ""
    rows = []
    for p in top3:
        delta = p.get("delta_pct", 0)
        delta_str = f"{'+' if delta > 0 else ''}{delta:.1f}".replace(".",",")
        rows.append(f"""
          <div class="tk-row">
            <span class="partij">{html.escape(p.get('partij',''))}</span>
            <span class="pct-gem">{p.get('pct_gemeente','—')}%</span>
            <span class="pct-nl">NL {p.get('pct_nl','—')}%</span>
            <span class="pct-delta {'pos' if delta > 0 else 'neg'}">{delta_str}</span>
          </div>
        """)
    return f"""
      <div class="tk-block">
        <div class="kicker mb-s">{html.escape(election_label)}-VERKIEZING · GROOTSTE 3 PARTIJEN IN GEMEENTE</div>
        {''.join(rows)}
      </div>
    """


# =============================================================================
# PAGE 6 — Voorzieningen + Onderwijs (alle URLs clickable)
# =============================================================================
def render_voorzieningen():
    """Eigen pagina voor voorzieningen — 7 categorieën in 2-koloms grid."""
    voorz_items = VOORZ.get("items", [])
    cats = {}
    for item in voorz_items:
        c = item.get("categorie", "anders")
        cats.setdefault(c, []).append(item)

    def voorz_inline(items, max_n=4):
        """Compact: TYPE eerst, dan naam — 'Bushalte: Corantijnstraat'."""
        if not items: return ""
        rows = []
        for it in items[:max_n]:
            naam = it.get("naam")
            label = it.get("label") or "—"  # bv "Restaurant", "Bushalte", "Bos"
            meters = it.get("meters")
            m_str = fmt_afstand(meters)
            # Format: "Type: Naam" of alleen "Type" als geen naam
            if naam and naam.lower() != label.lower():
                # TYPE in kleinkapitaal-monospace, NAAM als link
                display = (
                    f'<span class="vtype">{html.escape(label)}:</span> '
                    f'{link(gmaps_link(naam), naam)}'
                )
            elif naam:
                display = link(gmaps_link(naam), naam)
            else:
                display = f'<span class="vtype">{html.escape(label)}</span>'
            rows.append(f'<li><span class="vnaam">{display}</span><span class="vmeters">{m_str}</span></li>')
        return f'<ul class="voorz-inline">{"".join(rows)}</ul>'

    cat_labels = [
        ("transport",     "🚆 OV & VERVOER"),
        ("boodschappen",  "🛒 BOODSCHAPPEN"),
        ("zorg",          "🏥 ZORG"),
        ("kinderen",      "👶 KINDEREN"),
        ("sport",         "⚽ SPORT & GROEN"),
        ("cultuur",       "📚 CULTUUR"),
        ("entertainment", "🍴 ETEN & UITGAAN"),
    ]
    voorz_blocks = []
    for cat_key, cat_label in cat_labels:
        items = cats.get(cat_key, [])
        if not items: continue
        voorz_blocks.append(f"""
          <div class="voorz-cat-block">
            <div class="kicker mb-s">{cat_label} <span class="ref-inline">{len(items)} totaal</span></div>
            {voorz_inline(items, 4)}
          </div>
        """)

    # 2-koloms grid
    half = (len(voorz_blocks) + 1) // 2
    col1 = "".join(voorz_blocks[:half])
    col2 = "".join(voorz_blocks[half:])

    return f"""
    <section class="page">
      {phead("VOORZIENINGEN IN DE OMGEVING")}

      {section_h("09", "VOORZIENINGEN DICHTBIJ", "Wat vind je in de", "directe omgeving",
                 "Locaties binnen 1,5 km, gegroepeerd per categorie. Klik op een naam voor de route via Google Maps.")}

      <div class="voorz-2col">
        <div class="voorz-col">{col1}</div>
        <div class="voorz-col">{col2}</div>
      </div>

      {pfoot(6, total=9)}
    </section>
    """


def render_onderwijs():
    """Eigen pagina voor onderwijs — alle scholen + opvang met SoK-deeplinks."""
    scholen = (ON.get("scholen") or {}).get("top", [])
    opvang = (ON.get("kinderopvang") or {}).get("top", [])
    school_aantal = (ON.get("scholen") or {}).get("aantal", len(scholen))
    opvang_aantal = (ON.get("kinderopvang") or {}).get("aantal_locaties", len(opvang))
    opvang_plaatsen = (ON.get("kinderopvang") or {}).get("totaal_kindplaatsen", "?")
    school_radius = (ON.get("scholen") or {}).get("radius_m", 1500)
    opvang_radius = (ON.get("kinderopvang") or {}).get("radius_m", 500)

    # Inspectie-oordelen tellen
    from collections import Counter
    oordelen = Counter(s.get("inspectie_oordeel") for s in scholen)
    oordeel_samenvatting = " · ".join(f"{n}× {o.lower()}" for o, n in oordelen.items() if o)

    school_rows = []
    for s in scholen:
        oordeel = s.get("inspectie_oordeel", "—")
        oordeel_lvl = "good" if oordeel == "Voldoende" else ("warn" if oordeel == "Onvoldoende" else "neutral")
        url = school_url(s)
        naam_html = link(url, s.get("naam", "—")) if url else html.escape(s.get("naam", "—"))
        school_rows.append(f"""
          <tr>
            <td><strong>{naam_html}</strong>
                <div class="source">{html.escape(s.get('denominatie','—'))} · {html.escape(s.get('adres','') or '')}</div>
            </td>
            <td class="meters-cell">{s.get('meters','?')} m</td>
            <td>{chip(oordeel.lower(), oordeel_lvl)}</td>
            <td class="muted">Inspectie {(s.get('inspectie_peildatum') or '')[:4]}</td>
          </tr>
        """)

    opvang_rows = []
    for o in opvang:
        url = o.get("url")
        naam_html = link(url, o.get("naam", "—")) if url else html.escape(o.get("naam", "—"))
        opvang_rows.append(f"""
          <tr>
            <td><strong>{naam_html}</strong>
                <div class="source">{html.escape(o.get('type','—'))} · {html.escape(o.get('adres','—'))}</div>
            </td>
            <td class="meters-cell">{o.get('meters','?')} m</td>
            <td class="meters-cell">{o.get('kindplaatsen','?')}&nbsp;plaatsen</td>
          </tr>
        """)

    intro = (
        f"Onderwijsinstellingen binnen {school_radius//1000 if school_radius >= 1000 else school_radius/1000:g} km "
        f"met het oordeel van de Onderwijsinspectie. Kinderopvang-locaties binnen "
        f"{opvang_radius} m volgens het Landelijk Register Kinderopvang. "
        f"Alle namen zijn klikbaar — scholen openen op scholenopdekaart.nl, "
        f"opvang in het LRK."
    )

    return f"""
    <section class="page">
      {phead("ONDERWIJS & KINDEROPVANG")}

      {section_h("10", "KINDEREN & ONDERWIJS", "Scholen, opvang &", "kwaliteit", intro)}

      <div class="kicker mb-s">{school_aantal} BASISSCHOLEN BINNEN {school_radius} M <span class="ref-inline">{oordeel_samenvatting}</span></div>
      <table class="onderwijs-table">
        <tbody>{''.join(school_rows)}</tbody>
      </table>

      {f'<div class="kicker mb-s mt-m">{opvang_aantal} KINDEROPVANG-LOCATIES BINNEN {opvang_radius} M <span class="ref-inline">{opvang_plaatsen} totaal kindplaatsen</span></div>' if opvang_rows else ''}
      {('<table class="onderwijs-table"><tbody>' + ''.join(opvang_rows) + '</tbody></table>') if opvang_rows else ''}
      {('<p class="footnote">GGD-inspectierapporten per locatie: klik op de naam → LRK-portaal toont de meest recente onderzoeken. Voor kinderopvang bestaat geen landelijke geaggregeerde oordelen-dataset zoals voor basisscholen.</p>' if opvang_rows else '')}

      {pfoot(7, total=9)}
    </section>
    """


# =============================================================================
# PAGE 7 — Wat kun je verbouwen (mogelijkheden + monument + perceel)
# =============================================================================
def render_verbouwen():
    perceel = VERB.get("perceel") or {}
    achtererf = VERB.get("achtererf") or {}
    pand_h = VERB.get("pand_hoogte") or {}
    wkpb = VERB.get("wkpb") or []
    gem_mon = VERB.get("gem_monument") or {}
    omgvplan = VERB.get("omgevingsplan") or {}
    bestemd = VERB.get("beschermd_gezicht") or {}

    # Monument-status — DEDUPE: zowel wkpb als gem_mon kunnen overlappen
    monu_set = set()
    monu_dates = {}
    for w in wkpb:
        t = (w.get("monument_type") or "monument").strip().lower()
        monu_set.add(t)
        if w.get("datum_in_werking"):
            monu_dates[t] = w["datum_in_werking"]
    if gem_mon.get("is_monument"):
        monu_set.add("gemeentelijk monument")
    monumenten = sorted(monu_set)  # alfabetisch, geen duplicaten

    # Mogelijkheden cards
    cards = VERB.get("mogelijkheden", [])
    card_blocks = []
    for c in cards:
        lvl = c.get("level", "neutral")
        lvl_class = {"good": "good", "warn": "warn", "neutral": "neutral"}.get(lvl, "neutral")
        icon = c.get("icon", "·")
        # Vergunningcheck info
        vc = c.get("vergunningcheck") or {}
        vc_html = ""
        if vc.get("aantal_vragen"):
            vc_html = f'<div class="vc-info">✓ Gecheckt bij Omgevingsloket · {vc.get("aantal_vragen")} vragen · {vc.get("bestuurslaag","?")}</div>'
        card_blocks.append(f"""
          <div class="verb-card verb-{lvl_class}">
            <div class="verb-head">
              <span class="verb-icon">{icon}</span>
              <span class="verb-titel">{html.escape(c.get('titel','—'))}</span>
            </div>
            <div class="verb-samenvatting">{html.escape(c.get('samenvatting','—'))}</div>
            <div class="verb-detail">{html.escape(c.get('detail','—'))}</div>
            {vc_html}
          </div>
        """)

    # Perceel-stats
    diepte = achtererf.get("uitbouw_diepte_max_m")
    onbebouwd = achtererf.get("onbebouwd_m2")
    onbebouwd_pct = achtererf.get("onbebouwd_pct")
    nokhoogte = pand_h.get("nokhoogte_m")
    goothoogte = pand_h.get("goothoogte_m")

    return f"""
    <section class="page">
      {phead("VERBOUWMOGELIJKHEDEN")}

      {section_h("11", "WAT KUN JE VERBOUWEN", "Vergunningen, uitbouw &", "verduurzaming",
                 "Op basis van perceel-geometrie, dak-type en monument-status hebben we voor de vier meest gevraagde verbouwingen ingeschat wat hier mogelijk is. Voor elke optie hebben we de DSO Vergunningcheck van het Omgevingsloket bevraagd.")}

      <div class="grid-3">
        {stat("Monument-status",
              monumenten[0].capitalize() if monumenten else "Geen monument",
              source=("WKPB · BRK-PB" if monumenten else "WKPB · GEEN REGISTRATIE"),
              badge="welstandstoets vereist" if monumenten else "vergunningvrij mogelijk",
              badge_level="warn" if monumenten else "good")}
        {stat("Beschermd stadsgezicht",
              "Ja" if bestemd.get("naam") else "Nee",
              source=(html.escape(bestemd.get("naam","")) if bestemd.get("naam") else "RCE · TOWNSCAPES"),
              badge="welstand-zwaar" if bestemd.get("naam") else None,
              badge_level="warn" if bestemd.get("naam") else "good")}
        {stat("Perceel", perceel.get('oppervlakte_m2','—'), unit="m²", source="KADASTER · BRK")}
      </div>

      <div class="grid-3">
        {stat("Onbebouwd terrein", onbebouwd, unit="m²", source=f"SHAPELY · {onbebouwd_pct:.0f}% van perceel" if onbebouwd_pct else "PERCEEL − PAND")}
        {stat("Max uitbouw-diepte achter", f"{diepte:.1f}".replace(".",",") if diepte else "n.v.t.", unit="m" if diepte else None, source="ACHTERERF − 1 M BURENRECHT" if diepte else "GEEN EIGEN ACHTERTUIN")}
        {stat("Bouwlagen huidig", pand_h.get('bouwlagen','—'), source=f"3D BAG · NOK {nokhoogte:.1f} M" + (f" · GOOT {goothoogte:.1f} M" if goothoogte else "") if nokhoogte else "3D BAG")}
      </div>

      <div class="block-divider"></div>
      <div class="kicker mb-s">CONCRETE MOGELIJKHEDEN</div>

      <div class="verb-grid">
        {''.join(card_blocks)}
      </div>

      <div class="verb-deeplinks">
        <a href="{VERB.get('ruimtelijkeplannen_url','https://omgevingswet.overheid.nl/regels-op-de-kaart')}" target="_blank" rel="noopener">📋 Regels op de Kaart →</a>
        <a href="{VERB.get('omgevingsloket_url','https://omgevingswet.overheid.nl/checken/nieuw/stap/1')}" target="_blank" rel="noopener">🔍 Vergunningcheck Omgevingsloket →</a>
      </div>

      <p class="footnote">
        Omgevingsplan: <strong>{html.escape(omgvplan.get("naam","—"))}</strong> ·
        {omgvplan.get("aantal_activiteiten","?")} activiteiten · {omgvplan.get("aantal_regelteksten","?")} regelteksten van toepassing op deze locatie.
      </p>

      {pfoot(8, total=9)}
    </section>
    """


# =============================================================================
# PAGE 9 — Bereikbaarheid + Bronnen
# =============================================================================
def render_bereikbaarheid_bronnen():
    ber_blocks = []
    # Bereikbaarheid eerst proberen via /bereikbaarheid endpoint, anders via voorz
    if BER.get("available"):
        if BER.get("trein"):
            t = BER["trein"]
            bestemmingen = ", ".join(t.get("bestemmingen", [])[:3]) or "—"
            ber_blocks.append(stat(
                f"Trein — {t['naam']}",
                fmt_afstand(t.get("meters")),
                source=f"OSM · NAAR: {bestemmingen.upper()}",
                badge=f"{t.get('aantal_ic',0)}× IC, {t.get('aantal_sprinter',0)}× sprinter" if t.get('aantal_ic') or t.get('aantal_sprinter') else None,
                badge_level="neutral"))
        if BER.get("metro"):
            m = BER["metro"]
            ber_blocks.append(stat(
                f"Metro — {m.get('naam','?')}",
                fmt_afstand(m.get("meters")),
                source="OSM · LIJN " + ", ".join(m.get("lijnen", [])[:4])))
        if BER.get("tram"):
            t = BER["tram"]
            ber_blocks.append(stat(
                f"Tram — {t.get('naam','?')}",
                fmt_afstand(t.get("meters")),
                source="OSM · LIJN " + ", ".join(t.get("lijnen", [])[:4])))
        if BER.get("bus"):
            b = BER["bus"]
            ber_blocks.append(stat(
                f"Bus — {b.get('naam','?')}",
                fmt_afstand(b.get("meters")),
                source="OSM · LIJN " + ", ".join(b.get("lijnen", [])[:4])))
        if BER.get("snelweg"):
            s = BER["snelweg"]
            ber_blocks.append(stat(
                f"Snelweg — oprit {s.get('naam','?')}",
                fmt_afstand(s.get("meters")),
                source="OSM · OPRIT"))
        for w in BER.get("werkcentra", [])[:3]:
            ber_blocks.append(stat(
                f"Naar {w['stad']}",
                f"{w.get('ov_min','?')} min",
                unit="OV",
                source=f"SCHATTING UIT AFSTAND · {w.get('km','?')} km"))

    # Fallback uit voorzieningen-transport (BER.available was false)
    if not ber_blocks:
        transport = [v for v in VOORZ.get("items", []) if v.get("categorie") == "transport"]
        # Sorteer op categorie-prioriteit: trein > metro > tram > bus > snelweg > rest
        prio = {"treinstation": 1, "overstapstation": 2, "metro": 3, "tramhalte": 4,
                "bushalte": 5, "oprit_snelweg": 6}
        transport.sort(key=lambda v: (prio.get(v.get("type"), 9), v.get("meters", 9999)))
        for v in transport[:6]:
            naam = v.get("naam")
            label = v["label"]
            # Titel = "Type — Naam" (clikbaar als naam) of alleen "Type"
            titel = f"{label} — {naam}" if naam else label
            src = (
                f"OSM · {v.get('source','OSM').upper()}"
                if v.get("source") == "osm"
                else "CBS-BUURTCIJFERS · GEEN NAAM BESCHIKBAAR"
            )
            ber_blocks.append(stat(
                titel, fmt_afstand(v.get("meters")), source=src))
        # Voeg context-zin toe als ALLE items via CBS-fallback komen
        if transport and all(v.get("source") == "cbs" for v in transport):
            ber_blocks.append(
                '<p class="bar-context" style="grid-column: 1 / -1;">'
                'Namen niet beschikbaar — OSM-Overpass was niet bereikbaar bij deze scan; '
                'CBS-buurtafstanden gebruikt als fallback.</p>'
            )

    ber_html = ""
    while ber_blocks:
        chunk = ber_blocks[:3]
        ber_blocks = ber_blocks[3:]
        while len(chunk) < 3: chunk.append('<div class="stat"></div>')
        ber_html += f'<div class="grid-3">{"".join(chunk)}</div>'

    bronnen = [
        ("Kadaster", "Adres- &amp; pandgegevens (BAG), perceel (BRK), WOZ-Waardeloket", "live", "https://www.kadaster.nl"),
        ("Centraal Bureau voor de Statistiek", "Buurtcijfers: demografie, huishoudens, woningvoorraad, inkomen, opleiding", "2024", "https://www.cbs.nl/nl-nl/cijfers/detail/85984NED"),
        ("Rijksdienst voor Ondernemend Nederland", "Officiële energielabels (EP-Online)", "live", "https://www.ep-online.nl"),
        ("Politie", "Geregistreerde misdrijven en meldingen (open data)", "live", "https://data.politie.nl"),
        ("RIVM", "Luchtkwaliteit jaargemiddelden en geluidsbelasting", "2024", "https://www.atlasleefomgeving.nl"),
        ("Klimaateffectatlas", "Klimaatrisico's op basis van KNMI-scenario's", "2025", "https://www.klimaateffectatlas.nl"),
        ("Ministerie van BZK · Leefbaarometer", "Samengestelde leefbaarheidsscore (100-meter-grid)", "2024", "https://www.leefbaarometer.nl"),
        ("DUO &amp; Onderwijsinspectie", "Scholen, oordelen en kinderopvang (LRK)", "live", "https://www.onderwijsinspectie.nl"),
        ("Kiesraad", "Verkiezingsuitslagen per gemeente", "2025", "https://www.kiesraad.nl"),
        ("Rijksdienst Cultureel Erfgoed", "Monumentenregister + beschermd stadsgezicht", "2025", "https://www.cultureelerfgoed.nl"),
        ("OpenStreetMap", "Voorzieningen, OV-haltes, fiets/auto-routes", "live", "https://www.openstreetmap.org"),
        ("Rijksoverheid · PDOK", "Locatiegegevens &amp; adrespunten", "live", "https://www.pdok.nl"),
    ]
    bronnen_rows = "".join(f"""
      <tr>
        <td class="bron-naam"><strong>{link(url, n.replace('&amp;','&')) if url else n}</strong></td>
        <td class="muted">{omschrijving}</td>
        <td class="bron-jaar">{jaar}</td>
      </tr>
    """ for (n, omschrijving, jaar, url) in bronnen)

    return f"""
    <section class="page">
      {phead("BEREIKBAARHEID")}

      {section_h("12", "BEREIKBAARHEID", "Reistijden &", "openbaar vervoer",
                 "Dichtsbijzijnde OV-knooppunten en autobereikbaarheid op basis van OpenStreetMap-routes en POIs.")}

      {ber_html or '<p class="muted small">Geen bereikbaarheids-data beschikbaar voor deze locatie.</p>'}

      {pfoot()}
    </section>

    <section class="page">
      {phead("BRONNEN & VERANTWOORDING")}

      {section_h("13", "BRONNEN & VERANTWOORDING", "Waar", "komt deze data vandaan",
                 f"Alle cijfers in dit rapport zijn op {TIJD} live opgehaald bij de officiële instanties hieronder. Peiljaren staan per cijfer in het rapport zelf vermeld.")}

      <table class="bronnen-table">
        <tbody>{bronnen_rows}</tbody>
      </table>

      <p class="voorbehoud">
        <strong>Voorbehoud.</strong> Dit rapport is samengesteld uit openbare gegevens en geeft een
        momentopname. Buurtscan vervangt geen bouwkundige keuring, taxatie of juridisch advies.
        Voor belangrijke beslissingen raden we aan om naast dit rapport ook onafhankelijk advies in te
        winnen. Bij evidente fouten of ontbrekende gegevens: mail
        <a href="mailto:redactie@buurtscan.nl">redactie@buurtscan.nl</a> en we corrigeren of vergoeden zo snel mogelijk.
      </p>

      {pfoot(copyright=True)}
    </section>
    """


# =============================================================================
# CSS
# =============================================================================
CSS = r"""
@page {
  size: A4;
  margin: 0;
  /* Achtervang-chrome voor fysieke pagina's waar een logische sectie
     overrolt: minimal page number rechts onder. */
  @bottom-right {
    content: counter(page);
    font: 7pt 'IBM Plex Mono', monospace;
    color: #b0b0b0;
    margin: 8mm 14mm 4mm 0;
    letter-spacing: 0.08em;
  }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --ink: #1a1a1a;
  --muted: #767676;
  --line: #e8e6e0;
  --bg: #ffffff;
  --accent: #1f4536;
  --good: #1f7a4a;
  --warn: #b3461b;
  --neutral: #8a8a8a;
  --good-bg: #e8f1ec;
  --warn-bg: #f7e6dc;
  --neutral-bg: #ededeb;
}
html, body {
  background: var(--bg); color: var(--ink);
  font-family: 'Inter', -apple-system, 'Helvetica Neue', sans-serif;
  font-size: 9.5pt; line-height: 1.45; font-weight: 400;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}

/* Pages — elke .page is een hoofdstuk, mag doorrollen.
   page-break-before: always zorgt dat ELKE nieuwe sectie op een nieuwe
   fysieke pagina begint. Eerste sectie (.page-cover) overschrijft dat. */
.page {
  width: 210mm;
  min-height: 297mm;
  padding: 18mm 18mm 16mm;
  page-break-before: always;
  background: white;
  display: flex; flex-direction: column;
}
.page-cover { page-break-before: avoid; }

/* Geef de phead extra ademruimte na zichzelf (na de hairline rule).
   En zet pfoot ALTIJD onderaan via margin-top: auto. */
.phead { margin-bottom: 14mm; }
.pfoot { margin-top: auto; padding-top: 14mm; }

.phead {
  display: grid; grid-template-columns: 1fr auto;
  align-items: center; padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
  font-family: 'IBM Plex Mono', 'Courier New', monospace;
  font-size: 8.5pt; letter-spacing: 0.04em;
  /* margin-bottom in main .phead rule above geeft de ademruimte tot content */
}
.phead .logo {
  display: inline-block; width: 14px; height: 14px;
  background: var(--ink); color: white;
  text-align: center; font-size: 9pt; line-height: 14px;
  font-family: serif; font-weight: 600; margin-right: 4px;
}
.phead-right { font-size: 8pt; letter-spacing: 0.12em; text-align: right; }

.pfoot {
  display: grid; grid-template-columns: 1fr auto;
  border-top: 1px solid var(--line);
  padding-top: 8px;
  /* extra padding-top voor ademruimte komt van .pfoot regel boven */
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7.5pt; letter-spacing: 0.06em;
  color: var(--muted);
}
.pfoot.copyright span:first-child { font-size: 7pt; }

/* Cover */
.page-cover .cover-content { margin-top: 12mm; flex: 1; }
.page-cover h1 {
  font-family: 'Source Serif Pro', 'Georgia', serif;
  font-size: 30pt; line-height: 1.1; font-weight: 400;
  margin: 8px 0 18px; letter-spacing: -0.01em;
}
.cover-maps {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 12px; margin: 16px 0 18px;
}
.cover-maps figure { margin: 0; }
.cover-maps img {
  width: 100%; height: 100px; object-fit: cover;
  border: 1px solid var(--line); border-radius: 2px;
}
.cover-maps figcaption {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7pt; letter-spacing: 0.08em;
  color: var(--muted); margin-top: 4px;
  text-transform: uppercase;
}
.page-cover h1 em { font-style: italic; color: var(--accent); font-weight: 400; }
.cover-address { margin: 14px 0 14px; }
.cover-address .address-main {
  font-family: 'Source Serif Pro', 'Georgia', serif;
  font-size: 18pt; font-weight: 400; letter-spacing: -0.01em;
}
.cover-address .address-sub { font-size: 9.5pt; color: var(--muted); margin-top: 2px; }
.cover-address strong { color: var(--accent); font-weight: 500; }

.cover-meta {
  display: grid; grid-template-columns: 1fr 1fr 1fr;
  gap: 18px; padding: 12px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  margin-bottom: 14px;
}
.cover-meta .label {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7pt; letter-spacing: 0.08em;
  color: var(--muted); margin-bottom: 4px;
}
.cover-meta .value-large {
  font-family: 'Source Serif Pro', 'Georgia', serif;
  font-size: 12pt; margin-bottom: 2px;
}
.cover-meta .source { font-size: 8.5pt; color: var(--muted); }

.summary-block {
  border-left: 2px solid var(--accent);
  padding: 4px 14px; margin-top: 8px;
}
.summary-block .kicker { color: var(--accent); margin-bottom: 6px; }
.summary-block p { font-size: 10pt; line-height: 1.55; }

.samenvatting-bullets { list-style: none; padding: 0; margin: 0; }
.samenvatting-bullets li {
  display: grid;
  grid-template-columns: 70px 1fr;
  gap: 12px;
  padding: 5px 0;
  border-top: 1px solid var(--line);
  font-size: 9.5pt; line-height: 1.45;
}
.samenvatting-bullets li:first-child { border-top: 0; padding-top: 2px; }
.samenvatting-bullets .bul-tag {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7.5pt; letter-spacing: 0.1em;
  color: var(--accent); padding-top: 1px;
}
.samenvatting-bullets .bul-tekst { color: var(--ink); }
.samenvatting-bullets strong { font-weight: 500; color: var(--ink); }

/* Sections */
.kicker {
  font-family: 'IBM Plex Mono', 'Courier New', monospace;
  font-size: 8pt; letter-spacing: 0.12em;
  color: var(--accent); text-transform: uppercase;
}
.mb-s { margin-bottom: 6px; }
.mt-s { margin-top: 12px; }
.mt-m { margin-top: 18px; }
.ref-inline {
  font-family: 'Inter', sans-serif;
  font-size: 7.5pt; font-weight: 400;
  letter-spacing: 0; text-transform: none;
  color: var(--muted); margin-left: 8px;
}
.bar-context {
  font-size: 9pt; color: var(--ink);
  margin: 6px 0 12px; line-height: 1.5;
  font-style: italic;
}
.bar-context strong { color: var(--accent); font-weight: 500; font-style: normal; }
.section-head { margin: 12px 0 6px; }
.section-head h2 {
  font-family: 'Source Serif Pro', 'Georgia', serif;
  font-size: 17pt; font-weight: 400; margin: 2px 0 4px;
  letter-spacing: -0.01em; line-height: 1.15;
}
.section-head h2 em { font-style: italic; color: var(--accent); font-weight: 400; }
.section-head .intro {
  font-size: 9pt; color: var(--ink); line-height: 1.45;
  max-width: 140mm; margin-top: 4px;
}

/* Stats grid */
.grid-3 {
  display: grid; grid-template-columns: 1fr 1fr 1fr;
  gap: 6px 24px; margin: 6px 0;
  padding-top: 8px; border-top: 1px solid var(--line);
}
.grid-3:first-child, .grid-3.no-top { border-top: 0; padding-top: 0; }
.grid-2 {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 10px; margin: 6px 0;
}

.stat { padding-bottom: 4px; }
.stat .label {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7pt; letter-spacing: 0.08em;
  color: var(--muted); margin-bottom: 2px; text-transform: uppercase;
}
.stat .value {
  font-family: 'Source Serif Pro', 'Georgia', serif;
  font-size: 13pt; font-weight: 400; letter-spacing: -0.01em;
  line-height: 1.1; display: flex;
  align-items: baseline; gap: 5px; flex-wrap: wrap;
}
.stat .value .unit {
  font-family: 'Inter', sans-serif; font-size: 9.5pt;
  color: var(--muted); font-weight: 400;
}
.stat .source {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 6.5pt; letter-spacing: 0.06em;
  color: var(--muted); margin-top: 3px; text-transform: uppercase;
}
.stat .ref-nl {
  font-size: 8pt; color: var(--ink); margin-top: 2px;
  font-weight: 500;
}
.stat .ref-bet {
  font-size: 8pt; color: var(--muted); margin-top: 1px;
  line-height: 1.35; font-style: italic;
}

/* Chips */
.chip {
  display: inline-block; padding: 1px 7px;
  font-size: 8pt; font-weight: 400;
  border-radius: 3px; letter-spacing: 0.02em; vertical-align: middle;
}
.chip-good    { background: var(--good-bg); color: var(--good); }
.chip-warn    { background: var(--warn-bg); color: var(--warn); }
.chip-neutral { background: var(--neutral-bg); color: var(--ink); }

/* Energy label */
.elabel {
  display: inline-block; width: 22px; height: 22px;
  text-align: center; font-weight: 600; line-height: 22px;
  border-radius: 2px; color: white; font-size: 11pt;
}
.elabel-Ap, .elabel-A { background: #00aa44; }
.elabel-B { background: #6cca34; }
.elabel-C { background: #b6dc1d; color: #333; }
.elabel-D { background: #f0d11d; color: #333; }
.elabel-E { background: #f5a01d; }
.elabel-F { background: #ed5b14; }
.elabel-G { background: #d72e1f; }

/* Leefbaarometer hero */
.leef-hero {
  display: grid; grid-template-columns: 80px 1fr;
  gap: 22px; align-items: center; margin: 8px 0 12px;
}
.leef-score-block { text-align: center; }
.leef-score {
  font-family: 'Source Serif Pro', 'Georgia', serif;
  font-style: italic; font-size: 50pt; font-weight: 400;
  line-height: 1; text-align: center;
}
/* Empirisch percentiel-chip onder de grote score — toont 'Top X% NL' of
   'Bottom X% NL' met severity-kleur. Eerlijker dan de 1-9 klasse (klasse 9
   = top 13%, geen top 1%). */
.leef-pct {
  display: inline-block;
  margin-top: 6px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 8pt;
  font-weight: 500;
  letter-spacing: 0.01em;
  border: 1px solid transparent;
}
.leef-pct-good    { background: #e4f4eb; border-color: #c2e4cd; color: var(--good); }
.leef-pct-neutral { background: #eef4f2; border-color: #d0e0d8; color: var(--accent); }
.leef-pct-warn    { background: #fbe8e1; border-color: #f0cbbe; color: var(--warn); }
.leef-pct-caption {
  margin-top: 2px;
  font-size: 7pt;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 500;
}
.leef-info .leef-label {
  font-family: 'Source Serif Pro', 'Georgia', serif;
  font-size: 15pt; font-weight: 400; margin-bottom: 2px;
}
.leef-info strong { color: var(--accent); font-weight: 500; }
.leef-scale { margin-top: 14px; }
.scale-track {
  height: 4px;
  background: linear-gradient(to right, var(--warn), var(--neutral) 50%, var(--good));
  border-radius: 2px; position: relative;
}
.scale-marker {
  position: absolute; top: -3px;
  width: 2px; height: 10px; background: var(--ink);
  transform: translateX(-50%);
}
.scale-labels {
  display: grid; grid-template-columns: 1fr 1fr 1fr;
  margin-top: 6px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7pt; letter-spacing: 0.08em; color: var(--muted);
}
.scale-labels span:nth-child(2) { text-align: center; }
.scale-labels span:nth-child(3) { text-align: right; }

/* Callout */
.callout {
  padding: 8px 14px; margin: 10px 0;
  font-size: 9.5pt; border-left: 2px solid var(--accent);
}
.callout-neutral { background: #f8f7f3; }
.callout-mild   { background: #fdf5e6; border-left-color: #d9a441; color: #5a4215; }
.callout-strong { background: #fbe8e1; border-left: 3px solid var(--warn); color: #6a2818; }
.callout-strong strong { color: #6a2818; }

/* Trend blocks */
.trend-block {
  border: 1px solid var(--line); border-radius: 4px;
  padding: 10px 14px;
}
.trend-block.trend-good { border-left: 3px solid var(--good); background: var(--good-bg); }
.trend-block.trend-warn { border-left: 3px solid var(--warn); background: var(--warn-bg); }
.trend-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 6px;
}
.trend-period {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7.5pt; letter-spacing: 0.08em; color: var(--muted);
}
.trend-desc { font-size: 9.5pt; color: var(--ink); margin-bottom: 4px; }
.trend-dim {
  display: flex; justify-content: space-between;
  padding: 2px 0; font-size: 9pt;
}
.trend-dim .lab { color: var(--muted); }

/* Tables */
.risk-table, .voorz-table, .onderwijs-table, .bronnen-table {
  width: 100%; border-collapse: collapse; margin: 6px 0;
  font-size: 9pt;
}
.risk-table tr, .voorz-table tr, .onderwijs-table tr, .bronnen-table tr {
  border-top: 1px solid var(--line);
}
.risk-table tr:first-child, .voorz-table tr:first-child,
.onderwijs-table tr:first-child, .bronnen-table tr:first-child { border-top: none; }
.risk-table td, .voorz-table td, .onderwijs-table td, .bronnen-table td {
  padding: 5px 8px 5px 0; vertical-align: top;
}
.risk-table td:first-child { width: 35%; }
.risk-table td:nth-child(2) { width: 22%; }
.risk-table td:nth-child(3) { color: var(--muted); font-size: 9pt; }

.voorz-cat-block { margin-bottom: 16px; }
.voorz-table .label-cell {
  width: 28%;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 8pt; letter-spacing: 0.06em;
  color: var(--muted); text-transform: uppercase;
}
.voorz-table .naam-cell { font-weight: 500; }
.voorz-table .meters-cell {
  width: 18%; text-align: right; color: var(--muted);
  font-variant-numeric: tabular-nums;
}

/* Voorzieningen — 2-kolom layout met inline-lijsten per categorie */
.voorz-2col {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 24px; margin-top: 8px;
}
.voorz-inline {
  list-style: none; padding: 0; margin: 4px 0 0;
}
.voorz-inline li {
  display: flex; justify-content: space-between;
  padding: 4px 0; border-top: 1px solid var(--line);
  font-size: 9.5pt; gap: 12px;
}
.voorz-inline li:first-child { border-top: 0; }
.voorz-inline .vnaam { color: var(--ink); }
.voorz-inline .vtype {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7.5pt; color: var(--muted);
  letter-spacing: 0.04em; margin-left: 6px;
}
.voorz-inline .vmeters {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 8pt; color: var(--muted);
  font-variant-numeric: tabular-nums; flex-shrink: 0;
}

.onderwijs-table td:nth-child(2) { width: 18%; }
.onderwijs-table td:nth-child(3) {
  width: 22%; text-align: right;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 8pt; color: var(--muted); letter-spacing: 0.06em;
}
.onderwijs-table strong { font-weight: 500; }
.onderwijs-table .source { margin-top: 2px; font-size: 8pt; }
.onderwijs-table .meters-cell { text-align: right; color: var(--muted); }

.bronnen-table .bron-naam { width: 30%; }
.bronnen-table .bron-jaar {
  width: 10%; text-align: right;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 8pt; color: var(--muted); letter-spacing: 0.06em;
}

/* Bar segments */
.block-divider { border-top: 1px solid var(--line); margin: 18px 0 12px; }
.bar-stacked {
  display: flex; height: 12px; border-radius: 2px; overflow: hidden;
  margin: 6px 0 4px; background: var(--neutral-bg);
}
.bar-seg { height: 100%; }
.bar-seg.bar-good { background: var(--good); }
.bar-seg.bar-warn { background: var(--warn); }
.bar-seg.bar-neutral { background: var(--neutral); }
.bar-legend {
  display: flex; gap: 24px; font-size: 9.5pt; flex-wrap: wrap;
  margin-top: 4px;
}
.bar-legend .dot {
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 4px; vertical-align: middle;
}
.dot-good { background: var(--good); }
.dot-warn { background: var(--warn); }
.dot-neutral { background: var(--neutral); }
.bar-legend strong { font-weight: 500; color: var(--accent); }

/* TK */
.tk-block { margin: 14px 0; padding-top: 12px; border-top: 1px solid var(--line); }
.tk-row {
  display: grid; grid-template-columns: 1fr auto auto auto;
  gap: 16px; align-items: baseline;
  padding: 5px 0; border-top: 1px solid var(--line); font-size: 10pt;
}
.tk-row:first-of-type { border-top: 0; }
.tk-row .partij { font-weight: 500; }
.tk-row .pct-gem {
  font-family: 'Source Serif Pro', 'Georgia', serif; font-size: 12pt;
}
.tk-row .pct-nl {
  color: var(--muted); font-size: 8.5pt;
  font-family: 'IBM Plex Mono', monospace; letter-spacing: 0.04em;
}
.tk-row .pct-delta {
  font-family: 'IBM Plex Mono', monospace; font-size: 9pt;
  font-weight: 500; min-width: 38px; text-align: right;
}
.tk-row .pct-delta.pos { color: var(--good); }
.tk-row .pct-delta.neg { color: var(--warn); }

/* Links */
a {
  color: var(--accent); text-decoration: none;
  border-bottom: 1px solid #b8c8c0;
  transition: border-color 0.15s;
}
a:hover { border-bottom-color: var(--accent); }

/* Misc */
.muted { color: var(--muted); }
.small { font-size: 9pt; }
.footnote {
  margin-top: 10px; font-size: 8.5pt; color: var(--muted);
  border-top: 1px solid var(--line); padding-top: 8px;
}
.voorbehoud {
  margin: 18px 0 12px; padding: 12px 16px;
  background: #f8f7f3; font-size: 9pt; line-height: 1.6;
  border-left: 2px solid var(--accent);
}
.voorbehoud strong { color: var(--accent); }

/* Verbouwen-cards */
.verb-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  margin: 6px 0 12px;
}
.verb-card {
  border: 1px solid var(--line); border-radius: 4px;
  padding: 8px 12px; font-size: 8.5pt;
  page-break-inside: avoid;
}
.verb-card .verb-titel { font-size: 11pt; }
.verb-card .verb-samenvatting { font-size: 9pt; }
.verb-card .verb-detail { font-size: 8pt; line-height: 1.4; }
.verb-card.verb-good { border-left: 3px solid var(--good); background: #f6fbf8; }
.verb-card.verb-warn { border-left: 3px solid var(--warn); background: #fdf8f5; }
.verb-card.verb-neutral { border-left: 3px solid var(--neutral); background: #fafaf7; }
.verb-head {
  display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
}
.verb-icon { font-size: 12pt; }
.verb-titel {
  font-family: 'Source Serif Pro', Georgia, serif;
  font-size: 13pt; font-weight: 500;
}
.verb-samenvatting {
  font-weight: 500; color: var(--ink);
  font-size: 10pt; margin-bottom: 6px;
}
.verb-detail {
  color: var(--ink); font-size: 9pt;
  line-height: 1.5; margin-bottom: 8px;
}
.vc-info {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 7.5pt; letter-spacing: 0.06em;
  color: var(--good); padding-top: 6px;
  border-top: 1px solid var(--line); text-transform: uppercase;
}
.verb-deeplinks {
  display: flex; gap: 12px; margin: 16px 0;
}
.verb-deeplinks a {
  flex: 1; padding: 10px 14px; text-align: center;
  background: #f8f7f3; border: 1px solid var(--line);
  border-radius: 4px; font-size: 10pt; font-weight: 500;
  color: var(--accent); border-bottom-color: var(--line);
}
"""

# =============================================================================
# RENDER
# =============================================================================

def render_html(data: dict) -> str:
    """Render volledig HTML-rapport voor één adres.

    `data` is een dict met:
      scan, woz, voorz, klim, ber, extras, verb       — JSON-responses
      streetmap_png, perceel_png                       — bytes (optioneel)
    """
    _set_globals(data)
    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="utf-8">
  <title>Buurtscan — {html.escape(A.get('display_name','—'))}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Source+Serif+Pro:ital,wght@0,400;0,500;1,400;1,500&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>{CSS}</style>
</head>
<body>
  {render_cover()}
  {render_woning()}
  {render_waarde()}
  {render_wijk_karakter()}
  {render_economie()}
  {render_veiligheid_lucht()}
  {render_klimaat_demografie()}
  {render_voorzieningen()}
  {render_onderwijs()}
  {render_verbouwen()}
  {render_bereikbaarheid_bronnen()}
</body>
</html>
"""


# Standalone modus voor lokaal testen — gebruikt /tmp/p72_*.json
if __name__ == "__main__":
    ROOT = Path("/tmp")
    test_data = {
        "scan":   json.loads((ROOT / "p72_scan.json").read_text()),
        "woz":    json.loads((ROOT / "p72_woz.json").read_text()),
        "voorz":  json.loads((ROOT / "p72_voorz.json").read_text()),
        "klim":   json.loads((ROOT / "p72_klim.json").read_text()),
        "ber":    json.loads((ROOT / "p72_ber.json").read_text()),
        "extras": json.loads((ROOT / "p72_extras.json").read_text()),
        "verb":   json.loads((ROOT / "p72_verb.json").read_text()),
        # streetmap_png/perceel_png niet meegegeven — fallback naar /tmp paths
    }
    out_html = render_html(test_data)
    OUT = Path("/tmp/rapport_v2.html")
    OUT.write_text(out_html)
    print(f"Geschreven: {OUT} ({OUT.stat().st_size:,} bytes)")
