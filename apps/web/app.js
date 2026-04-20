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
