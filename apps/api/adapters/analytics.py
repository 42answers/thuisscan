"""
Minimalistische self-hosted analytics.

Principes:
- Géén cookies (AVG-proof, geen cookie-banner nodig)
- Géén IP-adressen opgeslagen (alleen geaggregeerd)
- Géén user-fingerprinting
- Events worden append-only geschreven naar JSONL-bestand

Events:
- page_load: elke pagina-render
- scan: user heeft een adres gescant
- pdf_download: user heeft PDF gedownload
- preview_click: user klikte op HTML-preview
- over_view: user bezocht /over

Frontend stuurt via navigator.sendBeacon() (fire-and-forget, niet blocking).

Data-bestand: apps/api/cache/analytics.jsonl
Format per regel:
    {"ts":"2026-04-23T15:00:00Z","event":"scan","host":"buurtscan.com","path":"/"}

Dashboard via /stats (admin-endpoint, gated door ADMIN_TOKEN env).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Cache-dir is volume-mounted op Fly; overleeft deploys.
_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
_ANALYTICS_FILE = _CACHE_DIR / "analytics.jsonl"

# Max regels voordat we roterer (voorkomt onbeperkte groei).
# ~100 bytes per event × 100k = 10 MB max.
_MAX_LINES = 100_000


def _ensure_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def track(
    event: str,
    *,
    host: Optional[str] = None,
    path: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Log één event. Non-blocking — fails silent bij disk-issues."""
    try:
        _ensure_dir()
        row: dict = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
        }
        if host: row["host"] = host
        if path: row["path"] = path
        if extra: row.update(extra)
        with _ANALYTICS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        pass  # analytics mag nooit een request blokkeren


def load_summary() -> dict:
    """Aggregeer de analytics-file naar een summary-dict voor /stats.

    Output:
      {
        "total": N,
        "per_event": {event: count},
        "per_day": {date: count},
        "top_paths": [(path, count), ...],
        "last_events": [...laatste 20],
      }
    """
    if not _ANALYTICS_FILE.exists():
        return {"total": 0, "per_event": {}, "per_day": {}, "top_paths": [], "last_events": []}

    per_event: dict[str, int] = {}
    per_day: dict[str, int] = {}
    per_path: dict[str, int] = {}
    last_events: list[dict] = []
    total = 0

    try:
        with _ANALYTICS_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                total += 1
                ev = row.get("event", "?")
                per_event[ev] = per_event.get(ev, 0) + 1
                day = (row.get("ts") or "")[:10]
                if day:
                    per_day[day] = per_day.get(day, 0) + 1
                p = row.get("path")
                if p:
                    per_path[p] = per_path.get(p, 0) + 1
                last_events.append(row)
    except Exception:
        pass

    top_paths = sorted(per_path.items(), key=lambda x: -x[1])[:15]
    last_events = last_events[-20:][::-1]
    return {
        "total": total,
        "per_event": dict(sorted(per_event.items(), key=lambda x: -x[1])),
        "per_day": dict(sorted(per_day.items())),
        "top_paths": top_paths,
        "last_events": last_events,
    }


def rotate_if_needed() -> None:
    """Als de file te groot wordt, houd alleen de laatste helft."""
    try:
        if not _ANALYTICS_FILE.exists(): return
        with _ANALYTICS_FILE.open(encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > _MAX_LINES:
            keep = lines[-_MAX_LINES // 2:]
            _ANALYTICS_FILE.write_text("".join(keep), encoding="utf-8")
    except Exception:
        pass
