// Warrant Scanner, IV Surface, and Options Scanner tabs.

let currentData = [];

let currentOptionsData = [];

let sortCol = null;

let sortAsc = true;

let optSortCol = null;

let optSortAsc = true;

let allSelected = false;

let allSelectedIV = false;

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

function toggleSelectAllIV() {
  const select = document.getElementById("ivStockSelect");
  allSelectedIV = !allSelectedIV;
  for (let opt of select.options) opt.selected = allSelectedIV;
  document.querySelector("#tab-ivsurface .sm").textContent = allSelectedIV ? "Deselect All" : "Select All";
}

function toggleAddStock() {
  const form = document.getElementById("addStockForm");
  form.style.display = form.style.display === "none" ? "flex" : "none";
}

async function addStock() {
  const code = document.getElementById("newStockCode").value.trim();
  const name = document.getElementById("newStockName").value.trim();
  if (!code) return;
  const select = document.getElementById("stockSelect");
  for (let opt of select.options) {
    if (opt.value === code) { toggleAddStock(); return; }
  }
  const option = document.createElement("option");
  option.value = code;
  option.textContent = name ? `${code} ${name}` : code;
  option.selected = true;
  select.appendChild(option);
  document.getElementById("newStockCode").value = "";
  document.getElementById("newStockName").value = "";
  toggleAddStock();
  await saveCustomStocks();
}

async function loadCustomStocks() {
  try {
    const res = await api("/get_custom_stocks");
    const stocks = await res.json();
    const select = document.getElementById("stockSelect");
    stocks.forEach(s => {
      for (let opt of select.options) { if (opt.value === s.code) return; }
      const option = document.createElement("option");
      option.value = s.code;
      option.textContent = s.name ? `${s.code} ${s.name}` : s.code;
      select.appendChild(option);
    });
  } catch (e) {}
}

async function saveCustomStocks() {
  const select = document.getElementById("stockSelect");
  const custom = Array.from(select.options)
    .filter(o => !DEFAULT_STOCKS.includes(o.value))
    .map(o => ({ code: o.value, name: o.textContent.replace(o.value, "").trim() }));
  await api("/save_custom_stocks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(custom),
  });
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

async function fetchData() {
  const filters = getFilters();
  if (!filters.stock_codes.length) {
    document.getElementById("status").textContent = "Please select at least one stock.";
    return;
  }
  document.getElementById("status").textContent = "Fetching…";
  document.getElementById("tableContainer").innerHTML = "";
  document.getElementById("downloadBtn").style.display = "none";

  const res = await api("/fetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  const data = await res.json();
  currentData = data.rows;
  setStatusWithAge("warrants", "status", `${data.count} warrants`, data);
  document.getElementById("downloadBtn").style.display = "inline-block";
  renderTable(currentData);
  return data;
}

function refreshWarrants() {
  return refreshNow("warrants",
    document.getElementById("status"),
    document.getElementById("refreshWarrantsBtn"),
    fetchData);
}

function renderTable(rows) {
  if (!rows.length) {
    document.getElementById("tableContainer").innerHTML = "<p style='padding:16px;color:var(--muted)'>No results.</p>";
    return;
  }
  const cols = Object.keys(rows[0]);
  let html = "<table><thead><tr>";
  cols.forEach((c, i) => {
    const arrow = sortCol === i ? (sortAsc ? " ▲" : " ▼") : "";
    html += `<th onclick="sortBy(${i})">${c}${arrow}</th>`;
  });
  html += "</tr></thead><tbody>";
  rows.forEach(row => {
    const cls = row.type === "Call" ? "call" : "put";
    html += "<tr>";
    cols.forEach(c => {
      const val = row[c];
      html += `<td>${c === "type" ? `<span class="${cls}">${val}</span>` : val}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody></table>";
  document.getElementById("tableContainer").innerHTML = html;
}

function sortBy(colIndex) {
  const key = Object.keys(currentData[0])[colIndex];
  if (sortCol === colIndex) sortAsc = !sortAsc;
  else { sortCol = colIndex; sortAsc = true; }
  currentData.sort((a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === "number") return sortAsc ? av - bv : bv - av;
    return sortAsc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  });
  renderTable(currentData);
}

function sortByColumn(key, asc) {
  if (!currentData.length) return;
  currentData.sort((a, b) => asc ? a[key] - b[key] : b[key] - a[key]);
  renderTable(currentData);
}

async function downloadCSV() {
  const res = await api("/download", {
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

function setIVSource(src) {
  const isOpt = src === "options";
  document.getElementById("iv-warrants-controls").style.display = isOpt ? "none" : "flex";
  document.getElementById("iv-options-controls").style.display  = isOpt ? "flex" : "none";
  document.getElementById("iv-src-warrants").style.background = isOpt ? "var(--surface)" : "var(--accent)";
  document.getElementById("iv-src-warrants").style.color = isOpt ? "var(--muted)" : "#fff";
  document.getElementById("iv-src-options").style.background  = isOpt ? "var(--accent)" : "var(--surface)";
  document.getElementById("iv-src-options").style.color  = isOpt ? "#fff" : "var(--muted)";
  document.getElementById("iv-plot").innerHTML = "";
  document.getElementById("iv-status").textContent = "";
}

async function fetchIVSurfaceOptions() {
  document.getElementById("iv-status").textContent = "Building surface…";
  document.getElementById("iv-plot").innerHTML = "";
  const payload = {
    stock_codes: [document.getElementById("ivOptProduct").value],
    option_type: document.getElementById("ivOptType").value,
  };
  let data;
  try {
    const res = await api("/iv_surface_options", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch(e) {
    document.getElementById("iv-status").textContent = "Error: " + e.message;
    return;
  }
  if (data.error) {
    document.getElementById("iv-status").textContent = "Error: " + data.error;
    return;
  }
  const product = document.getElementById("ivOptProduct").options[document.getElementById("ivOptProduct").selectedIndex].text;
  const callPut = document.getElementById("ivOptType").value;
  document.getElementById("iv-status").textContent = `${data.scatter_x.length} contracts plotted`;
  const traces = [
    {
      type: "surface",
      x: data.x, y: data.y, z: data.z,
      colorscale: "Viridis",
      opacity: 0.85,
      colorbar: { title: "IV (%)", tickfont: { color: "#8b90a0" }, titlefont: { color: "#8b90a0" } },
      name: "IV Surface",
    },
    {
      type: "scatter3d",
      x: data.scatter_x, y: data.scatter_y, z: data.scatter_z,
      mode: "markers",
      marker: { size: 4, color: callPut === "Call" ? "#f87171" : "#4ade80", opacity: 0.7 },
      text: data.labels,
      hovertemplate: "%{text}<br>Strike: %{x:.0f}<br>DTE: %{y}<br>IV: %{z:.1f}%<extra></extra>",
      name: callPut + "s",
    },
  ];
  Plotly.newPlot("iv-plot", traces, {
    title: { text: `${product} ${callPut} IV Surface`, font: { color: "#e2e8f0", size: 15 } },
    paper_bgcolor: "#13161f",
    plot_bgcolor: "#13161f",
    scene: {
      bgcolor: "#0d0f16",
      xaxis: { title: "Strike", color: "#8b90a0", gridcolor: "#252836", zerolinecolor: "#252836" },
      yaxis: { title: "Days to Expiry", color: "#8b90a0", gridcolor: "#252836", zerolinecolor: "#252836" },
      zaxis: { title: "IV (%)", range: [0, 150], color: "#8b90a0", gridcolor: "#252836", zerolinecolor: "#252836" },
      camera: { eye: { x: 1.5, y: -1.5, z: 1.0 } },
    },
    legend: { font: { color: "#8b90a0" }, bgcolor: "#13161f" },
    margin: { l: 0, r: 0, t: 48, b: 0 },
  }, { responsive: true });
}

async function fetchIVSurface() {
  const select = document.getElementById("ivStockSelect");
  const stock_codes = Array.from(select.selectedOptions).map(o => o.value);
  if (!stock_codes.length) {
    document.getElementById("iv-status").textContent = "Please select at least one stock.";
    return;
  }
  document.getElementById("iv-status").textContent = "Building surface…";
  document.getElementById("iv-plot").innerHTML = "";

  const payload = {
    stock_codes,
    option_type: document.getElementById("ivOptionType").value,
    highlight_code: document.getElementById("highlightCode").value.trim(),
  };

  const res = await api("/iv_surface", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();

  if (data.error) {
    document.getElementById("iv-status").textContent = "Error: " + data.error;
    return;
  }

  document.getElementById("iv-status").textContent = `${data.scatter_x.length} warrants plotted`;

  const traces = [
    {
      type: "surface",
      x: data.x, y: data.y, z: data.z,
      colorscale: "Viridis",
      opacity: 0.85,
      colorbar: { title: "IV (%)", tickfont: { color: "#8b90a0" }, titlefont: { color: "#8b90a0" } },
      name: "IV Surface",
    },
    {
      type: "scatter3d",
      x: data.scatter_x, y: data.scatter_y, z: data.scatter_z,
      mode: "markers",
      marker: { size: 3, color: "#ef4444", opacity: 0.5 },
      text: data.codes.map((c, i) => c + " " + data.names[i]),
      hovertemplate: "%{text}<br>Strike: %{x:.0f}<br>DTE: %{y}<br>IV: %{z:.1f}%<extra></extra>",
      name: "Warrants",
    },
  ];

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
      hovertemplate: `${h.code}<br>Strike: %{x:.0f}<br>DTE: %{y}<br>IV: %{z:.1f}%<extra></extra>`,
      name: "Selected: " + h.code,
    });
  }

  Plotly.newPlot("iv-plot", traces, {
    title: { text: "Warrant IV Surface", font: { color: "#e2e8f0", size: 15 } },
    paper_bgcolor: "#13161f",
    plot_bgcolor: "#13161f",
    scene: {
      bgcolor: "#0d0f16",
      xaxis: { title: "Strike (NT$)", color: "#8b90a0", gridcolor: "#252836", zerolinecolor: "#252836" },
      yaxis: { title: "Days to Expiry", color: "#8b90a0", gridcolor: "#252836", zerolinecolor: "#252836" },
      zaxis: { title: "IV (%)", range: [0, 100], color: "#8b90a0", gridcolor: "#252836", zerolinecolor: "#252836" },
      camera: { eye: { x: 1.5, y: -1.5, z: 1.0 } },
    },
    legend: { font: { color: "#8b90a0" }, bgcolor: "#13161f" },
    margin: { l: 0, r: 0, t: 48, b: 0 },
  }, { responsive: true });
}

let _optMarket = "tw";   // "tw" = TAIFEX (European), "us" = US ADR (American)

function setOptMarket(m, btn) {
  _optMarket = m;
  _saveView("ws_optMarket", m);
  document.querySelectorAll("#optmkt-btn-tw,#optmkt-btn-us").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById("optionStockSelect").style.display = m === "tw" ? "" : "none";
  document.getElementById("optionUsSelect").style.display   = m === "us" ? "" : "none";
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

async function fetchOptionsData() {
  const filters = getOptionsFilters();
  if (!filters.stock_codes.length) {
    document.getElementById("opt-status").textContent = "Please select at least one product.";
    return;
  }
  document.getElementById("opt-status").textContent = _optMarket === "us" ? "Fetching US ADR options (Yahoo, ~15 min delayed)…" : "Fetching…";
  document.getElementById("opt-tableContainer").innerHTML = "";
  document.getElementById("optDownloadBtn").style.display = "none";

  const res = await api(_optMarket === "us" ? "/us_options" : "/fetch_options", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  const data = await res.json();
  if (data.error) {
    document.getElementById("opt-status").textContent = "Error: " + data.error;
    return;
  }
  currentOptionsData = data.rows;
  setStatusWithAge("options", "opt-status", `${data.count} options`, data);
  document.getElementById("optDownloadBtn").style.display = "inline-block";
  renderOptionsTable(currentOptionsData);
  return data;
}

function refreshOptions() {
  const kind = _optMarket === "us" ? "us_options" : "tw_options";
  return refreshNow(kind,
    document.getElementById("opt-status"),
    document.getElementById("refreshOptionsBtn"),
    fetchOptionsData);
}

function renderOptionsTable(rows) {
  const container = document.getElementById("opt-tableContainer");
  if (!rows.length) {
    container.innerHTML = "<p style='padding:16px;color:var(--muted)'>No results.</p>";
    return;
  }
  const cols = Object.keys(rows[0]);
  let html = "<table><thead><tr>";
  cols.forEach((c, i) => {
    const arrow = optSortCol === i ? (optSortAsc ? " ▲" : " ▼") : "";
    html += `<th onclick="sortOptBy(${i})">${c}${arrow}</th>`;
  });
  html += "</tr></thead><tbody>";
  rows.forEach(row => {
    const cls = row.type === "Call" ? "call" : "put";
    html += "<tr>";
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
  const res = await api("/download_options", {
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
