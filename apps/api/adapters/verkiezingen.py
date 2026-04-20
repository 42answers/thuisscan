"""
TK2025-verkiezingen adapter — top 3 partijen per gemeente vs. landelijk.

Input  : gemeentecode (4 cijfers uit PDOK, of 'GM....')
Output : top-3 partijen met % stemmen + landelijk gemiddelde + delta

Databron: https://data.overheid.nl/dataset/verkiezingsuitslag-tweede-kamer-2025
  Publieke CSV (Kiesraad) met stemmen-per-lijst per gemeente.

De data is eenmaal verwerkt door scripts/sync_verkiezingen.py naar
'data/tk2025.json' (~37 KB, 343 gemeenten). Runtime is dus een lookup
uit een JSON-file — geen netwerk, <1ms.

Updaten: als er een nieuwe verkiezing is, haal het nieuwe TK*_CSV-bestand
op van data.overheid.nl en draai opnieuw sync_verkiezingen.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "tk2025.json"

# Lazy-loaded singleton cache
_DATA: Optional[dict] = None


def _load() -> dict:
    """Lees de JSON één keer per proces en cache in memory."""
    global _DATA
    if _DATA is None:
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                _DATA = json.load(f)
        except FileNotFoundError:
            _DATA = {"election": None, "nl_pct": {}, "per_gemeente": {}}
    return _DATA


@dataclass
class VerkiezingsUitslag:
    """Top-3 partijen voor deze gemeente + landelijk referentie."""

    election: str  # 'TK2025'
    date: str
    gemeentecode: str
    top3: list[dict]  # [{partij, pct_gemeente, pct_nl, delta_pct}]
    per_gemeente_beschikbaar: bool  # False = landelijke fallback


def fetch_top3(gemeentecode: str) -> Optional[VerkiezingsUitslag]:
    """Top-3 partijen bij laatste TK-verkiezing in deze gemeente.

    PDOK levert gemeentecode als 4 cijfers ('0534' voor Hillegom);
    Kiesraad gebruikt 'G' + die 4 cijfers ('G0534'). We normaliseren.
    """
    if not gemeentecode:
        return None
    data = _load()
    if not data.get("per_gemeente"):
        return None

    # Normaliseer naar 'Gnnnn'
    if gemeentecode.startswith("GM"):
        key = "G" + gemeentecode[2:]
    elif gemeentecode.startswith("G"):
        key = gemeentecode
    else:
        key = f"G{gemeentecode}"

    nl_pct = data.get("nl_pct", {})
    local = data.get("per_gemeente", {}).get(key)

    if local:
        top3 = [
            {
                "partij": row["partij"],
                "pct_gemeente": row["pct"],
                "pct_nl": nl_pct.get(row["partij"]),
                "delta_pct": (
                    round(row["pct"] - nl_pct[row["partij"]], 1)
                    if row["partij"] in nl_pct
                    else None
                ),
            }
            for row in local
        ]
        beschikbaar = True
    else:
        # Landelijke fallback — sorteer NL-pct, pak top 3
        sorted_nl = sorted(nl_pct.items(), key=lambda x: -x[1])[:3]
        top3 = [
            {"partij": p, "pct_gemeente": None, "pct_nl": pct, "delta_pct": None}
            for p, pct in sorted_nl
        ]
        beschikbaar = False

    return VerkiezingsUitslag(
        election=data.get("election", "TK2025"),
        date=data.get("date", ""),
        gemeentecode=key,
        top3=top3,
        per_gemeente_beschikbaar=beschikbaar,
    )
