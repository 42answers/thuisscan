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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from adapters import bag, pdok_locatie, static_maps, html_to_pdf, analytics
from adapters import payments_db, mollie, email_sender
import orchestrator
import rapport_template

app = FastAPI(
    title="Thuisscan API",
    version="0.1.0",
    description="Eén adres -> volledig woning- en buurtprofiel uit NL open data.",
)

# ===== Middleware stack (volgorde: eerst toegevoegd = buitenste laag) =====
# 1. GZip: 70% kleinere responses voor HTML/JSON/CSS/JS. Min 1KB om te
#    compresseren (kleinere assets niet nodig).
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 2. CORS: strict in productie. Alleen onze domeinen + localhost voor dev.
_CORS_ALLOWED = [
    "https://buurtscan.com",
    "https://www.buurtscan.com",
    "https://buurtscan.fly.dev",
    "http://localhost:8000",
    "http://localhost:8765",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ALLOWED,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# 3. Security headers — één middleware die alle responses verrijkt.
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Voeg veiligheids-headers toe aan elke response.

    - HSTS: browser forceert HTTPS voor 1 jaar (subdomains included).
    - X-Content-Type-Options nosniff: voorkomt MIME-sniffing.
    - X-Frame-Options DENY: voorkomt clickjacking via iframe.
    - Referrer-Policy: beperkt wat we in Referer-header doorsturen.
    - Permissions-Policy: schakelt camera/mic/geolocation expliciet uit.
    - CSP: basis policy; staat scripts toe van self + unpkg (MapLibre),
      images van self + data: (inline favicon + kaart-overlays) + PDOK/OSM
      tiles, fonts van Google Fonts.
    """
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)")
        # CSP: alleen toevoegen voor HTML-responses, niet voor API/JSON/PDF
        # (zou onze eigen PDF-render kunnen breken als scripts opengelaten
        # worden op het wrong content-type).
        ct = h.get("content-type", "")
        if ct.startswith("text/html"):
            h.setdefault("Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' https://unpkg.com https://maps.googleapis.com 'unsafe-inline'; "
                "style-src 'self' https://unpkg.com https://fonts.googleapis.com 'unsafe-inline'; "
                "img-src 'self' data: blob: https://*.openstreetmap.org https://*.pdok.nl "
                    "https://maps.googleapis.com https://maps.gstatic.com https://*.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com data:; "
                "connect-src 'self' https://api.pdok.nl https://*.openstreetmap.org "
                    "https://service.pdok.nl https://*.overheid.nl https://service.omgevingswet.overheid.nl "
                    "https://maps.googleapis.com; "
                "frame-src https://www.google.com; "  # Google Maps embed
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
        return response

app.add_middleware(SecurityHeadersMiddleware)


# ===== Rate-limiting =====
# Simpele in-memory rate-limit per IP per endpoint. Bewust geen Redis:
# single-machine Fly-deploy + sliding-window in een dict is voldoende.
# Doel: bescherming tegen scrapers / per-ongelukte refresh-loops.
#   /rapport.pdf → 10/uur (PDF-gen kost ~$0,50 aan CPU per piek)
#   /scan        → 60/min (snelle JSON-call)
#   default      → 300/min
import time as _rl_time
from collections import defaultdict as _defaultdict

_rate_windows: dict[str, list[float]] = _defaultdict(list)

_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/rapport.pdf": (10, 3600),   # 10 per uur
    "/rapport":     (30, 3600),   # 30 per uur
    "/scan":        (60, 60),     # 60 per minuut
}
_RATE_DEFAULT = (300, 60)          # 300 per minuut voor alle andere GETs


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Sla rate-limit over voor: static assets, health, OPTIONS, suggest
        path = request.url.path
        if (
            path.startswith("/static")
            or path in ("/health", "/favicon.ico", "/robots.txt", "/sitemap.xml",
                        "/og-image.png", "/config.js", "/styles.css", "/app.js",
                        "/", "/over", "/about", "/over-buurtscan")
            or request.method == "OPTIONS"
        ):
            return await call_next(request)

        limit, window_s = _RATE_LIMITS.get(path, _RATE_DEFAULT)
        # Client-IP — Fly.io zet X-Forwarded-For
        ip = request.headers.get("fly-client-ip") \
             or request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
             or (request.client.host if request.client else "unknown")
        key = f"{ip}:{path}"
        now = _rl_time.time()
        # Slim: prune oude entries inline
        window = _rate_windows[key]
        cutoff = now - window_s
        # Efficient: remove from front (timestamps zijn oplopend)
        while window and window[0] < cutoff:
            window.pop(0)
        if len(window) >= limit:
            retry = int(window[0] + window_s - now) + 1
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit: {limit} requests per {window_s}s. Retry in {retry}s.",
                },
                headers={
                    "Retry-After": str(retry),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
        window.append(now)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(limit - len(window))
        return response

app.add_middleware(RateLimitMiddleware)


@app.get("/health")
async def health() -> dict:
    """Lightweight health-check voor Fly.io load-balancer (elke 30s)."""
    return {"status": "ok"}


@app.get("/health/full")
async def health_full(token: str = Query("", description="ADMIN_TOKEN voor diepte-check")) -> dict:
    """Diepte-monitoring voor admin: check alle externe deps + endpoint-tijden.

    Beschermd met ADMIN_TOKEN omdat het externe API-calls triggert.
    Geschikt voor uptime-services zoals Better Stack / UptimeRobot
    (configureer met header X-Token of query ?token=).
    """
    admin_token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN niet gezet")
    if token != admin_token:
        raise HTTPException(status_code=401, detail="Ongeldig token")

    import asyncio as _aio, time as _t
    import httpx
    checks: dict = {"timestamp": _t.time()}

    async def _check(name, coro):
        t0 = _t.time()
        try:
            await coro
            checks[name] = {"ok": True, "ms": int((_t.time() - t0) * 1000)}
        except Exception as e:
            checks[name] = {"ok": False, "ms": int((_t.time() - t0) * 1000), "error": str(e)[:100]}

    async def ping(url):
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(url)
            r.raise_for_status()

    await _aio.gather(
        _check("pdok_locatieserver", ping("https://api.pdok.nl/bzk/locatieserver/search/v3_1/free?q=damrak&rows=1")),
        _check("pdok_bag_wfs", ping("https://service.pdok.nl/lv/bag/wfs/v2_0?REQUEST=GetCapabilities&SERVICE=WFS")),
        _check("cbs_kerncijfers", ping("https://opendata.cbs.nl/ODataApi/odata/85984NED?$top=1")),
        _check("rivm_alo", ping("https://geodata.rivm.nl/geoserver/wms?REQUEST=GetCapabilities&SERVICE=WMS")),
        _check("politie_open_data", ping("https://opendata.cbs.nl/ODataApi/odata/47022NED?$top=1")),
        _check("kadaster_woz", ping("https://api.kadaster.nl/lvwoz/wozwaardeloket-api/v1")),
        _check("openstreetmap_overpass", ping("https://overpass-api.de/api/status")),
        _check("klimaateffectatlas", ping("https://services1.arcgis.com/HyT4U7EQLPYkQyhT/arcgis/rest/services?f=json")),
        return_exceptions=True,
    )
    overall_ok = all(c.get("ok") for c in checks.values() if isinstance(c, dict) and "ok" in c)
    return {"status": "ok" if overall_ok else "degraded", "checks": checks}


@app.get("/health/uptime")
async def health_uptime() -> dict:
    """Publieke status-check (geen token, geen externe calls).

    Voor Better Stack / UptimeRobot status-page als basic ping.
    Returnt info zonder externe afhankelijkheden — altijd snel.
    """
    import time as _t
    return {
        "status": "ok",
        "service": "buurtscan",
        "version": "0.1.0",
        "timestamp": _t.time(),
        "uptime_hint": "200 = service draait; 5xx = down",
    }


# ===== Analytics =====
@app.get("/track")
async def track_event(
    event: str = Query(..., description="page_load, scan, pdf_download, preview, over_view"),
    request: Request = None,
) -> Response:
    """Log een event zonder cookies of IP. Called via navigator.sendBeacon.

    Elk event wordt anoniem geaggregeerd naar apps/api/cache/analytics.jsonl.
    Return 204 No Content zodat sendBeacon tevreden is + geen response-body.
    """
    referer = request.headers.get("referer") if request else None
    path = None
    if referer:
        try:
            from urllib.parse import urlparse
            path = urlparse(referer).path
        except Exception:
            path = None
    analytics.track(event=event, host=(request.headers.get("host") if request else None), path=path)
    # 1 op 1000: rotate if needed
    import random as _rand
    if _rand.random() < 0.001:
        analytics.rotate_if_needed()
    return Response(status_code=204)


@app.get("/stats")
async def stats_endpoint(
    token: str = Query("", description="Admin-token uit env ADMIN_TOKEN"),
) -> dict:
    """Basic analytics-dashboard (JSON). Beveiligd met ADMIN_TOKEN."""
    admin_token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN niet gezet op de server")
    if token != admin_token:
        raise HTTPException(status_code=401, detail="Ongeldig token")
    return analytics.load_summary()


@app.get("/admin/sales", response_class=HTMLResponse)
async def admin_sales_dashboard(
    token: str = Query("", description="Admin-token uit env ADMIN_TOKEN"),
    fmt: str = Query("html", regex="^(html|json)$",
                     description="'html' (browser-friendly) of 'json' (API/scripts)"),
) -> Response:
    """Verkoop-dashboard: omzet, recente paid, top adressen, status-breakdown.

    Beveiligd met dezelfde ADMIN_TOKEN als /stats. Twee output-formats:
      - html (default): direct in browser bekijken
      - json:           voor scripts/automation
    """
    admin_token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN niet gezet op de server")
    if not token or token != admin_token:
        raise HTTPException(status_code=401, detail="Ongeldig of ontbrekend token")

    summary = payments_db.stats_summary()
    test_mode = mollie.is_test_mode() if mollie.is_configured() else None
    mollie_state = (
        "TEST-mode (geen echt geld)" if test_mode is True
        else "LIVE-mode (echt geld!)" if test_mode is False
        else "niet geconfigureerd"
    )

    if fmt == "json":
        return _json_response({
            "summary": summary,
            "mollie_state": mollie_state,
        })

    return HTMLResponse(content=_admin_sales_html(summary, mollie_state))


def _json_response(payload: dict) -> Response:
    """Kleine helper — sommige hosts schrijven JSON met escapes die we
    niet willen in een dashboard-context. Hier kort en zonder ASCII-escape."""
    import json as _json
    return Response(
        content=_json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
    )


def _admin_sales_html(summary: dict, mollie_state: str) -> str:
    """Render een minimal-fuss HTML-dashboard. Geen externe assets, alles inline."""
    import html as _h
    per_status = summary.get("per_status", {}) or {}
    omzet = summary.get("omzet_eur", 0) or 0
    totaal = summary.get("totaal_betaald", 0) or 0
    top = summary.get("top_adressen", []) or []
    recent = summary.get("recent", []) or []

    # Status-breakdown rows
    status_rows = []
    for status_name in ("paid", "pending", "refunded", "expired"):
        s = per_status.get(status_name, {"n": 0, "cents_total": 0})
        status_rows.append(
            f"<tr><td>{status_name}</td><td>{s.get('n', 0)}</td>"
            f"<td>€&nbsp;{(s.get('cents_total', 0) or 0) / 100:.2f}</td></tr>"
        )
    # Catch-all voor andere statussen die we niet expliciet noemen
    for status_name, s in per_status.items():
        if status_name not in ("paid", "pending", "refunded", "expired"):
            status_rows.append(
                f"<tr><td>{_h.escape(status_name)}</td><td>{s.get('n', 0)}</td>"
                f"<td>€&nbsp;{(s.get('cents_total', 0) or 0) / 100:.2f}</td></tr>"
            )
    status_html = "".join(status_rows) or '<tr><td colspan="3"><em>geen data</em></td></tr>'

    top_html = "".join(
        f"<tr><td>{_h.escape(t.get('adres', ''))}</td><td>{t.get('n', 0)}×</td></tr>"
        for t in top
    ) or '<tr><td colspan="2"><em>nog niets verkocht</em></td></tr>'

    recent_html = "".join(
        f"<tr>"
        f"<td><code>{_h.escape(r.get('token_prefix', ''))}</code></td>"
        f"<td>{_h.escape(r.get('adres', ''))}</td>"
        f"<td>{_h.escape(r.get('email', '—'))}</td>"
        f"<td>€&nbsp;{(r.get('eur', 0) or 0):.2f}</td>"
        f"<td>{_h.escape((r.get('paid_at') or '')[:16].replace('T', ' '))}</td>"
        f"<td>{r.get('downloads', 0)}×</td>"
        f"<td>{_h.escape((r.get('valid_until') or '')[:10])}</td>"
        f"</tr>"
        for r in recent
    ) or '<tr><td colspan="7"><em>nog geen betaalde rapporten</em></td></tr>'

    mollie_class = (
        "badge-warn" if "LIVE" in mollie_state
        else "badge-ok" if "TEST" in mollie_state
        else "badge-neutral"
    )

    # Pre-compute KPI-waardes — vermijdt complexe expressies in f-string
    pending_n  = (per_status.get("pending")  or {}).get("n", 0)
    refunded_n = (per_status.get("refunded") or {}).get("n", 0)
    admin_token_for_link = os.environ.get("ADMIN_TOKEN", "").strip()

    return f"""<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8"><title>Sales-dashboard · Buurtscan admin</title>
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  background: #fafaf7; color: #1a1a1a; line-height: 1.5;
  padding: 2rem 1.5rem; max-width: 1100px; margin: 0 auto;
}}
h1 {{
  font-size: 1.6rem; font-weight: 600;
  letter-spacing: -0.01em; margin-bottom: 0.4rem;
}}
.sub {{ color: #6b6b6b; font-size: 0.9rem; margin-bottom: 1.6rem; }}
.badge {{
  display: inline-block; padding: 0.18rem 0.55rem;
  border-radius: 3px; font-size: 0.75rem;
  font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
}}
.badge-ok {{ background: #e4f4eb; color: #1d7a56; }}
.badge-warn {{ background: #fbe8e1; color: #a14a3a; }}
.badge-neutral {{ background: #ececec; color: #5d5d5d; }}
.kpis {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 0.8rem; margin: 1.5rem 0 2rem;
}}
.kpi {{
  background: #fff; border: 1px solid #e4e4e4; border-radius: 10px;
  padding: 1rem 1.2rem;
}}
.kpi .label {{
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
  color: #6b6b6b; margin-bottom: 0.3rem;
}}
.kpi .value {{
  font-size: 1.6rem; font-weight: 600; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}}
section {{
  background: #fff; border: 1px solid #e4e4e4; border-radius: 10px;
  padding: 1.2rem 1.4rem; margin-bottom: 1.2rem;
}}
section h2 {{
  font-size: 1.05rem; font-weight: 600; margin-bottom: 0.8rem;
}}
table {{
  width: 100%; border-collapse: collapse;
  font-size: 0.92rem;
}}
th, td {{
  padding: 0.5rem 0.7rem; text-align: left;
  border-bottom: 1px solid #f0f0ee;
  vertical-align: top;
}}
tr:last-child td {{ border-bottom: none; }}
th {{
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: #6b6b6b; font-weight: 600; background: #fafaf7;
}}
td code {{
  font-family: "SF Mono", Menlo, monospace; font-size: 0.82rem;
  background: #f3f3f0; padding: 0.1rem 0.4rem; border-radius: 3px;
}}
.muted {{ color: #6b6b6b; font-size: 0.85rem; }}
em {{ color: #6b6b6b; font-style: normal; }}
.refresh-hint {{
  font-size: 0.78rem; color: #8a8a8a; margin-top: 1.4rem; text-align: center;
}}
@media (max-width: 700px) {{
  .recent-table {{ overflow-x: auto; }}
  .recent-table table {{ min-width: 720px; }}
}}
</style>
</head><body>

<h1>Sales-dashboard <span class="badge {mollie_class}">{_h.escape(mollie_state)}</span></h1>
<p class="sub">Realtime cijfers uit <code>payments_db</code> · alleen jij ziet dit (token-beveiligd).</p>

<div class="kpis">
  <div class="kpi">
    <div class="label">Omzet (paid)</div>
    <div class="value">€&nbsp;{omzet:.2f}</div>
  </div>
  <div class="kpi">
    <div class="label">Aantal betaald</div>
    <div class="value">{totaal}</div>
  </div>
  <div class="kpi">
    <div class="label">Pending</div>
    <div class="value">{pending_n}</div>
  </div>
  <div class="kpi">
    <div class="label">Refunded</div>
    <div class="value">{refunded_n}</div>
  </div>
</div>

<section>
  <h2>Status-breakdown</h2>
  <table>
    <thead><tr><th>Status</th><th>Aantal</th><th>Som (€)</th></tr></thead>
    <tbody>{status_html}</tbody>
  </table>
</section>

<section>
  <h2>Top-10 meest verkochte adressen</h2>
  <table>
    <thead><tr><th>Adres</th><th>Aantal</th></tr></thead>
    <tbody>{top_html}</tbody>
  </table>
</section>

<section class="recent-table">
  <h2>Laatste 20 betaalde rapporten</h2>
  <table>
    <thead><tr>
      <th>Token</th><th>Adres</th><th>E-mail</th><th>€</th>
      <th>Paid at</th><th>Downloads</th><th>Valid until</th>
    </tr></thead>
    <tbody>{recent_html}</tbody>
  </table>
</section>

<p class="refresh-hint">
  Refresh deze pagina voor verse cijfers ·
  <a href="?token={_h.escape(admin_token_for_link)}&amp;fmt=json"
     style="color:#1f4536">JSON-versie</a>
</p>

</body></html>"""


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
async def _startup_init_db():
    """Maak SQLite payments-tabel aan als nog niet bestaat."""
    try:
        payments_db.init_db()
        print("[startup] payments DB ready", flush=True)
    except Exception as e:
        print(f"[startup] payments DB init failed: {e}", flush=True)


# ===============================================================
# PAYMENT FLOW (Mollie + magic-link)
# ===============================================================
from pydantic import BaseModel as _BaseModel, EmailStr as _EmailStr

class CheckoutRequest(_BaseModel):
    adres: str
    email: _EmailStr


def _public_base_url(request: Request) -> str:
    """Bouw publieke URL op basis van request — gebruikt voor Mollie redirects."""
    # Als achter Cloudflare/Fly proxy: prefereer X-Forwarded-Proto/Host
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    # Productie: forceer https + canonical buurtscan.com
    if "buurtscan" in host:
        return "https://buurtscan.com"
    return f"{proto}://{host}"


@app.post("/checkout")
async def checkout_endpoint(payload: CheckoutRequest, request: Request) -> dict:
    """Start een Mollie payment voor één rapport.

    Flow:
      1. Frontend POST {adres, email} hier
      2. Wij maken pending-rij in DB → krijgen token
      3. Mollie create_payment met onze callback URL + token in metadata
      4. Returnen checkout_url naar frontend → user redirect
    """
    if not mollie.is_configured():
        raise HTTPException(status_code=503,
            detail="Betaal-provider niet ingesteld (MOLLIE_API_KEY ontbreekt)")

    # IP voor abuse-detect (alleen hash opgeslagen, geen IP zelf)
    ip = (request.headers.get("fly-client-ip")
          or request.headers.get("cf-connecting-ip")
          or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else None))

    # 1. Pending-rij + token
    token = payments_db.create_pending(
        adres_query=payload.adres,
        email=payload.email,
        ip=ip,
        amount_cents=499,
    )

    # 2. Mollie payment
    base = _public_base_url(request)
    redirect = f"{base}/r/{token}/wachtkamer"  # Mollie stuurt user hierheen na betaling
    webhook = f"{base}/checkout/webhook"
    payment = await mollie.create_payment(
        amount_cents=499,
        description=f"Buurtscan rapport - {payload.adres[:80]}",
        redirect_url=redirect,
        webhook_url=webhook,
        metadata={"token": token, "adres": payload.adres[:120]},
    )
    if not payment:
        raise HTTPException(status_code=502, detail="Kon Mollie-betaling niet starten")

    # 3. Koppel payment-id aan onze token
    payments_db.attach_mollie_payment(token, payment["id"])

    # Analytics: checkout-start
    analytics.track(event="checkout_start", path=request.url.path)

    return {
        "ok": True,
        "checkout_url": payment["checkout_url"],
        "token": token,
        "test_mode": mollie.is_test_mode(),
    }


@app.post("/checkout/webhook")
async def checkout_webhook(request: Request) -> Response:
    """Mollie webhook — POST met payment-id in form-body.

    Mollie roept ons aan na elke status-wijziging. Wij MOETEN bij Mollie
    de actuele status opvragen (kunnen niet vertrouwen op webhook-body).
    """
    form = await request.form()
    payment_id = form.get("id")
    if not payment_id:
        return Response(status_code=400, content="Missing id")

    payment = await mollie.get_payment(payment_id)
    if not payment:
        return Response(status_code=200, content="ok")  # negeer onbekende payments

    status = payment.get("status")
    if status == "paid":
        # Markeer betaald in DB
        row = payments_db.mark_paid(payment_id)
        if row:
            # Stuur magic-link mail
            base = _public_base_url(request)
            magic_url = f"{base}/r/{row['token']}"
            await email_sender.send_magic_link(
                to_email=row["email"],
                adres=row["adres_query"],
                magic_url=magic_url,
                valid_until=row["valid_until"],
                bedrag_eur=row["amount_cents"] / 100,
            )
            analytics.track(event="payment_paid")
    elif status in ("expired", "failed", "canceled"):
        payments_db.mark_failed(payment_id, status)
        analytics.track(event=f"payment_{status}")

    # Mollie verwacht 200 als ack
    return Response(status_code=200, content="ok")


@app.get("/r/{token}/wachtkamer")
async def magic_link_wachtkamer(token: str) -> HTMLResponse:
    """Landing-pagina ná Mollie checkout, vóór de mail binnen is.

    Mollie stuurt user hier ongeacht uitkomst. Wij checken status:
    - pending → 'Wacht op bevestiging…' met auto-refresh
    - paid → redirect naar /r/<token>
    - expired/failed → 'Betaling niet doorgegaan'
    """
    valid, reason, row = payments_db.is_valid(token)
    if valid:
        # Direct doorsturen naar het rapport
        return HTMLResponse(
            content=f'<meta http-equiv="refresh" content="0; url=/r/{token}">'
                    f'<p>Betaling bevestigd! Doorsturen…</p>',
            status_code=200,
        )
    if reason == "pending":
        # Mollie heeft ons nog niet gewebhookt, maar user is er al
        return HTMLResponse(content=_wachtkamer_html(token, row), status_code=200)
    # Failed / expired
    return HTMLResponse(content=_betaling_mislukt_html(reason), status_code=200)


@app.get("/r/{token}", response_class=HTMLResponse)
async def magic_link_view(token: str, request: Request) -> HTMLResponse:
    """Magic-link → toon volledig rapport als token geldig."""
    valid, reason, row = payments_db.is_valid(token)
    if not valid:
        return HTMLResponse(content=_token_error_html(reason), status_code=403)

    payments_db.increment_download(token)
    analytics.track(event="report_view")

    # Genereer rapport (cached 15 min — als email-mailing en click binnen die
    # tijd zit, is alles instant). Hergebruik _gather_rapport_data.
    _data, html_str, _timing = await _gather_rapport_data(row["adres_query"])
    # Inject paid-flag zodat app.js _IS_PAID_VIEW true is en geen paywall
    # laat zien + de PDF-download-knop met token toont.
    html_str = _inject_paid_flag(html_str, token)
    return HTMLResponse(content=html_str)


def _inject_paid_flag(html: str, token: str) -> str:
    """Plak een <script>-tag in <head> die window.__buurtscan_paid + token zet.

    Token is URL-safe base64 (alleen [A-Za-z0-9_-]), maar we json-escapen
    voor zekerheid. Idempotent: roept geen kwaad als <head> ontbreekt
    (dan blijft HTML ongewijzigd — frontend valt terug op default OFF-flag).
    """
    import json as _json
    snippet = (
        f'<script>'
        f'window.__buurtscan_paid=true;'
        f'window.__buurtscan_token={_json.dumps(token)};'
        f'</script>'
    )
    # Zoek case-insensitief naar </head> en injecteer er net vóór.
    lower = html.lower()
    idx = lower.rfind("</head>")
    if idx == -1:
        # Geen </head>? Probeer na <head> (open-tag).
        open_idx = lower.find("<head>")
        if open_idx == -1:
            return html  # geen head, frontend werkt zonder flag (OFF)
        insert_at = open_idx + len("<head>")
        return html[:insert_at] + snippet + html[insert_at:]
    return html[:idx] + snippet + html[idx:]


@app.get("/r/{token}/pdf")
async def magic_link_pdf(token: str) -> Response:
    """Magic-link PDF-download. Onbeperkt binnen 7 dagen."""
    valid, reason, row = payments_db.is_valid(token)
    if not valid:
        return Response(status_code=403, content=_token_error_html(reason),
                        media_type="text/html")

    payments_db.increment_download(token)
    analytics.track(event="report_pdf")

    # Aparte cache-namespace 'paid:' zodat free /rapport.pdf en paid /r/.../pdf
    # niet door elkaar lopen — anders zou een eerder gecached free-PDF (zonder
    # paid-flag in HTML) een paid-user kunnen serveren met blur erin zodra de
    # globale paywall-flag straks aan staat.
    cache_key = "paid:" + _cache_key(row["adres_query"])
    pdf_hit = _PDF_CACHE.get(cache_key)
    if pdf_hit and (_time.time() - pdf_hit[0]) < _PDF_TTL_S:
        return Response(
            content=pdf_hit[1],
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{pdf_hit[2]}"'},
        )

    data, html_str, _timing = await _gather_rapport_data(row["adres_query"])
    # Zelfde paid-flag injectie als bij /r/{token} — anders blurt applyPaywall
    # in de Playwright-render zodra de globale flag straks aan gaat.
    html_str = _inject_paid_flag(html_str, token)
    pdf_bytes = await html_to_pdf.render_html_to_pdf(html_str)
    a = data["scan"].get("adres", {})
    label = (a.get("display_name") or "rapport").replace(",", "").replace(" ", "-")
    filename = f"Buurtscan-{label}.pdf"
    _PDF_CACHE[cache_key] = (_time.time(), pdf_bytes, filename)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _wachtkamer_html(token: str, row: Optional[dict]) -> str:
    adres = (row or {}).get("adres_query", "je adres")
    return f"""<!DOCTYPE html><html lang="nl"><head><meta charset="utf-8">
<title>Betaling wordt verwerkt - Buurtscan</title>
<meta http-equiv="refresh" content="5; url=/r/{token}/wachtkamer">
<style>body{{font-family:-apple-system,Helvetica,sans-serif;max-width:520px;margin:6rem auto;padding:0 1.5rem;text-align:center;color:#1a1a1a}}
h1{{font-family:Georgia,serif;font-size:2rem;letter-spacing:-0.01em;font-weight:400}}
em{{color:#1f4536}}
.spinner{{width:32px;height:32px;border:3px solid #e8e6e0;border-top-color:#1f4536;border-radius:50%;animation:spin 1s linear infinite;margin:2rem auto}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}</style></head>
<body><div class="spinner"></div><h1>Even <em>wachten</em>…</h1>
<p>We verwerken je betaling voor <strong>{adres}</strong>.</p>
<p style="color:#6b6b6b;font-size:.9rem">Dit duurt meestal 5-15 seconden.<br>
Je krijgt zo automatisch je rapport te zien én een mail met de link.</p></body></html>"""


def _betaling_mislukt_html(reason: str) -> str:
    return f"""<!DOCTYPE html><html lang="nl"><head><meta charset="utf-8">
<title>Betaling niet doorgegaan - Buurtscan</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:520px;margin:6rem auto;padding:0 1.5rem;text-align:center;color:#1a1a1a}}
h1{{font-family:Georgia,serif;font-size:2rem;letter-spacing:-0.01em;font-weight:400}}
em{{color:#a14a3a}}
a.btn{{display:inline-block;margin-top:1.5rem;padding:.75rem 1.5rem;background:#1f4536;color:#fff;border-radius:6px;text-decoration:none;font-weight:500}}</style></head>
<body><h1>Betaling <em>niet doorgegaan</em></h1>
<p>Status: {reason}. Er is geen geld afgeschreven.</p>
<a href="/" class="btn">← Terug naar Buurtscan</a></body></html>"""


def _token_error_html(reason: Optional[str]) -> str:
    titles = {
        "unknown": "Link onbekend",
        "pending": "Betaling nog niet bevestigd",
        "expired_time": "Link verlopen",
        "expired_status": "Link niet langer geldig",
        "refunded": "Aankoop is gerefund",
    }
    msgs = {
        "unknown": "Deze link bestaat niet (meer). Check de link in de e-mail.",
        "pending": "We wachten nog op bevestiging van Mollie. Probeer over een paar seconden opnieuw.",
        "expired_time": "Je 7-daagse toegangsperiode is voorbij. Koop een nieuw rapport om opnieuw toegang te krijgen.",
        "expired_status": "Deze toegang is ingetrokken. Mail redactie@buurtscan.nl als je denkt dat dit fout is.",
        "refunded": "Voor deze bestelling is geld teruggestort. Toegang is daarmee vervallen.",
    }
    title = titles.get(reason or "", "Geen toegang")
    msg = msgs.get(reason or "", "Onbekende status.")
    return f"""<!DOCTYPE html><html lang="nl"><head><meta charset="utf-8">
<title>{title} - Buurtscan</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:520px;margin:6rem auto;padding:0 1.5rem;text-align:center;color:#1a1a1a}}
h1{{font-family:Georgia,serif;font-size:2rem;letter-spacing:-0.01em;font-weight:400}}
em{{color:#a14a3a}}
a.btn{{display:inline-block;margin-top:1.5rem;padding:.75rem 1.5rem;background:#1f4536;color:#fff;border-radius:6px;text-decoration:none;font-weight:500}}</style></head>
<body><h1>{title}</h1><p>{msg}</p>
<a href="/" class="btn">→ Naar Buurtscan</a></body></html>"""


# ===============================================================


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

    @app.get("/manifest.json")
    async def serve_manifest() -> FileResponse:
        """PWA manifest — browsers gebruiken dit voor 'Toevoegen aan startscherm'."""
        return FileResponse(
            WEB_DIR / "manifest.json",
            media_type="application/manifest+json",
            headers={"Cache-Control": _STATIC_CACHE},
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


# ===== Custom 404 handler =====
# FastAPI's default 404 is een JSON-response. Voor HTML-paden (waar user
# direct naartoe navigeerde via link/typefout) tonen we onze mooie 404.html.
# Voor API-paden (/scan, /rapport, /woz etc) behouden we JSON.
_API_PREFIXES = (
    "/scan", "/suggest", "/lookup", "/woz", "/klimaat", "/bereikbaarheid",
    "/verbouwing", "/voorzieningen", "/woning-extras", "/pand-geometry",
    "/rapport", "/rapport.pdf", "/track", "/stats", "/health",
)


@app.exception_handler(404)
async def custom_404_handler(request, exc):
    """Toon 404.html voor page-navigation, JSON voor API-requests."""
    path = request.url.path
    # API-paden → JSON zoals gewoonlijk
    if any(path.startswith(p) for p in _API_PREFIXES):
        return JSONResponse(
            status_code=404,
            content={"detail": exc.detail if hasattr(exc, "detail") else "Not found"},
        )
    # Page-navigation → HTML
    target = WEB_DIR / "404.html"
    if target.exists():
        return FileResponse(target, status_code=404, headers={"Cache-Control": "no-cache"})
    # Fallback als 404.html onverhoopt weg is
    return HTMLResponse("<h1>404 — Pagina niet gevonden</h1>", status_code=404)
