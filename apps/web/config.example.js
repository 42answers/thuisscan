// Template voor config.js. Kopieer naar `config.js` en vul de keys in.
// Zonder keys werkt de app nog steeds — features met externe providers
// vallen terug op links naar het externe domein (nieuw tabblad).

// Backend-URL (leeg = same-origin; op Netlify: je Fly.io/Railway URL)
window.THUISSCAN_API_BASE = "";

// Google Maps Embed API voor inline Street View + Satelliet.
// Aanvragen (~5 min, gratis):
//   1. https://console.cloud.google.com → nieuw project
//   2. "Maps Embed API" activeren (geen betaalmethode nodig)
//   3. APIs & Services → Credentials → Create API key
//   4. Key beperken: HTTP referrers → alleen jouw domein(en)
//      (localhost:8765, thuisscan.netlify.app, etc.)
//   5. Plak key hieronder
window.GOOGLE_MAPS_API_KEY = "";
