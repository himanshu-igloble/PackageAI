/* Auth + token fetch wrapper.
 *
 * Loaded before app.js so every subsequent fetch automatically inherits the
 * Authorization header when a session token is present in localStorage, and
 * 402 responses (insufficient tokens) are surfaced through a global event
 * that the UI layer (auth-ui.js) listens for.
 *
 * Keeps the legacy "anonymous demo" flow alive: if no session is stored,
 * fetches still go through unauthenticated and the token gate in the
 * backend short-circuits (anonymous = no balance check).
 */
(function () {
  "use strict";

  const TOKEN_KEY = "cpg.session_token";
  const USER_KEY  = "cpg.user";
  const LEGACY_USER_KEY = "designedge.user_id";   // app.js reads this

  // Hydrate the legacy USER_ID slot from the stored auth user, so existing
  // app.js code that does `localStorage.getItem("designedge.user_id")` keeps
  // working without changes.
  try {
    const cached = JSON.parse(localStorage.getItem(USER_KEY) || "null");
    if (cached && cached.email) {
      localStorage.setItem(LEGACY_USER_KEY, cached.email);
    }
  } catch (e) { /* tolerant */ }

  const auth = {
    token()  { return localStorage.getItem(TOKEN_KEY); },
    user()   { try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
               catch (e) { return null; } },
    setSession(token, user) {
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, JSON.stringify(user));
      if (user && user.email) localStorage.setItem(LEGACY_USER_KEY, user.email);
      window.dispatchEvent(new CustomEvent("cpg:auth-changed", { detail: user }));
    },
    updateUser(user) {
      localStorage.setItem(USER_KEY, JSON.stringify(user));
      window.dispatchEvent(new CustomEvent("cpg:auth-changed", { detail: user }));
    },
    clear() {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      window.dispatchEvent(new CustomEvent("cpg:auth-changed", { detail: null }));
    },
  };
  window.cpgAuth = auth;

  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    init = init || {};
    let url = typeof input === "string" ? input : (input && input.url) || "";
    // Only attach auth on same-origin /api/ calls.
    const isApi = url.indexOf("/api/") === 0 || url.indexOf("api/") === 0;
    const token = auth.token();
    if (isApi && token) {
      const headers = new Headers(init.headers || (typeof input !== "string" && input.headers) || {});
      if (!headers.has("Authorization")) {
        headers.set("Authorization", "Bearer " + token);
      }
      init = { ...init, headers };
    }
    // Capture the active case_id from any /cases/{id}/... call so the
    // non-module auth-ui can find it without reaching into app.js's scope.
    if (isApi) {
      const m = url.match(/\/cases\/([a-f0-9]{16,})/);
      if (m) localStorage.setItem("cpg.case_id", m[1]);
    }
    const resp = await origFetch(input, init);
    if (resp.status === 402 && isApi) {
      let detail = null;
      try { detail = (await resp.clone().json()).detail; } catch (e) { /* ignore */ }
      window.dispatchEvent(new CustomEvent("cpg:insufficient-tokens", { detail }));
    } else if (resp.status === 401 && isApi && token) {
      // Stale token — clear it so the user is prompted to re-login.
      auth.clear();
      window.dispatchEvent(new CustomEvent("cpg:auth-required"));
    }
    return resp;
  };
})();
