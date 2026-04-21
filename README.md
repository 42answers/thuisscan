# Thuisscan

Eén adres → volledig woning- en buurtprofiel uit Nederlandse open data.

Zie `wiki/questions/Thuisscan App Voorstel.md` voor het product-ontwerp.

## Structuur

```
thuisscan/
├── apps/
│   ├── api/              # FastAPI backend
│   │   ├── main.py       # entrypoint
│   │   └── adapters/     # één module per databron
│   └── web/              # Next.js frontend (nog leeg)
└── fixtures/             # testadressen met verwachte outputs
```

## Fase 1 — PDOK spike

```bash
cd apps/api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Testen:
```bash
curl "http://localhost:8000/lookup?q=Damrak+1+Amsterdam"
```

## Frontend-configuratie (config.js)

De frontend leest runtime-config uit `apps/web/config.js`. Deze file is
**gitignored** omdat hij API-keys kan bevatten. Bij een fresh clone:

```bash
cp apps/web/config.example.js apps/web/config.js
# En vul je eigen keys in.
```

### Google Maps Embed API (optioneel, voor inline Street View)

Zonder key vallen de Street View / Satelliet tabs terug op een externe
Google Maps-link. Met een key zie je de beelden direct in de app.

1. Ga naar https://console.cloud.google.com → nieuw project
2. Activeer **"Maps Embed API"** (geen betaalmethode nodig, volledig gratis
   voor Embed — alleen de JavaScript Maps API kost geld bij overuse)
3. **APIs & Services → Credentials → Create API key**
4. **Restrict key → HTTP referrers**: zet alleen jouw domeinen
   (bv. `http://localhost:8765/*`, `https://thuisscan.netlify.app/*`) —
   dit voorkomt dat de key misbruikt wordt als hij uit de JS gelekt wordt
5. Plak de key in `apps/web/config.js` onder `GOOGLE_MAPS_API_KEY`

## Kadaster WOZ API-key aanvragen (optioneel)

Zonder key toont de app **buurt-gemiddelde WOZ** uit CBS. Met een Kadaster-key krijg je de **exacte WOZ-waarde per pand** inclusief historie (3-5 jaargangen).

### Aanvraag (gratis, ~500 calls/dag per key)

1. Ga naar: https://www.kadaster.nl/zakelijk/producten/adressen-en-gebouwen/woz-api-bevragen
2. Klik op **"Aanmelden WOZ API Bevragen"** (linkt naar `formulieren.kadaster.nl/aanmelden_lv_woz`)
3. Vul in:
   - **KvK-nummer** van jouw organisatie
   - **Doel**: bijv. *"Data-ontsluiting voor een bewoners-check-MVP"*
   - Contactgegevens
4. Kadaster beoordeelt de aanvraag en stuurt meestal **binnen 1-5 werkdagen** een e-mail met de API-key
5. Voeg de key toe aan `apps/api/.env`:
   ```
   KADASTER_WOZ_API_KEY=<jouw-key>
   ```
6. Herstart de server — de adapter pikt het automatisch op. UI-label verandert van "WOZ (buurtgemiddelde)" naar **"WOZ (dit pand)"**

### Quota

- Standaard ~500 requests/dag per key
- In de app is een **30-dagen-cache** ingebouwd (WOZ-waarden muteren jaarlijks), dus 1 request per adres per maand
- Bij overschrijding blijft de buurt-fallback werken zonder foutmelding

### Docs

- [Swagger UI](https://kadaster.github.io/WOZ-bevragen/swagger-ui) — alle velden van de API
- [Getting started](https://kadaster.github.io/WOZ-bevragen/getting-started)
- Broncode adapter: `apps/api/adapters/kadaster_woz.py`

## Bronnen (22 overheids-API's)

| Adapter | Bron | Status |
|---|---|---|
| `pdok_locatie` | PDOK Locatieserver (geocoding) | fase 1 |
| `bag` | Kadaster BAG OGC API Features | fase 2 |
| `cbs` | CBS OData v4 (Kerncijfers W&B) | fase 2 |
| `rvo_ep` | RVO EP-Online (energielabels) | fase 3 |
| `politie` | Politie Open Data via CBS OData | fase 3 |
| `rivm_lki` | RIVM Atlas Luchtkwaliteit | fase 4 |
| `rivm_geluid` | RIVM 3D Geluid WMS/WFS | fase 4 |
| `cas_klimaat` | Klimaateffectatlas WFS | fase 4 |
| `leefbaarometer` | Leefbaarometer WMS/WFS | fase 5 |
