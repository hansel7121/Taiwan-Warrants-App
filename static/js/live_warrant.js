// Live warrant sub-tab: broker credential entry (issue #42), the shared
// Watchlist editor (issue #44), and the connection-status panel (issue #45).
//
// The live price rows arrive with a later slice.
//
// Nothing here stores or echoes a secret: the password inputs are read once at
// submit, posted straight to /save_broker_credential, and cleared. The saved
// list is metadata only (the backend never returns a credential field), so
// there is nothing secret to render.

// Which inputs belong to which broker. KGI takes no cert (its cert setup is a
// manual one-time CLI step outside the app) and Fubon takes no Capacity Tier
// (fixed 200/5 for every account) — so each broker's form is a strict subset,
// toggled by the .bc-kgi / .bc-fubon classes in index.html.
const BC_FIELDS = {
  kgi:   { person_id: "bcPersonId", person_pwd: "bcPersonPwd" },
  fubon: { person_id: "bcPersonId", password: "bcPassword", cert_pass: "bcCertPass" },
};

function switchScannerSub(sub, btn) {
  document.querySelectorAll(".scsub-content").forEach(el => el.style.display = "none");
  document.querySelectorAll("#tab-scanner .pfsub-btn").forEach(el => el.classList.remove("active"));
  document.getElementById("scsub-" + sub).style.display = "block";
  if (btn) btn.classList.add("active");
  _saveView("ws_scannerSub", sub);
  if (sub === "live") {
    loadBrokerCredentials();
    loadWatchlistCodes();
    loadConnectionStatus();
  }
}

// Show only the selected broker's fields.
function onBrokerChange() {
  const broker = document.getElementById("bcBroker").value;
  document.querySelectorAll(".bc-kgi").forEach(el =>
    el.style.display = broker === "kgi" ? "" : "none");
  document.querySelectorAll(".bc-fubon").forEach(el =>
    el.style.display = broker === "fubon" ? "" : "none");
  _bcStatus("");
}

function _bcStatus(msg, isError) {
  const el = document.getElementById("bcStatus");
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "var(--muted)";
}

function _bcVal(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : "";
}

async function saveBrokerCredential() {
  const broker = document.getElementById("bcBroker").value;
  const form = new FormData();
  form.append("broker", broker);

  for (const [field, inputId] of Object.entries(BC_FIELDS[broker])) {
    const val = _bcVal(inputId);
    if (!val) { _bcStatus(`${field.replace(/_/g, " ")} is required`, true); return; }
    form.append(field, val);
  }

  if (broker === "fubon") {
    const file = document.getElementById("bcCert").files[0];
    // Optional: a re-save of the text fields must not force re-uploading a
    // cert that is already stored.
    if (file) {
      if (!file.name.toLowerCase().endsWith(".pfx")) {
        _bcStatus("Certificate must be a .pfx file", true); return;
      }
      form.append("cert", file);
    }
  } else {
    // Blank tier inputs are simply not sent, so the server applies its default.
    const symbols = _bcVal("bcSymbols"), connections = _bcVal("bcConnections");
    if (symbols) form.append("symbols_per_connection", symbols);
    if (connections) form.append("connections", connections);
  }

  const btn = document.getElementById("bcSaveBtn");
  btn.disabled = true;
  _bcStatus("Saving…");
  try {
    const res = await api("/save_broker_credential", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) { _bcStatus(data.error || "Save failed", true); return; }
    _bcStatus(`Saved ${broker.toUpperCase()} credential.`);
    _bcClearInputs();
    await loadBrokerCredentials();
  } catch (e) {
    _bcStatus("Save failed: " + (e && e.message ? e.message : e), true);
  } finally {
    btn.disabled = false;
  }
}

// Clear every secret input as soon as the post completes, so a password is not
// left sitting in the DOM for the rest of the session.
function _bcClearInputs() {
  ["bcPersonId", "bcPersonPwd", "bcPassword", "bcCertPass",
   "bcSymbols", "bcConnections", "bcCert"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
}

async function loadBrokerCredentials() {
  const container = document.getElementById("bcSavedContainer");
  if (!container) return;
  let rows = [];
  try {
    const res = await api("/list_broker_credentials");
    rows = await res.json();
  } catch (e) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">Could not load saved credentials.</div>';
    return;
  }
  if (!rows || !rows.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">No broker credential saved yet.</div>';
    return;
  }
  let html = '<table style="border-collapse:collapse;font-size:13px">'
    + '<tr><th style="text-align:left;padding:4px 12px 4px 0">Broker</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Capacity tier</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Cert</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Updated</th>'
    + '<th></th></tr>';
  rows.forEach(r => {
    const tier = `${r.symbols_per_connection} × ${r.connections}`;
    const cert = r.has_cert ? "uploaded" : "—";
    const updated = r.updated_at ? new Date(r.updated_at).toLocaleString() : "—";
    html += `<tr><td style="padding:4px 12px 4px 0">${r.broker.toUpperCase()}</td>`
      + `<td style="padding:4px 12px 4px 0">${tier}</td>`
      + `<td style="padding:4px 12px 4px 0">${cert}</td>`
      + `<td style="padding:4px 12px 4px 0">${updated}</td>`
      + `<td><button class="sm" onclick="removeBrokerCredential('${r.broker}')">Remove</button></td></tr>`;
  });
  container.innerHTML = html + "</table>";
}

async function removeBrokerCredential(broker) {
  if (!confirm(`Remove your ${broker.toUpperCase()} credential?`)) return;
  try {
    const res = await api("/remove_broker_credential", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ broker }),
    });
    const data = await res.json();
    if (!res.ok) { _bcStatus(data.error || "Remove failed", true); return; }
    _bcStatus(`Removed ${broker.toUpperCase()} credential.`);
    await loadBrokerCredentials();
  } catch (e) {
    _bcStatus("Remove failed: " + (e && e.message ? e.message : e), true);
  }
}

// ── Shared Watchlist ─────────────────────────────────────────────────────────
//
// One list for the whole app, not per-user: every watched code occupies a slot
// in the shared connection pool, so an add can be rejected because someone
// else filled the pool. The backend words that rejection (it knows the live
// N/M counts), and we surface its message verbatim.

function _wlStatus(msg, isError) {
  const el = document.getElementById("wlStatus");
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "var(--muted)";
}

// "031100" -> ["031100"]; "031100, 031101" -> ["031100", "031101"]. Blank
// pieces from a trailing comma or a double comma are dropped.
function _wlParseCodes(raw) {
  return raw.split(",").map(c => c.trim()).filter(c => c);
}

async function addWatchlistCodes() {
  const input = document.getElementById("wlCodes");
  const codes = _wlParseCodes(input ? input.value : "");
  if (!codes.length) { _wlStatus("Enter a warrant code first.", true); return; }

  const btn = document.getElementById("wlAddBtn");
  btn.disabled = true;
  _wlStatus("Adding…");
  try {
    const res = await api("/watchlist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes }),
    });
    const data = await res.json();
    // A full pool comes back as a 400 whose message already names the exact
    // N/M counts — show it as-is rather than paraphrasing.
    if (!res.ok || !data.ok) { _wlStatus(data.error || "Add failed", true); return; }
    input.value = "";
    const added = data.codes || [];
    _wlStatus(added.length
      ? `Added ${added.join(", ")}.`
      : "Already on the watchlist.");
    await loadWatchlistCodes();
  } catch (e) {
    _wlStatus("Add failed: " + (e && e.message ? e.message : e), true);
  } finally {
    btn.disabled = false;
  }
}

async function removeWatchlistCode(code) {
  try {
    const res = await api("/watchlist/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes: [code] }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) { _wlStatus(data.error || "Remove failed", true); return; }
    // Refetch rather than just dropping the row: the freed capacity has to be
    // real before the next add, and the list is shared so it may have moved.
    _wlStatus(`Removed ${code}.`);
    await loadWatchlistCodes();
  } catch (e) {
    _wlStatus("Remove failed: " + (e && e.message ? e.message : e), true);
  }
}

async function loadWatchlistCodes() {
  const container = document.getElementById("wlCodesContainer");
  if (!container) return;
  let codes = [];
  try {
    const res = await api("/watchlist/list");
    const data = await res.json();
    codes = (data && data.codes) || [];
  } catch (e) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">Could not load the watchlist.</div>';
    return;
  }
  if (!codes.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">No code watched yet.</div>';
    return;
  }
  let html = '<table style="border-collapse:collapse;font-size:13px">'
    + '<tr><th style="text-align:left;padding:4px 12px 4px 0">Code</th><th></th></tr>';
  codes.forEach(code => {
    const safe = String(code).replace(/[^0-9A-Za-z]/g, "");
    html += `<tr><td style="padding:4px 12px 4px 0">${safe}</td>`
      + `<td><button class="sm" onclick="removeWatchlistCode('${safe}')">Remove</button></td></tr>`;
  });
  container.innerHTML = html + "</table>";
}

// ── Connection status ────────────────────────────────────────────────────────
//
// One row per Broker Account in the shared pool — every user's, not just the
// caller's, because any account may be carrying a code you watch. Rows are
// labelled "(you)" rather than by owner: the backend has no user_id -> email
// mapping to name anyone with.
//
// Fetched on tab activation like everything else here; the worker writes its
// status on every transition, so reopening the tab is the refresh.

const CS_COLOURS = {
  connected:    "var(--success)",
  reconnecting: "var(--accent)",
  disconnected: "var(--danger)",
  stopped:      "var(--muted)",
};

async function loadConnectionStatus() {
  const container = document.getElementById("csContainer");
  if (!container) return;
  let rows = [];
  try {
    const res = await api("/broker/status");
    rows = await res.json();
  } catch (e) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">Could not load connection status.</div>';
    return;
  }
  if (!rows || !rows.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--muted)">No broker account to connect yet.</div>';
    return;
  }
  let html = '<table style="border-collapse:collapse;font-size:13px">'
    + '<tr><th style="text-align:left;padding:4px 12px 4px 0">Account</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Status</th>'
    + '<th style="text-align:left;padding:4px 12px 4px 0">Subscribed</th></tr>';
  rows.forEach(r => {
    const account = r.broker.toUpperCase() + (r.is_you ? " (you)" : "");
    const status = r.status || "not yet reported";
    const colour = CS_COLOURS[r.status] || "var(--muted)";
    // No status row at all means no worker has ever acted on this account, so
    // printing its share of the watchlist would claim a subscription that has
    // never existed.
    const subscribed = r.status ? `${r.subscribed}/${r.capacity}` : `—/${r.capacity}`;
    html += `<tr><td style="padding:4px 12px 4px 0">${account}</td>`
      + `<td style="padding:4px 12px 4px 0;color:${colour}">${status}</td>`
      + `<td style="padding:4px 12px 4px 0">${subscribed}</td></tr>`;
  });
  container.innerHTML = html + "</table>";
}

// Re-apply the Screener/Live warrant choice saved before the last reload, the
// same way common.js restores the top-level tab. The HTML renders Screener
// active, so this only acts when Live warrant was the last view.
function restoreScannerSub() {
  if (_readView("ws_scannerSub") !== "live") return;
  const btn = document.getElementById("scsub-btn-live");
  if (btn) switchScannerSub("live", btn);
}

document.addEventListener("DOMContentLoaded", () => {
  onBrokerChange();
  restoreScannerSub();
});
