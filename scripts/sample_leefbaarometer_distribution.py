#!/usr/bin/env python3
"""
Sample empirische distributie van Leefbaarometer 3.0 'afw' (continue
afwijking t.o.v. NL-gemiddelde) over Nederland.

Doel: een ECDF (empirical cumulative distribution function) bouwen zodat
we voor elke afw-waarde een betrouwbaar percentiel kunnen tonen — i.p.v.
de discrete 1-9 klasse die op stedelijke / welvarende locaties
disproportioneel vaak '9' geeft.

Aanpak (population-weighted via PC6-postcodes):
  1. Trek N random PC6-postcode-centroides via PDOK Locatieserver.
     Nederland heeft ~474.000 PC6's, elke representeert ~35 huishoudens —
     random selectie geeft natuurlijke bevolkings-weighted spreiding.
  2. Voor elke centroide: WMS GetFeatureInfo op lbm3:clippedgridscore24.
  3. Verzamel ~1500-2000 valid samples → fit ECDF.
  4. Schrijf ECDF als constante naar
     apps/api/adapters/leefbaarometer_distribution.py.

Usage:
    python3 scripts/sample_leefbaarometer_distribution.py [N]

Beleefd qua belasting: 6 concurrent requests, ~3-5 min voor 1500 samples.
Resultaat is one-time computation; check in git en gebruik in adapter.
"""
from __future__ import annotations

import asyncio
import random
import re
import sys
import time
from pathlib import Path

import httpx

WMS_URL = "https://geo.leefbaarometer.nl/wms"
LAYER = "lbm3:clippedgridscore24"
TIMEOUT_S = 12.0

PDOK_BASE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
# Aantal PC6-postcodes in NL (uit PDOK numFound). Update bij grote DB-mutaties.
PC6_TOTAL = 474234

# Beleefdheid: max 6 calls tegelijk. 1500 PC6's × 200ms ≈ 5 min.
CONCURRENCY = 6
RD_RE = re.compile(r"POINT\(([\d.]+)\s+([\d.]+)\)")


async def random_pc6_centroides(n: int, seed: int = 42) -> list[tuple[str, float, float]]:
    """Trek N random PC6-centroides via random PC4-prefix queries.

    PDOK heeft een deep-paging-limiet (start ≤ ~10000) waardoor random
    `start` op de hele postcode-set niet werkt. Workaround: trek een random
    PC4 (1000-9999), vraag PC6's met dat prefix (rows=1, start=random small),
    en pak één PC6 per PC4. Geeft natuurlijke geo + bevolkings-spreiding
    omdat elke PC4 ~5000 huishoudens telt.
    """
    rng = random.Random(seed)
    pcs: list[tuple[str, float, float]] = []
    seen_pcs: set[str] = set()
    sem = asyncio.Semaphore(4)
    target_attempts = int(n * 2.2)   # ~45% van PC4's bestaat → factor 2,2

    async def one(client, attempt_idx):
        async with sem:
            pc4 = rng.randint(1000, 9999)
            params = {
                "q": f"postcode:{pc4}*", "fq": "type:postcode",
                "rows": 1, "start": rng.randint(0, 50),  # max ~300 per PC4
                "fl": "postcode,centroide_rd",
            }
            try:
                r = await client.get(PDOK_BASE, params=params, timeout=10)
                if r.status_code != 200:
                    return
                docs = r.json().get("response", {}).get("docs", [])
                for d in docs:
                    pc = d.get("postcode")
                    if pc in seen_pcs:
                        continue
                    m = RD_RE.match(d.get("centroide_rd", ""))
                    if m:
                        seen_pcs.add(pc)
                        pcs.append((pc, float(m.group(1)), float(m.group(2))))
                        if len(pcs) % 200 == 0:
                            print(f"  PDOK: {len(pcs)} PC6's "
                                  f"(probed {attempt_idx + 1})", flush=True)
            except Exception:
                pass

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one(client, i) for i in range(target_attempts)))
    rng.shuffle(pcs)
    print(f"  PDOK: {len(pcs)} unieke PC6-centroides verzameld", flush=True)
    return pcs[:n]


async def fetch_lbm(client: httpx.AsyncClient, x: float, y: float) -> dict | None:
    """Eén GetFeatureInfo. Returnt properties-dict of None."""
    half = 25
    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
        "layers": LAYER, "query_layers": LAYER,
        "bbox": f"{x - half},{y - half},{x + half},{y + half}",
        "width": "3", "height": "3",
        "srs": "EPSG:28992", "x": "1", "y": "1",
        "info_format": "application/json",
    }
    try:
        r = await client.get(WMS_URL, params=params, timeout=TIMEOUT_S)
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            return None
        return feats[0].get("properties")
    except Exception:
        return None


async def sample_until(target: int, seed: int = 42) -> list[dict]:
    """Verzamel ~target valid samples via PC6-centroides."""
    print(f"Stap 1/2: PDOK PC6-postcodes ophalen (target {target})...", flush=True)
    pcs = await random_pc6_centroides(int(target * 1.15), seed=seed)

    valid: list[dict] = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def try_one(client, idx, pc, x, y):
        async with sem:
            props = await fetch_lbm(client, x, y)
            if props and props.get("afw") is not None:
                try:
                    afw = float(props["afw"])
                    valid.append({
                        "pc": pc, "x": round(x), "y": round(y),
                        "afw": afw,
                        "k": int(props.get("kscore", 0) or 0),
                        "won": int(props.get("kwon", 0) or 0),
                        "fys": int(props.get("kfys", 0) or 0),
                        "vrz": int(props.get("kvrz", 0) or 0),
                        "soc": int(props.get("ksoc", 0) or 0),
                        "onv": int(props.get("konv", 0) or 0),
                    })
                    if len(valid) % 100 == 0:
                        rate = len(valid) * 100 // (idx + 1)
                        print(f"  ... {len(valid)} / {target} valid "
                              f"(hit-rate {rate}%)", flush=True)
                except (ValueError, TypeError):
                    pass

    print(f"\nStap 2/2: {len(pcs)} WMS-calls (concurrency={CONCURRENCY})...",
          flush=True)
    async with httpx.AsyncClient() as client:
        tasks = [try_one(client, i, pc, x, y) for i, (pc, x, y) in enumerate(pcs)]
        for i in range(0, len(tasks), 100):
            await asyncio.gather(*tasks[i:i + 100])
            if len(valid) >= target:
                break
    return valid[:target] if len(valid) >= target else valid


def build_ecdf(samples: list[dict]) -> list[tuple[float, float]]:
    """Build sorted (afw, percentiel)-pairs.

    Percentiel = % van NL-bevolking met LAGERE leefbaarheid (dus hoog =
    excellent). Output: 200 evenredig verdeelde keypoints (0.5%-stappen)
    voor compacte storage; tussen keypoints lineair interpoleren.
    """
    if not samples:
        return []
    afws = sorted(s["afw"] for s in samples)
    n = len(afws)
    keypoints = []
    for pct in range(0, 1001):  # 0.0% ... 100.0% in stapjes van 0.1%
        idx = min(int(pct / 1000 * (n - 1)), n - 1)
        keypoints.append((round(afws[idx], 4), round(pct / 10, 1)))
    # Dedupliceer: bij plateaus dezelfde afw → keep eerste / laatste %.
    # We willen lower bound voor "Top X%" semantiek: een persoon met
    # afw = a krijgt het percentiel waar a == ECDF^-1(p) == lowest p where afws[idx] >= a.
    return keypoints


def write_module(samples: list[dict], ecdf: list[tuple[float, float]],
                 out_path: Path) -> None:
    """Schrijf de adapter-module met de constante."""
    if not samples:
        raise SystemExit("Geen samples — check WMS-server of bbox.")

    afws = [s["afw"] for s in samples]
    n = len(afws)

    # Per-klasse stats
    klasse_count: dict[int, int] = {}
    for s in samples:
        klasse_count[s["k"]] = klasse_count.get(s["k"], 0) + 1
    klasse_lines = "\n".join(
        f"#   klasse {k}: {v}× ({v * 100 / n:.1f}%)"
        for k, v in sorted(klasse_count.items())
    )

    # Min/median/max
    sorted_afw = sorted(afws)
    p10 = sorted_afw[int(n * 0.10)]
    p25 = sorted_afw[int(n * 0.25)]
    p50 = sorted_afw[int(n * 0.50)]
    p75 = sorted_afw[int(n * 0.75)]
    p90 = sorted_afw[int(n * 0.90)]
    p99 = sorted_afw[min(int(n * 0.99), n - 1)]

    body = f'''"""
Empirische ECDF van Leefbaarometer 3.0 'afw' (continue afwijking t.o.v. NL-gem).

GEGENEREERD door scripts/sample_leefbaarometer_distribution.py.
NIET handmatig wijzigen — herdraai het script bij een nieuwe peiljaar.

Sample-stats (n = {n}):
  min:    {min(afws):+.3f}
  p10:    {p10:+.3f}
  p25:    {p25:+.3f}
  p50:    {p50:+.3f}  (mediaan)
  p75:    {p75:+.3f}
  p90:    {p90:+.3f}
  p99:    {p99:+.3f}
  max:    {max(afws):+.3f}

Klasse-distributie in sample:
{klasse_lines}

Gebruik:
  from .leefbaarometer_distribution import percentile_from_afw
  pct = percentile_from_afw(0.27)  # → bv. 75.0  (= "Top 25% van NL")
"""
from __future__ import annotations

from typing import Optional

# (afw_threshold, cumulative_percentile_below) — gesorteerd op afw.
# Een persoon met afw=a krijgt percentiel = laatste pct waar threshold <= a.
# Resultaat-percentiel is "% van NL met LAGERE leefbaarheid", dus 99 = top 1%.
ECDF_KEYPOINTS: list[tuple[float, float]] = [
'''
    for afw, pct in ecdf:
        body += f"    ({afw:+.4f}, {pct:5.1f}),\n"
    body += ''']


def percentile_from_afw(afw: Optional[float]) -> Optional[float]:
    """Geef percentiel (% van NL met LAGERE leefbaarheid) voor een afw-waarde.

    afw is de continue afwijking uit Leefbaarometer WMS.
    Returnt None als afw None is. Resultaat clamped naar [0, 100].

    Voorbeelden:
      afw = -0.30  →  bv.  8.0  (= "buurt zit in onderste 8% van NL")
      afw =  0.00  →  bv. 50.0  (= mediaan)
      afw = +0.27  →  bv. 75.0  (= "buurt zit in top 25% van NL")
      afw = +0.65  →  bv. 96.5  (= "buurt zit in top 3,5% van NL")
    """
    if afw is None:
        return None
    try:
        a = float(afw)
    except (TypeError, ValueError):
        return None
    # Binary search — keypoints staan gesorteerd oplopend op afw.
    lo, hi = 0, len(ECDF_KEYPOINTS) - 1
    if a <= ECDF_KEYPOINTS[0][0]:
        return ECDF_KEYPOINTS[0][1]
    if a >= ECDF_KEYPOINTS[-1][0]:
        return ECDF_KEYPOINTS[-1][1]
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if ECDF_KEYPOINTS[mid][0] <= a:
            lo = mid
        else:
            hi = mid
    # Lineaire interpolatie tussen lo en hi
    a0, p0 = ECDF_KEYPOINTS[lo]
    a1, p1 = ECDF_KEYPOINTS[hi]
    if a1 == a0:
        return round(p0, 1)
    frac = (a - a0) / (a1 - a0)
    return round(p0 + frac * (p1 - p0), 1)


def top_percent_from_afw(afw: Optional[float]) -> Optional[float]:
    """Convenience: 'Top X%' i.p.v. ECDF-percentiel.

    100 - percentile_from_afw(afw). Een afw met percentiel 96 → top 4%.
    Returnt None als afw None.
    """
    p = percentile_from_afw(afw)
    return None if p is None else round(100.0 - p, 1)
'''

    out_path.write_text(body)
    print(f"\n✓ Geschreven: {out_path}")
    print(f"   ECDF-keypoints: {len(ecdf)}")


async def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    print(f"Sample-target: {target} valid grid-cellen "
          f"(maximaal {int(target * 2.5)} probes)")
    t0 = time.time()
    samples = await sample_until(target)
    elapsed = time.time() - t0
    print(f"\n→ {len(samples)} valid samples in {elapsed:.0f}s")

    if not samples:
        sys.exit("FAIL: 0 valid samples — server down of bbox fout.")

    ecdf = build_ecdf(samples)

    out = (Path(__file__).resolve().parent.parent
           / "apps" / "api" / "adapters"
           / "leefbaarometer_distribution.py")
    write_module(samples, ecdf, out)

    # Even live demo
    print("\nVoorbeeld-percentielen:")
    # Compileer + import direct
    import importlib.util
    spec = importlib.util.spec_from_file_location("lbm_dist", out)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for test_afw in (-0.40, -0.20, -0.10, 0.0, 0.10, 0.21, 0.27, 0.40, 0.52, 0.61, 0.80):
        p = mod.percentile_from_afw(test_afw)
        t = mod.top_percent_from_afw(test_afw)
        print(f"  afw = {test_afw:+.2f}  →  percentiel {p:5.1f}  →  Top {t:5.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
