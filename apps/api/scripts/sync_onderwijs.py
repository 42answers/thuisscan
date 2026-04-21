"""
Sync script — merge LRK + DUO-scholen + Inspectie-oordelen → onderwijs.json

Bronnen:
  - LRK (Landelijk Register Kinderopvang):
      https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv
      ~31K kinderopvang-locaties (KDV/BSO/VGO/GO) met BAG_id + postcode
  - DUO vestigingen basisonderwijs (~6K scholen):
      https://onderwijsdata.duo.nl/dataset/.../download/vestigingenbo.csv
  - Inspectie-oordelen (po/so/vo) via DUO:
      https://onderwijsdata.duo.nl/dataset/.../download/oordeel_po_so_vo.csv

Geocoding: via PDOK Locatieserver postcode → lat/lon (async, concurrent).
Output: apps/api/data/onderwijs.json — geladen door adapters/onderwijs.py
bij startup. 7 dagen cache-TTL zou ruim voldoende zijn; maandelijks
opnieuw draaien is genoeg.

Draai:
    cd apps/api && python3 scripts/sync_onderwijs.py
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

LRK_URL = "https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv"
DUO_SCHOLEN_URL = (
    "https://onderwijsdata.duo.nl/dataset/786f12ea-6224-42fd-ab72-de4d7d879535/"
    "resource/dcc9c9a5-6d01-410b-967f-810557588ba4/download/vestigingenbo.csv"
)
INSPECTIE_URL = (
    "https://onderwijsdata.duo.nl/dataset/31da72f2-2858-4bc3-848e-dfe4875ba669/"
    "resource/b48d6835-0534-4008-82c8-1754b9080113/download/oordeel_po_so_vo.csv"
)
SOK_SITEMAP_URL = "https://scholenopdekaart.nl/sitemap-basisscholen.xml"

# Script-relatieve paden: werkt vanuit zowel apps/api als repo-root
HERE = Path(__file__).resolve().parent
APP_API = HERE.parent
CACHE_DIR = APP_API / "cache"
DATA_DIR = APP_API / "data"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

OUT_PATH = DATA_DIR / "onderwijs.json"

PDOK_LOCATIE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

# Parallelisme voor geocoding — PDOK lijkt dit prima te handelen; wees
# beleefd met een max concurrency van 50 parallel requests.
GEOCODE_CONCURRENCY = 50

# User-agent — LRK weigert zonder user-agent
HEADERS = {"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"}


async def _download(url: str, dest: Path) -> None:
    """Stream download naar bestand als die nog niet bestaat."""
    if dest.exists() and dest.stat().st_size > 10_000:
        print(f"  [cache hit] {dest.name}")
        return
    print(f"  downloading {url[:70]}…")
    async with httpx.AsyncClient(timeout=120.0, headers=HEADERS) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    f.write(chunk)
    print(f"  saved → {dest} ({dest.stat().st_size // 1024} KB)")


def _parse_lrk(path: Path) -> list[dict]:
    """Extract actieve kinderopvang-locaties met BAG_id en kindplaatsen.

    LRK CSV is Latin-1 (Windows-1252) geëncodeerd, niet UTF-8.
    """
    out: list[dict] = []
    with path.open(encoding="cp1252", errors="replace") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("status") != "Ingeschreven":
                continue
            pc = (row.get("opvanglocatie_postcode") or "").strip().upper()
            if not pc:
                continue  # VGO zonder vaste locatie
            try:
                kindplaatsen = int(row.get("aantal_kindplaatsen") or 0)
            except ValueError:
                kindplaatsen = 0
            out.append({
                "naam": (row.get("actuele_naam_oko") or "").strip(),
                "type": row.get("type_oko") or "",   # KDV/BSO/VGO/GO
                "postcode": pc,
                "adres": (row.get("opvanglocatie_adres") or "").strip(),
                "gemeente": (row.get("verantwoordelijke_gemeente") or "").strip(),
                "kindplaatsen": kindplaatsen,
                "url": row.get("lrk_url") or "",
            })
    print(f"  {len(out)} actieve kinderopvang-locaties")
    return out


def _parse_scholen(path: Path) -> list[dict]:
    """DUO vestigingen basisonderwijs (BRIN + VESTIGINGSCODE)."""
    out: list[dict] = []
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            brin = (row.get("INSTELLINGSCODE") or "").strip()
            vest = (row.get("VESTIGINGSCODE") or "").strip()
            pc = (row.get("POSTCODE") or "").strip().upper()
            if not pc or not brin:
                continue
            out.append({
                "naam": (row.get("VESTIGINGSNAAM") or "").strip(),
                "brin": brin,                        # bv. '32JK'
                "vestiging": vest,                   # bv. '32JK00'
                "postcode": pc,
                "adres": (row.get("STRAATNAAM") or "").strip(),
                "plaats": (row.get("PLAATSNAAM") or "").strip(),
                "gemeente": (row.get("GEMEENTENAAM") or "").strip(),
                "denominatie": (row.get("DENOMINATIE") or "").strip(),
                "url": (row.get("INTERNETADRES") or "").strip(),
            })
    print(f"  {len(out)} basisonderwijs-vestigingen")
    return out


async def _fetch_sok_sitemap() -> dict[tuple[str, str], str]:
    """Download SoK-sitemap en bouw (plaats_slug, naam_slug) → url mapping.

    Scholen op de Kaart (SoK) heeft geen publieke BRIN→URL API en rendert
    client-side met Angular. De sitemap bevat echter ALLE school-URLs:
        https://scholenopdekaart.nl/basisscholen/<plaats>/<id>/<slug>/

    We bouwen een index op (plaats_slug, naam_slug) + een reverse-index
    op naam_slug alleen (voor unique-matches zonder plaats-match).
    """
    try:
        async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
            resp = await client.get(SOK_SITEMAP_URL)
            resp.raise_for_status()
            xml_text = resp.text
    except Exception as e:
        print(f"  sitemap-download mislukt: {e}")
        return {}

    import re as _re
    urls = _re.findall(r"<loc>(https://scholenopdekaart\.nl/basisscholen/[^<]+)</loc>", xml_text)
    print(f"  SoK sitemap: {len(urls)} school-URLs")
    index: dict[tuple[str, str], str] = {}
    for u in urls:
        parts = u.rstrip("/").split("/")
        if len(parts) < 6:
            continue
        plaats_slug, _id, naam_slug = parts[-3], parts[-2], parts[-1]
        # Voeg zowel ruwe slug als ALLE stripped varianten toe aan index.
        # SoK-slugs hebben vaak prefixen als 'obs-', 'cbs-', 'rkbs-' die
        # in DUO-namen anders of niet voorkomen ('Corantijn' vs 'obs-corantijn').
        for variant in _slug_variants(naam_slug.replace("-", " ")):
            index.setdefault((plaats_slug, variant), u)
        # En de ruwe originele vorm
        index.setdefault((plaats_slug, naam_slug), u)
    return index


def _slugify(s: str) -> str:
    """Slugify als Scholen op de Kaart: lowercase, alleen a-z0-9-."""
    import re as _re
    import unicodedata
    # Normalize accenten (é → e)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = _re.sub(r"[^a-z0-9\s-]", "", s)
    s = _re.sub(r"[\s-]+", "-", s.strip())
    return s.strip("-")


def _slug_variants(naam: str) -> list[str]:
    """Genereer meerdere slug-varianten voor fuzzy matching.

    DUO-namen variëren (bv 'R.K. Basisschool' / 'Rooms-Katholieke Basisschool'
    / 'RK Basisschool'). SoK heeft vaak consistenter namen. Door prefixen
    te strippen of variaties te proberen, raken we meer matches.
    """
    base = _slugify(naam)
    variants = [base]
    # Strip gangbare prefixen + schooltype-woorden
    stripped = base
    for pattern in [
        "openbare-basisschool-",
        "christelijke-basisschool-",
        "rooms-katholieke-basisschool-",
        "protestants-christelijke-basisschool-",
        "rooms-katholieke-",
        "protestants-christelijke-",
        "christelijk-kindcentrum-",
        "openbaar-kindcentrum-",
        "kindcentrum-",
        "basisschool-",
        "obs-",
        "cbs-",
        "rkbs-",
        "kbs-",
        "pcbs-",
        "rk-",
        "pc-",
        "cb-",
    ]:
        if stripped.startswith(pattern):
            stripped = stripped[len(pattern):]
            variants.append(stripped)
            break
    # Soms heeft DUO 'school' in de naam, SoK niet (of andersom)
    if stripped.startswith("de-") or stripped.startswith("het-"):
        variants.append(stripped.split("-", 1)[1])  # strip lidwoord
    # Unique + non-empty
    return list(dict.fromkeys(v for v in variants if v))


def _find_sok_url(
    naam: str, plaats: str, sok_index: dict[tuple[str, str], str]
) -> Optional[str]:
    """Zoek een Scholen op de Kaart-URL voor (naam, plaats).

    Probeer meerdere slug-varianten (exact + stripped prefixes) tegen
    de (plaats, naam) index van SoK. Retourneert None bij geen match.
    """
    if not naam or not plaats:
        return None
    plaats_slug = _slugify(plaats)
    for variant in _slug_variants(naam):
        url = sok_index.get((plaats_slug, variant))
        if url:
            return url
    # Slug-only match als er exact één SoK-URL met deze slug bestaat
    # (werkt voor unieke school-namen zoals 'de-visserschool')
    slug_only = {slug: url for (_, slug), url in sok_index.items()}
    for variant in _slug_variants(naam):
        url = slug_only.get(variant)
        if url:
            return url
    return None


def _parse_inspectie(path: Path) -> dict[str, dict]:
    """Map BRIN-vestiging → laatste eindoordeel.

    De CSV bevat meerdere rijen per vestiging (diverse onderzoeken door de
    jaren). We nemen de MEEST RECENTE peildatum per vestiging.
    """
    by_key: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Het kan po, so of vo zijn; voor onze sector (basis) filteren we op PO
            sector = (row.get("Sector") or "").strip()
            brin = (row.get("BRIN") or "").strip()
            vest = (row.get("Vestiging") or "").strip()
            if not brin:
                continue
            key = f"{brin}{vest}"  # 'BRIN' + 'Vestiging' → bv. '32JK00'
            oordeel = (row.get("EindoordeelKwaliteit") or "").strip()
            peildatum = (row.get("Peildatum") or "").strip()
            if not oordeel:
                continue
            existing = by_key.get(key)
            if not existing or peildatum > existing.get("peildatum", ""):
                by_key[key] = {
                    "oordeel": oordeel,
                    "peildatum": peildatum,
                    "sector": sector,
                }
    print(f"  {len(by_key)} vestigingen met inspectie-oordeel")
    return by_key


async def _geocode_postcodes(postcodes: list[str]) -> dict[str, tuple[float, float]]:
    """Batch-geocode postcodes via PDOK Locatieserver → {pc: (lat, lon)}.

    Unieke postcodes worden concurrent opgevraagd met een semaphore om
    PDOK niet te overvragen. Bij fouten: silent skip (die locatie wordt
    later genegeerd in de adapter).
    """
    sem = asyncio.Semaphore(GEOCODE_CONCURRENCY)
    out: dict[str, tuple[float, float]] = {}

    async def _one(client: httpx.AsyncClient, pc: str) -> None:
        async with sem:
            try:
                params = {
                    "q": f"postcode:{pc}",
                    "fl": "centroide_ll",
                    "rows": "1",
                    "fq": "type:postcode",
                }
                resp = await client.get(PDOK_LOCATIE, params=params)
                resp.raise_for_status()
                docs = resp.json().get("response", {}).get("docs", [])
                if not docs:
                    return
                # Format: "POINT(lon lat)"
                p = docs[0].get("centroide_ll", "")
                if p.startswith("POINT(") and p.endswith(")"):
                    lon_s, lat_s = p[6:-1].split()
                    out[pc] = (float(lat_s), float(lon_s))
            except Exception:
                return

    async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
        tasks = [_one(client, pc) for pc in postcodes]
        # Progress reporting per 500
        done = 0
        batch_size = 500
        for i in range(0, len(tasks), batch_size):
            await asyncio.gather(*tasks[i:i + batch_size])
            done = min(i + batch_size, len(tasks))
            print(f"  geocoded {done}/{len(tasks)}  ({len(out)} hits)")
    return out


def _attach_coords(items: list[dict], coords: dict[str, tuple[float, float]]) -> list[dict]:
    """Voeg lat/lon toe; drop entries zonder geocode-hit."""
    out = []
    for it in items:
        c = coords.get(it.get("postcode"))
        if not c:
            continue
        it["lat"], it["lon"] = c
        out.append(it)
    return out


async def main() -> None:
    t0 = time.time()
    print("[1/5] Downloading source CSVs…")
    lrk_csv = CACHE_DIR / "lrk.csv"
    scholen_csv = CACHE_DIR / "vestigingenbo.csv"
    inspectie_csv = CACHE_DIR / "oordeel_po_so_vo.csv"
    await _download(LRK_URL, lrk_csv)
    await _download(DUO_SCHOLEN_URL, scholen_csv)
    await _download(INSPECTIE_URL, inspectie_csv)

    print("[2/5] Parsing…")
    kinderopvang = _parse_lrk(lrk_csv)
    scholen = _parse_scholen(scholen_csv)
    inspectie_by_key = _parse_inspectie(inspectie_csv)

    print("[3a/5] Scholen op de Kaart sitemap → directe URLs…")
    sok_index = await _fetch_sok_sitemap()
    sok_matches = 0
    for s in scholen:
        url = _find_sok_url(s.get("naam", ""), s.get("plaats", ""), sok_index)
        if url:
            s["sok_url"] = url
            sok_matches += 1
    pct = 100 * sok_matches // max(1, len(scholen))
    print(f"  {sok_matches} / {len(scholen)} scholen hebben directe SoK-URL ({pct}%)")

    print("[3/5] Join scholen ↔ inspectie op BRIN-vestiging…")
    for s in scholen:
        key = f"{s['brin']}{s['vestiging'][-2:]}" if len(s["vestiging"]) >= 2 else s["brin"]
        # Try both: <BRIN><vestigingsnr> and <BRIN><vestiging>
        for try_key in (s["vestiging"], f"{s['brin']}00"):
            ins = inspectie_by_key.get(try_key)
            if ins:
                s["inspectie_oordeel"] = ins["oordeel"]
                s["inspectie_peildatum"] = ins["peildatum"]
                break
    joined_count = sum(1 for s in scholen if s.get("inspectie_oordeel"))
    print(f"  {joined_count} / {len(scholen)} scholen hebben inspectie-oordeel")

    print("[4/5] Geocoding unieke postcodes…")
    all_pcs = sorted({it["postcode"] for it in kinderopvang + scholen})
    print(f"  {len(all_pcs)} unieke postcodes")
    coords = await _geocode_postcodes(all_pcs)
    print(f"  → {len(coords)} postcodes geocoded ({100 * len(coords) // max(1, len(all_pcs))}%)")

    kinderopvang = _attach_coords(kinderopvang, coords)
    scholen = _attach_coords(scholen, coords)
    print(f"  kinderopvang met coord: {len(kinderopvang)}")
    print(f"  scholen met coord:      {len(scholen)}")

    print(f"[5/5] Schrijven naar {OUT_PATH}…")
    out = {
        "peildatum": time.strftime("%Y-%m-%d"),
        "bronnen": {
            "kinderopvang": "LRK Landelijk Register Kinderopvang",
            "scholen": "DUO basisonderwijs vestigingen",
            "inspectie": "Onderwijsinspectie (po/so/vo) via DUO",
        },
        "kinderopvang": kinderopvang,
        "scholen": scholen,
    }
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    size_mb = OUT_PATH.stat().st_size / 1e6
    print(f"Klaar in {time.time() - t0:.0f}s. File size: {size_mb:.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())
