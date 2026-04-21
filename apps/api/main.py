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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from adapters import bag, pdok_locatie
import orchestrator

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
    """Volledige Thuisscan voor een adres: 6 secties + voorzieningen-ringen.

    Hieronder orchestreert parallel PDOK + BAG + CBS. Secties 4/5/6
    (veiligheid, leefkwaliteit, klimaat) zijn nog placeholder — die komen
    in fase 3/4.
    """
    try:
        result = await orchestrator.scan(q)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scan faalde: {e}") from e
    return orchestrator.result_as_dict(result)


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

    @app.get("/")
    async def serve_index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/styles.css")
    async def serve_css() -> FileResponse:
        return FileResponse(WEB_DIR / "styles.css")

    @app.get("/app.js")
    async def serve_js() -> FileResponse:
        return FileResponse(WEB_DIR / "app.js")

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
