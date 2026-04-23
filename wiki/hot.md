---
type: meta
title: "Hot cache"
updated: 2026-04-23
tags: [meta, hot]
---

# Hot cache

Huidige focus-onderwerpen voor deze sessie-reeks.

## Actief in ontwikkeling
- **[[rapport-pdf-template-v2]]** — productie-versie live op buurtscan.fly.dev. Iteratief verfijnen: Haiku-intro-zinnen per hoofdstuk, server-side PDF (Chromium in Fly-image), pre-cache op /scan-call.

## Recent opgeleverd
- **[[rapport-pdf-template-v2]]** — `/rapport?q=…` endpoint + frontend-knop. Editorial design, 13 hoofdstukken, OSM + Kadaster kaarten op cover, bullet-samenvatting, per-stat NL-vergelijking, alle URLs klikbaar (scholen forced to scholenopdekaart.nl)
- **[[leefbaarometer-trend-integratie]]** — cover toont nu 2-jaars + 10-jaars trend naast huidige score; threshold ≥1, beide richtingen zichtbaar, "stabiel maar beweging"-override
- Sectie 10 "Verbouwingsmogelijkheden" — kavel-analyse + chips + 4 beslisboom-cards + deeplinks
- Haiku-extractor met 10-panden testset (`apps/api/tests/bp_extractor_testset.py`)
- Gemeentelijk-monument-adapter (Amsterdam direct via api.data.amsterdam.nl; overig: Google-search deeplink)
- OV-reistijd kalibratie tegen 9292
- Lazy-loading voor woning-extras + klimaat + bereikbaarheid + voorzieningen + verbouwing
