"""
Thuisscan API — fase 1 spike.

Exposes two endpoints that together form the complete address-lookup flow:
- GET /suggest?q=...   : lijst adres-kandidaten (voor frontend autocomplete)
- GET /lookup?id=...   : volledige adres-details + BAG-id + buurtcode

Draai lokaal:
    uvicorn main:app --reload

Test:
    curl "http://localhost:8000/suggest?q=Damrak+1+Amsterdam"
    curl "http://localhost:8000/lookup?id=<id-uit-suggest>"
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from adapters import bag, pdok_locatie, static_maps, html_to_pdf
import orchestrator
import rapport_template

app = FastAPI(
    title="Thuisscan API",
    version="0.1.0",
    description="Eén adres -> volledig woning- en buurtprofiel uit NL open data.",
)

# Tijdens MVP wijd open; in productie vervangen door specifiek domein.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/suggest")
async def suggest_endpoint(
    q: str = Query(..., min_length=2, description="Gedeeltelijk adres"),
    rows: int = Query(5, ge=1, le=20),
) -> dict:
    """Autocomplete-kandidaten voor een adres-invoerveld.

    Retourneert de ruwe Solr-docs uit PDOK met minimaal (id, weergavenaam).
    De frontend toont deze als dropdown; bij klik roep je /lookup aan.
    """
    try:
        docs = await pdok_locatie.suggest(q, rows=rows)
    except Exception as e:
        # PDOK downtime mag niet onze 500 worden als het iets anders is; log upstream.
        raise HTTPException(status_code=502, detail=f"PDOK suggest faalde: {e}") from e
    return {
        "query": q,
        "count": len(docs),
        "candidates": [
            {"id": d.get("id"), "weergavenaam": d.get("weergavenaam"), "type": d.get("type")}
            for d in docs
        ],
    }


@app.get("/lookup")
async def lookup_endpoint(
    id: str | None = Query(None, description="Adres-id uit /suggest"),
    q: str | None = Query(None, description="Alternatief: direct tekst (one-shot)"),
) -> dict:
    """Volledige adres-details, inclusief BAG-id + buurt/wijk/gemeente-codes.

    Twee modi:
    - id=...  : je hebt al geklikt op een suggestie, haal details op.
    - q=...   : shortcut, eerste hit van suggest + direct lookup.
    """
    if not id and not q:
        raise HTTPException(status_code=400, detail="Geef 'id' of 'q'")

    try:
        match = (
            await pdok_locatie.lookup(id) if id else await pdok_locatie.geocode(q)  # type: ignore[arg-type]
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PDOK lookup faalde: {e}") from e

    if match is None:
        raise HTTPException(status_code=404, detail="Geen adres gevonden")

    return {
        "display_name": match.display_name,
        "bag": {
            "verblijfsobject_id": match.bag_verblijfsobject_id,
            "pand_id": match.bag_pand_id,
        },
        "administratief": {
            "buurtcode": match.buurtcode,
            "wijkcode": match.wijkcode,
            "gemeentecode": match.gemeentecode,
            "postcode": match.postcode,
            "huisnummer": match.huisnummer,
        },
        "geometrie": {
            "wgs84": {"lat": match.lat, "lon": match.lon},
            "rd": {"x": match.rd_x, "y": match.rd_y},  # voor RIVM/Klimaat-WMS
        },
    }


@app.get("/scan")
async def scan_endpoint(q: str = Query(..., min_length=3, description="Adres")) -> dict:
    """Volledige Thuisscan voor een adres."""
    try:
        result = await orchestrator.scan(q)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scan faalde: {e}") from e

    # PRE-FETCH: warm de rapport-data parallel op de achtergrond zodat
    # bij PDF-knop click alle lazy endpoints + maps al gecached zijn.
    # Niet-blocking: scan-response gaat onmiddellijk naar de user.
    import asyncio as _aio
    async def _prefetch():
        try:
            await _gather_rapport_data(q)
            print(f"[prefetch] rapport-data ready voor: {q}", flush=True)
        except Exception as e:
            print(f"[prefetch] failed: {e}", flush=True)
    _aio.create_task(_prefetch())

    return orchestrator.result_as_dict(result)


# =============================================================================
# Rapport-data + PDF caches (in-memory, TTL).
# Per dag (datum-stempel) en per BAG-VBO. Bij dezelfde key in TTL = instant.
# =============================================================================
import time as _time

_DATA_CACHE: dict[str, tuple[float, dict, str]] = {}    # key → (ts, data, html)
_PDF_CACHE: dict[str, tuple[float, bytes, str]] = {}    # key → (ts, pdf, filename)
_DATA_TTL_S = 15 * 60          # 15 minuten — data muteert nauwelijks binnen sessie
_PDF_TTL_S = 24 * 3600          # 24 uur — PDF voor zelfde adres = 100% identiek

def _cache_key(q: str) -> str:
    """Sluitend-genoeg sleutel voor caching (dag-precision + lower-case adres)."""
    from datetime import date as _date
    return f"{_date.today().isoformat()}:{q.strip().lower()}"


async def _gather_rapport_data(q: str) -> tuple[dict, str, dict[str, float]]:
    """Verzamel alle data + render HTML voor het rapport.

    Returnt (data, html, timing). Gedeeld door /rapport en /rapport.pdf.
    Cached 15 min per (datum, q).
    """
    import asyncio as _aio
    timing: dict[str, float] = {}

    cache_key = _cache_key(q)
    hit = _DATA_CACHE.get(cache_key)
    if hit and (_time.time() - hit[0]) < _DATA_TTL_S:
        timing["from_cache"] = round(_time.time() - hit[0], 1)
        return hit[1], hit[2], timing

    t0 = _time.time()
    try:
        scan_result = await orchestrator.scan(q)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scan faalde: {e}") from e
    timing["scan"] = round(_time.time() - t0, 2)

    scan_dict = orchestrator.result_as_dict(scan_result)
    a = scan_dict.get("adres", {})
    lat = a.get("wgs84", {}).get("lat")
    lon = a.get("wgs84", {}).get("lon")
    rd_x = a.get("rd", {}).get("x")
    rd_y = a.get("rd", {}).get("y")
    bag_pand_id = (scan_dict.get("woning") or {}).get("bag_pand_id", "")
    bag_vbo_id = a.get("bag_verblijfsobject_id") or ""
    gemeentecode = a.get("gemeentecode") or ""
    buurtcode = a.get("buurtcode") or ""

    t1 = _time.time()
    woz_t = orchestrator.fetch_woz_pand(bag_vbo_id) if bag_vbo_id else _aio.sleep(0, {"available": False})
    voorz_t = orchestrator.fetch_voorzieningen(lat=lat, lon=lon, buurtcode=buurtcode, gemeentecode=gemeentecode)
    klim_t = orchestrator.fetch_klimaat_section(lat, lon, rd_x, rd_y) if (lat and rd_x) else _aio.sleep(0, {"available": False})
    ber_t = orchestrator.fetch_bereikbaarheid_section(lat, lon) if lat else _aio.sleep(0, {"available": False})
    extras_t = orchestrator.fetch_woning_extras_section(lat, lon, rd_x, rd_y, gemeentecode or None) if lat else _aio.sleep(0, {"available": False})
    verb_t = orchestrator.fetch_verbouwing_section(
        lat=lat, lon=lon, rd_x=rd_x, rd_y=rd_y,
        bag_pand_id=bag_pand_id, gemeentecode=gemeentecode,
        gemeente_naam=None, eigen_vbo_id=bag_vbo_id,
    ) if lat else _aio.sleep(0, {"available": False})
    streetmap_t = static_maps.fetch_streetmap_png(lat, lon) if lat else _aio.sleep(0, None)
    perceel_t = static_maps.fetch_perceel_png(rd_x, rd_y) if rd_x else _aio.sleep(0, None)

    woz, voorz, klim, ber, extras, verb, streetmap_png, perceel_png = await _aio.gather(
        woz_t, voorz_t, klim_t, ber_t, extras_t, verb_t, streetmap_t, perceel_t,
    )
    timing["lazy_parallel"] = round(_time.time() - t1, 2)

    t2 = _time.time()
    data = {
        "scan": scan_dict, "woz": woz, "voorz": voorz, "klim": klim,
        "ber": ber, "extras": extras, "verb": verb,
        "streetmap_png": streetmap_png, "perceel_png": perceel_png,
    }
    html_str = rapport_template.render_html(data)
    timing["html_render"] = round(_time.time() - t2, 2)

    # Schrijf naar cache
    _DATA_CACHE[cache_key] = (_time.time(), data, html_str)
    return data, html_str, timing


@app.get("/rapport", response_class=HTMLResponse)
async def rapport_endpoint(
    q: str = Query(..., min_length=3, description="Adres-zoekterm"),
) -> HTMLResponse:
    """Print-ready HTML-rapport voor een adres (preview/print-mode)."""
    _data, html_str, timing = await _gather_rapport_data(q)
    response = HTMLResponse(content=html_str, status_code=200)
    response.headers["X-Timing"] = ",".join(f"{k}={v}s" for k, v in timing.items())
    return response


@app.get("/rapport.pdf")
async def rapport_pdf_endpoint(
    q: str = Query(..., min_length=3, description="Adres-zoekterm"),
) -> Response:
    """Server-side gerenderde PDF (Playwright + Chromium). Direct download.

    Cached 24u per (datum, adres) — tweede klik = instant.
    """
    cache_key = _cache_key(q)
    pdf_hit = _PDF_CACHE.get(cache_key)
    if pdf_hit and (_time.time() - pdf_hit[0]) < _PDF_TTL_S:
        # Cache-hit: instant return
        return Response(
            content=pdf_hit[1],
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{pdf_hit[2]}"',
                "X-Timing": f"from_pdf_cache={round(_time.time() - pdf_hit[0], 1)}s_ago",
            },
        )

    data, html_str, timing = await _gather_rapport_data(q)
    t_pdf = _time.time()
    try:
        pdf_bytes = await html_to_pdf.render_html_to_pdf(html_str)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PDF-render faalde: {e}") from e
    timing["pdf_render"] = round(_time.time() - t_pdf, 2)

    a = data["scan"].get("adres", {})
    label = (a.get("display_name") or "rapport").replace(",", "").replace(" ", "-")
    filename = f"Buurtscan-{label}.pdf"

    # Cache PDF voor dezelfde dag
    _PDF_CACHE[cache_key] = (_time.time(), pdf_bytes, filename)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Timing": ",".join(f"{k}={v}s" for k, v in timing.items()),
        },
    )


@app.on_event("startup")
async def _startup_warm_chromium():
    """Warm Chromium op tijdens app-start.

    Eerste PDF-render kost cold ~8s door browser-launch + 1ste pagina;
    daarna ~2s warm. Pre-warmen verschuift die 8s naar startup-tijd
    (Fly machine wakker maken duurt al een seconde of 5).
    """
    import asyncio as _aio
    async def _warm():
        try:
            # Render een lege HTML naar PDF om Chromium te starten + 1ste page
            await html_to_pdf.render_html_to_pdf(
                "<html><body><h1>warmup</h1></body></html>", timeout_ms=10_000)
            print("[startup] Chromium warm", flush=True)
        except Exception as e:
            print(f"[startup] Chromium warmup failed: {e}", flush=True)
    # Niet-blocking: laat startup snel afronden
    _aio.create_task(_warm())


@app.on_event("shutdown")
async def _shutdown_html_to_pdf():
    """Sluit Chromium netjes af."""
    try:
        await html_to_pdf.shutdown()
    except Exception:
        pass


@app.get("/woz")
async def woz_endpoint(
    bag_vbo_id: str = Query(..., description="BAG verblijfsobject-identificatie"),
) -> dict:
    """Pand-specifieke WOZ-waarde via WOZ-loket viewer-API.

    Rate-limited op 1/sec globaal om WOZ-loket niet te overvragen.
    Cache 365 dagen per BAG-id (WOZ verandert jaarlijks).
    """
    try:
        return await orchestrator.fetch_woz_pand(bag_vbo_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"WOZ faalde: {e}") from e


@app.get("/klimaat")
async def klimaat_endpoint(
    lat: float = Query(..., description="WGS84 latitude"),
    lon: float = Query(..., description="WGS84 longitude"),
    rd_x: float = Query(..., description="RD X-coordinaat"),
    rd_y: float = Query(..., description="RD Y-coordinaat"),
) -> dict:
    """Klimaatrisico bodem-aware (CAS — 8 sub-calls, 500-1500ms cold).

    Aparte endpoint omdat deze te langzaam is voor de main /scan flow.
    """
    try:
        return await orchestrator.fetch_klimaat_section(lat, lon, rd_x, rd_y)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Klimaat faalde: {e}") from e


@app.get("/bereikbaarheid")
async def bereikbaarheid_endpoint(
    lat: float = Query(..., description="WGS84 latitude"),
    lon: float = Query(..., description="WGS84 longitude"),
) -> dict:
    """Bereikbaarheid (Overpass route-relations + werkcentra, 2-5s cold)."""
    try:
        return await orchestrator.fetch_bereikbaarheid_section(lat, lon)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bereikbaarheid faalde: {e}") from e


@app.get("/verbouwing")
async def verbouwing_endpoint(
    lat: float = Query(..., description="WGS84 latitude"),
    lon: float = Query(..., description="WGS84 longitude"),
    rd_x: float = Query(..., description="RD X-coordinaat"),
    rd_y: float = Query(..., description="RD Y-coordinaat"),
    bag_pand_id: str = Query("", description="BAG pand-ID voor footprint"),
    gemeentecode: str = Query("", description="CBS gemeentecode (zonder GM)"),
    gemeente_naam: str = Query("", description="Gemeentenaam voor deeplink"),
    huisnummertoevoeging: str = Query("", description="Toevoeging (bv '1' voor 1e verdieping)"),
    vbo_id: str = Query("", description="BAG verblijfsobject-ID voor stapeling-analyse"),
) -> dict:
    """Verbouwingsmogelijkheden (Sectie 10)."""
    try:
        return await orchestrator.fetch_verbouwing_section(
            lat, lon, rd_x, rd_y,
            bag_pand_id or None,
            gemeentecode=gemeentecode or None,
            gemeente_naam=gemeente_naam or None,
            huisnummertoevoeging=huisnummertoevoeging or None,
            eigen_vbo_id=vbo_id or None,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Verbouwing faalde: {e}") from e


@app.get("/woning-extras")
async def woning_extras_endpoint(
    lat: float = Query(..., description="WGS84 latitude"),
    lon: float = Query(..., description="WGS84 longitude"),
    rd_x: float = Query(..., description="RD X-coordinaat"),
    rd_y: float = Query(..., description="RD Y-coordinaat"),
    gemeentecode: str = Query("", description="CBS-gemeentecode"),
) -> dict:
    """Woning-extras (Rijksmonument + Groen in straat, 500-1500ms cold).

    Aparte endpoint omdat RCE WFS + Overpass te langzaam zijn voor main /scan.
    """
    try:
        return await orchestrator.fetch_woning_extras_section(
            lat, lon, rd_x, rd_y, gemeentecode or None
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Woning-extras faalde: {e}") from e


@app.get("/voorzieningen")
async def voorzieningen_endpoint(
    lat: float = Query(..., description="WGS84 latitude van het adres"),
    lon: float = Query(..., description="WGS84 longitude van het adres"),
    buurtcode: str = Query("", description="CBS-buurtcode (voor CBS-fallback)"),
    gemeentecode: str = Query("", description="CBS-gemeentecode (voor CBS-fallback)"),
) -> dict:
    """Voorzieningen rond een adres (OSM POI's + CBS-fallback).

    Aparte endpoint omdat de Overpass-call duur is (3-6s cold). De frontend
    roept /scan eerst aan (snel), toont de hoofdpagina, en haalt vervolgens
    deze endpoint in de achtergrond op. Zo wacht de gebruiker niet op de
    trage voorzieningen-call voor ze iets te zien krijgen.
    """
    try:
        return await orchestrator.fetch_voorzieningen(
            lat=lat, lon=lon, buurtcode=buurtcode, gemeentecode=gemeentecode
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Voorzieningen faalde: {e}") from e


@app.get("/pand-geometry")
async def pand_geometry(pand_id: str = Query(..., min_length=10)) -> dict:
    """GeoJSON-geometrie van een BAG-pand (voor kaart-overlay).

    Aparte endpoint zodat de frontend deze pas ophaalt als de kaart
    daadwerkelijk gerenderd wordt — bespaart 1 WFS-call op elk /scan.
    """
    try:
        geom = await bag.fetch_pand_geometry(pand_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"BAG fout: {e}") from e
    if geom is None:
        raise HTTPException(status_code=404, detail="Pand niet gevonden")
    return {"pand_id": pand_id, "geometry": geom}


# ---------------------------------------------------------------------------
# Static frontend serving.
# De MVP-frontend is pure HTML+JS+CSS (geen build-step). FastAPI servt de
# map apps/web/ direct op "/" zodat alles op één origin draait en er geen
# CORS-gedoe is tussen gescheiden dev-servers.
# ---------------------------------------------------------------------------
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # Cache-headers strategie:
    # - index.html: short cache (1 uur), valideren met etag
    # - styles.css/app.js: 1 dag cache met must-revalidate
    # - robots.txt/sitemap.xml: 1 dag
    # Browsers krijgen 304 bij niet-veranderde files na deploys.
    _STATIC_CACHE = "public, max-age=86400, must-revalidate"   # 24u
    _HTML_CACHE = "public, max-age=3600, must-revalidate"      # 1u

    @app.get("/")
    async def serve_index() -> FileResponse:
        return FileResponse(
            WEB_DIR / "index.html",
            headers={"Cache-Control": _HTML_CACHE},
        )

    @app.get("/over")
    @app.get("/over-buurtscan")
    @app.get("/about")
    async def serve_over() -> FileResponse:
        """About-pagina — uitleg over Buurtscan, bronnen, doelgroep."""
        return FileResponse(
            WEB_DIR / "over.html",
            headers={"Cache-Control": _HTML_CACHE},
        )

    @app.get("/og-image.png")
    async def serve_og_image() -> FileResponse:
        """Open Graph share-image (1200×630). Gegenereerd door scripts/maak_og_image.py."""
        target = WEB_DIR / "og-image.png"
        if not target.exists():
            raise HTTPException(status_code=404, detail="og-image.png niet aanwezig")
        return FileResponse(
            target, media_type="image/png",
            headers={"Cache-Control": _STATIC_CACHE},
        )

    @app.get("/styles.css")
    async def serve_css() -> FileResponse:
        return FileResponse(
            WEB_DIR / "styles.css",
            headers={"Cache-Control": _STATIC_CACHE},
        )

    @app.get("/app.js")
    async def serve_js() -> FileResponse:
        return FileResponse(
            WEB_DIR / "app.js",
            headers={"Cache-Control": _STATIC_CACHE},
        )

    @app.get("/robots.txt")
    async def serve_robots() -> Response:
        """robots.txt — open voor indexering met sitemap-link."""
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /rapport\n"          # rapport-renders zijn dynamisch, geen SEO-waarde
            "Disallow: /rapport.pdf\n"
            "Disallow: /scan\n"             # JSON API
            "Disallow: /verbouwing\n"
            "Disallow: /klimaat\n"
            "Disallow: /voorzieningen\n"
            "Disallow: /bereikbaarheid\n"
            "Disallow: /woz\n"
            "Disallow: /lookup\n"
            "Disallow: /suggest\n"
            "Disallow: /pand-geometry\n"
            "Disallow: /woning-extras\n"
            "\n"
            "Sitemap: https://buurtscan.com/sitemap.xml\n"
        )
        return Response(
            content=body, media_type="text/plain",
            headers={"Cache-Control": _STATIC_CACHE},
        )

    @app.get("/sitemap.xml")
    async def serve_sitemap() -> Response:
        """Minimale sitemap — homepage + about-pagina (toekomstig)."""
        from datetime import date as _date
        today = _date.today().isoformat()
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            '  <url>\n'
            '    <loc>https://buurtscan.com/</loc>\n'
            f'    <lastmod>{today}</lastmod>\n'
            '    <changefreq>weekly</changefreq>\n'
            '    <priority>1.0</priority>\n'
            '  </url>\n'
            '  <url>\n'
            '    <loc>https://buurtscan.com/over</loc>\n'
            f'    <lastmod>{today}</lastmod>\n'
            '    <changefreq>monthly</changefreq>\n'
            '    <priority>0.7</priority>\n'
            '  </url>\n'
            '</urlset>\n'
        )
        return Response(
            content=body, media_type="application/xml",
            headers={"Cache-Control": _STATIC_CACHE},
        )

    @app.get("/config.js")
    async def serve_config():
        """Dynamische config-injectie.

        Volgorde:
          1. Als config.js lokaal bestaat (dev op laptop) → die serveren
          2. Anders: genereer JS on-the-fly uit env-vars (productie/Fly.io)
          3. Als ook env-vars ontbreken → leeg config.example.js

        Op Fly: zet `fly secrets set GOOGLE_MAPS_API_KEY=AIza...` en
        de frontend pikt 'm automatisch op zonder rebuild.
        """
        from fastapi.responses import Response
        target = WEB_DIR / "config.js"
        if target.exists():
            return FileResponse(target, media_type="application/javascript")
        gmaps_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
        api_base = os.environ.get("THUISSCAN_API_BASE", "")
        body = (
            "// Auto-gegenereerd door backend uit env-vars\n"
            f'window.THUISSCAN_API_BASE = {api_base!r};\n'
            f'window.GOOGLE_MAPS_API_KEY = {gmaps_key!r};\n'
        )
        return Response(content=body, media_type="application/javascript")
