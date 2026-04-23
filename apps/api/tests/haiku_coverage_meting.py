"""
Haiku-coverage meting na dso.py fixes.

Doel: meten of de productie-pipeline (fetch_bp_regeltekst_voor_locatie +
extract_bp_regels) nu wel hits geeft op dezelfde 5 adressen waar het in de
vorige run 0/4 scoorde.

Per adres meten we 4 fasen zodat we bij failure weten waar het misgaat:
  Fase 1: aantal regelteksten van DSO (input set)
  Fase 2: aantal wIds na inhoud-filter
  Fase 3: aantal regelteksten met score >= 1 (= bouwhoogte-keywords)
  Fase 4: Haiku-extractie geslaagd? met welke waarden?
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httpx

from adapters import dso, bp_extractor

ADRESSEN = [
    {"slug": "amsterdam-paramaribo7", "label": "Amsterdam — Paramaribostraat 7", "rd": (118918, 487447)},
    {"slug": "amsterdam-paramaribo72-1", "label": "Amsterdam — Paramaribostraat 72-1", "rd": (118845, 485495)},
    {"slug": "rotterdam-witte-de-with", "label": "Rotterdam — Witte de Withstraat 50", "rd": (92500, 437200)},
    {"slug": "utrecht-leidsche-rijn", "label": "Utrecht — Leidsche Rijn", "rd": (130850, 456400)},
    {"slug": "deventer-binnenstad", "label": "Deventer — binnenstad", "rd": (208300, 472900)},
]


async def _diagnose_adres(adres: dict) -> dict:
    """4-fasen-diagnose per adres."""
    rd_x, rd_y = adres["rd"]
    out = {**adres}
    headers = dso._auth_headers()
    if not headers:
        return {**out, "fout": "geen DSO_API_KEY"}

    t0 = time.time()
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        # Fase 1: omgevingsplan + regelteksten van locatie
        regs = await dso._zoek_regelingen(client, rd_x, rd_y)
        op = dso._pick_omgevingsplan(regs)
        if op is None:
            return {**out, "fout": "geen omgevingsplan"}
        out["plan"] = op.officiele_titel

        enc = dso._encode_uri(op.uri_identificatie)
        url = f"{dso.DSO_PRES_BASE}/regelingen/{enc}/regeltekstannotaties/_zoek"
        body = {"geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
        hdr = {**headers, "Content-Type": "application/json", "Content-Crs": dso.RD_CRS}
        try:
            resp = await client.post(url, json=body, headers=hdr, params={"size": 50})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return {**out, "fout": f"annotaties-call: {e}"}

        regelteksten = data.get("regelteksten") or []
        out["fase1_regelteksten_totaal"] = len(regelteksten)

        # Fase 2: wIds-filter resultaat
        _PAT = ("__art_", "__para_", "__subsec_", "__sec_")
        wIds = [
            r.get("wId") for r in regelteksten
            if r.get("wId") and any(p in r["wId"] for p in _PAT)
        ]
        out["fase2_wIds_na_filter"] = len(wIds)
        if not wIds:
            out["fout_fase"] = "wIds-filter leverde 0 op"
            return out

    # Fase 3: parallel regelteksten ophalen + scoren
    headers = dso._auth_headers()
    sem = asyncio.Semaphore(12)
    scored: list[tuple[int, str]] = []

    async def _w(client, wId):
        async with sem:
            tekst = await dso._fetch_regeltekst_tekst(client, op.uri_identificatie, wId)
            if not tekst:
                return
            score = dso._relevantie_score(tekst)
            if score >= 1:
                scored.append((score, tekst))

    to_fetch = wIds[:250]
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        await asyncio.gather(*[_w(client, w) for w in to_fetch])
    out["fase3_geprobeerde_wIds"] = len(to_fetch)
    out["fase3_treffers_met_keywords"] = len(scored)

    if not scored:
        out["fout_fase"] = "geen tekst met bouwhoogte-keywords"
        out["fase_dt_s"] = round(time.time() - t0, 1)
        return out

    # Fase 4: top-5 naar Haiku
    scored.sort(reverse=True)
    top = scored[:5]
    haiku_input = "\n\n".join(t for _s, t in top)
    out["fase4_haiku_input_chars"] = len(haiku_input)
    out["fase4_top_scores"] = [s for s, _ in top]

    try:
        result = bp_extractor.extract_bp_regels(haiku_input)
    except Exception as e:
        out["fout_fase"] = f"Haiku-fout: {e}"
        out["fase_dt_s"] = round(time.time() - t0, 1)
        return out

    if result is None:
        out["fout_fase"] = "Haiku gaf None"
    else:
        out["haiku"] = {
            "max_bouwhoogte_m": result.max_bouwhoogte_m,
            "max_goothoogte_m": result.max_goothoogte_m,
            "max_bouwlagen": result.max_bouwlagen,
            "bestemming": result.bestemming,
        }

    out["fase_dt_s"] = round(time.time() - t0, 1)
    return out


async def main():
    if not os.getenv("DSO_API_KEY") or not os.getenv("ANTHROPIC_API_KEY"):
        print("MISSING: DSO_API_KEY of ANTHROPIC_API_KEY")
        sys.exit(1)
    print(f"Haiku-coverage meting — {len(ADRESSEN)} adressen\n")

    rapport = []
    for ad in ADRESSEN:
        print(f"\n=== {ad['label']} ===")
        try:
            r = await _diagnose_adres(ad)
            rapport.append(r)
        except Exception as e:
            print(f"  EXC: {e}")
            rapport.append({**ad, "fout": repr(e)})
            continue
        if r.get("fout"):
            print(f"  FOUT: {r['fout']}")
            continue
        print(f"  Plan: {r.get('plan','-')}  ({r.get('fase_dt_s','?')}s)")
        print(f"  Fase 1 — regelteksten: {r.get('fase1_regelteksten_totaal', '-')}")
        print(f"  Fase 2 — wIds na filter: {r.get('fase2_wIds_na_filter', '-')}")
        print(f"  Fase 3 — keyword-treffers: {r.get('fase3_treffers_met_keywords', '-')} "
              f"van {r.get('fase3_geprobeerde_wIds', '-')} geprobeerd")
        if "fout_fase" in r:
            print(f"  STOP: {r['fout_fase']}")
            continue
        print(f"  Fase 4 — Haiku-input: {r.get('fase4_haiku_input_chars','-')} chars, "
              f"top-scores={r.get('fase4_top_scores','-')}")
        if r.get("haiku"):
            h = r["haiku"]
            print(f"  HAIKU: bouwhoogte={h['max_bouwhoogte_m']} m, "
                  f"goothoogte={h['max_goothoogte_m']} m, "
                  f"bouwlagen={h['max_bouwlagen']}, "
                  f"bestemming={h['bestemming']!r}")

    # Eindoverzicht
    print(f"\n{'='*72}\nEINDRAPPORT — coverage")
    print('='*72)
    print(f"{'Adres':<42} {'rt':>5} {'wId':>5} {'kw':>5} {'h':>10}")
    print("-"*72)
    for r in rapport:
        h_str = "-"
        if r.get("haiku") and r["haiku"]["max_bouwhoogte_m"]:
            h_str = f"{r['haiku']['max_bouwhoogte_m']} m"
        elif r.get("fout") or r.get("fout_fase"):
            h_str = "FOUT"
        print(f"{r['label'][:40]:<42} "
              f"{r.get('fase1_regelteksten_totaal','-'):>5} "
              f"{r.get('fase2_wIds_na_filter','-'):>5} "
              f"{r.get('fase3_treffers_met_keywords','-'):>5} "
              f"{h_str:>10}")
    n_ok = sum(1 for r in rapport if r.get("haiku") and r["haiku"].get("max_bouwhoogte_m"))
    print(f"\nHaiku-bouwhoogte gevonden: {n_ok}/{len(rapport)}")


if __name__ == "__main__":
    asyncio.run(main())
