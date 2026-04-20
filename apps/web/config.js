// Runtime-configuratie voor de frontend.
// - Lokaal (FastAPI servt alles): laat THUISSCAN_API_BASE leeg.
// - Op Netlify/etc. met separate backend: zet deze naar jouw backend-URL,
//   bv. 'https://thuisscan-api.fly.dev' (zonder trailing slash).
window.THUISSCAN_API_BASE = "";
