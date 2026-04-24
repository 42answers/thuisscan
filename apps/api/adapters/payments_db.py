"""
SQLite-database voor betaalde Buurtscan-rapporten.

Tabel `paid_reports`:
- token TEXT PRIMARY KEY    — 32-byte URL-safe random (cryptographically secure)
- adres_query TEXT          — exacte zoekstring zoals door user ingevoerd
- email TEXT                — voor verzending magic-link
- mollie_payment_id TEXT    — referentie naar Mollie payment
- amount_cents INT          — wat is betaald (€4,99 = 499)
- status TEXT               — 'pending' | 'paid' | 'expired' | 'refunded'
- created_at TIMESTAMP      — wanneer de pending-row is gemaakt
- paid_at TIMESTAMP         — wanneer Mollie 'paid' callback gaf (NULL als nog pending)
- valid_until TIMESTAMP     — paid_at + 7 dagen
- download_count INT        — hoe vaak link is geopend (analytics)
- ip_hash TEXT              — sha256(IP+salt) voor abuse-detect, geen IP-storage

Cache-volume mount: /app/apps/api/cache/payments.sqlite
Overleeft deploys (Fly volume).
"""
from __future__ import annotations

import sqlite3
import secrets
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "payments.sqlite"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_reports (
    token              TEXT PRIMARY KEY,
    adres_query        TEXT NOT NULL,
    email              TEXT NOT NULL,
    mollie_payment_id  TEXT,
    amount_cents       INTEGER NOT NULL DEFAULT 499,
    status             TEXT NOT NULL DEFAULT 'pending',
    created_at         TEXT NOT NULL,
    paid_at            TEXT,
    valid_until        TEXT,
    download_count     INTEGER NOT NULL DEFAULT 0,
    ip_hash            TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_id ON paid_reports(mollie_payment_id);
CREATE INDEX IF NOT EXISTS idx_email ON paid_reports(email);
CREATE INDEX IF NOT EXISTS idx_status ON paid_reports(status);
"""

_GELDIGHEID_DAGEN = 7
_IP_SALT = os.environ.get("IP_HASH_SALT", "buurtscan-default-salt-change-me")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db() -> None:
    """Maak tabel aan als nog niet bestaat. Idempotent — veilig bij elke startup."""
    with _conn() as c:
        c.executescript(_SCHEMA)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_ip(ip: Optional[str]) -> Optional[str]:
    if not ip: return None
    return hashlib.sha256(f"{ip}:{_IP_SALT}".encode()).hexdigest()[:16]


def create_pending(adres_query: str, email: str, ip: Optional[str] = None,
                   amount_cents: int = 499) -> str:
    """Maak een pending-rij aan, returnt het generated token.

    Token = 32 bytes URL-safe ≈ 256 bits entropie — onmogelijk te raden.
    De rij krijgt status='pending'; pas na Mollie-webhook 'paid' wordt
    valid_until ingevuld en wordt de magic-link werkbaar.
    """
    token = secrets.token_urlsafe(32)
    with _conn() as c:
        c.execute("""
            INSERT INTO paid_reports
              (token, adres_query, email, amount_cents, status, created_at, ip_hash)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """, (token, adres_query, email, amount_cents, _now_iso(), _hash_ip(ip)))
    return token


def attach_mollie_payment(token: str, mollie_payment_id: str) -> None:
    """Koppel Mollie payment-id aan een pending token (na create_payment call)."""
    with _conn() as c:
        c.execute("""
            UPDATE paid_reports SET mollie_payment_id = ?
            WHERE token = ?
        """, (mollie_payment_id, token))


def mark_paid(mollie_payment_id: str) -> Optional[dict]:
    """Markeer als betaald + zet valid_until = nu + 7 dagen.

    Returnt de bijgewerkte rij (om mail te kunnen sturen) of None als
    we de payment-id niet kennen (bv. unrelated webhook).
    """
    paid_at = _now_iso()
    valid_until = (datetime.now(timezone.utc) + timedelta(days=_GELDIGHEID_DAGEN)
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as c:
        cur = c.execute("""
            UPDATE paid_reports
               SET status = 'paid', paid_at = ?, valid_until = ?
             WHERE mollie_payment_id = ? AND status = 'pending'
        """, (paid_at, valid_until, mollie_payment_id))
        if cur.rowcount == 0:
            return None
        row = c.execute("""
            SELECT * FROM paid_reports WHERE mollie_payment_id = ?
        """, (mollie_payment_id,)).fetchone()
        return dict(row) if row else None


def mark_failed(mollie_payment_id: str, status: str = "expired") -> None:
    """Mollie callback voor 'expired' / 'failed' / 'canceled'."""
    with _conn() as c:
        c.execute("""
            UPDATE paid_reports SET status = ?
            WHERE mollie_payment_id = ?
        """, (status, mollie_payment_id))


def get_by_token(token: str) -> Optional[dict]:
    """Lees de rij op token. Returnt None als onbestaand."""
    with _conn() as c:
        row = c.execute("""
            SELECT * FROM paid_reports WHERE token = ?
        """, (token,)).fetchone()
        return dict(row) if row else None


def is_valid(token: str) -> tuple[bool, Optional[str], Optional[dict]]:
    """Check of token bruikbaar is voor rapport-toegang.

    Returnt (is_valid, fout_reden, row).
    fout_reden ∈ {None, 'unknown', 'pending', 'refunded', 'expired_time', 'expired_status'}
    """
    row = get_by_token(token)
    if not row:
        return (False, "unknown", None)
    if row["status"] == "pending":
        return (False, "pending", row)
    if row["status"] == "refunded":
        return (False, "refunded", row)
    if row["status"] != "paid":
        return (False, "expired_status", row)
    # Tijds-check
    valid_until = row.get("valid_until")
    if not valid_until:
        return (False, "expired_status", row)
    try:
        until = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > until:
            return (False, "expired_time", row)
    except Exception:
        return (False, "expired_status", row)
    return (True, None, row)


def increment_download(token: str) -> None:
    """Tel hoe vaak de link is gebruikt (geen rate-limit, alleen analytics)."""
    with _conn() as c:
        c.execute("""
            UPDATE paid_reports SET download_count = download_count + 1
            WHERE token = ?
        """, (token,))


def stats_summary() -> dict:
    """Geaggregeerde verkoop-statistieken voor admin-dashboard."""
    with _conn() as c:
        rows = c.execute("""
            SELECT status, COUNT(*) AS n, SUM(amount_cents) AS cents_total
            FROM paid_reports GROUP BY status
        """).fetchall()
        per_status = {r["status"]: {"n": r["n"], "cents_total": r["cents_total"] or 0} for r in rows}
        # Omzet alleen van 'paid' (refunded telt niet)
        paid = per_status.get("paid", {"n": 0, "cents_total": 0})
        omzet_eur = paid["cents_total"] / 100
        # Top adressen (meest verkocht)
        top = c.execute("""
            SELECT adres_query, COUNT(*) AS n
            FROM paid_reports
            WHERE status = 'paid'
            GROUP BY adres_query
            ORDER BY n DESC
            LIMIT 10
        """).fetchall()
        # Recent paid
        recent = c.execute("""
            SELECT token, adres_query, email, amount_cents, paid_at, download_count, valid_until
            FROM paid_reports
            WHERE status = 'paid'
            ORDER BY paid_at DESC
            LIMIT 20
        """).fetchall()
        return {
            "per_status": per_status,
            "omzet_eur": omzet_eur,
            "totaal_betaald": paid["n"],
            "top_adressen": [{"adres": r["adres_query"], "n": r["n"]} for r in top],
            "recent": [
                {
                    "token_prefix": (r["token"] or "")[:8] + "…",  # niet volledig tonen
                    "adres": r["adres_query"],
                    "email": _mask_email(r["email"]),
                    "eur": r["amount_cents"] / 100,
                    "paid_at": r["paid_at"],
                    "downloads": r["download_count"],
                    "valid_until": r["valid_until"],
                }
                for r in recent
            ],
        }


def _mask_email(email: Optional[str]) -> str:
    """k****@example.com — voor admin-view zonder volle email te lekken."""
    if not email or "@" not in email: return "—"
    name, domain = email.split("@", 1)
    if len(name) <= 2: return f"{name[0]}***@{domain}"
    return f"{name[0]}***{name[-1]}@{domain}"


def cleanup_expired(older_than_days: int = 30) -> int:
    """Verwijder paid-rows die >30d verlopen zijn (privacy: data-retentie)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as c:
        cur = c.execute("""
            DELETE FROM paid_reports
            WHERE valid_until IS NOT NULL AND valid_until < ?
        """, (cutoff,))
        return cur.rowcount
