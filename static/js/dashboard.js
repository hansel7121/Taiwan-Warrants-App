// User Dashboard tab: watchlist stars, threshold alerts, multi-leg positions,
// the portfolio P&L widget, and the per-position payoff modal. Every route it
// calls scopes to the authenticated user server-side (services/db_user.py).
// Unit conventions follow Taiwan market mechanics (see CLAUDE.md):
//   warrant    quantity = board lots (張) of 1,000 units; a unit delivers
//              exercise_ratio underlying shares
//   tw_option  quantity = contracts; contract_size (2,000) shares each
//   underlying quantity = shares

const WARRANT_UNITS_PER_LOT = 1000;
const TW_CONTRACT_SIZE = 2000;
// Expiry warning window, in days, for starred instruments.
const EXPIRY_WARN_DAYS = 5;

let watchlistData = [];
let alertsData = [];
let positionsData = [];
let quoteMap = {};              // "kind:code" -> { bid, ask, iv, underlying_price, ... }
let watchSet = new Set();       // "kind:code" for the star toggles in the scanners
let _dashboardLoaded = false;

const qKey = (kind, code) => `${kind}:${code}`;

// PGRST205 = PostgREST cannot find the table. For this tab that always means
// the migration hasn't been applied, so say that instead of echoing the raw
// PostgREST payload.
function _dashError(e) {
  const msg = (e && e.message) ? e.message : String(e);
  if (msg.includes("PGRST205") || msg.includes("user_watchlist")) {
    return "Database tables not created yet — run supabase/migrations/020_user_dashboard.sql "
         + "in the Supabase SQL editor, then reload.";
  }
  return msg;
}

function _money(v) {
  if (v == null || !isFinite(v)) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}NT$${Math.abs(Math.round(v)).toLocaleString()}`;
}

function _num(v, dp = 2) {
  return (v == null || !isFinite(v)) ? "—" : Number(v).toFixed(dp);
}

// Shares delivered per quantity unit, and the cash multiplier on the quoted
// price. A warrant quote is per unit, an option quote is per share/point.
function legMultiplier(leg) {
  if (leg.kind === "warrant") return WARRANT_UNITS_PER_LOT;
  if (leg.kind === "tw_option") return leg.contract_size || TW_CONTRACT_SIZE;
  return 1;
}

function legShares(leg) {
  if (leg.kind === "warrant") return WARRANT_UNITS_PER_LOT * (leg.exercise_ratio || 1);
  if (leg.kind === "tw_option") return leg.contract_size || TW_CONTRACT_SIZE;
  return 1;
}

// ── Load ─────────────────────────────────────────────────────────────

function loadDashboardOnce() {
  if (_dashboardLoaded) return;
  _dashboardLoaded = true;
  loadDashboard();
}

async function loadDashboard() {
  const status = document.getElementById("dash-status");
  if (status) status.textContent = "Loading…";
  try {
    const [w, a, p] = await Promise.all([
      apiJson("/list_watchlist"),
      apiJson("/list_alerts"),
      apiJson("/list_positions"),
    ]);
    watchlistData = Array.isArray(w) ? w : [];
    alertsData = Array.isArray(a) ? a : [];
    positionsData = Array.isArray(p) ? p : [];
    watchSet = new Set(watchlistData.map(r => qKey(r.kind, r.code)));
    await refreshQuotes();
    if (status) status.textContent = "";
  } catch (e) {
    if (status) status.textContent = "Could not load dashboard: " + _dashError(e);
  }
  renderDashboard();
}

// One /user_quotes call covers the watchlist and every position leg.
async function refreshQuotes() {
  const seen = new Set(), instruments = [];
  const add = (kind, code, underlying) => {
    if (!kind || !code) return;
    const k = qKey(kind, code);
    if (seen.has(k)) return;
    seen.add(k);
    instruments.push({ kind, code, underlying_code: underlying || null });
  };
  watchlistData.forEach(r => add(r.kind, r.code, r.underlying_code));
  positionsData.forEach(p => (p.legs || []).forEach(
    l => add(l.kind, l.code, (l.meta && l.meta.underlying_code) || p.underlying_code)));
  if (!instruments.length) { quoteMap = {}; return; }
  const data = await apiJson("/user_quotes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruments }),
  });
  quoteMap = data.quotes || {};
}

function renderDashboard() {
  renderPnlWidget();
  renderPositions();
  renderWatchlist();
  renderAlerts();
}

// ── P&L widget ───────────────────────────────────────────────────────
// Mark-to-market on the side you would actually exit at: a long leg sells at
// the bid, a short leg is bought back at the ask.

function legMarks(leg, parent) {
  const q = quoteMap[qKey(leg.kind, leg.code)] || {};
  const exit = leg.direction > 0 ? (q.bid ?? q.ask) : (q.ask ?? q.bid);
  const mult = legMultiplier(leg);
  const cost = leg.direction * leg.quantity * mult * leg.entry_price;
  const value = (exit == null) ? null : leg.direction * leg.quantity * mult * exit;
  return { quote: q, exit, cost, value, pnl: value == null ? null : value - cost };
}

function positionMarks(position) {
  let cost = 0, value = 0, priced = 0, total = 0;
  (position.legs || []).forEach(leg => {
    const m = legMarks(leg, position);
    cost += m.cost;
    total += 1;
    if (m.value != null) { value += m.value; priced += 1; }
  });
  const complete = priced === total && total > 0;
  return { cost, value, priced, total, complete, pnl: complete ? value - cost : null };
}

function renderPnlWidget() {
  const el = document.getElementById("dash-kpis");
  if (!el) return;
  const open = positionsData.filter(p => !p.closed_at);
  let cost = 0, value = 0, unpriced = 0;
  open.forEach(p => {
    const m = positionMarks(p);
    cost += m.cost;
    value += m.value;
    if (!m.complete) unpriced += 1;
  });
  const pnl = value - cost;
  const pct = cost !== 0 ? (pnl / Math.abs(cost)) * 100 : null;
  const cls = pnl >= 0 ? "dash-up" : "dash-down";
  el.innerHTML = `
    <div class="dash-kpi"><span class="dash-kpi-k">Open positions</span>
      <b>${open.length}</b></div>
    <div class="dash-kpi"><span class="dash-kpi-k">Cost basis</span>
      <b>${_money(cost)}</b></div>
    <div class="dash-kpi"><span class="dash-kpi-k">Market value</span>
      <b>${_money(value)}</b></div>
    <div class="dash-kpi"><span class="dash-kpi-k">Unrealized P&amp;L</span>
      <b class="${cls}">${_money(pnl)}${pct == null ? "" : ` <span class="dash-kpi-pct">${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%</span>`}</b></div>
    ${unpriced ? `<div class="dash-kpi dash-kpi-warn"><span class="dash-kpi-k">No live quote</span>
      <b>${unpriced} position${unpriced > 1 ? "s" : ""}</b></div>` : ""}`;
}

// ── Positions ────────────────────────────────────────────────────────

function legLabel(leg) {
  if (leg.kind === "underlying") return `${leg.code} shares`;
  const t = leg.option_type ? ` ${leg.option_type}` : "";
  const k = leg.strike != null ? ` ${leg.strike}` : "";
  return `${leg.label || leg.code}${t}${k}`;
}

function renderPositions() {
  const el = document.getElementById("dash-positions");
  if (!el) return;
  if (!positionsData.length) {
    el.innerHTML = `<p class="dash-empty">No positions yet. Use <b>+ New Position</b> to add one.</p>`;
    return;
  }
  let html = `<table class="dash-table"><thead><tr>
    <th>Position</th><th>Legs</th><th>Nearest expiry</th>
    <th class="num">Cost</th><th class="num">Value</th><th class="num">P&amp;L</th><th></th>
  </tr></thead><tbody>`;
  positionsData.forEach(p => {
    const m = positionMarks(p);
    const legs = p.legs || [];
    const dtes = legs.map(l => l.days_to_expiry).filter(d => d != null);
    const nearest = dtes.length ? Math.min(...dtes) + "d" : "—";
    const cls = (m.pnl ?? 0) >= 0 ? "dash-up" : "dash-down";
    html += `<tr class="dash-row" onclick="openPayoffModal('${p.id}')">
      <td><b>${p.name || p.underlying_code || "Position"}</b>${p.closed_at ? ' <span class="dash-tag">closed</span>' : ""}</td>
      <td class="dash-legs">${legs.map(l =>
        `<span class="${l.direction > 0 ? "dash-long" : "dash-short"}">${l.direction > 0 ? "+" : "−"}${l.quantity} ${legLabel(l)}</span>`
      ).join("")}</td>
      <td>${nearest}</td>
      <td class="num">${_money(m.cost)}</td>
      <td class="num">${m.complete ? _money(m.value) : "—"}</td>
      <td class="num ${cls}">${m.pnl == null ? "—" : _money(m.pnl)}</td>
      <td class="num">${p.closed_at ? "" :
        `<button class="sm" onclick="event.stopPropagation();closePosition('${p.id}')">Close</button> `}<button class="sm" onclick="event.stopPropagation();deletePosition('${p.id}')">Delete</button></td>
    </tr>`;
  });
  el.innerHTML = html + "</tbody></table>";
}

// Closing keeps the row (and its history) but drops it out of the P&L widget,
// which only marks open positions.
async function closePosition(id) {
  const data = await apiJson("/close_position", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  const p = positionsData.find(x => x.id === id);
  if (p) p.closed_at = data.closed_at;
  renderDashboard();
}

async function deletePosition(id) {
  if (!confirm("Delete this position? This cannot be undone.")) return;
  await apiJson("/remove_position", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  positionsData = positionsData.filter(p => p.id !== id);
  renderDashboard();
}

// ── Watchlist ────────────────────────────────────────────────────────

function isStarred(kind, code) {
  return watchSet.has(qKey(kind, code));
}

// Star cell for a scanner table row. Payload is URI-encoded so it survives the
// HTML attribute intact. The expiry DATE is derived and stored alongside the
// DTE, because a DTE snapshot goes stale the moment it is written — and an
// expired instrument leaves the chain, so there is no live quote to re-derive
// it from later.
function starCell(kind, code, underlying, label, meta) {
  meta = Object.assign({}, meta);
  if (meta.days_to_expiry != null && meta.expiry_date == null) {
    meta.expiry_date = new Date(Date.now() + meta.days_to_expiry * 864e5)
      .toISOString().slice(0, 10);
  }
  const payload = encodeURIComponent(JSON.stringify(
    { kind, code, underlying_code: underlying, label, meta: meta || {} }));
  const on = isStarred(kind, code);
  return `<td class="star-cell" title="${on ? "Remove from watchlist" : "Add to watchlist"}"
    onclick="event.stopPropagation();toggleStar(this,'${payload}')">${on ? "★" : "☆"}</td>`;
}

async function toggleStar(el, payload) {
  const item = JSON.parse(decodeURIComponent(payload));
  const key = qKey(item.kind, item.code);
  const on = watchSet.has(key);
  const route = on ? "/remove_watchlist" : "/add_watchlist";
  el.textContent = on ? "☆" : "★";
  if (on) watchSet.delete(key); else watchSet.add(key);
  try {
    await apiJson(route, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
    });
  } catch (e) {
    // Roll the optimistic toggle back and say why — a silently un-starring
    // star just looks like the button is broken.
    el.textContent = on ? "★" : "☆";
    el.title = "Watchlist write failed: " + _dashError(e);
    if (on) watchSet.add(key); else watchSet.delete(key);
    alert("Could not update watchlist: " + _dashError(e));
    return;
  }
  _dashboardLoaded = false;   // next dashboard visit re-reads the list
}

function renderWatchlist() {
  const el = document.getElementById("dash-watchlist");
  if (!el) return;
  if (!watchlistData.length) {
    el.innerHTML = `<p class="dash-empty">Nothing starred yet. Click ☆ on any row in the Warrant or Options Scanner.</p>`;
    return;
  }
  let html = "";
  watchlistData.forEach(w => {
    const q = quoteMap[qKey(w.kind, w.code)] || {};
    const days = watchExpiryDays(w);
    const expiryTag = days == null ? ""
      : days <= 0 ? `<span class="dash-exp dash-exp-gone">expired</span>`
      : days <= EXPIRY_WARN_DAYS ? `<span class="dash-exp dash-exp-soon">${days}d left</span>`
      : `<span class="dash-exp">${days}d</span>`;
    html += `<div class="dash-watch-row">
      <div class="dash-watch-head">
        <b>${w.label || w.code}</b>${expiryTag}
        <span class="dash-watch-sub">${w.underlying_code || ""} ${w.kind === "warrant" ? "warrant" : "option"}</span>
        <button class="sm" onclick="removeStarred('${w.kind}','${w.code}')">✕</button>
      </div>
      <div class="dash-watch-quotes">
        <span>bid <b>${_num(q.bid)}</b></span>
        <span>ask <b>${_num(q.ask)}</b></span>
        <span>IV <b>${q.iv == null ? "—" : (q.iv * 100).toFixed(1) + "%"}</b></span>
        <span>spot <b>${_num(q.underlying_price)}</b></span>
      </div>
      <button class="sm" onclick="openAlertForm('${w.kind}','${w.code}','${w.underlying_code || ""}')">+ Alert</button>
    </div>`;
  });
  el.innerHTML = html;
}

async function removeStarred(kind, code) {
  await apiJson("/remove_watchlist", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, code }),
  });
  watchlistData = watchlistData.filter(w => !(w.kind === kind && w.code === code));
  watchSet.delete(qKey(kind, code));
  renderDashboard();
}

// ── Expiry alerts ────────────────────────────────────────────────────
// Every starred instrument gets an expiry alert for free — no threshold to
// configure, since an expiring warrant is always worth knowing about.

const _utcDay = (d) => Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());

// Days until a starred instrument expires, or null if unknowable.
// Prefer the live quote — it tracks the real chain. Fall back to the expiry
// date captured when it was starred, which is what makes this work at all: an
// expired instrument drops out of the chain entirely, so there is no quote left
// to read a DTE from. Rows starred before expiry_date was recorded fall back
// again to counting the star-time DTE forward from created_at.
function watchExpiryDays(w) {
  const q = quoteMap[qKey(w.kind, w.code)] || {};
  if (q.days_to_expiry != null) return Math.floor(q.days_to_expiry);
  const meta = w.meta || {};
  let expiryMs = null;
  if (meta.expiry_date) {
    expiryMs = Date.parse(meta.expiry_date + "T00:00:00Z");
  } else if (meta.days_to_expiry != null && w.created_at) {
    expiryMs = _utcDay(new Date(w.created_at)) + meta.days_to_expiry * 864e5;
  }
  if (expiryMs == null || !isFinite(expiryMs)) return null;
  return Math.round((expiryMs - _utcDay(new Date())) / 864e5);
}

// [{ item, days, level }] for anything expired or inside the warning window.
function expiryAlerts() {
  const out = [];
  watchlistData.forEach(w => {
    const days = watchExpiryDays(w);
    if (days == null) return;
    if (days <= 0) out.push({ item: w, days, level: "expired" });
    else if (days <= EXPIRY_WARN_DAYS) out.push({ item: w, days, level: "soon" });
  });
  return out.sort((a, b) => a.days - b.days);
}

function renderExpiryAlerts() {
  return expiryAlerts().map(e => `
    <div class="dash-alert ${e.level === "expired" ? "dash-alert-on" : "dash-alert-warn"}">
      <div class="dash-alert-head">
        <b>${e.item.label || e.item.code}</b>
        <span>expiry</span>
      </div>
      <div class="dash-alert-body">
        ${e.level === "expired"
          ? `<span class="dash-alert-badge">EXPIRED</span>${e.days < 0 ? ` ${-e.days}d ago` : " today"}`
          : `<span class="dash-alert-badge dash-alert-badge-warn">EXPIRES IN ${e.days}D</span>`}
      </div>
    </div>`).join("");
}

// ── Alerts ───────────────────────────────────────────────────────────
// Evaluated in the browser each time the dashboard loads, against the same
// quote snapshot the rest of the tab uses. A fire is posted back so the row
// still shows "last fired" after a reload.

function alertValue(alert) {
  const q = quoteMap[qKey(alert.kind, alert.code)] || {};
  if (alert.metric === "underlying") return q.underlying_price;
  if (alert.metric === "iv") return q.iv;
  return q[alert.metric];
}

function alertFires(alert, value) {
  if (value == null || !isFinite(value)) return false;
  return alert.direction === "above" ? value >= alert.threshold : value <= alert.threshold;
}

function renderAlerts() {
  const el = document.getElementById("dash-alerts");
  if (!el) return;
  const expiry = renderExpiryAlerts();
  if (!alertsData.length && !expiry) {
    el.innerHTML = `<p class="dash-empty">No alerts. Star an instrument, then use <b>+ Alert</b> to set a threshold. Expiry alerts appear here automatically.</p>`;
    return;
  }
  let html = expiry;
  alertsData.forEach(a => {
    const value = alertValue(a);
    const fired = alertFires(a, value);
    const unit = a.metric === "iv" ? "%" : "";
    const shown = a.metric === "iv" && value != null ? value * 100 : value;
    const thr = a.metric === "iv" ? a.threshold * 100 : a.threshold;
    if (fired) recordTrigger(a, value);
    html += `<div class="dash-alert ${fired ? "dash-alert-on" : ""}">
      <div class="dash-alert-head">
        <b>${a.code}</b>
        <span>${a.metric} ${a.direction === "above" ? "≥" : "≤"} ${thr}${unit}</span>
        <button class="sm" onclick="deleteAlert('${a.id}')">✕</button>
      </div>
      <div class="dash-alert-body">
        now <b>${shown == null ? "—" : Number(shown).toFixed(2)}${unit}</b>
        ${fired ? '<span class="dash-alert-badge">TRIGGERED</span>' : ""}
        ${a.last_triggered_at && !fired
          ? `<span class="dash-alert-last">last fired ${new Date(a.last_triggered_at).toLocaleString()}</span>` : ""}
      </div>
    </div>`;
  });
  el.innerHTML = html;
}

// Fire-and-forget: a failed write only costs the "last fired" timestamp.
function recordTrigger(alert, value) {
  apiJson("/record_alert_trigger", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: alert.id, value }),
  }).catch(() => {});
}

function openAlertForm(kind, code, underlying) {
  const modal = document.getElementById("alertModal");
  if (!modal) return;
  modal.style.display = "block";
  document.getElementById("alert-kind").value = kind;
  document.getElementById("alert-code").value = code;
  document.getElementById("alert-underlying").value = underlying || "";
  document.getElementById("alert-target").textContent = code;
  document.getElementById("alert-threshold").value = "";
  document.getElementById("alert-error").textContent = "";
}

function closeAlertForm() {
  const modal = document.getElementById("alertModal");
  if (modal) modal.style.display = "none";
}

async function submitAlert() {
  const err = document.getElementById("alert-error");
  const metric = document.getElementById("alert-metric").value;
  const raw = parseFloat(document.getElementById("alert-threshold").value);
  if (!isFinite(raw)) { err.textContent = "Threshold must be a number."; return; }
  // IV is entered as a percent but stored as a decimal, matching the quote.
  const threshold = metric === "iv" ? raw / 100 : raw;
  const body = {
    kind: document.getElementById("alert-kind").value,
    code: document.getElementById("alert-code").value,
    underlying_code: document.getElementById("alert-underlying").value || null,
    metric,
    direction: document.getElementById("alert-direction").value,
    threshold,
  };
  try {
    const data = await apiJson("/add_alert", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    alertsData.unshift(data.alert);
    closeAlertForm();
    renderAlerts();
  } catch (e) {
    err.textContent = "Could not save alert: " + _dashError(e);
  }
}

async function deleteAlert(id) {
  await apiJson("/remove_alert", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  alertsData = alertsData.filter(a => a.id !== id);
  renderAlerts();
}

// ── New position ─────────────────────────────────────────────────────

let _legSeq = 0;

function openNewPosition() {
  const modal = document.getElementById("newPositionModal");
  if (!modal) return;
  modal.style.display = "block";
  document.getElementById("np-name").value = "";
  document.getElementById("np-underlying").value = "";
  document.getElementById("np-error").textContent = "";
  document.getElementById("np-legs").innerHTML = "";
  _legSeq = 0;
  addLegRow();
}

function closeNewPosition() {
  const modal = document.getElementById("newPositionModal");
  if (modal) modal.style.display = "none";
}

// Legs are unbounded — a position can be as many as the user adds.
function addLegRow(prefill) {
  const id = ++_legSeq;
  const p = prefill || {};
  const row = document.createElement("div");
  row.className = "np-leg";
  row.id = "np-leg-" + id;
  row.innerHTML = `
    <select class="np-kind" onchange="onLegKindChange(${id})">
      <option value="warrant">Warrant</option>
      <option value="tw_option">TW option</option>
      <option value="underlying">Underlying shares</option>
    </select>
    <select class="np-direction">
      <option value="1">Long</option>
      <option value="-1">Short</option>
    </select>
    <input class="np-code" placeholder="Code" value="${p.code || ""}" />
    <input class="np-qty" type="number" step="any" min="0" placeholder="Qty (張)" value="${p.quantity || ""}" />
    <input class="np-entry" type="number" step="any" placeholder="Entry price" value="${p.entry_price || ""}" />
    <select class="np-type">
      <option value="">—</option>
      <option value="Call">Call</option>
      <option value="Put">Put</option>
    </select>
    <input class="np-strike" type="number" step="any" placeholder="Strike" />
    <input class="np-dte" type="number" step="1" min="0" placeholder="DTE" />
    <input class="np-ratio" type="number" step="any" placeholder="Ratio" title="Warrant: underlying shares per unit. Option: contract size (2000)." />
    <button class="sm" onclick="document.getElementById('np-leg-${id}').remove()">✕</button>`;
  document.getElementById("np-legs").appendChild(row);
  onLegKindChange(id);
}

// Warrants are long-only in Taiwan (cash-settled, cannot be written), and a
// share leg has no strike/expiry — reflect both in the form.
function onLegKindChange(id) {
  const row = document.getElementById("np-leg-" + id);
  if (!row) return;
  const kind = row.querySelector(".np-kind").value;
  const isUnderlying = kind === "underlying";
  const dir = row.querySelector(".np-direction");
  ["np-type", "np-strike", "np-dte", "np-ratio"].forEach(c => {
    row.querySelector("." + c).style.display = isUnderlying ? "none" : "";
  });
  row.querySelector(".np-qty").placeholder =
    kind === "warrant" ? "Qty (張)" : kind === "tw_option" ? "Contracts" : "Shares";
  row.querySelector(".np-ratio").placeholder = kind === "warrant" ? "Ratio" : "2000";
  if (kind === "warrant") {
    dir.value = "1";
    dir.disabled = true;
    dir.title = "Taiwan warrants are long-only — they cannot be written or shorted.";
  } else {
    dir.disabled = false;
    dir.title = "";
  }
}

function collectLegs() {
  const legs = [];
  document.querySelectorAll("#np-legs .np-leg").forEach(row => {
    const kind = row.querySelector(".np-kind").value;
    const code = row.querySelector(".np-code").value.trim();
    const quantity = parseFloat(row.querySelector(".np-qty").value);
    const entry_price = parseFloat(row.querySelector(".np-entry").value);
    if (!code) return;
    const ratio = parseFloat(row.querySelector(".np-ratio").value);
    const leg = {
      kind, code,
      label: code,
      direction: kind === "warrant" ? 1 : parseInt(row.querySelector(".np-direction").value, 10),
      quantity, entry_price,
      option_type: row.querySelector(".np-type").value || null,
      strike: parseFloat(row.querySelector(".np-strike").value),
      days_to_expiry: parseInt(row.querySelector(".np-dte").value, 10),
    };
    if (kind === "warrant") leg.exercise_ratio = isFinite(ratio) ? ratio : 1;
    if (kind === "tw_option") leg.contract_size = isFinite(ratio) ? ratio : TW_CONTRACT_SIZE;
    ["strike", "days_to_expiry"].forEach(k => { if (!isFinite(leg[k])) leg[k] = null; });
    legs.push(leg);
  });
  return legs;
}

async function submitPosition() {
  const err = document.getElementById("np-error");
  const legs = collectLegs();
  if (!legs.length) { err.textContent = "Add at least one leg with a code."; return; }
  const bad = legs.find(l => !isFinite(l.quantity) || l.quantity <= 0 || !isFinite(l.entry_price));
  if (bad) { err.textContent = `Leg ${bad.code} needs a positive quantity and an entry price.`; return; }
  const body = {
    name: document.getElementById("np-name").value.trim() || null,
    underlying_code: document.getElementById("np-underlying").value.trim() || null,
    legs,
  };
  try {
    await apiJson("/add_position", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    closeNewPosition();
    await loadDashboard();
  } catch (e) {
    err.textContent = "Could not save position: " + _dashError(e);
  }
}

// ── Payoff modal ─────────────────────────────────────────────────────
// P&L of the whole position across underlying price, at a chosen number of
// days from today. A leg whose expiry has passed by then settles at intrinsic;
// a leg still alive is marked with Black-Scholes on its entry IV.

let _payoffPosition = null;

function openPayoffModal(id) {
  const p = positionsData.find(x => x.id === id);
  if (!p) return;
  _payoffPosition = p;
  const modal = document.getElementById("payoffModal");
  if (!modal) return;
  modal.style.display = "block";
  const legs = p.legs || [];
  const maxDte = Math.max(0, ...legs.map(l => l.days_to_expiry || 0));
  const slider = document.getElementById("payoff-dte");
  slider.max = maxDte;
  slider.value = maxDte;
  document.getElementById("payoff-title").textContent =
    (p.name || p.underlying_code || "Position") + " — payoff";
  renderPayoff();
}

function closePayoffModal() {
  const modal = document.getElementById("payoffModal");
  if (modal) modal.style.display = "none";
  _payoffPosition = null;
}

function legIv(leg) {
  const q = quoteMap[qKey(leg.kind, leg.code)] || {};
  return leg.iv || q.iv || 0.4;   // fall back to a plausible vol so the curve still draws
}

// P&L of one leg at underlying S, `days` from today. Warrant and option values
// are per-share Black-Scholes scaled by the leg's own share count; the quoted
// entry price is scaled by its own multiplier.
function legPnlAt(leg, S, days) {
  const mult = legMultiplier(leg);
  const entryNotional = leg.direction * leg.quantity * mult * leg.entry_price;
  if (leg.kind === "underlying") {
    return leg.direction * leg.quantity * (S - leg.entry_price);
  }
  const K = leg.strike;
  if (K == null) return 0;
  const isCall = (leg.option_type || "Call") === "Call";
  const tau = Math.max(0, ((leg.days_to_expiry ?? 0) - days)) / 365;
  const perShare = tau <= 0
    ? (isCall ? Math.max(0, S - K) : Math.max(0, K - S))
    : (isCall ? bsCall(S, K, tau, R_FREE, legIv(leg)) : bsPut(S, K, tau, R_FREE, legIv(leg)));
  // A warrant unit delivers exercise_ratio shares, so its per-unit value is the
  // per-share value times the ratio; an option contract is priced per share.
  const perQuoteUnit = leg.kind === "warrant" ? perShare * (leg.exercise_ratio || 1) : perShare;
  const exitNotional = leg.direction * leg.quantity * mult * perQuoteUnit;
  return exitNotional - entryNotional;
}

function renderPayoff() {
  const p = _payoffPosition;
  if (!p) return;
  const legs = p.legs || [];
  const days = parseInt(document.getElementById("payoff-dte").value, 10) || 0;
  document.getElementById("payoff-dte-label").textContent =
    days === 0 ? "today" : `${days} days from today`;

  const spot = legs.map(l => (quoteMap[qKey(l.kind, l.code)] || {}).underlying_price)
    .find(v => v != null);
  const strikes = legs.map(l => l.strike).filter(k => k != null);
  const center = spot || (strikes.length ? strikes.reduce((a, b) => a + b, 0) / strikes.length : 100);
  const lo = Math.max(0.01, Math.min(center * 0.6, ...strikes.map(k => k * 0.6)));
  const hi = Math.max(center * 1.4, ...strikes.map(k => k * 1.4));

  const xs = [], ys = [];
  const steps = 160;
  for (let i = 0; i <= steps; i++) {
    const S = lo + (hi - lo) * (i / steps);
    xs.push(S);
    ys.push(legs.reduce((sum, leg) => sum + legPnlAt(leg, S, days), 0));
  }

  const marks = payoffStrikeMarks(
    legs.filter(l => l.strike != null)
        .map(l => ({ K: l.strike, label: legLabel(l), dir: l.direction })),
    spot);
  marks.shapes.push({ type: "line", x0: lo, x1: hi, y0: 0, y1: 0,
    line: { color: "rgba(255,255,255,0.25)", width: 1 } });

  Plotly.newPlot("payoff-chart", [{
    x: xs, y: ys, type: "scatter", mode: "lines",
    line: { color: "#4f8ef7", width: 2 }, hovertemplate: "S %{x:.2f}<br>P&L %{y:,.0f}<extra></extra>",
  }], {
    margin: { l: 60, r: 20, t: 10, b: 40 },
    xaxis: { title: "Underlying price", gridcolor: "rgba(255,255,255,0.06)" },
    yaxis: { title: "P&L (NT$)", gridcolor: "rgba(255,255,255,0.06)" },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#c8ccd8", size: 11 },
    shapes: marks.shapes, annotations: marks.annotations,
    showlegend: false,
  }, { displayModeBar: false, responsive: true });

  const m = positionMarks(p);
  document.getElementById("payoff-legs").innerHTML = `
    <table class="dash-table"><thead><tr>
      <th>Leg</th><th class="num">Qty</th><th class="num">Entry</th>
      <th class="num">Mark</th><th class="num">P&amp;L</th>
    </tr></thead><tbody>
    ${legs.map(l => {
      const lm = legMarks(l, p);
      return `<tr>
        <td><span class="${l.direction > 0 ? "dash-long" : "dash-short"}">${l.direction > 0 ? "Long" : "Short"}</span> ${legLabel(l)}</td>
        <td class="num">${l.quantity}</td>
        <td class="num">${_num(l.entry_price)}</td>
        <td class="num">${_num(lm.exit)}</td>
        <td class="num ${(lm.pnl ?? 0) >= 0 ? "dash-up" : "dash-down"}">${lm.pnl == null ? "—" : _money(lm.pnl)}</td>
      </tr>`;
    }).join("")}
    </tbody></table>
    <div class="dash-payoff-total">Position P&amp;L at current marks:
      <b class="${(m.pnl ?? 0) >= 0 ? "dash-up" : "dash-down"}">${m.pnl == null ? "—" : _money(m.pnl)}</b></div>`;
}
