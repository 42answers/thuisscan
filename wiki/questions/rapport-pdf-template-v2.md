---
type: synthesis
title: "PDF-rapport template v2.0 — backend endpoint + frontend-knop"
created: 2026-04-23
updated: 2026-04-23
tags:
  - rapport
  - pdf
  - design
  - sectie-output
status: developing
related:
  - "[[verbouwingsmogelijkheden-ontwerp]]"
  - "[[verbouwings-beslisboom-fase-2a]]"
---

# PDF-rapport template v2.0 — buurt-focus, klikbaar, productie-klaar

## Wat het is

Een editorial-stijl 13-hoofdstuk PDF-rapport voor één adres, gegenereerd
door `apps/api/rapport_template.py`, beschikbaar via het backend-endpoint
`GET /rapport?q=<adres>` en geopend met een knop in de scan-UI.

De gebruiker klikt **"📄 Volledig rapport openen"** boven de scan-titel,
de browser opent een nieuwe tab met het volledige HTML-rapport, en de
gebruiker kan via Cmd+P / Ctrl+P naar PDF exporteren.

## Architectuur

```
buurtscan.com
    └── frontend (app.js renderPrintKnop)
        └── opent /rapport?q=… in nieuwe tab

backend (main.py rapport_endpoint)
    ├── orchestrator.scan(q)                  # snelle CBS+BAG+lucht+geluid
    ├── orchestrator.fetch_woz_pand(vbo_id)   # WOZ-Waardeloket
    ├── orchestrator.fetch_voorzieningen(…)   # OSM Overpass
    ├── orchestrator.fetch_klimaat_section(…) # Klimaateffectatlas
    ├── orchestrator.fetch_bereikbaarheid_section(…)
    ├── orchestrator.fetch_woning_extras_section(…)
    ├── orchestrator.fetch_verbouwing_section(…)
    ├── static_maps.fetch_streetmap_png(lat,lon)   # OSM tile-stitcher
    └── static_maps.fetch_perceel_png(rd_x,rd_y)   # PDOK Kadaster WMS

         ↓ alles parallel, ~17s cold

rapport_template.render_html(data) → str
         ↓
HTMLResponse 200 (~700KB met embedded base64-kaarten)
```

## Design-keuzes

### Typografie
- **Source Serif Pro** voor titels en grote getallen, met cursief accent
  ("dit adres", "klimaatverandering", "wonen")
- **Inter** voor lopende tekst en chips
- **IBM Plex Mono** voor labels, sources en pagina-chrome
- Accent-kleur `#1f4536` (donkergroen-teal)

### Layout
- A4 portrait, padding 18mm horizontaal, 18mm boven, 16mm onder
- Elke `.page` is `min-height: 297mm`, mag overrollen naar volgende
  fysieke pagina
- **`page-break-before: always`** zorgt dat nieuw hoofdstuk = nieuwe
  fysieke pagina (cover uitgezonderd)
- `@page { @bottom-right { content: counter(page) } }` geeft
  automatische pagina-nummer rechtsonder bij elke fysieke pagina

### Cover
- Hero: "Wat moet je weten over **deze buurt**?"
- 2 kaarten naast elkaar: OSM-straatkaart links, Kadaster perceel rechts
- Meta-strip: rapportnr · geldigheid · 22 bronnen
- Samenvatting als BULLET-LIJST per onderwerp:
  - PAND: pand-karakter + bouwjaar + opp + buurt
  - BUURT: Leefbaarometer-score + verdict
  - WAARDE: WOZ + m² + trend
  - BEREIK: trein/tram/bus/snelweg met namen tussen haakjes
  - AANDACHT: criminaliteit, monument, klimaat, fundering

### Per-stat ref-velden
Elke statistiek toont onder het getal:
- chip-badge (good/warn/neutral) met `chip_text`
- regel "vs NL: X" met `nl_gemiddelde`
- italic muted regel met `betekenis`

### Voorzieningen-formaat
TYPE eerst, dan naam: "**Bushalte**: Corantijnstraat — 0,1 km".
Klikbaar via Google Maps.

### Onderwijs
- Scholen: ALLE URLs forceren naar scholenopdekaart.nl (via
  `school_url()` helper)
- Kinderopvang: alle URLs naar LRK
- GGD-tekst expliciet: *"Klik op de naam → LRK-portaal toont meest
  recente onderzoeken. Voor kinderopvang bestaat geen landelijke
  geaggregeerde oordelen-dataset."*

### Verbouwen
- 4 cards: uitbouw / dakkapel / tuinhuis / zonnepanelen
- Monument-status gededupliceerd (was 3× in eerdere versie)
- "Beschermd stadsgezicht" ipv "beschermd gezicht"
- "Bouwlagen huidig" ipv jargon "PAND-MASSA"
- Deeplinks naar Regels op de Kaart + Vergunningcheck Omgevingsloket

## Wat NIET in zit (bewust)

- **Optopping-card**: max-bouwhoogte data is sinds bruidsschat 1-1-2024
  niet structureel beschikbaar (RP v4 = 5%, DSO Presenteren = 0/1365
  getallen). Deeplink naar Regels op de Kaart vervangt dit.
- **GGD-oordelen kinderopvang**: geen landelijke dataset; LRK-link is
  de pragmatische route.
- **Claude Haiku context-zinnen**: nog niet geintegreerd. Zou per
  hoofdstuk een "so what"-zin kunnen genereren met de data van dat
  hoofdstuk. Follow-up.

## Bronnen die het rapport gebruikt (22 totaal)

Kadaster (BAG, BRK, WOZ-Waardeloket, Wkpb, BRT, Kadastrale Kaart) ·
PDOK Locatieserver · CBS Kerncijfers Wijken & Buurten · Politie Open
Data · RIVM (lucht, geluid) · Klimaateffectatlas (KNMI/CAS) ·
Leefbaarometer 3.0 (BZK) · DUO Basisscholen · Onderwijsinspectie ·
LRK Kinderopvang · OpenStreetMap (Overpass) · 3D BAG (TU Delft) ·
RVO EP-Online · Kiesraad · DSO Omgevingsdocumenten Presenteren v8 ·
DSO Toepasbare Regels v3 · Ruimtelijke Plannen v4 · RCE Townscapes ·
Anthropic Claude Haiku (BP-tekst extractie) · Amsterdam Monumenten API.

## Productie-status

- Endpoint live: https://buurtscan.fly.dev/rapport?q=...
- Cold response: ~17 sec (orchestrator-calls + 2 kaart-fetches parallel)
- Output: ~700 KB HTML met embedded base64-kaarten
- PDF na browser-print: 13-15 A4-pagina's

## Gewenste vervolgstappen

1. **Server-side PDF**: Chrome headless integreren zodat /rapport.pdf
   direct een PDF-binary retourneert (i.p.v. HTML voor browser-print).
   Vereist Chromium in de Fly-image (~250 MB extra).
2. **Claude Haiku intro-zinnen**: per hoofdstuk een lead-in die de
   "so what" voor dit specifieke adres samenvat.
3. **Async pre-cache**: bij `/scan`-call alvast `/rapport`-data
   warmlopen zodat de print-knop instant opent.
4. **Custom logo / branding** voor de "B" placeholder.

## Code-pointers

- `apps/api/rapport_template.py` — render-engine (1860 regels)
- `apps/api/adapters/static_maps.py` — OSM stitcher + Kadaster WMS
- `apps/api/main.py:rapport_endpoint` — `/rapport?q=…` route
- `apps/web/app.js:renderPrintKnop` — frontend-knop
- `apps/web/styles.css` (laatste blok) — `.rapport-knop` styling
