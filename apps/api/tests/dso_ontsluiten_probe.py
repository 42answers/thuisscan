"""
DSO Omgevingsinformatie Ontsluiten v2 — /bevragen probe.

Hypothese (van user-suggestie): er bestaat een endpoint
  POST .../ontsluiten/v2/bevragen
met body
  {"locatie": {"point": {"coordinates": [x,y], "crs": "EPSG:28992"}},
   "vastgesteld": true}
dat structured maatvoeringsvlak-data teruggeeft (max bouwhoogte, max
goothoogte) — wat Presenteren v8 niet bleek te hebben.

We proberen dit endpoint + een aantal varianten op 3 adressen, dumpen elk
response en scannen op hoogte-attributen.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httpx

DUMP_DIR = "/tmp/dso_ontsluiten_dumps"
os.makedirs(DUMP_DIR, exist_ok=True)

ONT_BASE = "https://service.omgevingswet.overheid.nl/publiek/omgevingsinformatie/api/ontsluiten/v2"

ADRESSEN = [
    {
        "slug": "amsterdam-paramaribo72-1",
        "label": "Amsterdam — Paramaribostraat 72-1",
        "rd": (118845, 485495),
    },
    {
        "slug": "amsterdam-paramaribo7",
        "label": "Amsterdam — Paramaribostraat 7",
        "rd": (118918, 487447),
    },
    {
        "slug": "utrecht-leidsche-rijn",
        "label": "Utrecht — Leidsche Rijn",
        "rd": (130850, 456400),
    },
]

# Endpoint+body-varianten om te testen — we beginnen met user-suggestie en
# proberen alternatieven als die 404/400 geeft.
VARIANTEN = [
    # Variant 1: user-suggestie — POST /bevragen met locatie.point + vastgesteld
    {
        "naam": "v1-bevragen-locatie-point",
        "method": "POST",
        "path": "/bevragen",
        "body": lambda x, y: {
            "locatie": {"point": {"coordinates": [x, y], "crs": "EPSG:28992"}},
            "vastgesteld": True,
        },
    },
    # Variant 2: documenten-zoek met geometrie (DSO-conventie)
    {
        "naam": "v2-documenten-zoek-geometrie",
        "method": "POST",
        "path": "/documenten/_zoek",
        "body": lambda x, y: {
            "geometrie": {"type": "Point", "coordinates": [x, y]},
        },
    },
    # Variant 3: locaties-zoek (omgekeerde route)
    {
        "naam": "v3-locaties-zoek-geometrie",
        "method": "POST",
        "path": "/locaties/_zoek",
        "body": lambda x, y: {
            "geometrie": {"type": "Point", "coordinates": [x, y]},
        },
    },
    # Variant 4: bevragen met geometrie i.p.v. locatie (DSO-stijl)
    {
        "naam": "v4-bevragen-geometrie",
        "method": "POST",
        "path": "/bevragen",
        "body": lambda x, y: {
            "geometrie": {"type": "Point", "coordinates": [x, y]},
        },
    },
    # Variant 5: maatvoeringen direct (als die endpoint bestaat)
    {
        "naam": "v5-maatvoeringen-zoek",
        "method": "POST",
        "path": "/maatvoeringen/_zoek",
        "body": lambda x, y: {
            "geometrie": {"type": "Point", "coordinates": [x, y]},
        },
    },
]


def _auth_headers() -> dict:
    key = os.getenv("DSO_API_KEY")
    if not key:
        raise SystemExit("DSO_API_KEY niet gezet")
    return {
        "x-api-key": key,
        "Accept": "application/hal+json",
        "Content-Type": "application/json",
        "Content-Crs": "http://www.opengis.net/def/crs/EPSG/0/28992",
        "User-Agent": "buurtscan/1.0 (probe)",
    }


HOOGTE_TERMS = (
    "hoogte", "bouwhoogte", "goothoogte", "nokhoogte",
    "maatvoering", "maatvoeringsvlak", "kwantitatieveWaarde",
    "norm", "omgevingsnorm", "waarde", "eenheid",
)


def _walk(obj, pad=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{pad}.{k}" if pad else k
            if isinstance(v, (dict, list)):
                yield from _walk(v, sub)
            else:
                yield sub, v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            sub = f"{pad}[{i}]"
            if isinstance(v, (dict, list)):
                yield from _walk(v, sub)
            else:
                yield sub, v


def _vind_hoogte(blob: dict) -> list[tuple[str, object]]:
    treffers = []
    for pad, val in _walk(blob):
        if not any(t.lower() in pad.lower() for t in HOOGTE_TERMS):
            continue
        if isinstance(val, (int, float)):
            treffers.append((pad, val))
        elif isinstance(val, str) and val and any(c.isdigit() for c in val):
            treffers.append((pad, val[:80]))
    return treffers


async def _probe(client: httpx.AsyncClient, adres: dict, variant: dict):
    x, y = adres["rd"]
    url = f"{ONT_BASE}{variant['path']}"
    body = variant["body"](x, y)
    try:
        if variant["method"] == "POST":
            r = await client.post(url, json=body, timeout=15)
        else:
            r = await client.get(url, params=body, timeout=15)
    except Exception as e:
        return {"status": "EXC", "detail": repr(e)[:120]}
    out = {"status": r.status_code, "size": len(r.content)}
    if r.status_code in (200, 201):
        try:
            blob = r.json()
        except Exception:
            out["body_text"] = r.text[:300]
            return out
        # Dump
        path = os.path.join(DUMP_DIR, f"{adres['slug']}_{variant['naam']}.json")
        with open(path, "w") as f:
            json.dump(blob, f, indent=2, ensure_ascii=False)
        out["dump"] = path
        out["top_keys"] = list(blob.keys()) if isinstance(blob, dict) else f"list[{len(blob)}]"
        treffers = _vind_hoogte(blob) if isinstance(blob, dict) else []
        out["treffers"] = len(treffers)
        out["treffer_voorbeeld"] = treffers[:5]
    elif r.status_code in (400, 404, 422):
        # Belangrijk: foutbody bevat soms hint over juiste endpoint/parameter
        try:
            err = r.json()
            out["error"] = err
        except Exception:
            out["error_text"] = r.text[:400]
    return out


async def main():
    headers = _auth_headers()
    print(f"DSO Ontsluiten v2 probe — {len(VARIANTEN)} varianten × {len(ADRESSEN)} adressen")
    print(f"Dumps -> {DUMP_DIR}/\n")

    async with httpx.AsyncClient(headers=headers) as client:
        for adres in ADRESSEN:
            print(f"\n=== {adres['label']} (RD {adres['rd']}) ===")
            for v in VARIANTEN:
                res = await _probe(client, adres, v)
                status = res.get("status")
                line = f"  [{v['naam']:<35}] {v['method']} {v['path']:<25} → {status}"
                if status in (200, 201):
                    line += f"  size={res.get('size')} treffers={res.get('treffers')}"
                    print(line)
                    if res.get("treffers", 0) > 0:
                        print(f"      KEYS: {res.get('top_keys')}")
                        for pad, val in res.get("treffer_voorbeeld", []):
                            print(f"      • {pad} = {val!r}")
                else:
                    print(line)
                    err = res.get("error") or res.get("error_text") or res.get("detail")
                    if err:
                        err_str = json.dumps(err) if not isinstance(err, str) else err
                        print(f"      → {err_str[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
