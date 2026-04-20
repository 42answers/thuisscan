#!/usr/bin/env python3
"""
Eenmalige sync van Kiesraad-CSV naar compacte per-gemeente-JSON.

Draaien na elke nieuwe Tweede Kamer-verkiezing:
    python scripts/sync_verkiezingen.py [ZIP-URL]

Default: TK2025 CSV op data.overheid.nl.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import httpx

# data.overheid.nl — Kiesraad TK2025 definitieve uitslag in CSV
DEFAULT_ZIP_URL = (
    "https://data.overheid.nl/sites/default/files/dataset/"
    "a16f3352-c9ce-4831-a314-f989d442a258/resources/"
    "Verkiezingsuitslag%20Tweede%20Kamer%202025%20%28CSV%20Formaat%29.zip"
)

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "tk2025.json"
CACHE_ZIP = Path(__file__).resolve().parent.parent / "cache" / "tk2025.zip"

# Partijnaam -> korte UI-variant (long names eten anders ruimte)
SHORT = {
    "PVV (Partij voor de Vrijheid)": "PVV",
    "GROENLINKS / Partij van de Arbeid (PvdA)": "GL-PvdA",
    "GROENLINKS-PvdA": "GL-PvdA",
    "Partij van de Arbeid (P.v.d.A.)": "PvdA",
    "Partij voor de Dieren": "PvdD",
    "Forum voor Democratie": "FVD",
    "Volt Nederland": "Volt",
    "Staatkundig Gereformeerde Partij (SGP)": "SGP",
    "ChristenUnie": "CU",
    "SP (Socialistische Partij)": "SP",
    "BoerBurgerBeweging": "BBB",
    "Nieuw Sociaal Contract": "NSC",
    "Piratenpartij - De Groenen": "Piraten",
}


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ZIP_URL

    # Gebruik lokale cache als die er is (snellere development)
    if CACHE_ZIP.exists():
        print(f"Lokale cache gebruikt: {CACHE_ZIP}")
        zip_bytes = CACHE_ZIP.read_bytes()
    else:
        print(f"Downloaden {url}")
        CACHE_ZIP.parent.mkdir(parents=True, exist_ok=True)
        resp = httpx.get(url, timeout=120.0, follow_redirects=True)
        resp.raise_for_status()
        zip_bytes = resp.content
        CACHE_ZIP.write_bytes(zip_bytes)
        print(f"  {len(zip_bytes):,} bytes opgeslagen in {CACHE_ZIP}")

    # Extract CSV
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        csv_name = next(
            n for n in z.namelist()
            if n.lower().endswith(".csv") and "uitslag" in n.lower()
        )
        print(f"Parsen: {csv_name}")
        with z.open(csv_name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter=";")
            rows = list(reader)

    # Aggregeer stemmen per (RegioCode, LijstNaam)
    by_regio: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        if r.get("VeldType") != "LijstAantalStemmen":
            continue
        code = r.get("RegioCode", "")
        partij = (r.get("LijstNaam") or "").strip()
        if not partij:
            continue
        try:
            stemmen = int(r.get("Waarde") or 0)
        except ValueError:
            stemmen = 0
        if stemmen > 0:
            by_regio[code][partij] = stemmen

    # Landelijk totaal (L528 = Nederland)
    nl_stemmen = by_regio.get("L528", {})
    nl_total = sum(nl_stemmen.values())
    if nl_total == 0:
        print("FOUT: geen landelijk totaal gevonden")
        return 1
    nl_pct = {_short(p): round(100 * s / nl_total, 1) for p, s in nl_stemmen.items()}

    # Per gemeente top 3 met afgeronde percentages
    per_gemeente: dict[str, list[dict]] = {}
    for code, stemmen in by_regio.items():
        if not code.startswith("G"):
            continue
        total = sum(stemmen.values())
        if total == 0:
            continue
        top3 = sorted(stemmen.items(), key=lambda x: -x[1])[:3]
        per_gemeente[code] = [
            {"partij": _short(p), "pct": round(100 * s / total, 1)}
            for p, s in top3
        ]

    out = {
        "election": "TK2025",
        "date": "2025-10-29",
        "nl_pct": nl_pct,
        "per_gemeente": per_gemeente,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"Klaar: {len(per_gemeente)} gemeenten -> {OUT_PATH}")
    print(f"  Landelijk top 3: {sorted(nl_pct.items(), key=lambda x: -x[1])[:3]}")
    return 0


def _short(partij: str) -> str:
    return SHORT.get(partij, partij)


if __name__ == "__main__":
    raise SystemExit(main())
