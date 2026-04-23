---
type: synthesis
title: "Leefbaarometer trend-integratie (2-jaar + 10-jaar)"
created: 2026-04-22
updated: 2026-04-22
tags:
  - leefbaarometer
  - trend
  - cover
  - buurt
  - wms
status: developing
related:
  - "[[verbouwingsmogelijkheden-ontwerp]]"
---

# Leefbaarometer trend-integratie

De cover (bovenaan de scan) toonde alleen de actuele Leefbaarometer-score (1-9).
Een puntmeting zegt echter weinig over de koers van een buurt: een "6 goed" nu
die tien jaar geleden een "8" was, is structureel aan het afglijden — en dat
signaal hoort zichtbaar te zijn. We integreren daarom 2-jaars (2022→2024) en
10-jaars (2014→2024) ontwikkelings-layers van Leefbaarometer 3.0 (BZK), op
hetzelfde 100m-grid als de huidige score.

## Bron & endpoints

Leefbaarometer 3.0, peiljaar 2024, WMS op `https://geo.leefbaarometer.nl/wms`.
Er zijn 4 layers die we parallel raadplegen via GetFeatureInfo:

| Layer | Wat |
|-------|-----|
| `lbm3:clippedgridscore24` | Huidige score (100m-grid rond adres) |
| `lbm3:buurtscore24` | Zelfde score geaggregeerd naar CBS-buurt (diagnostisch — grid vs buurt geeft "gunstige uithoek"-signaal) |
| `lbm3:clippedgridontwikkeling22_24` | 2-jaars-ontwikkeling, klasse 1-9 |
| `lbm3:clippedgridontwikkeling14_24` | 10-jaars-ontwikkeling, klasse 1-9 |

De trend-layers gebruiken dezelfde 1-9 klasse-schaal als de score-layer, maar
met een andere betekenis:

- `1` = sterk verslechterd
- `5` = geen verandering (stabiel)
- `9` = sterk verbeterd

Naast de klasse levert de WMS een continuous `score`-veld dat fungeert als
`raw_delta` (positief = vooruit). Elke trend-layer levert ook de 5
sub-dimensies (`kwon`, `kfys`, `kvrz`, `ksoc`, `konv`) — cruciaal, want dat is
hoe je op de cover kunt laten zien waar de beweging vandaan komt.

## Adapter-contract

`apps/api/adapters/leefbaarometer.py` haalt alle 4 layers in één
`asyncio.gather` op (~200-400 ms wall-clock i.p.v. sum). Failure op een
trend-layer is silent: `ontwikkeling_recent` / `ontwikkeling_lang` vallen
terug naar `None`, score-layer blijft werken.

```python
@dataclass
class Ontwikkeling:
    periode: str            # "2014-2024"
    score: int              # 1-9 totaal-klasse
    label: str              # "verbeterd" / "stabiel" / "verslechterd"
    raw_delta: float        # continuous afwijking (positief = vooruit)
    per_dimensie: dict      # {key: klasse} voor won/fys/vrz/soc/onv
```

## Serialisatie naar UI (`_serialize_ontwikkeling`)

De orchestrator (`apps/api/orchestrator.py`) vertaalt een `Ontwikkeling` naar
een UI-klaar dict per horizon (`"2 jaar"` / `"10 jaar"`). Drie ontwerpkeuzes
zijn het noteren waard:

### 1. Chip-niveau volgt totaal-label
`label == "verbeterd"` → chip `good` (groen), `"verslechterd"` → `warn` (rood),
anders `neutral` (grijs). Hiermee zie je in één oogopslag of de buurt
opbouwend of afbrokkelend is.

### 2. Threshold = |klasse − 5| ≥ 1, niet ≥ 2
Eerste implementatie filterde dimensies met `|Δ| ≥ 2`. Gevolg: te veel
stilte. Bij klasse 6 of 4 is er al een **reële** verandering die consistent
moet zijn met een totaal dat "verbeterd" of "verslechterd" scoort. We
verlaagden de drempel naar 1. Gradatie is afgeleid:

| abs(Δ) | Gradatie |
|--------|----------|
| 1 | licht |
| 2 | matig |
| 3-4 | sterk |

### 3. BEIDE kanten tonen, altijd
Een 10-jaars trend kan tegelijk voorzieningen (+) en overlast (-) laten zien.
Oude versie koos alleen de "sterkste_verandering" (één item) — dan verdween
het negatieve signaal achter het positieve bij gelijke delta. Nieuwe
serialisatie geeft een `veranderingen`-lijst: top 2 verbeteringen + top 2
verslechteringen, gesorteerd op magnitude. Praktisch voorbeeld: Paramaribo
10j toont "Overlast & veiligheid licht verbeterd (6/9)" apart van "Wonen
matig verslechterd (3/9)".

### 4. Stabiel-met-beweging is een aparte toestand
Als `4 ≤ klasse ≤ 6` (totaal stabiel) **maar** er zijn wel significante
dimensie-veranderingen, overschrijven we de beschrijving naar:

> *"Totaal stabiel over 10 jaar, maar onder de motorkap wél beweging:"*

Zo is de chip "Stabiel" niet langer in tegenspraak met een dimensie die
"licht verslechterd" is. Legacy `sterkste_verandering` blijft gevuld voor
backward-compat van oude frontends.

## Frontend-rendering

`apps/web/app.js` → `renderCoverOntwikkeling(ontwikkeling)` +
`ontwikkelingBlock(o)`. Zit onder de score-chips op de cover, twee blocks
naast elkaar (2-kolom grid — `.cover-ontwikkeling`). Per block:

- `.trend-head`: pijl (↑/→/↓ op klasse) + horizon-label ("2 jaar" / "10 jaar")
  + chip (capitalized `label`)
- `.trend-desc`: de zin (bv. "Sterk verbeterd over 10 jaar.")
- Per dimensie een `.trend-dim` regel: pijl + label + gradatie-richting
  ("matig verslechterd") + klasse-tag ("3/9")
- `.trend-period`: de ruwe periode-tekst ("2014-2024") als onder-regel

Kleurschema sluit aan op de rest: `--strong` (groen), `--weak` (rood),
`--accent` (neutraal). Backgrounds zijn licht getint (`#f2faf5` good /
`#fcf5f3` warn / `#fafaf8` neutral).

## Waarom dit niet optioneel is

Een scan die alleen een puntmeting geeft, misleidt bij buurten die op hun
koerswisseling staan. Voorbeelden die we zagen tijdens ontwikkeling:

- **Structureel afglijdend**: buurt-klasse 6 (goed) met 10-jaars klasse 3 —
  "onvoldoende → licht verslechterd" over een decennium. Zonder trend lees
  je dit als "prima buurt".
- **Herstel na dip**: buurt-klasse 5 met recent (2j) klasse 7 — de grafiek
  wijst op actief beleid / gentrificatie. Zonder 2-jaars view mis je dat.
- **Asymmetrische beweging**: voorzieningen ↑ sterk, overlast ↑ sterk — vaak
  bij binnenstedelijke verdichting. De lezer moet beide zien om zelf te
  wegen.

## Open punten

- **Buurt-niveau trend-layer?** We halen nu alleen grid-trend. Een
  buurt-aggregatie zou het "gunstige uithoek"-signaal ook in de tijd
  beschikbaar maken.
- **Ouder historisch peiljaar?** Leefbaarometer 3.0 gaat terug tot 2014.
  Voor wijken waar de trend-breuk vóór 2014 ligt (bv. herstructurering in
  de jaren '00), zien we alleen de staart.
- **Cache-TTL**: trends wijzigen jaarlijks met het nieuwe peiljaar; huidige
  WMS-call heeft geen eigen cache-laag. Overwegen: trendresultaat mee-cachen
  met de buurtscore onder `_BUURT_TTL_S` (24u).

## Referenties in de codebase

- `apps/api/adapters/leefbaarometer.py:38-39` — layer-constants + rationale
- `apps/api/adapters/leefbaarometer.py:187-226` — `_parse_ontwikkeling`
- `apps/api/orchestrator.py:1968-2094` — `_serialize_ontwikkeling`
- `apps/web/app.js:1784-1850` — `renderCoverOntwikkeling` / `ontwikkelingBlock`
- `apps/web/styles.css:849-940` — `.cover-ontwikkeling` + `.trend-*`
