"""
RP v4 — archief / historisch / peildatum parameter-probe.

Doel: ontdekken of er een query-parameter bestaat die de oude (door
bruidsschat geretireerde) brood-en-boter bestemmings­plannen alsnog
terugbrengt. Onze huidige zoek krijgt alleen overlay-plannen terug omdat
de échte ruimtelijke BP's per 1-1-2024 in het Omgevingsplan zijn opgegaan.

We testen 8 query-varianten op 4 adressen (Amsterdam, Utrecht, Hillegom,
Eindhoven) — adressen waarop de huidige adapter NIETS oplevert. Als één
variant historische BP's met `maximum bouwhoogte`-maatvoering teruggeeft,
weten we de hefboom.
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

ADRESSEN = [
    {"slug": "amsterdam-paramaribo7", "rd": (118733, 485810)},
    {"slug": "utrecht-domplein", "rd": (136827, 455914)},
    {"slug": "hillegom-sixlaan", "rd": (99867, 479119)},
    {"slug": "eindhoven-stratumseind", "rd": (161495, 383063)},
]

# Varianten: lege baseline + 7 alternatieve strategieën
VARIANTEN = [
    ("baseline", {}, {}),
    # 1. archief expliciet
    ("archief=true", {"archief": "true"}, {}),
    ("archief=alle", {"archief": "alle"}, {}),
    ("archief=vervangen", {"archief": "vervangen"}, {}),
    # 2. peildatum vóór omgevingswet
    ("peildatum=2023-12-31", {"peildatum": "2023-12-31"}, {}),
    # 3. planstatus filter
    ("planstatus=vastgesteld", {"planstatus": "vastgesteld"}, {}),
    ("planstatus=onherroepelijk", {"planstatus": "onherroepelijk"}, {}),
    # 4. body-veld voor historisch
    ("body-historisch=true", {}, {"historisch": True}),
    # 5. body-veld archief
    ("body-archief=true", {}, {"archief": True}),
    # 6. expand-related parameters
    ("expand=alle", {"_expand": "alle"}, {}),
    # 7. v3 fallback (oude API)
    ("v3-baseline", {}, {}, "https://ruimte.omgevingswet.overheid.nl/ruimtelijke-plannen/api/opvragen/v3"),
]


def _hdrs():
    auth = rp._auth_headers() or {}
    return {**auth, "Content-Type": "application/json", "Content-Crs": "epsg:28992"}


async def _zoek(client, base_url: str, rd_x: float, rd_y: float,
                extra_params: dict, extra_body: dict) -> dict:
    """POST /plannen/_zoek met extra parameters/body."""
    body = {"_geo": {"intersects": {"type": "Point", "coordinates": [rd_x, rd_y]}}}
    body.update(extra_body)
    params = {"size": 50, **extra_params}
    try:
        r = await client.post(
            f"{base_url}/plannen/_zoek",
            params=params,
            headers=_hdrs(),
            json=body,
            timeout=15,
        )
    except Exception as e:
        return {"_status": "EXC", "_detail": repr(e)}
    out = {"_status": r.status_code, "_size": len(r.content)}
    if r.status_code == 200:
        try:
            data = r.json()
            plannen = (data.get("_embedded") or {}).get("plannen") or []
            out["plannen"] = plannen
            out["aantal"] = len(plannen)
        except Exception as e:
            out["parse_error"] = repr(e)
    else:
        try:
            out["error"] = r.json()
        except Exception:
            out["error_text"] = r.text[:200]
    return out


async def _maatv_test(client, base_url: str, plan_id: str) -> int:
    """Hoeveel maatvoeringen heeft een plan, en hoeveel met bouwhoogte?"""
    auth = rp._auth_headers() or {}
    try:
        r = await client.get(
            f"{base_url}/plannen/{plan_id}/maatvoeringen",
            params={"size": 200},
            headers=auth,
            timeout=15,
        )
        if r.status_code != 200:
            return -1, -1
        mv = (r.json().get("_embedded") or {}).get("maatvoeringen") or []
        n_total = len(mv)
        n_hoogte = 0
        for m in mv:
            for o in m.get("omvang") or []:
                if "bouwhoogte" in (o.get("naam") or "").lower():
                    n_hoogte += 1
                    break
        return n_total, n_hoogte
    except Exception:
        return -1, -1


async def main():
    if not os.getenv("RUIMTELIJKE_PLANNEN_API_KEY"):
        sys.exit("RUIMTELIJKE_PLANNEN_API_KEY niet gezet")

    print(f"RP v4 archief-probe — {len(VARIANTEN)} varianten × {len(ADRESSEN)} adressen\n")

    rapport: list[dict] = []
    async with httpx.AsyncClient() as client:
        for adres in ADRESSEN:
            print(f"\n=== {adres['slug']} (RD {adres['rd']}) ===")
            for var in VARIANTEN:
                naam, params, body = var[0], var[1], var[2]
                base = var[3] if len(var) > 3 else rp.RP_BASE
                res = await _zoek(client, base, adres["rd"][0], adres["rd"][1], params, body)

                if res.get("_status") != 200:
                    err = res.get("error") or res.get("error_text") or res.get("_detail")
                    err_str = str(err)[:120] if err else ""
                    print(f"  [{naam:<22}] {res.get('_status')}  {err_str}")
                    continue

                plannen = res.get("plannen") or []
                # Tel types + filter BP's
                types = Counter(p.get("type") or "?" for p in plannen)
                bp = [p for p in plannen if p.get("type") in ("bestemmingsplan", "beheersverordening")]
                # Filter overlay-namen weg om kandidaten te zien
                OVERLAY = ("paraplu", "facet", "herziening", "parkeer", "archeologie",
                           "datacenters", "darkstores", "flitsbezorging", "crisis-")
                echte_bp = [p for p in bp if not any(o in (p.get("naam") or "").lower() for o in OVERLAY)]

                samenvatting = f"plannen={len(plannen)} (BP={len(bp)}, niet-overlay={len(echte_bp)})"
                print(f"  [{naam:<22}] 200  {samenvatting}")

                if echte_bp:
                    # Test maatvoering op eerste echte BP
                    pid = echte_bp[0].get("id")
                    n_mv, n_h = await _maatv_test(client, base, pid)
                    print(f"      → echt BP: '{(echte_bp[0].get('naam') or '')[:60]}'")
                    print(f"        maatvoering: total={n_mv}, met-bouwhoogte={n_h}")
                    if n_h > 0:
                        print(f"        🎯 GEVONDEN — variant '{naam}' bracht een BP terug met bouwhoogte-data!")
                        rapport.append({
                            "adres": adres["slug"], "variant": naam,
                            "bp_naam": echte_bp[0].get("naam"),
                            "bp_id": pid, "n_mv": n_mv, "n_hoogte": n_h,
                        })

    print(f"\n\n{'='*72}\nEINDRAPPORT — Welke varianten leveren echte BP's met hoogte?")
    print('='*72)
    if not rapport:
        print("  GEEN ENKELE variant leverde een nieuw BP met bouwhoogte-maatvoering.")
        print("  Conclusie: de bruidsschat-overgang heeft de IMRO-BP's definitief gearchiveerd")
        print("  zonder dat ze via RP v4 nog opvraagbaar zijn op locatie.")
    else:
        for r in rapport:
            print(f"  {r['adres']:<28} via '{r['variant']}' → {r['bp_naam']!r}: {r['n_hoogte']} hoogte-mv")


if __name__ == "__main__":
    asyncio.run(main())
