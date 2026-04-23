"""
HTML → PDF rendering via Playwright headless Chromium.

Browser-instance wordt 1× opgestart bij module-import en gerecycled tussen
requests (warme browser = ~2s per PDF i.p.v. ~8s cold-start).

Dockerfile installeert Chromium via:
    RUN python -m playwright install --with-deps chromium

Lokaal voor testen:
    pip install playwright
    python -m playwright install chromium
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional

# Playwright is een runtime-dependency die we lazy importeren zodat unit-
# tests + dev-environment zonder Chromium nog steeds andere endpoints kunnen
# laden.
_browser = None
_playwright_ctx = None
_lock = asyncio.Lock()


async def _ensure_browser():
    """Start een gedeelde Chromium als die nog niet draait."""
    global _browser, _playwright_ctx
    if _browser is not None:
        return _browser
    async with _lock:
        if _browser is not None:
            return _browser
        from playwright.async_api import async_playwright
        _playwright_ctx = await async_playwright().start()
        _browser = await _playwright_ctx.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        print("[html_to_pdf] Chromium gestart", file=sys.stderr)
    return _browser


async def render_html_to_pdf(html: str, *, timeout_ms: int = 30_000) -> bytes:
    """Render een complete HTML-string naar PDF-bytes (A4, geen marges).

    Gebruikt page.set_content + page.pdf — geen netwerk-fetch nodig voor
    de HTML zelf. CSS @page rules in de HTML bepalen de layout.

    Voor onze rapport-template betekent dit:
      - A4 portrait
      - Geen extra marges (.page CSS doet padding)
      - Pagina-counters via @page { @bottom-right }
    """
    browser = await _ensure_browser()
    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        # set_content wacht standaard op 'load' — geen externe fetches
        # behalve Google Fonts (gespecificeerd in <head>). Wachttijd cap
        # voor het geval fonts.googleapis.com traag is.
        await page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        return pdf_bytes
    finally:
        await page.close()
        await ctx.close()


async def shutdown():
    """Schoon afsluiten — gebruikt door FastAPI shutdown hook."""
    global _browser, _playwright_ctx
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright_ctx is not None:
        await _playwright_ctx.stop()
        _playwright_ctx = None
