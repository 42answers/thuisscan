"""
Probe-script voor DSO API — verifieer dat onze key werkt en vind de exacte
endpoint-structuur voordat we productie-integratie doen.

Run:
    cd apps/api
    DSO_API_KEY=<key> python3 tests/dso_probe.py

Test per endpoint: 200 = werkt, 401/403 = key-issue, 404 = fout endpoint,
5xx = serverfout. De output toont response-keys zodat we het JSON-schema
kunnen matchen in adapters/dso.py en adapters/vergunningcheck.py.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

KEY = os.getenv("DSO_API_KEY")
if not KEY:
    print("FOUT: zet DSO_API_KEY in je shell")
    sys.exit(1)

# Damrak 1 als testlocatie (RD)
RD_X = 121691
RD_Y = 487810

HEADERS = {
    "x-api-key": KEY,
    "Accept": "application/hal+json",
    "User-Agent": "buurtscan-probe/0.1",
}


def try_endpoint(name: str, method: str, url: str, **kwargs):
    print(f"\n=== {name} ===")
    print(f"{method} {url}")
    try:
        with httpx.Client(timeout=20.0) as c:
            resp = c.request(method, url, headers=HEADERS, **kwargs)
        print(f"  status: {resp.status_code}")
        try:
            data = resp.json()
            if isinstance(data, dict):
                print(f"  keys: {list(data.keys())[:15]}")
                # Embedded content
                emb = data.get("_embedded") or {}
                if emb:
                    for k, v in emb.items():
                        print(f"    _embedded.{k}: {len(v) if isinstance(v, list) else 'obj'}")
                # Show first feature/item preview
                for key in ("documenten", "regeltekstannotaties", "divisieannotaties", "activiteiten"):
                    items = emb.get(key) or []
                    if items:
                        print(f"    FIRST {key}: {json.dumps(items[0], ensure_ascii=False)[:300]}")
                        break
            elif isinstance(data, list):
                print(f"  list length: {len(data)}")
                if data:
                    print(f"    first: {json.dumps(data[0], ensure_ascii=False)[:300]}")
        except Exception:
            print(f"  body[:300]: {resp.text[:300]}")
    except Exception as e:
        print(f"  EXCEPTION: {e}")


# 1. Omgevingsdocumenten Presenteren v8 — regelingen zoeken op locatie
PRES = "https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8"

# Geen directe locatie-endpoint; we proberen een paar realistische paden
try_endpoint(
    "Presenteren v8 — documenten list (basis)",
    "GET",
    f"{PRES}/documenten?size=1",
)
try_endpoint(
    "Presenteren v8 — documenten _zoek (geo)",
    "POST",
    f"{PRES}/documenten/_zoek",
    json={"zoekParameters": [
        {"parameter": "locatie.punt", "waarden": [f"POINT({RD_X} {RD_Y})"]},
    ], "page": 0, "size": 3},
    headers={**HEADERS, "Content-Type": "application/json", "Content-Crs": "epsg:28992"},
)

# 2. Omgevingsinformatie Ontsluiten v2 — mogelijk betere locatie-entry
ONTSL = "https://service.omgevingswet.overheid.nl/publiek/omgevingsinformatie/api/ontsluiten/v2"
try_endpoint(
    "Ontsluiten v2 — root",
    "GET",
    f"{ONTSL}",
)
try_endpoint(
    "Ontsluiten v2 — documenten _zoek",
    "POST",
    f"{ONTSL}/documenten/_zoek",
    json={"zoekParameters": [
        {"parameter": "locatie.punt", "waarden": [f"POINT({RD_X} {RD_Y})"]},
    ]},
    headers={**HEADERS, "Content-Type": "application/json", "Content-Crs": "epsg:28992"},
)

# 3. Toepasbare Regels — zoekinterface (voor activiteiten)
TR_ZOEK = "https://service.omgevingswet.overheid.nl/publiek/toepasbare-regels/api/zoekinterface/v2"
try_endpoint(
    "Toepasbare regels zoek v2 — activiteiten",
    "GET",
    f"{TR_ZOEK}/activiteiten?size=3",
)

# 4. Vergunningcheck (Uitvoeren services v3)
VC = "https://service.omgevingswet.overheid.nl/publiek/toepasbare-regels/api/toepasbareregelsuitvoerenservices/v3"
try_endpoint(
    "Vergunningcheck v3 — _uitvoeren minimaal",
    "POST",
    f"{VC}/_uitvoeren",
    json={
        "functioneleStructuurRefs": ["/join/id/stop/pv28/nl.imow.1-pv-aanbouw"],
        "locatie": {
            "geometrie": {"type": "Point", "coordinates": [RD_X, RD_Y]},
            "crs": "epsg:28992",
        },
        "antwoorden": [],
    },
    headers={**HEADERS, "Content-Type": "application/json"},
)

print("\n" + "=" * 60)
print("Klaar. Gebruik output om adapter-schemas te corrigeren.")
