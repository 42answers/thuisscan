"""
Static maps voor het PDF-rapport — OSM tile-stitcher + PDOK Kadaster WMS.

Twee functies:
  fetch_streetmap_png(lat, lon) -> bytes : OSM-tiles 3x2 gestitcht + groene marker
  fetch_perceel_png(rd_x, rd_y) -> bytes : Kadastrale Kaart WMS (perceel + bebouwing)

Beide returnen PNG-bytes; orchestrator embed ze als data-URL in de cover.
"""
from __future__ import annotations

import asyncio
import math
from io import BytesIO
from typing import Optional

import httpx

USER_AGENT = "Buurtscan/1.0 (vandeweijer@gmail.com)"
TIMEOUT_S = 10.0


def _deg2num(lat_deg: float, lon_deg: float, zoom: int) -> tuple[float, float]:
    """WGS84 → slippy-tile coords (float)."""
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = (lon_deg + 180.0) / 360.0 * n
    ytile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return xtile, ytile


async def fetch_streetmap_png(
    lat: float, lon: float, zoom: int = 17, w: int = 900, h: int = 420,
) -> Optional[bytes]:
    """OSM-tile stitch met centrale groene marker.

    Vereist Pillow voor stitching. Geeft None terug als PIL niet beschikbaar.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    cx, cy = _deg2num(lat, lon, zoom)
    cx_px = cx * 256
    cy_px = cy * 256
    px_left = cx_px - w / 2
    px_top = cy_px - h / 2
    tx_start = int(px_left // 256)
    ty_start = int(px_top // 256)
    tx_end = int((px_left + w) // 256) + 1
    ty_end = int((px_top + h) // 256) + 1

    canvas = Image.new("RGB", ((tx_end - tx_start) * 256, (ty_end - ty_start) * 256), "white")

    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:
        async def fetch_tile(tx, ty):
            try:
                r = await client.get(f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png")
                r.raise_for_status()
                tile = Image.open(BytesIO(r.content))
                return (tx, ty, tile)
            except Exception:
                return (tx, ty, None)

        tasks = [
            fetch_tile(tx, ty)
            for tx in range(tx_start, tx_end)
            for ty in range(ty_start, ty_end)
        ]
        for task in await asyncio.gather(*tasks):
            tx, ty, tile = task
            if tile is not None:
                canvas.paste(tile, ((tx - tx_start) * 256, (ty - ty_start) * 256))

    crop_left = px_left - tx_start * 256
    crop_top = px_top - ty_start * 256
    img = canvas.crop((int(crop_left), int(crop_top), int(crop_left + w), int(crop_top + h)))

    # Marker
    draw = ImageDraw.Draw(img)
    cx_img, cy_img = w // 2, h // 2
    draw.line([(cx_img, cy_img - 22), (cx_img, cy_img)], fill=(31, 69, 54), width=3)
    draw.ellipse([cx_img - 9, cy_img - 30, cx_img + 9, cy_img - 12],
                 fill=(31, 69, 54), outline="white", width=2)
    draw.ellipse([cx_img - 3, cy_img - 25, cx_img + 3, cy_img - 19], fill="white")

    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


async def fetch_perceel_png(
    rd_x: float, rd_y: float, delta: int = 80, w: int = 900, h: int = 420,
) -> Optional[bytes]:
    """PDOK Kadastrale Kaart WMS — perceel + bebouwing + straatnaam-labels."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    bbox = f"{rd_x - delta},{rd_y - delta // 2},{rd_x + delta},{rd_y + delta // 2}"
    url = (
        "https://service.pdok.nl/kadaster/kadastralekaart/wms/v5_0?"
        "REQUEST=GetMap&SERVICE=WMS&VERSION=1.3.0&"
        "LAYERS=Bebouwing,Perceel,OpenbareRuimteNaam&"
        "CRS=EPSG:28992&"
        f"BBOX={bbox}&WIDTH={w}&HEIGHT={h}&"
        "FORMAT=image/png&STYLES=&TRANSPARENT=false"
    )
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(url)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        return None

    # Centerpoint marker
    draw = ImageDraw.Draw(img)
    cx, cy = w // 2, h // 2
    draw.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], outline=(31, 69, 54), width=3)
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(31, 69, 54))

    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
