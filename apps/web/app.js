// Thuisscan frontend — vanilla JS, geen build step.
// Backend levert per indicator een {value, unit, ref:{chip_level, chip_text,
// nl_gemiddelde, norm, betekenis}}. De renderField()-helper bouwt daar een
// consistent blok van: grote waarde + chip + referentieregel + betekenis-zin.

// API_BASE resolve-volgorde:
//   1. window.THUISSCAN_API_BASE (uit config.js, overrijdbaar op Netlify)
//   2. leeg = same-origin (FastAPI servt de frontend zelf, lokaal)
//   3. localhost fallback voor dev
const API_BASE =
  (typeof window.THUISSCAN_API_BASE === 'string' && window.THUISSCAN_API_BASE)
    ? window.THUISSCAN_API_BASE
    : window.location.origin.startsWith('http://localhost:87')
    ? ''
    : 'http://localhost:8765';

// ---- DOM refs ----
const $q = document.getElementById('q');
const $suggestions = document.getElementById('suggestions');
const $result = document.getElementById('result');
const $loading = document.getElementById('loading');
const $error = document.getElementById('error');

// ---- Autocomplete ----
let suggestTimer = null;
let activeIndex = -1;
let currentCandidates = [];

$q.addEventListener('input', () => {
  clearTimeout(suggestTimer);
  const q = $q.value.trim();
  if (q.length < 3) { hideSuggestions(); return; }
  suggestTimer = setTimeout(() => fetchSuggestions(q), 180);
});

$q.addEventListener('keydown', (e) => {
  if ($suggestions.hidden) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); setActive((activeIndex + 1) % currentCandidates.length); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((activeIndex - 1 + currentCandidates.length) % currentCandidates.length); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    if (activeIndex >= 0) pickCandidate(currentCandidates[activeIndex]);
    else if (currentCandidates.length > 0) pickCandidate(currentCandidates[0]);
  } else if (e.key === 'Escape') hideSuggestions();
});

document.addEventListener('click', (e) => {
  if (!$suggestions.contains(e.target) && e.target !== $q) hideSuggestions();
});

async function fetchSuggestions(q) {
  try {
    const r = await fetch(`${API_BASE}/suggest?q=${encodeURIComponent(q)}&rows=6`);
    if (!r.ok) return;
    const data = await r.json();
    currentCandidates = data.candidates || [];
    renderSuggestions();
  } catch (_) {}
}

function renderSuggestions() {
  if (currentCandidates.length === 0) { hideSuggestions(); return; }
  $suggestions.innerHTML = currentCandidates
    .map((c, i) => `<li role="option" data-idx="${i}">${escape(c.weergavenaam)}</li>`)
    .join('');
  $suggestions.hidden = false;
  activeIndex = -1;
  [...$suggestions.children].forEach((li) => {
    li.addEventListener('click', () => pickCandidate(currentCandidates[+li.dataset.idx]));
  });
}

function setActive(i) {
  activeIndex = i;
  [...$suggestions.children].forEach((li, idx) =>
    li.setAttribute('aria-selected', idx === i ? 'true' : 'false')
  );
}
function hideSuggestions() { $suggestions.hidden = true; activeIndex = -1; }
function pickCandidate(c) { $q.value = c.weergavenaam; hideSuggestions(); runScan(c.weergavenaam); }

// ---- Scan runner ----
async function runScan(query) {
  $error.hidden = true;
  $result.hidden = true;
  $loading.hidden = false;
  try {
    const r = await fetch(`${API_BASE}/scan?q=${encodeURIComponent(query)}`);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `API error ${r.status}`);
    }
    render(await r.json());
  } catch (e) {
    $error.textContent = `Kon adres niet scannen: ${e.message}`;
    $error.hidden = false;
  } finally {
    $loading.hidden = true;
  }
}

// ---- Render helper: consistent field-block met chip + ref + betekenis ----
// indicator-objecten uit de backend hebben: {value, unit?, ref?}
// fmt  = optionele waarde-formatter (default 'value unit')
// extra = optionele extra-regel onder de meaning (bv. 'buurt X · Y panden')
function fieldHTML(label, indicator, fmt, extra, opts) {
  if (!indicator || indicator.value == null) {
    return fieldEmpty(label);
  }
  const display = fmt ? fmt(indicator) : `${indicator.value}${indicator.unit ? ' ' + indicator.unit : ''}`;
  const ref = indicator.ref;
  const strongClass = (opts && opts.strong_class) ? ` class="${opts.strong_class}"` : '';
  // Als fmt een HTML-string teruggaf (bv. met badge-span), niet escapen.
  // Heuristiek: start met '<' = al HTML.
  const rendered = typeof display === 'string' && display.startsWith('<')
    ? display : escape(String(display));
  const parts = [`<div class="field"><span class="label">${escape(label)}</span><strong${strongClass}>${rendered}</strong>`];
  if (ref) {
    const arrow = ref.chip_level === 'good' ? '↑' : ref.chip_level === 'warn' ? '↓' : '→';
    parts.push(`<p class="chip chip-${ref.chip_level}">${arrow} ${escape(ref.chip_text)}</p>`);
    const refParts = [];
    if (ref.nl_gemiddelde) refParts.push(`NL-gemiddelde: ${escape(ref.nl_gemiddelde)}`);
    if (ref.norm) refParts.push(escape(ref.norm));
    if (refParts.length) parts.push(`<p class="refline">${refParts.join(' · ')}</p>`);
    if (ref.betekenis) parts.push(`<p class="meaning">${escape(ref.betekenis)}</p>`);
  }
  if (extra) parts.push(`<p class="hint">${extra}</p>`);
  parts.push('</div>');
  return parts.join('');
}

function fieldEmpty(label) {
  // Nette "geen data" variant — CBS zwijgt bij geheimhouding (<50 inwoners),
  // RIVM geeft geen value buiten NL, etc. Geef altijd dezelfde uitleg.
  return `<div class="field"><span class="label">${escape(label)}</span>
    <strong class="muted-strong">geen data</strong>
    <p class="hint">Niet gepubliceerd door bronhouder (vaak door geheimhouding bij zeer kleine buurten).</p>
  </div>`;
}

// ---- Render ----
function render(d) {
  setText('r-address', d.adres.display_name || '—');

  // Kaart + externe viewers (#11)
  renderMap(d);

  // Sociale betekenis-laag (#15): 3 menselijke vragen met verdict
  renderVragen(d.sociale_vragen || []);

  // Buurtnaam tonen als beschikbaar — veel begrijpelijker dan de CBS-code.
  // De code blijft in kleine grijze tag voor volledigheid.
  const buurtText = d.adres.buurt_naam
    ? `Buurt ${d.adres.buurt_naam}`
    : `Buurt ${d.adres.buurtcode || '—'}`;
  setText('r-codes',
    `Postcode ${d.adres.postcode || '—'} · ${buurtText} · Gemeente ${d.adres.gemeentecode || '—'}`);

  // Cover: Leefbaarometer-score bovenaan
  renderCover(d.cover);

  // Sectie 1: woning — alles via fieldHTML voor consistentie.
  // WOZ-waarde wordt hier getoond (buurt-gemiddelde uit CBS) i.p.v. gebruiksdoel,
  // omdat gebruiksdoel al impliciet blijkt uit bouwjaar+label; WOZ is relevanter.
  // Adres-precieze WOZ vereist PKIO-certificaat (overheidsonly) — v2.
  const w = d.woning || {};
  const labelClass = (w.energielabel && w.energielabel.value)
    ? `label-badge label-${w.energielabel.value.replace(/\+/g, 'p')}` : '';
  // WOZ: prefereer adres-specifiek (Kadaster) boven buurt-gemiddelde (CBS).
  // Als Kadaster-key ontbreekt, komt alleen het buurt-gemiddelde door.
  const wozBuurt = (d.wijk_economie && d.wijk_economie.woz) ? d.wijk_economie.woz : null;
  const wozAdres = w.woz_adres || null;
  const wozLabel = wozAdres ? 'WOZ-waarde (dit pand)' : 'WOZ-waarde (buurtgemiddelde)';
  const wozField = wozAdres || wozBuurt;
  const wozExtra = wozAdres && wozAdres.peildatum
    ? `Peildatum ${wozAdres.peildatum} · bron Kadaster WOZ`
    : (wozBuurt && wozBuurt.trend_pct_per_jaar != null ? renderTrend(wozBuurt) : null);
  renderGrid('s-woning-grid', [
    fieldHTML('Bouwjaar', w.bouwjaar, it => `${it.value}`),
    fieldHTML('Oppervlakte', w.oppervlakte,
      it => `${it.value?.toLocaleString('nl-NL')} m²`),
    fieldHTML(wozLabel, wozField, it => formatEuro(it.value), wozExtra),
    fieldHTML('Energielabel', w.energielabel,
      it => it.value ? `<span class="${labelClass}">${it.value}</span>` : 'niet geregistreerd',
      w.energielabel && w.energielabel.datum ? `Registratie: ${w.energielabel.datum}` : null),
  ]);

  // Sectie 2: wijk-economie (WOZ zit nu in sectie 1, geen duplicatie).
  const we = d.wijk_economie || {};
  const opl = we.opleiding_hoog;
  const oplExtra = opl && opl.breakdown
    ? `laag ${opl.breakdown.laag_pct ?? '?'}% · midden ${opl.breakdown.midden_pct ?? '?'}% · hoog ${opl.breakdown.hoog_pct ?? '?'}%`
    : null;
  renderGrid('s-wijk-grid', [
    fieldHTML('Gemiddeld inkomen per inwoner', we.inkomen_per_inwoner, it => formatEuro(it.value)),
    fieldHTML('Arbeidsparticipatie', we.arbeidsparticipatie),
    fieldHTML('Hoogopgeleid (hbo/wo)', opl, it => `${it.value}%`, oplExtra),
  ]);

  // Voorzieningen — lijst gesorteerd op afstand (geen ringen meer)
  renderVoorzieningenList(d.voorzieningen);

  // Sectie 3: buren — alles via fieldHTML
  const b = d.buren || {};
  const grid = [
    fieldHTML('Eenpersoonshuishoudens', b.eenpersoons),
    fieldHTML('Huishoudens met kinderen', b.met_kinderen),
    fieldHTML('Inwoners in buurt', b.inwoners,
      it => it.value.toLocaleString('nl-NL')),
    fieldHTML('Dichtheid', b.dichtheid,
      it => `${it.value.toLocaleString('nl-NL')} /km²`),
  ];
  // TK2023-verkiezing top-3 als aparte full-width row onder de grid
  if (b.verkiezing_tk2023) {
    grid.push(renderVerkiezing(b.verkiezing_tk2023));
  }
  renderGrid('s-buren-grid', grid);

  // Sectie 4: veiligheid
  const v = d.veiligheid || {};
  renderGrid('s-veiligheid-grid', [
    fieldHTML('Woninginbraken', v.woninginbraak,
      it => it.value != null ? `${it.value} per 1.000 inw` : '—'),
    fieldHTML('Totaal misdrijven (12 mnd)', v.totaal,
      it => it.value != null ? `${it.value} per 1.000 inw` : '—'),
  ]);
  setText('f-periode', v.periode ? prettyPeriode(v.periode) : '—');

  // Sectie 5: leefkwaliteit + geluid
  const l = d.leefkwaliteit || {};
  const geluidExtra = l.geluid ? renderGeluidDetail(l.geluid) : null;
  const geluidHTML = l.geluid
    ? fieldHTML('Geluid (Lden op gevel)', l.geluid, it => `${it.value} dB`, geluidExtra)
    : fieldEmpty('Geluid (Lden op gevel)').replace('—', '<span class="pending">geen data</span>');
  renderGrid('s-lucht-grid', [
    fieldHTML('PM2.5 (fijnstof, jaargem.)', l.pm25),
    fieldHTML('NO₂ (stikstofdioxide)', l.no2),
    fieldHTML('PM10 (grof fijnstof)', l.pm10),
    geluidHTML,
  ]);

  // Sectie 6: klimaat
  const k = d.klimaat || {};
  const paalrotExtra = k.paalrot && k.paalrot.buurt
    ? `Buurt ${escape(k.paalrot.buurt)} · ${k.paalrot.aantal_panden?.toLocaleString('nl-NL') || '?'} panden (worst-case scenario)`
    : null;
  renderGrid('s-klimaat-grid', [
    fieldHTML('Funderingsrisico (paalrot)', k.paalrot,
      it => it.value != null ? `${it.value}%` : '—', paalrotExtra),
    fieldHTML('Hittestress (warme nachten)', k.hittestress,
      it => it.label ? `${it.label} (klasse ${it.value}/5)` : '—'),
  ]);

  // Provenance
  const provs = d.provenance || [];
  setText('p-woning', findProv(provs, 'woning'));
  setText('p-wijk', findProv(provs, 'wijk_economie'));
  setText('p-veiligheid', findProv(provs, 'veiligheid'));
  setText('p-lucht', findProv(provs, 'leefkwaliteit'));
  setText('p-klimaat', findProv(provs, 'klimaat'));

  $result.hidden = false;
}

function renderGrid(gridId, itemsHTML) {
  const el = document.getElementById(gridId);
  if (!el) return;
  el.innerHTML = itemsHTML.filter(Boolean).join('');
}

function prettyPeriode(p) {
  if (!p) return '—';
  const m = ['jan','feb','mrt','apr','mei','jun','jul','aug','sep','okt','nov','dec'];
  return p.replace(/(\d{4})MM(\d{2})/g, (_, y, mm) => `${m[+mm - 1] || mm} ${y}`);
}

function findProv(provs, section) {
  const match = provs.find((p) => (p.section || '').includes(section));
  if (!match) return '';
  return `Bron: ${match.source}${match.peildatum ? ' · ' + match.peildatum : ''}`;
}

// ---- TK2023-verkiezing: top 3 partijen + landelijk % (full-width row) ----
function renderVerkiezing(v) {
  const top3 = v.top3 || [];
  if (top3.length === 0) return '';
  const rows = top3.map((p) => {
    const gem = p.pct_gemeente != null ? `${p.pct_gemeente}%` : '—';
    const nl = p.pct_nl != null ? `${p.pct_nl}%` : '—';
    const delta = p.delta_pct != null
      ? ` <span class="${p.delta_pct > 0 ? 'trend-good' : 'trend-warn'} trend-chip">${p.delta_pct > 0 ? '+' : ''}${p.delta_pct}</span>`
      : '';
    return `<li><strong>${escape(p.partij)}</strong> <span class="gem">${gem}</span> <span class="nl">(landelijk ${nl})</span>${delta}</li>`;
  }).join('');
  const note = v.per_gemeente_beschikbaar
    ? ''
    : '<p class="hint">Gemeente-specifieke uitslag nog niet in onze database — toont landelijke top 3.</p>';
  const electionLabel = (v.election || 'TK2025');
  const electionDate = v.date ? ` (${v.date})` : '';
  return `<div class="field field-fullwidth">
    <span class="label">Top 3 ${escape(electionLabel)}${escape(electionDate)}</span>
    <ul class="verkiezing-list">${rows}</ul>
    ${note}
  </div>`;
}

// ---- Geluid: bronnen-uitsplitsing als extra-regel (returnt inline string) ----
function renderGeluidDetail(g) {
  const b = g.per_bron || {};
  const items = Object.entries(b).filter(([_, v]) => v > 0).sort(([, a], [, b2]) => b2 - a);
  if (items.length === 0) return null;
  const labels = { wegverkeer: '🚗 weg', treinverkeer: '🚆 trein', vliegverkeer: '✈️ vlieg' };
  return 'Bronnen: ' + items.map(([k, v]) => `${labels[k] || k} ${v}dB`).join(' · ');
}

// ---- WOZ-trend mini-sparkline ----
function renderTrend(woz) {
  const series = woz.trend_series || [];
  const pct = woz.trend_pct_per_jaar;
  const arrow = pct > 0 ? '↑' : pct < 0 ? '↓' : '→';
  const color = pct >= 5 ? 'good' : pct <= -2 ? 'warn' : 'neutral';
  const sparkline = series.length >= 2
    ? `<span class="spark">${series.map(p => `<span title="${p.year}: ${formatEuro(p.woz_eur)}">${p.year}: ${formatEuro(p.woz_eur)}</span>`).join(' → ')}</span>`
    : '';
  return `<span class="trend-chip trend-${color}">${arrow} ${pct > 0 ? '+' : ''}${pct}% per jaar</span> ${sparkline}`;
}

// ---- Kaart (MapLibre GL) + externe viewer-links ----
// Gebruikt PDOK BRT Achtergrondkaart als gratis basiskaart (geen API-key).
// Pand-polygoon wordt per request uit de BAG WFS opgehaald en als overlay
// getekend. Externe viewers (Google Street View, Satelliet, BAG-viewer) staan
// rechts onder de kaart — zodat de user zelf kan inzoomen op details die we
// niet zelf kunnen tekenen (3D gebouwen, foto's).

let _map = null;
let _mapCurrentLatLon = null;  // onthoudt laatst getoonde adres voor lazy tab-loading

async function renderMap(d) {
  const el = document.getElementById('s-map');
  if (!el || !d.adres || !d.adres.wgs84) return;
  const { lat, lon } = d.adres.wgs84;
  if (!lat || !lon) { el.hidden = true; return; }
  el.hidden = false;
  _mapCurrentLatLon = { lat, lon, displayName: d.adres.display_name };

  // BAG 3D-viewer link (externe, altijd nieuw tabblad — grote Kadaster-app)
  document.getElementById('m-bag3d').href =
    `https://bagviewer.kadaster.nl/lvbag/bag-viewer/index.html#?searchQuery=${encodeURIComponent(d.adres.display_name)}`;

  // Reset tabs: zet 'Kaart' terug als actief bij elke nieuwe scan
  _activateTab('map');

  // Wacht tot MapLibre geladen is (script had `defer`)
  if (typeof maplibregl === 'undefined') {
    setTimeout(() => renderMap(d), 200);
    return;
  }

  if (!_map) {
    _map = new maplibregl.Map({
      container: 'map',
      // PDOK BRT Achtergrondkaart (Mapbox-style, WebMercator) — gratis, geen key.
      // Endpoint geverifieerd apr 2026: /kadaster/brt-achtergrondkaart/ogc/v1/
      style: 'https://api.pdok.nl/kadaster/brt-achtergrondkaart/ogc/v1/styles/standaard__webmercatorquad?f=mapbox',
      center: [lon, lat],
      zoom: 17,
      attributionControl: { compact: true },
    });
    _map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
  } else {
    _map.flyTo({ center: [lon, lat], zoom: 17, duration: 400 });
  }

  // Verwijder oude marker/polygoon voordat we nieuwe toevoegen
  if (window._mapMarker) window._mapMarker.remove();
  window._mapMarker = new maplibregl.Marker({ color: '#2a6b5e' })
    .setLngLat([lon, lat])
    .addTo(_map);

  // Optioneel: BAG pand-polygoon ophalen via onze backend (zie orchestrator)
  const pandId = (d.woning && d.woning.bag_pand_id) || null;
  if (pandId) loadPandPolygon(pandId);
}

// ---- Tab-switching tussen Kaart / Street View / Satelliet ----
// Street View + Satelliet gebruiken Google Maps Embed API (vereist key).
// Zonder key valt de respectievelijke tab terug op een externe link-knop.

function _activateTab(view) {
  // Update tab-states
  document.querySelectorAll('.map-tab[data-view]').forEach(t => {
    t.classList.toggle('active', t.dataset.view === view);
  });
  // Update pane-visibility
  document.querySelectorAll('.map-pane').forEach(p => {
    const target = 'map' + (view === 'map' ? '' : '-' + view);
    p.classList.toggle('active', p.id === target);
  });

  // Lazy-load de inhoud van de tab zodra hij zichtbaar wordt
  if (view === 'streetview') _loadStreetView();
  if (view === 'satellite') _loadSatellite();
  // Resize kaart wanneer we terugkomen (MapLibre heeft dit nodig na hidden)
  if (view === 'map' && _map) setTimeout(() => _map.resize(), 50);
}

// Tab-klik handlers (eenmaal binden bij page load)
document.addEventListener('click', (e) => {
  const tab = e.target.closest('.map-tab[data-view]');
  if (tab) _activateTab(tab.dataset.view);
});

function _loadStreetView() {
  const pane = document.getElementById('map-streetview');
  if (!pane || !_mapCurrentLatLon) return;
  const { lat, lon, displayName } = _mapCurrentLatLon;
  const key = window.GOOGLE_MAPS_API_KEY;
  if (!key) {
    // Fallback: externe link als er geen key is
    pane.innerHTML = `
      <div class="map-fallback">
        <p>Street View is alleen extern beschikbaar zonder Google Maps Embed-key.</p>
        <a class="map-btn" target="_blank" rel="noopener"
           href="https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lon}">
          Open in Google Maps ↗
        </a>
      </div>`;
    return;
  }
  // Alleen iframe vervangen als lat/lon verschilt (voorkomt unnodige reload)
  const wanted = `sv:${lat},${lon}`;
  if (pane.dataset.loaded === wanted) return;
  pane.dataset.loaded = wanted;
  pane.innerHTML = `
    <iframe loading="lazy" allowfullscreen
      src="https://www.google.com/maps/embed/v1/streetview?key=${encodeURIComponent(key)}&location=${lat},${lon}&heading=0&pitch=0&fov=90"></iframe>`;
}

function _loadSatellite() {
  const pane = document.getElementById('map-satellite');
  if (!pane || !_mapCurrentLatLon) return;
  const { lat, lon } = _mapCurrentLatLon;
  const key = window.GOOGLE_MAPS_API_KEY;
  if (!key) {
    pane.innerHTML = `
      <div class="map-fallback">
        <p>Satelliet-weergave vereist een Google Maps Embed-key.</p>
        <a class="map-btn" target="_blank" rel="noopener"
           href="https://www.google.com/maps/@${lat},${lon},19z/data=!3m1!1e3">
          Open in Google Maps ↗
        </a>
      </div>`;
    return;
  }
  const wanted = `sat:${lat},${lon}`;
  if (pane.dataset.loaded === wanted) return;
  pane.dataset.loaded = wanted;
  pane.innerHTML = `
    <iframe loading="lazy" allowfullscreen
      src="https://www.google.com/maps/embed/v1/view?key=${encodeURIComponent(key)}&center=${lat},${lon}&zoom=19&maptype=satellite"></iframe>`;
}

async function loadPandPolygon(pandId) {
  try {
    const r = await fetch(`${API_BASE}/pand-geometry?pand_id=${pandId}`);
    if (!r.ok) return;
    const gj = await r.json();
    if (!gj || !gj.geometry) return;

    // Verwijder vorige layer + source
    if (_map.getLayer('pand-fill')) _map.removeLayer('pand-fill');
    if (_map.getLayer('pand-line')) _map.removeLayer('pand-line');
    if (_map.getSource('pand')) _map.removeSource('pand');

    _map.addSource('pand', {
      type: 'geojson',
      data: { type: 'Feature', geometry: gj.geometry, properties: {} },
    });
    _map.addLayer({
      id: 'pand-fill', type: 'fill', source: 'pand',
      paint: { 'fill-color': '#2a6b5e', 'fill-opacity': 0.35 },
    });
    _map.addLayer({
      id: 'pand-line', type: 'line', source: 'pand',
      paint: { 'line-color': '#1a5346', 'line-width': 2 },
    });
  } catch (_) { /* stille fout: kaart werkt zonder polygoon ook */ }
}

// ---- Sociale betekenis-laag: 3 menselijke vragen ----
function renderVragen(vragen) {
  const el = document.getElementById('s-vragen');
  if (!el) return;
  if (!vragen.length) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = vragen.map(v => renderVraag(v)).join('');
}

function renderVraag(v) {
  const icon = escape(v.icoon || '•');
  const verdictClass = `vraag-${v.verdict || 'neutral'}`;
  const scoreLabel = escape(v.score_label || '');
  const vraag = escape(v.vraag || '');
  const samenvatting = escape(v.samenvatting || '');

  // Elke categorie = een 'bakje' met eigen verdict-dot + samenvatting +
  // een details-disclosure met de onderliggende factoren.
  const categorieen = (v.categorieen || []).map(c => {
    const lvl = c.verdict || 'neutral';
    const factoren = (c.factoren || []).map(f => `
      <li class="vraag-factor vf-${f.level || 'neutral'}">
        <span class="vf-dot"></span>
        <span class="vf-label">${escape(f.label)}</span>
        <span class="vf-value">${escape(f.value_text)}</span>
      </li>`).join('');
    return `
      <details class="cat cat-${lvl}">
        <summary>
          <span class="cat-dot"></span>
          <span class="cat-icon">${escape(c.icoon || '•')}</span>
          <span class="cat-naam">${escape(c.naam)}</span>
          <span class="cat-sam">${escape(c.samenvatting || '')}</span>
          <span class="cat-chevron">▾</span>
        </summary>
        <ul class="vraag-factoren">${factoren}</ul>
      </details>`;
  }).join('');

  return `
    <article class="vraag-card ${verdictClass}">
      <header class="vraag-header">
        <span class="vraag-icoon">${icon}</span>
        <h3 class="vraag-title">${vraag}</h3>
        <span class="vraag-badge">${scoreLabel}</span>
      </header>
      <p class="vraag-sam">${samenvatting}</p>
      <div class="vraag-cats">${categorieen}</div>
    </article>
  `;
}

// ---- Cover: Leefbaarometer totaal-score + 5 sub-dimensies ----
function renderCover(cover) {
  const el = document.getElementById('s-cover');
  if (!cover || !cover.available) { if (el) el.hidden = true; return; }
  el.hidden = false;
  setText('cover-number', cover.score);
  setText('cover-label', cover.label ? capitalize(cover.label) : '');
  const betekenis = cover.betekenis || '';
  const prefix = cover.vs_nl_gem === 'rond'
    ? 'Exact op NL-gemiddelde.'
    : `${capitalize(cover.vs_nl_gem)} NL-gemiddelde.`;
  setText('cover-meaning', `${prefix} ${betekenis}`);
  const fill = document.getElementById('cover-fill');
  if (fill) fill.style.width = `${cover.percentile_nl || 0}%`;
  el.dataset.level = cover.score >= 7 ? 'good' : cover.score >= 4 ? 'neutral' : 'warn';

  // Cover highlights: 2-3 chips die direct "wat valt op" samenvatten
  renderHighlights(cover.highlights || []);

  // Grid-vs-buurt vergelijking helder verwoord (bv. "100 m: 8/9, buurt: 6/9")
  const compareEl = document.getElementById('cover-compare');
  if (compareEl) {
    if (cover.grid_vs_buurt) {
      compareEl.textContent = cover.grid_vs_buurt;
      compareEl.hidden = false;
    } else {
      compareEl.hidden = true;
    }
  }

  // 5 sub-dimensies als mini-balkjes + eventuele waarschuwing bij scheve spread.
  renderCoverDims(cover.dimensies || [], cover.waarschuwing);
}

function renderHighlights(highlights) {
  const el = document.getElementById('cover-highlights');
  if (!el) return;
  if (!highlights.length) { el.innerHTML = ''; return; }
  el.innerHTML = highlights.map(h => `
    <li class="hl hl-${h.level || 'neutral'}">
      <span class="hl-dot"></span>
      <span class="hl-label">${escape(h.label)}</span>
      <span class="hl-value">${escape(h.value)}</span>
    </li>
  `).join('');
}

function renderCoverDims(dims, waarschuwing) {
  const el = document.getElementById('cover-dims');
  if (!el) return;
  if (!dims.length) { el.innerHTML = ''; return; }
  const rows = dims.map((d) => {
    const pct = Math.max(3, (d.score - 1) / 8 * 100);
    const level = d.score >= 7 ? 'good' : d.score >= 4 ? 'neutral' : 'warn';
    return `
      <li class="dim-row" title="${escape(d.beschrijving)}">
        <span class="dim-label">${escape(d.label)}</span>
        <span class="dim-bar"><span class="dim-bar-fill dim-${level}" style="width:${pct}%"></span></span>
        <span class="dim-score">${d.score}<span class="dim-max">/9</span></span>
      </li>
    `;
  }).join('');
  const waarschHTML = waarschuwing
    ? `<div class="cover-waarschuwing">⚠️ ${escape(waarschuwing)}</div>`
    : '';
  el.innerHTML = `
    <div class="cover-dims-header">Opbouw van de score</div>
    <ul class="cover-dims-list">${rows}</ul>
    ${waarschHTML}
  `;
}

// ---- Voorzieningen-lijst (vervangt de ringen) ----
// Elk item: emoji + label + mini-balkje (breedte = relatief aan max-afstand)
// + afstand in km/m. Lezen als een lijst, niet als een ruimtelijk diagram.
function renderVoorzieningenList(voorzieningen) {
  const el = document.getElementById('voorz-list');
  if (!el) return;
  const items = (voorzieningen && voorzieningen.items) || [];
  if (items.length === 0) {
    el.innerHTML = '<li class="muted">Geen voorzieningen-data beschikbaar voor deze locatie.</li>';
    return;
  }
  // Schaal-balk: max-km bepaalt de 100% breedte. Cap bij 10 km; verder is
  // toch alles 'ver' en wil je niet dat één extreme waarde alle balkjes
  // visueel doet schrumpen.
  const maxKm = Math.min(10, Math.max(...items.map(v => v.km || 0)));
  el.innerHTML = items.map((v) => {
    const km = v.km;
    const widthPct = maxKm > 0 ? Math.max(2, Math.min(100, 100 * km / maxKm)) : 0;
    const display = km < 1 ? `${Math.round(km * 1000)} m` : `${km.toFixed(1)} km`;
    // Nabijheids-klasse voor subtiele kleuring: ≤0.5km = goed, ≤2km = neutraal, >2km = ver
    const nearClass = km <= 0.5 ? 'v-near' : km <= 2 ? 'v-mid' : 'v-far';
    return `
      <li class="voorz-item ${nearClass}">
        <span class="voorz-emoji">${v.emoji || '•'}</span>
        <span class="voorz-label">${escape(v.label || v.type)}</span>
        <span class="voorz-bar"><span class="voorz-bar-fill" style="width:${widthPct}%"></span></span>
        <span class="voorz-dist">${display}</span>
      </li>
    `;
  }).join('');
}

// ---- utils ----
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
function escape(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function capitalize(s) { return s ? s[0].toUpperCase() + s.slice(1) : ''; }
function formatEuro(n) {
  return new Intl.NumberFormat('nl-NL', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 }).format(n);
}

if (window.location.hash === '#demo') {
  $q.value = 'Damrak 1 Amsterdam';
  runScan('Damrak 1 Amsterdam');
}
