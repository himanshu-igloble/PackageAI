/* Auth + token UI mount.
 *
 * Adds (without disturbing app.js):
 *   - A token-balance chip + login/logout button in the topbar.
 *   - A login modal (default credentials shown in placeholder).
 *   - A buy-credits modal triggered by 402 responses.
 *   - An admin "Users" pane reachable from the topbar, which lists every
 *     user and lets the admin allocate tokens or create new users.
 *   - A PCR substitution card on the report stage that pulls
 *     /api/cases/{id}/pcr-substitution and renders the deltas.
 *
 * All UI is built imperatively so it can sit alongside the existing module
 * code without an import.
 */
(function () {
  "use strict";

  const API = "/api";
  const auth = window.cpgAuth;
  if (!auth) {
    console.warn("[auth-ui] cpgAuth missing — auth.js failed to load");
    return;
  }

  // -------------------------------------------------------- styles ----------
  const STYLE = `
    .cpg-chip { display:inline-flex; align-items:center; gap:6px;
      padding:4px 10px; border-radius:999px; background:rgba(255,255,255,0.08);
      color:#e6f1ff; font:500 12px/1.4 system-ui,sans-serif; margin-right:8px;
      border:1px solid rgba(255,255,255,0.14); cursor:default; }
    .cpg-chip--low { background:#7a2b1f; border-color:#a44; }
    .cpg-chip__dot { width:6px; height:6px; border-radius:50%; background:#3ad29f; }
    .cpg-chip--low .cpg-chip__dot { background:#ffb74d; }
    .cpg-btn { padding:4px 10px; border-radius:6px; border:1px solid rgba(255,255,255,0.18);
      background:rgba(255,255,255,0.06); color:#e6f1ff; font:500 12px/1.4 system-ui,sans-serif;
      cursor:pointer; margin-right:6px; }
    .cpg-btn:hover { background:rgba(255,255,255,0.12); }
    .cpg-btn--primary { background:#2563eb; border-color:#1d4ed8; }
    .cpg-btn--primary:hover { background:#1d4ed8; }

    .cpg-modal-back { position:fixed; inset:0; background:rgba(8,15,26,0.72);
      display:flex; align-items:center; justify-content:center; z-index:9999; }
    .cpg-modal { background:#0d1726; color:#e6f1ff; border:1px solid #1e2c44;
      border-radius:12px; min-width:360px; max-width:560px; width:90%; padding:22px 24px;
      font:14px/1.5 system-ui,sans-serif; box-shadow:0 24px 60px rgba(0,0,0,0.6); }
    .cpg-modal h2 { margin:0 0 6px; font-size:18px; }
    .cpg-modal p  { margin:0 0 14px; color:#9fb1cc; font-size:13px; }
    .cpg-modal label { display:block; font-size:12px; color:#9fb1cc; margin:10px 0 4px; }
    .cpg-modal input, .cpg-modal select { width:100%; padding:8px 10px; border-radius:6px;
      border:1px solid #2a3a5a; background:#0a1322; color:#e6f1ff; font-size:13px;
      box-sizing:border-box; }
    .cpg-modal .row { display:flex; gap:10px; }
    .cpg-modal .row > * { flex:1; }
    .cpg-modal .actions { display:flex; gap:8px; justify-content:flex-end; margin-top:18px; }
    .cpg-modal .err { color:#ff8a8a; font-size:12px; min-height:16px; margin-top:8px; }
    .cpg-modal .ok  { color:#7ee2b8; font-size:12px; min-height:16px; margin-top:8px; }

    .cpg-pack { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
    .cpg-pack button { flex:1 1 130px; text-align:left; background:#0a1322;
      border:1px solid #1f2c46; padding:12px; border-radius:8px; color:#e6f1ff; cursor:pointer; }
    .cpg-pack button:hover { border-color:#2563eb; }
    .cpg-pack b { display:block; font-size:14px; margin-bottom:2px; }
    .cpg-pack span { color:#9fb1cc; font-size:12px; }

    .pcr-card { margin-top:16px; }
    .pcr-card .pcr-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px,1fr));
      gap:14px; margin-top:10px; }
    .pcr-card .pcr-cell { background:rgba(58,210,159,0.06); border:1px solid rgba(58,210,159,0.18);
      border-radius:8px; padding:12px; }
    .pcr-card .pcr-cell h4 { margin:0 0 4px; font-size:12px; color:#9fb1cc;
      text-transform:uppercase; letter-spacing:0.04em; }
    .pcr-card .pcr-cell .v  { font-size:18px; font-weight:600; }
    .pcr-card .pcr-cell .d  { color:#3ad29f; font-size:12px; margin-top:4px; }
    .pcr-card .pcr-cell .d--bad { color:#ffb74d; }
    .pcr-card__head h2 { margin:0 0 4px; }
    .pcr-card__actions { margin-top:14px; display:flex; gap:8px; }
    .pcr-formula { font-size:11px; color:#7e90ac; margin-top:10px; font-family:ui-monospace,monospace; }
    .pcr-caveat  { background:#221a08; border:1px solid #4a3712; color:#ffd58a;
      padding:8px 10px; border-radius:6px; font-size:12px; margin-top:8px; }

    .cpg-users { width:100%; border-collapse:collapse; margin-top:12px; font-size:13px; }
    .cpg-users th, .cpg-users td { padding:6px 8px; border-bottom:1px solid #1f2c46; text-align:left; }
    .cpg-users th { color:#9fb1cc; font-weight:500; font-size:11px; text-transform:uppercase; }
    .cpg-users input { width:80px; padding:3px 6px; }

    /* ---- Optimise heatmap strip suppression -------------------------
       Keep only the optimise comparison strip hidden here. The Results
       stage owns its own 3-up viewer layout and should stay visible. */
    [data-page="variant"] .viewer-strip,
    [data-page="optimise"] .opt-heatmap-strip { display: none !important; }

    /* ---- PCR alternative badge -------------------------------------- */
    .pcr-badge { display:inline-block; margin-left:8px; padding:1px 7px;
      background:#0f6b3a; color:#d6f6e3; border:1px solid #1f9e58; border-radius:999px;
      font-size:10px; font-weight:700; letter-spacing:0.04em; vertical-align:middle; }
    .opt-row--pcr { background:rgba(31,158,88,0.04); }

    /* ---- Topbar role chip ------------------------------------------- */
    .cpg-role { display:inline-flex; align-items:center; gap:6px;
      padding:2px 8px; border-radius:999px; margin-right:6px;
      background:rgba(80,140,255,0.12); color:#a9c4ff;
      font:600 11px/1.4 system-ui,sans-serif; text-transform:uppercase;
      letter-spacing:0.04em; }
    .cpg-role--admin { background:rgba(255,170,80,0.16); color:#ffce9a;
      border:1px solid rgba(255,170,80,0.3); }
    .cpg-org { color:#9fb1cc; font:500 12px/1.4 system-ui,sans-serif;
      margin-right:10px; opacity:0.85; }

    /* ---- PCR Intelligence card (optimise page) --------------------- */
    .pcr-intel { margin:0 0 18px; padding:14px 18px;
      background: linear-gradient(180deg, rgba(31,158,88,0.10), rgba(31,158,88,0.04));
      border:1px solid rgba(31,158,88,0.35); border-radius:12px; }
    .pcr-intel__head { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
    .pcr-intel__badge { display:inline-block; padding:2px 10px; border-radius:999px;
      background:#0f6b3a; color:#d6f6e3; border:1px solid #1f9e58;
      font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; }
    .pcr-intel h3 { margin:0; font-size:15px; font-weight:600; }
    .pcr-intel p  { margin:6px 0 0; color:#a9c4ff; font-size:13px; line-height:1.5; }
    .pcr-intel__metrics { display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; }
    .pcr-intel__metrics .m { background:rgba(31,158,88,0.10);
      border:1px solid rgba(31,158,88,0.35); border-radius:8px; padding:8px 12px; }
    .pcr-intel__metrics .m .k { display:block; font-size:10px; color:#8ad5b0;
      letter-spacing:0.06em; text-transform:uppercase; }
    .pcr-intel__metrics .m .v { font-size:16px; font-weight:600; color:#e6f1ff; }

    /* PCR row accent on the comparison ledger */
    .opt-row--pcr td:first-child { border-left:3px solid #1f9e58; }

    /* Topbar grouping — visual separator between the org/role chips and
       the token/buy/users/sign-in chips, without changing fonts. */
    #cpg-auth-bar { padding-left:10px; border-left:1px solid rgba(255,255,255,0.08); }
    #cpg-auth-bar .cpg-role + .cpg-chip { margin-left:2px; }

    /* ---- Actual-ISTA learning panel (results page) ------------------ */
    .accuracy-panel { margin-top:14px; padding:14px; border:1px solid #1f2c46;
      border-radius:10px; background:#0c1525; color:#e6f1ff;
      font:13px/1.5 system-ui,sans-serif; }
    .accuracy-panel h3 { margin:0 0 4px; font-size:15px; }
    .accuracy-panel p  { margin:0 0 10px; color:#9fb1cc; font-size:12px; }
    .accuracy-panel label { display:block; font-size:11px; color:#9fb1cc; margin:8px 0 3px; }
    .accuracy-panel select, .accuracy-panel input, .accuracy-panel textarea {
      width:100%; padding:6px 8px; border-radius:6px; border:1px solid #2a3a5a;
      background:#0a1322; color:#e6f1ff; font-size:13px; box-sizing:border-box; }
    .accuracy-panel .row { display:flex; gap:10px; }
    .accuracy-panel .row > * { flex:1; }
    .accuracy-panel .narrative { background:#0a1322; border-left:3px solid #1f9e58;
      padding:8px 10px; margin-top:10px; font-size:12px; color:#cfe1ff; border-radius:4px; }
    .accuracy-panel .cal-pill { display:inline-block; padding:2px 8px; border-radius:999px;
      background:rgba(80,140,255,0.14); color:#a9c4ff; font-size:11px; margin-left:6px; }
  `;
  const styleEl = document.createElement("style");
  styleEl.textContent = STYLE;
  document.head.appendChild(styleEl);

  // -------------------------------------------------------- helpers ---------
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === "style") Object.assign(e.style, attrs[k]);
      else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else if (k === "html") e.innerHTML = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    for (const c of children) {
      if (c == null) continue;
      e.appendChild(c.nodeType ? c : document.createTextNode(c));
    }
    return e;
  }

  function modal(buildBody, opts) {
    opts = opts || {};
    const back = el("div", { class: "cpg-modal-back" });
    const m = el("div", { class: "cpg-modal" });
    back.appendChild(m);
    back.addEventListener("click", (ev) => { if (ev.target === back && opts.dismissable !== false) close(); });
    function close() { back.remove(); }
    buildBody(m, close);
    document.body.appendChild(back);
    return close;
  }

  async function api(path, opts) {
    const r = await fetch(API + path, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { /* tolerant */ }
    if (!r.ok) {
      const err = new Error((body && (body.detail || body.message)) || (r.status + " " + r.statusText));
      err.status = r.status; err.body = body;
      throw err;
    }
    return body;
  }

  // -------------------------------------------------------- topbar chip ----
  function mountTopbar() {
    const topbar = document.querySelector("header.topbar");
    if (!topbar) return;
    if (document.getElementById("cpg-auth-bar")) return;

    const bar = el("div", { id: "cpg-auth-bar", style: { display: "flex", alignItems: "center", marginRight: "8px" } });

    // Organisation chip removed from the navbar per spec — too cluttered.
    // We keep a hidden sentinel so refreshChip() doesn't have to null-check.
    const org = el("span", { class: "cpg-org", id: "cpg-org-label",
                             "aria-hidden": "true", style: { display: "none" } }, "");
    bar.appendChild(org);

    // Role chip — only "admin" surfaces on the navbar; regular users get
    // nothing here, since "ROLE: user" reads as noise.
    const role = el("span", { class: "cpg-role", id: "cpg-role-chip" }, "");
    role.style.display = "none";
    bar.appendChild(role);

    // Token chip + Buy button both moved to the dashboard. We retain
    // invisible sentinels so other code paths don't need null-checks.
    const chip = el("span", {
      class: "cpg-chip", id: "cpg-token-chip", "aria-hidden": "true",
      style: { display: "none" },
    }, el("span", { class: "cpg-chip__dot" }),
       el("span", { id: "cpg-token-chip-label" }, "—"));
    bar.appendChild(chip);

    const buyBtn = el("button", { class: "cpg-btn", id: "cpg-buy-btn",
                                  title: "Purchase more tokens",
                                  onClick: showBuyModal,
                                  style: { display: "none" } }, "Buy");
    bar.appendChild(buyBtn);

    // Users (admin panel) button is hidden from the navbar in this build —
    // admin operations move into a dedicated /admin route in a future pass.
    // We keep a hidden sentinel so refreshChip() doesn't need null-checks.
    const adminBtn = el("button", { class: "cpg-btn", id: "cpg-admin-btn",
                                    title: "Admin · users",
                                    onClick: showAdminModal,
                                    style: { display: "none" },
                                    "aria-hidden": "true" }, "Users");
    bar.appendChild(adminBtn);

    const authBtn = el("button", { class: "cpg-btn cpg-btn--primary", id: "cpg-auth-btn",
                                   onClick: onAuthBtn }, "Sign in");
    bar.appendChild(authBtn);

    // Insert before the existing user-chip (or at the end).
    const userChip = topbar.querySelector(".user-chip");
    topbar.insertBefore(bar, userChip || null);
    refreshChip();
  }

  function refreshChip() {
    const user = auth.user();
    const label = document.getElementById("cpg-token-chip-label");
    const chip = document.getElementById("cpg-token-chip");
    const authBtn = document.getElementById("cpg-auth-btn");
    const adminBtn = document.getElementById("cpg-admin-btn");
    const buyBtn = document.getElementById("cpg-buy-btn");
    const org = document.getElementById("cpg-org-label");
    const role = document.getElementById("cpg-role-chip");
    if (!label) return;
    if (user) {
      label.textContent = (user.token_balance ?? 0) + " tokens";
      chip.classList.toggle("cpg-chip--low", (user.token_balance ?? 0) < 3);
      chip.title = `Signed in as ${user.email} · 1 token per simulation`;
      authBtn.textContent = "Sign out";
      // Users (admin panel) button stays hidden in the topbar across all roles
      // — admin functions are accessed elsewhere. Buy button is also hidden;
      // the dashboard Tokens tile is the canonical buy-credits CTA.
      adminBtn.style.display = "none";
      buyBtn.style.display = "none";
      // Avatar: take the user's first initial (M for Mayank). The full
      // email is intentionally NOT rendered in the topbar.
      const avatar = document.getElementById("user-avatar");
      if (avatar) {
        const seed = (user.name || user.email || "?").trim();
        avatar.textContent = (seed.charAt(0) || "?").toUpperCase();
        avatar.title = user.email || "";
      }
      const userNameEl = document.getElementById("user-name");
      if (userNameEl) userNameEl.style.display = "none";
      // Organisation label — extract everything after @ in the email, or
      // use the user name if it's clearly a brand. Drops trailing TLDs so
      // "shipping@bytedge.ai" → "BYTEDGE".
      if (org) {
        let domain = (user.email || "").split("@")[1] || "";
        domain = domain.replace(/\.(com|ai|io|net|org|co|in|uk|de|fr)$/i, "");
        domain = domain.split(".").pop() || domain;
        org.textContent = domain.toUpperCase();
        org.style.display = domain ? "" : "none";
      }
      if (role) {
        role.textContent = user.role || "user";
        role.classList.toggle("cpg-role--admin", user.role === "admin");
        role.style.display = "";
      }
    } else {
      label.textContent = "Anonymous";
      chip.classList.remove("cpg-chip--low");
      chip.title = "Sign in to track token usage";
      authBtn.textContent = "Sign in";
      adminBtn.style.display = "none";
      buyBtn.style.display = "none";
      if (org)  org.style.display = "none";
      if (role) role.style.display = "none";
    }
  }

  function onAuthBtn() {
    const user = auth.user();
    if (user) {
      // Sign out
      fetch(API + "/auth/logout", { method: "POST" }).catch(() => {});
      auth.clear();
      refreshChip();
    } else {
      showLoginModal();
    }
  }

  // -------------------------------------------------------- login ----------
  function showLoginModal() {
    modal((m, close) => {
      m.appendChild(el("h2", null, "Sign in"));
      m.appendChild(el("p", null, "Simulation runs consume one token each. Admins can allocate tokens to other users."));
      const email = el("input", { type: "email", placeholder: "you@example.com", autocomplete: "username" });
      const pwd   = el("input", { type: "password", placeholder: "Password", autocomplete: "current-password" });
      const err   = el("div", { class: "err" });

      m.appendChild(el("label", null, "Email"));
      m.appendChild(email);
      m.appendChild(el("label", null, "Password"));
      m.appendChild(pwd);
      m.appendChild(err);

      const submit = el("button", { class: "cpg-btn cpg-btn--primary", onClick: async () => {
        err.textContent = "";
        try {
          const r = await api("/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email: email.value, password: pwd.value }),
          });
          auth.setSession(r.token, r.user);
          refreshChip();
          close();
        } catch (e) { err.textContent = e.message || "login failed"; }
      } }, "Sign in");
      const cancel = el("button", { class: "cpg-btn", onClick: close }, "Continue anonymously");
      m.appendChild(el("div", { class: "actions" }, cancel, submit));

      setTimeout(() => email.focus(), 50);
    });
  }

  // -------------------------------------------------------- buy credits ----
  let _packs = null;
  async function showBuyModal(prefill) {
    if (!auth.user()) { showLoginModal(); return; }
    if (!_packs) {
      try { _packs = (await api("/auth/pricing")).packs; }
      catch (e) { _packs = {}; }
    }
    modal((m, close) => {
      m.appendChild(el("h2", null, "Buy simulation tokens"));
      const sub = prefill && prefill.message
        ? prefill.message
        : "Pick a pack. In production this triggers a PayU / Stripe checkout; in this build the tokens are credited immediately for demo purposes.";
      m.appendChild(el("p", null, sub));
      const grid = el("div", { class: "cpg-pack" });
      Object.entries(_packs).forEach(([key, pack]) => {
        grid.appendChild(el("button", { onClick: async () => {
          try {
            const u = await api("/billing/checkout", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ pack: key }),
            });
            auth.updateUser(u); refreshChip(); close();
          } catch (e) { alert(e.message); }
        } },
          el("b", null, pack.label || key),
          el("span", null, "$" + pack.price_usd + " · " + pack.tokens + " runs"),
        ));
      });
      m.appendChild(grid);
      m.appendChild(el("div", { class: "actions" },
        el("button", { class: "cpg-btn", onClick: close }, "Close"),
      ));
    });
  }

  // -------------------------------------------------------- admin: users ---
  async function showAdminModal() {
    let users = [];
    try { users = await api("/auth/users"); }
    catch (e) { alert(e.message); return; }
    modal((m, close) => {
      m.appendChild(el("h2", null, "Users"));
      m.appendChild(el("p", null, "Allocate tokens or create new users. Each token grants one simulation run."));

      const tbody = el("tbody");
      const renderRow = (u) => {
        const inp = el("input", { type: "number", value: "10", min: "1" });
        const row = el("tr", null,
          el("td", null, u.email),
          el("td", null, u.role),
          el("td", null, String(u.token_balance)),
          el("td", null, inp,
            el("button", { class: "cpg-btn", style: { marginLeft: "6px" }, onClick: async () => {
              try {
                const updated = await api("/auth/users/" + u.user_id + "/tokens", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ delta: parseInt(inp.value, 10) || 0, notes: "topbar grant" }),
                });
                const replacement = renderRow(updated);
                tbody.replaceChild(replacement, row);
              } catch (e) { alert(e.message); }
            } }, "+ tokens"),
          ),
        );
        return row;
      };
      users.forEach((u) => tbody.appendChild(renderRow(u)));

      m.appendChild(el("table", { class: "cpg-users" },
        el("thead", null, el("tr", null,
          el("th", null, "Email"), el("th", null, "Role"),
          el("th", null, "Balance"), el("th", null, "Allocate"),
        )),
        tbody,
      ));

      // --- Create new user form ---
      m.appendChild(el("h2", { style: { marginTop: "18px" } }, "Create user"));
      const ne = el("input", { type: "email", placeholder: "email" });
      const np = el("input", { type: "password", placeholder: "initial password" });
      const nn = el("input", { type: "text", placeholder: "name" });
      const nt = el("input", { type: "number", value: "10", min: "0" });
      const nr = el("select", null,
        el("option", { value: "user" }, "user"),
        el("option", { value: "admin" }, "admin"),
      );
      const cerr = el("div", { class: "err" });
      m.appendChild(el("div", { class: "row" },
        el("div", null, el("label", null, "Email"), ne),
        el("div", null, el("label", null, "Name"), nn),
      ));
      m.appendChild(el("div", { class: "row" },
        el("div", null, el("label", null, "Password"), np),
        el("div", null, el("label", null, "Tokens"), nt),
        el("div", null, el("label", null, "Role"), nr),
      ));
      m.appendChild(cerr);

      m.appendChild(el("div", { class: "actions" },
        el("button", { class: "cpg-btn", onClick: close }, "Close"),
        el("button", { class: "cpg-btn cpg-btn--primary", onClick: async () => {
          cerr.textContent = "";
          try {
            const u = await api("/auth/users", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                email: ne.value, password: np.value, name: nn.value,
                role: nr.value, initial_tokens: parseInt(nt.value, 10) || 0,
              }),
            });
            tbody.appendChild(renderRow(u));
            ne.value = np.value = nn.value = ""; nt.value = "10";
          } catch (e) { cerr.textContent = e.message; }
        } }, "Create"),
      ));
    });
  }

  // -------------------------------------------------------- PCR card -------
  async function refreshPCRCard() {
    const card = document.getElementById("pcr-card");
    if (!card) return;
    // Find the active case_id from the global, falling back to localStorage.
    let caseId = window.CURRENT_CASE_ID || localStorage.getItem("cpg.case_id") || localStorage.getItem("designedge.case_id");
    if (!caseId) { card.hidden = true; return; }
    try {
      const r = await api("/cases/" + caseId + "/pcr-substitution");
      renderPCR(r);
      card.hidden = false;
    } catch (e) {
      // 404 = no PCR analogue available; that's expected for many materials.
      card.hidden = true;
    }
  }

  function fmtPct(v) {
    if (v == null) return "—";
    const s = (v > 0 ? "+" : "") + v.toFixed(1) + "%";
    return s;
  }

  function renderPCR(r) {
    const body = document.getElementById("pcr-card-body");
    if (!body) return;
    body.innerHTML = "";

    const title = document.getElementById("pcr-card-title");
    if (title) title.textContent = `${r.baseline_material} → ${r.candidate_material}`;
    const sub = document.getElementById("pcr-card-sub");
    if (sub) {
      const componentClause = r.pcr_component ? `Comparing: ${r.pcr_component}. ` : "";
      const volumeClause = (r.caveats || []).some(c => c.includes("volume not available"))
        ? "Mass figures shown are per-kg references (no geometry volume available)."
        : "All figures are calculated from component volume × density × carbon intensity.";
      sub.textContent = (
        componentClause +
        `Candidate carries ${r.candidate_recycled_content_pct.toFixed(0)}% post-consumer recycled content. ` +
        volumeClause
      );
    }

    const grid = el("div", { class: "pcr-grid" });
    function cell(title, value, delta, deltaLabel, badIfPositive) {
      const c = el("div", { class: "pcr-cell" });
      c.appendChild(el("h4", null, title));
      c.appendChild(el("div", { class: "v" }, value));
      if (delta != null) {
        const isBad = badIfPositive ? delta > 0 : delta < -50;
        c.appendChild(el("div", { class: "d" + (isBad ? " d--bad" : "") }, deltaLabel));
      }
      return c;
    }
    grid.appendChild(cell("Part mass (baseline)",
      `${r.baseline_part_mass_g} g`, null, null));
    grid.appendChild(cell("Part mass (PCR)",
      `${r.candidate_part_mass_g} g`, r.mass_delta_pct,
      `${fmtPct(r.mass_delta_pct)} mass`));
    if (r.baseline_part_carbon_kg_co2e != null) {
      grid.appendChild(cell("Carbon (baseline)",
        `${r.baseline_part_carbon_kg_co2e.toFixed(4)} kg CO₂e`));
    }
    if (r.candidate_part_carbon_kg_co2e != null) {
      grid.appendChild(cell("Carbon (PCR)",
        `${r.candidate_part_carbon_kg_co2e.toFixed(4)} kg CO₂e`,
        r.carbon_delta_pct, `${fmtPct(r.carbon_delta_pct)} carbon`));
    }
    if (r.annual_carbon_savings_kg_co2e != null) {
      grid.appendChild(cell(`Annual savings @ ${r.annual_units.toLocaleString()} units`,
        `${r.annual_carbon_savings_kg_co2e.toLocaleString()} kg CO₂e`));
    }
    body.appendChild(grid);

    if (Object.keys(r.mechanical_delta || {}).length) {
      const m = el("div", { style: { marginTop: "10px", color: "#9fb1cc", fontSize: "12px" } });
      const parts = Object.entries(r.mechanical_delta).map(([k, v]) =>
        `${k.replace("_pct", "")}: ${fmtPct(v)}`);
      m.textContent = "Mechanical impact — " + parts.join(", ");
      body.appendChild(m);
    }
    body.appendChild(el("div", { class: "pcr-formula" }, r.formula));
    (r.caveats || []).forEach((c) => body.appendChild(el("div", { class: "pcr-caveat" }, c)));
  }

  function wirePCRButtons() {
    const refresh = document.getElementById("pcr-refresh");
    if (refresh) refresh.addEventListener("click", refreshPCRCard);
    const apply = document.getElementById("pcr-apply");
    if (apply) apply.addEventListener("click", async () => {
      const caseId = window.CURRENT_CASE_ID || localStorage.getItem("cpg.case_id") || localStorage.getItem("designedge.case_id");
      if (!caseId) return;
      try {
        const r = await api("/cases/" + caseId + "/pcr-substitution");
        const ok = confirm(
          `Re-run the full analysis with "${r.candidate_material}" (${r.candidate_recycled_content_pct.toFixed(0)}% PCR) ` +
          `replacing "${r.baseline_material}"?\n\nThis consumes 1 simulation token.`
        );
        if (!ok) return;
        // Delegate to app.js's approvePlan() which handles the loading overlay,
        // SSE narration, lastSnapshot update, heatmap reload, and navigation.
        // The material edit is baked into the approve payload so the backend
        // uses the PCR material for the entire analysis.
        if (typeof window.PACKTWIN_PCR_APPLY === "function") {
          window.PACKTWIN_PCR_APPLY(r.candidate_material);
        } else {
          // Fallback: should not normally be reached — app.js may not have loaded.
          alert("PCR re-run is not available yet. Please refresh the page and try again.");
        }
      } catch (e) {
        if (e.status === 402) {
          showBuyModal(e.body && e.body.detail);
        } else {
          alert(e.message || "Could not fetch PCR substitution data.");
        }
      }
    });
  }

  // -------------------------------------------------------- PCR intel ------
  // Refreshes the "PCR Intelligence" metric strip on the optimise stage. The
  // optimiser is run by app.js and stores its result on window.lastOptResult
  // (see app.js line near `lastOptResult = `). We poll a few times after
  // navigating to #/optimise to catch the result without coupling to app.js.
  function refreshPCRIntel() {
    const metrics = document.getElementById("pcr-intel-metrics");
    if (!metrics) return;
    const result = window.lastOptResult;
    if (!result || !Array.isArray(result.comparison_rows)) {
      metrics.hidden = true; return;
    }
    const pcrRow = result.comparison_rows.find(r => r.is_pcr);
    const baseline = result.comparison_rows.find(r => r.name === "Original");
    if (!pcrRow) { metrics.hidden = true; return; }
    metrics.innerHTML = "";
    const m = (k, v) => {
      const c = el("div", { class: "m" });
      c.appendChild(el("span", { class: "k" }, k));
      c.appendChild(el("span", { class: "v" }, v));
      return c;
    };
    metrics.appendChild(m("PCR material",
      pcrRow.material + " · " + (pcrRow.recycled_content_pct || 100).toFixed(0) + "% recycled"));
    if (baseline && pcrRow.mass_g != null && baseline.mass_g) {
      const d = ((pcrRow.mass_g - baseline.mass_g) / baseline.mass_g * 100).toFixed(1);
      metrics.appendChild(m("Mass vs baseline", (d > 0 ? "+" : "") + d + "%"));
    }
    if (pcrRow.carbon_intensity_kg_co2e_per_kg != null) {
      metrics.appendChild(m("Carbon intensity",
        pcrRow.carbon_intensity_kg_co2e_per_kg + " kg CO₂e/kg"));
    }
    if (pcrRow.min_safety_factor != null) {
      metrics.appendChild(m("Min ISTA SF", pcrRow.min_safety_factor.toFixed(2)));
    }
    metrics.hidden = false;
  }

  // -------------------------------------------------------- accuracy panel
  async function refreshAccuracyPanel() {
    const panel = document.getElementById("accuracy-panel");
    if (!panel) return;
    const caseId = window.CURRENT_CASE_ID || localStorage.getItem("cpg.case_id") || localStorage.getItem("designedge.case_id");
    if (!caseId) { panel.hidden = true; return; }
    // Show panel once the case has produced at least one ISTA verdict.
    try {
      const r = await api("/cases/" + caseId + "/learning");
      panel.hidden = false;
      const pill = document.getElementById("acc-cal-pill");
      if (pill) pill.textContent = "calibration ×" + (r.calibration_multiplier || 1);
      const hist = (r.history && r.history.records) || [];
      const narrEl = document.getElementById("acc-narrative");
      if (hist.length && narrEl) {
        const last = hist[0];
        narrEl.hidden = false;
        narrEl.innerHTML =
          `<b>Last recorded test:</b> predicted <em>${last.predicted_verdict || "—"}</em>` +
          ` (min SF ${last.predicted_min_sf ?? "—"}), actual <em>${last.actual_verdict}</em>.` +
          ` Root cause: ${last.root_cause}. Calibration set to ×${last.calibration_multiplier}.` +
          (last.learning_narrative ? `<br>${last.learning_narrative}` : "");
      } else if (narrEl) {
        narrEl.hidden = true;
      }
    } catch (e) {
      panel.hidden = true;
    }
  }

  function wireAccuracyPanel() {
    const submit = document.getElementById("acc-submit");
    if (!submit) return;
    submit.addEventListener("click", async () => {
      const caseId = window.CURRENT_CASE_ID || localStorage.getItem("cpg.case_id") || localStorage.getItem("designedge.case_id");
      if (!caseId) return;
      const verdict = (document.getElementById("acc-verdict") || {}).value || "pass";
      const mode = (document.getElementById("acc-mode") || {}).value || null;
      const drop = parseFloat((document.getElementById("acc-drop") || {}).value);
      const notes = (document.getElementById("acc-notes") || {}).value || null;
      submit.disabled = true; submit.textContent = "Recording…";
      try {
        const r = await api("/cases/" + caseId + "/actual-ista", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            actual_verdict: verdict,
            actual_failure_mode: mode,
            actual_drop_height_m: isFinite(drop) ? drop : null,
            notes,
          }),
        });
        const narrEl = document.getElementById("acc-narrative");
        const pill = document.getElementById("acc-cal-pill");
        if (pill) pill.textContent = "calibration ×" + r.calibration_multiplier;
        if (narrEl) {
          narrEl.hidden = false;
          narrEl.innerHTML =
            `<b>Recorded.</b> Predicted <em>${r.predicted_verdict || "—"}</em>` +
            ` (min SF ${r.predicted_min_sf ?? "—"}), actual <em>${r.actual_verdict}</em>.` +
            ` Root cause: ${r.root_cause}. Future runs on this material × packaging-type` +
            ` pair will use calibration ×${r.calibration_multiplier}.` +
            (r.learning_narrative ? `<br>${r.learning_narrative}` : "");
        }
      } catch (e) {
        alert(e.message);
      } finally {
        submit.disabled = false; submit.textContent = "Record actual result";
      }
    });
  }

  // -------------------------------------------------------- wiring ---------
  function init() {
    mountTopbar();
    wirePCRButtons();
    refreshPCRCard();
    wireAccuracyPanel();
    refreshAccuracyPanel();

    // Refresh user balance + PCR card on key UI events.
    window.addEventListener("hashchange", () => {
      if (location.hash.indexOf("report") >= 0 || location.hash.indexOf("results") >= 0) {
        refreshPCRCard();
        refreshAccuracyPanel();
      }
      if (location.hash.indexOf("optimise") >= 0) {
        // The optimisation result is populated by app.js after the LLM run;
        // poll a few times so we catch it without coupling to its lifecycle.
        let n = 0;
        const t = setInterval(() => { refreshPCRIntel(); if (++n > 20) clearInterval(t); }, 800);
      }
    });
    window.addEventListener("cpg:auth-changed", refreshChip);
    window.addEventListener("cpg:insufficient-tokens", (ev) => showBuyModal(ev.detail));
    window.addEventListener("cpg:auth-required", () => {
      refreshChip();
      showLoginModal();
    });

    // Periodic balance refresh so the chip stays accurate after a run.
    setInterval(async () => {
      if (!auth.token()) return;
      try {
        const u = await api("/auth/me");
        auth.updateUser(u);
        refreshChip();
      } catch (e) { /* token may have expired; cleared by 401 handler */ }
    }, 15_000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
