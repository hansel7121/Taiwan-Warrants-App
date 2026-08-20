// Browser-side mirror of services/roles.py: what the current mode may see, and
// the toggle that switches between them. The server already strips admin-only
// markup/script tags from the page, so can() guards code paths, not DOM. The
// toggle writes a cookie and reloads — the server decides what gets sent.

// Keep in sync with FEATURES in services/roles.py.
const FEATURES = {
  scanner: "user",
  options: "user",
  ivsurface: "user",
  live: "admin",
  dashboard: "user",
  watchlist: "user",
  alerts: "user",
  positions: "user",
  usoptions: "admin",
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
