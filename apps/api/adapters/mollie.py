"""
Mollie payments adapter — €4,99 voor één buurtscan-rapport.

Flow:
  1. create_payment(amount, description, redirect_url, webhook_url, metadata)
     → POST naar Mollie API → Mollie returnt payment-id + checkout-URL
  2. Frontend redirect user naar checkout-URL
  3. User betaalt → Mollie callt onze webhook met payment-id
  4. Webhook handler → verify_payment(payment_id) → check status
  5. Als 'paid' → mark_paid in payments_db → email versturen

Test-modus: gebruik MOLLIE_TEST_KEY (test_xxxx)
Live-modus: gebruik MOLLIE_LIVE_KEY (live_xxxx)
Auto-detect via env: MOLLIE_API_KEY met 'test_' of 'live_' prefix.

Docs: https://docs.mollie.com/reference/v2/payments-api/create-payment
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

MOLLIE_BASE = "https://api.mollie.com/v2"
TIMEOUT_S = 12.0


def _api_key() -> Optional[str]:
    """Lees Mollie API-key uit env. None als niet gezet (dev mode)."""
    return os.environ.get("MOLLIE_API_KEY", "").strip() or None


def is_configured() -> bool:
    return _api_key() is not None


def is_test_mode() -> bool:
    """Test-mode = key begint met 'test_'."""
    k = _api_key() or ""
    return k.startswith("test_")


async def create_payment(
    amount_cents: int,
    description: str,
    redirect_url: str,
    webhook_url: str,
    metadata: Optional[dict] = None,
    customer_email: Optional[str] = None,
) -> Optional[dict]:
    """Maak een nieuwe Mollie payment. Returnt {'id', 'checkout_url'} of None bij fail.

    Argumenten:
      amount_cents : 499 voor €4,99
      description  : 'Buurtscan rapport - {adres}'
      redirect_url : waar Mollie user naartoe stuurt na (succes/fail)
      webhook_url  : MOET publiek bereikbaar zijn (geen localhost)
      metadata     : custom dict (we sturen ons internal token mee)
      customer_email: bij meegeven gebruikt Mollie deze in checkout
    """
    key = _api_key()
    if not key:
        return None

    body = {
        "amount": {
            "currency": "EUR",
            "value": f"{amount_cents / 100:.2f}",  # Mollie vereist 2 decimalen
        },
        "description": description[:255],
        "redirectUrl": redirect_url,
        "webhookUrl": webhook_url,
        "locale": "nl_NL",
        "method": ["ideal", "creditcard", "bancontact", "applepay", "paypal"],
    }
    if metadata:
        body["metadata"] = metadata
    # NB: customer_email kan in 'billingEmail' veld, niet als API-veld
    # We slaan email zelf op in payments_db; Mollie heeft 'm niet nodig.

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "buurtscan/1.0 (NL)",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(f"{MOLLIE_BASE}/payments", json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        try:
            err = e.response.json()
        except Exception:
            err = {"detail": str(e)}
        print(f"[mollie] create_payment failed: {err}", flush=True)
        return None
    except Exception as e:
        print(f"[mollie] create_payment exception: {e}", flush=True)
        return None

    payment_id = data.get("id")
    checkout_url = ((data.get("_links") or {}).get("checkout") or {}).get("href")
    if not (payment_id and checkout_url):
        print(f"[mollie] create_payment incomplete response: {data}", flush=True)
        return None

    return {
        "id": payment_id,
        "checkout_url": checkout_url,
        "raw": data,
    }


async def get_payment(payment_id: str) -> Optional[dict]:
    """Haal payment-status op bij Mollie. Returnt full payment-dict of None.

    Webhook flow: Mollie POST naar onze webhook met alleen het payment_id.
    Wij MOETEN bij Mollie checken wat de status is — kunnen niet vertrouwen
    op een webhook-body (dat zou spoofing risico zijn).
    """
    key = _api_key()
    if not key:
        return None
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": "buurtscan/1.0 (NL)",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.get(f"{MOLLIE_BASE}/payments/{payment_id}", headers=headers)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        print(f"[mollie] get_payment {payment_id} failed: {e}", flush=True)
        return None


async def is_paid(payment_id: str) -> bool:
    """Convenience: True als Mollie status='paid' is."""
    p = await get_payment(payment_id)
    return bool(p and p.get("status") == "paid")
