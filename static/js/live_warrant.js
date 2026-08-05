// Live warrant sub-tab: broker credential entry (issue #42).
//
// Only the credential form for now — the Watchlist editor, connection-status
// panel and the live price rows arrive with the later slices.
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
  if (sub === "live") loadBrokerCredentials();
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
