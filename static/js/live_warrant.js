// Live Warrant tab: polls /live_warrant_data every 500ms and diff-patches only
// the table cells that changed, instead of rebuilding the table each tick —
// the DOM-rebuild cost was the actual bottleneck in scripts/fubon_quote_viewer.py
// at a few hundred tracked codes, not the websocket or Python-side cost (issue #69).
//
// Two views share the same poll/data: a flat table (one row per warrant, all
// 5 book levels inline) and an orderbook card grid (mirrors the layout of
// scripts/fubon_quote_viewer.py, one card per warrant with its own 5-row ladder).

let _lwLoaded = false;
let _lwPollTimer = null;
let _lwInFlight = false;
let _lwRows = {};           // code -> table row els, keyed by _lwBuildRow()
let _lwBooks = {};          // code -> orderbook card els, keyed by _lwBuildBook()
let _lwView = "table";      // "table" | "orderbook"
const _lwLastText = new WeakMap();

function _lwSetText(el, s) {
  if (!el) return;
  if (_lwLastText.get(el) !== s) {
    el.textContent = s;
    _lwLastText.set(el, s);
  }
}

// price (vol), e.g. "12.05 (67)" — "—" when the level has no data.
function _lwLevelText(price, size) {
  if (price === null || price === undefined) return "—";
  return `${price.toLocaleString()} (${size === null || size === undefined ? "—" : size.toLocaleString()})`;
}

function _lwAge(b) {
  if (b.age === null) return "no data yet";
  const secs = Math.round(b.age);
  const t = secs < 90 ? `${secs}s` : `${Math.round(secs / 60)}m`;
  return b.src === "rest" ? `snapshot, ${t} old` : `${t} ago`;
}

async function loadLiveWarrantOnce() {
  if (_lwLoaded) return;
  _lwLoaded = true;
  const stocks = await fetchProductList("/list_warrant_stocks");
  populateProductSelect(document.getElementById("live-scan-underlying"), stocks, { selectFirst: true });
  _lwPollTimer = setInterval(_lwPoll, 500);
  _lwPoll();
}

function setLiveView(view) {
  _lwView = view;
  document.getElementById("live-table-view").style.display = view === "table" ? "" : "none";
  document.getElementById("live-orderbook-view").style.display = view === "orderbook" ? "" : "none";
  document.getElementById("live-view-table-btn").classList.toggle("active", view === "table");
  document.getElementById("live-view-orderbook-btn").classList.toggle("active", view === "orderbook");
  if (_lwLastData) _lwRender(_lwLastData);
}

// ── Table view: one row per warrant, 5 bid + 5 ask levels inline ──────────
function _lwBuildRow(code) {
  const tr = document.createElement("tr");
  tr.className = "live-row";

  const codeTd = document.createElement("td");
  codeTd.textContent = code;
  tr.appendChild(codeTd);

  const nameTd = document.createElement("td");
  tr.appendChild(nameTd);

  // Displayed lowest bid -> highest bid, then lowest ask -> highest ask,
  // i.e. level 5..1 for bids (best bid is level 1) then level 1..5 for asks.
  const bidTds = [4, 3, 2, 1, 0].map(() => {
    const td = document.createElement("td");
    td.className = "bid";
    tr.appendChild(td);
    return td;
  });
  const askTds = [0, 1, 2, 3, 4].map(() => {
    const td = document.createElement("td");
    td.className = "ask";
    tr.appendChild(td);
    return td;
  });

  const ageTd = document.createElement("td");
  ageTd.className = "live-updated";
  tr.appendChild(ageTd);

  const removeTd = document.createElement("td");
  const removeBtn = document.createElement("button");
  removeBtn.className = "sm";
  removeBtn.textContent = "Remove";
  removeBtn.onclick = () => removeLiveWarrant(code);
  removeTd.appendChild(removeBtn);
  tr.appendChild(removeTd);

  return { tr, cells: { name: nameTd, bid: bidTds, ask: askTds, age: ageTd } };
}

function _lwPatchRow(el, b) {
  _lwSetText(el.cells.name, b.name || "-");
  // rows[i] is level i+1 (0 = best). bidTds[0] = level 5 (lowest, worst) ... bidTds[4] = level 1 (best).
  [4, 3, 2, 1, 0].forEach((lvl, i) => {
    const r = b.rows[lvl] || {};
    _lwSetText(el.cells.bid[i], _lwLevelText(r.bid, r.bid_size));
  });
  [0, 1, 2, 3, 4].forEach((lvl, i) => {
    const r = b.rows[lvl] || {};
    _lwSetText(el.cells.ask[i], _lwLevelText(r.ask, r.ask_size));
  });
  _lwSetText(el.cells.age, _lwAge(b));
}

// ── Orderbook view: one card per warrant, 5-row bid/ask ladder ────────────
function _lwBuildBook(code) {
  const root = document.createElement("div");
  root.className = "live-book";

  const h3 = document.createElement("h3");
  root.appendChild(h3);

  const table = document.createElement("table");
  table.innerHTML = "<tr><th>Bid Vol</th><th>Bid</th><th></th><th>Ask</th><th>Ask Vol</th></tr>";
  const rows = [];
  for (let i = 0; i < 5; i++) {
    const tr = document.createElement("tr");
    const bs = document.createElement("td"); bs.className = "bid";
    const bp = document.createElement("td"); bp.className = "bid";
    const lv = document.createElement("td"); lv.className = "lv"; lv.textContent = i + 1;
    const ap = document.createElement("td"); ap.className = "ask";
    const as = document.createElement("td"); as.className = "ask";
    [bs, bp, lv, ap, as].forEach(td => tr.appendChild(td));
    table.appendChild(tr);
    rows.push({ tr, bs, bp, ap, as });
  }
  root.appendChild(table);

  const meta = document.createElement("div");
  meta.className = "live-book-meta";
  const metaText = document.createTextNode("");
  const removeLink = document.createElement("a");
  removeLink.textContent = "remove";
  removeLink.href = "#";
  removeLink.onclick = (e) => { e.preventDefault(); removeLiveWarrant(code); };
  meta.appendChild(metaText);
  meta.appendChild(document.createTextNode("  "));
  meta.appendChild(removeLink);
  root.appendChild(meta);

  return { root, h3, rows, meta: metaText, title: null };
}

function _lwPatchBook(el, b) {
  const title = `${b.code}  ${b.name || "-"}`;
  if (el.title !== title) { el.h3.textContent = title; el.title = title; }

  for (let i = 0; i < el.rows.length; i++) {
    const r = b.rows[i], slot = el.rows[i];
    slot.tr.className = r && r.level === 1 ? "best" : "";
    _lwSetText(slot.bs, _lwCell(r ? r.bid_size : null));
    _lwSetText(slot.bp, _lwCell(r ? r.bid : null));
    _lwSetText(slot.ap, _lwCell(r ? r.ask : null));
    _lwSetText(slot.as, _lwCell(r ? r.ask_size : null));
  }

  const metaStr = b.age === null ? "no data yet" : _lwAge(b);
  if (el.meta.data !== metaStr) el.meta.data = metaStr;
}

function _lwCell(v) { return v === null || v === undefined ? "—" : v.toLocaleString(); }

let _lwLastData = null;

function _lwStatusLine(d) {
  const dot = d.connected ? "ok" : "warn";
  const connSummary = d.connections
    .map(c => `#${c.index}:${c.state}(${c.subs}/${d.max_subs / d.max_connections})`)
    .join(" ");
  let html = `<span class="dot ${dot}"></span><b>${d.connected ? "connected" : "not connected"}</b>`
    + ` &nbsp;|&nbsp; subscriptions <b>${d.subs}/${d.max_subs}</b>`
    + (connSummary ? ` &nbsp;|&nbsp; ${connSummary}` : "");
  if (d.session_error) html += ` &nbsp;|&nbsp; <span class="down">${d.session_error}</span>`;
  return html;
}

function _lwRender(d) {
  _lwLastData = d;
  const statusEl = document.getElementById("live-status");
  if (statusEl) statusEl.innerHTML = _lwStatusLine(d);

  if (_lwView === "table") _lwRenderTable(d);
  else _lwRenderOrderbook(d);

  const emptyEl = document.getElementById("live-empty");
  if (emptyEl) emptyEl.style.display = d.books.length === 0 ? "block" : "none";
}

function _lwRenderTable(d) {
  const tbody = document.getElementById("live-tbody");
  if (!tbody) return;

  const seen = {};
  d.books.forEach(b => {
    seen[b.code] = true;
    let el = _lwRows[b.code];
    if (!el) {
      el = _lwRows[b.code] = _lwBuildRow(b.code);
      tbody.appendChild(el.tr);
    }
    _lwPatchRow(el, b);
  });

  Object.keys(_lwRows).forEach(code => {
    if (!seen[code]) {
      _lwRows[code].tr.remove();
      delete _lwRows[code];
    }
  });

  if (d.books.length === 0) tbody.innerHTML = "";
}

function _lwRenderOrderbook(d) {
  const container = document.getElementById("live-orderbook-grid");
  if (!container) return;

  const seen = {};
  d.books.forEach(b => {
    seen[b.code] = true;
    let el = _lwBooks[b.code];
    if (!el) {
      el = _lwBooks[b.code] = _lwBuildBook(b.code);
      container.appendChild(el.root);
    }
    _lwPatchBook(el, b);
  });

  Object.keys(_lwBooks).forEach(code => {
    if (!seen[code]) {
      _lwBooks[code].root.remove();
      delete _lwBooks[code];
    }
  });

  if (d.books.length === 0) container.innerHTML = "";
}

function _lwPoll() {
  if (_lwInFlight) return;
  const tab = document.getElementById("tab-live");
  if (!tab || !tab.classList.contains("active")) return;
  _lwInFlight = true;
  apiJson("/live_warrant_data")
    .then(d => { _lwInFlight = false; _lwRender(d); })
    .catch(e => {
      _lwInFlight = false;
      const statusEl = document.getElementById("live-status");
      if (statusEl) statusEl.textContent = "server unreachable: " + (e && e.message ? e.message : e);
    });
}

async function addLiveWarrant() {
  const input = document.getElementById("live-add-code");
  const code = (input.value || "").trim();
  if (!code) return;
  const statusEl = document.getElementById("live-add-status");
  try {
    await apiJson("/add_live_warrant", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    input.value = "";
    if (statusEl) statusEl.textContent = "";
  } catch (e) {
    if (statusEl) statusEl.textContent = e.message || String(e);
  }
}

async function removeLiveWarrant(code) {
  try {
    await apiJson("/remove_live_warrant", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch (e) {
    const statusEl = document.getElementById("live-add-status");
    if (statusEl) statusEl.textContent = e.message || String(e);
  }
}

async function runLiveWarrantScan() {
  const underlying = document.getElementById("live-scan-underlying").value;
  const topN = document.getElementById("live-scan-topn").value;
  const statusEl = document.getElementById("live-scan-status");
  if (!underlying || !topN) return;
  const btn = document.getElementById("live-scan-btn");
  btn.disabled = true;
  if (statusEl) statusEl.textContent = "Scanning…";
  try {
    const res = await apiJson("/scan_live_warrant", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ underlying, top_n: Number(topN) }),
    });
    if (statusEl) statusEl.textContent = `+${res.added.length} added, -${res.removed.length} removed`;
  } catch (e) {
    if (statusEl) statusEl.textContent = e.message || String(e);
  } finally {
    btn.disabled = false;
  }
}

async function _lwSessionAction(btnId, endpoint, verb) {
  const btn = document.getElementById(btnId);
  const statusEl = document.getElementById("live-status");
  btn.disabled = true;
  btn.textContent = verb + "ing…";
  try {
    await apiJson(endpoint, { method: "POST", headers: { "Content-Type": "application/json" } });
  } catch (e) {
    if (statusEl) statusEl.textContent = verb + " failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
    btn.textContent = verb;
  }
}

function reconnectLiveWarrant() { return _lwSessionAction("live-reconnect-btn", "/reconnect_live_warrant", "Reconnect"); }
function connectLiveWarrant() { return _lwSessionAction("live-connect-btn", "/connect_live_warrant", "Connect"); }
function disconnectLiveWarrant() { return _lwSessionAction("live-disconnect-btn", "/disconnect_live_warrant", "Disconnect"); }
