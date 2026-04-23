"""
DSO Presenteren — dedicated /omgevingsnormen endpoint-probe.

User-suggestie: er zou een eigen endpoint /omgevingsnormen bestaan met een
normType-filter. Eerder zagen we dat het omgevingsnormen[]-array in de
regeltekstannotaties-response altijd LEEG is voor bruidsschat-plannen — maar
een dedicated endpoint zou kunnen putten uit een andere index.

We testen op 3 adressen × ~10 endpoint-varianten (v8 én v1, GET én POST,
verschillende paden).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httpx

DUMP_DIR = "/tmp/dso_omgnormen_dumps"
os.makedirs(DUMP_DIR, exist_ok=True)

PRES_V8 = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8"
PRES_V1 = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v1"
ONT_V2 = "https://service.omgevingswet.overheid.nl/publiek/omgevingsinformatie/api/ontsluiten/v2"

ADRESSEN = [
    {"slug": "amsterdam-paramaribo72-1", "rd": (118845, 485495), "label": "Amsterdam Paramaribostraat 72-1"},
    {"slug": "utrecht-leidsche-rijn", "rd": (130850, 456400), "label": "Utrecht Leidsche Rijn"},
    {"slug": "deventer-binnenstad", "rd": (208300, 472900), "label": "Deventer binnenstad"},
]


def _h() -> dict:
    key = os.getenv("DSO_API_KEY")
    if not key:
        raise SystemExit("DSO_API_KEY niet gezet")
    return {
        "x-api-key": key,
        "Accept": "application/hal+json",
        "Content-Type": "application/json",
        "Content-Crs": "http://www.opengis.net/def/crs/EPSG/0/28992",
        "User-Agent": "buurtscan/1.0 (probe-omgnormen)",
    }


# Een rijtje varianten — bestaat het endpoint überhaupt? In welke versie?
def varianten(x: float, y: float):
    body_geo = {"geometrie": {"type": "Point", "coordinates": [x, y]}}
    return [
        # v8 — POST _zoek (DSO conventie voor andere endpoints)
        ("v8-omgnorm-zoek-POST", "POST", f"{PRES_V8}/omgevingsnormen/_zoek", body_geo, None),
        # v8 — GET met locatie query
        ("v8-omgnorm-GET-loc", "GET", f"{PRES_V8}/omgevingsnormen", None, {"locatie": f"{x},{y}"}),
        # v8 — GET met point WKT-style
        ("v8-omgnorm-GET-point", "GET", f"{PRES_V8}/omgevingsnormen", None, {"point": f"{x},{y}"}),
        # v8 — GET met geometrie
        ("v8-omgnorm-GET-geom", "GET", f"{PRES_V8}/omgevingsnormen", None, {"geometrie": f"POINT({x} {y})"}),
        # v8 — GET met normType filter (user-suggestie)
        ("v8-omgnorm-GET-typ", "GET", f"{PRES_V8}/omgevingsnormen", None,
         {"locatie": f"{x},{y}", "normType": "maximale-bouwhoogte"}),
        # v8 — bare endpoint check
        ("v8-omgnorm-bare", "GET", f"{PRES_V8}/omgevingsnormen", None, None),
        # v1 — bestaat user-suggested versie?
        ("v1-omgnorm-zoek-POST", "POST", f"{PRES_V1}/omgevingsnormen/_zoek", body_geo, None),
        ("v1-omgnorm-GET-loc", "GET", f"{PRES_V1}/omgevingsnormen", None, {"locatie": f"{x},{y}"}),
        ("v1-bare", "GET", f"{PRES_V1}/", None, None),
        # ontsluiten /omgevingsnormen
        ("ont-omgnorm-zoek-POST", "POST", f"{ONT_V2}/omgevingsnormen/_zoek", body_geo, None),
    ]


HOOGTE_T = ("hoogte", "bouwhoogte", "goothoogte", "maatvoering", "norm", "waarde", "kwantitatieve")


def _walk(o, p=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{p}.{k}" if p else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, f"{p}[{i}]")
    else:
        yield p, o


def _hoogte_in(blob) -> list:
    if not isinstance(blob, (dict, list)):
        return []
    out = []
    for p, v in _walk(blob):
        if not any(t in p.lower() for t in HOOGTE_T):
            continue
        if isinstance(v, (int, float)):
            out.append((p, v))
        elif isinstance(v, str) and v and any(c.isdigit() for c in v):
            out.append((p, v[:60]))
    return out


async def _probe(client, naam, method, url, body, params):
    try:
        if method == "POST":
            r = await client.post(url, json=body, params=params, timeout=15)
        else:
            r = await client.get(url, params=params, timeout=15)
    except Exception as e:
        return naam, "EXC", repr(e)[:120], 0, []
    sz = len(r.content)
    if r.status_code in (200, 201):
        try:
            blob = r.json()
        except Exception:
            return naam, r.status_code, f"non-json: {r.text[:80]}", sz, []
        treffers = _hoogte_in(blob)
        return naam, r.status_code, blob, sz, treffers
    err = ""
    try:
        ej = r.json()
        err = ej.get("detail") or json.dumps(ej)[:200]
    except Exception:
        err = r.text[:200]
    return naam, r.status_code, err, sz, []


async def main():
    headers = _h()
    print(f"DSO /omgevingsnormen probe — {len(ADRESSEN)} adressen × 10 varianten\n")
    treffer_naar_dump = []
    async with httpx.AsyncClient(headers=headers) as client:
        for ad in ADRESSEN:
            print(f"\n=== {ad['label']} ===")
            for naam, method, url, body, params in varianten(*ad["rd"]):
                n, st, blob_or_err, sz, treffers = await _probe(client, naam, method, url, body, params)
                if st in (200, 201):
                    info = ""
                    if isinstance(blob_or_err, dict):
                        keys = list(blob_or_err.keys())[:5]
                        info = f"keys={keys}"
                    elif isinstance(blob_or_err, list):
                        info = f"list[{len(blob_or_err)}]"
                    print(f"  [{n:<28}] {method} → {st}  size={sz}  treffers={len(treffers)}  {info}")
                    if treffers:
                        path = os.path.join(DUMP_DIR, f"{ad['slug']}_{n}.json")
                        with open(path, "w") as f:
                            json.dump(blob_or_err, f, indent=2, ensure_ascii=False)
                        treffer_naar_dump.append(path)
                        for p, v in treffers[:8]:
                            print(f"      • {p} = {v!r}")
                else:
                    print(f"  [{n:<28}] {method} → {st}  {str(blob_or_err)[:120]}")

    print(f"\n=== Samenvatting ===")
    if treffer_naar_dump:
        print(f"Dumps met hoogte-treffers ({len(treffer_naar_dump)}):")
        for d in treffer_naar_dump:
            print(f"  - {d}")
    else:
        print("GEEN endpoint-variant leverde structured hoogte-data op.")


if __name__ == "__main__":
    asyncio.run(main())
