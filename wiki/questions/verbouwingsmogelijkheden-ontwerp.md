---
type: synthesis
title: "Verbouwingsmogelijkheden — ontwerp Sectie 10"
created: 2026-04-22
updated: 2026-04-22
question: "Hoe maken we verbouwingsmogelijkheden zo concreet mogelijk in een aparte Buurtscan-sectie?"
answer_quality: solid
status: developing
tags:
  - feature-design
  - open-data
  - nl-geo
  - verbouwing
related:
  - "[[log]]"
---

# Verbouwingsmogelijkheden — ontwerp Sectie 10

Sectie 10 van de Buurtscan-scan geeft een koper concrete getallen over wat hij aan een pand mag verbouwen. Niet "check het bestemmingsplan" maar: "je kan aan de achterkant 24 m² uitbouwen, waarschijnlijk vergunningvrij, want er is 6 m × 4 m vrij achtererf en je zit niet in beschermd gezicht."

## Publieke data-bronnen NL

### Tier 1 — integreren in Fase 1

**Kadastraal perceel** (PDOK BRK-Publiek WFS)
- Endpoint: `https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0`
- Laag: `kadastralekaartv5:Perceel`
- Levert: perceel-polygoon + oppervlakte
- Gratis, geen auth

**Beschermd stads-/dorpsgezicht** (RCE WFS, andere laag dan rijksmonumenten)
- Endpoint: `https://services.rce.geovoorziening.nl/rce/wfs`
- Laag: `rce:BPG` (Beschermde Gezichten)
- Levert: vlak van het beschermde gezicht + gezicht-naam
- Implicatie: binnen → géén vergunningvrij bouwen, altijd omgevingsvergunning + welstand. Amsterdam-centrum, Utrecht-binnenstad, Haarlem-centrum vallen hier vrijwel volledig onder.

**Bestemmingsplan / omgevingsplan** (PDOK Plangebieden WFS)
- Endpoint: `https://service.pdok.nl/kadaster/plangebieden/wfs/v1_0`
- Lagen: `plangebieden:Bestemmingsplan_gebied` + `Enkelbestemming_vlak`
- Levert: welke plan(nen) gelden, type bestemming ("Wonen-4", "Centrum-1", "Gemengd-2"), plan-naam + deeplink naar RP-viewer

### Tier 2 — integreren in Fase 2

**DSO Omgevingsloket API** (sinds 1-1-2024, Omgevingswet)
- Endpoint: `https://service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v7/`
- Publieke REST API, gratis, geen auth
- Locatie-query → activiteiten + regelkwalificaties
- Nadeel: semi-gestructureerde juridische tekst. Oplossing: **Claude Haiku** inzetten als extractor om bv. "max bouwhoogte in meters" of "max aantal bouwlagen" te destilleren. Test eerst op ~10 representatieve panden om kwaliteit te valideren voordat we het live zetten.

**BGT pand-details** (PDOK BGT WFS)
- Voor detectie van bestaande niet-geregistreerde uitbouwen (BGT pand − BAG pand = vaak een aanbouw zonder BAG-registratie).

### Tier 3 — niet landelijk beschikbaar

**Welstandsnota** — per gemeente verschillend. Amsterdam, Utrecht, Rotterdam publiceren zones als WFS, kleinere gemeenten niet. Praktisch: deeplink naar gemeente-welstand.

**Volledige vergunningvrij-wizard** — 10+ voorwaarden uit Bbl art. 2.27-2.29. Juridisch riskant om automatisch "vergunningvrij" te claimen. Pragmatische oplossing: "aannemelijk vergunningvrij" + deeplink naar de officiële Vergunningcheck op omgevingsloket.nl.

## UI-blokken (voorstel Sectie 10)

### Blok 1 — Kavel-analyse

Perceel X m² · pand Y m² · onbebouwd (X-Y) m² · waarvan achtererf ≈Z m². Mini-bar toont bebouwingspercentage.

### Blok 2 — Bouwkundige status (chips)

- 🏛️ Rijksmonument: ja/nee (uit woning_extras — al aanwezig)
- 🏛️ Beschermd gezicht: ja/nee (uit RCE-BPG)
- 📋 Bestemmingsplan: naam + type + [viewer-link]

### Blok 3 — Concrete mogelijkheden (grid van 4 cards)

| Kaart | Concreet |
|---|---|
| ✅ **Uitbouw achter** | "max {diepte} m diep × {breedte} m breed = {m²} extra. Plat dak ≤ 3 m. Waarschijnlijk vergunningvrij." |
| ⚠️ **Optopping** | "BP-bouwhoogte {H} m; huidig pand {h} m → {H−h} m ruimte. Met vergunning." |
| ⚠️ **Dakkapel** | "Achterkant vergunningvrij ≤1,75 m. Voorkant vergunning (+ welstand bij beschermd gezicht)." |
| 🏡 **Tuinhuis** | "Achtererf >50 % onbebouwd → tot 30 m² vergunningvrij." |

### Blok 4 — Actie-buttons

- [Vergunningcheck omgevingsloket.nl ↗] — pand-ID voorgevuld
- [Bestemmingsplan op kaart ↗] — perceel-ID voorgevuld

## Lokale geometrie-berekeningen (Shapely)

**Onbebouwd terrein** = perceel-polygoon − pand-polygoon (shapely.difference)

**Voor/achter detectie**:
- BAG levert het entrypoint-coord (adres-punt, meestal aan de voordeur)
- Zijde van het pand dichtst bij entrypoint = voorzijde
- Tegenovergelegen zijde = achterzijde
- Heuristiek faalt in ~5 % gevallen bij hoekpanden of doorzonwoningen → disclaimer

**Achtererfgebied** = onbebouwd terrein aan de achterzijde, geclipped binnen het perceel

**Uitbouw-diepte max** = diepte achtererfgebied − 1 m (burenrecht: minimum afstand tot erfgrens voor niet-transparante bouwwerken)

**Breedte uitbouw max** = pand-breedte tussen de twee zij-erfgrenzen (rijtjeshuis: vaak volledige breedte; tussenwoning: vaak alleen het achter-verlengde)

## Beslisboom (drijft Blok 3)

```
uitbouw_achter:
  if rijksmonument: ❌ "monumenten-vergunning vereist"
  elif beschermd_gezicht: ⚠️ "vergunning + welstand verplicht"
  elif achtererf_diepte < 2m: ❌ "geen ruimte"
  elif achtererf_diepte >= 4m and achtererf_breedte >= 3m:
       ✅ "{diepte}×{breedte}={m²} m² — aannemelijk vergunningvrij"
  else:
       ⚠️ "{m²} m² maar diepte/breedte krap — check Bbl-regels"

optopping:
  if rijksmonument: ❌
  bp_bouwhoogte_max = extract via Claude Haiku uit DSO-regels
  if pand_hoogte + 3 <= bp_bouwhoogte_max: ✅ "ruimte voor 1 extra laag"
  else: ❌ "BP staat extra hoogte niet toe"

dakkapel_achter:
  if rijksmonument or beschermd_gezicht: ⚠️ "vergunning"
  else: ✅ "vergunningvrij ≤1,75 m hoog"

tuinhuis:
  onbebouwd_pct = (perceel-pand) / perceel * 100
  if onbebouwd_pct >= 50%: ✅ "tot 30 m² vergunningvrij"
  elif >= 25%: ⚠️ "tot 4 m² vergunningvrij"
  else: ❌
```

## Claude Haiku — BP-bouwhoogte extractor

De DSO-API retourneert regels als ongestructureerde juridische tekst. Voorbeeld:
> "Binnen het bestemmingsvlak mag de bouwhoogte niet meer bedragen dan 9 meter, gemeten vanaf peil, met dien verstande dat ondergeschikte bouwdelen zoals schoorstenen hiervan afgezonderd zijn."

Haiku wordt gebruikt met een strikte prompt:
```
Extract from the following Dutch planning rule:
- max_bouwhoogte_m: integer or null
- max_bouwlagen: integer or null
- kapverplichting: boolean or null
- plat_dak_toegestaan: boolean or null
Return ONLY valid JSON. No explanation.

Rule text: {regeltekst}
```

**Validatieprotocol** (voor go-live):
1. Selecteer 10 representatieve panden (mix: centrum, vooroorlog, naoorlog, landelijk, beschermd gezicht)
2. DSO-API response handmatig valideren → grond-waarheid vaststellen
3. Haiku-extractie uitvoeren, vergelijken
4. Accept-criterium: 90%+ correct op `max_bouwhoogte_m`. Als lager → prompt iteratie óf feature naar "Indicatief, niet gegarandeerd"

**Schatting kosten**: ~100 tokens input + ~30 tokens output per extractie ≈ €0,0001. Met caching op plan-ID (niet op pand) is de totale kostenvoetafdruk te verwaarlozen.

## Implementatie-plan

### Fase 1 — MVP (1 dag)

1. `apps/api/adapters/verbouwing.py`:
   - `async def fetch_verbouwing(lat, lon, rd_x, rd_y, bag_pand_id) -> VerbouwingsInfo`
   - 3 parallel calls: BRK-perceel, RCE-BPG, PDOK-Plangebieden
   - Shapely-berekening perceel−pand
2. Nieuw endpoint `/verbouwing?…` in `main.py`, 30-dagen cache (alle drie bronnen veranderen zelden)
3. Frontend `s-verbouwing` sectie:
   - Blok 1 (kavel) + Blok 2 (chips) + Blok 4 (links)
   - Géén beslisboom-cards nog
4. Deploy + live verify op 3 adressen (grachtenpand, rijtjeshuis, vrijstaand)

### Fase 2 — concrete cards (1-2 dagen)

1. Claude Haiku integratie voor DSO-regeltekst extractie
2. Test-suite: 10 panden handmatig valideren
3. Beslisboom implementeren → Blok 3 met 4 cards
4. Voor/achter detectie heuristiek + disclaimer
5. Deeplink naar Omgevingsloket Vergunningcheck met perceel-ID voorgevuld

## Eerlijke beperkingen (expliciet in UI)

- BP-regels uit DSO zijn semi-gestructureerd; Haiku-extractie is indicatief
- Voor/achter detectie faalt in ~5 % gevallen (hoekpanden, doorzonwoningen)
- Welstandsregels zijn niet landelijk beschikbaar — alleen gemeente-deeplink
- Alle mogelijkheden indicatief — definitief altijd via omgevingsloket.nl/vergunningcheck

## Extra latency

3 async calls (BRK + RCE-BPG + PDOK-Plangebieden) à ~200-500 ms, parallel → ~500 ms totaal. Plus Shapely-berekening lokaal (~5 ms). Sectie komt via lazy endpoint (zoals /klimaat en /bereikbaarheid) zodat de main /scan niet wacht.
