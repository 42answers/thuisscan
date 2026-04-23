---
type: synthesis
title: "Verbouwings-beslisboom Fase 2a — Haiku + 3 cards live"
created: 2026-04-22
updated: 2026-04-22
question: "Hoe werkt de Fase 2a-beslisboom voor verbouwingsmogelijkheden, en welke accuracy levert Claude Haiku op bestemmingsplan-regels?"
answer_quality: solid
status: evergreen
tags:
  - verbouwing
  - open-data
  - llm
  - haiku
  - validatie
  - feature-live
related:
  - "[[verbouwingsmogelijkheden-ontwerp]]"
---

# Verbouwings-beslisboom Fase 2a — Haiku + 3 cards live

Sectie 10 van Buurtscan levert nu concrete verbouwingsmogelijkheden per adres, in 4 beslisboom-cards. Drie cards (Uitbouw-achter, Dakkapel, Tuinhuis) werken volledig op bestaande data; de vierde (Optopping) wacht op DSO-integratie. Claude Haiku is geïntegreerd als extractor voor ongestructureerde plan-tekst en valideert op 97,1% overall, 100% op kritieke velden.

## Haiku-validatieresultaten (10 samples)

| Veld | Accuracy |
|---|---|
| max_bouwhoogte_m | 100% |
| max_goothoogte_m | 100% |
| max_bouwlagen | 100% |
| max_bebouwingspercentage | 100% |
| bestemming | 100% |
| kap_verplicht | 90% |
| plat_dak_toegestaan | 90% |
| **OVERALL** | **97,1%** |

Samples zijn representatief gekozen over 5 as-dimensies: stad/platteland, centrum/nieuwbouw, rijtjes/vrijstaand/appartement, beschermd/niet, moderne omgevingsplan-stijl vs traditioneel BP. Eén sample (Drenthe, vrijstaande woning) veroorzaakte de 2 mismatches: Haiku concludeerde uit "nokhoogte + goothoogte" dat kap verplicht was, terwijl de ground-truth `None` markeerde. Dat is een redelijke-maar-assertieve interpretatie; niet fout genoeg om de regel aan te passen.

Model: `claude-haiku-4-5` via Anthropic API. Kosten per extractie ~$0,002 (input ~1500 tokens, output <100). Latency 1-4 seconden per call. Cache 30 dagen op plan-ID maakt de kosten verwaarloosbaar.

## Architectuur

```
Frontend → GET /verbouwing
     ↓
orchestrator.fetch_verbouwing_section()
     ↓
_cached_fetch_verbouwing (30d cache, key v2)
     ↓
verbouwing.fetch_verbouwing() — parallel:
     ├─ BRK-Publiek WFS (kadastraal perceel)
     ├─ RCE WFS rce:Townscapes (beschermd gezicht)
     ├─ BAG-WFS pand-polygoon (RD)
     └─ gemeentelijk_monument.fetch_gemeentelijk_monument()
          └─ Amsterdam: api.data.amsterdam.nl/v1/monumenten
          └─ elders: Google-search deeplink

     ↓ (Shapely perceel ∩ pand; perceel − pand = onbebouwd)

_build_verbouwing(v) → serialize
     + _build_mogelijkheden(v) → beslisboom 4 cards

     ↓

Frontend render: kavel-tiles + chips + 4 decision cards
```

Bestanden:
- `apps/api/adapters/bp_extractor.py` — Haiku-wrapper met strikte JSON-only prompt
- `apps/api/adapters/verbouwing.py` — BRK + RCE + BAG + Shapely
- `apps/api/adapters/gemeentelijk_monument.py` — per-gemeente dispatcher
- `apps/api/orchestrator.py::_build_mogelijkheden()` — beslisboom-logica
- `apps/api/tests/bp_extractor_testset.py` — 10-panden validatie-suite
- `apps/web/app.js::renderMogelijkheidCard()` — frontend card-render
- `apps/web/styles.css` — `.verb-card-{good,neutral,warn,unknown}`

## Beslisboom-logica

Vier mogelijkheden met 4 niveaus (`good` / `neutral` / `warn` / `unknown`). Hiërarchie van beperkingen:

1. **Rijksmonument**: vrijwel alles → `warn` (monumenten-vergunning)
2. **Beschermd stadsgezicht**: uitbouw+tuinhuis → `warn/neutral`, dakkapel → `neutral` (vergunning + welstand)
3. **Appartement** (BAG-pand > 3× perceel): uitbouw → `warn` (VvE)
4. **Achtererf-criteria** (Bbl vergunningvrij-regels):
   - Diepte ≥ 4 m → uitbouw `good` (~m² schatting)
   - 2-4 m → `neutral` (krap)
   - < 2 m → `warn` (geen ruimte)
5. **Tuinhuis**: onbebouwd_pct ≥ 50% → `good` (tot 30 m²); ≥ 25% → `neutral` (tot 4 m²); anders → `warn`
6. **Dakkapel**: achterkant vergunningvrij tenzij monument/beschermd → `good`
7. **Optopping**: tijdelijk `unknown` voor alle niet-monument/niet-appartement panden → wacht op DSO-API

## Live validatie op 4 pand-typen

| Adres | Type | Uitbouw | Dakkapel | Tuinhuis | Optopping |
|---|---|---|---|---|---|
| Sixlaan 4 Hillegom | vrijstaand, ruime tuin | ✅ ~14 m² | ✅ | ✅ | ⚪ BP |
| Hoofschestraat 1 Grave | beschermd gezicht | 🟥 vergunning | 🟧 | 🟧 | ⚪ BP |
| 2e Weteringdwarsstraat 71 | rijksmonument | 🟥 monument | 🟥 | 🟧 | 🟥 |
| Damrak 1 Amsterdam | appartement, beschermd | 🟥 | 🟧 | 🟧 | ⚪ BP |

De resultaten matchen de verwachte juridische realiteit — rijksmonument krijgt overal de zwaarste waarschuwing, beschermd gezicht krijgt een lichte waarschuwing op uitbouw en dakkapel, vrijstaand in open gebied krijgt drie groene lampen.

## DSO API — bevindingen uit onderzoek

1. **Publiek toegankelijk**: "in principe mag iedereen de services in het digitaal stelsel gebruiken, inclusief ontwikkelaars van nieuwe applicaties". Ook particulieren en bedrijven. Geen overheidsorgaan vereist.
2. **Gratis**: "all data and services are free to use".
3. **Fair Use Policy** geldt — redelijke request-volumes, geen misbruik.
4. **V7 uit fasering** per okt 2025, unsupported tot feb 2026; **v8** is actueel: `service.omgevingswet.overheid.nl/publiek/omgevingsdocumenten/api/presenteren/v8/`
5. **API-key via aanvraagformulier** op developer.omgevingswet.overheid.nl — enkele werkdagen verwerkingstijd.
6. **Rate limit** 200 req/sec (ruim).

## Wat nog wacht (Fase 2b)

1. Roel vraagt DSO-key aan via het aanvraagformulier op developer.omgevingswet.overheid.nl
2. Secret opgezet via `fly secrets set DSO_API_KEY="..." -a buurtscan`
3. Nieuwe adapter `dso.py` haalt regels per locatie (coord → regeling → geo-zoek binnen regeling)
4. bp_extractor.py parset de gevonden regeltekst → BPRegels
5. Optopping-card krijgt echte logica:
   - BAG pand-hoogte (3D-BAG of geschat uit bouwjaar+type) vs bp.max_bouwhoogte_m
   - Als delta ≥ 3 m → `good` ("~1 extra bouwlaag mogelijk met vergunning")
   - Anders → `warn` ("BP staat extra hoogte niet toe")

## Belangrijke ontwerpbeslissingen

- **Conservatief bij onzekerheid**: bp_extractor retourneert `None` liever dan gokken. Frontend toont `unknown`-kleur i.p.v. vals-positieve `good`. Juridisch claim "aannemelijk vergunningvrij" ipv "zeker" voorkomt aansprakelijkheid.
- **Shapely perceel ∩ pand**: essentieel voor rijtjeshuizen en gestapelde bouw waar het BAG-pand over meerdere percelen loopt. Levert een werkelijk vertaalbaar getal "jouw woning-footprint" i.p.v. de grootte van het hele rijtje.
- **Amsterdam-specifieke monumentenroute**: er bestaat geen landelijk gemeentelijk-monument-register. Per-gemeente dispatcher: grote gemeenten met open data (Amsterdam nu, Utrecht/Rotterdam/Den Haag later) krijgen directe check; andere krijgen een Google-search-deeplink i.p.v. onderhouds-intensieve URL-tabel.
- **Lazy endpoints voor zware calls**: /verbouwing (~600-1100ms), /klimaat, /bereikbaarheid, /voorzieningen, /woning-extras staan buiten de kritieke /scan-path. Hoofdpagina rendert in <1,5s; secties vullen zich na.
- **Haiku boven Sonnet**: voor gestructureerde JSON-extractie uit NL-tekst is Haiku ruim voldoende (100% op getallen). Kostprijs ~$0,002 per extractie; met 30-dagen cache op plan-ID nadert dat de kosten-nul.
