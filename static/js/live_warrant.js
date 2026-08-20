// Live Warrant tab: polls /live_warrant_data every 500ms and diff-patches only
// the table cells that changed, instead of rebuilding the table each tick —
// the DOM-rebuild cost was the actual bottleneck in scripts/fubon_quote_viewer.py
// at a few hundred tracked codes, not the websocket or Python-side cost (issue #69).

let _lwLoaded = false;
let _lwPollTimer = null;
let _lwInFlight = false;
let _lwRows = {};           // code -> { tr, cells: {...}, ladderTr, ladderCells, name }
let _lwExpanded = new Set();
const _lwLastText = new WeakMap();

function _lwSetText(el, s) {
  if (!el) return;
  if (_lwLastText.get(el) !== s) {
    el.textContent = s;
    _lwLastText.set(el, s);
  }
}

function _lwCell(v) { return v === null || v === undefined ? "—" : v.toLocaleString(); }

function _lwAge(b) {
  if (b.age === null) return "no data yet";
  const t = b.age < 90 ? `${b.age}s` : `${Math.round(b.age / 60)}m`;
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

function _lwBuildRow(code) {
  const tr = document.createElement("tr");
  tr.className = "live-row";

  const toggleTd = document.createElement("td");
  toggleTd.className = "live-toggle";
  toggleTd.style.cursor = "pointer";
  toggleTd.onclick = () => toggleLiveRow(code);
  tr.appendChild(toggleTd);

  const codeTd = document.createElement("td");
  codeTd.textContent = code;
  tr.appendChild(codeTd);

  const nameTd = document.createElement("td");
  tr.appendChild(nameTd);

  const bidSizeTd = document.createElement("td");
  bidSizeTd.className = "bid";
  const bidTd = document.createElement("td");
  bidTd.className = "bid";
  const askTd = document.createElement("td");
  askTd.className = "ask";
  const askSizeTd = document.createElement("td");
  askSizeTd.className = "ask";
  [bidSizeTd, bidTd, askTd, askSizeTd].forEach(td => tr.appendChild(td));

  const ageTd = document.createElement("td");
  ageTd.style.color = "var(--muted)";
  tr.appendChild(ageTd);

  const removeTd = document.createElement("td");
  const removeBtn = document.createElement("button");
  removeBtn.className = "sm";
  removeBtn.textContent = "Remove";
  removeBtn.onclick = () => removeLiveWarrant(code);
  removeTd.appendChild(removeBtn);
  tr.appendChild(removeTd);

  return {
    tr, name: null,
    cells: { toggle: toggleTd, name: nameTd, bidSize: bidSizeTd, bid: bidTd, ask: askTd, askSize: askSizeTd, age: ageTd },
    ladderTr: null,
  };
}

function _lwBuildLadder(code) {
  const tr = document.createElement("tr");
  tr.className = "live-ladder-row";
  const td = document.createElement("td");
  td.colSpan = 8;
  const table = document.createElement("table");
  table.className = "live-ladder";
  table.innerHTML = "<tr><th>Level</th><th>Bid Vol</th><th>Bid</th><th>Ask</th><th>Ask Vol</th></tr>";
  const cells = [];
  for (let i = 0; i < 5; i++) {
    const row = document.createElement("tr");
    const lv = document.createElement("td"); lv.textContent = i + 1;
    const bs = document.createElement("td"); bs.className = "bid";
    const bp = document.createElement("td"); bp.className = "bid";
    const ap = document.createElement("td"); ap.className = "ask";
    const as = document.createElement("td"); as.className = "ask";
    [lv, bs, bp, ap, as].forEach(c => row.appendChild(c));
    table.appendChild(row);
    cells.push({ bs, bp, ap, as });
  }
  td.appendChild(table);
  tr.appendChild(td);
  return { tr, cells };
}

function toggleLiveRow(code) {
  if (_lwExpanded.has(code)) _lwExpanded.delete(code);
  else _lwExpanded.add(code);
  if (_lwLastData) _lwRender(_lwLastData);
}

function _lwPatchRow(el, b, code) {
  _lwSetText(el.cells.name, b.name || "-");
  el.cells.toggle.textContent = _lwExpanded.has(code) ? "▾" : "▸";
  const best = b.rows[0] || {};
  _lwSetText(el.cells.bidSize, _lwCell(best.bid_size));
  _lwSetText(el.cells.bid, _lwCell(best.bid));
  _lwSetText(el.cells.ask, _lwCell(best.ask));
  _lwSetText(el.cells.askSize, _lwCell(best.ask_size));
  _lwSetText(el.cells.age, _lwAge(b));

  if (_lwExpanded.has(code)) {
    if (!el.ladderTr) {
      const ladder = _lwBuildLadder(code);
      el.ladderTr = ladder.tr;
      el.ladderCells = ladder.cells;
      el.tr.after(ladder.tr);
    }
    b.rows.forEach((r, i) => {
      const c = el.ladderCells[i];
      _lwSetText(c.bs, _lwCell(r.bid_size));
      _lwSetText(c.bp, _lwCell(r.bid));
      _lwSetText(c.ap, _lwCell(r.ask));
      _lwSetText(c.as, _lwCell(r.ask_size));
    });
  } else if (el.ladderTr) {
    el.ladderTr.remove();
    el.ladderTr = null;
    el.ladderCells = null;
  }
}

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
    _lwPatchRow(el, b, b.code);
  });

  Object.keys(_lwRows).forEach(code => {
    if (!seen[code]) {
      _lwRows[code].tr.remove();
      if (_lwRows[code].ladderTr) _lwRows[code].ladderTr.remove();
      delete _lwRows[code];
      _lwExpanded.delete(code);
    }
  });

  if (d.books.length === 0) {
    tbody.innerHTML = "";
    document.getElementById("live-empty").style.display = "block";
  } else {
    document.getElementById("live-empty").style.display = "none";
  }
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

async function reconnectLiveWarrant() {
  const btn = document.getElementById("live-reconnect-btn");
  const statusEl = document.getElementById("live-status");
  btn.disabled = true;
  btn.textContent = "Reconnecting…";
  try {
    await apiJson("/reconnect_live_warrant", { method: "POST", headers: { "Content-Type": "application/json" } });
  } catch (e) {
    if (statusEl) statusEl.textContent = "Reconnect failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Reconnect";
  }
}
