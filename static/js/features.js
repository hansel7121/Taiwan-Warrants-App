// Browser-side mirror of services/roles.py: what the current mode may see, and
// the corner toggle that switches between them. Loaded before common.js so
// every other script can call can(). The server has already stripped
// admin-only markup and script tags from the page (index.html reads
// show_admin), so can() is for guarding code paths, not for hiding DOM.
//
// The toggle writes a cookie and reloads — the server, not the browser,
// decides what gets sent. That is the same path a real deployment gate will
// use; only the source of the role changes. Not a security boundary today.

// Keep in sync with FEATURES in services/roles.py.
const FEATURES = {
  scanner: "user",
  options: "user",
  ivsurface: "user",
  live: "user",
  arb: "admin",
  portfolio: "admin",
  suggestions: "admin",
  home: "admin",
  clock: "admin",
  products: "admin",
  syncUniverse: "admin",
};

const MODE_COOKIE = "app_mode";

// Mode the server rendered this page in; falls back to the cookie.
function currentMode() {
  if (window.APP_MODE) return window.APP_MODE;
  const m = document.cookie.match(/(?:^|;\s*)app_mode=(admin|user)/);
  return m ? m[1] : "admin";
}

function can(feature) {
  return currentMode() === "admin" || FEATURES[feature] === "user";
}

// Persist the mode and reload so the server re-renders the page for it.
function setMode(mode) {
  document.cookie = MODE_COOKIE + "=" + mode + ";path=/;max-age=31536000;samesite=lax";
  location.reload();
}

function toggleMode() {
  setMode(currentMode() === "admin" ? "user" : "admin");
}
