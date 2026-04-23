"""
BP-extractor — haalt structured bouwregels uit ongestructureerde plan-tekst.

Waarom een LLM hier: bestemmingsplan- en omgevingsplan-regels zijn juridische
tekst ("de bouwhoogte mag niet meer bedragen dan 9 meter, gemeten vanaf peil,
met uitzondering van ondergeschikte bouwdelen zoals schoorstenen, antennes en
liftopbouwen"). Reguliere regex-extractie mist context (welke hoogte geldt voor
welk bestemmingsvlak, wat zijn uitzonderingen, etc.). Claude Haiku kan dit met
90%+ accuracy parseren naar een vaste schema — goedkoop én snel genoeg voor
elk scan-verzoek.

Strategie:
1. Input: ruwe Nederlandse plan-tekst (gekregen uit DSO API in Fase 2b;
   voor nu uit een handmatige testset).
2. Prompt dwingt JSON-only output met vaste keys.
3. Parsing + validatie — bij schema-afwijking retourneren we None (conservatief
   i.p.v. vals-positief bouwhoogte-claim aan een koper tonen).

Kosten: Haiku ~$1/M input + $5/M output. Typische plantekst ~1500 tokens input,
output <100 tokens → ~$0.002 per extractie. Met 30-dagen cache op plan-ID is
de kostenvoetafdruk verwaarloosbaar.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

# Import lazy: anthropic-SDK ontbreekt lokaal in sommige dev-setups.
try:
    import anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

# Haiku is ruim voldoende voor gestructureerde NL-tekst → JSON extractie.
# Sonnet als jumping-point geven we later als validatie onder target blijft.
MODEL = "claude-haiku-4-5"

# Systeem-prompt dwingt JSON-only output. Veldnamen staan vast om downstream
# parsing eenvoudig te maken. Alle getallen INTEGER of null.
_SYSTEM_PROMPT = """Je bent een extractie-assistent voor Nederlandse bestemmingsplan- en
omgevingsplanregels. Je krijgt de regel-tekst van een bestemmingsvlak voor een
specifiek perceel. Extraheer de volgende velden naar JSON:

- max_bouwhoogte_m: maximaal toegestane bouwhoogte in meters (getal) of null
- max_goothoogte_m: maximale goothoogte in meters (getal) of null
- max_bouwlagen: maximaal aantal bouwlagen (getal) of null
- max_bebouwingspercentage: maximum bebouwingspercentage van het perceel (0-100, getal) of null
- kap_verplicht: true als kap verplicht is, false als plat dak toegestaan, null als onbekend
- plat_dak_toegestaan: true/false, null als onbekend
- bestemming: de hoofdbestemming (bv 'Wonen', 'Centrum-1', 'Gemengd-2'), null als niet aangegeven
- toelichting: korte (max 30 woorden) Nederlandse samenvatting voor een leek

REGELS:
1. Antwoord UITSLUITEND met één valide JSON-object, zonder uitleg ervoor/na.
2. Bij twijfel → null (niet raden).
3. Ondergeschikte bouwdelen (schoorstenen, antennes) tellen niet mee voor max_bouwhoogte_m.
4. Als de tekst expliciet "kap verplicht" of "alleen kapwoningen" vermeldt → kap_verplicht=true, plat_dak_toegestaan=false.
5. Als de tekst "plat dak toegestaan" of "zowel plat als kap" vermeldt → plat_dak_toegestaan=true.
6. max_bebouwingspercentage alleen als expliciet genoemd als PERCENTAGE; niet als m²-cap.
"""


@dataclass
class BPRegels:
    """Gestructureerde bouwregels uit plantekst-extractie."""
    max_bouwhoogte_m: Optional[float] = None
    max_goothoogte_m: Optional[float] = None
    max_bouwlagen: Optional[int] = None
    max_bebouwingspercentage: Optional[int] = None
    kap_verplicht: Optional[bool] = None
    plat_dak_toegestaan: Optional[bool] = None
    bestemming: Optional[str] = None
    toelichting: Optional[str] = None
    # Meta
    extractie_model: Optional[str] = None
    ruwe_tekst_lengte: Optional[int] = None


def _coerce_field(raw: dict, key: str, want: str):
    """Pak raw[key] en cast naar het verwachte type, of None bij failure."""
    v = raw.get(key)
    if v is None:
        return None
    try:
        if want == "int":
            return int(float(v))
        if want == "float":
            return float(v)
        if want == "bool":
            return bool(v) if isinstance(v, bool) else None  # strikte bool-check
        if want == "str":
            return str(v) if v else None
    except (TypeError, ValueError):
        return None
    return None


def _parse_response(txt: str) -> Optional[dict]:
    """Trek een JSON-object uit de modeloutput.

    Haiku volgt meestal de JSON-only instructie, maar soms plakt 'ie er tekst
    voor of na. We zoeken naar het eerste `{` en laatste `}` en parsen daarlussen.
    """
    if not txt:
        return None
    i = txt.find("{")
    j = txt.rfind("}")
    if i < 0 or j < 0 or j <= i:
        return None
    try:
        return json.loads(txt[i : j + 1])
    except json.JSONDecodeError:
        return None


def extract_bp_regels(
    regel_tekst: str,
    api_key: Optional[str] = None,
    model: str = MODEL,
) -> Optional[BPRegels]:
    """Hoofdentry: stuur plan-tekst naar Haiku, krijg gestructureerde BPRegels.

    Retourneert None als:
    - anthropic-SDK niet geïnstalleerd
    - geen API-key beschikbaar (noch arg, noch env `ANTHROPIC_API_KEY`)
    - Haiku-call faalt (netwerk/rate-limit)
    - response niet valide JSON

    Conservatieve houding: liever None dan fout bouwhoogte-claim richting koper.
    """
    if not regel_tekst or not regel_tekst.strip():
        return None
    if not _HAS_SDK:
        return None
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None

    client = anthropic.Anthropic(api_key=key)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Planregel-tekst:\n\n```\n{regel_tekst.strip()}\n```\n\nGeef JSON.",
                }
            ],
        )
    except Exception:
        return None

    txt = ""
    for block in msg.content or []:
        if getattr(block, "type", "") == "text":
            txt += getattr(block, "text", "")
    parsed = _parse_response(txt)
    if not parsed:
        return None

    return BPRegels(
        max_bouwhoogte_m=_coerce_field(parsed, "max_bouwhoogte_m", "float"),
        max_goothoogte_m=_coerce_field(parsed, "max_goothoogte_m", "float"),
        max_bouwlagen=_coerce_field(parsed, "max_bouwlagen", "int"),
        max_bebouwingspercentage=_coerce_field(parsed, "max_bebouwingspercentage", "int"),
        kap_verplicht=_coerce_field(parsed, "kap_verplicht", "bool"),
        plat_dak_toegestaan=_coerce_field(parsed, "plat_dak_toegestaan", "bool"),
        bestemming=_coerce_field(parsed, "bestemming", "str"),
        toelichting=_coerce_field(parsed, "toelichting", "str"),
        extractie_model=model,
        ruwe_tekst_lengte=len(regel_tekst),
    )
