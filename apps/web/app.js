// Thuisscan frontend — vanilla JS, geen build step.
// Backend levert per indicator een {value, unit, ref:{chip_level, chip_text,
// nl_gemiddelde, norm, betekenis}}. De renderField()-helper bouwt daar een
// consistent blok van: grote waarde + chip + referentieregel + betekenis-zin.

// API_BASE resolve-volgorde:
//   1. Als config.js hem expliciet heeft gezet (ook een lege string!) → die gebruiken
//      (lege string = same-origin, wat klopt voor zowel lokaal als productie)
//   2. Alleen als config.js helemaal niet geladen is → localhost-fallback voor dev
const API_BASE =
  typeof window.THUISSCAN_API_BASE === 'string'
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
  // Vanaf 2 chars suggesties tonen (PDOK accepteert al vanaf 1;
  // 2 is snelste balance tussen UX en zinvolle hits)
  if (q.length < 2) { hideSuggestions(); return; }
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
// ---- Lichtgewicht analytics: cookieless, niet-blocking, geen IP ----
function track(event) {
  try {
    const url = `${API_BASE}/track?event=${encodeURIComponent(event)}`;
    if (navigator.sendBeacon) navigator.sendBeacon(url);
    else fetch(url, { method: 'GET', keepalive: true }).catch(() => {});
  } catch (_) { /* analytics mag nooit stuk gaan */ }
}
// Initial page-load event (niet-kritiek, async)
if (document.readyState === 'complete') track('page_load');
else window.addEventListener('load', () => track('page_load'));


async function runScan(query) {
  track('scan');
  $error.hidden = true;
  $result.hidden = true;
  $loading.hidden = true;   // we gebruiken skeleton i.p.v. simpele spinner

  // Skeleton: toon direct de sectie-structuur met placeholder-blokken.
  // Geeft user feedback dat de scan loopt + demonstreert de UI-structuur
  // voordat de data er is (feels ~2x sneller psychologisch).
  renderSkeleton();

  // Progress-indicator: update elke seconde met stap + verstreken tijd.
  const startTs = Date.now();
  const steps = [
    [0, 'Adres opzoeken…'],
    [2, 'Pand- en WOZ-gegevens…'],
    [4, 'Buurt- en leefbaarometer…'],
    [6, 'Veiligheid en luchtkwaliteit…'],
    [8, 'Klimaat en demografie…'],
    [10, 'Voorzieningen en onderwijs…'],
    [12, 'Rapport samenstellen…'],
  ];
  const progressEl = document.getElementById('skel-progress');
  const progressInterval = setInterval(() => {
    const elapsed = (Date.now() - startTs) / 1000;
    const current = steps.slice().reverse().find(([t]) => elapsed >= t) || steps[0];
    if (progressEl) {
      progressEl.innerHTML = `<span class="skel-dot"></span>${current[1]} <span class="skel-sec">${Math.round(elapsed)}s</span>`;
    }
  }, 400);

  try {
    const r = await fetch(`${API_BASE}/scan?q=${encodeURIComponent(query)}`);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `API error ${r.status}`);
    }
    clearInterval(progressInterval);
    hideSkeleton();
    render(await r.json());
  } catch (e) {
    clearInterval(progressInterval);
    hideSkeleton();
    $error.textContent = `Kon adres niet scannen: ${e.message}`;
    $error.hidden = false;
  }
}

function renderSkeleton() {
  let host = document.getElementById('skeleton');
  if (!host) {
    host = document.createElement('div');
    host.id = 'skeleton';
    host.className = 'skeleton';
    host.innerHTML = `
      <div id="skel-progress" class="skel-progress">
        <span class="skel-dot"></span>Adres opzoeken…
      </div>
      <div class="skel-card">
        <div class="skel-bar skel-bar-title"></div>
        <div class="skel-bar skel-bar-sub"></div>
      </div>
      <div class="skel-card skel-map"></div>
      <div class="skel-card">
        <div class="skel-bar skel-bar-h3"></div>
        <div class="skel-grid">
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
        </div>
      </div>
      <div class="skel-card">
        <div class="skel-bar skel-bar-h3"></div>
        <div class="skel-grid">
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
          <div><div class="skel-bar skel-bar-label"></div><div class="skel-bar skel-bar-value"></div></div>
        </div>
      </div>
    `;
    const main = document.getElementById('result') || document.body;
    main.parentNode.insertBefore(host, main);
  }
  host.hidden = false;
}

function hideSkeleton() {
  const host = document.getElementById('skeleton');
  if (host) host.hidden = true;
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
  const rendered = typeof display === 'string' && display.startsWith('<')
    ? display : escape(String(display));
  // Scope-indicator inline: als CBS de data op een grover niveau publiceert,
  // tonen we klein en grijs "(wijk)" achter de value. 'buurt' is default en
  // blijft onzichtbaar — dat is de hoofdmodus.
  const sc = indicator.scope;
  const scopeSuffix = (sc && sc !== 'buurt')
    ? ` <span class="scope-inline" title="buurtcijfer niet gepubliceerd door CBS — toont ${escape(sc)}-gemiddelde">(${escape(sc)})</span>`
    : '';
  const parts = [`<div class="field"><span class="label">${escape(label)}</span><strong${strongClass}>${rendered}${scopeSuffix}</strong>`];
  if (ref) {
    // Geen pijltje — de chip-kleur (groen/oker/rood) draagt al het signaal.
    // De pijl ("→" / "↑" / "↓") dubbelde met de kleur en leest rommelig.
    parts.push(`<p class="chip chip-${ref.chip_level}">${escape(ref.chip_text)}</p>`);
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

  // Print-knop (volledig HTML-rapport in nieuwe tab; user gebruikt Cmd+P
  // voor PDF-export). Dynamisch toegevoegd — bewust niet via index.html
  // zodat we geen knop tonen vóór een geslaagde scan.
  renderPrintKnop(d.adres.display_name);

  // Kaart + externe viewers (#11)
  renderMap(d);

  // Sociale vragen-blok (3 menselijke verdicten) is op verzoek verwijderd:
  // de data was samengesteld uit dezelfde onderliggende metrics die ook
  // in de secties zelf staan — voelde als dubbele samenvatting.
  // renderVragen(d.sociale_vragen || []);

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
  // WOZ: prefereer pand-WOZ (uit WOZ-Waardeloket) boven buurt-gemiddelde (CBS).
  // Pand-WOZ heeft ook trend_pct_per_jaar en historie — beide visualiseren.
  const wozBuurt = (d.wijk_economie && d.wijk_economie.woz) ? d.wijk_economie.woz : null;
  const wozAdres = w.woz_adres || null;
  const wozLabel = wozAdres ? 'WOZ-waarde (dit pand)' : 'WOZ-waarde (buurtgemiddelde)';
  const wozField = wozAdres || wozBuurt;
  // Onderschrift: bij pand-WOZ tonen we trend + historie (renderTrend kan beide).
  // Bij buurt-WOZ alleen trend.
  let wozExtra = null;
  if (wozAdres) {
    if (wozAdres.trend_pct_per_jaar != null) {
      wozExtra = renderTrend(wozAdres);
    } else if (wozAdres.peildatum) {
      wozExtra = `Peildatum ${wozAdres.peildatum} · bron WOZ-Waardeloket`;
    }
  } else if (wozBuurt && wozBuurt.trend_pct_per_jaar != null) {
    wozExtra = renderTrend(wozBuurt);
  }
  const woningCells = [
    fieldHTML('Bouwjaar', w.bouwjaar, it => `${it.value}`),
    fieldHTML('Oppervlakte', w.oppervlakte,
      it => `${it.value?.toLocaleString('nl-NL')} m²`),
    fieldHTML(wozLabel, wozField, it => formatEuro(it.value), wozExtra),
    fieldHTML('Energielabel', w.energielabel,
      it => it.value ? `<span class="${labelClass}">${it.value}</span>` : 'niet geregistreerd',
      w.energielabel && w.energielabel.datum ? `Registratie: ${w.energielabel.datum}` : null),
  ];
  // Extra's: Rijksmonument / Groen in straat — lazy geladen via
  // /woning-extras endpoint (RCE WFS + Overpass, 500-1500ms cold).
  // Als data al binnen is (warm cache server-side doorgegeven), toon direct.
  const rijksHTML = renderRijksmonument(w.rijksmonument);
  if (rijksHTML) woningCells.push(rijksHTML);
  const groenHTML = renderGroen(w.groen);
  if (groenHTML) woningCells.push(groenHTML);
  renderGrid('s-woning-grid', woningCells);

  // Woning-extras lazy-loader: rijksmonument + groen komen na initial render.
  // Zo wacht de hoofd-scan niet op de trage Overpass-groen-query.
  if (w.extras_pending) {
    loadWoningExtrasAsync(d.adres, woningCells);
  }

  // Sectie 2: wijk-economie
  // Grid 2×2: inkomen · arbeid · opleiding · WOZ-buurt-met-trend
  // Eronder fullwidth: eigendomsverhouding stacked bar
  const we = d.wijk_economie || {};
  const opl = we.opleiding_hoog;
  const oplExtra = opl && opl.breakdown
    ? `laag ${opl.breakdown.laag_pct ?? '?'}% · midden ${opl.breakdown.midden_pct ?? '?'}% · hoog ${opl.breakdown.hoog_pct ?? '?'}%`
    : null;
  const wijkGrid = [
    fieldHTML('Gemiddeld inkomen per inwoner', we.inkomen_per_inwoner, it => formatEuro(it.value)),
    fieldHTML('Arbeidsparticipatie', we.arbeidsparticipatie),
    fieldHTML('Hoogopgeleid (hbo/wo)', opl, it => `${it.value}%`, oplExtra),
  ];
  // 4e cel: buurt-WOZ met trend + mini-historie (vult het lege kwadrant
  // rechts onder 'Hoogopgeleid'). Data is al aanwezig in we.woz.
  if (we.woz && we.woz.value) {
    wijkGrid.push(renderWozBuurt(we.woz));
  }
  // Eigendomsverhouding als full-width stacked-bar onder de grid
  if (we.eigendomsverhouding) {
    const eigHTML = renderEigendomsverhouding(we.eigendomsverhouding);
    if (eigHTML) wijkGrid.push(eigHTML);
  }
  renderGrid('s-wijk-grid', wijkGrid);

  // Voorzieningen — lazy geladen via /voorzieningen endpoint (duurt 3-6s cold
  // op Overpass). Eerst skeleton tonen; loadVoorzieningenAsync vervangt het
  // zodra de call terug is. Cached responses (~100ms) zijn vrijwel instant.
  if (d.voorzieningen && d.voorzieningen.pending) {
    renderVoorzieningenSkeleton();
    loadVoorzieningenAsync(d.adres);
  } else {
    renderVoorzieningenList(d.voorzieningen);
  }

  // Klimaat en bereikbaarheid zijn ook lazy (te traag voor main /scan).
  // Beide krijgen een mini-loading-state en vullen in wanneer data terug is.
  if (d.klimaat && d.klimaat.pending) {
    renderKlimaatSkeleton();
    loadKlimaatAsync(d.adres);
  } else {
    renderKlimaat(d.klimaat);
  }
  if (d.bereikbaarheid && d.bereikbaarheid.pending) {
    renderBereikbaarheidSkeleton();
    loadBereikbaarheidAsync(d.adres);
  } else {
    renderBereikbaarheid(d.bereikbaarheid);
  }

  // Sectie 10: Verbouwingsmogelijkheden — lazy geladen via /verbouwing.
  // 3 WFS-calls (BRK + RCE + BAG-geom) + Shapely, ~600-900ms cold.
  // We hebben bag_PAND_id nodig (gebouw), niet verblijfsobject_id (unit).
  // Die zit in de woning-sectie.
  if (d.verbouwing && d.verbouwing.pending) {
    renderVerbouwingSkeleton();
    loadVerbouwingAsync(d.adres, (d.woning || {}).bag_pand_id || '');
  } else {
    renderVerbouwing(d.verbouwing);
  }

  // Pand-specifieke WOZ zit nu al in de eerste /scan response
  // (woning.woz_adres uit WOZ-Waardeloket). Alleen als die om wat voor
  // reden ook ontbreekt, doen we nog een retry via de losse /woz endpoint
  // — bv. bij een nieuwbouw-pand waar de cache nog leeg was.
  const bagVbo = d.adres && d.adres.bag_verblijfsobject_id;
  const heeftPandWoz = (d.woning || {}).woz_adres && (d.woning.woz_adres.value);
  if (bagVbo && !heeftPandWoz) {
    loadWozAsync(bagVbo, d.wijk_economie && d.wijk_economie.woz);
  }

  // Sectie 3: buren — layout herzien om karakter van de buurt te tonen
  // i.p.v. inhoudsloze getallen als totaal-inwoners en dichtheid.
  // Grid (2×2): huishoudensverdeling + demografie.
  // Full-width daaronder: leeftijdsprofiel (stacked bar) + TK-uitslag.
  const b = d.buren || {};
  // Layout: 2×2 grid met demografie, dan full-width bars eronder.
  //   rij 1: Eenpersoonshuishoudens | Huishoudens met kinderen
  //   rij 2: Gem. huishoudensgrootte | TK-uitslag (lokale top 3)
  //   dan:   Leeftijdsmix (full-width)
  //          Migratieachtergrond (full-width)
  const grid = [
    fieldHTML('Eenpersoonshuishoudens', b.eenpersoons),
    fieldHTML('Huishoudens met kinderen', b.met_kinderen),
    fieldHTML('Gem. huishoudensgrootte', b.huishoudensgrootte,
      it => `${it.value} pers.`),
  ];
  // TK-uitslag compact in het kwadrant naast huishoudensgrootte
  if (b.verkiezing_tk2023) {
    grid.push(renderVerkiezing(b.verkiezing_tk2023));
  }
  if (b.leeftijdsprofiel) {
    const leefHTML = renderLeeftijdsprofiel(b.leeftijdsprofiel);
    if (leefHTML) grid.push(leefHTML);
  }
  if (b.migratieachtergrond) {
    const migHTML = renderMigratieachtergrond(b.migratieachtergrond);
    if (migHTML) grid.push(migHTML);
  }
  renderGrid('s-buren-grid', grid);

  // Sectie 4: veiligheid — 2×2 grid: inbraak+fietsendiefstal (spullen),
  // geweld+totaal (persoon + context).
  const v = d.veiligheid || {};
  renderGrid('s-veiligheid-grid', [
    fieldHTML('Woninginbraken', v.woninginbraak,
      it => it.value != null ? `${it.value} per 1.000 inw` : '—'),
    fieldHTML('Fietsendiefstal (12 mnd)', v.fietsendiefstal,
      it => it.value != null ? `${it.value} per 1.000 inw` : '—'),
    fieldHTML('Geweldsmisdrijven (12 mnd)', v.geweld,
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

  // (Sectie 6 klimaat wordt lazy geladen, zie hierboven.)

  // Sectie 7: kinderen & onderwijs
  renderOnderwijs(d.onderwijs);

  // (Sectie 8 bereikbaarheid wordt lazy geladen, zie hierboven.)

  // Provenance
  const provs = d.provenance || [];
  setText('p-woning', findProv(provs, 'woning'));
  setText('p-wijk', findProv(provs, 'wijk_economie'));
  setText('p-veiligheid', findProv(provs, 'veiligheid'));
  setText('p-lucht', findProv(provs, 'leefkwaliteit'));
  setText('p-klimaat', findProv(provs, 'klimaat'));
  setText('p-onderwijs', findProv(provs, 'onderwijs'));
  setText('p-bereikbaarheid', findProv(provs, 'bereikbaarheid'));

  $result.hidden = false;

  // Paywall: bedek alle premium secties + voeg betaal-card toe
  // (alleen op publieke scan; bij _IS_PAID_VIEW slaat de functie zichzelf over)
  applyPaywall(d.adres.display_name);
}

// PAID-MODE: backend zet window.__buurtscan_paid = true op /r/<token>-pages.
// Op publieke /scan-pages staat dat niet → toon checkout-knop ipv PDF-knop.
const _IS_PAID_VIEW = typeof window !== 'undefined' && window.__buurtscan_paid === true;

// PAYWALL FEATURE-FLAG — staat standaard UIT zodat de live-site normaal werkt
// terwijl we de paywall in parallel afbouwen. Drie manieren om aan te zetten:
//   1. URL-param ?paywall=1   → tijdelijk in jouw browser (jij kan testen)
//   2. localStorage flag      → blijvend in jouw browser (handig voor dev)
//   3. const _PAYWALL_DEFAULT_ON = true → globaal voor iedereen (bij go-live)
// Wanneer alles af is + Mollie-keys gezet → flip _PAYWALL_DEFAULT_ON op true,
// deploy, klaar. Tot die tijd zien gebruikers de gewone PDF-knop.
const _PAYWALL_DEFAULT_ON = false;
const _PAYWALL_ENABLED = (function () {
  if (typeof window === 'undefined') return false;
  try {
    const u = new URLSearchParams(window.location.search);
    if (u.get('paywall') === '1') return true;
    if (u.get('paywall') === '0') return false;
    if (window.localStorage && window.localStorage.getItem('buurtscan_paywall') === '1') return true;
  } catch (_) { /* private mode etc. */ }
  return _PAYWALL_DEFAULT_ON;
})();
if (typeof console !== 'undefined') {
  console.info('[buurtscan] paywall:', _PAYWALL_ENABLED ? 'ON' : 'OFF (legacy free-PDF flow active)');
}

function renderPrintKnop(adres) {
  if (!adres) return;
  const host = document.getElementById('rapport-knop-host');
  if (!host) return;
  if (_IS_PAID_VIEW) {
    // Betaalde sessie — PDF-download via magic-link
    const filename = `Buurtscan-${adres.replace(/[^a-zA-Z0-9]+/g, '-')}.pdf`;
    const token = window.__buurtscan_token || '';
    host.innerHTML = `
      <button type="button" id="pdf-btn" class="rapport-knop"
              data-token="${token}" data-filename="${filename}"
              title="Download PDF (geldige link)">
        <span class="btn-icon">📄</span><span class="btn-label">Rapport als PDF</span>
      </button>
    `;
    document.getElementById('pdf-btn').addEventListener('click', _handlePdfDownloadPaid);
  } else if (_PAYWALL_ENABLED) {
    // Vrije scan + paywall AAN — checkout-knop
    host.innerHTML = `
      <button type="button" id="checkout-btn" class="rapport-knop"
              data-adres="${adres.replace(/"/g, '&quot;')}"
              title="Volledig rapport voor € 4,99 — link 7 dagen geldig in je mail">
        <span class="btn-icon">📄</span>
        <span class="btn-label">Volledig rapport — € 4,99</span>
      </button>
    `;
    document.getElementById('checkout-btn').addEventListener('click', _openCheckoutModal);
  } else {
    // Vrije scan + paywall UIT (default) — legacy gratis PDF-download
    const filename = `Buurtscan-${adres.replace(/[^a-zA-Z0-9]+/g, '-')}.pdf`;
    host.innerHTML = `
      <button type="button" id="pdf-btn" class="rapport-knop"
              data-adres="${adres.replace(/"/g, '&quot;')}" data-filename="${filename}"
              title="Download het volledige rapport als PDF">
        <span class="btn-icon">📄</span><span class="btn-label">Rapport als PDF</span>
      </button>
    `;
    document.getElementById('pdf-btn').addEventListener('click', _handlePdfDownloadFree);
  }
}

// Legacy free PDF-download (paywall UIT). Hits /rapport.pdf?q=<adres>.
async function _handlePdfDownloadFree(e) {
  const btn = e.currentTarget;
  if (btn.classList.contains('loading') || btn.classList.contains('done')) return;
  track('pdf_download');
  const adres = btn.dataset.adres;
  const filename = btn.dataset.filename;
  const labelEl = btn.querySelector('.btn-label');
  const iconEl = btn.querySelector('.btn-icon');
  const orig = { icon: iconEl.innerHTML, label: labelEl.textContent };
  btn.classList.add('loading'); btn.disabled = true;
  iconEl.innerHTML = '<span class="spinner"></span>';
  labelEl.textContent = 'PDF wordt gemaakt…';
  const startTs = Date.now();
  const timer = setInterval(() => {
    labelEl.textContent = `PDF wordt gemaakt… ${Math.round((Date.now() - startTs) / 1000)}s`;
  }, 1000);
  try {
    const r = await fetch(`${API_BASE}/rapport.pdf?q=${encodeURIComponent(adres)}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    clearInterval(timer);
    btn.classList.remove('loading'); btn.classList.add('done');
    iconEl.textContent = '✓';
    labelEl.textContent = `Gedownload (${Math.round((Date.now() - startTs) / 1000)}s)`;
    setTimeout(() => {
      btn.classList.remove('done'); btn.disabled = false;
      iconEl.innerHTML = orig.icon; labelEl.textContent = orig.label;
    }, 3000);
  } catch (err) {
    clearInterval(timer);
    btn.classList.remove('loading'); btn.classList.add('error');
    iconEl.textContent = '⚠️';
    labelEl.textContent = 'Fout — probeer opnieuw';
    setTimeout(() => {
      btn.classList.remove('error'); btn.disabled = false;
      iconEl.innerHTML = orig.icon; labelEl.textContent = orig.label;
    }, 4000);
  }
}

// PDF-download voor BETAALDE sessie via magic-link
async function _handlePdfDownloadPaid(e) {
  const btn = e.currentTarget;
  if (btn.classList.contains('loading') || btn.classList.contains('done')) return;
  track('pdf_download');
  const token = btn.dataset.token;
  const filename = btn.dataset.filename;
  const labelEl = btn.querySelector('.btn-label');
  const iconEl = btn.querySelector('.btn-icon');
  const orig = { icon: iconEl.innerHTML, label: labelEl.textContent };
  btn.classList.add('loading'); btn.disabled = true;
  iconEl.innerHTML = '<span class="spinner"></span>';
  labelEl.textContent = 'PDF wordt gemaakt…';
  const startTs = Date.now();
  const timer = setInterval(() => {
    labelEl.textContent = `PDF wordt gemaakt… ${Math.round((Date.now() - startTs) / 1000)}s`;
  }, 1000);
  try {
    const r = await fetch(`${API_BASE}/r/${token}/pdf`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    clearInterval(timer);
    btn.classList.remove('loading'); btn.classList.add('done');
    iconEl.textContent = '✓';
    labelEl.textContent = `Gedownload (${Math.round((Date.now() - startTs) / 1000)}s)`;
    setTimeout(() => {
      btn.classList.remove('done'); btn.disabled = false;
      iconEl.innerHTML = orig.icon; labelEl.textContent = orig.label;
    }, 3000);
  } catch (err) {
    clearInterval(timer);
    btn.classList.remove('loading'); btn.classList.add('error');
    iconEl.textContent = '⚠️';
    labelEl.textContent = 'Fout — probeer opnieuw';
    setTimeout(() => {
      btn.classList.remove('error'); btn.disabled = false;
      iconEl.innerHTML = orig.icon; labelEl.textContent = orig.label;
    }, 4000);
  }
}

// Checkout-modal voor betaling via Mollie
function _openCheckoutModal(e) {
  const btn = e.currentTarget;
  const adres = btn.dataset.adres;
  if (document.getElementById('checkout-modal')) return;
  const modal = document.createElement('div');
  modal.id = 'checkout-modal';
  modal.className = 'checkout-modal';
  modal.innerHTML = `
    <div class="modal-backdrop" data-close></div>
    <div class="modal-card" role="dialog" aria-labelledby="modal-title" aria-modal="true">
      <button class="modal-close" data-close aria-label="Sluiten">×</button>
      <h2 id="modal-title">Volledig rapport — <em>€ 4,99</em></h2>
      <p class="modal-sub">Voor <strong>${_escapeHtml(adres)}</strong></p>
      <ul class="modal-features">
        <li>📊 13 hoofdstukken: WOZ, klimaat, demografie, onderwijs, voorzieningen, verbouwen</li>
        <li>🗺️ OpenStreetMap-straatkaart + Kadaster perceel-kaart</li>
        <li>📄 PDF-download (browser-grade kwaliteit)</li>
        <li>🔗 Link <strong>7 dagen geldig</strong> in je e-mail</li>
        <li>↩️ 3 dagen geld-terug-garantie</li>
      </ul>
      <form id="checkout-form" class="modal-form">
        <label for="checkout-email">Je e-mailadres</label>
        <input type="email" id="checkout-email" name="email" required
               placeholder="jij@voorbeeld.nl" autocomplete="email"
               aria-describedby="email-hint">
        <span id="email-hint" class="field-hint">We sturen je rapport-link hierheen</span>
        <button type="submit" class="btn-pay">
          <span class="btn-pay-label">Doorgaan naar betaling →</span>
        </button>
        <div id="checkout-error" class="field-error" hidden></div>
      </form>
      <p class="modal-foot">
        Betalen via <strong>iDEAL</strong>, creditcard, Bancontact, Apple Pay of PayPal.<br>
        <a href="/voorwaarden" target="_blank">Algemene voorwaarden</a> ·
        <a href="/privacy" target="_blank">Privacy</a>
      </p>
    </div>
  `;
  document.body.appendChild(modal);
  modal.querySelectorAll('[data-close]').forEach(el => el.addEventListener('click', () => {
    modal.remove();
    document.removeEventListener('keydown', _modalEscapeHandler);
  }));
  document.addEventListener('keydown', _modalEscapeHandler);
  setTimeout(() => document.getElementById('checkout-email')?.focus(), 50);
  document.getElementById('checkout-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const email = document.getElementById('checkout-email').value.trim();
    const errorEl = document.getElementById('checkout-error');
    const submitBtn = ev.target.querySelector('button[type="submit"]');
    const labelEl = submitBtn.querySelector('.btn-pay-label');
    const origLabel = labelEl.textContent;
    errorEl.hidden = true;
    submitBtn.disabled = true;
    labelEl.innerHTML = '<span class="spinner spinner-dark"></span> Bezig…';
    track('checkout_submit');
    try {
      const r = await fetch(`${API_BASE}/checkout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adres, email }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      const data = await r.json();
      if (data.checkout_url) window.location.href = data.checkout_url;
      else throw new Error('Geen checkout-url ontvangen');
    } catch (err) {
      errorEl.textContent = `Fout: ${err.message}`;
      errorEl.hidden = false;
      submitBtn.disabled = false;
      labelEl.textContent = origLabel;
    }
  });
}

function _modalEscapeHandler(e) {
  if (e.key === 'Escape') {
    document.getElementById('checkout-modal')?.remove();
    document.removeEventListener('keydown', _modalEscapeHandler);
  }
}

function _escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

// Verberg premium secties met paywall-card ervoor (alleen op publieke scan)
function applyPaywall(adres) {
  if (_IS_PAID_VIEW) return;
  if (!_PAYWALL_ENABLED) return;   // feature-flag — paywall pas actief bij go-live
  const PREMIUM_IDS = [
    's-buren', 's-veiligheid', 's-leefkwaliteit', 's-klimaat',
    's-onderwijs', 's-bereikbaarheid', 's-voorzieningen', 's-verbouwing',
  ];
  let firstPremium = null;
  PREMIUM_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el && !el.hidden) {
      el.classList.add('premium-blur');
      el.setAttribute('aria-hidden', 'true');
      el.setAttribute('inert', '');
      if (!firstPremium) firstPremium = el;
    }
  });
  if (!firstPremium) return;
  if (document.querySelector('.paywall-card')) return;  // al toegevoegd

  const card = document.createElement('section');
  card.className = 'card paywall-card';
  card.innerHTML = `
    <div class="paywall-badge">PREMIUM</div>
    <h3>De rest van het rapport zit achter een betaalmuur 🔒</h3>
    <p class="paywall-intro">
      Boven zie je de gratis voorvertoning: leefbaarheid, woning-feiten en WOZ.<br>
      Voor het volledige rapport over <strong>${_escapeHtml(adres)}</strong>:
    </p>
    <ul class="paywall-list">
      <li>👥 <strong>Bewoners &amp; demografie</strong> — eigendom, leeftijd, herkomst, politieke voorkeur</li>
      <li>🛡️ <strong>Veiligheid</strong> — criminaliteit per categorie vs NL-gemiddelde</li>
      <li>🫁 <strong>Lucht &amp; geluid</strong> — fijnstof, NO₂, geluidsbelasting</li>
      <li>🌊 <strong>Klimaatrisico 2050</strong> — overstroming, hitte, paalrot, verschilzetting</li>
      <li>🏫 <strong>Onderwijs</strong> — alle scholen + opvang met inspectie-oordeel</li>
      <li>🚆 <strong>Bereikbaarheid</strong> — OV, snelweg, reistijden naar werkcentra</li>
      <li>🛒 <strong>Voorzieningen</strong> — alle POIs in 7 categorieën binnen 1,5 km</li>
      <li>🛠️ <strong>Verbouwen</strong> — uitbouw, dakkapel, tuinhuis, zonnepanelen + monument-status</li>
      <li>📄 <strong>PDF-download</strong> — direct in je inbox, 7 dagen geldig</li>
    </ul>
    <button type="button" class="btn-pay-large" id="paywall-cta"
            data-adres="${adres.replace(/"/g, '&quot;')}">
      📄 Volledig rapport voor € 4,99
    </button>
    <p class="paywall-foot">
      iDEAL · creditcard · Apple Pay · PayPal &nbsp;·&nbsp; 3 dagen geld-terug
    </p>
  `;
  firstPremium.parentNode.insertBefore(card, firstPremium);
  document.getElementById('paywall-cta').addEventListener('click', _openCheckoutModal);
}

// _handlePdfDownload (legacy): vervangen door _handlePdfDownloadPaid voor
// magic-link route. Functie verwijderd om dead code te vermijden.

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
    return `<li><strong>${escape(p.partij)}</strong> <span class="gem">${gem}</span> <span class="nl">(NL ${nl})</span>${delta}</li>`;
  }).join('');
  const note = v.per_gemeente_beschikbaar
    ? ''
    : '<p class="hint">Gemeente-specifieke uitslag nog niet beschikbaar — toont landelijke top 3.</p>';
  const electionLabel = (v.election || 'TK2025');
  // Compact (single-column) variant: past in het leeg quadrant naast
  // Gemiddelde huishoudensgrootte. Fullwidth zou onnodig ruimte pakken.
  // Label expliciet 'in deze gemeente' zodat duidelijk is dat het LOKAAL
  // is (percentage naast partij), met landelijk tussen haakjes.
  const gemeenteLabel = v.per_gemeente_beschikbaar
    ? 'Grootste partijen in deze gemeente'
    : `Grootste partijen (landelijke ${electionLabel})`;
  return `<div class="field">
    <span class="label">${escape(gemeenteLabel)}</span>
    <ul class="verkiezing-list verkiezing-compact">${rows}</ul>
    ${note}
  </div>`;
}

// ---- Buurt-WOZ met trend + mini-historie (sectie 2, 4e grid-cel) ----
// Toont gemiddelde WOZ-waarde van de buurt + jaarlijkse groei-% + 2-punts
// historie. Complement op pand-specifieke WOZ in sectie 1: hier zie je het
// 'buurt-narratief', daar de exacte pand-waarde.
function renderWozBuurt(woz) {
  if (!woz || !woz.value) return '';
  const eur = '€ ' + woz.value.toLocaleString('nl-NL');
  const scope = woz.scope;
  const scopeSuffix = (scope && scope !== 'buurt')
    ? ` <span class="scope-inline" title="buurtcijfer niet gepubliceerd door CBS">(${escape(scope)})</span>`
    : '';
  const ref = woz.ref;
  // Trend: CAGR over beschikbare jaren (trend_pct_per_jaar) + historie
  const trend = woz.trend_pct_per_jaar;
  let trendChipHtml = '';
  if (trend != null) {
    const lvl = trend >= 3 ? 'good' : trend <= -2 ? 'warn' : 'neutral';
    const arrow = trend > 0 ? '↑' : trend < 0 ? '↓' : '→';
    trendChipHtml = `<p class="chip chip-${lvl}">${arrow} ${trend > 0 ? '+' : ''}${trend}% per jaar</p>`;
  }
  // Mini-historie uit trend_series (typisch 2 jaren bij KWB)
  const series = Array.isArray(woz.trend_series) ? woz.trend_series : [];
  let histHtml = '';
  if (series.length >= 2) {
    const oldest = series[0];
    const newest = series[series.length - 1];
    histHtml = `<p class="hint">${escape(oldest.year)}: €${oldest.woz_eur.toLocaleString('nl-NL')} → ${escape(newest.year)}: €${newest.woz_eur.toLocaleString('nl-NL')}</p>`;
  }
  // Chip over niveau (bv. 'boven NL-gemiddelde')
  const refChipHtml = ref
    ? `<p class="chip chip-${ref.chip_level}">${escape(ref.chip_text)}</p>`
    : '';
  // NL-referentie
  const refLine = (ref && (ref.nl_gemiddelde || ref.norm))
    ? `<p class="refline">${escape(ref.nl_gemiddelde || '')}${ref.norm ? ' · ' + escape(ref.norm) : ''}</p>`
    : '';
  const meaning = (ref && ref.betekenis)
    ? `<p class="meaning">${escape(ref.betekenis)}</p>`
    : '';
  return `<div class="field">
    <span class="label">WOZ-waarde buurt</span>
    <strong>${eur}${scopeSuffix}</strong>
    ${refChipHtml}
    ${trendChipHtml}
    ${refLine}
    ${meaning}
    ${histHtml}
  </div>`;
}

// ---- Eigendomsverhouding als stacked bar (koop / sociale huur / particulier) ----
// Full-width row onder de grid-2. Elk van de 3 segmenten toont zijn %, met een
// kleurcodering die karakter weergeeft (niet goed/slecht). De interpretatie-
// tekst eronder vertelt het verhaal: "corporatie-wijk", "koop-dominant", etc.
function renderEigendomsverhouding(eig) {
  if (!eig) return '';
  const koop = eig.koop_pct;
  const soc = eig.sociale_huur_pct;
  const par = eig.particuliere_huur_pct;
  // Als geen enkele waarde bekend is, helemaal niet renderen
  if (koop == null && soc == null && par == null) return '';
  // Segmenten met kleur-codering per type
  const segments = [
    { key: 'koop',    label: 'Koop',             pct: koop, color: 'eig-koop' },
    { key: 'soc',     label: 'Sociale huur',     pct: soc,  color: 'eig-soc' },
    { key: 'par',     label: 'Particuliere huur', pct: par, color: 'eig-par' },
  ].filter(s => s.pct != null && s.pct > 0);
  // Segment balken — flex, elk zijn breedte in %
  const bar = segments.map(s =>
    `<span class="eig-seg ${s.color}" style="flex:${s.pct}"
      title="${escape(s.label)}: ${s.pct}%"></span>`
  ).join('');
  // Legenda met bolletje per kleur + label + pct
  const legend = segments.map(s =>
    `<span class="eig-leg"><span class="eig-dot ${s.color}"></span>${escape(s.label)} <strong>${s.pct}%</strong></span>`
  ).join('');
  // Scope-indicator (buurt/wijk/gemeente) achter de titel
  const scopeSuffix = eig.scope && eig.scope !== 'buurt'
    ? ` <span class="scope-inline" title="op ${escape(eig.scope)}-niveau gepubliceerd">(${escape(eig.scope)})</span>`
    : '';
  const ref = eig.ref;
  const refHTML = ref ? `
    <p class="chip chip-${ref.chip_level}">${escape(ref.chip_text)}</p>
    ${ref.betekenis ? `<p class="meaning">${escape(ref.betekenis)}</p>` : ''}
    ${ref.nl_gemiddelde ? `<p class="refline">${escape(ref.nl_gemiddelde)}</p>` : ''}
  ` : '';
  return `<div class="field field-fullwidth eigendom">
    <span class="label">Eigendomsverhouding woningen${scopeSuffix}</span>
    <div class="eig-bar" aria-label="Verdeling koop, sociale huur, particuliere huur">${bar}</div>
    <div class="eig-legend">${legend}</div>
    ${refHTML}
  </div>`;
}

// ---- Leeftijdsprofiel als stacked bar (kinderen / 15-65 / 65+) ----
// Zelfde visuele taal als renderEigendomsverhouding voor consistentie.
// 3 klassen: 0-15 = kinderen, 15-65 = werkzame leeftijd, 65+ = ouderen.
function renderLeeftijdsprofiel(leef) {
  if (!leef) return '';
  const j = leef.pct_jong;
  const m = leef.pct_midden;
  const o = leef.pct_oud;
  if (j == null && m == null && o == null) return '';
  const segments = [
    { key: 'jong',   label: '0–15 jr',    pct: j, color: 'leef-jong' },
    { key: 'midden', label: '15–65 jr',   pct: m, color: 'leef-midden' },
    { key: 'oud',    label: '65+',        pct: o, color: 'leef-oud' },
  ].filter(s => s.pct != null && s.pct > 0);
  const bar = segments.map(s =>
    `<span class="eig-seg ${s.color}" style="flex:${s.pct}"
      title="${escape(s.label)}: ${s.pct}%"></span>`
  ).join('');
  const legend = segments.map(s =>
    `<span class="eig-leg"><span class="eig-dot ${s.color}"></span>${escape(s.label)} <strong>${s.pct}%</strong></span>`
  ).join('');
  const scopeSuffix = leef.scope && leef.scope !== 'buurt'
    ? ` <span class="scope-inline" title="op ${escape(leef.scope)}-niveau gepubliceerd">(${escape(leef.scope)})</span>`
    : '';
  const ref = leef.ref;
  const refHTML = ref ? `
    <p class="chip chip-${ref.chip_level}">${escape(ref.chip_text)}</p>
    ${ref.betekenis ? `<p class="meaning">${escape(ref.betekenis)}</p>` : ''}
    ${ref.nl_gemiddelde ? `<p class="refline">${escape(ref.nl_gemiddelde)}</p>` : ''}
  ` : '';
  return `<div class="field field-fullwidth eigendom">
    <span class="label">Leeftijdsmix buurt${scopeSuffix}</span>
    <div class="eig-bar" aria-label="Verdeling 0-15, 15-65, 65+ jaar">${bar}</div>
    <div class="eig-legend">${legend}</div>
    ${refHTML}
  </div>`;
}

// ---- Migratieachtergrond als stacked bar (NL / Westers / Niet-westers) ----
// Peiljaar 2020 (laatste jaar waarin CBS dit op buurt-niveau publiceerde).
// Scope kan buurt/wijk/gemeente zijn — tonen we duidelijk als label.
function renderMigratieachtergrond(mig) {
  if (!mig) return '';
  const nl = mig.pct_nederlands;
  const w = mig.pct_westers;
  const nw = mig.pct_niet_westers;
  if (nl == null && w == null && nw == null) return '';
  const segments = [
    { key: 'nl',  label: 'Nederlands',   pct: nl, color: 'mig-nl' },
    { key: 'w',   label: 'Westers',      pct: w,  color: 'mig-w' },
    { key: 'nw',  label: 'Niet-westers', pct: nw, color: 'mig-nw' },
  ].filter(s => s.pct != null && s.pct > 0);
  const bar = segments.map(s =>
    `<span class="eig-seg ${s.color}" style="flex:${s.pct}"
      title="${escape(s.label)}: ${s.pct}%"></span>`
  ).join('');
  const legend = segments.map(s =>
    `<span class="eig-leg"><span class="eig-dot ${s.color}"></span>${escape(s.label)} <strong>${s.pct}%</strong></span>`
  ).join('');
  const scope = mig.scope || 'buurt';
  const scopeSuffix = scope !== 'buurt'
    ? ` <span class="scope-inline" title="op ${escape(scope)}-niveau gepubliceerd">(${escape(scope)})</span>`
    : '';
  const peilSuffix = mig.peiljaar
    ? ` <span class="scope-inline" title="CBS publiceert dit veld niet meer op buurt-niveau sinds 2021">(peiljaar ${escape(mig.peiljaar)})</span>`
    : '';
  const ref = mig.ref;
  const refHTML = ref ? `
    <p class="chip chip-${ref.chip_level}">${escape(ref.chip_text)}</p>
    ${ref.betekenis ? `<p class="meaning">${escape(ref.betekenis)}</p>` : ''}
    ${ref.nl_gemiddelde ? `<p class="refline">${escape(ref.nl_gemiddelde)}</p>` : ''}
  ` : '';
  return `<div class="field field-fullwidth eigendom">
    <span class="label">Migratieachtergrond${scopeSuffix}${peilSuffix}</span>
    <div class="eig-bar" aria-label="Verdeling Nederlands, Westers, Niet-westers">${bar}</div>
    <div class="eig-legend">${legend}</div>
    ${refHTML}
  </div>`;
}

// ---- Sectie 7 · Kinderen & onderwijs (LRK + DUO + Onderwijsinspectie) ----
// Toont binnen 1.5 km van het adres:
//   - Kinderopvang: totaal aantal locaties + kindplaatsen, top 5 dichtstbij
//   - Scholen (basis): aantal + inspectie-oordelen-split, top 5 dichtstbij
// Voor scholen: chip met inspectie-oordeel (Voldoende = groen, Onvoldoende/
// Zeer zwak = rood, Zonder actueel oordeel = grijs).
function renderOnderwijs(o) {
  const section = document.getElementById('s-onderwijs');
  const host = document.getElementById('s-onderwijs-content');
  if (!section || !host) return;
  if (!o || o.available === false) { section.hidden = true; return; }

  const ko = o.kinderopvang || {};
  const sc = o.scholen || {};
  const koRadius = ko.radius_m ? (ko.radius_m < 1000 ? `${ko.radius_m} m` : `${(ko.radius_m/1000).toFixed(1)} km`) : '1 km';
  const scRadius = sc.radius_m ? (sc.radius_m < 1000 ? `${sc.radius_m} m` : `${(sc.radius_m/1000).toFixed(1)} km`) : '1.5 km';

  // Samenvatting (top-regel) van kinderopvang
  const koHeader = ko.aantal_locaties > 0
    ? `<strong>${ko.aantal_locaties}</strong> kinderopvang-locaties binnen ${koRadius}` +
      (ko.totaal_kindplaatsen ? ` · ${ko.totaal_kindplaatsen.toLocaleString('nl-NL')} kindplaatsen` : '')
    : `Geen kinderopvang binnen ${koRadius} gevonden.`;

  const koTypeLabels = { KDV: 'dagverblijf', BSO: 'buitenschoolse opvang', VGO: 'gastouder', GO: 'gastouder-buro' };
  const koTypeSummary = ko.per_type
    ? Object.entries(ko.per_type).filter(([_, n]) => n > 0)
      .map(([t, n]) => `${n} ${koTypeLabels[t] || t}`)
      .join(' · ')
    : '';

  const koTopList = (ko.top || []).map(it => {
    // LRK-deeplink: ouders kunnen inspectierapport (PDF) inzien per locatie
    const infoLink = it.url
      ? `<a href="${escape(it.url)}" target="_blank" rel="noopener" class="onderwijs-info-link" title="Open LRK-pagina met inspectierapport">info ↗</a>`
      : '';
    return `
    <li class="onderwijs-item">
      <span class="onderwijs-icoon">${it.type === 'KDV' ? '👶' : it.type === 'BSO' ? '🎒' : '🏠'}</span>
      <span class="onderwijs-main">
        <span class="onderwijs-naam">${escape(it.naam || '(onbekend)')} ${infoLink}</span>
        <span class="onderwijs-sub">${escape(koTypeLabels[it.type] || it.type || '')}${it.kindplaatsen ? ` · ${it.kindplaatsen} kindplaatsen` : ''}</span>
      </span>
      <span class="onderwijs-dist">${formatMeters(it.meters)}</span>
    </li>
  `;
  }).join('');

  // Scholen — top 5 met inspectie-chip
  const schHeader = sc.aantal > 0
    ? `<strong>${sc.aantal}</strong> basisscholen binnen ${scRadius}`
    : `Geen basisscholen binnen ${scRadius} gevonden.`;

  const schOordelenBadges = sc.oordelen
    ? Object.entries(sc.oordelen).filter(([_, n]) => n > 0).map(([label, n]) => {
        const lvl = label === 'Voldoende' ? 'good'
          : (label === 'Onvoldoende' || label === 'Zeer zwak') ? 'warn'
          : 'neutral';
        return `<span class="chip chip-${lvl}">${n}× ${escape(label.toLowerCase())}</span>`;
      }).join(' ')
    : '';

  const schTopList = (sc.top || []).map(it => {
    const oordeel = it.inspectie_oordeel;
    const oordeelLvl = oordeel === 'Voldoende' ? 'good'
      : (oordeel === 'Onvoldoende' || oordeel === 'Zeer zwak') ? 'warn'
      : 'neutral';
    const oordeelChip = oordeel
      ? `<span class="chip chip-${oordeelLvl} chip-inline">${escape(oordeel)}</span>`
      : '';
    // it.url bevat bij voorkeur de directe Scholen-op-de-Kaart URL
    // (via sync-sitemap-match, ~74% van de scholen). Fallback: als we
    // geen SoK-match hebben, gebruiken we Google site-search naar SoK.
    let infoUrl = it.url;
    if (!infoUrl || !infoUrl.includes("scholenopdekaart.nl")) {
      // Geen directe SoK-URL; bouw Google-search als fallback
      infoUrl = it.naam
        ? `https://www.google.com/search?q=${encodeURIComponent(it.naam + ' site:scholenopdekaart.nl')}`
        : '';
    }
    const infoLink = infoUrl
      ? `<a href="${escape(infoUrl)}" target="_blank" rel="noopener" class="onderwijs-info-link" title="Open Scholen op de Kaart">info ↗</a>`
      : '';
    return `
      <li class="onderwijs-item">
        <span class="onderwijs-icoon">🏫</span>
        <span class="onderwijs-main">
          <span class="onderwijs-naam">${escape(it.naam || '(onbekend)')} ${infoLink}</span>
          <span class="onderwijs-sub">${escape(it.denominatie || '')} ${oordeelChip}</span>
        </span>
        <span class="onderwijs-dist">${formatMeters(it.meters)}</span>
      </li>
    `;
  }).join('');

  host.innerHTML = `
    <div class="onderwijs-block">
      <h4 class="onderwijs-header">Kinderopvang</h4>
      <p class="onderwijs-sum">${koHeader}${koTypeSummary ? ` <span class="muted small">(${koTypeSummary})</span>` : ''}</p>
      ${koTopList ? `<ul class="onderwijs-list">${koTopList}</ul>` : ''}
    </div>
    <div class="onderwijs-block">
      <h4 class="onderwijs-header">Basisscholen</h4>
      <p class="onderwijs-sum">${schHeader} ${schOordelenBadges}</p>
      ${schTopList ? `<ul class="onderwijs-list">${schTopList}</ul>` : ''}
      <p class="hint hint-small">'Zonder actueel oordeel' = nog geen nieuw inspectiebezoek; geen signaal van tekortkoming.</p>
    </div>
  `;
  section.hidden = false;
}

function formatMeters(m) {
  if (m == null) return '—';
  return m < 1000 ? `${m} m` : `${(m / 1000).toFixed(1)} km`;
}

// ---- Async loaders voor klimaat + bereikbaarheid ----
// Beide secties zijn te traag voor main /scan (klimaat: 8 CAS-calls ~1s;
// bereikbaarheid: Overpass 2-5s cold). Ze krijgen eigen endpoints en worden
// in de achtergrond opgehaald nadat de hoofdpagina al getoond is.

function renderKlimaatSkeleton() {
  const grid = document.getElementById('s-klimaat-grid');
  if (!grid) return;
  // 4 skeleton-kaarten (2 grid-cellen × 2 rijen)
  const rows = Array.from({ length: 4 }, () => `
    <div class="field skel-field">
      <span class="skel-bar skel-label"></span>
      <span class="skel-bar skel-value"></span>
      <span class="skel-bar skel-chip"></span>
      <span class="skel-bar skel-meaning"></span>
    </div>
  `).join('');
  grid.innerHTML = rows;
}

// ---- Sectie 10: Verbouwingsmogelijkheden (lazy-loaded) ----
// Toont perceel-data + beschermd gezicht + onbebouwd achtererf + deeplinks
// naar ruimtelijkeplannen.nl en omgevingsloket.nl. Fase 1 MVP zonder
// beslisboom; Fase 2 voegt de concrete cards toe (uitbouw/dakkapel/
// tuinhuis) op basis van Claude Haiku DSO-extractie. De optopping-card
// is verwijderd omdat max-bouwhoogte via open data niet beschikbaar
// is (zie orchestrator._build_mogelijkheden).

function renderVerbouwingSkeleton() {
  const section = document.getElementById('s-verbouwing');
  const host = document.getElementById('s-verbouwing-content');
  if (!section || !host) return;
  host.innerHTML = `
    <div class="verb-grid">
      <div class="verb-tile skel-tile"><span class="skel-bar skel-label"></span><span class="skel-bar skel-sub"></span></div>
      <div class="verb-tile skel-tile"><span class="skel-bar skel-label"></span><span class="skel-bar skel-sub"></span></div>
      <div class="verb-tile skel-tile"><span class="skel-bar skel-label"></span><span class="skel-bar skel-sub"></span></div>
    </div>
  `;
  section.hidden = false;
}

async function loadVerbouwingAsync(adres, bagPandId) {
  if (!adres || !adres.wgs84 || !adres.rd) return;
  // Gemeentenaam uit buurtnaam afleiden is onbetrouwbaar; we sturen alleen
  // de CBS-gemeentecode + display_name (adres bevat "... {straat} {huisnr},
  // {postcode} {plaats}") — de backend fallt terug op een Google-search
  // deeplink als de gemeentenaam niet exact de woonplaats is.
  const plaats = extractPlaatsFromDisplayName(adres.display_name || '');
  const params = new URLSearchParams({
    lat: String(adres.wgs84.lat),
    lon: String(adres.wgs84.lon),
    rd_x: String(adres.rd.x),
    rd_y: String(adres.rd.y),
    bag_pand_id: bagPandId || '',
    gemeentecode: adres.gemeentecode || '',
    gemeente_naam: plaats || '',
    huisnummertoevoeging: adres.huisnummertoevoeging || '',
    vbo_id: adres.bag_verblijfsobject_id || '',
  });
  try {
    const r = await fetch(`${API_BASE}/verbouwing?${params.toString()}`);
    if (!r.ok) throw new Error(`API ${r.status}`);
    const v = await r.json();
    renderVerbouwing(v);
  } catch (e) {
    const host = document.getElementById('s-verbouwing-content');
    if (host) host.innerHTML = `<p class="muted small">Verbouwings-data tijdelijk niet beschikbaar.</p>`;
  }
}

function renderVerbouwing(v) {
  const section = document.getElementById('s-verbouwing');
  const host = document.getElementById('s-verbouwing-content');
  const prov = document.getElementById('p-verbouwing');
  if (!section || !host) return;
  if (!v || !v.available) { section.hidden = true; return; }
  section.hidden = false;

  // Blok 1 — Kavel-analyse. We tonen 'pand-footprint op dit perceel' — bij
  // rijtjeshuizen is de BAG-pand-polygoon het hele rijtje, dus we clippen
  // op perceel zodat de koper z'n eigen woning-footprint ziet, niet de buren.
  const perceel = v.perceel || null;
  const pandOpPerceelM2 = v.pand_op_perceel_m2 || null;
  const pandTotaalM2 = v.pand_totaal_m2 || null;
  const ach = v.achtererf || null;
  const typeHint = v.woning_type_hint || 'onbekend';
  const kavelBlok = (perceel || pandOpPerceelM2 || ach) ? `
    <div class="verb-kavel">
      <div class="verb-kavel-row">
        ${perceel ? `<div class="verb-kavel-item"><span class="label">Perceel</span><strong>${perceel.oppervlakte_m2.toLocaleString('nl-NL')} m²</strong></div>` : ''}
        ${pandOpPerceelM2 ? `<div class="verb-kavel-item"><span class="label">Woning-footprint</span><strong>${pandOpPerceelM2.toLocaleString('nl-NL')} m²</strong>${pandTotaalM2 && pandTotaalM2 !== pandOpPerceelM2 ? `<span class="muted small">BAG-pand totaal: ${pandTotaalM2.toLocaleString('nl-NL')} m²</span>` : ''}</div>` : ''}
        ${ach ? `<div class="verb-kavel-item"><span class="label">Onbebouwd</span><strong>${ach.onbebouwd_m2.toLocaleString('nl-NL')} m²</strong><span class="muted small">${ach.onbebouwd_pct}% van perceel</span></div>` : ''}
        ${ach && ach.achtererf_m2 > 0 ? `<div class="verb-kavel-item"><span class="label">Achtererf (indicatief)</span><strong>${ach.achtererf_m2.toLocaleString('nl-NL')} m²</strong></div>` : ''}
      </div>
    </div>
  ` : '';

  // Blok 2 — Bouwkundige status (chips)
  const chips = [];
  if (v.beschermd_gezicht) {
    chips.push(`<span class="chip chip-warn" title="Rijksbeschermd stads- of dorpsgezicht (RCE). Alle verbouwingen aan de buitenkant vereisen omgevingsvergunning + welstandsadvies. Geen vergunningvrij bouwen toegestaan.">🏛️ Beschermd stadsgezicht: ${escape(v.beschermd_gezicht.naam)}</span>`);
  } else {
    chips.push(`<span class="chip chip-good" title="Dit pand ligt niet binnen een rijksbeschermd stads- of dorpsgezicht. Vergunningvrij bouwen is in principe mogelijk (mits niet rijksmonument of gemeentelijk monument).">🏛️ Géén beschermd gezicht</span>`);
  }
  // Monument-status: Wkpb geeft landelijk dekkend antwoord. Amsterdam-API
  // (v.gem_monument) was de oude route; nu alleen nog fallback. We tonen
  // één van deze: rijksmonument, gemeentelijk monument, of "geen monument".
  const wkpbArr = v.wkpb || [];
  const hasRijks = wkpbArr.some(b => b.grondslag_code === 'EWE' || b.grondslag_code === 'EWA')
                   || (v.gem_monument && v.gem_monument.checked && v.gem_monument.is_monument
                       && (v.gem_monument.status || '').toLowerCase().includes('rijks'));
  const hasGemMon = wkpbArr.some(b => b.grondslag_code === 'GWA')
                    || (v.gem_monument && v.gem_monument.checked && v.gem_monument.is_monument);
  if (hasRijks) {
    chips.push(`<span class="chip chip-warn" title="Dit pand is een rijksmonument. Voor wijzigingen aan de buitenkant heb je altijd een monumentenvergunning nodig.">🏛️ Rijksmonument</span>`);
  } else if (hasGemMon) {
    chips.push(`<span class="chip chip-warn" title="Dit pand staat geregistreerd in het gemeentelijk monumentenregister. Verbouwingen aan de buitenkant vereisen een omgevingsvergunning.">🏛️ Gemeentelijk monument</span>`);
  } else {
    chips.push(`<span class="chip chip-good" title="Landelijk gecontroleerd via Kadaster Wkpb-register — geen monument-aanwijzing.">🏛️ Géén monument</span>`);
  }
  // Appartement-chip: alleen als BAG-pand > 3× perceel — dat is het enige
  // signaal waar we zeker van zijn (meerdere units in één gebouw over één
  // perceel). De bredere 'rij vs grondgebonden'-heuristiek was onbetrouwbaar
  // dus daar hangen we geen chip aan.
  const isAppartement = (perceel && pandTotaalM2
    && pandTotaalM2 / Math.max(1, perceel.oppervlakte_m2) > 3);
  if (isAppartement) {
    chips.push(`<span class="chip chip-warn" title="Appartementen en gestapelde woningen mogen niet zonder instemming van de VvE of mede-eigenaren verbouwen aan het casco of de buitenruimte.">🏢 Appartement — uitbouw vereist VvE-toestemming</span>`);
  } else if (ach && ach.uitbouw_diepte_max_m != null) {
    // Tooltip uitleg: hoe we deze ruimte berekenen.
    const tooltip = `Afstand van de pand-achtergevel tot de achterste perceelgrens, minus 1 m burenrecht-marge. Berekend uit BAG-pand-polygoon en BRK-perceelgrens; voorzijde = kant van de adres-ingang. Bij hoekpanden kan dit afwijken.`;
    if (ach.uitbouw_diepte_max_m >= 3) {
      chips.push(`<span class="chip chip-good" title="${escape(tooltip)}">↔ ${ach.uitbouw_diepte_max_m} m ruimte voor uitbouw achter</span>`);
    } else if (ach.uitbouw_diepte_max_m > 0) {
      chips.push(`<span class="chip chip-neutral" title="${escape(tooltip)}">↔ ${ach.uitbouw_diepte_max_m} m achter (krap)</span>`);
    } else {
      chips.push(`<span class="chip chip-warn" title="${escape(tooltip)}">↔ geen achtererf voor uitbouw</span>`);
    }
  }
  const chipsHTML = `<div class="verb-chips">${chips.join('')}</div>`;

  // Blok 4 — Action-buttons (deeplinks)
  const actions = [];
  if (v.omgevingsloket_url) {
    actions.push(`<a class="verb-btn" href="${escape(v.omgevingsloket_url)}" target="_blank" rel="noopener" title="Opent de Vergunningcheck. Doorloop ~10 vragen over het type verbouwing; je krijgt een definitief antwoord: vergunningvrij, meldingsplichtig of vergunning vereist.">🔎 Vergunningcheck op Omgevingsloket ↗</a>`);
  }
  if (v.ruimtelijkeplannen_url) {
    actions.push(`<a class="verb-btn" href="${escape(v.ruimtelijkeplannen_url)}" target="_blank" rel="noopener" title="Opent 'Regels op de kaart' op het Omgevingsloket. Voer daar je adres in om de geldende regels per perceel te zien.">📋 Regels op de kaart ↗</a>`);
  }
  const actionsHTML = actions.length ? `<div class="verb-actions">${actions.join('')}</div>` : '';

  // (De achtererf-disclaimer is verwijderd; criteria-checklist per card maakt
  // voldoende expliciet welke aannames we maken.)
  const infoRegel = '';

  // Blok 3 — Beslisboom-cards: 4 concrete mogelijkheden
  const mogelijkheden = Array.isArray(v.mogelijkheden) ? v.mogelijkheden : [];
  const cardsHTML = mogelijkheden.length ? `
    <div class="verb-cards-wrap">
      <div class="verb-cards-title">Wat kun je concreet?</div>
      <div class="verb-cards">
        ${mogelijkheden.map(renderMogelijkheidCard).join('')}
      </div>
    </div>
  ` : '';

  host.innerHTML = `
    ${kavelBlok}
    ${chipsHTML}
    ${infoRegel}
    ${cardsHTML}
    ${actionsHTML}
  `;

  if (prov) {
    prov.textContent = 'Bron: Kadaster BRK-Publiek · RCE Cultuurhistorie · BAG · ruimtelijkeplannen.nl · omgevingsloket.nl';
  }
}

// Plaats-naam uit een display_name halen: "Sixlaan 4, 2182AB Hillegom".
// Laatste whitespace-token achter de postcode is de plaats.
function extractPlaatsFromDisplayName(dn) {
  if (!dn) return '';
  // Match: optionele postcode gevolgd door plaatsnaam
  const m = dn.match(/\b\d{4}\s?[A-Z]{2}\s+(.+?)$/);
  return m ? m[1].trim() : '';
}

// ---- Beslisboom-card voor Verbouwingsmogelijkheden ----
// Toont één concrete mogelijkheid met kleur-status en toelichting. De 'level'
// komt uit de backend-beslisboom: good (groen/ja) / neutral (oranje/mits) /
// warn (rood/vergunning-plicht) / unknown (grijs/data ontbreekt).
function renderMogelijkheidCard(m) {
  if (!m) return '';
  const level = m.level || 'unknown';
  const icon = m.icon || '·';
  // Vergunningcheck-bevestiging: alleen tonen dat de activiteit op deze
  // locatie geldig is (DSO heeft 'm herkend). Aantallen activiteiten/vragen
  // weglaten — die aantallen zijn het totaal van alle varianten (dakkapel
  // voor/zij/achter, nieuw/vervangen etc.) en de user hoeft via conditional
  // branching maar 5-15 vragen te doorlopen, niet de volle 347. Dat
  // getal-weergeven verwart meer dan dat het helpt.
  const vc = m.vergunningcheck || null;
  const vcBadge = (vc && vc.aantal_activiteiten > 0) ? `
    <div class="verb-card-vc" title="De overheid herkent deze activiteit op dit adres in de officiële planregels. Voor een sluitend ja/nee-antwoord doorloop je een korte vragenlijst op Omgevingsloket.">
      <span class="verb-card-vc-dot"></span>
      ✓ Gecheckt bij Omgevingsloket
    </div>
  ` : '';
  // Criteria-checklist (alleen voor uitbouw, waar Bbl 8 voorwaarden heeft).
  // Toont per Bbl-criterium of het automatisch is gecheckt (✓), faalt (✗), of
  // door user bevestigd moet worden (?). Dat vervangt de vage "wel checken
  // op Omgevingsloket" door een concrete transparante lijst.
  const criteria = Array.isArray(m.criteria) ? m.criteria : [];
  const criteriaBlock = criteria.length ? `
    <details class="verb-card-crits">
      <summary>Bouwregels-checklist (${criteria.filter(c => c.status === 'pass').length}/${criteria.length} automatisch gecheckt)</summary>
      <ul>
        ${criteria.map(c => {
          const icon = c.status === 'pass' ? '✓' : c.status === 'fail' ? '✗' : '?';
          return `<li class="crit-${escape(c.status)}">
            <span class="crit-icon">${icon}</span>
            <span class="crit-label">${escape(c.label)}</span>
            <span class="crit-detail">${escape(c.detail || '')}</span>
          </li>`;
        }).join('')}
      </ul>
    </details>
  ` : '';
  return `
    <div class="verb-card verb-card-${escape(level)}" title="${escape(m.detail || '')}">
      <div class="verb-card-head">
        <span class="verb-card-icon">${escape(icon)}</span>
        <strong class="verb-card-titel">${escape(m.titel || '')}</strong>
      </div>
      <div class="verb-card-samenvatting">${escape(m.samenvatting || '')}</div>
      <div class="verb-card-detail">${escape(m.detail || '')}</div>
      ${criteriaBlock}
      ${vcBadge}
    </div>
  `;
}

function renderBebouwingsBar(perceelM2, pandM2) {
  const pct = Math.max(1, Math.min(99, Math.round(100 * pandM2 / perceelM2)));
  return `
    <div class="verb-bar" aria-label="Bebouwingsgraad">
      <div class="verb-bar-fill" style="width:${pct}%"></div>
      <span class="verb-bar-label">${pct}% bebouwd</span>
    </div>
  `;
}

// Lazy loader voor woning-extras (RCE WFS rijksmonument + Overpass-groen).
// Deze worden NA de hoofd-render opgehaald, zodat de pagina snel toont.
// Bij succes: append de extras cells aan de woning-grid.
async function loadWoningExtrasAsync(adres, baseCells) {
  if (!adres || !adres.wgs84 || !adres.rd) return;
  const params = new URLSearchParams({
    lat: String(adres.wgs84.lat),
    lon: String(adres.wgs84.lon),
    rd_x: String(adres.rd.x),
    rd_y: String(adres.rd.y),
    gemeentecode: adres.gemeentecode || '',
  });
  try {
    const r = await fetch(`${API_BASE}/woning-extras?${params.toString()}`);
    if (!r.ok) return;
    const ex = await r.json();
    if (!ex || !ex.available) return;
    // APPEND-ONLY: we voegen de extras (rijksmonument, groen) toe aan de
    // bestaande grid zonder de andere cells opnieuw te bouwen. Belangrijk
    // omdat loadWozAsync tegelijk de WOZ-cel upgrade van buurt → pand;
    // als we hier renderGrid() aanroepen met oude baseCells, zouden we die
    // pand-WOZ overschrijven met buurt-WOZ uit de oorspronkelijke render.
    const grid = document.getElementById('s-woning-grid');
    if (!grid) return;
    const extras = [];
    const rijksHTML = renderRijksmonument(ex.rijksmonument);
    if (rijksHTML) extras.push(rijksHTML);
    const groenHTML = renderGroen(ex.groen);
    if (groenHTML) extras.push(groenHTML);
    if (extras.length) {
      grid.insertAdjacentHTML('beforeend', extras.join(''));
    }
  } catch (e) {
    // Stil falen — de hoofd-4 cells zijn al getoond.
  }
}

async function loadKlimaatAsync(adres) {
  if (!adres || !adres.wgs84 || !adres.rd) return;
  const params = new URLSearchParams({
    lat: String(adres.wgs84.lat),
    lon: String(adres.wgs84.lon),
    rd_x: String(adres.rd.x),
    rd_y: String(adres.rd.y),
  });
  try {
    const r = await fetch(`${API_BASE}/klimaat?${params.toString()}`);
    if (!r.ok) throw new Error(`API ${r.status}`);
    const k = await r.json();
    renderKlimaat(k);
  } catch (e) {
    const grid = document.getElementById('s-klimaat-grid');
    if (grid) grid.innerHTML = `<p class="muted small">Klimaat-data tijdelijk niet beschikbaar.</p>`;
  }
}

function renderBereikbaarheidSkeleton() {
  const section = document.getElementById('s-bereikbaarheid');
  const host = document.getElementById('s-bereikbaarheid-content');
  if (!section || !host) return;
  host.innerHTML = `
    <ul class="bereik-list">
      ${Array.from({ length: 4 }, () => `
        <li class="bereik-item">
          <span class="bereik-icoon">·</span>
          <span class="bereik-main">
            <span class="skel-bar skel-label"></span>
            <span class="skel-bar skel-sub"></span>
          </span>
          <span class="skel-bar skel-dist"></span>
        </li>
      `).join('')}
    </ul>
  `;
  section.hidden = false;
}

async function loadWozAsync(bagVboId, wozBuurt) {
  if (!bagVboId) return;
  try {
    const r = await fetch(`${API_BASE}/woz?bag_vbo_id=${encodeURIComponent(bagVboId)}`);
    if (!r.ok) return;
    const w = await r.json();
    if (!w.available || !w.huidige_waarde_eur) return;
    // Vervang de WOZ-cel in sectie 1 (woning) met pand-specifieke waarde.
    // Target: het 3e veld in s-woning-grid (volgorde Bouwjaar/Oppervlakte/
    // WOZ/Energielabel). Werkt robuust door te zoeken op <strong> die het
    // buurtgemiddelde nu toont.
    const wozEur = '€ ' + w.huidige_waarde_eur.toLocaleString('nl-NL');
    const peil = w.huidige_peildatum ? w.huidige_peildatum.substr(0, 4) : '';
    const trend = w.trend_pct_per_jaar;
    // Chip: kleur op basis van stijging
    let level = 'neutral', chipText = 'WOZ-waarde bekend';
    if (trend != null) {
      if (trend >= 3) { level = 'good'; chipText = `+${trend}% per jaar`; }
      else if (trend <= -2) { level = 'warn'; chipText = `${trend}% per jaar`; }
      else { level = 'neutral'; chipText = `${trend > 0 ? '+' : ''}${trend}% per jaar`; }
    }
    // Laatste 3 peiljaren expliciet — historie is nieuwste eerst, we draaien
    // om zodat de pijl-richting (oud → nieuw) natuurlijk leest. Een 3-punts
    // reeks toont meteen of een buurt structureel stijgt, of net pas dit jaar.
    const hist = w.historie || [];
    let histLine = '';
    const laatste3 = hist.slice(0, 3).reverse(); // 3 nieuwste, oud → nieuw
    if (laatste3.length >= 2) {
      histLine = laatste3
        .filter(h => h && h.peildatum && h.waarde_eur != null)
        .map(h => `${h.peildatum.substr(0,4)}: €${h.waarde_eur.toLocaleString('nl-NL')}`)
        .join(' → ');
    }
    // Schrijf de nieuwe WOZ-cel
    const grid = document.getElementById('s-woning-grid');
    if (!grid) return;
    // Zoek het .field waarvan de .label 'WOZ' bevat
    const fields = grid.querySelectorAll('.field');
    for (const f of fields) {
      const lbl = f.querySelector('.label');
      if (lbl && lbl.textContent.includes('WOZ')) {
        f.innerHTML = `
          <span class="label">WOZ-waarde (dit pand)</span>
          <strong>${escape(wozEur)}</strong>
          <p class="chip chip-${level}">${escape(chipText)}</p>
          <p class="refline">Peildatum ${escape(peil)} · bron WOZ-waardeloket</p>
          ${histLine ? `<p class="hint">${escape(histLine)}</p>` : ''}
        `;
        break;
      }
    }
  } catch (_) {
    // Silent fallback — buurt-WOZ blijft zichtbaar
  }
}

async function loadBereikbaarheidAsync(adres) {
  if (!adres || !adres.wgs84) return;
  const params = new URLSearchParams({
    lat: String(adres.wgs84.lat),
    lon: String(adres.wgs84.lon),
  });
  try {
    const r = await fetch(`${API_BASE}/bereikbaarheid?${params.toString()}`);
    if (!r.ok) throw new Error(`API ${r.status}`);
    const b = await r.json();
    renderBereikbaarheid(b);
  } catch (e) {
    const host = document.getElementById('s-bereikbaarheid-content');
    if (host) host.innerHTML = `<p class="muted small">Bereikbaarheid-data tijdelijk niet beschikbaar.</p>`;
  }
}

// ---- Sectie 8 · Bereikbaarheid (OV-halten + route-tellingen) ----
// OSM-data voor NL OV-routes is niet compleet — lijnen-aantallen zijn
// ondergrens. Als 0: geen zichtbare lijn-info, wel naam + afstand.
function renderBereikbaarheid(b) {
  const section = document.getElementById('s-bereikbaarheid');
  const host = document.getElementById('s-bereikbaarheid-content');
  if (!section || !host) return;
  if (!b || b.available === false) { section.hidden = true; return; }

  const rows = [];
  // OV per modaliteit
  const ov = [
    { key: 'trein', icoon: '🚆', label: 'Treinstation' },
    { key: 'metro', icoon: '🚇', label: 'Metro' },
    { key: 'tram',  icoon: '🚋', label: 'Tram' },
    { key: 'bus',   icoon: '🚌', label: 'Bus' },
  ];
  for (const o of ov) {
    const h = b[o.key];
    if (!h) continue;
    // Voor treinen: interne NS-trajectnummers (4100, 8100) zijn onbruikbaar
    // voor reizigers. In plaats daarvan tonen we IC/Sprinter-aantal + top-3
    // bestemmingen (from/to uit OSM-route-relations).
    let detail = '';
    if (o.key === 'trein') {
      const bits = [];
      if (h.aantal_ic > 0) bits.push(`<span class="bereik-lijn">${h.aantal_ic}× IC</span>`);
      if (h.aantal_sprinter > 0) bits.push(`<span class="bereik-lijn">${h.aantal_sprinter}× Sprinter</span>`);
      const dest = (h.bestemmingen || []).slice(0, 4).join(', ');
      const destMore = h.bestemmingen && h.bestemmingen.length > 4 ? ` +${h.bestemmingen.length - 4}` : '';
      detail = bits.join(' ') + (dest ? ` · <span class="muted small">naar ${escape(dest)}${destMore}</span>` : '');
    } else {
      // Bus/tram/metro: lijn-refs zijn wel betekenisvol (17, 15, 52, etc)
      const lijnenStr = (h.lijnen && h.lijnen.length)
        ? `<span class="bereik-lijnen">${h.lijnen.slice(0, 12).map(l => `<span class="bereik-lijn">${escape(String(l))}</span>`).join(' ')}${h.lijnen.length > 12 ? ` <span class="muted small">+${h.lijnen.length - 12}</span>` : ''}</span>`
        : '';
      detail = lijnenStr;
    }
    rows.push(`
      <li class="bereik-item">
        <span class="bereik-icoon">${o.icoon}</span>
        <span class="bereik-main">
          <span class="bereik-naam">${escape(h.naam || o.label)}</span>
          <span class="bereik-sub">${escape(o.label)}${detail ? ' · ' + detail : ''}</span>
        </span>
        <span class="bereik-dist">${formatMeters(h.meters)}</span>
      </li>
    `);
  }
  // Auto — dichtstbijzijnde snelweg-oprit
  if (b.snelweg) {
    rows.push(`
      <li class="bereik-item">
        <span class="bereik-icoon">🛣️</span>
        <span class="bereik-main">
          <span class="bereik-naam">Oprit snelweg${b.snelweg.naam ? ' — ' + escape(b.snelweg.naam) : ''}</span>
          <span class="bereik-sub">Auto-ontsluiting</span>
        </span>
        <span class="bereik-dist">${formatMeters(b.snelweg.meters)}</span>
      </li>
    `);
  }
  // Grote werkcentra met geschatte OV-reistijd
  let werkHTML = '';
  if (b.werkcentra && b.werkcentra.length) {
    const wcRows = b.werkcentra.map(w => {
      const tijd = w.ov_min != null ? `~${w.ov_min} min OV` : '';
      return `
      <li class="bereik-item">
        <span class="bereik-icoon">🏙️</span>
        <span class="bereik-main">
          <span class="bereik-naam">${escape(w.stad)}</span>
          <span class="bereik-sub">${escape(w.station)}${tijd ? ' · ' + tijd : ''}</span>
        </span>
        <span class="bereik-dist">${w.km} km</span>
      </li>
    `;
    }).join('');
    werkHTML = `
      <h4 class="bereik-section-title">Afstand tot grote werkcentra</h4>
      <ul class="bereik-list">${wcRows}</ul>
    `;
  }

  if (rows.length === 0 && !werkHTML) {
    host.innerHTML = '<p class="muted small">Geen OV-halten of snelwegopritten binnen loop-/fietsafstand gevonden.</p>';
    section.hidden = false;
    return;
  }
  host.innerHTML = `
    ${rows.length ? `<h4 class="bereik-section-title">Dichtstbijzijnde halten &amp; oprit</h4><ul class="bereik-list">${rows.join('')}</ul>` : ''}
    ${werkHTML}
  `;
  section.hidden = false;
}

// ---- Sectie 6 · Klimaatrisico (bodem-aware) ----
// Backend retourneert een lijst risicos[] gefilterd op wat relevant is voor
// de bodemsoort van deze locatie. Hier rendert de UI die dynamisch als
// grid-2 cards. Legacy fallback als backend een oude response levert.
function renderKlimaat(k) {
  const grid = document.getElementById('s-klimaat-grid');
  if (!grid) return;
  const risks = (k && Array.isArray(k.risicos)) ? k.risicos : [];

  if (risks.length === 0) {
    // Legacy fallback voor oude responses (paalrot + hittestress velden).
    const paalrotExtra = k && k.paalrot && k.paalrot.buurt
      ? `Buurt ${escape(k.paalrot.buurt)} · ${k.paalrot.aantal_panden?.toLocaleString('nl-NL') || '?'} panden (worst-case scenario)`
      : null;
    const cells = [];
    if (k && k.paalrot) cells.push(fieldHTML('Funderingsrisico (paalrot)', k.paalrot,
      it => it.value != null ? `${it.value}%` : '—', paalrotExtra));
    if (k && k.hittestress) cells.push(fieldHTML('Hittestress (warme nachten)', k.hittestress,
      it => it.label ? `${it.label} (klasse ${it.value}/5)` : '—'));
    renderGrid('s-klimaat-grid', cells);
    return;
  }

  // Directe grid zonder bodemtype-header — de filter is achter de schermen
  // logic, niet iets om aan de gebruiker uit te leggen. De relevante
  // risico's verschijnen gewoon; irrelevante zijn er simpelweg niet.
  //
  // BELANGRIJK: schrijf direct in s-klimaat-grid (dat IS al een grid-2).
  // Een geneste <div class="grid-2"> zou als enkel element in de outer
  // grid belanden en daardoor alleen de linker helft vullen (bug die
  // zichtbaar was: alle kolommen geperst in helft breedte).
  const cells = risks.map(r => renderKlimaatRisk(r)).filter(Boolean);
  renderGrid('s-klimaat-grid', cells);
}

function renderKlimaatRisk(r) {
  if (!r) return '';
  // Compose "waarde" text: pct / klasse X/5 / waarde + eenheid
  let display = '—';
  if (r.pct != null) display = `${r.pct}%`;
  else if (r.klasse != null) {
    // Klasse 0 = expliciet 'geen risico' (bv. overstroming bij NoData)
    // — toon als 'geen' i.p.v. verwarrend 'klasse 0/5'.
    display = r.klasse === 0 ? 'geen' : `klasse ${r.klasse}/5`;
  }
  else if (r.waarde != null && r.eenheid) {
    // Waarde 0 = expliciet 'geen risico' (overstromingsdiepte NoData)
    if (r.waarde === 0) {
      display = 'geen';
    } else if (r.key === 'overstroming_diepte' && r.waarde >= 100) {
      // Bij overstromingsdiepte: ook meters tonen
      display = `${(r.waarde / 100).toFixed(1)} m`;
    } else {
      display = `${r.waarde} ${r.eenheid}`;
    }
  }

  // Extra-regel: voor paalrot/verschilzetting de aantal-panden info
  const extra = r.aantal_panden
    ? `${r.aantal_panden.toLocaleString('nl-NL')} panden in ${escape(r.buurtnaam || 'de buurt')}`
    : null;

  // Reformat als field-blok zodat het qua stijl bij de rest past.
  // We bouwen het handmatig omdat fieldHTML een 'indicator' met .value verwacht.
  const ref = r.ref;
  const parts = [`<div class="field"><span class="label">${escape(r.label)}</span><strong>${escape(display)}</strong>`];
  if (ref) {
    parts.push(`<p class="chip chip-${ref.chip_level}">${escape(ref.chip_text)}</p>`);
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

// ---- Woning-extras: Rijksmonument / Erfpacht / Groen ----
// Losse field-cellen in sectie 1 grid. Tonen alleen als data er is.

function renderRijksmonument(rm) {
  if (!rm || !rm.monument_nummer) return '';
  const cat = rm.subcategorie || rm.hoofdcategorie || 'monument';
  const urlLink = rm.url
    ? ` <a href="${escape(rm.url)}" target="_blank" rel="noopener" class="onderwijs-info-link" title="Open Rijksmonumentenregister">register ↗</a>`
    : '';
  return `<div class="field">
    <span class="label">Rijksmonument</span>
    <strong>🏛️ Ja${urlLink}</strong>
    <p class="chip chip-warn">verbouwregels</p>
    <p class="refline">Nr. ${escape(String(rm.monument_nummer))} · ${escape(cat)}</p>
    <p class="meaning">Verbouwing vereist monumenten-vergunning; verkoop kan subsidie-voordelen hebben maar restricties op ingrepen.</p>
  </div>`;
}

function renderErfpacht(ef) {
  if (!ef) return '';
  const lvl = ef.niveau === 'hoog' ? 'warn' : ef.niveau === 'middel' ? 'neutral' : 'good';
  return `<div class="field">
    <span class="label">Erfpacht-prevalentie</span>
    <strong>${escape(ef.niveau)}</strong>
    <p class="chip chip-${lvl}">~${ef.pct_schatting}% van gemeente</p>
    <p class="refline">Gemeente-niveau · pand-specifiek via BRK/notaris</p>
    <p class="meaning">${escape(ef.toelichting)} Vraag altijd naar canon/afkoop bij aankoop.</p>
  </div>`;
}

function renderGroen(g) {
  if (!g || !g.straal_m) return '';
  const pct = g.groen_pct || 0;
  const level = pct >= 20 ? 'good' : pct >= 8 ? 'neutral' : 'warn';
  const chip = pct >= 20 ? 'veel groen' : pct >= 8 ? 'gemengd' : 'weinig groen';
  const ha = (g.groen_m2 / 10000).toFixed(2);
  const meaning = pct >= 20
    ? 'Ruim groen direct om het adres: parken, tuinen of bos binnen loopafstand.'
    : pct >= 8
    ? 'Enig groen in loopafstand — typisch stedelijk gemengd.'
    : 'Weinig openbaar groen direct om het adres. Voor parken moet je wat verder lopen.';
  return `<div class="field">
    <span class="label">Groen in straat (${g.straal_m} m)</span>
    <strong>${ha} ha</strong>
    <p class="chip chip-${level}">${chip}</p>
    <p class="refline">${pct}% van cirkel · ${g.aantal_elementen} stukken</p>
    <p class="meaning">${meaning}</p>
  </div>`;
}

// ---- Kaart (MapLibre GL) + externe viewer-links ----
// Gebruikt PDOK BRT Achtergrondkaart als gratis basiskaart (geen API-key).
// Pand-polygoon wordt per request uit de BAG WFS opgehaald en als overlay
// getekend. Externe viewers (Google Street View, Satelliet, BAG-viewer) staan
// rechts onder de kaart — zodat de user zelf kan inzoomen op details die we
// niet zelf kunnen tekenen (3D gebouwen, foto's).

let _map = null;
let _mapCurrentLatLon = null;  // onthoudt laatst getoonde adres voor lazy tab-loading
let _mapPandCentroid = null;   // [lat, lon] van het pand voor Street View heading

async function renderMap(d) {
  const el = document.getElementById('s-map');
  if (!el || !d.adres || !d.adres.wgs84) return;
  const { lat, lon } = d.adres.wgs84;
  if (!lat || !lon) { el.hidden = true; return; }
  el.hidden = false;
  _mapCurrentLatLon = { lat, lon, displayName: d.adres.display_name };
  _mapPandCentroid = null;
  _pandGeometryCache = null;  // nieuwe scan -> oude cache weg

  // PREFETCH pand-polygoon meteen (parallel aan de rest). Als user direct
  // op Street View klikt krijgt hij een fallback-view met alleen adres-
  // coördinaten; zodra polygoon binnen is DRAAIT de camera naar het pand.
  const pandId = (d.woning && d.woning.bag_pand_id) || null;
  if (pandId) _prefetchPand(pandId);

  // Reset tabs: Google Maps is default — bekend + rijk met POIs/wegennamen
  _activateTab('gmaps');

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

  // Polygoon-overlay: als polygoon al geprefetched is, meteen tekenen.
  // Anders laadt _prefetchPand() hem nog, en haalt loadPandPolygon hem
  // daarna uit de cache zonder dubbele WFS-call.
  const pandForMap = (d.woning && d.woning.bag_pand_id) || null;
  if (pandForMap) loadPandPolygon(pandForMap);
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
  if (view === 'gmaps') _loadGoogleMaps();
  // Resize kaart wanneer we terugkomen (MapLibre heeft dit nodig na hidden)
  if (view === 'map' && _map) setTimeout(() => _map.resize(), 50);
}

function _loadGoogleMaps() {
  const pane = document.getElementById('map-gmaps');
  if (!pane || !_mapCurrentLatLon) return;
  const { lat, lon, displayName } = _mapCurrentLatLon;
  const key = window.GOOGLE_MAPS_API_KEY;
  if (!key) {
    pane.innerHTML = `
      <div class="map-fallback">
        <p>Google Maps weergave vereist een Embed-key.</p>
        <a class="map-btn" target="_blank" rel="noopener"
           href="https://www.google.com/maps?q=${encodeURIComponent(displayName)}">
          Open in Google Maps ↗
        </a>
      </div>`;
    return;
  }
  const wanted = `gm:${lat},${lon}`;
  if (pane.dataset.loaded === wanted) return;
  pane.dataset.loaded = wanted;
  // place-mode: Google kaart met marker op het adres + POIs + wegennamen
  const url = `https://www.google.com/maps/embed/v1/place`
    + `?key=${encodeURIComponent(key)}`
    + `&q=${encodeURIComponent(displayName)}`
    + `&zoom=17`;
  pane.innerHTML = `<iframe loading="lazy" allowfullscreen src="${url}"></iframe>`;
}

// Tab-klik handlers (eenmaal binden bij page load)
document.addEventListener('click', (e) => {
  const tab = e.target.closest('.map-tab[data-view]');
  if (tab) _activateTab(tab.dataset.view);
});

function _loadStreetView() {
  const pane = document.getElementById('map-streetview');
  if (!pane || !_mapCurrentLatLon) return;
  const { lat, lon } = _mapCurrentLatLon;
  const key = window.GOOGLE_MAPS_API_KEY;
  if (!key) {
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
  // Twee modi:
  //  A. Centroid beschikbaar → camera 20m zuidelijk van pand, heading
  //     berekent bearing naar centroid. Camera kijkt richting pand.
  //  B. Geen centroid nog → INSTANT fallback: adres-lat/lon met heading=0.
  //     Google's Street View pano-lookup zoekt de dichtstbijzijnde foto
  //     automatisch; resultaat is vaak al redelijk gericht op de woning.
  //     Wanneer polygoon binnenkomt, herlaadt _prefetchPand() dit iframe
  //     met de juiste heading (camera draait alsnog naar het pand).
  // Eerdere implementatie offsette 35m naar het zuiden als "straat-punt";
  // bij straten die anders lopen landde dat bij een verkeerd huisnummer.
  // Nieuwe aanpak: geef Google het EXACTE adres-coord; Google's pano-lookup
  // vindt zelf de dichtstbijzijnde buitenpano (is bijna altijd correct).
  // Heading berekenen we uit pand-polygoon als die er is — dan wijst de
  // camera meteen naar het pand. Anders laat Google z'n default behouden.
  const centroid = _mapPandCentroid;
  let heading = 0;
  if (centroid) {
    // Bearing van adres-coord naar pand-centroid — klein verschil maar
    // geeft Google een hint welke kant op te kijken na 't vinden van de pano.
    heading = _bearing(lat, lon, centroid[0], centroid[1]);
  }
  const wanted = `sv:${lat.toFixed(6)},${lon.toFixed(6)}:${Math.round(heading)}`;
  if (pane.dataset.loaded === wanted) return;
  pane.dataset.loaded = wanted;
  const url = `https://www.google.com/maps/embed/v1/streetview`
    + `?key=${encodeURIComponent(key)}`
    + `&location=${lat},${lon}`
    + `&heading=${Math.round(heading)}`
    + `&pitch=5&fov=90`
    + `&source=outdoor`;
  // allow-attribute voorkomt Permissions-Policy violations in de console;
  // Google Street View heeft accelerometer/gyroscope nodig voor 360°-panning.
  pane.innerHTML = `<iframe loading="lazy" allowfullscreen allow="accelerometer; gyroscope; fullscreen" src="${url}"></iframe>`;
}

// Centroid (simpel gemiddelde) van een GeoJSON Polygon/MultiPolygon, als [lat, lon]
function _polygonCentroidLL(geom) {
  // GeoJSON is [lon, lat]; we flatten alle rings/polygons en middelen
  let pts = [];
  const collect = c => {
    if (typeof c[0] === 'number') { pts.push(c); return; }
    c.forEach(collect);
  };
  if (geom.coordinates) collect(geom.coordinates);
  if (!pts.length) return null;
  const lon = pts.reduce((s, p) => s + p[0], 0) / pts.length;
  const lat = pts.reduce((s, p) => s + p[1], 0) / pts.length;
  return [lat, lon];
}

// Great-circle bearing van (lat1,lon1) naar (lat2,lon2), in graden [0-360)
function _bearing(lat1, lon1, lat2, lon2) {
  const toRad = d => d * Math.PI / 180, toDeg = r => r * 180 / Math.PI;
  const φ1 = toRad(lat1), φ2 = toRad(lat2);
  const Δλ = toRad(lon2 - lon1);
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
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

// Prefetch polygoon bij scan — los van kaart-rendering. Cache op pandId
// zodat we niet opnieuw fetchen als user naar Kadaster-tab gaat.
let _pandGeometryCache = null;

async function _prefetchPand(pandId) {
  try {
    const r = await fetch(`${API_BASE}/pand-geometry?pand_id=${pandId}`);
    if (!r.ok) return;
    const gj = await r.json();
    if (!gj || !gj.geometry) return;
    _pandGeometryCache = gj;
    _mapPandCentroid = _polygonCentroidLL(gj.geometry);

    // Zodra we de centroid hebben: als de user op Street View staat,
    // herlaad die met de juiste heading (camera draait naar pand).
    const activeTab = document.querySelector('.map-tab.active');
    if (activeTab && activeTab.dataset.view === 'streetview') {
      _loadStreetView();
    }
  } catch (_) { /* stille fout */ }
}

async function loadPandPolygon(pandId) {
  // Kaart-tab: gebruikt gecachte geometry als beschikbaar; anders ophalen.
  try {
    let gj = _pandGeometryCache;
    if (!gj) {
      const r = await fetch(`${API_BASE}/pand-geometry?pand_id=${pandId}`);
      if (!r.ok) return;
      gj = await r.json();
      if (!gj || !gj.geometry) return;
      _pandGeometryCache = gj;
      _mapPandCentroid = _polygonCentroidLL(gj.geometry);
    }

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

// Map score_5 (1-5) naar CSS-level voor kleuring
function _levelForScore(s) {
  if (s >= 4) return 'good';
  if (s <= 2) return 'warn';
  return 'neutral';
}

function renderVraag(v) {
  const icon = escape(v.icoon || '•');
  const vraag = escape(v.vraag || '');
  const samenvatting = escape(v.samenvatting || '');
  const score10 = v.score_10 != null ? v.score_10 : null;
  const label = escape(v.label || v.score_label || '');
  const advies = escape(v.advies || '');

  // Kleur-thema van de vraag-card = agg van de categorieën (score 10)
  const vraagLevel = score10 != null
    ? (score10 >= 7 ? 'good' : score10 >= 4 ? 'neutral' : 'warn')
    : 'neutral';

  const categorieen = (v.categorieen || []).map(c => {
    const s = c.score_5 || 3;
    const lvl = _levelForScore(s);
    const pct = (s / 5) * 100;
    const factoren = (c.factoren || []).map(f => `
      <li class="vraag-factor vf-${f.level || 'neutral'}">
        <span class="vf-dot"></span>
        <span class="vf-label">${escape(f.label)}</span>
        <span class="vf-value">${escape(f.value_text)}</span>
      </li>`).join('');
    return `
      <details class="cat cat-${lvl}">
        <summary>
          <div class="cat-row">
            <span class="cat-icon">${escape(c.icoon || '•')}</span>
            <span class="cat-naam">${escape(c.naam)}</span>
            <span class="cat-label">${escape(c.label || '')}</span>
          </div>
          <div class="cat-bar-row">
            <span class="cat-bar"><span class="cat-bar-fill dim-${lvl}" style="width:${pct}%"></span></span>
            <span class="cat-score">${s}<span class="cat-score-max">/5</span></span>
          </div>
          <p class="cat-sam">${escape(c.samenvatting || '')}</p>
        </summary>
        <ul class="vraag-factoren">${factoren}</ul>
      </details>`;
  }).join('');

  const scoreBadge = score10 != null
    ? `<span class="vraag-score10">${score10}<span class="vraag-score10-max">/10</span></span>`
    : '';
  const adviesBlock = advies ? `
    <div class="vraag-advies">
      <span class="vraag-advies-icoon">💡</span>
      <p>${advies}</p>
    </div>` : '';

  return `
    <article class="vraag-card vraag-${vraagLevel}">
      <header class="vraag-header">
        <span class="vraag-icoon">${icon}</span>
        <h3 class="vraag-title">${vraag}</h3>
        ${scoreBadge}
        <span class="vraag-badge">${label}</span>
      </header>
      <p class="vraag-sam">${samenvatting}</p>
      <div class="vraag-cats">${categorieen}</div>
      ${adviesBlock}
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
  // Voeg empirisch percentiel toe: "Top X% van Nederland" — eerlijker dan
  // de officiële klasse 9 die ~13% van NL pakt. Komt uit ECDF op 1556-sample.
  const ctx = formatTopPct(cover.top_pct_nl);
  setText('cover-meaning', ctx ? `${prefix} ${ctx} ${betekenis}` : `${prefix} ${betekenis}`);
  // Vul de balk met het echte percentiel-below (% van NL met lagere score),
  // niet meer met de lineaire (klasse-1)/8 mapping. Voor afw=+0.27 (Damrak)
  // wordt dat 96% i.p.v. 100% — visueel correcter.
  const fill = document.getElementById('cover-fill');
  if (fill) {
    const fillPct = (cover.pct_below_nl != null)
      ? cover.pct_below_nl
      : (cover.percentile_nl || 0);
    fill.style.width = `${fillPct}%`;
  }
  el.dataset.level = cover.score >= 7 ? 'good' : cover.score >= 4 ? 'neutral' : 'warn';

  // Chips (Energielabel, WOZ-trend, Paalrot) zijn uit de cover verwijderd —
  // ze tellen niet mee in de Leefbaarometer-score en suggereerden ten
  // onrechte dat ze bijdroegen aan de 9/9. (Gaan mogelijk naar een aparte
  // 'Wat valt op'-sectie boven de kaart.)

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
  // Severity bepaalt of het een prominente strook (strong) of subtiele
  // hint (mild) wordt. Bij geen waarschuwing rendert er niets.
  renderCoverDims(
    cover.dimensies || [],
    cover.waarschuwing,
    cover.waarschuwing_severity,
  );

  // Trend-sectie: hoe ontwikkelt deze buurt zich? (2-jaar + 10-jaar)
  renderCoverOntwikkeling(cover.ontwikkeling);
}

// Format "Top X% van Nederland" — handelt edge cases:
//   - top_pct ≥ 50  → "minder dan 50% van NL ligt hierboven" (te neutraal, skip)
//   - top_pct ≥ 5   → "Top 7% van Nederland."
//   - top_pct ≥ 0.5 → "Top 1% van Nederland."
//   - top_pct < 0.5 → "Top <1% van Nederland." (sample-tail-uiteinde)
function formatTopPct(topPct) {
  if (topPct == null || isNaN(topPct)) return '';
  if (topPct >= 50) {
    // Onderkant — formuleer als "boven X%"
    const below = (100 - topPct).toFixed(0);
    return `Boven ${below}% van Nederland scoort lager.`;
  }
  if (topPct < 0.5) return 'Top <1% van Nederland.';
  if (topPct < 1)   return 'Top 1% van Nederland.';
  return `Top ${Math.round(topPct)}% van Nederland.`;
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

function renderCoverDims(dims, waarschuwing, severity) {
  const el = document.getElementById('cover-dims');
  if (!el) return;
  if (!dims.length) { el.innerHTML = ''; return; }
  const rows = dims.map((d) => {
    const pct = Math.max(3, (d.score - 1) / 8 * 100);
    const level = d.score >= 7 ? 'good' : d.score >= 4 ? 'neutral' : 'warn';
    // Visueel markeren als deze sub-dimensie de zwakke plek is (≤3) —
    // belangrijke leesbevestiging dat de waarschuwing erboven hierover gaat.
    const markClass = d.score <= 3 ? ' dim-row-flag' : '';
    return `
      <li class="dim-row${markClass}">
        <div class="dim-info">
          <span class="dim-label">${escape(d.label)}</span>
          <span class="dim-desc">${escape(d.beschrijving)}</span>
        </div>
        <span class="dim-bar"><span class="dim-bar-fill dim-${level}" style="width:${pct}%"></span></span>
        <span class="dim-score">${d.score}<span class="dim-max">/9</span></span>
      </li>
    `;
  }).join('');
  // Waarschuwing met severity-classe: 'strong' = oranje strook, 'mild' = subtiele zin
  const waarschHTML = waarschuwing
    ? `<div class="cover-waarschuwing cover-waarschuwing-${severity || 'mild'}">
         <span class="cover-waarschuwing-icon">${severity === 'strong' ? '⚠️' : 'ℹ️'}</span>
         <span class="cover-waarschuwing-text">${escape(waarschuwing)}</span>
       </div>`
    : '';
  el.innerHTML = `
    <div class="cover-dims-header">Opbouw van de score</div>
    ${waarschHTML}
    <ul class="cover-dims-list">${rows}</ul>
  `;
}

// ---- Cover: ontwikkeling (trend over tijd) ----
// Toont 2 blokken (recent 2-jaar + lange termijn 10-jaar), elk met een chip
// (verbeterd/stabiel/verslechterd), de klasse op een 1-9 schaal, en de
// dimensie die het meest is veranderd. Rendert in #cover-ontwikkeling;
// dat element wordt dynamisch aan de cover toegevoegd als het nog niet bestaat.
function renderCoverOntwikkeling(ontwikkeling) {
  const cover = document.getElementById('s-cover');
  if (!cover) return;
  let host = document.getElementById('cover-ontwikkeling');
  const hasData =
    ontwikkeling && (ontwikkeling.recent || ontwikkeling.lang);
  if (!hasData) { if (host) host.innerHTML = ''; return; }
  if (!host) {
    host = document.createElement('div');
    host.id = 'cover-ontwikkeling';
    host.className = 'cover-ontwikkeling';
    // Invoegen vóór de provenance (laatste <p.prov> in s-cover)
    const prov = cover.querySelector('.prov');
    if (prov) cover.insertBefore(host, prov);
    else cover.appendChild(host);
  }

  const blocks = [];
  if (ontwikkeling.recent) blocks.push(ontwikkelingBlock(ontwikkeling.recent));
  if (ontwikkeling.lang) blocks.push(ontwikkelingBlock(ontwikkeling.lang));

  host.innerHTML = `
    <div class="cover-dims-header">Hoe ontwikkelt deze buurt zich?</div>
    <div class="trend-grid">${blocks.join('')}</div>
  `;
}

function ontwikkelingBlock(o) {
  const chipLevel = o.chip_level || 'neutral';
  const arrow = o.klasse >= 7 ? '↑' : o.klasse <= 3 ? '↓' : '→';
  // Toon ALLE significante veranderingen (top verbetering + top verslechtering).
  // Fallback naar legacy 'sterkste_verandering' voor oude API-responses.
  const changes = Array.isArray(o.veranderingen) && o.veranderingen.length
    ? o.veranderingen
    : (o.sterkste_verandering ? [o.sterkste_verandering] : []);
  const changeLines = changes.map(c => {
    const lvl = c.richting === 'verbeterd' ? 'good' : 'warn';
    const arr = c.richting === 'verbeterd' ? '↑' : '↓';
    // Gradatie uit backend ('licht verbeterd' / 'matig verslechterd' / etc.)
    // Fallback voor oude responses: alleen richting.
    const richting = c.richting_tekst || c.richting;
    // Toon ook de klasse (bv. 6/9) zodat duidelijk is hoe ver iets is bewogen
    const klasseTag = c.klasse != null
      ? `<span class="trend-dim-klasse">${c.klasse}/9</span>`
      : '';
    return `
      <div class="trend-dim trend-dim-${lvl}">
        <span class="trend-dim-arrow">${arr}</span>
        <span class="trend-dim-label">${escape(c.label)}</span>
        <span class="trend-dim-dir">${escape(richting)}</span>
        ${klasseTag}
      </div>
    `;
  }).join('');
  return `
    <div class="trend-block trend-${chipLevel}">
      <div class="trend-head">
        <span class="trend-arrow">${arrow}</span>
        <span class="trend-horizon">${escape(o.horizon || o.periode || '')}</span>
        <span class="trend-chip trend-chip-${chipLevel}">${escape(capitalize(o.label || ''))}</span>
      </div>
      <div class="trend-desc">${escape(o.beschrijving || '')}</div>
      ${changeLines}
      <div class="trend-period">${escape(o.periode || '')}</div>
    </div>
  `;
}

// ---- Voorzieningen-lijst met filter-chips ----
// Filter-categorieen mappen naar de 'categorie'-tag die de backend meegeeft.
// 'Alles' is default. State wordt in window._voorzFilter bewaard zodat
// resize/rerender van kaart de filter niet reset.
const VOORZ_FILTERS = [
  { key: 'alles',         label: 'Alles',         icon: '·' },
  { key: 'kinderen',      label: 'Kinderen',      icon: '👶' },
  { key: 'zorg',          label: 'Zorg',          icon: '🏥' },
  { key: 'boodschappen',  label: 'Boodschappen',  icon: '🛒' },
  { key: 'transport',     label: 'Transport',     icon: '🚆' },
  { key: 'sport',         label: 'Sport & groen', icon: '⚽' },
  { key: 'cultuur',       label: 'Cultuur',       icon: '📚' },
  { key: 'entertainment', label: 'Entertainment', icon: '🍴' },
];

let _voorzData = null;   // volledige voorzieningen-respons
let _voorzFilter = 'alles';

// Toont een skeleton-loader (8 fake rijen) zodat de sectie ruimte inneemt
// terwijl de /voorzieningen endpoint laadt. Voorkomt 'jumpy' UI.
function renderVoorzieningenSkeleton() {
  const list = document.getElementById('voorz-list');
  const filters = document.getElementById('voorz-filters');
  if (filters) filters.innerHTML = '<div class="voorz-loading-note">Voorzieningen worden opgehaald uit OpenStreetMap…</div>';
  if (!list) return;
  const rows = Array.from({ length: 8 }, () => `
    <li class="voorz-item voorz-skeleton">
      <span class="voorz-emoji">·</span>
      <span class="voorz-main"><span class="skel-bar skel-label"></span><span class="skel-bar skel-sub"></span></span>
      <span class="voorz-bar"><span class="skel-bar"></span></span>
      <span class="voorz-dist"><span class="skel-bar skel-dist"></span></span>
    </li>
  `).join('');
  list.innerHTML = rows;
}

// Haalt /voorzieningen op nadat de hoofdpagina al is gerenderd. Werkt met
// lat/lon uit het adres-object; buurtcode/gemeentecode voor CBS-fallback.
async function loadVoorzieningenAsync(adres) {
  if (!adres || !adres.wgs84) return;
  const params = new URLSearchParams({
    lat: String(adres.wgs84.lat),
    lon: String(adres.wgs84.lon),
    buurtcode: adres.buurtcode || '',
    gemeentecode: adres.gemeentecode || '',
  });
  try {
    const r = await fetch(`${API_BASE}/voorzieningen?${params.toString()}`);
    if (!r.ok) throw new Error(`API ${r.status}`);
    const voorz = await r.json();
    renderVoorzieningenList(voorz);
  } catch (e) {
    const list = document.getElementById('voorz-list');
    if (list) list.innerHTML = `<li class="muted small">Voorzieningen konden niet worden geladen (${escape(e.message)}).</li>`;
    const filters = document.getElementById('voorz-filters');
    if (filters) filters.innerHTML = '';
  }
}

function renderVoorzieningenList(voorzieningen) {
  _voorzData = voorzieningen;
  _voorzFilter = 'alles';
  renderVoorzFilters();
  renderVoorzItems();
}

function renderVoorzFilters() {
  const el = document.getElementById('voorz-filters');
  if (!el) return;
  const items = (_voorzData && _voorzData.items) || [];
  // Verberg filter-chips die voor dit adres helemaal geen items opleveren
  const beschikbaar = new Set(items.map(v => v.categorie).concat(['alles']));
  el.innerHTML = VOORZ_FILTERS
    .filter(f => beschikbaar.has(f.key))
    .map(f => {
      const active = f.key === _voorzFilter ? ' active' : '';
      return `<button class="voorz-filter${active}" data-filter="${f.key}">
        <span class="vf-icon">${f.icon}</span>${escape(f.label)}
      </button>`;
    }).join('');
}

function renderVoorzItems() {
  const el = document.getElementById('voorz-list');
  if (!el) return;
  let items = (_voorzData && _voorzData.items) || [];
  if (_voorzFilter !== 'alles') {
    items = items.filter(v => v.categorie === _voorzFilter);
  }
  if (items.length === 0) {
    el.innerHTML = '<li class="muted">Geen items in deze categorie.</li>';
    return;
  }
  const maxKm = Math.min(10, Math.max(...items.map(v => v.km || 0)));
  el.innerHTML = items.map((v) => {
    // Meters heeft voorrang (OSM exact). Km is altijd aanwezig als fallback.
    const meters = v.meters != null ? v.meters : (v.km ? Math.round(v.km * 1000) : 0);
    const km = v.km || 0;
    const widthPct = maxKm > 0 ? Math.max(2, Math.min(100, 100 * km / maxKm)) : 0;
    const display = meters < 1000
      ? `${meters} m`
      : `${(meters / 1000).toFixed(meters < 10000 ? 1 : 0)} km`;
    const nearClass = meters <= 500 ? 'v-near' : meters <= 2000 ? 'v-mid' : 'v-far';
    // Naam (OSM) onder het label — bv. "Treinstation — Amsterdam Centraal"
    // Bij CBS-fallback tonen we een "gemeente-gemiddelde" disclaimer.
    const naamHtml = v.naam
      ? `<span class="voorz-naam">${escape(v.naam)}</span>`
      : (v.source === 'cbs'
          ? '<span class="voorz-naam voorz-approx">gemeente-gemiddelde</span>'
          : '');
    return `
      <li class="voorz-item ${nearClass}">
        <span class="voorz-emoji">${v.emoji || '•'}</span>
        <span class="voorz-main">
          <span class="voorz-label">${escape(v.label || v.type)}</span>
          ${naamHtml}
        </span>
        <span class="voorz-bar"><span class="voorz-bar-fill" style="width:${widthPct}%"></span></span>
        <span class="voorz-dist">${display}</span>
      </li>
    `;
  }).join('');
}

// Filter-chip klikhandler (event delegation, 1x gebonden)
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.voorz-filter');
  if (!btn) return;
  _voorzFilter = btn.dataset.filter || 'alles';
  renderVoorzFilters();
  renderVoorzItems();
});

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
