#!/usr/bin/env python3
"""
Build statische GeoJSON-bestanden voor de leefbaarheidskaart van Nederland.

Output (in apps/web/data/lbm/):
  - provincies.geojson   12 provincies (afgeleid uit gemeente-aggregatie)
  - gemeenten.geojson    342 gemeenten (BZK lbm3:gemeentescore24)
  - wijken.geojson       3245 wijken (BZK lbm3:wijkscore24)
  - buurten.geojson      12147 buurten (BZK lbm3:buurtscore24)

Elke feature bevat:
  - top-level afgeleide velden (naam, code, klasse, top_pct_nl, ...)
  - 'raw' object met ALLE BZK-velden (kscore, kafw, kwon..konv klasse,
     won..onv continu, year). Behouden zodat de frontend later
     custom-gewichten kan toepassen (bv. 'voorzieningen 50%, overlast 30%').

Bronnen:
  - BZK WFS lbm3:gemeentescore24 / wijkscore24 / buurtscore24
  - PDOK Bestuurlijke Gebieden     → provincie geometrie + mapping

Aggregatie naar provincie:
  - MVP: rekenkundig gemiddelde over alle gemeenten in de provincie
  - Aggregaten op TOTAAL afw én per sub-dimensie (won/fys/vrz/soc/onv)

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
TIMEOUT_S = 240.0   # buurten = 12k features = grote response (~80 MB raw)

# Sub-dimensie keys (continu + klasse) — gebruikt overal
SUB_KEYS = ("won", "fys", "vrz", "soc", "onv")


# =============================================================================
# WFS-fetches
# =============================================================================
def fetch_bzk(layer: str) -> list[dict]:
    """Generic — haal alle features van een BZK WFS layer in één call."""
    print(f"→ BZK {layer} ophalen…", flush=True)
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": layer,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }
    r = httpx.get(BZK_WFS, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    feats = r.json().get("features", [])
    print(f"   ✓ {len(feats)} features", flush=True)
    return feats


def fetch_pdok_gemeenten() -> dict[str, dict]:
    """PDOK gemeente-mapping → CBS-code → {provincie_code, provincie_naam}."""
    print("→ PDOK gemeente-mapping ophalen…", flush=True)
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "bestuurlijkegebieden:Gemeentegebied",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }
    r = httpx.get(PDOK_WFS, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    by_code: dict[str, dict] = {}
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        cbs_code = p.get("identificatie")
        if cbs_code:
            by_code[cbs_code] = {
                "provincie_code": p.get("ligtInProvincieCode"),
                "provincie_naam": p.get("ligtInProvincieNaam"),
            }
    print(f"   ✓ {len(by_code)} mappings", flush=True)
    return by_code


def fetch_pdok_provincies() -> list[dict]:
    """PDOK provincie geometries (12 features met code + naam)."""
    print("→ PDOK provincie-geometries ophalen…", flush=True)
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "bestuurlijkegebieden:Provinciegebied",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }
    r = httpx.get(PDOK_WFS, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    feats = r.json().get("features", [])
    print(f"   ✓ {len(feats)} provincies", flush=True)
    return feats


# =============================================================================
# Geometry helpers
# =============================================================================
def simplify_geometry(geom: dict, tolerance: float) -> dict:
    """Douglas-Peucker simplify; preserve_topology voorkomt zelf-intersecties."""
    try:
        s = shape(geom).simplify(tolerance, preserve_topology=True)
        return mapping(s)
    except Exception:
        return geom


def extract_raw(p: dict) -> dict:
    """Bewaar ALLE leefbaarheid-velden uit BZK WFS — zodat de frontend later
    custom-gewichten kan toepassen op de continue sub-dimensies."""
    return {
        "kscore": p.get("kscore"),
        "kafw":   p.get("kafw"),
        "afw":    p.get("afw"),
        # klasse 1-9 per sub-dim
        **{f"k{k}": p.get(f"k{k}") for k in SUB_KEYS},
        # continu per sub-dim — basis voor custom weging
        **{k: p.get(k) for k in SUB_KEYS},
        "year":   p.get("year"),
    }


def feature_chip_props(p: dict, code_field: str = "id") -> dict:
    """Standaard top-level afgeleide velden (gedeeld tussen alle niveaus).

    Behoudt 'raw' subobject voor herwegen-mogelijkheden.
    """
    afw = p.get("afw")
    return {
        "code": p.get(code_field),
        "naam": p.get("name") or p.get("gemeente"),
        "gemeente": p.get("gemeente"),  # voor wijk/buurt: parent-context
        "klasse": p.get("kscore"),
        "afw": round(float(afw), 4) if afw is not None else None,
        "top_pct_nl": top_percent_from_afw(afw),
        "pct_below_nl": percentile_from_afw(afw),
        "raw": extract_raw(p),
    }


# =============================================================================
# Build per niveau
# =============================================================================
def gemeentecode_from_id(feat_id: str) -> str | None:
    """Extract gemeente-code uit BZK feature-id.

    Conventie:
      gemeente-id: 'GM0014'
      wijk-id:     'WK001400'   → eerste 4 num cijfers = gemeente
      buurt-id:    'BU00140000' → idem
    """
    if not feat_id or len(feat_id) < 6:
        return None
    # Strip alphabetic prefix, neem eerste 4 numerieke chars + prefix 'GM'
    digits = "".join(c for c in feat_id[2:] if c.isdigit())
    if len(digits) < 4:
        return None
    return f"GM{digits[:4]}"


def build_geojson_from_bzk(features: list[dict], simplify_tol: float,
                           pdok_map: dict[str, dict] | None = None,
                           is_gemeente: bool = False) -> dict:
    """Maak GeoJSON FeatureCollection uit BZK WFS features.

    pdok_map: voor gemeente (direct id-lookup) én voor wijk/buurt
              (via gemeentecode_from_id derivation) — voegt provincie-info toe.
    """
    out = []
    for f in features:
        p = f.get("properties", {})
        props = feature_chip_props(p)
        if props["afw"] is None or props["code"] is None:
            continue
        # Provincie-koppeling
        if pdok_map:
            if is_gemeente:
                info = pdok_map.get(props["code"])
            else:
                gem_code = gemeentecode_from_id(props["code"])
                info = pdok_map.get(gem_code) if gem_code else None
            if info:
                props["provincie"] = info["provincie_naam"]
                props["provincie_code"] = info["provincie_code"]
        out.append({
            "type": "Feature",
            "geometry": simplify_geometry(f.get("geometry"), simplify_tol),
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": out}


def split_by_provincie(fc: dict) -> dict[str, dict]:
    """Split een FeatureCollection in N FeatureCollections per provincie_code.

    Features zonder provincie_code komen in bucket '_orphan'.
    """
    by_prov: dict[str, list] = {}
    for f in fc["features"]:
        pcode = (f.get("properties") or {}).get("provincie_code") or "_orphan"
        by_prov.setdefault(pcode, []).append(f)
    return {
        pcode: {"type": "FeatureCollection", "features": feats}
        for pcode, feats in by_prov.items()
    }


def compute_bbox(geom: dict) -> list[float]:
    """[minLon, minLat, maxLon, maxLat] uit een GeoJSON-geometry."""
    minLon = minLat = float("inf")
    maxLon = maxLat = float("-inf")

    def walk(c):
        nonlocal minLon, minLat, maxLon, maxLat
        if isinstance(c[0], (int, float)):
            lon, lat = c[0], c[1]
            if lon < minLon: minLon = lon
            if lon > maxLon: maxLon = lon
            if lat < minLat: minLat = lat
            if lat > maxLat: maxLat = lat
        else:
            for sub in c:
                walk(sub)

    walk(geom["coordinates"])
    return [round(minLon, 4), round(minLat, 4), round(maxLon, 4), round(maxLat, 4)]


def fc_bbox(fc: dict) -> list[float]:
    """Combined bbox van alle features in een FeatureCollection."""
    minLon = minLat = float("inf")
    maxLon = maxLat = float("-inf")
    for f in fc["features"]:
        b = compute_bbox(f["geometry"])
        if b[0] < minLon: minLon = b[0]
        if b[1] < minLat: minLat = b[1]
        if b[2] > maxLon: maxLon = b[2]
        if b[3] > maxLat: maxLat = b[3]
    return [round(minLon, 4), round(minLat, 4), round(maxLon, 4), round(maxLat, 4)]


def build_provincies_geojson(pdok_provs: list[dict],
                              gemeenten_fc: dict) -> dict:
    """Provincie-aggregatie via rekenkundig gemiddelde van gemeente-data.

    Aggregeert ZOWEL totaal-afw als alle 5 sub-dimensie-continuvelden,
    zodat de frontend later eigen weging kan toepassen op provincie-niveau.
    """
    # Map provincie_code → list of {totaal afw, sub afws} dicts
    by_prov: dict[str, list[dict]] = {}
    by_prov_naam: dict[str, str] = {}
    for f in gemeenten_fc["features"]:
        p = f["properties"]
        pcode = p.get("provincie_code")
        if not pcode:
            continue
        raw = p.get("raw", {})
        record = {
            "afw": raw.get("afw"),
            **{k: raw.get(k) for k in SUB_KEYS},
        }
        # Skip als afw of een sub None is
        if record["afw"] is None:
            continue
        by_prov.setdefault(pcode, []).append(record)
        if p.get("provincie"):
            by_prov_naam[pcode] = p["provincie"]

    out = []
    for f in pdok_provs:
        pp = f.get("properties", {})
        pcode = pp.get("code") or pp.get("identificatie")
        records = by_prov.get(pcode, [])
        if not records:
            print(f"   ⚠ provincie {pcode} ({pp.get('naam')}): geen gemeente-data", flush=True)
            continue
        n = len(records)

        def avg(key):
            vs = [r[key] for r in records if r.get(key) is not None]
            return round(sum(vs) / len(vs), 4) if vs else None

        avg_afw = avg("afw")
        sub_avgs = {k: avg(k) for k in SUB_KEYS}

        out.append({
            "type": "Feature",
            "geometry": simplify_geometry(f.get("geometry"), 0.002),
            "properties": {
                "code": pcode,
                "naam": pp.get("naam"),
                "afw": avg_afw,
                "top_pct_nl": top_percent_from_afw(avg_afw),
                "pct_below_nl": percentile_from_afw(avg_afw),
                "n_gemeenten": n,
                "min_gem_afw": round(min(r["afw"] for r in records), 4),
                "max_gem_afw": round(max(r["afw"] for r in records), 4),
                # Raw aggregaten — frontend kan hiermee custom-weging
                # toepassen bv. avg_afw_custom = 0.4*vrz + 0.3*onv + ...
                "raw": {
                    "afw": avg_afw,
                    **sub_avgs,
                    # Geen klasse-aggregaat (klasse is afgeleide, niet
                    # zinvol om te middelen). Year erbij voor consistentie.
                    "year": "2024",
                },
            },
        })
    return {"type": "FeatureCollection", "features": out}


# =============================================================================
# I/O + main
# =============================================================================
def write_geojson(path: Path, fc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = path.stat().st_size / 1024
    # Voor FeatureCollections feature-count tonen; voor index.json (plain dict) alleen size
    if isinstance(fc, dict) and "features" in fc:
        print(f"   ✓ {path.relative_to(ROOT)}  "
              f"({size_kb:.0f} KB · {len(fc['features'])} features)", flush=True)
    else:
        print(f"   ✓ {path.relative_to(ROOT)}  ({size_kb:.0f} KB)", flush=True)


def main():
    print("=" * 60)
    print("Leefbaarheidskaart — build (provincie+gemeente+wijk+buurt)")
    print("=" * 60)

    # --- Fetch alle bronnen ---
    bzk_gem  = fetch_bzk("lbm3:gemeentescore24")
    pdok_map = fetch_pdok_gemeenten()
    pdok_provs = fetch_pdok_provincies()
    bzk_wijk = fetch_bzk("lbm3:wijkscore24")
    bzk_buurt = fetch_bzk("lbm3:buurtscore24")

    # --- Build per niveau ---
    # Simplify-tolerance per niveau:
    #   provincie 0.002° (~200m) — heel-NL view, ruwe contour
    #   gemeente  0.0005° (~50m) — stad-view
    #   wijk      0.0003° (~30m) — stadsdeel-view, kleinere polygons
    #   buurt     0.0001° (~10m) — straat-view, behoud detail
    print("\n→ Build gemeenten.geojson…", flush=True)
    gem_fc = build_geojson_from_bzk(bzk_gem, simplify_tol=0.0005,
                                     pdok_map=pdok_map, is_gemeente=True)
    write_geojson(OUT_DIR / "gemeenten.geojson", gem_fc)

    print("\n→ Build provincies.geojson (uit gemeente-aggregatie)…", flush=True)
    prov_fc = build_provincies_geojson(pdok_provs, gem_fc)
    write_geojson(OUT_DIR / "provincies.geojson", prov_fc)

    print("\n→ Build wijken.geojson (één bestand, lazy-load)…", flush=True)
    wijk_fc = build_geojson_from_bzk(bzk_wijk, simplify_tol=0.0003,
                                      pdok_map=pdok_map)
    write_geojson(OUT_DIR / "wijken.geojson", wijk_fc)

    print("\n→ Build buurten/ (split per provincie voor lazy fetch)…", flush=True)
    buurt_fc = build_geojson_from_bzk(bzk_buurt, simplify_tol=0.0001,
                                       pdok_map=pdok_map)
    # NIET één geojson — splitsen per provincie naar 12 bestanden + index
    buurt_per_prov = split_by_provincie(buurt_fc)
    buurt_dir = OUT_DIR / "buurten"
    buurt_dir.mkdir(parents=True, exist_ok=True)
    # Cleanup oude files
    for old in buurt_dir.glob("*.geojson"):
        old.unlink()
    if (buurt_dir / "index.json").exists():
        (buurt_dir / "index.json").unlink()

    index = {}
    for pcode, sub_fc in buurt_per_prov.items():
        if pcode == "_orphan":
            print(f"   ⚠ {len(sub_fc['features'])} buurten zonder provincie — overgeslagen")
            continue
        fname = f"{pcode}.geojson"
        write_geojson(buurt_dir / fname, sub_fc)
        index[pcode] = {
            "file": f"buurten/{fname}",
            "n_features": len(sub_fc["features"]),
            "bbox": fc_bbox(sub_fc),
            # naam erbij: pak uit eerste feature
            "naam": (sub_fc["features"][0]["properties"].get("provincie") if sub_fc["features"] else pcode),
        }
    # Index-bestand: minimaal, gebruikt door frontend om te bepalen welke provincie te fetchen op zoom
    write_geojson(buurt_dir / "index.json", index)
    # Verwijder de monolithische buurten.geojson als die nog bestaat
    old_monolith = OUT_DIR / "buurten.geojson"
    if old_monolith.exists():
        old_monolith.unlink()
        print(f"   ✓ oude monolithische buurten.geojson verwijderd")

    # --- Sanity stats ---
    print("\n=== Klaar ===")
    for naam, fc in [("provincies", prov_fc), ("gemeenten", gem_fc),
                     ("wijken", wijk_fc), ("buurten", buurt_fc)]:
        afws = sorted(f["properties"]["afw"] for f in fc["features"]
                      if f["properties"].get("afw") is not None)
        if not afws: continue
        n = len(afws)
        print(f"\n{naam}-afw verdeling (n={n}):")
        print(f"  min  = {afws[0]:+.3f}")
        print(f"  p25  = {afws[n//4]:+.3f}")
        print(f"  p50  = {afws[n//2]:+.3f}")
        print(f"  p75  = {afws[3*n//4]:+.3f}")
        print(f"  max  = {afws[-1]:+.3f}")

    # Top-5 / bottom-5 buurten — visuele sanity
    sorted_buurten = sorted(buurt_fc["features"],
                            key=lambda f: -f["properties"]["afw"])
    print(f"\nTop-5 leefbaarste buurten van NL:")
    for f in sorted_buurten[:5]:
        p = f["properties"]
        print(f"  {p['naam']:<25} ({p.get('gemeente') or '?':<18}) afw={p['afw']:+.3f} top {p['top_pct_nl']:.1f}%")
    print(f"\nOnderste 5 buurten van NL:")
    for f in sorted_buurten[-5:]:
        p = f["properties"]
        print(f"  {p['naam']:<25} ({p.get('gemeente') or '?':<18}) afw={p['afw']:+.3f} top {p['top_pct_nl']:.1f}%")


if __name__ == "__main__":
    main()
