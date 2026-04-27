#!/usr/bin/env python3
"""
Build statische GeoJSON-bestanden voor de leefbaarheidskaart van Nederland.

Output (in apps/web/data/lbm/):
  - gemeenten.geojson     342 gemeenten met klasse + afw + sub-scores + top_pct_nl
  - provincies.geojson    12 provincies met aggregaten (rekenkundig gem. + percentile)

Bronnen:
  - BZK WFS lbm3:gemeentescore24  → gemeente klasse/afw + 5 sub-dimensies + geometry
  - PDOK Bestuurlijke Gebieden    → gemeente → provincie mapping + provincie geometries

Aggregatie naar provincie:
  - MVP: rekenkundig gemiddelde van gemeente-afw binnen elke provincie
  - Fase 2 verbetering: populatie-gewogen (vereist CBS Statline call)

Gebruik:
  python3 scripts/build_leefbaarheidskaart.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from shapely.geometry import shape, mapping

# Voeg api/adapters toe aan path zodat we de bestaande ECDF kunnen hergebruiken
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))
from adapters.leefbaarometer_distribution import (
    percentile_from_afw, top_percent_from_afw,
)

BZK_WFS = "https://geo.leefbaarometer.nl/wfs"
PDOK_WFS = "https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0"
OUT_DIR = ROOT / "apps" / "web" / "data" / "lbm"
TIMEOUT_S = 180.0   # provincie-geometries zijn groot, ~30-60s download


def fetch_bzk_gemeenten() -> list[dict]:
    """Haal alle 342 gemeenten met leefbaarheidsscores op uit BZK WFS."""
    print("→ BZK gemeentescore24 ophalen…", flush=True)
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "lbm3:gemeentescore24",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }
    r = httpx.get(BZK_WFS, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    data = r.json()
    features = data.get("features", [])
    print(f"   ✓ {len(features)} gemeenten", flush=True)
    return features


def fetch_pdok_gemeenten() -> dict[str, dict]:
    """Haal PDOK gemeente-records (voor provincie-mapping) — index op CBS-code.

    PDOK gemeente-id is 'GM0014' formaat — match met BZK 'id' veld direct.
    """
    print("→ PDOK gemeente-mapping ophalen…", flush=True)
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "bestuurlijkegebieden:Gemeentegebied",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }
    r = httpx.get(PDOK_WFS, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    data = r.json()
    by_code: dict[str, dict] = {}
    for f in data.get("features", []):
        p = f.get("properties", {})
        cbs_code = p.get("identificatie")
        if cbs_code:
            by_code[cbs_code] = {
                "naam": p.get("naam"),
                "provincie_code": p.get("ligtInProvincieCode"),
                "provincie_naam": p.get("ligtInProvincieNaam"),
            }
    print(f"   ✓ {len(by_code)} gemeente-mappings", flush=True)
    return by_code


def fetch_pdok_provincies() -> list[dict]:
    """Haal PDOK provincie-features met geometry."""
    print("→ PDOK provincie-geometries ophalen…", flush=True)
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "bestuurlijkegebieden:Provinciegebied",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }
    r = httpx.get(PDOK_WFS, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    features = r.json().get("features", [])
    print(f"   ✓ {len(features)} provincies", flush=True)
    return features


def simplify_geometry(geom: dict, tolerance: float) -> dict:
    """Vereenvoudig een GeoJSON-geometry met Douglas-Peucker.

    tolerance is in graden (lon/lat). 0.001 ≈ 100m, 0.0005 ≈ 50m.
    Behoudt topologie zodat polygons niet zelf-intersecteren.
    """
    try:
        s = shape(geom).simplify(tolerance, preserve_topology=True)
        return mapping(s)
    except Exception:
        return geom   # bij failure: geef origineel terug


def build_gemeenten_geojson(bzk_features: list[dict],
                            pdok_map: dict[str, dict]) -> dict:
    """Combineer BZK-scores + PDOK-mapping → één GeoJSON FeatureCollection."""
    out_features = []
    missing_pdok = 0
    for f in bzk_features:
        p = f.get("properties", {})
        cbs_code = p.get("id")    # bv. 'GM0014'
        afw = p.get("afw")
        kscore = p.get("kscore")
        if cbs_code is None or afw is None:
            continue
        pdok_info = pdok_map.get(cbs_code)
        if not pdok_info:
            missing_pdok += 1
        out_features.append({
            "type": "Feature",
            # Aggressive simplify — gemeente-zoom toont contour, geen detail nodig.
            # 0.0005° ≈ 50m — onzichtbaar op zoom 8-10.
            "geometry": simplify_geometry(f.get("geometry"), 0.0005),
            "properties": {
                "code": cbs_code,
                "naam": p.get("gemeente") or p.get("name"),
                "klasse": kscore,
                "afw": round(float(afw), 4),
                "top_pct_nl": top_percent_from_afw(afw),
                "pct_below_nl": percentile_from_afw(afw),
                # Sub-dimensies (klasse 1-9)
                "sub": {
                    "won": p.get("kwon"),
                    "fys": p.get("kfys"),
                    "vrz": p.get("kvrz"),
                    "soc": p.get("ksoc"),
                    "onv": p.get("konv"),
                },
                # Provincie-koppeling voor frontend-filter
                "provincie": (pdok_info or {}).get("provincie_naam"),
                "provincie_code": (pdok_info or {}).get("provincie_code"),
            },
        })
    if missing_pdok:
        print(f"   ⚠ {missing_pdok} gemeenten zonder PDOK-mapping (mogelijk recent samengevoegd)",
              flush=True)
    return {
        "type": "FeatureCollection",
        "features": out_features,
    }


def build_provincies_geojson(pdok_provs: list[dict],
                              gemeenten_fc: dict,
                              pdok_map: dict[str, dict]) -> dict:
    """Provincie-GeoJSON met aggregaten op basis van gemeente-afw.

    MVP: rekenkundig gemiddelde over alle gemeenten in de provincie.
    NIET populatie-gewogen — een grote stad telt evenveel als een dorpje.
    Nuance: BZK zelf publiceert geen provincie-niveau; dit is onze
    afgeleide weergave.
    """
    # Map provincie_code → list[afw] uit gemeenten
    by_prov: dict[str, list[float]] = {}
    by_prov_naam: dict[str, str] = {}
    for f in gemeenten_fc["features"]:
        p = f["properties"]
        pcode = p.get("provincie_code")
        if not pcode:
            continue
        by_prov.setdefault(pcode, []).append(p["afw"])
        if p.get("provincie"):
            by_prov_naam[pcode] = p["provincie"]

    # Bouw features uit PDOK-provincies + onze aggregaten
    out_features = []
    for f in pdok_provs:
        pp = f.get("properties", {})
        pcode = pp.get("code") or pp.get("identificatie")
        afw_list = by_prov.get(pcode, [])
        if not afw_list:
            print(f"   ⚠ provincie {pcode} ({pp.get('naam')}): geen gemeente-data",
                  flush=True)
            continue
        avg_afw = sum(afw_list) / len(afw_list)
        out_features.append({
            "type": "Feature",
            # Provincie-zoom (5-7) toont alleen ruwe contour — sterker simplify.
            # 0.002° ≈ 200m, voldoende voor heel-NL view.
            "geometry": simplify_geometry(f.get("geometry"), 0.002),
            "properties": {
                "code": pcode,
                "naam": pp.get("naam"),
                "afw": round(avg_afw, 4),
                "top_pct_nl": top_percent_from_afw(avg_afw),
                "pct_below_nl": percentile_from_afw(avg_afw),
                "n_gemeenten": len(afw_list),
                "min_gem_afw": round(min(afw_list), 4),
                "max_gem_afw": round(max(afw_list), 4),
            },
        })
    return {
        "type": "FeatureCollection",
        "features": out_features,
    }


def write_geojson(path: Path, fc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = path.stat().st_size / 1024
    print(f"   ✓ {path.relative_to(ROOT)}  ({size_kb:.0f} KB · {len(fc['features'])} features)",
          flush=True)


def main():
    print("=" * 60)
    print("Leefbaarheidskaart — build (Fase 1: provincie + gemeente)")
    print("=" * 60)

    bzk_gem = fetch_bzk_gemeenten()
    pdok_map = fetch_pdok_gemeenten()
    pdok_provs = fetch_pdok_provincies()

    print("\n→ Build gemeenten.geojson…", flush=True)
    gem_fc = build_gemeenten_geojson(bzk_gem, pdok_map)
    write_geojson(OUT_DIR / "gemeenten.geojson", gem_fc)

    print("\n→ Build provincies.geojson…", flush=True)
    prov_fc = build_provincies_geojson(pdok_provs, gem_fc, pdok_map)
    write_geojson(OUT_DIR / "provincies.geojson", prov_fc)

    print("\n=== Klaar ===")
    # Quick sanity stats
    afws = sorted(f["properties"]["afw"] for f in gem_fc["features"])
    print(f"\nGemeente-afw verdeling:")
    print(f"  min  = {afws[0]:+.3f}")
    print(f"  p25  = {afws[len(afws)//4]:+.3f}")
    print(f"  p50  = {afws[len(afws)//2]:+.3f}")
    print(f"  p75  = {afws[3*len(afws)//4]:+.3f}")
    print(f"  max  = {afws[-1]:+.3f}")
    print(f"\nProvincie-aggregaten (gesorteerd, hoog → laag):")
    sorted_provs = sorted(prov_fc["features"],
                          key=lambda f: -f["properties"]["afw"])
    for f in sorted_provs:
        p = f["properties"]
        print(f"  {p['naam']:<20} afw={p['afw']:+.3f}  "
              f"top {p['top_pct_nl']:>5.1f}%  ({p['n_gemeenten']} gem.)")


if __name__ == "__main__":
    main()
