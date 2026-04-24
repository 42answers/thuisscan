# Paywall Deploy Checklist

Stap-voor-stap draaiboek voor het live zetten van de €4,99-paywall.

De code is volledig af en staat achter een feature-flag. Tot je `go` geeft,
werkt de landingspagina ongestyled (zoals voorheen). Met `?paywall=1` in de
URL kun je zelf elk moment testen wat bezoekers straks zien.

---

## 1. Mollie-account (15 min)

1. Ga naar https://www.mollie.com en maak een account aan.
2. Kies **Nederland** als land, account-type **Particulier** of **Eenmanszaak** — voor KVK-loos starten is particulier OK tot ~€10k omzet.
3. Geef op: IBAN-rekening (waar de verdiensten naartoe gaan), kopie ID, basis NAW.
4. Mollie doet compliance-check (1-3 werkdagen). In de tussentijd krijg je direct een **test-API-key** om te ontwikkelen.
5. Dashboard → **Developers → API-keys** → kopieer de `test_xxx` key voor ontwikkeling.
6. Later (na compliance-OK): kopieer de `live_xxx` key.

**Belangrijk:** Mollie vraagt bij aanvraag om een werkende URL naar je
voorwaarden en privacy — die staan al op `/voorwaarden` en `/privacy`.

### Test-modus vs live

* `test_xxx` → Mollie werkt in de achtergrond, iDEAL opent een **test-bank** die niks afschrijft. Perfect voor eindtests.
* `live_xxx` → echt geld. Pas flippen na succesvolle test-order.

---

## 2. Resend-account + domein (30 min)

1. https://resend.com → account aanmaken (gratis tier: 3000 mails/maand).
2. **Dashboard → API Keys** → maak key `re_xxx`.

### Domein-verificatie (optioneel maar aanbevolen)

Zonder verificatie verstuurt Resend vanaf `onboarding@resend.dev` — werkt, maar
ziet er minder pro uit. Met verificatie vanaf `noreply@buurtscan.com`:

1. **Dashboard → Domains → Add Domain** → `buurtscan.com`
2. Resend geeft 3-4 DNS-records (SPF + DKIM + optioneel DMARC). Kopieer die.
3. Bij je domeinregistrar (waar buurtscan.com is geregistreerd): DNS-zone → voeg de records toe:
   * `TXT @ "v=spf1 include:resend.com ~all"` (of merge met bestaande SPF)
   * `CNAME resend._domainkey.buurtscan.com → resend._domainkey.<…>.resend.com`
   * (optioneel) DMARC TXT-record
4. Terug in Resend → **Verify** drukken. Werkt meestal binnen 5-30 minuten.

**Als je DNS niet wil aanraken nu:** sla dit over. De mail komt dan vanaf
`onboarding@resend.dev` met `reply-to: redactie@buurtscan.nl`.

---

## 3. Fly-secrets zetten (5 min)

In project-root (`thuisscan/`):

```bash
# Mollie — start met test-key!
fly secrets set MOLLIE_API_KEY=test_xxx_YOUR_MOLLIE_TEST_KEY

# Resend
fly secrets set RESEND_API_KEY=re_xxx_YOUR_RESEND_KEY

# Als je domein verifieerde:
fly secrets set RESEND_FROM="Buurtscan <noreply@buurtscan.com>"

# Random zout voor IP-hash (maak een lange random string):
fly secrets set IP_HASH_SALT="$(openssl rand -hex 32)"

# Admin-token voor /admin/sales + /stats (jouw geheim):
fly secrets set ADMIN_TOKEN="$(openssl rand -hex 24)"

# Optioneel reply-to override:
fly secrets set RESEND_REPLY_TO="redactie@buurtscan.nl"
```

Na `fly secrets set` hervalt Fly.io automatisch → backend restart met nieuwe env-vars.

---

## 4. End-to-end test (paywall nog OFF voor publiek)

Met de flag nog op `false` in `app.js`, test zelf:

1. Open https://buurtscan.com/?paywall=1 → adres invullen → zie de blur + paywall-card + CTA-knop.
2. Klik **"Volledig rapport — € 4,99"** → modal opent → vul je eigen e-mail in → klik **"Doorgaan naar betaling"**.
3. Mollie-checkout opent → kies **iDEAL** → kies **TEST-bank (Stripe, of Mollie Test)** → betaal.
4. Na betaling: redirect naar `/r/<token>/wachtkamer` → na 5-15s auto-refresh naar `/r/<token>` (volledig rapport).
5. Check je inbox: e-mail van Resend met magic-link — klik hem → opent het rapport.
6. Klik **"Rapport als PDF"** op de paid-page → PDF downloadt correct.
7. Ga naar https://buurtscan.com/admin/sales?token=YOUR_ADMIN_TOKEN → zie 1 verkoop staan.
8. (optioneel) Test expired-flow: pas in DB `valid_until` aan naar gisteren → open `/r/<token>` → zie "Link verlopen".

### Fout-scenario's om te checken

* **Mollie cancellen:** in Mollie-checkout → "Cancel" → zie `/r/<token>/wachtkamer` met "betaling niet doorgegaan".
* **Magic-link na 7 dagen:** zie boven — pas `valid_until` aan, test expired-pagina.
* **Verkeerde token in URL:** open `/r/garbage123` → zie "Link onbekend".

---

## 5. Go-live: paywall activeren voor iedereen

Zodra de end-to-end test slaagt met de test-key:

1. Zet Mollie op live: `fly secrets set MOLLIE_API_KEY=live_xxx_YOUR_LIVE_KEY`
2. In `apps/web/app.js` regel ~478: wijzig `const _PAYWALL_DEFAULT_ON = false;` naar `const _PAYWALL_DEFAULT_ON = true;`
3. Commit + push → Netlify deploy trigger.
4. Doe nog één eindtest met een **echte €4,99 iDEAL-betaling** (jezelf) zodat je zeker weet dat de live-key werkt.

---

## 6. Rollback (als er iets misgaat)

**Paywall snel uitzetten zonder deploy:**

Nee, niet mogelijk — de flag zit in gecompileerde JS. Maar:

* Alle bezoekers krijgen `console.info` met flag-status → je ziet direct welke versie draait.
* `?paywall=0` in de URL override't een ON-deploy terug naar free PDF (per-sessie).

**Echte rollback:**

1. `apps/web/app.js` — flip `_PAYWALL_DEFAULT_ON` terug op `false`.
2. Commit + push → Netlify deployt binnen ~1-2 minuten.

**Refund handmatig:**

```bash
# Via Mollie-dashboard (makkelijkst): payments → klik op betaling → "Refund"
# Ook in onze DB markeren:
fly ssh console
python3 -c "
from adapters.payments_db import _conn
with _conn() as c:
    c.execute(\"UPDATE paid_reports SET status='refunded' WHERE token='XXX'\")
"
```

---

## 7. Dagelijkse checks

* **Admin-dashboard:** https://api.buurtscan.com/admin/sales?token=YOUR_ADMIN_TOKEN
  → omzet, recent paid, top-adressen, status-breakdown.
* **Mollie-dashboard:** overview van alle transactions, chargebacks, refunds.
* **Resend-dashboard:** bezorgingsrate (spam-risico → domein-verificatie doen!).
* **Fly-logs:** `fly logs` — let op `[email] sent to ...` en `[mollie] ...` lines.

---

## 8. Open punten die later kunnen

* **Domein-verificatie Resend** — als je nu met `onboarding@resend.dev` start, later alsnog DNS doen voor betere deliverability.
* **KVK registratie** — bij meer dan ~€10k/jaar wordt het verstandig. Dan ook BTW-nummer + op voorwaarden bijwerken.
* **Boekhouding-export** — `payments_db.stats_summary()` levert JSON; eenvoudig CSV-export toevoegen als je dat nodig hebt.
* **Email-template A/B** — Resend ondersteunt templates; de HTML zit nu hardcoded in `email_sender.py`.
* **Affiliate-link voor Mollie** — Mollie heeft een referral-programma.
* **Reverse-trigger voor pending-cleanup** — dagelijkse cronjob die `payments_db.cleanup_expired()` aanroept voor privacy (data-retentie: 30 dagen na `valid_until`).

---

## Bij problemen

Mail jezelf naar redactie@buurtscan.nl om je monitoring-alerts te loggen.
Raadpleeg deze file als checklist bij iedere release.
