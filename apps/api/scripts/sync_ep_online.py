#!/usr/bin/env python3
"""
Maandelijkse sync van EP-Online bulk-bestanden naar lokale SQLite-cache.

Draaischema (aanbevolen):
  - Op de 2e dag van de maand -> download het volledige 'totaalbestand'
  - Elke nacht -> dagbestand (mutaties) toepassen

Voor MVP: alleen maandelijks, geen mutaties. Dat geeft elke buurt ~1 maand
vertraging in label-wijzigingen; acceptabel voor een 'adres-check'-app.

Vereist env-var: RVO_API_KEY

Gebruik:
    python scripts/sync_ep_online.py                # totaalbestand huidige maand
    python scripts/sync_ep_online.py --dry-run      # alleen URL's listen
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

# Repo root toevoegen aan path zodat we rvo_ep SCHEMA_DDL kunnen importeren
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapters.rvo_ep import DB_PATH, SCHEMA_DDL  # noqa: E402

# RVO EP-Online bulk-download endpoint.
# URL-formaat: /EPBestanden/{jaar}/{jaar}{maand:02}/v{volgnr}.zip
# Exacte URL bij aanvraag van de API-key; placeholder hier ter referentie.
DOWNLOAD_INFO_URL = (
    "https://public.ep-online.nl/api/v5/Mutatiebestand/DownloadInfo"
    "?fileType=csv&xmlVersion=4"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--month", help="YYYY-MM, default huidige maand")
    args = parser.parse_args()

    api_key = os.environ.get("RVO_API_KEY")
    if not api_key:
        print("ERROR: RVO_API_KEY env-var ontbreekt.")
        print("Vraag een key aan via https://www.rvo.nl/onderwerpen/wetten-en-regels-gebouwen/ep-online")
        return 2

    # Stap 1: /Mutatiebestand/DownloadInfo geeft een pre-signed downloadUrl (24u geldig)
    print("Opvragen DownloadInfo...")
    info_resp = httpx.get(
        DOWNLOAD_INFO_URL, headers={"Authorization": api_key}, timeout=30.0
    )
    if info_resp.status_code != 200:
        print(f"DownloadInfo-call faalde: HTTP {info_resp.status_code}")
        print(info_resp.text[:500])
        return 1
    info = info_resp.json()
    url = info["downloadUrl"]
    fname = info["bestandsnaam"]
    print(f"  Bestand: {fname}")
    print(f"  Geldig tot: {info.get('geldigTotEnMet')}")
    if args.dry_run:
        return 0

    # Stap 2: stream-download naar disk (niet BytesIO) om low-RAM hosts
    # niet te belasten. 225 MB op disk is goedkoper dan in RAM, en de
    # zipfile kan dan streaming door de file heen lopen.
    zip_path = DB_PATH.parent / "ep_online_latest.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading (~225MB) naar {zip_path}...")
    with httpx.stream("GET", url, timeout=600.0, follow_redirects=True) as resp:
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:500]}")
            return 1
        with zip_path.open("wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    print(f"  Download klaar: {zip_path.stat().st_size / 1e6:.0f} MB op disk")

    # Format (RVO v4): regel 1-2 metadata, regel 3 header, regel 4+ data
    with zipfile.ZipFile(zip_path) as z:
        csv_name = next(
            (n for n in z.namelist() if n.lower().endswith(".csv")), None
        )
        if csv_name is None:
            print("Geen CSV in ZIP gevonden.")
            return 1
        print(f"Parsing {csv_name} (~1.5GB, streaming)...")

        with z.open(csv_name) as f:
            text_io = io.TextIOWrapper(f, encoding="utf-8-sig")
            # Eerste 2 regels: metadata
            meta_1 = text_io.readline()
            meta_2 = text_io.readline()
            print(f"  Metadata: {meta_1.strip()} | {meta_2.strip()}")
            # Regel 3 = header; rest = data via DictReader
            reader = csv.DictReader(text_io, delimiter=";")
            rows = reader  # streaming iterator; geen list()!

            # Build fresh DB — doen we binnen de with-block zodat de CSV-stream open blijft.
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = DB_PATH.with_suffix(".db.tmp")
            if tmp_path.exists():
                tmp_path.unlink()
            conn = sqlite3.connect(tmp_path)
            try:
                conn.executescript(SCHEMA_DDL)
                # Memory-efficient: ~6 MB cache (ipv default 2000 pages * 4KB=8MB,
                # groeit bij writes). Op low-RAM hosts (Fly 512 MB) is dit cruciaal;
                # lokaal heeft het geen impact op snelheid.
                conn.execute("PRAGMA journal_mode = OFF")
                conn.execute("PRAGMA synchronous = OFF")
                conn.execute("PRAGMA cache_size = -2000")  # 2 MB cache
                conn.execute("PRAGMA temp_store = FILE")
                _insert_rows(conn, rows)
                conn.commit()
            finally:
                conn.close()

    # Atomic swap: oud bestand pas vervangen als nieuw volledig is gebouwd
    if DB_PATH.exists():
        DB_PATH.unlink()
    tmp_path.rename(DB_PATH)
    print(f"Done. SQLite: {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.0f} MB)")
    return 0


def _insert_rows(conn: sqlite3.Connection, rows) -> None:
    """Stream rijen in batches van 50k naar SQLite.

    Werkelijke kolomnamen uit RVO v4 CSV (2026-04):
      Postcode, Huisnummer, Huisletter, Huisnummertoevoeging,
      Energieklasse, EnergieIndex, Registratiedatum, Berekeningstype,
      Gebouwklasse, Gebouwtype, BAGVerblijfsobjectID, Bouwjaar.
    """
    count = 0
    batch: list[tuple] = []
    for row in rows:
        postcode = (row.get("Postcode") or "").replace(" ", "").upper()
        huisnummer = str(row.get("Huisnummer") or "").strip()
        toevoeging = (
            (row.get("Huisletter") or "") + (row.get("Huisnummertoevoeging") or "")
        ).strip()
        # Sla lege postcodes/huisnummers over (komen voor bij oude registraties)
        if not postcode or not huisnummer:
            continue
        # Bouw gebruiksdoel-string uit Gebouwklasse + Gebouwtype
        klasse = row.get("Gebouwklasse") or ""
        gtype = row.get("Gebouwtype") or ""
        gebruiksdoel = f"{klasse} {gtype}".strip() if klasse or gtype else None
        batch.append(
            (
                postcode,
                huisnummer,
                toevoeging,
                row.get("Energieklasse"),
                _to_float(row.get("EnergieIndex")),
                row.get("Registratiedatum"),
                row.get("Berekeningstype"),
                gebruiksdoel,
                row.get("BAGVerblijfsobjectID"),
                None,  # meta
            )
        )
        if len(batch) >= 10_000:
            _flush(conn, batch)
            count += len(batch)
            batch.clear()
            # Periodieke commit houdt SQLite-journal klein (cruciaal op low-RAM)
            # + geeft regelmatige I/O zodat SSH-connection levend blijft.
            if count % 100_000 == 0:
                conn.commit()
            if count % 500_000 == 0:
                print(f"  ... {count:,} rijen verwerkt", flush=True)
    if batch:
        _flush(conn, batch)
        count += len(batch)
    print(f"  {count:,} rijen opgeslagen")


def _flush(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO ep_labels (
            postcode, huisnummer, toevoeging, label_klasse, energie_index,
            registratiedatum, berekeningstype, gebruiksdoel, bag_vbo_id, meta
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
