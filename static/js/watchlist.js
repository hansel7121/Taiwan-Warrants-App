// Watchlist stars on the Warrant and Options Scanner rows: starCell() draws the
// toggle, toggleStar() writes it through /add_watchlist and /remove_watchlist
// (services/db_user.py, scoped to the signed-in user), and loadWatchSetOnce()
// fills the starred set at boot so the scanners render existing stars.

let watchSet = new Set();       // "kind:code" for the star toggles in the scanners
let _watchSetLoaded = false;

const qKey = (kind, code) => `${kind}:${code}`;

// PGRST205 = PostgREST cannot find the table: the migration hasn't been applied.
function _watchError(e) {
  const msg = (e && e.message) ? e.message : String(e);
  if (msg.includes("PGRST205") || msg.includes("user_watchlist")) {
    return "Database tables not created yet — run supabase/migrations/020_user_dashboard.sql "
         + "in the Supabase SQL editor, then reload.";
  }
  return msg;
}

// Fill watchSet from the server so scanner rows render starred instead of all-hollow.
async function loadWatchSetOnce() {
  if (_watchSetLoaded) return;
  _watchSetLoaded = true;
  try {
    const w = await apiJson("/list_watchlist");
    watchSet = new Set((Array.isArray(w) ? w : []).map(r => qKey(r.kind, r.code)));
  } catch (e) {
    _watchSetLoaded = false;   // let a later call retry
  }
}

function isStarred(kind, code) {
  return watchSet.has(qKey(kind, code));
}

// Star cell for a scanner table row. Payload is URI-encoded so it survives the
// HTML attribute intact. The expiry DATE is stored alongside the DTE, because a
// DTE snapshot goes stale the moment it is written.
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
    // Roll the optimistic toggle back and say why.
    el.textContent = on ? "★" : "☆";
    el.title = "Watchlist write failed: " + _watchError(e);
    if (on) watchSet.add(key); else watchSet.delete(key);
    alert("Could not update watchlist: " + _watchError(e));
  }
}
