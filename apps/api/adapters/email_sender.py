"""
Transactional email via Resend.com — magic-link versturing.

Setup:
  1. Maak gratis account op resend.com
  2. Verifieer domein buurtscan.com (DNS records: SPF + DKIM)
  3. Maak API-key, zet als env-var: RESEND_API_KEY=re_xxx
  4. Optional: zet RESEND_FROM=noreply@buurtscan.com (default = onboarding@resend.dev)

Zonder verified domein: send vanaf onboarding@resend.dev (test-modus)
Met verified domein: send vanaf noreply@buurtscan.com

Bij ontbrekende key: print mail naar stderr (development fallback) zodat
de flow getest kan worden zonder echt mail te sturen.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import httpx

RESEND_API = "https://api.resend.com/emails"
TIMEOUT_S = 10.0


def _api_key() -> Optional[str]:
    return os.environ.get("RESEND_API_KEY", "").strip() or None


def _from_address() -> str:
    """Sender-adres. Default = Resend test-domein (werkt zonder DNS)."""
    return os.environ.get(
        "RESEND_FROM",
        "Buurtscan <onboarding@resend.dev>",
    )


def is_configured() -> bool:
    return _api_key() is not None


async def send_magic_link(
    to_email: str,
    adres: str,
    magic_url: str,
    valid_until: str,   # ISO-datum, bv "2026-04-30T15:00:00Z"
    bedrag_eur: float = 4.99,
) -> bool:
    """Stuur magic-link mail naar koper. Returnt True bij succes.

    Bij missing API-key: print naar stderr en returnt True (dev mode).
    """
    # Format geldig-tot in NL-vrije-tekst
    nl_until = _format_dutch_date(valid_until)
    subject = f"Je Buurtscan-rapport voor {adres}"
    text_body = f"""Hi,

Bedankt voor je bestelling. Je rapport voor "{adres}" is klaar.

Open hier:
{magic_url}

De link is geldig tot {nl_until} (7 dagen). Je kunt onbeperkt vaak
het rapport openen of als PDF downloaden binnen die periode.

Vragen? Antwoord op deze mail of bekijk buurtscan.com/over.

Bedrag: € {bedrag_eur:.2f} incl. BTW
KVK: Buurtscan · Nederland

Met vriendelijke groet,
Team Buurtscan
"""

    html_body = _render_html(adres, magic_url, nl_until, bedrag_eur)

    key = _api_key()
    if not key:
        # Dev mode — log mail naar stderr in plaats van te versturen
        print("=" * 60, file=sys.stderr)
        print(f"[email DEV-MODE] (geen RESEND_API_KEY) → zou mail sturen:", file=sys.stderr)
        print(f"  To:      {to_email}", file=sys.stderr)
        print(f"  Subject: {subject}", file=sys.stderr)
        print(f"  Link:    {magic_url}", file=sys.stderr)
        print(f"  Geldig:  {nl_until}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        return True

    payload = {
        "from": _from_address(),
        "to": [to_email],
        "subject": subject,
        "text": text_body,
        "html": html_body,
        "reply_to": os.environ.get("RESEND_REPLY_TO", "redactie@buurtscan.nl"),
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(
                RESEND_API,
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            print(f"[email] sent to {to_email} (id: {r.json().get('id','?')})", flush=True)
            return True
    except httpx.HTTPStatusError as e:
        print(f"[email] resend HTTP error: {e.response.status_code} {e.response.text[:200]}", flush=True)
        return False
    except Exception as e:
        print(f"[email] resend exception: {e}", flush=True)
        return False


def _format_dutch_date(iso: str) -> str:
    """ISO-tijd → '30 april 2026 om 15:00'."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return iso
    nl_maanden = ["januari","februari","maart","april","mei","juni","juli","augustus",
                  "september","oktober","november","december"]
    return f"{dt.day} {nl_maanden[dt.month-1]} {dt.year} om {dt.hour:02d}:{dt.minute:02d}"


def _render_html(adres: str, magic_url: str, nl_until: str, bedrag_eur: float) -> str:
    """Editorial HTML-mail die past bij Buurtscan-stijl."""
    # html escapen — basis
    import html as _h
    adres_e = _h.escape(adres)
    return f"""<!DOCTYPE html>
<html lang="nl"><head><meta charset="utf-8"><title>Je Buurtscan-rapport</title></head>
<body style="margin:0;padding:0;font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;background:#fafaf7;color:#1a1a1a;line-height:1.6">
  <div style="max-width:560px;margin:0 auto;padding:32px 24px">
    <div style="text-align:center;margin-bottom:32px">
      <span style="display:inline-block;width:32px;height:32px;background:#1f4536;color:#fff;text-align:center;line-height:32px;font-family:Georgia,serif;font-size:20px;font-weight:600;border-radius:4px;vertical-align:-6px;margin-right:8px">B</span>
      <span style="font-size:20px;font-weight:600;letter-spacing:-0.01em">Buurtscan</span>
    </div>
    <h1 style="font-family:Georgia,serif;font-size:26px;line-height:1.2;letter-spacing:-0.01em;font-weight:400;margin:0 0 16px">
      Je rapport is <em style="color:#1f4536">klaar</em>.
    </h1>
    <p style="font-size:16px;color:#4a4a4a;margin:0 0 28px">
      Hier is je volledige Buurtscan-rapport voor <strong style="color:#1a1a1a">{adres_e}</strong>.
    </p>
    <div style="text-align:center;margin:32px 0">
      <a href="{magic_url}" style="display:inline-block;padding:14px 28px;background:#1f4536;color:#fff;text-decoration:none;font-weight:500;border-radius:6px;font-size:16px">📄 Open je rapport</a>
    </div>
    <div style="background:#fff;border:1px solid #e8e6e0;border-radius:10px;padding:16px 20px;margin:24px 0;font-size:14px;color:#4a4a4a">
      <strong style="color:#1f4536">⏰ Geldig tot:</strong> {nl_until}<br>
      <strong style="color:#1f4536">📥 Onbeperkt downloaden</strong> — open het rapport en bewaar de PDF zo vaak je wilt binnen 7 dagen.
    </div>
    <p style="font-size:14px;color:#6b6b6b;margin:24px 0 8px">
      Werkt de knop niet? Plak deze link in je browser:<br>
      <a href="{magic_url}" style="color:#1f4536;word-break:break-all">{magic_url}</a>
    </p>
    <hr style="border:none;border-top:1px solid #e8e6e0;margin:32px 0">
    <p style="font-size:13px;color:#8a8a8a;margin:0">
      Bedrag: <strong>€ {bedrag_eur:.2f}</strong> incl. BTW · 3 dagen geld-terug-garantie<br>
      Vragen? Antwoord op deze mail of mail <a href="mailto:redactie@buurtscan.nl" style="color:#1f4536">redactie@buurtscan.nl</a>.<br>
      <span style="opacity:0.7">© 2026 Buurtscan · Nederland</span>
    </p>
  </div>
</body></html>"""
