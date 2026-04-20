"""
RVO EP-Online adapter — energielabels per woning.

Input  : postcode + huisnummer (of BAG verblijfsobject-id)
Output : energielabel-klasse (A++++ .. G), registratiedatum, gebruiksdoel

**Architectuur-keuze — geen live API, maar lokale cache**

EP-Online werkt niet als een normale REST-API; in plaats daarvan publiceert
RVO maandelijks een integraal bulkbestand (~3M regels) + dagelijkse mutaties.
Dit is voor MVP-gebruik juist ideaal:
  - Eenmaal per maand downloaden => lokale SQLite-cache
  - Lookups zijn <1ms (geen netwerkcall in request-pad)
  - Geen keepalive tegen een key-quotum nodig

**Setup (eenmalig)**
  1. Vraag een API-key aan via het RVO-webformulier (KvK-nummer vereist,
     gebruikt in ~5 minuten geactiveerd).
  2. Zet 'RVO_API_KEY=...' in .env
  3. Draai `python scripts/sync_ep_online.py` (zie daar) om het maandbestand
     te downloaden en in SQLite te laden.
  4. Plan een cronjob/launchd-job op de 2e van de maand om bij te werken.

**Zonder key** — de adapter retourneert None voor elk adres; UI toont netjes
'nog niet beschikbaar'.

Docs: https://www.rvo.nl/onderwerpen/wetten-en-regels-gebouwen/ep-online
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# SQLite-cache ligt naast de app; kan groot worden (~300MB na 3M rows)
# maar blijft een read-only file na sync. In productie zet je dit op een
# persistent disk (Fly volumes / Railway disk).
DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "ep_online.db"


@dataclass
class Energielabel:
    """Resultaat van een EP-Online lookup."""

    postcode: str
    huisnummer: str
    label_klasse: Optional[str]  # "A+++++", "A++++", ..., "G"
    energie_index: Optional[float]  # bv. 1.20
    registratiedatum: Optional[str]  # ISO-datum
    berekeningstype: Optional[str]  # "Bestaande bouw", "Nieuwbouw"
    gebruiksdoel: Optional[str]


def _get_conn() -> Optional[sqlite3.Connection]:
    """Open de SQLite-cache read-only; returneer None als niet aanwezig.

    We openen **readonly** zodat parallele requests niet locking veroorzaken;
    schrijven mag alleen het sync-script doen.
    """
    if not DB_PATH.exists():
        return None
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def fetch_label(postcode: str, huisnummer: str, toevoeging: str = "") -> Optional[Energielabel]:
    """Lookup op postcode + huisnummer (+ optionele toevoeging).

    Retourneert None als:
      - De SQLite-cache nog niet is gebouwd (geen API-key of nog niet gesynct)
      - Het adres geen geregistreerd label heeft (bv. nieuwbouw in aanvraag)

    We normaliseren postcode naar '1234AB' (6 chars, zonder spatie) zodat
    'Damrak 1, 1012 LG' en '1012LG' hetzelfde raken.
    """
    conn = _get_conn()
    if conn is None:
        return None

    pc = (postcode or "").replace(" ", "").upper()
    hn = str(huisnummer or "").strip()
    tv = (toevoeging or "").strip()

    try:
        cur = conn.execute(
            """
            SELECT postcode, huisnummer, toevoeging, label_klasse,
                   energie_index, registratiedatum, berekeningstype, gebruiksdoel
            FROM ep_labels
            WHERE postcode = ? AND huisnummer = ?
              AND (? = '' OR toevoeging = ?)
            ORDER BY registratiedatum DESC
            LIMIT 1
            """,
            (pc, hn, tv, tv),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return Energielabel(
        postcode=row[0],
        huisnummer=row[1],
        label_klasse=row[3],
        energie_index=row[4],
        registratiedatum=row[5],
        berekeningstype=row[6],
        gebruiksdoel=row[7],
    )


# ---------------------------------------------------------------------------
# Schema voor het sync-script
# ---------------------------------------------------------------------------
# We definiëren hier zodat zowel de adapter als het sync-script deze kennen,
# zonder circulaire import. Het sync-script (scripts/sync_ep_online.py) bouwt
# ep_online.db aan deze structuur.

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS ep_labels (
    postcode         TEXT NOT NULL,
    huisnummer       TEXT NOT NULL,
    toevoeging       TEXT NOT NULL DEFAULT '',
    label_klasse     TEXT,            -- 'A+++++' .. 'G'
    energie_index    REAL,
    registratiedatum TEXT,            -- ISO-8601
    berekeningstype  TEXT,
    gebruiksdoel     TEXT,
    bag_vbo_id       TEXT,
    meta             TEXT             -- JSON-blob voor overige RVO-velden
);
CREATE INDEX IF NOT EXISTS idx_postcode_huisnummer
    ON ep_labels(postcode, huisnummer);
CREATE INDEX IF NOT EXISTS idx_bag_vbo
    ON ep_labels(bag_vbo_id);
"""
