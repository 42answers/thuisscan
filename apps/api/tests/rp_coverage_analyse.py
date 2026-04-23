"""
Ruimtelijke Plannen v4 — coverage-analyse op 20 adressen.

Doel: vaststellen WAAR de huidige `ruimtelijke_plannen.fetch_maatvoeringen()`
het laat afweten, zodat we gericht de adapter kunnen verbreden.

Per adres meten we 5 fasen:
  1. Geocoding via PDOK Locatieserver (ground-truth RD)
  2. RP `/plannen/_zoek` → lijst plannen + types
  3. Filter op huidige BP/beheersverordening → kandidaten
  4. Per kandidaat: maatvoeringen geo-zoek + plan-niveau
  5. Welke `naam`-velden komen voor in `omvang[]` (om te zien of er buiten
     "bouwhoogte/goothoogte" andere relevante naamvelden zijn)

Eindrapport:
  - Coverage % met huidige adapter
  - Diagnose top-3 mis-redenen
  - Welke alternatieve plan-types of naam-substrings extra coverage geven
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httpx

from adapters import ruimtelijke_plannen as rp
from adapters import pdok_locatie

# 20 adressen — diverse mix van regio, bouwperiode, plan-type
ADRESSEN = [
    "Paramaribostraat 7, Amsterdam",
    "Buitenveldertselaan 200, Amsterdam",
    "Witte de Withstraat 50, Rotterdam",
    "Dorpsstraat 100, Nesselande",
    "Bezuidenhoutseweg 30, Den Haag",
    "Domplein 1, Utrecht",
    "Vleutensevaart 100, Utrecht",            # Leidsche Rijn
    "Stratumseind 1, Eindhoven",
    "Vismarkt 1, Groningen",
    "Vrijthof 30, Maastricht",
    "Sixlaan 4, Hillegom",
    "Stationsstraat 100, Almere",
    "Het Rond 1, Houten",
    "Brink 5, Roden",
    "Markt 1, Drachten",
    "Markt 1, Domburg",
    "Theodoor Dorrenplein 1, Valkenburg aan de Geul",
    "Park 30, Nuenen",
    "Markt 1, Lichtenvoorde",
    "Dorpsstraat 1, Vlieland",
]


async def _geocode(client, q: str):
    """Locatieserver lookup → RD."""
    try:
        m = await pdok_locatie.geocode(q)
        if m and m.rd_x and m.rd_y:
            return (m.rd_x, m.rd_y, m.display_name or q)
    except Exception as e:
        print(f"    geocode-exc: {e}")
    return None


async def _zoek_plannen_full(client, rd_x, rd_y):
    """Identiek aan rp._zoek_plannen maar bewaart de FULL plannen-lijst."""
    auth = rp._auth_headers() or {}
    headers = {**auth, "Content-Type": "application/json", "Content-Crs": "epsg:28992"}
    body = {"_geo": {"intersects": {"type": "Point", "coordinates": [rd_x, rd_y]}}}
    try:
        resp = await client.post(
            f"{rp.RP_BASE}/plannen/_zoek",
            params={"size": 50},
            headers=headers,
            json=body,
            timeout=rp.TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get("_embedded") or {}).get("plannen") or []
    except Exception as e:
        return [{"_error": repr(e)}]


async def _diagnose_adres(adres_q: str) -> dict:
    """Volledige fase-per-fase diagnose voor één adres."""
    out: dict = {"query": adres_q}

    # Fase 1: geocoding
    async with httpx.AsyncClient(timeout=15) as gc:
        loc = await _geocode(gc, adres_q)
    if not loc:
        out["fout"] = "geocoding mislukt"
        return out
    rd_x, rd_y, label = loc
    out["label"] = label
    out["rd"] = (rd_x, rd_y)

    headers = rp._auth_headers()
    if not headers:
        out["fout"] = "geen RP_API_KEY"
        return out

    async with httpx.AsyncClient(timeout=rp.TIMEOUT_S, headers=headers) as client:
        # Fase 2: alle plannen op locatie
        plannen = await _zoek_plannen_full(client, rd_x, rd_y)
        if plannen and "_error" in plannen[0]:
            out["fout"] = f"plannen_zoek: {plannen[0]['_error']}"
            return out
        out["fase2_aantal_plannen"] = len(plannen)
        out["fase2_types"] = Counter(p.get("type") or "?" for p in plannen)

        if not plannen:
            out["fout_fase"] = "geen plannen op locatie"
            return out

        # Fase 3: huidige BP/beheersverordening filter
        bp_huidig = [p for p in plannen if p.get("type") in ("bestemmingsplan", "beheersverordening")]
        out["fase3_bp_huidig"] = len(bp_huidig)
        # Verbreed filter: alle plan-typen die maatvoering KUNNEN dragen
        bp_breder = [p for p in plannen if p.get("type") in (
            "bestemmingsplan", "beheersverordening",
            "wijzigingsplan", "uitwerkingsplan", "inpassingsplan",
            "bestemmingsplanBuitengebied", "rectificatie",
        )]
        out["fase3_bp_breder"] = len(bp_breder)

        if not bp_breder:
            out["fout_fase"] = "geen BP-achtige plannen"
            out["plan_voorbeelden"] = [(p.get("type"), p.get("naam") or "?")[:80]
                                       for p in plannen[:5]]
            return out

        # Fase 4: per BP maatvoeringen ophalen — beide endpoints
        plan_diag = []
        gevonden_hoogte = False
        gevonden_via = None
        eerste_hoogte = None
        # alle_naamvelden globaal verzamelen om patronen te zien
        naam_verzamelaar: Counter = Counter()

        for p in bp_breder[:5]:  # max 5 plannen, anders te traag
            pid = p.get("id")
            if not pid:
                continue
            mv_geo = await rp._fetch_maatvoeringen_geo(client, pid, rd_x, rd_y)
            mv_full = []
            if not mv_geo:
                mv_full = await rp._fetch_maatvoeringen(client, pid)
            mv = mv_geo or mv_full
            naamvelden = []
            for m in mv:
                for o in m.get("omvang") or []:
                    n = o.get("naam") or ""
                    if n:
                        naamvelden.append(n.lower())
                        naam_verzamelaar[n.lower()] += 1
            hoogtes = rp._extract_hoogtes(mv)
            heeft_hoogte = any(hoogtes[k] is not None for k in ("max_bouwhoogte_m", "max_goothoogte_m"))
            plan_diag.append({
                "pid": pid,
                "type": p.get("type"),
                "naam": (p.get("naam") or "")[:60],
                "n_mv_geo": len(mv_geo),
                "n_mv_full": len(mv_full),
                "n_mv_used": len(mv),
                "hoogtes": hoogtes,
                "naamvelden_unique": list(set(naamvelden))[:8],
            })
            if heeft_hoogte and not gevonden_hoogte:
                gevonden_hoogte = True
                gevonden_via = ("geo" if mv_geo else "full")
                eerste_hoogte = hoogtes
                eerste_hoogte["plan_id"] = pid
                eerste_hoogte["plan_naam"] = p.get("naam")

        out["fase4_plan_diag"] = plan_diag
        out["fase4_naam_top"] = naam_verzamelaar.most_common(15)
        out["resultaat_huidig"] = bool(gevonden_hoogte and any(
            d["type"] in ("bestemmingsplan", "beheersverordening") and (
                d["hoogtes"]["max_bouwhoogte_m"] or d["hoogtes"]["max_goothoogte_m"]
            ) for d in plan_diag
        ))
        out["resultaat_breder"] = bool(gevonden_hoogte)
        out["gevonden_via"] = gevonden_via
        out["hoogte"] = eerste_hoogte
    return out


async def main():
    if not os.getenv("RUIMTELIJKE_PLANNEN_API_KEY"):
        sys.exit("RUIMTELIJKE_PLANNEN_API_KEY niet gezet")
    print(f"RP v4 coverage-analyse — {len(ADRESSEN)} adressen\n")

    rapport = []
    for q in ADRESSEN:
        print(f"\n=== {q} ===")
        try:
            r = await _diagnose_adres(q)
            rapport.append(r)
        except Exception as e:
            print(f"  EXC: {e}")
            rapport.append({"query": q, "fout": repr(e)})
            continue
        if r.get("fout"):
            print(f"  FOUT: {r['fout']}")
            continue
        print(f"  RD: {r.get('rd')}  ({r.get('label','-')[:60]})")
        print(f"  Fase 2: {r.get('fase2_aantal_plannen')} plannen, types={dict(r.get('fase2_types', {}))}")
        print(f"  Fase 3: BP-huidig={r.get('fase3_bp_huidig')}, BP-breder={r.get('fase3_bp_breder')}")
        if r.get("fout_fase"):
            print(f"  STOP: {r['fout_fase']}")
            if r.get("plan_voorbeelden"):
                for tp, nm in r["plan_voorbeelden"]:
                    print(f"    - {tp}: {nm}")
            continue
        for d in r.get("fase4_plan_diag", []):
            ho = d["hoogtes"]
            print(f"  Plan [{d['type']}]: '{d['naam']}'")
            print(f"    n_mv: geo={d['n_mv_geo']} full={d['n_mv_full']} → "
                  f"bh={ho['max_bouwhoogte_m']} m, gh={ho['max_goothoogte_m']} m, "
                  f"lagen={ho['max_bouwlagen']}")
            if d["naamvelden_unique"]:
                print(f"    omvang-naamvelden: {d['naamvelden_unique']}")
        if r.get("resultaat_huidig"):
            print(f"  ✓ HUIDIG ADAPTER VINDT: bh={r['hoogte'].get('max_bouwhoogte_m')} m, "
                  f"gh={r['hoogte'].get('max_goothoogte_m')} m  (via {r.get('gevonden_via')})")
        elif r.get("resultaat_breder"):
            print(f"  ⚠ ALLEEN MET VERBREDE FILTER: bh={r['hoogte'].get('max_bouwhoogte_m')} m, "
                  f"gh={r['hoogte'].get('max_goothoogte_m')} m  (via {r.get('gevonden_via')})")
        else:
            print(f"  ✗ GEEN HOOGTE GEVONDEN")
            if r.get("fase4_naam_top"):
                print(f"    naamvelden in plan: {r['fase4_naam_top'][:8]}")

    # ============== EINDRAPPORT ==============
    print(f"\n\n{'='*78}\nEINDRAPPORT — Coverage-analyse RP v4\n{'='*78}")
    print(f"{'Adres':<48} {'huidig':>8} {'breder':>8}")
    print("-"*78)
    n_huidig = 0
    n_breder = 0
    n_total = 0
    for r in rapport:
        n_total += 1
        if r.get("fout") or r.get("fout_fase"):
            print(f"{r['query'][:46]:<48} {'-':>8} {'-':>8}  ({r.get('fout') or r.get('fout_fase')})")
            continue
        h = "✓" if r.get("resultaat_huidig") else "✗"
        b = "✓" if r.get("resultaat_breder") else "✗"
        if r.get("resultaat_huidig"): n_huidig += 1
        if r.get("resultaat_breder"): n_breder += 1
        bh = r.get("hoogte", {}).get("max_bouwhoogte_m") if r.get("hoogte") else None
        print(f"{r['query'][:46]:<48} {h:>8} {b:>8}  bh={bh}")

    print(f"\nCoverage HUIDIG (BP/beheersverordening only):  {n_huidig}/{n_total} ({100*n_huidig/n_total:.0f}%)")
    print(f"Coverage VERBREED (incl wijz/uit/inpassing):     {n_breder}/{n_total} ({100*n_breder/n_total:.0f}%)")
    print(f"Winst door verbreden:                            +{n_breder - n_huidig} adressen")

    # Mis-redenen analyse
    print(f"\n{'='*78}\nDIAGNOSE missers")
    print('='*78)
    missers = [r for r in rapport if not r.get("resultaat_breder") and not r.get("fout")]
    redenen: Counter = Counter()
    for r in missers:
        if r.get("fout_fase"):
            redenen[r["fout_fase"]] += 1
        elif not r.get("hoogte"):
            redenen["BP gevonden, maar geen bouwhoogte/goothoogte in maatvoeringen"] += 1
    for r, n in redenen.most_common():
        print(f"  {n}× {r}")

    # Naamvelden-aggregatie
    print(f"\n{'='*78}\nNAAMVELDEN — wat staat er in omvang.naam (top 20)")
    print('='*78)
    alle_namen: Counter = Counter()
    for r in rapport:
        for n, c in (r.get("fase4_naam_top") or []):
            alle_namen[n] += c
    for n, c in alle_namen.most_common(20):
        marker = "← bouwhoogte" if "bouwhoogte" in n else ("← goothoogte" if "goothoogte" in n else "")
        print(f"  {c:>4}× {n[:60]:<60} {marker}")


if __name__ == "__main__":
    asyncio.run(main())
