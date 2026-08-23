// Warrant Scanner, IV Surface, and Options Scanner tabs.

let currentData = [];

let currentOptionsData = [];

let sortCol = null;

let sortAsc = true;

let optSortCol = null;

let optSortAsc = true;

let allSelected = false;


// Remember the current view so a page reload restores it. sessionStorage
// (not localStorage) is deliberate: a reload keeps your place, but opening
// the site fresh in a new tab/window still lands on the default Taiwan
// options view. Guarded so private-mode / disabled storage can't break nav.

function toggleSelectAll() {
  const select = document.getElementById("stockSelect");
  allSelected = !allSelected;
  for (let opt of select.options) opt.selected = allSelected;
  document.querySelectorAll(".btn-row .sm")[0].textContent = allSelected ? "Deselect All" : "Select All";
}

// Unified Add / Remove product UI. Warrant = code + an auto-looked-up name
// shown as a confirm step; TW/US option = every field entered manually (no
// auto-lookup, since ADR ratios / commodity IDs aren't reliably derivable).
// All three refresh every derived select afterward via initProductSelects().

function toggleAddProduct(kind) {
  const form = document.getElementById("addForm_" + kind);
  if (form) form.style.display = form.style.display === "none" ? "flex" : "none";
}

async function lookupWarrantName() {
  const code = document.getElementById("newWarrantCode").value.trim();
  const resultEl = document.getElementById("warrantLookupResult");
  if (!code) { resultEl.textContent = ""; resultEl.dataset.name = ""; return; }
  try {
    const res = await api(`/lookup_warrant_stock?code=${encodeURIComponent(code)}`);
    const data = await res.json();
    resultEl.dataset.name = data.name || "";
    resultEl.textContent = data.name
      ? `Found: ${data.code} ${data.name}`
      : `${code}: name not found (will add with no name)`;
  } catch (e) {
    resultEl.textContent = "Lookup failed";
    resultEl.dataset.name = "";
  }
}

async function addWarrantStock() {
  const codeEl = document.getElementById("newWarrantCode");
  const code = codeEl.value.trim();
  if (!code) return;
  const resultEl = document.getElementById("warrantLookupResult");
  const name = resultEl.dataset.name || "";
  await api("/add_warrant_stock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, name }),
  });
  codeEl.value = "";
  resultEl.textContent = "";
  resultEl.dataset.name = "";
  toggleAddProduct("warrant");
  await initProductSelects();
}

async function addTwOptionProduct() {
  const code = document.getElementById("newTwCode").value.trim();
  const commodityIds = document.getElementById("newTwCommodityIds").value.trim();
  const ticker = document.getElementById("newTwTicker").value.trim();
  const exerciseRatio = document.getElementById("newTwExerciseRatio").value.trim();
  const name = document.getElementById("newTwName").value.trim();
  if (!code || !commodityIds || !ticker || !exerciseRatio) return;
  await api("/add_tw_option_product", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      code,
      commodity_ids: commodityIds.split(",").map(s => s.trim()).filter(Boolean),
      ticker,
      exercise_ratio: Number(exerciseRatio),
      name: name || null,
    }),
  });
  ["newTwCode", "newTwCommodityIds", "newTwTicker", "newTwExerciseRatio", "newTwName"]
    .forEach(id => { document.getElementById(id).value = ""; });
  toggleAddProduct("tw_option");
  await initProductSelects();
}

async function addUsOptionProduct() {
  const code = document.getElementById("newUsCode").value.trim();
  const adrTicker = document.getElementById("newUsAdrTicker").value.trim();
  const fxTicker = document.getElementById("newUsFxTicker").value.trim();
  const adrRatio = document.getElementById("newUsAdrRatio").value.trim();
  const name = document.getElementById("newUsName").value.trim();
  if (!code || !adrTicker || !fxTicker || !adrRatio) return;
  await api("/add_us_option_product", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      code, adr_ticker: adrTicker, fx_ticker: fxTicker,
      adr_ratio: Number(adrRatio), name: name || null,
    }),
  });
  ["newUsCode", "newUsAdrTicker", "newUsFxTicker", "newUsAdrRatio", "newUsName"]
    .forEach(id => { document.getElementById(id).value = ""; });
  toggleAddProduct("us_option");
  await initProductSelects();
}

const PRODUCT_REMOVE = {
  warrant:   { selectId: "stockSelect",       url: "/remove_warrant_stock" },
  tw_option: { selectId: "optionStockSelect", url: "/remove_tw_option_product" },
  us_option: { selectId: "optionUsSelect",    url: "/remove_us_option_product" },
};

async function removeSelectedProduct(kind) {
  const cfg = PRODUCT_REMOVE[kind];
  const select = document.getElementById(cfg.selectId);
  const codes = Array.from(select.selectedOptions).map(o => o.value);
  if (!codes.length) return;
  if (!confirm(`Remove ${codes.join(", ")} from the tracked list? This cannot be undone.`)) return;
  for (const code of codes) {
    await api(cfg.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  }
  await initProductSelects();
}

function getFilters() {
  const select = document.getElementById("stockSelect");
  return {
    stock_codes: Array.from(select.selectedOptions).map(o => o.value),
    option_type: document.getElementById("optionType").value,
    min_days: document.getElementById("minDays").value,
    max_days: document.getElementById("maxDays").value,
    min_leverage: document.getElementById("minLeverage").value,
    max_tv_pct: document.getElementById("maxTvPct").value,
    min_volume: document.getElementById("minVolume").value,
  };
}

async function readWarrant() {
  const filters = getFilters();
  if (!filters.stock_codes.length) {
    document.getElementById("status").textContent = "Please select at least one stock.";
    return;
  }
  document.getElementById("status").textContent = "Reading…";
  document.getElementById("tableContainer").innerHTML = "";
  document.getElementById("downloadBtn").style.display = "none";

  const res = await api("/read_warrant", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  const data = await res.json();
  // The backend only sets `error` for a genuine fetch failure; a clean scan
  // that matched nothing comes back count:0 with no error and must not be
  // dressed up as one (same distinction arb.js makes).
  if (data.error) {
    document.getElementById("status").textContent = "Error: " + data.error;
    return;
  }
  currentData = data.rows;
  setStatusWithAge("warrants", "status", `${data.count} warrants`, data);
  document.getElementById("downloadBtn").style.display = "inline-block";
  renderTable(currentData);
  return data;
}

function syncWarrant() {
  return refreshNow("warrants",
    document.getElementById("status"),
    document.getElementById("refreshWarrantsBtn"),
    readWarrant,
    document.getElementById("tableContainer"),
    document.getElementById("readWarrantBtn"));
}

// Dedicated handler for the slow universe re-scrape. Unlike syncWarrant()
// this does NOT go through refreshNow(): the route isn't in that helper's
// routeMap, the wording is about listed securities (not prices), and there's
// no price table to re-read afterward. The /sync_universe route runs
// synchronously and blocks until the scrape+store completes, so the POST
// resolving means the sync is done. Mirrors refreshNow's UX: disable the
// button while in flight, re-enable + restore on completion, surface errors.
async function syncUniverse() {
  const statusEl = document.getElementById("status");
  const btn = document.getElementById("syncUniverseBtn");
  if (!btn) return;
  const origLabel = btn.textContent;
  const wasDisabled = btn.disabled;
  btn.disabled = true;
  btn.textContent = "Syncing…";
  if (statusEl) statusEl.textContent = "Re-scraping listed securities… this takes a few minutes";
  try {
    await api("/sync_universe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (statusEl) statusEl.textContent = "Universe updated — listed securities re-scraped.";
  } catch (e) {
    if (statusEl) statusEl.textContent = "Universe sync failed: " + (e && e.message ? e.message : e);
  } finally {
    btn.disabled = wasDisabled;
    btn.textContent = origLabel;
  }
}

function escHtml(s) {
  return String(s).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Header text for columns whose key doesn't read well raw (th is uppercased by
// CSS, so an underscored key would show as BID_TIME_VALUE_PCT).
const WARRANT_COL_LABELS = {
  bid_time_value_pct: "BID TIME VALUE PCT",
  ask_time_value_pct: "ASK TIME VALUE PCT",
};

// Warrant-code search: the hit is floated to the top of whatever order the
// current sort produced and highlighted, so a code you're watching stays
// visible without narrowing the scan. Matches within the loaded results only.
function warrantSearchQuery() {
  const el = document.getElementById("warrantSearch");
  return el ? el.value.trim().toUpperCase() : "";
}

function applyWarrantSearch() {
  if (!currentData.length) return;
  renderTable(currentData);
}

function renderTable(rows) {
  if (!rows.length) {
    document.getElementById("tableContainer").innerHTML = "<p style='padding:16px;color:var(--muted)'>No results.</p>";
    return;
  }
  const query = warrantSearchQuery();
  const isHit = r => !!query && String(r.warrant_code || "").toUpperCase().includes(query);
  const ordered = query ? [...rows.filter(isHit), ...rows.filter(r => !isHit(r))] : rows;
  const cols = Object.keys(rows[0]);
  const star = can("watchlist");
  let html = "";
  if (query && !ordered.some(isHit)) {
    html += `<p style="padding:10px 16px;color:var(--muted)">No warrant code matching "${escHtml(query)}" in the current results.</p>`;
  }
  html += "<table><thead><tr>";
  if (star) html += `<th title="Watchlist">☆</th>`;
  cols.forEach((c, i) => {
    const arrow = sortCol === i ? (sortAsc ? " ▲" : " ▼") : "";
    html += `<th onclick="sortBy(${i})">${WARRANT_COL_LABELS[c] || c}${arrow}</th>`;
  });
  html += "</tr></thead><tbody>";
  ordered.forEach(row => {
    const cls = row.type === "Call" ? "call" : "put";
    html += `<tr${isHit(row) ? ' class="row-pinned"' : ""}>`;
    if (star) html += starCell("warrant", row.warrant_code, row.underlying_code,
      row.warrant_name || row.warrant_code,
      { type: row.type, strike: row.strike, days_to_expiry: row.days_to_expiry,
        exercise_ratio: row.exercise_ratio });
    cols.forEach(c => {
      // null = no quote on that side, so the derived metric doesn't exist.
      const val = row[c] === null || row[c] === undefined ? "—" : row[c];
      html += `<td>${c === "type" ? `<span class="${cls}">${val}</span>` : val}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody></table>";
  document.getElementById("tableContainer").innerHTML = html;
}

// Missing cells (no quote on that side, so no derived metric) sort last in both
// directions — never as 0, never as the string "null".
function compareRows(key, asc) {
  return (a, b) => {
    const av = a[key], bv = b[key];
    const aGone = av === null || av === undefined;
    const bGone = bv === null || bv === undefined;
    if (aGone || bGone) return aGone === bGone ? 0 : (aGone ? 1 : -1);
    if (typeof av === "number" && typeof bv === "number") return asc ? av - bv : bv - av;
    return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  };
}

function sortBy(colIndex) {
  const key = Object.keys(currentData[0])[colIndex];
  if (sortCol === colIndex) sortAsc = !sortAsc;
  else { sortCol = colIndex; sortAsc = true; }
  currentData.sort(compareRows(key, sortAsc));
  renderTable(currentData);
}

function sortByColumn(key, asc) {
  if (!currentData.length) return;
  currentData.sort(compareRows(key, asc));
  renderTable(currentData);
}

async function downloadCSV() {
  const res = await api("/read_warrant_csv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(getFilters()),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "warrants.csv";
  a.click();
}

// ── Surface filters ────────────────────────────────────────────────────────
// One control shared by both sub-tabs. The dropdown chooses which bound pair the
// Min/Max inputs edit; every filter stays applied with whatever it holds, and a
// blank bound means unbounded. IV is in vol points, the unit the z-axis uses.
const _IV_FILTER_DEFAULTS = { iv: { min: 20, max: 100 }, dte: { min: "", max: "" }, strike: { min: "", max: "" } };
const _IV_FILTER_LABEL = { iv: "IV", dte: "DTE", strike: "K" };
let _ivFilters = JSON.parse(JSON.stringify(_IV_FILTER_DEFAULTS));
let _ivFilterKind = "iv";

function setIVFilter(kind) {
  _ivFilterKind = kind;
  document.getElementById("ivFilterMin").value = _ivFilters[kind].min;
  document.getElementById("ivFilterMax").value = _ivFilters[kind].max;
  renderIVFilterSummary();
}

function onIVFilterInput() {
  _ivFilters[_ivFilterKind] = {
    min: document.getElementById("ivFilterMin").value,
    max: document.getElementById("ivFilterMax").value,
  };
  renderIVFilterSummary();
}

function resetIVFilters() {
  _ivFilters = JSON.parse(JSON.stringify(_IV_FILTER_DEFAULTS));
  setIVFilter(_ivFilterKind);
}

// All three are listed, not just the one being edited — otherwise a bound set
// under another dropdown entry silently shapes the plot.
function renderIVFilterSummary() {
  const parts = [];
  for (const k of ["iv", "dte", "strike"]) {
    const { min, max } = _ivFilters[k];
    if (min === "" && max === "") continue;
    parts.push(`${_IV_FILTER_LABEL[k]} ${min === "" ? "−∞" : min}–${max === "" ? "∞" : max}`);
  }
  const el = document.getElementById("iv-filter-summary");
  if (el) el.textContent = parts.length ? "Active: " + parts.join("  ·  ") : "No filters";
}

function ivFilterPayload() {
  const out = {};
  for (const k of ["iv", "dte", "strike"]) {
    out[k + "_min"] = _ivFilters[k].min === "" ? null : Number(_ivFilters[k].min);
    out[k + "_max"] = _ivFilters[k].max === "" ? null : Number(_ivFilters[k].max);
  }
  return out;
}

function setIVSource(src) {
  const isOpt = src === "options";
  document.getElementById("iv-warrants-controls").style.display = isOpt ? "none" : "flex";
  document.getElementById("iv-options-controls").style.display  = isOpt ? "flex" : "none";
  document.getElementById("iv-src-warrants").classList.toggle("active", !isOpt);
  document.getElementById("iv-src-options").classList.toggle("active", isOpt);
  document.getElementById("iv-plot").innerHTML = "";
  document.getElementById("iv-status").textContent = "";
}

// ── Surface rendering ──────────────────────────────────────────────────────
// Every plot carries BOTH quote sides as separate sheets. They are built from
// their own IV columns and filtered independently, so the two can cover
// different strikes and expiries — the gap between them IS the bid/ask vol
// spread, which a single mid-based sheet hides.
const _IV_SIDE = {
  ask: { colorscale: "Viridis", dot: "#ef4444", label: "Ask" },
  bid: { colorscale: "Cividis", dot: "#38bdf8", label: "Bid" },
};

function ivSurfaceTraces(data, dotSize) {
  const traces = [];
  for (const side of ["ask", "bid"]) {
    const d = data[side];
    if (!d) continue;
    const cfg = _IV_SIDE[side];
    traces.push({
      type: "surface", x: d.x, y: d.y, z: d.z,
      colorscale: cfg.colorscale, opacity: 0.8, showscale: side === "ask",
      colorbar: { title: "IV (%)", tickfont: { color: "#7b8794" }, titlefont: { color: "#7b8794" } },
      name: cfg.label + " surface", showlegend: true,
    });
    traces.push({
      type: "scatter3d", x: d.scatter_x, y: d.scatter_y, z: d.scatter_z,
      mode: "markers",
      marker: { size: dotSize, color: cfg.dot, opacity: 0.6 },
      text: d.labels,
      hovertemplate: `%{text}<br>${cfg.label} IV: %{z:.1f}%<br>Strike: %{x:.0f}<br>DTE: %{y}<extra></extra>`,
      name: cfg.label,
    });
  }
  return traces;
}

function ivPlottedCount(data) {
  return ["ask", "bid"].map(k => (data[k] ? data[k].scatter_x.length : 0));
}

function ivSceneLayout(title) {
  return {
    title: { text: title, font: { color: "#d8dee4", size: 15 } },
    paper_bgcolor: "#151a1f",
    plot_bgcolor: "#151a1f",
    scene: {
      bgcolor: "#0d1013",
      xaxis: { title: "Strike", color: "#7b8794", gridcolor: "#222a31", zerolinecolor: "#222a31" },
      yaxis: { title: "Days to Expiry", color: "#7b8794", gridcolor: "#222a31", zerolinecolor: "#222a31" },
      zaxis: { title: "IV (%)", color: "#7b8794", gridcolor: "#222a31", zerolinecolor: "#222a31" },
      camera: { eye: { x: 1.5, y: -1.5, z: 1.0 } },
    },
    legend: { font: { color: "#7b8794" }, bgcolor: "#151a1f" },
    margin: { l: 0, r: 0, t: 48, b: 0 },
  };
}

async function fetchIVSurfaceOptions() {
  document.getElementById("iv-status").textContent = "Building surfaces…";
  document.getElementById("iv-plot").innerHTML = "";
  const sel = document.getElementById("ivOptProduct");
  const payload = {
    stock_codes: [sel.value],
    option_type: document.getElementById("ivOptType").value,
    ...ivFilterPayload(),
  };
  let data;
  try {
    const res = await api("/iv_surface_options", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch (e) {
    document.getElementById("iv-status").textContent = "Error: " + e.message;
    return;
  }
  if (data.error) {
    document.getElementById("iv-status").textContent = "Error: " + data.error;
    return;
  }
  const product = sel.options[sel.selectedIndex].text;
  const callPut = document.getElementById("ivOptType").value;
  const [na, nb] = ivPlottedCount(data);
  document.getElementById("iv-status").textContent =
    `${na} ask · ${nb} bid contracts plotted`;
  Plotly.newPlot("iv-plot", ivSurfaceTraces(data, 4),
    ivSceneLayout(`${product} ${callPut} IV Surface — bid vs ask`), { responsive: true });
}

async function fetchIVSurface() {
  const select = document.getElementById("ivStockSelect");
  const stock_codes = Array.from(select.selectedOptions).map(o => o.value);
  const status = document.getElementById("iv-status");
  if (!stock_codes.length) {
    status.textContent = "Please select an underlying stock.";
    return;
  }
  // A surface spanning two underlyings interpolates across a strike range where
  // no instrument exists, so it draws the triangulation rather than the market.
  if (stock_codes.length > 1) {
    status.textContent = "Please select only one underlying stock.";
    return;
  }
  status.textContent = "Building surfaces…";
  document.getElementById("iv-plot").innerHTML = "";

  const payload = {
    stock_codes,
    option_type: document.getElementById("ivOptionType").value,
    highlight_code: document.getElementById("highlightCode").value.trim(),
    ...ivFilterPayload(),
  };

  let data;
  try {
    const res = await api("/iv_surface", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch (e) {
    status.textContent = "Error: " + e.message;
    return;
  }
  if (data.error) {
    status.textContent = "Error: " + data.error;
    return;
  }

  const [na, nb] = ivPlottedCount(data);
  status.textContent = `${na} ask · ${nb} bid warrants plotted`;

  const traces = ivSurfaceTraces(data, 3);
  if (data.highlight) {
    const h = data.highlight;
    traces.push({
      type: "scatter3d",
      x: [h.x], y: [h.y], z: [h.z],
      mode: "markers+text",
      marker: { size: 6, color: "#fbbf24", symbol: "diamond", opacity: 1.0, line: { color: "#fff", width: 2 } },
      text: [h.code],
      textposition: "top center",
      textfont: { color: "#fbbf24", size: 12 },
      hovertemplate: `${h.code}<br>Strike: %{x:.0f}<br>DTE: %{y}<br>Ask IV: %{z:.1f}%<extra></extra>`,
      name: "Selected: " + h.code,
    });
  }

  Plotly.newPlot("iv-plot", traces,
    ivSceneLayout(`${stock_codes[0]} Warrant IV Surface — bid vs ask`), { responsive: true });
}

function _setDisplay(id, value) {
  const el = document.getElementById(id);
  if (el) el.style.display = value;
}

function setOptMarket(m, btn) {
  _optMarket = m;
  _saveView("ws_optMarket", m);
  document.querySelectorAll("#optmkt-btn-tw,#optmkt-btn-us").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  _setDisplay("optionStockSelect", m === "tw" ? "" : "none");
  _setDisplay("optionUsSelect", m === "us" ? "" : "none");
  // Product add/remove controls are admin-only, so absent in user mode.
  _setDisplay("twProductBtnRow", m === "tw" ? "" : "none");
  _setDisplay("usProductBtnRow", m === "us" ? "" : "none");
  _setDisplay("addForm_tw_option", "none");
  _setDisplay("addForm_us_option", "none");
  // Min Leverage is a warrant/TW-option metric; US chain has no leverage col.
  const lev = document.getElementById("optMinLeverage").closest("label");
  if (lev) lev.style.display = m === "us" ? "none" : "";
  document.getElementById("opt-tableContainer").innerHTML = "";
  document.getElementById("opt-status").textContent = "";
  document.getElementById("optDownloadBtn").style.display = "none";
}

function getOptionsFilters() {
  const select = document.getElementById(_optMarket === "us" ? "optionUsSelect" : "optionStockSelect");
  return {
    stock_codes: Array.from(select.selectedOptions).map(o => o.value),
    option_type: document.getElementById("optionsType").value,
    min_days: document.getElementById("optMinDays").value,
    max_days: document.getElementById("optMaxDays").value,
    min_leverage: document.getElementById("optMinLeverage").value,
    min_volume: document.getElementById("optMinVolume").value,
  };
}

async function readOption() {
  const filters = getOptionsFilters();
  if (!filters.stock_codes.length) {
    document.getElementById("opt-status").textContent = "Please select at least one product.";
    return;
  }
  document.getElementById("opt-status").textContent = _optMarket === "us" ? "Reading US ADR options (Yahoo, ~15 min delayed)…" : "Reading…";
  document.getElementById("opt-tableContainer").innerHTML = "";
  document.getElementById("optDownloadBtn").style.display = "none";

  const res = await api(_optMarket === "us" ? "/read_us_option" : "/read_tw_option", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  const data = await res.json();
  // `error` is now set only for a genuine fetch failure — a clean scan that
  // matched nothing arrives as count:0 with no error and reads neutrally.
  if (data.error) {
    document.getElementById("opt-status").textContent = "Error: " + data.error;
    return;
  }
  currentOptionsData = data.rows;
  setStatusWithAge("options", "opt-status",
    data.count ? `${data.count} options` : "No options match filters", data);
  document.getElementById("optDownloadBtn").style.display = "inline-block";
  renderOptionsTable(currentOptionsData);
  return data;
}

function syncOption() {
  // Freeze the market this sync targets. readOption() reads the LIVE
  // _optMarket when it runs, so if the user switches TW<->US mid-sync, this
  // sync's trailing read must not fire — it would be an unrequested fetch for
  // whatever market the user has since switched to, clobbering their view.
  const market = _optMarket;
  const kind = market === "us" ? "us_options" : "tw_options";
  const statusEl = document.getElementById("opt-status");
  const syncingMsg = "Syncing market data… this may take a bit";
  return refreshNow(kind,
    statusEl,
    document.getElementById("refreshOptionsBtn"),
    () => {
      if (_optMarket !== market) {
        // User moved to the other market while this sync was running — the
        // data is safely stored either way; just don't render it here.
        if (statusEl.textContent === syncingMsg) statusEl.textContent = "";
        return;
      }
      return readOption();
    },
    document.getElementById("opt-tableContainer"),
    document.getElementById("readOptionBtn"));
}

function renderOptionsTable(rows) {
  const container = document.getElementById("opt-tableContainer");
  if (!rows.length) {
    container.innerHTML = "<p style='padding:16px;color:var(--muted)'>No results.</p>";
    return;
  }
  const cols = Object.keys(rows[0]);
  // Only Taiwan options are starrable — the US chain is admin-only.
  const star = can("watchlist") && _optMarket === "tw";
  let html = "<table><thead><tr>";
  if (star) html += `<th title="Watchlist">☆</th>`;
  cols.forEach((c, i) => {
    const arrow = optSortCol === i ? (optSortAsc ? " ▲" : " ▼") : "";
    html += `<th onclick="sortOptBy(${i})">${c}${arrow}</th>`;
  });
  html += "</tr></thead><tbody>";
  rows.forEach(row => {
    const cls = row.type === "Call" ? "call" : "put";
    html += "<tr>";
    if (star) html += starCell("tw_option", row.contract, row.stock_code, row.contract,
      { type: row.type, strike: row.strike, days_to_expiry: row.days_to_expiry,
        contract_size: row.exercise_ratio });
    cols.forEach(c => {
      const val = row[c] === null ? "—" : row[c];
      html += `<td>${c === "type" ? `<span class="${cls}">${val}</span>` : val}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody></table>";
  container.innerHTML = html;
}

function sortOptBy(colIndex) {
  if (!currentOptionsData.length) return;
  const key = Object.keys(currentOptionsData[0])[colIndex];
  if (optSortCol === colIndex) optSortAsc = !optSortAsc;
  else { optSortCol = colIndex; optSortAsc = true; }
  currentOptionsData.sort((a, b) => {
    const av = a[key], bv = b[key];
    if (av === null) return 1;
    if (bv === null) return -1;
    if (typeof av === "number") return optSortAsc ? av - bv : bv - av;
    return optSortAsc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  });
  renderOptionsTable(currentOptionsData);
}

function sortOptionsBy(key, asc) {
  if (!currentOptionsData.length) return;
  currentOptionsData.sort((a, b) => {
    const av = a[key] ?? (asc ? Infinity : -Infinity);
    const bv = b[key] ?? (asc ? Infinity : -Infinity);
    return asc ? av - bv : bv - av;
  });
  renderOptionsTable(currentOptionsData);
}

async function downloadOptionsCSV() {
  // US chain: build the CSV client-side from the loaded rows (the US
  // endpoint has no dedicated CSV route). TW: stream from the server.
  if (_optMarket === "us") {
    if (!currentOptionsData.length) return;
    const cols = Object.keys(currentOptionsData[0]);
    const esc = v => v == null ? "" : `"${String(v).replace(/"/g, '""')}"`;
    const csv = [cols.join(",")].concat(
      currentOptionsData.map(r => cols.map(c => esc(r[c])).join(","))
    ).join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "us_options.csv"; a.click();
    return;
  }
  const res = await api("/read_tw_option_csv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(getOptionsFilters()),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "options.csv";
  a.click();
}

// ── Arb Finder ─────────────────────────────────────────────────────
