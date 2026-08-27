// Live Warrant tab: polls /live_warrant_data every 500ms and diff-patches only
// the table cells that changed, instead of rebuilding the table each tick —
// the DOM-rebuild cost was the actual bottleneck in scripts/fubon_quote_viewer.py
// at a few hundred tracked codes, not the websocket or Python-side cost (issue #69).
//
// Derived columns (time value, TV%) are recomputed server-side only when a
// warrant's own best bid/ask actually moves (services/live_warrant.py's
// _recompute_if_dirty) — this file just renders whatever the payload carries,
// same 500ms poll either way.

let _lwLoaded = false;
let _lwPollTimer = null;
let _lwLogPollTimer = null;
let _lwInFlight = false;
let _lwRows = {};           // code -> table row els, keyed by _lwBuildRow()
let _lwLogLastId = 0;
const _lwLastText = new WeakMap();

// Same bounds as the Warrant Scanner's WARRANT_FILTER_SPEC (scanners.js),
// minus Leverage (this tab never computes it — that would need IV/delta,
// which are deliberately never solved here, not even internally) and minus
// Volume (this tab doesn't show a Volume column at all).
const LIVE_WARRANT_FILTER_SPEC = [
  { key: "dte", label: "Days to expiry", short: "DTE",
    min: { field: "min_days", value: 0, attrs: { min: 0, step: 1 } },
    max: { field: "max_days", value: 365, attrs: { min: 0, step: 1 } } },
  { key: "tv", label: "Time value %", short: "TV%",
    max: { field: "max_tv_pct", value: 100, attrs: { min: 0, step: 0.1 } } },
];

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
  mountBoundFilters("liveFilters", LIVE_WARRANT_FILTER_SPEC);
  _lwPollTimer = setInterval(_lwPoll, 500);
  _lwLogPollTimer = setInterval(_lwPollLog, 1000);
  _lwPoll();
}

// ── Table view: one row per warrant ────────────────────────────────────────
function _lwBuildRow(code) {
  const tr = document.createElement("tr");
  tr.className = "live-row";

  const codeTd = document.createElement("td");
  codeTd.textContent = code;
  tr.appendChild(codeTd);

  const nameTd = document.createElement("td");
  tr.appendChild(nameTd);

  // Contract terms (strike/dte/ratio) and the tick-kernel columns (time_value,
  // *_time_value_pct) all arrive on slower/async paths than the raw book — the
  // first is a one-shot REST call backfilled in the background, the second only
  // updates once the code's own best level has ticked — so they render as "—"
  // until the backend has something for them.
  const cols = ["underlying_code", "type", "underlying_price", "best_bid", "best_ask",
                "strike", "dte", "ratio",
                "time_value", "bid_time_value_pct", "ask_time_value_pct"];
  // Best Bid/Ask keep the same red/green coloring the old 10-column ladder used.
  const colorClass = { best_bid: "bid", best_ask: "ask" };
  const tds = {};
  cols.forEach(k => {
    const td = document.createElement("td");
    td.className = "live-term live-term-" + k + (colorClass[k] ? " " + colorClass[k] : "");
    tr.appendChild(td);
    tds[k] = td;
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

  return { tr, cells: { name: nameTd, age: ageTd, ...tds } };
}

// A row the backend could not fully set up: still tracked, still listed, just
// not streaming yet. The reason goes in the title attribute — never in the name,
// which is what used to render as "(FugleAPIError)".
function _lwDegraded(b) {
  if (b.pending) return b.error ? `pending — ${b.error}` : "pending — not subscribed yet";
  return b.error || "";
}

// Strike/ratio/time-value print to a fixed dp; trailing zeros on a round
// number are noise.
function _lwNum(v, dp) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(dp).replace(/\.?0+$/, "") || "0";
}

function _lwPct(v, dp) {
  if (v === null || v === undefined) return "—";
  return `${_lwNum(v, dp)}%`;
}

function _lwDte(v) {
  if (v === null || v === undefined) return "—";
  return v < 0 ? `${v}d (expired)` : `${v}d`;
}

function _lwPatchRow(el, b) {
  _lwSetText(el.cells.name, b.name || b.code);
  _lwSetText(el.cells.underlying_code, b.underlying_code || "—");
  _lwSetText(el.cells.type, b.type || "—");
  _lwSetText(el.cells.underlying_price, _lwNum(b.underlying_price, 2));
  const best = b.best || {};
  _lwSetText(el.cells.best_bid, _lwLevelText(best.bid, best.bid_size));
  _lwSetText(el.cells.best_ask, _lwLevelText(best.ask, best.ask_size));
  _lwSetText(el.cells.strike, _lwNum(b.strike, 2));
  _lwSetText(el.cells.dte, _lwDte(b.dte));
  _lwSetText(el.cells.ratio, _lwNum(b.exercise_ratio, 4));
  _lwSetText(el.cells.time_value, _lwNum(b.time_value, 2));
  _lwSetText(el.cells.bid_time_value_pct, _lwPct(b.bid_time_value_pct, 2));
  _lwSetText(el.cells.ask_time_value_pct, _lwPct(b.ask_time_value_pct, 2));
  // Near-dated warrants are the ones worth spotting at a glance.
  el.cells.dte.classList.toggle("live-dte-near", b.dte !== null && b.dte !== undefined && b.dte <= 30);
  // The underlying's own row (always first — see get_data()) reads as a stock
  // quote, not a warrant, so it gets a visual break from the rows below it.
  el.tr.classList.toggle("live-underlying-row", b.type === "Underlying");
  const degraded = _lwDegraded(b);
  if (el.degraded !== degraded) {
    el.tr.classList.toggle("live-degraded", !!degraded);
    el.tr.title = degraded;
    el.degraded = degraded;
  }
  _lwSetText(el.cells.age, _lwAge(b));
}

// Bound semantics start from logic/warrant_logic.py::_apply_warrant_filters
// (DTE: min <= dte <= max), but with one deliberate difference: on the
// Scanner, a row is a fully-computed batch snapshot, so dte is never null
// there. On this tab a freshly (re)subscribed code starts with dte still
// null — backfilled gradually by retry_pending(), not instantly — so a null
// value here means "not loaded yet", not "fails the bound", and must PASS
// rather than be hidden. Same treatment ask_time_value_pct already had.
// Getting this wrong hides every row after every redeploy until the terms
// backfill catches up.
function _lwPassesFilter(b, bounds) {
  const dte = bounds.dte || {};
  if (dte.min !== null && dte.min !== undefined
      && b.dte !== null && b.dte !== undefined && b.dte < dte.min) return false;
  if (dte.max !== null && dte.max !== undefined
      && b.dte !== null && b.dte !== undefined && b.dte > dte.max) return false;

  const tv = bounds.tv || {};
  if (tv.max !== null && tv.max !== undefined
      && b.ask_time_value_pct !== null && b.ask_time_value_pct !== undefined
      && b.ask_time_value_pct > tv.max) return false;

  return true;
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
  if (d.pending) html += ` &nbsp;|&nbsp; <span class="down">${d.pending} pending</span>`;
  if (d.terms_missing) html += ` &nbsp;|&nbsp; <span class="muted">${d.terms_missing} awaiting terms</span>`;
  if (d.errors) {
    // One line per distinct message, not per code: a throttled scan reports the
    // same error a thousand times.
    const kinds = (d.error_kinds || []).join("; ");
    html += ` &nbsp;|&nbsp; <span class="down" title="${kinds}">${d.errors} with errors</span>`;
    if (kinds) html += ` <span class="muted">(${kinds})</span>`;
  }
  if (d.session_error) html += ` &nbsp;|&nbsp; <span class="down">${d.session_error}</span>`;
  return html;
}

function _lwRender(d) {
  _lwLastData = d;
  const statusEl = document.getElementById("live-status");
  if (statusEl) statusEl.innerHTML = _lwStatusLine(d);

  _lwRenderTable(d);

  const emptyEl = document.getElementById("live-empty");
  if (emptyEl) emptyEl.style.display = d.books.length === 0 ? "block" : "none";
}

function _lwRenderTable(d) {
  const tbody = document.getElementById("live-tbody");
  if (!tbody) return;

  const bounds = bfBounds("liveFilters");
  const seen = {};
  d.books.forEach(b => {
    seen[b.code] = true;
    let el = _lwRows[b.code];
    if (!el) {
      el = _lwRows[b.code] = _lwBuildRow(b.code);
      tbody.appendChild(el.tr);
    }
    _lwPatchRow(el, b);
    el.tr.style.display = _lwPassesFilter(b, bounds) ? "" : "none";
  });

  Object.keys(_lwRows).forEach(code => {
    if (!seen[code]) {
      _lwRows[code].tr.remove();
      delete _lwRows[code];
    }
  });

  if (d.books.length === 0) tbody.innerHTML = "";
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

// ── Console log: book-change diffs, collapsed by default ───────────────────
// Only polls while the panel is open — no point paying for it collapsed.
const LW_LOG_MAX_LINES = 500;

function _lwPollLog() {
  const details = document.getElementById("live-console-log");
  if (!details || !details.open) return;
  apiJson(`/live_warrant_log?since=${_lwLogLastId}`)
    .then(d => {
      _lwLogLastId = d.latest_id;
      if (!d.entries || !d.entries.length) return;
      const body = document.getElementById("live-console-log-body");
      if (!body) return;
      const frag = document.createDocumentFragment();
      d.entries.forEach(e => {
        const line = document.createElement("div");
        // Debug checkpoints (freeze diagnostics — see services/live_warrant.py's
        // _log_debug) carry no code/recalculated-columns/duration, just a plain
        // message; highlighted so the exact last-thing-that-happened before a
        // freeze is easy to spot while scrolling a busy log.
        if (e.level === "debug") {
          line.className = "lw-log-debug";
          line.textContent = `[${e.ts}] ${e.diff}`;
        } else {
          line.textContent =
            `[${e.ts}] ${e.code} "${e.diff}" - "${e.recalculated}" - took +${e.duration_s}s`;
        }
        frag.appendChild(line);
      });
      body.appendChild(frag);
      while (body.childElementCount > LW_LOG_MAX_LINES) {
        body.removeChild(body.firstChild);
      }
      body.scrollTop = body.scrollHeight;
    })
    .catch(() => {});  // transient — next tick retries
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
  // "" is unset; "0" is a deliberate whole-chain scan, so test the string.
  if (!underlying || topN === "") return;
  const btn = document.getElementById("live-scan-btn");
  btn.disabled = true;
  // A whole-chain scan is ~1,050 REST seeds, so it is tens of seconds, not one.
  if (statusEl) statusEl.textContent = Number(topN) === 0
    ? "Subscribing the entire chain — this takes a while…" : "Scanning…";
  try {
    const res = await _lwScanRequest(underlying, Number(topN), false);
    if (statusEl) statusEl.textContent = _lwScanSummary(res);
  } catch (e) {
    if (statusEl) statusEl.textContent = e.message || String(e);
  } finally {
    btn.disabled = false;
  }
}

async function removeLiveWarrantUnderlying() {
  const underlying = document.getElementById("live-scan-underlying").value;
  const statusEl = document.getElementById("live-scan-status");
  if (!underlying) return;
  if (!confirm(`Remove every tracked warrant for ${underlying}? This does not remove ${underlying}'s own live price.`)) return;
  const btn = document.getElementById("live-remove-underlying-btn");
  btn.disabled = true;
  try {
    const r = await apiJson("/remove_live_warrant_underlying", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ underlying }),
    });
    if (statusEl) statusEl.textContent = `removed ${r.removed} warrants for ${underlying}`;
  } catch (e) {
    if (statusEl) statusEl.textContent = e.message || String(e);
  } finally {
    btn.disabled = false;
  }
}

// A refused scan (409) means nothing was changed — the resolved chain was so
// much smaller than what is tracked that a truncated catalog response is the
// likelier explanation. Deleting on that basis is how codes go missing, so the
// re-run is an explicit confirmation rather than an automatic retry.
async function _lwScanRequest(underlying, topN, force) {
  try {
    return await apiJson("/scan_live_warrant", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ underlying, top_n: topN, force }),
    });
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    if (!force && /force/i.test(msg) && confirm(msg + "\n\nApply it anyway?")) {
      return _lwScanRequest(underlying, topN, true);
    }
    throw e;
  }
}

function _lwScanSummary(res) {
  const parts = [`+${res.added.length} added, -${res.removed.length} removed`];
  if (res.chain) parts.push(`chain ${res.chain}`);
  if (res.failed && res.failed.length) parts.push(`${res.failed.length} pending retry`);
  if (res.catalog_complete === false) parts.push("catalog incomplete");
  if (res.volume_missing) parts.push(`${res.volume_missing} ranked without volume`);
  return parts.join(" · ");
}

async function retryLiveWarrant() {
  const statusEl = document.getElementById("live-status");
  const btn = document.getElementById("live-retry-btn");
  btn.disabled = true;
  try {
    const r = await apiJson("/retry_live_warrant", {
      method: "POST", headers: { "Content-Type": "application/json" },
    });
    if (statusEl) statusEl.textContent =
      `retried: ${r.subscribed} subscribed, ${r.reseeded} re-seeded, ${r.terms} terms fetched, `
      + `${r.pending} pending, ${r.terms_missing} still awaiting terms`;
  } catch (e) {
    if (statusEl) statusEl.textContent = "retry failed: " + (e.message || e);
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
