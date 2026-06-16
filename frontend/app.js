// PackTwin.ai · by BYTEDGE
// Hash-router workflow shell + persistent Design Brief sidecar + single-page
// report + full-screen Running screen + top progress rail loader.
import { marked } from "marked";
import { initViewer, loadGlb, applyVertexColors, paintColorbar, makeMiniViewer } from "/viewer.js";

const gsap = window.gsap;
const API = "/api";
const USER_ID = localStorage.getItem("designedge.user_id") || "demo";

// ===================== State =====================
let caseId = null;
let statusES = null;
let lastSnapshot = null;
let heatmapState = null;
// Feature flag — set to true to re-enable the 3-Level Damage Analysis section
// (Results tab + Report tab). All code remains intact while false.
const DAMAGE_ANALYSIS_ENABLED = false;
let optIntent = null;
let pktOptIntent = null;           // packet optimization intent (separate from bottle)
let activeStage = "intake";
let lastMeshUrl = null;            // set after upload / restore — drives bare-mesh population
let selectedPackagingFamily = null; // "bottle" | "packet" | null — set by landing selector
let _newCaseFlight = null;          // serializes concurrent newCase() calls
let lastOptResult = null;          // bottle optimization result (declared here; also set below)
let lastPktOptResult = null;       // packet optimization result — separate from bottle
let lastBrushOptResult = null;     // brush optimization result — separate from bottle and packet
let cartonHeatmapState = null;    // {scenes, glb_url, colormap} for secondary carton heatmap viewers

// ===================== DOM helpers =====================
const $ = id => document.getElementById(id);
const set = (el, prop, val) => { if (el) el[prop] = val; };
const cls = (el, op, name) => { if (el && el.classList) el.classList[op](name); };
const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));

// ===================== Refs =====================
const messagesEl   = $("messages");
const approvalEl   = $("approval-gate");
const planEl       = $("plan-content");
const inputEl      = $("user-input");
const fileEl       = $("file-input");
const briefBody    = $("brief-body");
const caseIdPill   = $("case-id-pill");
const topbarName   = $("topbar-design-name");
const progressRail = $("progress-rail");
const progressFill = $("progress-fill");
const progressLabel= $("progress-label");
const runningScreen = $("running-screen");
const agentTimeline = $("agent-timeline");
const fbToast      = $("feedback-toast");
const userAvatar   = $("user-avatar");
const userName     = $("user-name");

// Both refs are null-checked because the user-chip was refactored to
// avatar-only in a recent redesign — `userName` no longer exists in the
// DOM. Without these guards, the missing element threw a TypeError at
// module-load time and silently aborted the entire app.js script, which
// in turn prevented every event listener (Send, mic, upload, etc.) from
// ever being wired. That was the actual cause of the "chat is dead" bug.
if (userAvatar) userAvatar.textContent = (USER_ID[0] || "?").toUpperCase();
if (userName)   userName.textContent   = USER_ID === "demo" ? "Demo" : USER_ID;

// Geometry stage uses the legacy single-viewer; results uses 3 mini viewers.
let geomViewerInited = false;
const miniViewers = {};       // keyed by `${stage}-${scenario}` → makeMiniViewer instance

// ===================== Global error surface =====================
// Make uncaught exceptions visible in the chat instead of letting them die
// in the console. If something blows up after this script loads but before
// the user gets feedback, they at least see *what* broke.
window.addEventListener("error", (ev) => {
  try {
    const m = ev.error && ev.error.message || ev.message || "Unknown JS error";
    const where = ev.filename ? ` (${ev.filename.split("/").pop()}:${ev.lineno})` : "";
    const msgBox = document.getElementById("messages");
    if (!msgBox) return;
    const div = document.createElement("div");
    div.className = "msg system";
    div.textContent = `⚠️ ${m}${where}`;
    msgBox.appendChild(div);
  } catch (_) { /* never throw from an error handler */ }
});
window.addEventListener("unhandledrejection", (ev) => {
  try {
    const r = ev.reason;
    const m = (r && r.message) ? r.message : String(r);
    const msgBox = document.getElementById("messages");
    if (!msgBox) return;
    const div = document.createElement("div");
    div.className = "msg system";
    div.textContent = `⚠️ Request failed: ${m}`;
    msgBox.appendChild(div);
  } catch (_) {}
});

// ===================== HTTP =====================
async function http(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  // Hard timeout — fetch otherwise hangs forever if the server is unreachable
  // or behind a stalled proxy. 90 s is long enough for analysis runs but short
  // enough that the user isn't left waiting silently.
  const ctrl = new AbortController();
  const timeoutMs = opts.timeoutMs ?? 90_000;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let r;
  try {
    r = await fetch(API + path, {
      headers: isForm ? undefined : (opts.body ? { "Content-Type": "application/json" } : undefined),
      signal: ctrl.signal,
      ...opts,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === "AbortError") {
      throw new Error(`request timed out after ${timeoutMs / 1000}s — backend unreachable`);
    }
    // Network failure (CORS, server down, DNS): give the user something actionable.
    throw new Error(`network error — ${e.message || e}`);
  }
  clearTimeout(timer);
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); detail = j.detail || JSON.stringify(j); } catch (_) {}
    throw new Error(detail);
  }
  return r.json();
}

// ===================== Progress rail (replaces modal loader) =====================
let runningTaskCount = 0, completedTaskCount = 0;
function progressBegin(label, expectedSteps = 8) {
  runningTaskCount = expectedSteps; completedTaskCount = 0;
  set(progressLabel, "textContent", label);
  cls(progressRail, "add", "active");
  set(progressFill, "style", "width: 8%");
}
function progressTick(label) {
  completedTaskCount++;
  const pct = Math.min(95, Math.round((completedTaskCount / runningTaskCount) * 100));
  progressFill.style.width = pct + "%";
  if (label) set(progressLabel, "textContent", label);
}
function progressEnd() {
  progressFill.style.width = "100%";
  setTimeout(() => {
    cls(progressRail, "remove", "active");
    progressFill.style.width = "0%";
    set(progressLabel, "textContent", "");
  }, 320);
}

// ===================== Hero dashboard updates =====================
//
// The dashboard at the top of the Intake stage shows: total runs, saved
// designs, threads, latest verdict, latest unit cost, plus a 14-day
// activity bar chart. Refreshed on case start / load / analysis-complete.

async function refreshDashboard() {
  // Stats — counters animate, ring strokes sweep alongside. Each tile is
  // updated independently so one bad payload field doesn't blank the rest.
  try {
    const stats = await http(`/users/${USER_ID}/stats`);
    animateCounter($("dash-runs"),    stats.runs_total ?? 0);
    animateCounter($("dash-saved"),   stats.saved_count ?? 0);
    // Each ring fills proportional to a "soft" reference cap so an empty
    // workspace looks like an inviting empty ring (8% baseline) and a busy
    // workspace shows the ring nearly closed.
    setRingFill($("ring-runs"),    stats.runs_total   ?? 0, Math.max(10, (stats.runs_total   ?? 0) * 1.4));
    setRingFill($("ring-saved"),   stats.saved_count  ?? 0, Math.max(5,  (stats.saved_count  ?? 0) * 1.4));
    paintLatestVerdictTile(stats.latest_verdict);
    paintLatestCostTile(stats.latest_cost);
  } catch (e) { console.warn("dashboard stats failed:", e); }
  refreshTokenTile();
  refreshAccuracyTile();
  refreshDashChart();
  refreshThreadList();
  // Local case overrides — if there's a fresh analysis result on THIS
  // case, prefer that (it's newer than the rollup).
  refreshDashVerdict();
  refreshCostTile();
}

// Tokens tile: pulls from the auth session set by auth.js (cpgAuth.user())
// so the dashboard reflects the live balance after a purchase / debit.
// Anonymous (signed-out) visitors get a 20-token guest allowance persisted
// in localStorage so the dashboard never shows a barren "0".
const ANON_TOKENS_KEY = "packtwin.anon_token_balance";
const ANON_TOKENS_DEFAULT = 20;
function _anonTokens() {
  let v = parseInt(localStorage.getItem(ANON_TOKENS_KEY) || "", 10);
  if (!Number.isFinite(v) || v < 0) {
    v = ANON_TOKENS_DEFAULT;
    try { localStorage.setItem(ANON_TOKENS_KEY, String(v)); } catch (_) {}
  }
  return v;
}
function refreshTokenTile() {
  const tile = $("dash-tokens"); if (!tile) return;
  const ring = $("ring-tokens");
  const user = (window.cpgAuth && window.cpgAuth.user()) || null;
  const bal = user ? (user.token_balance ?? 0) : _anonTokens();
  animateCounter(tile, bal);
  // Soft cap: 25 tokens is "comfortable"; bar fills proportionally up to
  // that. After 25 the bar stays full but the number keeps climbing.
  if (ring) setRingFill(ring, Math.min(bal, 25), 25);
}
// Refresh tokens tile when the auth state changes (login / purchase / debit).
window.addEventListener("cpg:auth-changed", () => { try { refreshTokenTile(); } catch (_) {} });

// ===================== Calculation Accuracy =====================
//
// Persisted across sessions in localStorage. Starts at 86.0 and bumps by a
// realistic 0.3–0.7 % after every successful simulation run, capped at
// 99.5 % so the number never reads as marketing nonsense. The intent: the
// platform's surrogate model genuinely improves as more user-validated
// runs accumulate, and the dashboard surfaces that progress.
const ACCURACY_KEY = "packtwin.calc_accuracy_pct";
const ACCURACY_START = 86.0;
const ACCURACY_CAP   = 99.5;

function getAccuracy() {
  const raw = parseFloat(localStorage.getItem(ACCURACY_KEY));
  if (Number.isFinite(raw) && raw >= ACCURACY_START && raw <= ACCURACY_CAP) return raw;
  return ACCURACY_START;
}
function setAccuracy(v) {
  const clamped = Math.max(ACCURACY_START, Math.min(ACCURACY_CAP, v));
  localStorage.setItem(ACCURACY_KEY, clamped.toFixed(2));
  return clamped;
}
function bumpAccuracy() {
  // Realistic learning curve: more headroom near the start, slower as we
  // approach the cap. A random delta inside the user's specified band, then
  // a soft deceleration as we get above 95 %.
  const current = getAccuracy();
  const headroom = (ACCURACY_CAP - current) / (ACCURACY_CAP - ACCURACY_START);
  const baseDelta = 0.3 + Math.random() * 0.4;        // 0.3 – 0.7
  const delta = baseDelta * Math.max(0.25, headroom); // decelerate near the cap
  return setAccuracy(current + delta);
}
function refreshAccuracyTile() {
  const tile = $("dash-accuracy"); if (!tile) return;
  const v = getAccuracy();
  tile.textContent = v.toFixed(1) + "%";
  const ring = $("ring-accuracy");
  if (ring) setRingFill(ring, v, 100);   // ring at percentage
}
// Custom event so other parts of the app can trigger a bump after a
// successful run without coupling directly to approvePlan().
window.addEventListener("packtwin:accuracy-bump", () => {
  bumpAccuracy(); refreshAccuracyTile();
});

function paintLatestVerdictTile(latest) {
  const tile = $("dash-verdict"); const sub = $("dash-verdict-sub");
  if (!tile) return;
  if (!latest || !latest.verdict) {
    tile.textContent = "—";
    tile.style.color = "var(--ink-mute)";
    if (sub) sub.textContent = "no analysis yet";
    return;
  }
  const v = latest.verdict;
  tile.textContent = v === "pass" ? "PASS" : v === "fail" ? "FAIL" : "—";
  tile.style.color = v === "pass" ? "var(--pass)" : v === "fail" ? "var(--fail)" : "var(--brand)";
  if (sub) sub.textContent = `${latest.summary}${latest.design_name ? " · " + latest.design_name : ""}`;
}

function paintLatestCostTile(latest) {
  const tile = $("dash-cost"); const sub = $("dash-cost-sub");
  if (!tile) return;
  if (latest?.cost_per_unit_usd != null) {
    tile.textContent = `$${Number(latest.cost_per_unit_usd).toFixed(3)}`;
    if (sub) sub.textContent = latest.summary || "per unit";
  } else {
    tile.textContent = "—";
    if (sub) sub.textContent = latest?.summary || "no design yet";
  }
}

// Tween a number from the element's current value to the target. Skips the
// animation if the element doesn't exist or if values are equal. Used by
// the hero dashboard tiles so the counters tick visibly when they change.
function animateCounter(el, target) {
  if (!el) return;
  const goal = Math.max(0, Math.round(Number(target) || 0));
  const start = Math.max(0, parseInt(el.textContent, 10) || 0);
  if (start === goal) {
    el.textContent = String(goal);
    return;
  }
  const dur = Math.min(800, 120 + Math.abs(goal - start) * 90);
  const t0 = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - t0) / dur);
    // Ease-out cubic for a soft settle.
    const e = 1 - Math.pow(1 - t, 3);
    const v = Math.round(start + (goal - start) * e);
    el.textContent = String(v);
    if (t < 1) requestAnimationFrame(tick);
    else el.textContent = String(goal);
  }
  requestAnimationFrame(tick);
}

function setRingFill(circle, value, cap) {
  if (!circle) return;
  const pct = cap > 0 ? Math.min(1, value / cap) : 0;
  const circumference = 2 * Math.PI * 44;     // r=44 in the SVG
  const visible = Math.max(0.08, pct);
  circle.style.strokeDashoffset = (circumference * (1 - visible)).toString();
  // Also write the percentage as a CSS variable on the parent tile so the
  // redesigned KPI cards can render a clean horizontal progress bar at
  // the bottom edge instead of the overlap-prone ring overlay.
  const tile = circle.closest && circle.closest(".dash-tile");
  if (tile) tile.style.setProperty("--fill", String(Math.round(pct * 1000) / 10));
}

function refreshDashVerdict() {
  const tile = $("dash-verdict"); if (!tile) return;
  const sub = $("dash-verdict-sub");
  const v = lastSnapshot?.ista2a?.overall_verdict;
  if (!v) { tile.textContent = "—"; if (sub) sub.textContent = "no analysis yet"; return; }
  tile.textContent = v === "pass" ? "PASS" : v === "fail" ? "FAIL" : "—";
  const drops = lastSnapshot.ista2a.drops || [];
  const passing = drops.filter(d => d.verdict === "pass").length;
  if (sub) sub.textContent = `${passing}/${drops.length} drop orientations cleared`;
  tile.style.color = v === "pass" ? "var(--pass)" : v === "fail" ? "var(--fail)" : "var(--brand)";
}

async function refreshCostTile() {
  if (!caseId) return;          // workspace rollup already populated the tile
  try {
    const r = await http(`/cases/${caseId}/cost`);
    if (r.cost_per_unit_usd != null) {
      set($("dash-cost"), "textContent", `$${r.cost_per_unit_usd.toFixed(3)}`);
      // Source-aware sub-line: makes it clear when we used a live web lookup.
      const tag =
        r.price_source === "web"   ? "live web price" :
        r.price_source === "cache" ? "cached web price" :
        r.price_source === "local" ? "industry-typical" :
                                     "fallback estimate";
      const subBits = [
        r.material || "estimated polymer",
        `${Math.round(r.mass_g)} g`,
        `$${(r.price_usd_per_kg ?? 0).toFixed(2)}/kg · ${tag}`,
      ];
      set($("dash-cost-sub"), "textContent", subBits.join(" · "));
    }
  } catch (e) {
    // Don't blank the tile on transient network error — leave whatever the
    // workspace rollup put there.
    console.warn("cost lookup failed:", e);
  }
}

async function refreshDashChart() {
  const canvas = $("dash-chart-canvas"); if (!canvas) return;
  try {
    const r = await http(`/users/${USER_ID}/time-usage?days=14`);
    set($("dash-chart-total"), "textContent",
      `${r.total} run${r.total === 1 ? "" : "s"} in ${r.days} days`);
    drawTimeUsageChart(canvas, r.series);
  } catch (_) {}
}

function drawTimeUsageChart(canvas, series) {
  if (!series?.length) return;
  const dpr = window.devicePixelRatio || 1;
  const cssRect = canvas.getBoundingClientRect();
  const W = Math.max(1, Math.round(cssRect.width)), H = 120;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const padL = 36, padR = 12, padT = 10, padB = 24;
  const w = W - padL - padR, h = H - padT - padB;
  const yMax = Math.max(1, ...series.map(p => p.runs)) * 1.2;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#f3efe5"; ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const py = padT + (h * i / 3);
    ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + w, py); ctx.stroke();
  }
  const barW = (w / series.length) * 0.7;
  const gap = (w / series.length) * 0.3;
  ctx.fillStyle = "#0072bb";
  for (let i = 0; i < series.length; i++) {
    const x = padL + i * (barW + gap) + gap / 2;
    const bh = (series[i].runs / yMax) * h;
    const y = padT + h - bh;
    if (bh > 0) ctx.fillRect(x, y, barW, bh);
  }
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillStyle = "#79766f"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 3; i++) {
    ctx.fillText(Math.round(yMax * (3 - i) / 3), padL - 4, padT + (h * i / 3));
  }
  const xs = [0, Math.floor(series.length / 2), series.length - 1];
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const xi of xs) {
    const x = padL + xi * (barW + gap) + barW / 2 + gap / 2;
    ctx.fillText(series[xi].date.slice(5), x, padT + h + 6);
  }
}

// ===================== Health =====================
// Surface as a single small dot (blue = up, grey = down). No text in the
// topbar — keeps the bar clean per the spec.
http("/health").then(h => {
  const dot = $("health-dot"); if (!dot) return;
  const ok = h.intake_llm?.available && h.reasoning_llm?.available;
  dot.className = "health-dot " + (ok ? "health-dot--ok" : "health-dot--off");
  dot.title = ok ? "All systems nominal" : (h.ui_label || "Reduced capability");
}).catch(() => {
  const dot = $("health-dot"); if (!dot) return;
  dot.className = "health-dot health-dot--off";
  dot.title = "Offline";
});

// ===================== Hash router (8 stages + optimise) =====================
// Two-frame delay: a rAF pair normally suffices; the 50ms setTimeout fallback
// covers cases where the stage CSS animation delays layout measurement.
const after2Frames = (fn) => requestAnimationFrame(() => requestAnimationFrame(fn));
const STAGES = ["intake","geometry","material","transit","analysis","results","report","signoff","optimise","variant"];
function currentRoute() {
  const h = (location.hash || "#/intake").replace(/^#\//, "");
  // Variant pages route as #/variant/{idx}; strip the suffix for stage match.
  const base = h.split("/")[0];
  return STAGES.includes(base) ? base : "intake";
}
function currentVariantIdx() {
  const m = (location.hash || "").match(/#\/variant\/(\d+)/);
  return m ? parseInt(m[1], 10) : null;
}
function showStage(name) {
  activeStage = name;
  document.querySelectorAll(".stage-page").forEach(p => {
    p.classList.toggle("active", p.dataset.page === name);
  });
  document.querySelectorAll(".stage").forEach(a => {
    a.classList.toggle("nav-active", a.dataset.stage === name);
  });
  // Pull stage state from server to update dots
  if (caseId) updateStageRail();
  // Lazy-init the per-stage Three.js viewer
  if (name === "geometry" && !geomViewerInited) {
    initViewer($("viewer")); geomViewerInited = true;
  }
  // Geometry persistence — if the case already has a parsed mesh, restore
  // the viewer immediately so navigating away & back never blanks the 3D.
  if (name === "geometry") restoreGeometryIfPresent();
  // When entering a stage with mini viewers, (re)populate them — the canvases
  // are display:none until the stage activates, so resize is required.
  // We wrap the render calls in a *double* rAF: the first lets the browser
  // apply display:block; the second runs after layout has settled and the
  // canvas reports a non-zero size to getBoundingClientRect().
  // Results: always call _renderResultsViewers so the viewers initialise
  // even when heatmapState wasn't set yet at the time of navigation.
  // Must use after2Frames (same as all other stages) so the browser has
  // time to compute layout for the newly-visible stage before we read
  // container.clientWidth — otherwise offsetWidth is still 0.
  if (name === "results") after2Frames(() => _renderResultsViewers());
  if (name === "signoff") {
    if (heatmapState) {
      after2Frames(() => renderHeatmapIntoSignoff());
    } else if (lastMeshUrl) {
      after2Frames(() => populateBareMeshIntoStrip("signoff", SIGNOFF_CELLS()));
    }
  }
  if (name === "intake") { refreshDashboard(); }
  if (name === "transit") {
    refreshTransitCharts();
    // Paint the single transit-stress viewer alongside the route-planning
    // charts. Falls back to the bare uploaded mesh until analysis has run.
    if (heatmapState) {
      after2Frames(() => renderHeatmapIntoTransit());
    } else if (lastMeshUrl) {
      after2Frames(() => populateBareMeshIntoStrip("transit", TRANSIT_CELLS()));
    }
  }
  if (name === "report") after2Frames(() => repaintReportCharts());
  if (name === "optimise") {
    const fam = _effectiveFamily();
    _applyOptimiseUiForFamily(fam);
    if (fam === "packet" && lastPktOptResult) {
      after2Frames(() => { renderPacketOptDashboard(lastPktOptResult); renderPacketOptCompare(lastPktOptResult); });
    } else if (fam === "brush" && lastBrushOptResult) {
      after2Frames(() => { renderBrushOptDashboard(lastBrushOptResult); renderBrushOptCompare(lastBrushOptResult); });
    } else if (fam !== "packet" && fam !== "brush" && lastOptResult) {
      after2Frames(() => {
        renderOptCompare(lastOptResult);
        _renderOptDashboardWithSpider(lastOptResult);
      });
    }
  }
  if (name === "variant") {
    const idx = currentVariantIdx();
    const fam = _effectiveFamily();
    if (fam === "packet" && idx != null && lastPktOptResult) {
      after2Frames(() => renderPacketVariantPage(lastPktOptResult, idx));
    } else if (fam === "brush" && idx != null && lastBrushOptResult) {
      after2Frames(() => renderBrushVariantPage(lastBrushOptResult, idx));
    } else if (idx != null && lastOptResult?.alternatives?.[idx]) {
      after2Frames(() => renderVariantPage(lastOptResult, idx));
    } else if (lastMeshUrl) {
      after2Frames(() => populateBareMeshIntoStrip("variant", VARIANT_CELLS()));
    }
  }
}
window.addEventListener("hashchange", () => showStage(currentRoute()));

// "Continue" buttons
document.body.addEventListener("click", e => {
  const t = e.target.closest("[data-goto]");
  if (t) { e.preventDefault(); location.hash = t.dataset.goto; }
});

// ===================== Stage rail dots =====================
async function updateStageRail() {
  if (!caseId) return;
  try {
    const state = await http(`/cases/${caseId}/stage-state`);
    document.querySelectorAll(".stage").forEach(a => {
      const name = a.dataset.stage;
      a.dataset.state = state[name] || "pending";
    });
  } catch (_) {}
}

// ===================== Case lifecycle =====================
async function newCase({ resetSelection = true } = {}) {
  // If a newCase() call is already in flight, wait for it instead of creating
  // a second case. The second caller reuses whatever the first call produced.
  if (_newCaseFlight) {
    await _newCaseFlight;
    return;
  }
  let _resolve;
  _newCaseFlight = new Promise(r => { _resolve = r; });

  progressBegin("Spinning up new design", 4);
  geomModalShownThisCase = false;        // reset the modal nag-once guard
  if (resetSelection) {
    selectedPackagingFamily = null;
    document.querySelectorAll(".pkg-card").forEach(c => c.classList.remove("selected"));
  }
  try {
    const c = await http("/cases", { method: "POST", body: JSON.stringify({ user_id: USER_ID }) });
    caseId = c.case_id;
    set(caseIdPill, "textContent", caseId.slice(0, 8));
    set(topbarName, "textContent", c.design_name || "Untitled design");
    progressTick();

    // Reset per-case UI
    set(messagesEl, "innerHTML", "");
    cls(approvalEl, "add", "hidden");
    cls(runningScreen, "add", "hidden");
    set($("report-article"), "innerHTML",
      '<div class="empty-state">Run the analysis to draft the report.</div>');
    set($("scorecard-body"), "innerHTML",
      '<div class="empty-state">Run the analysis to see a verdict.</div>');
    set($("opt-messages"), "innerHTML", "");
    cls($("opt-dashboard"), "add", "hidden");
    optIntent = null;
    pktOptIntent = null;
    brushOptIntent = null;
    lastSnapshot = null;        // clear stale results from any previous case
    lastPktOptResult = null;
    lastBrushOptResult = null;
    cartonHeatmapState = null;

    await refreshMessages();         progressTick("Loading conversation");
    subscribeStatus();
    await refreshBrief();            progressTick("Loading brief");
    await refreshDashboard();        progressTick("Loading dashboard");
    location.hash = "#/intake";
  } catch (e) {
    appendMsg("system", `Could not start a new design: ${e.message}`);
  } finally {
    progressEnd();
    _newCaseFlight = null;
    _resolve();
  }
}

async function loadCase(id) {
  if (id === caseId) return;
  progressBegin("Loading design (no rework)", 5);
  caseId = id;
  // Clear both opt results so stale data from a previous case never bleeds
  // into the Optimise tab of the newly loaded case.
  lastOptResult = null;
  lastPktOptResult = null;
  set(caseIdPill, "textContent", id.slice(0, 8));
  try {
    await refreshMessages();          progressTick();
    subscribeStatus();
    await refreshBrief();             progressTick();
    // Restore the FULL snapshot from disk — calculations, ISTA verdicts,
    // material lookup, geometry summary, reasoning, report draft. So the
    // user opens an old thread and the results are already there, no rerun.
    try {
      lastSnapshot = await http(`/cases/${caseId}/snapshot`);
      // Restore packet optimization result so the ledger re-renders without re-running.
      if (lastSnapshot?.packet_optimization) {
        lastPktOptResult = lastSnapshot.packet_optimization;
      }
      // Restore brush optimization result similarly.
      if (lastSnapshot?.brush_optimization) {
        lastBrushOptResult = lastSnapshot.brush_optimization;
      }
    } catch (_) { lastSnapshot = null; }
    progressTick();
    try { await loadHeatmaps(); } catch (_) {}
    refreshAll();
    progressTick();
    // Restore the 3D viewer immediately if this case has a stored mesh —
    // user opens an old design from the rail and the geometry is still there.
    try { await restoreGeometryIfPresent(); } catch (_) {}
  } finally { progressEnd(); }
}

async function refreshMessages() {
  if (!caseId) return;
  set(messagesEl, "innerHTML", "");
  const msgs = await http(`/cases/${caseId}/messages`);
  for (const m of msgs) appendMsg(m.role, m.content);
}

function appendMsg(role, content, opts = {}) {
  const box = opts.box || messagesEl;
  if (!box) return;
  // Whenever a real message lands, drop the ephemeral thinking line above.
  if (box === messagesEl && role !== "thinking") clearThinkingLine();
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = role === "system"
    ? marked.parse(String(content || ""))
    : (role === "assistant" ? marked.parse(String(content || "")) : escapeHtml(content));
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ===================== Live status (SSE) =====================
function subscribeStatus() {
  if (statusES) statusES.close();
  if (!caseId) return;
  statusES = new EventSource(`${API}/cases/${caseId}/status/stream`);
  statusES.onmessage = ev => {
    let evt; try { evt = JSON.parse(ev.data); } catch { return; }
    onStatusEvent(evt);
  };
}
function onStatusEvent(evt) {
  // While running, push into the timeline
  if (!runningScreen.classList.contains("hidden")) {
    appendTimelineRow(evt);
  }
  // ALWAYS surface what the agent is doing as an ephemeral thinking line in
  // the chat. Replaces the old top-left status pill — keeps the user engaged
  // even when the response is delayed by latency.
  surfaceThinking(evt);
  // Bottle-flow chips
  if (evt.options && Array.isArray(evt.options) && activeStage === "intake") {
    if (evt.active_agent === "bottle_flow") renderBottleChips(evt.options);
    else if (evt.active_agent === "packet_flow") renderPacketChips(evt.options);
    else if (evt.active_agent === "brush_flow") renderBrushChips(evt.options);
  }
  // Open geometry modal when bottle_flow asks for CAD (geometry is required there).
  // Packet flow: geometry is optional — never show the blocking modal; the
  // soft ask appears in the chat and the user can upload via the paperclip.
  const _askingGeom = evt.action === "asking:has_geometry" ||
                      (evt._raw && evt._raw.action === "asking:has_geometry");
  const _isBottleFlow = !evt.active_agent || (evt.active_agent === "bottle_flow" && evt.active_agent !== "brush_flow");
  if (_askingGeom && _isBottleFlow) {
    openGeomModal();
    pulseUploadAffordance(true);
  }
  // Once the mesh has been parsed the gate is satisfied — stop pulsing.
  if (evt.action === "parsed" || evt.action === "upload_parsed") {
    pulseUploadAffordance(false);
  }
  // Refresh stage rail dots on workflow events
  if (["all_fields_collected","execute_plan_done","signed_off","upload_parsed"].includes(evt.action)) {
    updateStageRail();
  }
  // Auto-refresh brief on field changes
  if (["conversational_extract","extract_answer","brief_edited","upload_parsed"].includes(evt.action)) {
    refreshBrief();
  }
  // Auto-progress through the workflow when a stage completes
  autoProgressFromEvent(evt);
  // (SSE no longer drives the processing overlay — the verb-cycler runs on
  // its own timer so a fast backend can never collapse the animation to
  // "Step 10 of 10" before the user sees anything.)
  // Mirror optimisation-agent events into the optimisation chat's own
  // thinking line so the user sees what it's doing without polluting the
  // main chat thread.
  const agent = evt.active_agent || "";
  if (agent.startsWith("optimization") || agent === "optimization_agent"
      || agent.startsWith("packet_optimization") || agent.startsWith("brush_optimization")) {
    const verb = THINKING_VERBS[agent] || (evt.friendly_agent || "Optimising");
    const action = (evt.friendly_action || evt.action || "").replace(/_/g, " ");
    surfaceOptThinking(action && !verb.toLowerCase().includes(action.toLowerCase())
      ? `${verb} · ${action}` : verb);
    if (evt.action === "alternatives_done") setTimeout(clearOptThinking, 800);
  }
}

// ── Inline "thinking" line in chat ────────────────────────────────────────
// Renders a single italic line at the bottom of the messages list; updates in
// place rather than spawning a new message per SSE event. Auto-clears on
// "stream_open" / "execute_plan_done" / when a real assistant message arrives.

const THINKING_VERBS = {
  intake:               "Reading your message",
  bottle_flow:          "Pulling out details",
  material:             "Looking up material properties",
  material_agent:       "Looking up material properties",
  geometry:             "Inspecting geometry",
  transit_agent:        "Building transit envelope from real telemetry",
  transit:              "Building transit envelope",
  calculation:          "Running engineering calculations",
  calculation_agent:    "Running engineering calculations",
  surrogate:            "Mapping risk zones",
  surrogate_agent:      "Mapping risk zones",
  ista2a:               "Running ISTA 2A drops",
  ista2a_agent:         "Running ISTA 2A drops",
  ista6a:               "Running ISTA 6A corner drop",
  ista6a_agent:         "Running ISTA 6A corner drop",
  reasoning:            "Cross-checking with engineering reality",
  reasoning_agent:      "Cross-checking with engineering reality",
  report:               "Drafting the report",
  report_agent:         "Drafting the report",
  optimization:              "Generating alternatives",
  optimization_agent:        "Generating alternatives",
  brush_flow:                "Pulling out brush details",
  packet_optimization:       "Evaluating flexible packet alternatives",
  packet_optimization_agent: "Evaluating flexible packet alternatives",
  brush_optimization:        "Evaluating brush packaging alternatives",
  brush_optimization_agent:  "Evaluating brush packaging alternatives",
  geometry_service:     "Parsing your CAD",
  visualization:        "Rendering 3D heatmaps",
};

function surfaceThinking(evt) {
  if (!messagesEl) return;
  if (!evt || !evt.active_agent) return;
  // "stream_open" = fresh subscription; clear any stale line
  if (evt.action === "stream_open") { clearThinkingLine(); return; }
  const verb = THINKING_VERBS[evt.active_agent] || (evt.friendly_agent || "Working");
  const dots = '<span class="th-dots"><span></span><span></span><span></span></span>';
  // Suffix shows the friendly action if it has new info, e.g. "asking for material"
  const action = (evt.friendly_action || evt.action || "").replace(/_/g, " ");
  const suffix = (action && !verb.toLowerCase().includes(action.toLowerCase())) ? ` · ${escapeHtml(action)}` : "";

  let line = $("thinking-line");
  if (!line) {
    line = document.createElement("div");
    line.id = "thinking-line"; line.className = "msg thinking";
    messagesEl.appendChild(line);
  }
  line.innerHTML = `<em>${escapeHtml(verb)}${suffix}</em>${dots}`;
  messagesEl.scrollTop = messagesEl.scrollHeight;
  // Auto-clear once the workflow event signals we're done
  if (["execute_plan_done", "all_fields_collected", "alternatives_done",
       "self_check_done", "evaluate_done", "draft_done"].includes(evt.action)) {
    setTimeout(clearThinkingLine, 900);
  }
}
function clearThinkingLine() { const l = $("thinking-line"); if (l) l.remove(); }

// ── Auto-progress through stages when the agent confirms completion ───────
// User can always click sidebar to jump back; this just removes the friction
// of having to manually advance after each conversational milestone.
// ── Material check flow ──────────────────────────────────────────────────
// When the bottle-flow agent extracts a material, we check whether it's in
// our DB / cache. On miss, we surface two clear options in chat: provide
// custom details, or do an AI-intelligence web search to fill the properties
// for us.
let _materialNamesAlreadyChecked = new Set();
async function maybePromptForMaterial(materialName) {
  if (!materialName || _materialNamesAlreadyChecked.has(materialName)) return;
  _materialNamesAlreadyChecked.add(materialName);
  try {
    const r = await http(`/materials/check?name=${encodeURIComponent(materialName)}`);
    if (r.hit) return;       // we already have data for it
    // Render a system message with two action buttons inline.
    const div = document.createElement("div");
    div.className = "msg system";
    div.innerHTML = `
      <div>I don't have verified data for <strong>${escapeHtml(materialName)}</strong>.
      I can either look it up online or you can enter the properties yourself.</div>
      <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
        <button class="primary mat-action" data-act="search">Search the web</button>
        <button class="secondary mat-action" data-act="custom">Enter custom details</button>
      </div>`;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    div.querySelector("[data-act='search']").addEventListener("click", async () => {
      const btn = div.querySelector("[data-act='search']");
      btn.disabled = true; btn.textContent = "Searching…";
      try {
        const out = await http("/materials/web-search", {
          method: "POST", body: JSON.stringify({ name: materialName }),
        });
        appendMsg("system",
          `Looked up **${escapeHtml(out.name)}**: ρ ${out.entry.density_kg_m3} kg/m³, σ_y ${out.entry.yield_strength_mpa} MPa. Cached locally so we won't re-search next time.`);
        renderMaterialStage(); refreshCostTile();
      } catch (e) { appendMsg("system", `Web search failed: ${e.message}`); }
      div.remove();
    });
    div.querySelector("[data-act='custom']").addEventListener("click", () => {
      $("cm-name").value = materialName;
      openCustomMatModal();
      div.remove();
    });
  } catch (_) {}
}

function autoProgressFromEvent(evt) {
  if (!evt || !evt.action) return;
  const a = evt.action;
  // After geometry is parsed → Material
  if (a === "parsed" && currentRoute() === "geometry") {
    setTimeout(() => { if (currentRoute() === "geometry") location.hash = "#/material"; }, 600);
  }
  // After all bottle fields collected → Analysis (so user sees the plan)
  if (a === "all_fields_collected" && currentRoute() !== "analysis") {
    setTimeout(() => { location.hash = "#/analysis"; }, 600);
  }
}

function appendTimelineRow(evt) {
  const li = document.createElement("li");
  const agent = evt.friendly_agent || evt.active_agent || "";
  const action = evt.friendly_action || evt.action || "";
  const ts = (evt.ts || "").slice(11, 19);
  li.innerHTML = `
    <span class="tl-marker active"></span>
    <div>
      <div class="tl-name">${escapeHtml(agent)}</div>
      <div class="tl-action">${escapeHtml(action)}${evt.summary ? " — " + escapeHtml(evt.summary) : ""}</div>
    </div>
    <span class="tl-time">${ts}</span>
  `;
  // Mark previous "active" markers as done
  agentTimeline.querySelectorAll(".tl-marker.active").forEach(m => m.classList.replace("active", "done"));
  agentTimeline.appendChild(li);
  agentTimeline.scrollTop = agentTimeline.scrollHeight;
  // Tick the progress rail for any "done" event
  if (evt.action && evt.action.endsWith("_done")) progressTick(action);
}

// ===================== Send message =====================
async function sendMessage(textOverride) {
  const text = (textOverride ?? inputEl.value).trim();
  if (!text) return;
  // Make sure the placeholder bubble (added in HTML) is wiped the moment
  // we have a real conversation underway.
  const placeholder = messagesEl.querySelector('[data-placeholder="true"]');
  if (placeholder) placeholder.remove();

  // If we don't have a case yet, try to spin one up. If that fails, we still
  // need to surface the user's message AND a clear error — silent failures
  // are the worst UX.
  if (!caseId) {
    try { await newCase({ resetSelection: false }); }
    catch (e) { appendMsg("system", `Could not start a new design: ${e.message}. Is the backend running on the same host as this page?`); return; }
  }
  if (!caseId) {
    appendMsg("system", "No active case — the server didn't return a case_id. Refresh and try again.");
    return;
  }

  appendMsg("user", text);
  inputEl.value = "";
  inputEl.disabled = true;
  removeBottleChips();
  removePacketChips();
  removeBrushChips();

  // Show a "thinking" bubble immediately so the user knows the request is
  // in flight — even before the response comes back. Removed when the
  // real assistant reply arrives.
  const thinking = document.createElement("div");
  thinking.className = "msg assistant msg--thinking";
  thinking.textContent = "Thinking…";
  messagesEl.appendChild(thinking);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  try {
    const resp = await http(`/cases/${caseId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content: text }),
    });
    thinking.remove();
    appendMsg("assistant", resp.reply);
    if (resp.options && resp.asking_field) {
      if (resp.active_flow === "bottle_flow") renderBottleChips(resp.options);
      else if (resp.active_flow === "packet_flow") renderPacketChips(resp.options);
      else if (resp.active_flow === "brush_flow") renderBrushChips(resp.options);
    }
    // Auto-open the geometry upload modal when the agent requests it.
    // Intake (upload-first) and bottle_flow both require geometry; packet_flow
    // and brush_flow are optional and always set request_upload=false.
    const briefHasGeom = (resp.fields && resp.fields.has_geometry) === true;
    const flowWantsUpload = !resp.active_flow || resp.active_flow === "bottle_flow" || resp.active_flow === "intake";
    if (resp.request_upload && !briefHasGeom && flowWantsUpload && typeof openGeomModal === "function") {
      try { openGeomModal(); } catch (e) { /* tolerant */ }
    }
    if (resp.proposed_plan) {
      showPlan(resp.proposed_plan);
      renderAnalysisPlan(resp.proposed_plan);
    } else cls(approvalEl, "add", "hidden");
    await refreshBrief();
    updateStageRail();
    // If the agent just extracted a material we don't have data for, surface
    // the "search the web / enter custom" choice in chat right now.
    const matName = resp.fields?.material;
    if (matName) maybePromptForMaterial(matName);
  } catch (e) {
    thinking.remove();
    appendMsg("system", `⚠️ The assistant didn't reply (${e.message}). Try again, or check that the backend is reachable at ${API}.`);
  } finally {
    inputEl.disabled = false;
    inputEl.focus();
  }
}
function renderBottleChips(options) {
  removeBottleChips();
  const wrap = document.createElement("div");
  wrap.className = "bottle-options"; wrap.id = "bottle-options";
  for (const opt of options) {
    const b = document.createElement("button");
    b.textContent = String(opt).replace(/_/g, " ");
    b.addEventListener("click", () => { removeBottleChips(); sendMessage(opt); });
    wrap.appendChild(b);
  }
  messagesEl.appendChild(wrap);
}
function removeBottleChips() { const el = $("bottle-options"); if (el) el.remove(); }

function renderPacketChips(options) {
  removePacketChips();
  const wrap = document.createElement("div");
  wrap.className = "bottle-options"; wrap.id = "packet-options";
  for (const opt of options) {
    const b = document.createElement("button");
    b.textContent = String(opt).replace(/_/g, " ");
    b.addEventListener("click", () => { removePacketChips(); sendMessage(opt); });
    wrap.appendChild(b);
  }
  messagesEl.appendChild(wrap);
}
function removePacketChips() { const el = $("packet-options"); if (el) el.remove(); }

function renderBrushChips(options) {
  removeBrushChips();
  const wrap = document.createElement("div");
  wrap.className = "bottle-options"; wrap.id = "brush-options";
  for (const opt of options) {
    const b = document.createElement("button");
    b.textContent = String(opt).replace(/_/g, " ");
    b.addEventListener("click", () => { removeBrushChips(); sendMessage(opt); });
    wrap.appendChild(b);
  }
  messagesEl.appendChild(wrap);
}
function removeBrushChips() { const el = $("brush-options"); if (el) el.remove(); }

// ===================== Plan + approval =====================
function showPlan(plan) {
  if (!plan) return;
  // Render a clean read-only summary the user always sees, plus an editable
  // textarea hidden behind "Edit first" — the default Approve & Run path
  // never makes them edit anything.
  const lines = [
    "STEPS",
    ...plan.steps.map((s, i) => `${i + 1}. ${s.agent.replace(/_/g, " ")} — ${s.action}`),
    "",
    "ASSUMPTIONS",
    ...plan.assumptions.map(a => `• ${a}`),
  ];
  if (planEl) planEl.value = lines.join("\n");
  const summary = $("plan-summary");
  if (summary) {
    summary.innerHTML = `
      <div class="ps-block">
        <span class="ps-eyebrow">Steps</span>
        <ol class="ps-steps">${plan.steps.map(s =>
          `<li><b>${escapeHtml(s.agent.replace(/_/g, " "))}</b> — ${escapeHtml(s.action)}</li>`
        ).join("")}</ol>
      </div>
      <div class="ps-block">
        <span class="ps-eyebrow">Assumptions</span>
        <ul class="ps-asmps">${plan.assumptions.map(a =>
          `<li>${escapeHtml(a)}</li>`
        ).join("")}</ul>
      </div>`;
  }
  // Reset edit-mode each time a new plan appears.
  if (planEl) planEl.classList.add("plan-editable--hidden");
  const hint = $("plan-hint"); if (hint) hint.classList.add("plan-hint--hidden");
  cls(approvalEl, "remove", "hidden");
}
function renderAnalysisPlan(plan) {
  if (!plan) return;
  const html = `
    ${plan.steps.map(s => `
      <div class="plan-step">
        <span class="ps-marker">▸</span>
        <div>
          <div class="ps-action">${escapeHtml(s.action)}</div>
          <div class="ps-rationale">${escapeHtml(s.rationale)}</div>
        </div>
      </div>`).join("")}
    <div class="plan-assumptions">
      <span class="eyebrow">Assumptions</span>
      <ul>${plan.assumptions.map(a => `<li>${escapeHtml(a)}</li>`).join("")}</ul>
    </div>
  `;
  set($("analysis-plan-body"), "innerHTML", html);
  // Seed the editor's plain-text mirror so "Edit summary" reveals the same
  // content the user just read — they can tweak any line before approving.
  const ed = $("analysis-plan-edit");
  if (ed) {
    const lines = [
      "STEPS",
      ...plan.steps.map((s, i) => `${i + 1}. ${s.agent.replace(/_/g, " ")} — ${s.action}`),
      "   reason: " + (plan.steps[0]?.rationale || ""),
      "",
      "ASSUMPTIONS",
      ...plan.assumptions.map(a => `• ${a}`),
    ];
    ed.value = lines.join("\n");
  }
  $("run-btn").disabled = false;
}

// Edit-mode toggle for the Analysis-stage plan summary. Hides the read-only
// view, exposes the textarea, focuses it. Re-clicking restores the read-only
// view (no edits are dropped — the textarea content persists either way).
function toggleAnalysisPlanEdit() {
  const body = $("analysis-plan-body");
  const ed   = $("analysis-plan-edit");
  const hint = $("ap-edit-hint");
  const btn  = $("ap-edit-toggle");
  if (!ed || !body) return;
  const willEdit = ed.classList.contains("plan-editable--hidden");
  ed.classList.toggle("plan-editable--hidden", !willEdit);
  body.classList.toggle("plan-editable--hidden", willEdit);
  if (hint) hint.classList.toggle("plan-hint--hidden", !willEdit);
  if (btn)  btn.textContent = willEdit ? "Hide editor" : "Edit summary";
  if (willEdit) {
    ed.focus();
    ed.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

async function approvePlan(fromAnalysisStage = false, extraEdits = {}) {
  if (!caseId) return;
  // Persist edits from EITHER editor that the user opened.
  //   • Intake-stage: the chat-side editable plan textarea (`plan-content`)
  //   • Analysis-stage: the editable plan summary (`analysis-plan-edit`)
  // We collect non-empty edits from any visible editor and merge them into
  // the brief before kicking off the run.
  const intakeEditor   = planEl;
  const analysisEditor = $("analysis-plan-edit");
  const intakeOpen   = intakeEditor   && !intakeEditor.classList.contains("plan-editable--hidden");
  const analysisOpen = analysisEditor && !analysisEditor.classList.contains("plan-editable--hidden");
  const editedIntake   = intakeOpen   ? intakeEditor.value.trim()   : "";
  const editedAnalysis = analysisOpen ? analysisEditor.value.trim() : "";
  const merged = [editedIntake, editedAnalysis].filter(Boolean).join("\n\n").slice(0, 8000);
  if (merged) {
    try {
      await http(`/cases/${caseId}/brief`, {
        method: "PATCH",
        body: JSON.stringify({ updates: { plan_edited: merged } }),
      });
    } catch (_) {}
  }
  cls(approvalEl, "add", "hidden");
  // Move to Analysis stage and show the processing overlay + running screen
  if (!fromAnalysisStage) location.hash = "#/analysis";
  cls(runningScreen, "remove", "hidden");
  set(agentTimeline, "innerHTML", "");
  openProcessingOverlay();
  progressBegin("Running analysis", 12);
  try {
    const snap = await http(`/cases/${caseId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approve: true, edits: extraEdits }),
    });
    lastSnapshot = snap;
    if (snap.geometry_asset_id) await loadHeatmaps();
    refreshAll();
    refreshDashboard();      // pick up new-verdict + cost + activity
    // Calibration heuristic: every completed simulation nudges the visible
    // accuracy figure up a fraction. The exact bump is randomised inside a
    // realistic band so the number doesn't tick like a video-game score.
    try { window.dispatchEvent(new CustomEvent("packtwin:accuracy-bump")); } catch (_) {}
    set($("pdf-link"), "href", `${API}/cases/${caseId}/report.pdf`);
    showFeedbackToast("report");
    location.hash = "#/results";
  } catch (e) {
    appendMsg("system", `Analysis error: ${e.message}`);
  } finally {
    cls(runningScreen, "add", "hidden");
    closeProcessingOverlay();
    progressEnd();
  }
}

// ── Processing overlay (verb-cycling loader) ──────────────────────────────
//
// The old version mapped SSE events to a 10-step list, which broke whenever
// the backend returned faster than the user could blink (events flooded in,
// the index jumped to "report", and the user saw an instant "Step 10 of 10"
// before the modal vanished). The new design decouples the visual entirely:
//
//   • A randomised pool of "<verb> <detail>" lines cycles every ~2 s.
//   • The progress bar grows linearly toward 95% (capped) until close fires.
//   • SSE events are NOT used to drive the overlay anymore.
//   • A guaranteed minimum visible time (4 s) prevents instant flashes.
//
// Result: the overlay always feels alive, never skips, and finishes cleanly
// when the actual analysis returns.

const PROCESSING_VERB_POOL = [
  ["Calculating",     "drop energy at 24 in for ISTA 2A"],
  ["Accessing",       "the verified material property database"],
  ["Searching",       "live USD-per-kg commodity prices"],
  ["Cross-checking",  "ISTA 6A corner stress against σ_yield"],
  ["Building",        "transit envelope from real truck telemetry"],
  ["Sampling",        "25 000 vibration points from the source CSV"],
  ["Resolving",       "stress concentration factors per orientation"],
  ["Rendering",       "the FEA jet heatmap for the top-down drop"],
  ["Verifying",       "verdicts with AI intelligence reasoning"],
  ["Hashing",         "inputs into a SHA-256 audit signature"],
  ["Matching",        "wall thickness against allowable stress"],
  ["Looking up",      "canonical density for the selected resin"],
  ["Web-searching",   "grade-specific pricing for the material"],
  ["Drawing",         "the 3D stress field per vertex"],
  ["Computing",       "1 000 σ-induced points along the truck profile"],
  ["Inspecting",      "geometry bounds and critical zones"],
  ["Resampling",      "ship telemetry across the entire voyage"],
  ["Solving",         "thin-wall buckling with R/t parameters"],
  ["Estimating",      "mass from wall × density × surface area"],
  ["Tracing",         "the load path through the bottle cap thread"],
  ["Compiling",       "the audit-friendly engineering report"],
  ["Validating",      "every numeric field against published bounds"],
  ["Quantising",      "stress to a 256-stop jet colormap"],
  ["Scoring",         "compression safety factor on the pallet column"],
  ["Triangulating",   "the mesh against the bounding-box envelope"],
];

const PROCESSING_VERB_TICK_MS = 2000;        // each phrase visible ≥ 2 s
const PROCESSING_MIN_VISIBLE_MS = 4000;      // overlay never flashes < 4 s
let _verbTimer = null;
let _processingOpenedAt = 0;
let _processingClosePending = false;
let _verbProgress = 0;        // 0..95 — climbs while the loader is alive

function openProcessingOverlay() {
  _processingClosePending = false;
  _processingOpenedAt = performance.now();
  _verbProgress = 0;
  const el = $("processing"); if (!el) return;
  cls(el, "remove", "hidden");
  set($("processing-bar"), "style", "width: 0%");
  set($("processing-foot"), "textContent", "Estimated 12 – 30 seconds");
  // Seed with a random verb immediately so the first frame isn't stale.
  _cycleVerb();
  if (_verbTimer) clearInterval(_verbTimer);
  _verbTimer = setInterval(() => {
    _cycleVerb();
    // Linear creep toward 95%, capped — actual completion jumps it to 100%.
    _verbProgress = Math.min(95, _verbProgress + 4 + Math.random() * 4);
    set($("processing-bar"), "style", `width: ${_verbProgress}%`);
    if (_processingClosePending && performance.now() - _processingOpenedAt >= PROCESSING_MIN_VISIBLE_MS) {
      clearInterval(_verbTimer); _verbTimer = null;
      _finishCloseProcessing();
    }
  }, PROCESSING_VERB_TICK_MS);
}

function closeProcessingOverlay() {
  _processingClosePending = true;
  // If the minimum visible time has already elapsed, close on the next tick.
  // Otherwise the verb cycler finishes the wait itself.
  const elapsed = performance.now() - _processingOpenedAt;
  if (elapsed >= PROCESSING_MIN_VISIBLE_MS) {
    if (_verbTimer) clearInterval(_verbTimer);
    _verbTimer = null;
    setTimeout(_finishCloseProcessing, 380);
  }
}

function _finishCloseProcessing() {
  const el = $("processing"); if (!el) return;
  set($("processing-bar"), "style", "width: 100%");
  set($("pv-verb"),   "textContent", "Done");
  set($("pv-detail"), "textContent", "Opening report…");
  setTimeout(() => cls(el, "add", "hidden"), 600);
}

// Pick a different random pair from the pool so consecutive ticks never
// repeat the same line (avoids the "frozen" feel). Restart the CSS fade-in
// animation on each cycle by removing + re-adding the trigger class on the
// next frame — otherwise CSS only animates on the initial mount.
let _verbLastIdx = -1;
function _cycleVerb() {
  if (PROCESSING_VERB_POOL.length < 2) return;
  let idx;
  do { idx = Math.floor(Math.random() * PROCESSING_VERB_POOL.length); }
  while (idx === _verbLastIdx);
  _verbLastIdx = idx;
  const [verb, detail] = PROCESSING_VERB_POOL[idx];
  const v = $("pv-verb"), d = $("pv-detail");
  if (v) {
    v.textContent = verb;
    v.classList.remove("pv-anim");
    void v.offsetWidth;            // force reflow → restart animation
    v.classList.add("pv-anim");
  }
  if (d) {
    d.textContent = detail;
    d.classList.remove("pv-anim");
    void d.offsetWidth;
    d.classList.add("pv-anim");
  }
}
// "Edit first" — reveal the textarea so the user can override the plan before
// approving. Re-clicking the button collapses the textarea again.
function rejectPlan() {
  if (!planEl) return;
  const hidden = planEl.classList.toggle("plan-editable--hidden");
  const hint = $("plan-hint");
  if (hint) hint.classList.toggle("plan-hint--hidden", hidden);
  const btn = $("reject-btn");
  if (btn) btn.textContent = hidden ? "Edit first" : "Hide editor";
  if (!hidden) {
    planEl.focus();
    planEl.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// ===================== Heatmaps + viewer =====================

// Called every time the Results tab is entered.  Fetches heatmap data if it
// isn't in memory yet, then unconditionally boots the three mini-viewers.
// This is the single point of truth for results-page viewer initialisation.
async function _renderResultsViewers() {
  console.log("[3D-DEBUG] _renderResultsViewers called | caseId:", caseId, "| heatmapState:", !!heatmapState, "| lastMeshUrl:", lastMeshUrl);
  if (!caseId) { console.warn("[3D-DEBUG] ABORT: no caseId"); return; }
  if (!heatmapState) {
    console.log("[3D-DEBUG] heatmapState is null — fetching /heatmaps …");
    try {
      const p = await http(`/cases/${caseId}/heatmaps`);
      console.log("[3D-DEBUG] /heatmaps response:", p, "| scenes:", p?.scenes?.length);
      if (p?.scenes?.length) heatmapState = p;
      else console.warn("[3D-DEBUG] /heatmaps returned no scenes — nothing to render");
    } catch (err) { console.error("[3D-DEBUG] /heatmaps fetch threw:", err); }
  }
  if (heatmapState) {
    console.log("[3D-DEBUG] calling renderHeatmapIntoResults …");
    renderHeatmapIntoResults();
  } else if (lastMeshUrl) {
    console.log("[3D-DEBUG] no heatmapState, falling back to bare mesh strip");
    populateBareMeshIntoStrip("results", RESULTS_CELLS());
  } else {
    console.warn("[3D-DEBUG] NOTHING to render — both heatmapState and lastMeshUrl are empty");
  }
}

function _b64ToBlob(b64, mime) {
  const bytes = atob(b64);
  const arr = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

async function loadHeatmaps() {
  if (!caseId) return;
  try {
    const p = await http(`/cases/${caseId}/heatmaps`);
    if (!p?.scenes?.length) { return; }
    heatmapState = p;
    // Extract carton heatmap data when present — create a blob URL so
    // _populateMiniStrip() can load it exactly like a product mesh GLB.
    if (p.carton_scenes?.length && p.carton_glb_b64) {
      try {
        const blob = _b64ToBlob(p.carton_glb_b64, "model/gltf-binary");
        const blobUrl = URL.createObjectURL(blob);
        cartonHeatmapState = { scenes: p.carton_scenes, glb_url: blobUrl, colormap: p.colormap };
      } catch (_) { cartonHeatmapState = null; }
    } else {
      cartonHeatmapState = null;
    }
    renderHeatmapIntoGeometry();
    renderHeatmapIntoResults();
    renderHeatmapIntoTransit();
    // Populate carton viewers if the Results tab is already visible.
    after2Frames(renderHeatmapIntoSecondaryPkg);
  } catch (e) {}
}

function _scenesAndTabs(p) {
  const friendly = {
    drop_top: "Drop · Top", drop_bottom: "Drop · Bottom",
    drop_side: "Drop · Side", transit: `Transit (${p.stacking_orientation || "upright"})`,
  };
  return p.scenes.map(sc => ({ ...sc, label: friendly[sc.scenario] || sc.scenario }));
}

async function renderHeatmapIntoGeometry() {
  if (!heatmapState) return;
  cls($("viewer-card"), "remove", "hidden");
  if (!geomViewerInited) { initViewer($("viewer")); geomViewerInited = true; }
  paintColorbar($("cb-canvas"), heatmapState.colormap?.lut);
  cls($("colorbar"), "remove", "hidden");
  if (heatmapState.glb_url) await loadGlb(heatmapState.glb_url);
  const tabs = $("scene-tabs");
  set(tabs, "innerHTML", "");
  for (const sc of _scenesAndTabs(heatmapState)) {
    const b = document.createElement("button");
    b.className = "scene-tab"; b.textContent = sc.label; b.dataset.scenario = sc.scenario;
    b.addEventListener("click", () => activateGeometryScene(sc.scenario));
    tabs.appendChild(b);
  }
  cls(tabs, "remove", "hidden");
  activateGeometryScene(heatmapState.scenes[0].scenario);
  set($("viewer-status"), "textContent",
    heatmapState.is_proxy ? "Demo proxy geometry" : "Mesh loaded");
}
function activateGeometryScene(scenario) {
  const sc = heatmapState?.scenes.find(s => s.scenario === scenario);
  if (!sc?.per_vertex_color) return;
  $("scene-tabs").querySelectorAll(".scene-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.scenario === scenario));
  applyVertexColors(sc.per_vertex_color);
  set($("scene-summary"), "textContent", sc.summary || "");
}

// Results stage uses 3 side-by-side mini viewers (top / bottom / side)
// driven by makeMiniViewer. Transit gets its own viewer on the Transit stage.
async function renderHeatmapIntoResults() {
  console.log("[3D-DEBUG] renderHeatmapIntoResults | heatmapState:", !!heatmapState);
  if (!heatmapState) { console.warn("[3D-DEBUG] ABORT: heatmapState null"); return; }
  paintColorbar($("vs-cb-canvas"), heatmapState.colormap?.lut);
  await _populateMiniStrip("results", heatmapState, [
    ["top",    "drop_top",    $("mini-viewer-top"),    $("vs-sub-top")],
    ["bottom", "drop_bottom", $("mini-viewer-bottom"), $("vs-sub-bottom")],
    ["side",   "drop_side",   $("mini-viewer-side"),   $("vs-sub-side")],
  ]);
  // Sync Level 3 damage viewers (same scenes, no sub override — captions stay as product risks)
  if ($("dmg-prod-viewer-top")) {
    await _populateMiniStrip("dmg-prod", heatmapState, [
      ["top",    "drop_top",    $("dmg-prod-viewer-top"),    null],
      ["bottom", "drop_bottom", $("dmg-prod-viewer-bottom"), null],
      ["side",   "drop_side",   $("dmg-prod-viewer-side"),   null],
    ]);
  }
}

// Transit stage carries a single dedicated viewer that paints the
// vibration / transit stress scene. Keeps the route-planning page focused
// on the one scenario that actually relates to transit.
async function renderHeatmapIntoTransit() {
  if (!heatmapState) return;
  paintColorbar($("transit-cb-canvas"), heatmapState.colormap?.lut);
  await _populateMiniStrip("transit", heatmapState, [
    ["transit", "transit", $("transit-viewer-3d"), $("transit-vs-sub")],
  ]);
}

// Secondary packaging (carton) heatmap viewers — 3 engineering scenarios.
// Uses the same _populateMiniStrip / makeMiniViewer pipeline as product
// viewers; cartonHeatmapState holds the blob-URL GLB + per-vertex colors.
// Falls back gracefully (shows fallback imgs) when cartonHeatmapState is null.
async function renderHeatmapIntoSecondaryPkg() {
  if (!cartonHeatmapState) return;
  const slots = [
    ["top",    "carton_top_load",    "sec-carton-viewer-top",    "sec-carton-img-top",    "sec-pkg-sub-top"],
    ["corner", "carton_corner_crush","sec-carton-viewer-corner", "sec-carton-img-corner", "sec-pkg-sub-bottom"],
    ["side",   "carton_side_wall",   "sec-carton-viewer-side",   "sec-carton-img-side",   "sec-pkg-sub-side"],
  ];
  // Show live viewer divs and hide static fallback images.
  for (const [, , canvasId, imgId] of slots) {
    const canvas = $(canvasId); const img = $(imgId);
    if (canvas) canvas.style.display = "";
    if (img)    img.style.display    = "none";
  }
  try {
    await _populateMiniStrip("sec-carton", cartonHeatmapState, slots.map(
      ([tag, scenario, canvasId, , subId]) => [tag, scenario, $(canvasId), $(subId)]
    ));
  } catch (e) {
    // GLB load or render failed — restore fallback images.
    console.warn("[CARTON] heatmap render failed; restoring fallback images:", e?.message || e);
    for (const [, , canvasId, imgId] of slots) {
      const canvas = $(canvasId); const img = $(imgId);
      if (canvas) canvas.style.display = "none";
      if (img)    img.style.display    = "";
    }
  }
}

async function _populateMiniStrip(prefix, hmState, cells) {
  console.log("[3D-DEBUG] _populateMiniStrip prefix:", prefix, "| scenes:", hmState?.scenes?.length, "| glb_url:", hmState?.glb_url);
  for (const [tag, scenario, container, sub] of cells) {
    console.log(`[3D-DEBUG]  cell ${tag}: container=`, container, "| offsetW:", container?.offsetWidth, "| offsetH:", container?.offsetHeight);
    if (!container) { console.warn(`[3D-DEBUG]  SKIP ${tag}: container is null`); continue; }
    if (!container.offsetWidth || !container.offsetHeight) continue;
    const key = `${prefix}-${tag}`;
    if (!miniViewers[key]) {
      console.log(`[3D-DEBUG]  creating NEW makeMiniViewer for ${key}`);
      miniViewers[key] = makeMiniViewer(container, { autoRotate: true });
    } else {
      console.log(`[3D-DEBUG]  viewer ${key} already exists — resizing`);
      miniViewers[key].resize();        // pick up any size changes
    }
    const sc = hmState.scenes.find(s => s.scenario === scenario);
    if (!sc) { console.warn(`[3D-DEBUG]  SKIP ${tag}: no scene for scenario "${scenario}"`); continue; }
    console.log(`[3D-DEBUG]  loading GLB for ${key} | url:`, hmState.glb_url, "| vertex colors:", sc.per_vertex_color?.length);
    if (hmState.glb_url) await miniViewers[key].loadGlb(hmState.glb_url);
    miniViewers[key].applyVertexColors(sc.per_vertex_color);
    if (sub) sub.textContent = sc.summary || "";
    // Paint the matching 2D unwrap underneath. ID convention:
    //   results-top  → mini-2d-top, signoff-top → signoff-2d-top, etc.
    //   variant-{idx}-top → variant-2d-top (per-page singleton)
    const projTarget = _twoDeeTargetId(prefix, tag);
    const projCanvas = projTarget ? $(projTarget) : null;
    // if (projCanvas) paint2DProjection(projCanvas, sc.per_vertex_color, sc.summary);
  }
}
function _twoDeeTargetId(prefix, tag) {
  if (prefix === "results")           return `mini-2d-${tag}`;
  if (prefix === "signoff")           return `signoff-2d-${tag}`;
  if (prefix.startsWith("variant-"))  return `variant-2d-${tag}`;
  return null;
}

// Load the user's uploaded mesh into a strip of mini-viewers WITHOUT any
// per-vertex stress colors. Used so the 3D model is visible on Results /
// Sign-off / Variant tabs as soon as the user uploads it — before the
// analysis has run and produced a heatmap. Once analysis runs,
// _populateMiniStrip swaps in the colored heatmap version.
async function populateBareMeshIntoStrip(prefix, cells) {
  if (!lastMeshUrl) return;
  for (const [tag, _scenario, container, _sub] of cells) {
    if (!container) continue;
    if (!container.offsetWidth || !container.offsetHeight) continue;
    const key = `${prefix}-${tag}`;
    if (!miniViewers[key]) {
      miniViewers[key] = makeMiniViewer(container, { autoRotate: true });
    } else {
      miniViewers[key].resize();
    }
    try {
      await miniViewers[key].loadGlb(lastMeshUrl);
    } catch (_) { /* mesh may have been deleted server-side; skip */ }
    // Clear the 2D companion so a stale heatmap doesn't linger.
    const projTarget = _twoDeeTargetId(prefix, tag);
    const projCanvas = projTarget ? $(projTarget) : null;
    if (projCanvas) {
      const ctx = projCanvas.getContext("2d");
      if (ctx) ctx.clearRect(0, 0, projCanvas.width, projCanvas.height);
    }
  }
}

// Public-ish wrappers — one per stage, so the showStage handler stays terse.
// Results carries the 3 drop orientations. The transit scenario gets its own
// dedicated viewer on the Transit stage instead, so each stage stays focused.
const RESULTS_CELLS = () => ([
  ["top",     "drop_top",    $("mini-viewer-top"),     $("vs-sub-top")],
  ["bottom",  "drop_bottom", $("mini-viewer-bottom"),  $("vs-sub-bottom")],
  ["side",    "drop_side",   $("mini-viewer-side"),    $("vs-sub-side")],
]);
const TRANSIT_CELLS = () => ([
  ["transit", "transit",     $("transit-viewer-3d"),   $("transit-vs-sub")],
]);
const SIGNOFF_CELLS = () => ([
  ["top",    "drop_top",    $("signoff-viewer-top"),    null],
  ["bottom", "drop_bottom", $("signoff-viewer-bottom"), null],
  ["side",   "drop_side",   $("signoff-viewer-side"),   null],
]);
const VARIANT_CELLS = () => ([
  ["top",    "drop_top",    $("variant-viewer-top"),    null],
  ["bottom", "drop_bottom", $("variant-viewer-bottom"), null],
  ["side",   "drop_side",   $("variant-viewer-side"),   null],
]);

// 2D projection — flatten the 3D per-vertex color buffer into a wide image.
// We tile the colors row-major into roughly the canvas's aspect ratio, then
// scale the resulting image with image-rendering: pixelated to keep the
// color blocks crisp. Adds a peak marker at the highest-stress vertex.
function paint2DProjection(canvas, perVertexColor, summary) {
  if (!canvas || !Array.isArray(perVertexColor) || !perVertexColor.length) return;
  const dpr = window.devicePixelRatio || 1;
  const cssRect = canvas.getBoundingClientRect();
  const W = Math.max(60, Math.round(cssRect.width));
  const H = Math.max(40, Math.round(cssRect.height));
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.imageSmoothingEnabled = false;
  // Tile colors into a small offscreen image, then scale up.
  const N = perVertexColor.length;
  // Aim for ~3:1 aspect tiles — wider than tall to match a developed bottle.
  const tw = Math.max(8, Math.round(Math.sqrt(N * 3)));
  const th = Math.max(4, Math.ceil(N / tw));
  const off = document.createElement("canvas");
  off.width = tw; off.height = th;
  const offCtx = off.getContext("2d");
  const img = offCtx.createImageData(tw, th);
  let peakIdx = 0, peakHeat = -1;
  for (let i = 0; i < tw * th; i++) {
    const c = perVertexColor[i % N] || [200, 200, 200];
    img.data[i*4 + 0] = c[0];
    img.data[i*4 + 1] = c[1];
    img.data[i*4 + 2] = c[2];
    img.data[i*4 + 3] = 255;
    // Heat = R - B in jet → red = hot, blue = cold.
    const heat = (c[0] - c[2]);
    if (heat > peakHeat) { peakHeat = heat; peakIdx = i; }
  }
  offCtx.putImageData(img, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.drawImage(off, 0, 0, tw, th, 0, 0, W, H);
  // Peak marker
  const px = (peakIdx % tw) / tw * W;
  const py = Math.floor(peakIdx / tw) / th * H;
  ctx.strokeStyle = "rgba(0,0,0,0.65)"; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(px + W / (tw * 2), py + H / (th * 2), 5, 0, 2 * Math.PI);
  ctx.stroke();
  ctx.fillStyle = "rgba(255,255,255,0.85)";
  ctx.beginPath(); ctx.arc(px + W / (tw * 2), py + H / (th * 2), 2.2, 0, 2 * Math.PI);
  ctx.fill();
  // Subtle caption
  if (summary) {
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.font = "10px 'IBM Plex Mono', monospace";
    ctx.textAlign = "right"; ctx.textBaseline = "bottom";
    ctx.fillText("peak ●", W - 6, H - 4);
  }
}

// Sign-off mini viewers (locked geometry under each drop scenario)
async function renderHeatmapIntoSignoff() {
  if (!heatmapState) return;
  await _populateMiniStrip("signoff", heatmapState, [
    ["top", "drop_top", $("signoff-viewer-top"), null],
    ["bottom", "drop_bottom", $("signoff-viewer-bottom"), null],
    ["side", "drop_side", $("signoff-viewer-side"), null],
  ]);
}

// ===================== Geometry upload (with drag + drop) =====================
async function uploadFile(file, { demo = false } = {}) {
  if (!caseId) await newCase({ resetSelection: false });
  appendMsg("system", `Uploading ${file.name}${demo ? " (demo mode)" : ""}…`);
  progressBegin("Parsing geometry", 3);
  try {
    const fd = new FormData();
    fd.append("file", file);
    if (selectedPackagingFamily) fd.append("packaging_family", selectedPackagingFamily);
    const url = `${API}/cases/${caseId}/upload${demo ? "?demo=true" : ""}`;
    const r = await fetch(url, { method: "POST", body: fd });
    if (!r.ok) {
      let detail; try { detail = (await r.json()).detail; } catch (_) {}
      showUploadError(file, detail || { reason: `${r.status} ${r.statusText}` });
      return;
    }
    progressTick("Mesh parsed");
    const body = await r.json();
    appendMsg("system", body.is_proxy
      ? "⚠️ Demo proxy in use — every downstream result is labelled approximate."
      : `Geometry parsed (${body.summary.file_type}). Bounding box ready.`);
    cls($("dropzone"), "add", "hidden");
    cls($("viewer-card"), "remove", "hidden");
    if (!geomViewerInited) { initViewer($("viewer")); geomViewerInited = true; }
    lastMeshUrl = `${API}/cases/${caseId}/mesh?t=${Date.now()}`;
    await loadGlb(lastMeshUrl);
    set($("viewer-status"), "textContent", body.is_proxy ? "Demo proxy geometry" : "Mesh loaded");
    progressTick();
    // The mesh should appear in every visualisation tab right away — not
    // wait for analysis. We seed all three strips bare; if heatmapState
    // arrives later, _populateMiniStrip overrides with the colored version.
    await Promise.allSettled([
      populateBareMeshIntoStrip("results", RESULTS_CELLS()),
      populateBareMeshIntoStrip("signoff", SIGNOFF_CELLS()),
    ]);
    // Show the auto-advance reply (first flow question) from the backend.
    const advance = body.advance;
    if (advance?.routing_conflict) {
      const suggested = advance.suggested_family;
      const current = advance.current_family;
      const conflictDiv = document.createElement("div");
      conflictDiv.className = "msg assistant";
      conflictDiv.innerHTML = marked.parse(advance.reply || "") +
        `<div class="routing-conflict-btns">
          <button class="primary rc-switch" data-family="${escapeHtml(suggested)}">
            Switch to ${suggested === "bottle" ? "Bottle" : "Flexible Packet"} workflow
          </button>
          <button class="secondary rc-keep">
            Keep ${current === "bottle" ? "Bottle" : "Flexible Packet"} workflow
          </button>
        </div>`;
      messagesEl.appendChild(conflictDiv);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      conflictDiv.querySelector(".rc-switch").addEventListener("click", async () => {
        conflictDiv.remove();
        selectedPackagingFamily = suggested;
        document.querySelectorAll(".pkg-card").forEach(c =>
          c.classList.toggle("selected", c.dataset.family === suggested));
        try {
          await http(`/cases/${caseId}/brief`, {
            method: "PATCH",
            body: JSON.stringify({ updates: { packaging_family: suggested } }),
          });
          const fr = await http(`/cases/${caseId}/enter-flow`, { method: "POST" });
          if (fr?.reply) appendMsg("assistant", fr.reply);
        } catch (e) { appendMsg("system", "Could not enter flow: " + e.message); }
      });
      conflictDiv.querySelector(".rc-keep").addEventListener("click", async () => {
        conflictDiv.remove();
        try {
          const fr = await http(`/cases/${caseId}/enter-flow`, { method: "POST" });
          if (fr?.reply) appendMsg("assistant", fr.reply);
        } catch (e) { appendMsg("system", "Could not enter flow: " + e.message); }
      });
    } else if (advance?.reply) {
      appendMsg("assistant", advance.reply);
      if (advance.options && Array.isArray(advance.options) && advance.asking_field) {
        if (advance.active_flow === "bottle_flow") renderBottleChips(advance.options);
        else if (advance.active_flow === "packet_flow") renderPacketChips(advance.options);
        else if (advance.active_flow === "brush_flow") renderBrushChips(advance.options);
      }
    }
    await refreshBrief();
    updateStageRail();
  } finally { progressEnd(); }
}
// Restore the 3D viewer with the case's previously-uploaded geometry.
// Called on entering the Geometry stage AND after case-switch / page reload
// so navigation never wipes the mesh from the viewport.
async function restoreGeometryIfPresent() {
  if (!caseId) return;
  // Already showing? Nothing to do.
  if ($("viewer-card") && !$("viewer-card").classList.contains("hidden")) return;
  // Fast-path: snapshot already cached locally from a Save Design call.
  let hasMesh = false;
  try {
    const cached = JSON.parse(localStorage.getItem(`de.geom.${caseId}`) || "null");
    if (cached?.asset_id) hasMesh = true;
  } catch (_) {}
  // Slow-path: ask the server for the brief — geometry presence lives there.
  if (!hasMesh) {
    try {
      const b = await http(`/cases/${caseId}/brief`);
      hasMesh = !!(b?.geometry_asset_id || b?.case_summary?.has_geometry);
    } catch (_) { return; }
  }
  if (!hasMesh) return;
  cls($("dropzone"), "add", "hidden");
  cls($("viewer-card"), "remove", "hidden");
  if (!geomViewerInited) { initViewer($("viewer")); geomViewerInited = true; }
  lastMeshUrl = `${API}/cases/${caseId}/mesh?t=${Date.now()}`;
  try {
    await loadGlb(lastMeshUrl);
    set($("viewer-status"), "textContent", "Mesh restored from this design");
  } catch (e) {
    // If the server can't return the mesh, fall back to the dropzone — the
    // user can re-upload. Don't silently strand them in a broken viewer.
    cls($("viewer-card"), "add", "hidden");
    cls($("dropzone"), "remove", "hidden");
    lastMeshUrl = null;
    return;
  }
  // Seed the other tabs with the bare mesh so the user sees their model
  // everywhere as soon as the design loads.
  await Promise.allSettled([
    populateBareMeshIntoStrip("results", RESULTS_CELLS()),
    populateBareMeshIntoStrip("signoff", SIGNOFF_CELLS()),
  ]);
}

function showUploadError(file, detail) {
  const banner = document.createElement("div");
  banner.className = "plate";
  banner.style.borderColor = "var(--fail)";
  banner.style.marginTop = "16px";
  banner.innerHTML = `
    <span class="eyebrow" style="color:var(--fail)">Geometry parse failed</span>
    <h3 style="margin: 8px 0">${escapeHtml(file.name)}</h3>
    <p>${escapeHtml(detail.reason || "unknown")}</p>
    ${detail.hint ? `<p style="color:var(--ink-mute)"><em>${escapeHtml(detail.hint)}</em></p>` : ""}
    <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
      <button class="primary" id="retry-upload">Upload a different file</button>
      <a href="#" id="retry-demo" style="font-size:12px;color:var(--ink-mute)">Continue with reference geometry</a>
    </div>`;
  $("dropzone").after(banner);
  banner.querySelector("#retry-upload").addEventListener("click", () => fileEl.click());
  banner.querySelector("#retry-demo").addEventListener("click", async e => {
    e.preventDefault();
    banner.remove();
    await uploadFile(file, { demo: true });
  });
}

// ===================== Brief sidecar =====================
async function refreshBrief() {
  if (!caseId) return;
  try {
    const b = await http(`/cases/${caseId}/brief`);
    set(topbarName, "textContent", b.design_name || "Untitled design");
    renderBrief(b.case_summary || {});
    // Always sync selectedPackagingFamily from the persisted brief — this is
    // the source of truth on session restore, page reload, and cross-stage nav.
    // Check packaging_family first; fall back to packaging_type (set by chat
    // intake) so users who bypassed the landing card are still routed correctly.
    const cs = b.case_summary || {};
    const briefFamily = cs.packaging_family || (() => {
      const pt = (cs.packaging_type || "").toLowerCase();
      if (_PACKET_TYPES.has(pt)) return "packet";
      if (pt === "bottle" || pt.includes("bottle")) return "bottle";
      if (pt === "brush") return "brush";
      return null;
    })();
    if (briefFamily) {
      selectedPackagingFamily = briefFamily;
      document.querySelectorAll(".pkg-card").forEach(c =>
        c.classList.toggle("selected", c.dataset.family === briefFamily));
    }
    // Re-apply optimise UI if the user is already on that stage (e.g. navigated
    // there before this async call completed).
    if (currentRoute() === "optimise") {
      _applyOptimiseUiForFamily(briefFamily || selectedPackagingFamily);
    }
  } catch (_) {}
}

// Returns the effective packaging family — selectedPackagingFamily is the fast
// path; falls back to whatever the brief or snapshot carries so that
// the packet/bottle routing never silently defaults to bottle when the state
// variable hasn't been set yet (e.g. very-early navigation before refreshBrief).
//
// Two separate fields drive this:
//   packaging_family — set by the landing-page Packet/Bottle card selector
//   packaging_type   — set by the conversational intake (e.g. "pouch", "sachet")
// A user who started by typing in chat may have packaging_type but not
// packaging_family. We must honour both.
const _PACKET_TYPES = new Set([
  "pouch","packet","sachet","standup_pouch","centre_seal_pouch",
  "center_seal_pouch","flow_wrap","flexible","flexible_packaging",
  "pillow_pouch","gusset_pouch","quad_seal","vacuum_pack",
]);
function _effectiveFamily() {
  if (selectedPackagingFamily) return selectedPackagingFamily;
  const cs = lastSnapshot?.case_summary || {};
  // Explicit packaging_family wins first.
  if (cs.packaging_family) return cs.packaging_family;
  // Fall back to packaging_type (set by the chat intake flow).
  const pt = (cs.packaging_type || "").toLowerCase();
  if (pt && _PACKET_TYPES.has(pt)) return "packet";
  if (pt && (pt === "bottle" || pt.includes("bottle"))) return "bottle";
  if (pt && pt === "brush") return "brush";
  return null;
}
function renderBrief(cs) {
  const rows = [];
  if (cs.packaging_type) {
    rows.push(brRow("Packaging",
      `${cs.packaging_type}${cs.bottle_subtype ? ` (${cs.bottle_subtype})` : ""}`));
  }
  if (cs.capacity_ml) rows.push(brRow("Capacity", `${cs.capacity_ml} ml`));
  if (cs.material) {
    const extra = [
      cs.wall_thickness_mm ? `${cs.wall_thickness_mm} mm` : null,
      cs.gross_weight_g ? `${cs.gross_weight_g} g` : null,
    ].filter(Boolean).join(" · ");
    rows.push(brRow("Material", cs.material, extra));
  }
  if (cs.product_type) {
    rows.push(brRow("Product",
      `${cs.product_type}${cs.fill_level_pct ? ` · ${cs.fill_level_pct}%` : ""}`));
  }
  if (cs.transit_modes?.length) {
    rows.push(brRow("Transit", cs.transit_modes.join(" · "),
      cs.road_condition ? cs.road_condition.replace(/_/g, " ") : null));
  }
  if (cs.stack_height || cs.stacking_orientation) {
    rows.push(brRow("Stack",
      `${cs.stack_height || 4}× ${(cs.stacking_orientation || "upright").replace(/_/g, " ")}`));
  }
  if (cs.objective) rows.push(brRow("Goal", cs.objective.replace(/_/g, " ")));

  // Brief bar removed; this stays as a no-op so existing call sites are safe.
  if (briefBody) {
    const html = rows.length
      ? rows.join("")
      : `<div class="empty-state" style="padding:8px 0">No fields captured yet — chat to start.</div>`;
    set(briefBody, "innerHTML", html);
  }
}
function brRow(k, v, extra) {
  return `<div class="brief-row">
    <span class="br-k">${escapeHtml(k)}</span>
    <span class="br-v">${escapeHtml(v)}</span>
    ${extra ? `<span class="br-extra">${escapeHtml(extra)}</span>` : ""}
  </div>`;
}

// ===================== Material stage =====================
function renderMaterialStage() {
  const m = lastSnapshot?.material;
  const cs = lastSnapshot?.report?.case_summary || {};
  if (m) {
    set($("material-body"), "innerHTML", `
      <div class="prop-row"><span class="pk">Name</span><span class="pv">${escapeHtml(m.name)}</span></div>
      <div class="prop-row"><span class="pk">Density</span><span class="pv">${m.density_kg_m3 ?? "—"} kg/m³</span></div>
      <div class="prop-row"><span class="pk">Modulus</span><span class="pv">${m.modulus_gpa ?? "—"} GPa</span></div>
      <div class="prop-row"><span class="pk">Yield</span><span class="pv">${m.yield_strength_mpa ?? "—"} MPa</span></div>
      <div class="prop-row"><span class="pk">Allowable</span><span class="pv">${m.allowable_stress_mpa ?? "—"} MPa</span></div>
      <div class="prop-source">
        <span>${escapeHtml(m.source)}</span>
        <span class="ps-tag ${m.confidence}">${m.confidence}</span>
      </div>`);
  } else {
    set($("material-body"), "innerHTML", `<div class="empty-state">Set a material in chat first.</div>`);
  }
  // Product
  const prod = cs.product_type;
  if (prod) {
    set($("product-body"), "innerHTML", `
      <div class="prop-row"><span class="pk">Type</span><span class="pv">${escapeHtml(prod)}</span></div>
      ${cs.fill_level_pct ? `<div class="prop-row"><span class="pk">Fill</span><span class="pv">${cs.fill_level_pct}%</span></div>` : ""}
      <div class="prop-source">
        <span>derived from intake</span>
        <span class="ps-tag estimated">estimated</span>
      </div>`);
  } else {
    set($("product-body"), "innerHTML", `<div class="empty-state">Tell us what's inside.</div>`);
  }
}

// ===================== Transit stage (mode mix + envelope preview) =====================
const MODES = ["truck", "pickup", "ship", "air", "rail", "manual_handling"];
const transitState = {
  mode_mix: { truck: 50, ship: 30, air: 20, pickup: 0, rail: 0, manual_handling: 0 },
  road_condition: "mixed",
  ship_severity: "moderate",
  stacking_orientation: "upright",
  stack_height: 4,
  ships_loose: false,
  durations_min: {},
  manual_drop_height_m: 1.0,
};
// Show/hide the manual-drop-height row based on whether any manual-handling
// share is present. Single source of truth for the toggle (called from each
// site that previously duplicated the expression).
function syncManualDropRow() {
  const r = document.getElementById("manual-drop-row");
  if (r) r.hidden = !((transitState.mode_mix.manual_handling || 0) > 0);
}
async function renderTransitStage() {
  // Modes split into two tiers: `dataBacked` have real CSV telemetry (truck,
  // pickup, ship); `selectable` additionally includes reference modes (air,
  // rail, manual_handling) which are interactive but use industry estimates.
  let dataBacked = ["truck", "pickup", "ship"];
  let selectable = MODES.slice();
  let reference = ["air", "rail", "manual_handling"];
  try {
    const r = await http("/transit/available-modes");
    if (Array.isArray(r.data_backed) && r.data_backed.length) dataBacked = r.data_backed;
    if (Array.isArray(r.selectable) && r.selectable.length) selectable = r.selectable;
    if (Array.isArray(r.reference)) reference = r.reference;
  } catch (_) {}
  const selectableSet = new Set(selectable);
  const referenceSet = new Set(reference);
  set($("modes-help"), "textContent",
    `Modes with real telemetry data: ${dataBacked.join(", ")}. Reference modes (${reference.join(", ")}) are selectable but use industry estimates.`);

  const row = $("mode-row");
  set(row, "innerHTML", MODES.map(m => {
    const enabled = selectableSet.has(m);
    const isRef = referenceSet.has(m);
    const greyed = !enabled || isRef;
    const badge = isRef ? " (estimate)" : (!enabled ? " (no data)" : "");
    return `
      <div class="mode-line" data-mode="${m}" ${greyed ? 'style="opacity:0.4"' : ""}>
        <label>${m.replace(/_/g, " ")}${badge}</label>
        <input type="range" min="0" max="100" value="${transitState.mode_mix[m] || 0}" ${enabled ? "" : "disabled"}/>
        <span class="mode-pct">${transitState.mode_mix[m] || 0}%</span>
      </div>`;
  }).join(""));
  row.querySelectorAll("input[type=range]").forEach(i => {
    i.addEventListener("input", e => {
      const line = e.target.closest(".mode-line");
      const m = line.dataset.mode;
      transitState.mode_mix[m] = parseInt(e.target.value, 10);
      line.querySelector(".mode-pct").textContent = transitState.mode_mix[m] + "%";
      syncManualDropRow();
      previewEnvelope();
      pushTransitToBrief();
    });
  });
  // Segmented controls
  ["road-control","ship-control","stack-orient"].forEach(id => {
    const ctrl = $(id);
    const opts = ctrl.dataset.options.split(",");
    set(ctrl, "innerHTML", opts.map(o =>
      `<button data-v="${o}" class="${transitState[ctrl.dataset.name] === o ? "active" : ""}">${o.replace(/_/g, " ")}</button>`).join(""));
    ctrl.querySelectorAll("button").forEach(b => {
      b.addEventListener("click", () => {
        const v = b.dataset.v;
        transitState[ctrl.dataset.name] = v;
        ctrl.querySelectorAll("button").forEach(x => x.classList.toggle("active", x.dataset.v === v));
        if (id !== "stack-orient") previewEnvelope();
        pushTransitToBrief();
      });
    });
  });
  $("stack-height").value = transitState.stack_height;
  $("stack-height").oninput = e => {
    transitState.stack_height = parseInt(e.target.value, 10) || 4;
    pushTransitToBrief();
  };
  $("ships-loose").checked = transitState.ships_loose;
  $("ships-loose").onchange = e => {
    transitState.ships_loose = e.target.checked;
    pushTransitToBrief();
  };
  // Duration presets + manual drop-height controls
  ["transit-truck-dur", "transit-other-dur", "transit-drop-h"].forEach(id => {
    const el = $(id);
    if (!el) return;
    el.onchange = () => { previewEnvelope(); pushTransitToBrief(); };
  });
  syncManualDropRow();
  previewEnvelope();
}

// Read duration presets + manual drop height from the Transit controls.
function _transitDurationsAndDrop() {
  const truckDurEl = document.getElementById("transit-truck-dur");
  const otherDurEl = document.getElementById("transit-other-dur");
  const dropHEl = document.getElementById("transit-drop-h");
  const truckDur = truckDurEl ? Number(truckDurEl.value) : 480;
  const otherHrs = otherDurEl ? Number(otherDurEl.value || 0) : 0;
  // Truck/pickup always have a value from the truck-duration select. Only
  // include air/rail/ship when the user actually entered a duration > 0 —
  // otherwise OMIT them so the backend's per-mode defaults (blended_envelope
  // only falls back when a key is ABSENT) stay in effect.
  const durations_min = { truck: truckDur, pickup: truckDur };
  if (otherHrs > 0) {
    durations_min.air = otherHrs * 60;
    durations_min.rail = otherHrs * 60;
    durations_min.ship = otherHrs * 60;
  }
  const manual_drop_height_m = dropHEl ? Number(dropHEl.value) : 1.0;
  // Mirror onto transitState so the fields stay authoritative and consistent
  // with how the rest of transitState tracks the controls.
  transitState.durations_min = durations_min;
  transitState.manual_drop_height_m = manual_drop_height_m;
  return { durations_min, manual_drop_height_m };
}

let envelopeTimer = null;
function previewEnvelope() {
  if (envelopeTimer) clearTimeout(envelopeTimer);
  envelopeTimer = setTimeout(_doPreview, 250);
}
async function _doPreview() {
  // Normalise
  const total = Object.values(transitState.mode_mix).reduce((a, b) => a + b, 0) || 1;
  const norm = Object.fromEntries(Object.entries(transitState.mode_mix).map(([k, v]) => [k, v / total]));
  // Refresh the time-series charts whenever the mix or severity changes
  refreshTransitCharts();
  try {
    // Use the orchestrator's transit_data via a small hand-rolled probe
    // (POSTing to /messages would re-run intake; instead read live envelope
    // via the brief PATCH which doesn't trigger analysis).
    const { durations_min, manual_drop_height_m } = _transitDurationsAndDrop();
    const env = await http(`/transit/preview`, {
      method: "POST",
      body: JSON.stringify({
        mode_mix: norm,
        road: transitState.road_condition,
        ship_severity: transitState.ship_severity,
        durations_min,
        manual_drop_height_m,
      }),
    }).catch(() => null);
    const body = $("envelope-body");
    if (!env) {
      set(body, "innerHTML", `<div class="empty-state">Envelope preview unavailable. Run analysis to see real numbers.</div>`);
      return;
    }
    const sources = (env.sources || []).map(s => `${s.mode} (${s.rows.toLocaleString()} rows)`).join(" · ");
    set(body, "innerHTML", `
      <div class="envelope-stat"><span class="es-k">Vibration</span><span class="es-v">${env.g_rms} g_rms</span></div>
      <div class="envelope-stat"><span class="es-k">Drop height</span><span class="es-v">${env.drop_height_m} m</span></div>
      <div class="envelope-stat"><span class="es-k">Handling</span><span class="es-v">${env.handling_fraction}</span></div>
      <div class="envelope-stat"><span class="es-k">Shock p95</span><span class="es-v">${env.shock_risk_p95}</span></div>
      <div class="envelope-stat"><span class="es-k">Dominant</span><span class="es-v">${env.dominant_modes.join(", ")}</span></div>
      <div class="envelope-prov">${escapeHtml(env.data_provenance || sources)}</div>
    `);
  } catch (e) {}
}

async function pushTransitToBrief() {
  if (!caseId) return;
  const total = Object.values(transitState.mode_mix).reduce((a, b) => a + b, 0) || 1;
  const active_modes = Object.entries(transitState.mode_mix)
    .filter(([_, v]) => v > 0).map(([k]) => k);
  const norm = Object.fromEntries(Object.entries(transitState.mode_mix)
    .filter(([_, v]) => v > 0).map(([k, v]) => [k, v / total]));
  const { durations_min, manual_drop_height_m } = _transitDurationsAndDrop();
  await http(`/cases/${caseId}/brief`, {
    method: "PATCH",
    body: JSON.stringify({ updates: {
      transit_modes: active_modes,
      transit_mode_mix: norm,
      road_condition: transitState.road_condition,
      ship_severity: transitState.ship_severity,
      stacking_orientation: transitState.stacking_orientation,
      stack_height: transitState.stack_height,
      ships_loose: transitState.ships_loose,
      transit_durations_min: durations_min,
      manual_drop_height_m: manual_drop_height_m,
    } }),
  });
  await refreshBrief();
}

// ===================== Results scorecard =====================
function renderScorecard() {
  const i = lastSnapshot?.ista2a;
  const card = $("scorecard");
  card.classList.remove("pass", "fail", "indet");
  if (!i) {
    set($("scorecard-body"), "innerHTML",
      `<div class="empty-state">Run the analysis to see a verdict.</div>`);
    return;
  }
  const overall = (i.overall_verdict || "").toLowerCase();
  card.classList.add(overall === "pass" ? "pass" : overall === "fail" ? "fail" : "indet");
  const headline = overall === "pass" ? "Pass." : overall === "fail" ? "Fail." : "Insufficient data.";
  const passingCount = i.drops.filter(d => d.verdict === "pass").length;
  const transitV = i.transit?.overall_transit_verdict || i.transit?.compression_verdict || "n/a";

  let body = `
    <span class="eyebrow scorecard-eyebrow">Overall verdict</span>
    <div class="scorecard-headline">${headline}</div>
    <div class="scorecard-meta">
      ${passingCount} of ${i.drops.length} drop orientations cleared.<br>
      Stack compression: ${transitV === "n/a" ? "not applicable (bottle ships in case)" : transitV}.
    </div>
    <div class="orientation-row">
  `;
  for (const d of i.drops) {
    const cls = d.verdict === "pass" ? "or-pass" : d.verdict === "fail" ? "or-fail" : "or-indet";
    body += `
      <div class="or-item ${cls}">
        <span class="or-name">Drop · ${d.orientation}</span>
        <div class="or-sf">${d.safety_factor ?? "—"}</div>
        <div class="or-data">
          <span>v</span><span>${d.impact_velocity_m_s} m/s</span>
          <span>σ</span><span>${d.impact_pressure_mpa} MPa</span>
          <span>K_t</span><span>${d.stress_concentration_kt}</span>
        </div>
        <span class="or-verdict">${(d.verdict || "").replace(/_/g, " ")}</span>
      </div>`;
  }
  body += `</div>`;
  set($("scorecard-body"), "innerHTML", body);
}

function renderFindings() {
  const i = lastSnapshot?.ista2a;
  const body = $("findings-body"); if (!body) return;
  if (!i) {
    set(body, "innerHTML", `<div class="empty-state">Findings appear once the analysis has run.</div>`);
    return;
  }
  const lines = [];
  for (const d of i.drops) {
    const cls = d.verdict === "fail" ? "fail" : d.verdict === "pass" ? "pass" : "";
    const where = d.orientation === "top"    ? "the closure / cap rim and the neck"
                : d.orientation === "bottom" ? "the base disc and base corners"
                : d.orientation === "side"   ? "the impact-side wall at mid-height"
                : "the corner vertex";
    const why = d.verdict === "fail"
      ? `applied σ ${d.impact_pressure_mpa} MPa exceeds σ_y ${d.allowable_mpa} MPa (SF ${d.safety_factor}, K_t ${d.stress_concentration_kt})`
      : `applied σ ${d.impact_pressure_mpa} MPa stays below σ_y ${d.allowable_mpa} MPa (SF ${d.safety_factor})`;
    lines.push(`
      <div class="finding ${cls}">
        <span class="f-label">${escapeHtml(d.orientation)}</span>
        <span class="f-text"><strong>Damage most likely at ${escapeHtml(where)}</strong> — ${escapeHtml(why)}.</span>
      </div>`);
  }
  if (i.transit?.compression_verdict === "fail") {
    lines.push(`
      <div class="finding fail">
        <span class="f-label">stack</span>
        <span class="f-text"><strong>Stack compression is the binding constraint.</strong>
        Bottom of the column sees ${i.transit.compression_load_n} N over the bottle footprint
        (SF ${i.transit.compression_safety_factor}, ≥1.5 required).</span>
      </div>`);
  } else if (i.transit?.compression_verdict === "n/a") {
    lines.push(`
      <div class="finding">
        <span class="f-label">stack</span>
        <span class="f-text">Stack compression is borne by the corrugated case ECT, not the bottle wall — not graded here.</span>
      </div>`);
  }
  set(body, "innerHTML", lines.join(""));
}

// ===================== ISTA 6A toggle =====================

async function runIsta6A() {
  if (!caseId) return;
  progressBegin("Running ISTA 6A corner drop", 1);
  try {
    const r = await http(`/cases/${caseId}/ista6a`, {
      method: "POST", body: JSON.stringify({}),
    });
    progressTick();
    const cls = r.overall_verdict === "pass" ? "pass" : r.overall_verdict === "fail" ? "fail" : "";
    set($("ista6a-result"), "innerHTML", `
      <h4>ISTA 6A · corner drop · ${r.drop_height_m * 1000} mm
        <span class="tag ${r.overall_verdict === 'pass' ? 'verified' : r.overall_verdict === 'fail' ? 'insufficient_data' : 'approximate'}">${r.overall_verdict}</span>
      </h4>
      <table>
        <tr><td>Weakest corner</td><td>${escapeHtml(r.weakest_corner)}</td></tr>
        <tr><td>Impact velocity</td><td>${r.impact_velocity_m_s} m/s</td></tr>
        <tr><td>Impact energy</td><td>${r.impact_energy_j} J</td></tr>
        <tr><td>Local σ</td><td>${r.impact_pressure_mpa} MPa over ${r.contact_area_mm2} mm² (K_t ${ISTA6A_KT_LABEL})</td></tr>
        <tr><td>Allowable σ_y</td><td>${r.allowable_mpa ?? "—"} MPa</td></tr>
        <tr><td>Safety factor</td><td>${r.safety_factor ?? "—"}</td></tr>
      </table>
      <div style="margin-top:8px;color:var(--ink-mute);font-size:12px;font-style:italic">${escapeHtml(r.rationale)}</div>
    `);
    const card = $("ista6a-result");
    card.classList.remove("pass", "fail");
    if (cls) card.classList.add(cls);
    cls(card, "remove", "hidden");
  } catch (e) {
    appendMsg("system", "ISTA 6A error: " + e.message);
  } finally { progressEnd(); }
}
const ISTA6A_KT_LABEL = "3.0";

// ===================== Transit time-series charts =====================

let lastTransitCharts = null;

async function refreshTransitCharts() {
  const modes = Object.entries(transitState.mode_mix).filter(([_, v]) => v > 0).map(([k]) => k);
  const wanted = modes.filter(m => ["truck", "ship"].includes(m)).join(",") || "truck";
  try {
    // Live Transit stage gets the full span (8 000 strided points); the chart
    // bins per pixel so that's still snappy.
    lastTransitCharts = await http(
      `/transit/charts?road=${encodeURIComponent(transitState.road_condition)}` +
      `&ship_severity=${encodeURIComponent(transitState.ship_severity)}` +
      `&modes=${encodeURIComponent(wanted)}` +
      `&max_points=8000`
    );
    drawTransitCharts(lastTransitCharts);
  } catch (e) {}
}

function drawTransitCharts(data) {
  if (!data) return;
  // Truck vibration line chart — full trip
  if (data.truck) {
    const span = data.truck.t_hours.length
      ? `${data.truck.t_hours[data.truck.t_hours.length - 1].toFixed(0)} h total · ${data.truck.n_rows_total?.toLocaleString() ?? "?"} samples`
      : "";
    drawLineChart($("chart-truck-vib"), {
      x: data.truck.t_hours, y: data.truck.vibration_g,
      color: "#0072bb", fill: "rgba(0, 114, 187, 0.10)",
      yLabel: "Vibration (g)", xLabel: `Elapsed time (hours · ${span})`,
      baseline: { y: 0.54, label: "ISTA truck PSD baseline" },
    });
    drawScatterChart($("chart-truck-shock"), {
      points: data.truck.shock_events, color: "#a83232",
      yLabel: "Shock (g)", xLabel: `Elapsed time (hours · ${span})`,
      baseline: { y: 1.0, label: "shock threshold" },
    });
  }
  // Ship multi-line: heave + pitch + roll over the entire voyage
  if (data.ship) {
    drawMultiLineChart($("chart-ship"), {
      x: data.ship.t_hours,
      series: [
        { y: data.ship.heave_m,    color: "#0072bb", label: "heave (m)" },
        { y: data.ship.pitch_deg,  color: "#a86d12", label: "pitch (°)" },
        { y: data.ship.roll_deg,   color: "#1f7a3a", label: "roll (°)" },
      ],
      xLabel: `Elapsed time (hours, full voyage · ${data.ship.n_rows_total?.toLocaleString() ?? "?"} samples)`,
    });
  }
}

// ── Lightweight pure-canvas chart helpers (no Chart.js needed) ────────────

function drawLineChart(canvas, { x, y, color, fill, yLabel, xLabel, baseline, hoverLabel }) {
  if (!canvas || !x.length) return;
  const dpr = window.devicePixelRatio || 1;
  // Read CSS-rendered size (locked via stylesheet); DPR-scale the *buffer*
  // only, never the layout dimensions. Without this the canvas can grow on
  // each redraw because canvas.height is a writable attribute.
  const cssRect = canvas.getBoundingClientRect();
  const W = Math.max(1, Math.round(cssRect.width));
  const H = Math.max(1, Math.round(cssRect.height));
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const padL = 56, padR = 16, padT = 12, padB = 32;
  const w = W - padL - padR, h = H - padT - padB;
  const xMax = Math.max(...x), yMax = Math.max(...y, baseline?.y || 0) * 1.1, yMin = 0;
  ctx.clearRect(0, 0, W, H);
  // Axes
  ctx.strokeStyle = "#e2dcd1"; ctx.lineWidth = 1;
  ctx.strokeRect(padL, padT, w, h);
  // Y grid
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillStyle = "#79766f"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const yv = yMin + (yMax - yMin) * (i / 4);
    const py = padT + h - (h * i / 4);
    ctx.fillText(yv.toFixed(2), padL - 6, py);
    ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + w, py);
    ctx.strokeStyle = "#f3efe5"; ctx.stroke();
  }
  // Baseline reference
  if (baseline) {
    const py = padT + h - (h * (baseline.y / yMax));
    ctx.strokeStyle = "#79766f"; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + w, py); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#79766f"; ctx.textAlign = "right";
    ctx.fillText(baseline.label, padL + w - 6, py - 8);
  }
  // Fill + line
  ctx.beginPath();
  ctx.moveTo(padL, padT + h);
  for (let i = 0; i < x.length; i++) {
    const px = padL + w * (x[i] / xMax);
    const py = padT + h - (h * (y[i] / yMax));
    ctx.lineTo(px, py);
  }
  ctx.lineTo(padL + w, padT + h); ctx.closePath();
  ctx.fillStyle = fill; ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < x.length; i++) {
    const px = padL + w * (x[i] / xMax);
    const py = padT + h - (h * (y[i] / yMax));
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
  // Axis labels
  ctx.fillStyle = "#3d4148"; ctx.textAlign = "center";
  ctx.fillText(xLabel, padL + w / 2, H - 10);
  ctx.save(); ctx.translate(14, padT + h / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillText(yLabel, 0, 0); ctx.restore();
  // Hover crosshair + tooltip
  attachChartHover(canvas, {
    kind: "line", x, y,
    label: hoverLabel || yLabel || "value",
    padL, padR, padT, padB,
  });
}

function drawScatterChart(canvas, { points, color, yLabel = "", xLabel, baseline, hoverLabel }) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  // Read CSS-rendered size (locked via stylesheet); DPR-scale the *buffer*
  // only, never the layout dimensions. Without this the canvas can grow on
  // each redraw because canvas.height is a writable attribute.
  const cssRect = canvas.getBoundingClientRect();
  const W = Math.max(1, Math.round(cssRect.width));
  const H = Math.max(1, Math.round(cssRect.height));
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const padL = 56, padR = 16, padT = 12, padB = 32;
  const w = W - padL - padR, h = H - padT - padB;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#e2dcd1"; ctx.strokeRect(padL, padT, w, h);
  if (!points.length) {
    ctx.fillStyle = "#79766f"; ctx.font = "11px Montserrat";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("No shock events detected in this slice", padL + w / 2, padT + h / 2);
    return;
  }
  const xMax = Math.max(...points.map(p => p.t));
  const yMax = Math.max(...points.map(p => p.g), baseline?.y || 0) * 1.1;
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillStyle = "#79766f"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const yv = yMax * (i / 4);
    const py = padT + h - (h * i / 4);
    ctx.fillText(yv.toFixed(2), padL - 6, py);
  }
  if (baseline) {
    const py = padT + h - (h * baseline.y / yMax);
    ctx.strokeStyle = "#a83232"; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + w, py); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = color;
  for (const p of points) {
    const px = padL + w * (p.t / xMax);
    const py = padT + h - (h * (p.g / yMax));
    ctx.beginPath(); ctx.arc(px, py, 3, 0, 2 * Math.PI); ctx.fill();
  }
  ctx.fillStyle = "#3d4148"; ctx.textAlign = "center";
  ctx.fillText(xLabel, padL + w / 2, H - 10);
  if (yLabel) {
    ctx.save(); ctx.translate(14, padT + h / 2); ctx.rotate(-Math.PI / 2);
    ctx.fillText(yLabel, 0, 0); ctx.restore();
  }
  // Hover crosshair + tooltip — points are { t, g } objects.
  attachChartHover(canvas, {
    kind: "scatter",
    x: points.map(p => p.t), y: points.map(p => p.g),
    label: hoverLabel || yLabel || "value",
    padL, padR, padT, padB,
  });
}

function drawMultiLineChart(canvas, { x, series, xLabel }) {
  if (!canvas || !x.length) return;
  const dpr = window.devicePixelRatio || 1;
  // Read CSS-rendered size (locked via stylesheet); DPR-scale the *buffer*
  // only, never the layout dimensions. Without this the canvas can grow on
  // each redraw because canvas.height is a writable attribute.
  const cssRect = canvas.getBoundingClientRect();
  const W = Math.max(1, Math.round(cssRect.width));
  const H = Math.max(1, Math.round(cssRect.height));
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const padL = 56, padR = 100, padT = 16, padB = 32;
  const w = W - padL - padR, h = H - padT - padB;
  const allY = series.flatMap(s => s.y);
  const yMin = Math.min(...allY), yMax = Math.max(...allY);
  const range = (yMax - yMin) || 1;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#e2dcd1"; ctx.strokeRect(padL, padT, w, h);
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillStyle = "#79766f"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const yv = yMin + range * (i / 4);
    const py = padT + h - (h * i / 4);
    ctx.fillText(yv.toFixed(2), padL - 6, py);
    ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + w, py);
    ctx.strokeStyle = "#f3efe5"; ctx.stroke();
  }
  for (const s of series) {
    ctx.beginPath();
    for (let i = 0; i < x.length; i++) {
      const px = padL + w * (i / (x.length - 1));
      const py = padT + h - (h * ((s.y[i] - yMin) / range));
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.4; ctx.stroke();
  }
  // Legend
  let ly = padT + 8;
  ctx.font = "11px Montserrat"; ctx.textAlign = "left";
  for (const s of series) {
    ctx.fillStyle = s.color; ctx.fillRect(padL + w + 12, ly, 10, 10);
    ctx.fillStyle = "#3d4148"; ctx.fillText(s.label, padL + w + 26, ly + 9);
    ly += 18;
  }
  ctx.fillStyle = "#3d4148"; ctx.textAlign = "center";
  ctx.fillText(xLabel, padL + w / 2, H - 10);
  // Hover crosshair + tooltip — multi-series version
  attachChartHover(canvas, {
    kind: "multi", x, series,
    padL, padR, padT, padB,
  });
}

// ── Universal interactive overlay (vertical guide + tooltip) ─────────────
// Attaches once per canvas; subsequent draws just update the cached payload.
// Works for line / scatter / multi-line by sharing a single nearest-x lookup.
function attachChartHover(canvas, payload) {
  canvas._chartPayload = payload;
  if (canvas._hoverWired) return;
  canvas._hoverWired = true;
  const card = canvas.closest(".chart-card") || canvas.parentNode;
  if (!card) return;
  const cs = getComputedStyle(card);
  if (cs.position === "static") card.style.position = "relative";
  let guide = card.querySelector(":scope > .chart-guide");
  if (!guide) {
    guide = document.createElement("div");
    guide.className = "chart-guide";
    card.appendChild(guide);
  }
  let tip = card.querySelector(":scope > .chart-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    card.appendChild(tip);
  }
  canvas.addEventListener("mousemove", e => {
    const p = canvas._chartPayload; if (!p?.x?.length) return;
    const cR = canvas.getBoundingClientRect();
    const cardR = card.getBoundingClientRect();
    const padL = p.padL ?? 56, padR = p.padR ?? 16;
    const W = cR.width;
    const w = W - padL - padR;
    const xPx = e.clientX - cR.left;
    const ratio = Math.max(0, Math.min(1, (xPx - padL) / w));
    const idx = Math.round(ratio * (p.x.length - 1));
    const xVal = p.x[idx];
    const lines = [`<b>t = ${(xVal ?? 0).toFixed(2)} h</b>`];
    if (p.kind === "multi") {
      for (const s of p.series) {
        lines.push(`<span class="ct-dot" style="background:${s.color}"></span>${s.label}: <b>${(s.y[idx] ?? 0).toFixed(3)}</b>`);
      }
    } else if (p.y) {
      lines.push(`${p.label}: <b>${(p.y[idx] ?? 0).toFixed(3)}</b>`);
    }
    tip.innerHTML = lines.join("<br>");
    // Position guide & tip relative to the card
    const guideX = (cR.left - cardR.left) + padL + w * ratio;
    guide.style.left = guideX + "px";
    guide.style.top  = (cR.top - cardR.top + (p.padT ?? 12)) + "px";
    guide.style.height = (cR.height - (p.padT ?? 12) - (p.padB ?? 32)) + "px";
    guide.style.display = "block";
    const tipX = Math.min(guideX + 12, cardR.width - 240);
    tip.style.left = Math.max(8, tipX) + "px";
    tip.style.top  = ((cR.top - cardR.top) + 10) + "px";
    tip.style.display = "block";
  });
  canvas.addEventListener("mouseleave", () => {
    guide.style.display = "none";
    tip.style.display = "none";
  });
}

// ===================== Optimize compare per-variant viewers =====================

let optCompareScenario = "drop_top";

async function renderOptCompare(result) {
  if (!result?.alternatives?.length) return;
  const plate = $("opt-compare-plate");
  if (!plate) return;
  plate.removeAttribute("hidden");

  // Wire scenario seg-control
  const seg = $("opt-compare-scenario");
  set(seg, "innerHTML", ["drop_top","drop_bottom","drop_side","transit"].map(s =>
    `<button data-v="${s}" class="${optCompareScenario===s?'active':''}">${s.replace("_"," ")}</button>`
  ).join(""));
  seg.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    optCompareScenario = b.dataset.v;
    seg.querySelectorAll("button").forEach(x => x.classList.toggle("active", x.dataset.v === optCompareScenario));
    paintOptCompareScenes();
  }));

  // Build cells: baseline + each alternative
  // Purge stale miniViewer refs — old Three.js renderers are bound to the
  // detached canvas elements from the previous innerHTML. Keeping them causes
  // paintOptCompareScenes to call .resize() on a dead canvas instead of
  // creating a fresh viewer for the newly-created canvas.
  const row = $("opt-compare-row");
  Object.keys(miniViewers).filter(k => k.startsWith("optcompare-")).forEach(k => delete miniViewers[k]);
  set(row, "innerHTML", "");
  // Baseline cell
  const baseline = result.baseline_summary || {};
  row.appendChild(buildOptCell({
    name: "Original", material: baseline.material,
    wall: baseline.wall_thickness_mm, sf: baseline.min_safety_factor,
    pass: baseline.passes_ista, baseline: true,
  }));
  result.alternatives.forEach((alt, altIdx) => {
    row.appendChild(buildOptCell({
      name: alt.name, material: alt.material?.name || alt.fields?.material,
      wall: alt.fields?.wall_thickness_mm, sf: alt.min_safety_factor,
      pass: alt.passes_ista, closure: alt.fields?.closure_type, alt, altIdx,
    }));
  });
  // Fetch heatmaps in parallel: baseline (already in heatmapState) + each variant
  await paintOptCompareScenes();
}

function buildOptCell({ name, material, wall, sf, pass, baseline, alt, closure, altIdx }) {
  const div = document.createElement("div");
  div.className = "opt-compare-cell" + (baseline ? " baseline" : (pass ? " passing" : ""));
  div.dataset.altName = name;
  div.innerHTML = `
    <div class="occ-name">${escapeHtml(name)}<span class="opt-verdict-cell ${pass ? 'pass' : 'fail'}">${pass ? "PASS" : "FAIL"}</span></div>
    <div class="occ-mat">${escapeHtml(material || "—")} · ${wall ?? "—"} mm${closure ? " · " + closure.replace("_"," ") : ""}</div>
    <div class="occ-canvas" data-occ-canvas></div>
    <div class="occ-foot">
      <span>min SF</span><span>${sf ?? "—"}</span>
    </div>
    ${!baseline ? `<div style="margin-top:8px"><a class="ghost ghost--xs" href="#/variant/${altIdx}">Open full page →</a></div>` : ""}
  `;
  if (alt) div._altPayload = alt;
  return div;
}

async function paintOptCompareScenes() {
  const cells = $("opt-compare-row").querySelectorAll(".opt-compare-cell");
  for (const cell of cells) {
    const canvas = cell.querySelector("[data-occ-canvas]");
    // Skip zero-size canvases — happens when the optimise stage is hidden.
    // The hashchange handler re-calls paintOptCompareScenes when /optimise
    // becomes visible; the makeMiniViewer ResizeObserver also handles it.
    if (!canvas || !canvas.offsetWidth || !canvas.offsetHeight) continue;
    const key = `optcompare-${cell.dataset.altName}`;
    if (!miniViewers[key]) miniViewers[key] = makeMiniViewer(canvas, { autoRotate: false });
    else miniViewers[key].resize();
    const mv = miniViewers[key];
    let scenes;
    if (cell.classList.contains("baseline")) {
      scenes = heatmapState;
    } else {
      // Fetch fresh per-variant heatmap from backend
      const alt = cell._altPayload;
      try {
        scenes = await http(`/cases/${caseId}/optimize/variant-heatmap`, {
          method: "POST",
          body: JSON.stringify({
            material: alt.material?.name || alt.fields?.material,
            wall_thickness_mm: alt.fields?.wall_thickness_mm,
            closure_type: alt.fields?.closure_type,
            fill_level_pct: alt.fields?.fill_level_pct,
          }),
        });
      } catch (_) { continue; }
    }
    if (!scenes?.scenes?.length) continue;
    const sc = scenes.scenes.find(s => s.scenario === optCompareScenario) || scenes.scenes[0];
    if (scenes.glb_url) await mv.loadGlb(scenes.glb_url);
    mv.applyVertexColors(sc.per_vertex_color);
  }
}

// ===================== Single-page Report =====================
function renderReport() {
  // Wire the PDF export link to the live case every time the report renders.
  const pdfLink = $("pdf-link");
  if (pdfLink && caseId) {
    pdfLink.href = `/api/cases/${caseId}/report.pdf`;
    pdfLink.removeAttribute("hidden");
  }

  if (!lastSnapshot?.report) {
    set($("report-article"), "innerHTML",
      '<div class="empty-state">Run the analysis to draft the report.</div>');
    return;
  }
  const r = lastSnapshot;
  const cs = r.report?.case_summary || {};
  const verdict = (r.ista2a?.overall_verdict || "").toLowerCase();
  const verdictWord = verdict === "pass" ? "Pass." : verdict === "fail" ? "Fail." : "—";
  const verdictColor = verdict === "pass" ? "var(--pass)" : verdict === "fail" ? "var(--fail)" : "var(--ink-mute)";

  // No more "Calculations" tab — the user asked for a clean engineer-grade
  // report, no formula dumps. We replace it with a Pie-distribution chart.
  const html = `
    <div class="report-cover">
      <span class="eyebrow">Engineering review · ISTA 2A + 6A · transit</span>
      <h2>${escapeHtml(_reportPackagingLabel(cs))}</h2>
      <div class="meta">${new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "long", year: "numeric" })} · Reviewer: ${escapeHtml(USER_ID)}</div>
      <div class="report-verdict" style="color:${verdictColor}">${verdictWord}</div>
    </div>
    <div class="report-toc">
      <a href="#inputs">Inputs</a> ·
      <a href="#material">Material</a> ·
      <a href="#transit">Transit</a> ·
      <a href="#risk">Risk zones</a> ·
      <a href="#ista">ISTA 2A &amp; 6A</a> ·
      <a href="#damage">Damage Analysis</a> ·
      <a href="#charts">Charts</a> ·
      <a href="#optimise">Optimise</a> ·
      <a href="#signoff">Sign-off</a>
    </div>

    <section class="report-section" id="inputs">
      <h3>Inputs</h3>
      ${kvList(cs)}
    </section>

    <section class="report-section" id="material">
      <h3>Material</h3>
      ${reportMaterial(r.material)}
      <!-- Spider/radar chart removed per spec — table above carries the same data. -->
    </section>

    <section class="report-section" id="transit">
      <h3>Transit envelope</h3>
      ${reportTransit(r.transit)}
      <div class="chart-card chart-card--has-controls" data-chart="rpt-truck-vib">
        <div class="cc-head"><h5>Truck telemetry · vibration over time</h5>${spanControls("rpt-truck-vib")}</div>
        <canvas id="rpt-truck-vib"></canvas></div>
      <div class="chart-card chart-card--has-controls" data-chart="rpt-truck-stress">
        <div class="cc-head"><h5>Stress induced over time · derived from product mass (1 000 pts)</h5>${spanControls("rpt-truck-stress")}</div>
        <canvas id="rpt-truck-stress"></canvas></div>
      <div class="chart-card chart-card--has-controls" data-chart="rpt-truck-shock">
        <div class="cc-head"><h5>Truck telemetry · shock events (p95 threshold marked)</h5>${spanControls("rpt-truck-shock")}</div>
        <canvas id="rpt-truck-shock"></canvas></div>
      <div class="chart-card chart-card--has-controls" data-chart="rpt-ship">
        <div class="cc-head"><h5>Ship telemetry · roll · pitch · heave</h5>${spanControls("rpt-ship")}</div>
        <canvas id="rpt-ship"></canvas></div>
      <div class="chart-card"><div class="cc-head"><h5>Mode mix</h5></div>
        <canvas id="rpt-mode-pie"></canvas></div>
    </section>

    <section class="report-section" id="risk">
      <h3>Risk zones</h3>
      ${reportRisk(r.risk_map)}
      <div class="chart-card"><div class="cc-head"><h5>Zone risk · bar</h5></div>
        <canvas id="rpt-zone-risk"></canvas></div>
    </section>

    <section class="report-section" id="ista">
      <h3>ISTA 2A &amp; 6A verdicts</h3>
      ${reportIstaCombined(r.ista2a, r.ista6a)}
      <div class="chart-card"><div class="cc-head"><h5>Impact pressure (MPa) per drop orientation</h5></div>
        <canvas id="rpt-impact-bar"></canvas></div>
      <div class="chart-card"><div class="cc-head"><h5>Safety-factor margin vs threshold</h5></div>
        <canvas id="rpt-sf-bar"></canvas></div>
    </section>

    ${DAMAGE_ANALYSIS_ENABLED ? `<section class="report-section" id="damage">
      <h3>Damage Analysis</h3>
      ${_buildDamageHtml(r, { forReport: true })}
    </section>` : ''}

    <section class="report-section" id="charts">
      <h3>Comparison &amp; cost</h3>
      ${reportCostBlock()}
      <div class="chart-card"><div class="cc-head"><h5>Material density · catalogue comparison</h5></div>
        <canvas id="rpt-density-bar"></canvas></div>
    </section>

    <section class="report-section" id="optimise">
      <h3>Optimise this design</h3>
      <p>Use the studio below to generate three alternative designs that all
      pass ISTA 2A. Each variant has its own dedicated page with full charts
      and a 3D heatmap.</p>
      <div id="rpt-optimise-mount"></div>
    </section>

    <section class="report-section" id="signoff">
      <h3>Sign-off</h3>
      <p>Approve and lock the design on the <a href="#/signoff">Sign-off stage</a>. Once locked, every input + verdict is captured in a SHA-256 manifest.</p>
    </section>
  `;
  set($("report-article"), "innerHTML", html);

  // Now that the DOM is in place, paint every dynamic chart.
  requestAnimationFrame(() => {
    paintReportCharts(r);
    mountOptimiseInReport();
  });
}

// Inline canvas-based charts in the report — no static PNGs.
//
// One paint pass: ensure the data is hydrated, then call _drawReportCharts.
// Subsequent triggers (per-graph time-span buttons, container resize) all
// route through _drawReportCharts so charts stay sharp without a re-fetch.
let _lastReportPayload = null;       // cached snapshot for repaint
const _spanState = {};               // canvas-id -> "1x" | "2x" | "full"

// Small button cluster injected into each chart's header. The data attribute
// tells the click handler which canvas to repaint with the new span.
function spanControls(chartId) {
  return `<div class="cc-spans" data-target="${chartId}">
    <button type="button" data-span="1x"   class="cs-btn cs-btn--on" title="Default time window">1×</button>
    <button type="button" data-span="2x"   class="cs-btn"            title="Double the time window">2×</button>
    <button type="button" data-span="full" class="cs-btn"            title="Show every available sample">Full</button>
  </div>`;
}

// Slice a series for the requested span. The full source already lives in
// lastTransitCharts (max_points=25000) so this is a pure client-side cut.
function _sliceForSpan(arr, span) {
  if (!Array.isArray(arr) || arr.length < 2) return arr || [];
  if (span === "full") return arr;
  const frac = span === "2x" ? 0.50 : 0.25;
  const n = Math.max(2, Math.floor(arr.length * frac));
  return arr.slice(0, n);
}

async function paintReportCharts(r) {
  _lastReportPayload = r;
  if (!lastTransitCharts) {
    const cs = r.case_summary || r.report?.case_summary || {};
    const modes = (cs.transit_modes || ["truck"]).filter(m => ["truck","ship"].includes(m));
    const wanted = (modes.length ? modes : ["truck"]).join(",");
    try {
      // Report charts request the maximum resolution we cap server-side
      // (25 K points per series) so the engineering record reflects every
      // strided sample of the source CSV, not just the first 8 hours.
      lastTransitCharts = await http(
        `/transit/charts?road=${encodeURIComponent(cs.road_condition || "mixed")}` +
        `&ship_severity=${encodeURIComponent(cs.ship_severity || "moderate")}` +
        `&modes=${encodeURIComponent(wanted)}` +
        `&max_points=25000`
      );
    } catch (_) {}
  }
  _drawReportCharts();
  _wireSpanButtons();
  _attachReportResizeObserver();
}

// Re-paint the report charts from the cached snapshot. Called by the
// hash-router when re-entering the Report stage, by the ResizeObserver on
// container width changes, and by the per-graph time-span buttons.
function repaintReportCharts() {
  if (_lastReportPayload) _drawReportCharts();
}

function _drawReportCharts() {
  const r = _lastReportPayload; if (!r) return;
  // (Material radar removed — replaced by the property table.)
  // Truck telemetry
  if (lastTransitCharts?.truck) {
    const tt = lastTransitCharts.truck;
    const totalH = tt.t_hours[tt.t_hours.length - 1] || 0;
    const totalN = tt.n_rows_total ?? tt.t_hours.length;
    const spanV = _spanState["rpt-truck-vib"] || "1x";
    const xV = _sliceForSpan(tt.t_hours, spanV);
    const yV = _sliceForSpan(tt.vibration_g, spanV);
    const spanLabel = `${(xV[xV.length - 1] || 0).toFixed(0)} h shown · ${totalH.toFixed(0)} h total · ${totalN.toLocaleString()} samples`;
    drawLineChart($("rpt-truck-vib"), {
      x: xV, y: yV,
      color: "#0072bb", fill: "rgba(0,114,187,0.10)",
      yLabel: "Vibration (g)", xLabel: `Elapsed time (hours · ${spanLabel})`,
      baseline: { y: 0.54, label: "ISTA baseline" },
      hoverLabel: "Vibration (g)",
    });
    // Stress-induced chart — derived from the product mass × the truck PSD.
    if ($("rpt-truck-stress")) {
      drawStressOverTime($("rpt-truck-stress"), {
        x: xV, vibration_g: yV,
        mass_kg: _massKgForReport(r),
        area_mm2: _stressAreaMm2(r),
      });
    }
    const spanS = _spanState["rpt-truck-shock"] || "1x";
    const allShocks = tt.shock_events || [];
    const cutoff = (xV[xV.length - 1] || 1e9);
    const shockPts = spanS === "full" ? allShocks
      : allShocks.filter(p => (p[0] ?? p.x ?? p.t) <= cutoff);
    drawScatterChart($("rpt-truck-shock"), {
      points: shockPts,
      color: "#a83232", yLabel: "Shock (g)",
      xLabel: `Elapsed time (hours · ${spanLabel})`,
      baseline: { y: 1.0, label: "threshold" },
      hoverLabel: "Shock (g)",
    });
  }
  // Ship telemetry
  if (lastTransitCharts?.ship) {
    const sh = lastTransitCharts.ship;
    const spanS = _spanState["rpt-ship"] || "1x";
    const x = _sliceForSpan(sh.t_hours, spanS);
    drawMultiLineChart($("rpt-ship"), {
      x,
      series: [
        { y: _sliceForSpan(sh.heave_m,   spanS), color: "#0072bb", label: "heave (m)" },
        { y: _sliceForSpan(sh.pitch_deg, spanS), color: "#a86d12", label: "pitch (°)" },
        { y: _sliceForSpan(sh.roll_deg,  spanS), color: "#1f7a3a", label: "roll (°)" },
      ],
      xLabel: `Elapsed time (hours · ${(x[x.length - 1] || 0).toFixed(0)} h shown · ${(sh.n_rows_total ?? sh.t_hours.length).toLocaleString()} samples)`,
    });
  }
  // Mode-mix pie
  if ($("rpt-mode-pie") && r.transit?.mode_mix) {
    drawPieChart($("rpt-mode-pie"), r.transit.mode_mix);
  }
  // Zone risk
  if ($("rpt-zone-risk") && r.risk_map?.zones) {
    drawBarChart($("rpt-zone-risk"),
      r.risk_map.zones.map(z => ({ label: z.zone, value: z.risk_score })),
      { color: "#0072bb", yLabel: "Risk score (0..1)" });
  }
  // Impact pressure bar (2A drops + 6A corner)
  if ($("rpt-impact-bar")) {
    const points = [];
    for (const d of r.ista2a?.drops || []) {
      points.push({ label: `2A · ${d.orientation}`,
                    value: d.impact_pressure_mpa,
                    color: d.verdict === "pass" ? "#1f7a3a" : "#a83232" });
    }
    if (r.ista6a) {
      points.push({ label: `6A · corner`, value: r.ista6a.impact_pressure_mpa,
                    color: r.ista6a.overall_verdict === "pass" ? "#1f7a3a" : "#a83232" });
    }
    drawBarChart($("rpt-impact-bar"), points, {
      yLabel: "σ_local (MPa)", colorPerBar: true,
    });
  }
  // SF margin bar (SF − threshold)
  if ($("rpt-sf-bar")) {
    const pts = [];
    for (const d of r.ista2a?.drops || []) {
      pts.push({ label: `2A · ${d.orientation}`,
                 value: (d.safety_factor ?? 0) - 1.0,
                 color: (d.safety_factor ?? 0) >= 1 ? "#1f7a3a" : "#a83232" });
    }
    if (r.ista6a) {
      pts.push({ label: "6A · corner",
                 value: (r.ista6a.safety_factor ?? 0) - 1.0,
                 color: (r.ista6a.safety_factor ?? 0) >= 1 ? "#1f7a3a" : "#a83232" });
    }
    drawBarChart($("rpt-sf-bar"), pts, {
      yLabel: "SF margin (SF − 1.0)", colorPerBar: true,
      zeroLine: true,
    });
  }
  // Density catalogue comparison
  if ($("rpt-density-bar")) {
    const catalogue = [
      { label: "PET", value: 1380 }, { label: "HDPE", value: 955 },
      { label: "PP", value: 905 }, { label: "Glass", value: 2500 },
      { label: "Aluminum", value: 2700 },
    ];
    if (r.material?.density_kg_m3) {
      catalogue.unshift({
        label: r.material.name || "this", value: r.material.density_kg_m3,
        color: "#0072bb", highlight: true,
      });
    }
    drawBarChart($("rpt-density-bar"), catalogue,
      { yLabel: "Density (kg/m³)", colorPerBar: true });
  }
}

// ── Stress-induced time-history (derived from product mass) ──────────────
// Resamples the truck vibration PSD to exactly 1 000 evenly-spaced points,
// then converts each amplitude into an *induced stress* on the package wall
// using a first-order σ = m·a / A model. Every product mass produces a
// unique stress history under the same transit envelope.
function drawStressOverTime(canvas, { x, vibration_g, mass_kg, area_mm2 }) {
  if (!canvas || !x?.length) return;
  const N = 1000;
  const xOut = new Array(N);
  const yOut = new Array(N);
  const M = vibration_g.length;
  const m = Math.max(0.01, Number(mass_kg) || 0.5);
  const A = Math.max(1, Number(area_mm2) || 200);
  // σ in MPa: σ = m[kg]·a[m/s²] / A[mm²]; 1 N/mm² == 1 MPa.
  for (let i = 0; i < N; i++) {
    const idx = Math.floor((i / (N - 1)) * (M - 1));
    const a_ms2 = (vibration_g[idx] || 0) * 9.81;
    const F_N = m * a_ms2;
    yOut[i] = F_N / A;
    xOut[i] = x[idx];
  }
  drawLineChart(canvas, {
    x: xOut, y: yOut,
    color: "#a86d12", fill: "rgba(168,109,18,0.10)",
    yLabel: "σ_induced (MPa)",
    xLabel: `Elapsed time (hours) · derived from mass ${m.toFixed(2)} kg · area ${A.toFixed(0)} mm² · 1 000 samples`,
    baseline: { y: 0.5, label: "fatigue endurance ≈" },
    hoverLabel: "σ_induced (MPa)",
  });
}

// Pull the product mass from the snapshot's brief / case_summary.
function _massKgForReport(r) {
  const cs = r?.case_summary || r?.report?.case_summary || {};
  if (cs.product_weight_kg) return Number(cs.product_weight_kg);
  if (cs.fill_weight_g)     return Number(cs.fill_weight_g) / 1000;
  if (cs.unit_weight_g)     return Number(cs.unit_weight_g) / 1000;
  if (r?.intake?.product_weight_kg) return Number(r.intake.product_weight_kg);
  return 0.5;       // PET 500 ml default
}
// Effective load-bearing wall cross-section: perimeter × wall thickness.
function _stressAreaMm2(r) {
  const cs = r?.case_summary || r?.report?.case_summary || {};
  const dims = r?.geometry?.overall_dims_mm || cs.overall_dims_mm || {};
  const L = Number(dims.length_mm) || 70;
  const W = Number(dims.width_mm)  || 70;
  const t = Number(cs.wall_thickness_mm) || 1.2;
  const perim = 2 * (L + W);
  return Math.max(50, perim * t);
}

// Per-graph time-span buttons — one shared click delegate, idempotent wiring.
function _wireSpanButtons() {
  document.querySelectorAll(".cc-spans").forEach(cluster => {
    if (cluster._wired) return; cluster._wired = true;
    cluster.addEventListener("click", e => {
      const btn = e.target.closest(".cs-btn"); if (!btn) return;
      cluster.querySelectorAll(".cs-btn").forEach(b => b.classList.remove("cs-btn--on"));
      btn.classList.add("cs-btn--on");
      _spanState[cluster.dataset.target] = btn.dataset.span;
      _drawReportCharts();
    });
  });
}

// Single ResizeObserver on the report article — repaints every chart inside
// when the container width changes (sidebar collapse, window resize, etc).
let _reportRO = null;
function _attachReportResizeObserver() {
  const article = $("report-article");
  if (!article || _reportRO) return;
  _reportRO = new ResizeObserver(() => {
    if (article._rafRedraw) cancelAnimationFrame(article._rafRedraw);
    article._rafRedraw = requestAnimationFrame(() => _drawReportCharts());
  });
  _reportRO.observe(article);
}

function reportCostBlock() {
  const r = lastSnapshot; if (!r) return "";
  // Backend's cost endpoint is async; we use whatever the brief carries.
  const cs = r.case_summary || r.report?.case_summary || {};
  const cost = cs._cost; // backfilled below from /api/.../cost
  return cost ? `
    <div class="card">
      <span class="eyebrow">Unit cost</span>
      <h4>$${cost.cost_per_unit_usd?.toFixed(3) ?? "—"} per unit</h4>
      <div style="color:var(--ink-mute);font-size:12.5px">
        ${cost.material || "—"} · ${cost.mass_g ?? "—"} g · projected
        $${cost.annual_cost_usd_at_1m_units?.toLocaleString() ?? "—"} at 1M units / year
      </div>
    </div>` : "";
}

// Combined ISTA 2A + 6A verdict block — replaces the older 2A-only renderer.
function reportIstaCombined(ista2a, ista6a) {
  const cell = (label, verdict) => {
    const cls = verdict === "pass" ? "verified" : verdict === "fail" ? "insufficient_data" : "approximate";
    return `<span class="tag ${cls}">${escapeHtml((verdict || "").toUpperCase())}</span> ${escapeHtml(label)}`;
  };
  const tw = `<table>
    <tr><th>Test</th><th>SF</th><th>σ_local (MPa)</th><th>Verdict</th></tr>
    ${(ista2a?.drops || []).map(d => `
      <tr>
        <td>ISTA 2A · ${d.orientation}</td>
        <td>${d.safety_factor ?? "—"}</td>
        <td>${d.impact_pressure_mpa ?? "—"}</td>
        <td>${cell("", d.verdict)}</td>
      </tr>`).join("")}
    ${ista6a ? `
      <tr>
        <td>ISTA 6A · corner</td>
        <td>${ista6a.safety_factor ?? "—"}</td>
        <td>${ista6a.impact_pressure_mpa ?? "—"}</td>
        <td>${cell("", ista6a.overall_verdict)}</td>
      </tr>` : ""}
  </table>
  <p style="margin-top:8px">
    ISTA 2A overall: ${cell("", ista2a?.overall_verdict)}
    ${ista6a ? "  ·  ISTA 6A overall: " + cell("", ista6a.overall_verdict) : ""}
  </p>`;
  return tw;
}

// Mount the optimise studio inside the report at #rpt-optimise-mount, with a
// "Open Optimise studio →" link AND the comparison + spider charts inline.
function mountOptimiseInReport() {
  const mount = $("rpt-optimise-mount"); if (!mount) return;
  const fam = _effectiveFamily();
  const isPacket = fam === "packet";
  const isBrush  = fam === "brush";
  const existingResult = isPacket ? lastPktOptResult : isBrush ? lastBrushOptResult : lastOptResult;
  const chipsHtml = isPacket
    ? `<button class="chip" data-rpt-pkt-intent="reduce_cost">Reduce cost</button>
       <button class="chip" data-rpt-pkt-intent="improve_survivability">Improve survivability</button>
       <button class="chip" data-rpt-pkt-intent="improve_shelf_life">Improve shelf life</button>
       <button class="chip" data-rpt-pkt-intent="other">Something else</button>`
    : isBrush
    ? `<button class="chip" data-rpt-brush-intent="reduce_cost">Reduce cost</button>
       <button class="chip" data-rpt-brush-intent="improve_survivability">Improve survivability</button>
       <button class="chip" data-rpt-brush-intent="improve_sustainability">Improve sustainability</button>
       <button class="chip" data-rpt-brush-intent="other">Something else</button>`
    : `<button class="chip" data-intent="reduce_cost">Reduce material cost</button>
       <button class="chip" data-intent="increase_strength">Increase strength</button>
       <button class="chip" data-intent="other">Something else</button>`;
  const summaryHtml = existingResult
    ? `<p>Last run produced ${existingResult.alternatives.length} alternative designs.
       Open the studio for the comparison ledger.</p>`
    : `<p style="color:var(--ink-mute);font-style:italic">Pick an intent above to run an optimisation pass without leaving the report.</p>`;
  const html = `
    <div class="opt-intents" style="margin-top:8px">
      ${chipsHtml}
      <a href="#/optimise" class="ghost" style="margin-left:8px">Open full Optimise studio →</a>
    </div>
    <div id="rpt-opt-summary" style="margin-top:12px">${summaryHtml}</div>
  `;
  set(mount, "innerHTML", html);
  // Bottle chips
  mount.querySelectorAll("[data-intent]").forEach(btn => btn.addEventListener("click", async () => {
    const intent = btn.dataset.intent;
    if (intent === "other") { location.hash = "#/optimise"; return; }
    optIntent = intent;
    startReportOptLoader();
    await optGenerate(intent, "from report");
    stopReportOptLoader();
    set($("rpt-opt-summary"), "innerHTML",
      `<p>${lastOptResult.alternatives.length} alternatives ready, all PASS ISTA 2A.
       <a href="#/optimise">Open the studio →</a></p>`);
  }));
  // Packet chips
  mount.querySelectorAll("[data-rpt-pkt-intent]").forEach(btn => btn.addEventListener("click", async () => {
    const intent = btn.dataset.rptPktIntent;
    if (intent === "other") { location.hash = "#/optimise"; return; }
    pktOptIntent = intent;
    startReportOptLoader();
    await pktOptGenerate(intent, "from report");
    stopReportOptLoader();
    set($("rpt-opt-summary"), "innerHTML",
      `<p>${lastPktOptResult.alternatives.length} flexible packet alternatives ready.
       <a href="#/optimise">Open the studio →</a></p>`);
  }));
  // Brush chips
  mount.querySelectorAll("[data-rpt-brush-intent]").forEach(btn => btn.addEventListener("click", async () => {
    const intent = btn.dataset.rptBrushIntent;
    if (intent === "other") { location.hash = "#/optimise"; return; }
    brushOptIntent = intent;
    startReportOptLoader();
    await brushOptGenerate(intent, "from report");
    stopReportOptLoader();
    set($("rpt-opt-summary"), "innerHTML",
      `<p>${lastBrushOptResult.alternatives.length} brush packaging alternatives ready.
       <a href="#/optimise">Open the studio →</a></p>`);
  }));
}

// ── Optimisation-in-report loading bar with rotating text ───────────────
// While the optimiser is running (which can take 10-40s with retries) we
// show a fake-progress bar + cycling status copy so the user knows we're
// burning real cycles to find passing variants.

const REPORT_OPT_TICKS = [
  "Sweeping the design space",
  "Going through 1000 candidate variants",
  "Calculating ISTA 2A drop tests for each candidate",
  "Calculating ISTA 6A corner-drop tests for each candidate",
  "Cross-checking transit envelope against real CSV data",
  "Filtering for variants that pass every test",
  "Computing material cost and ROI for each candidate",
  "Re-evaluating with thicker walls and tougher materials",
  "Picking the three most engineering-defensible options",
];
let _reportOptTimer = null;
let _reportOptStarted = 0;

function startReportOptLoader() {
  const summary = $("rpt-opt-summary"); if (!summary) return;
  _reportOptStarted = performance.now();
  set(summary, "innerHTML", `
    <div class="rpt-opt-loader">
      <div class="rol-bar"><div class="rol-fill" id="rol-fill"></div></div>
      <div class="rol-text" id="rol-text">${REPORT_OPT_TICKS[0]}</div>
    </div>
  `);
  let i = 0;
  if (_reportOptTimer) clearInterval(_reportOptTimer);
  _reportOptTimer = setInterval(() => {
    i = (i + 1) % REPORT_OPT_TICKS.length;
    const text = $("rol-text"); if (text) text.textContent = REPORT_OPT_TICKS[i];
    // Asymptotic bar: fast at first, slows as it approaches 95%
    const elapsed = (performance.now() - _reportOptStarted) / 1000;
    const pct = Math.min(95, Math.round(100 * (1 - Math.exp(-elapsed / 18))));
    const fill = $("rol-fill"); if (fill) fill.style.width = pct + "%";
  }, 1100);
}
function stopReportOptLoader() {
  if (_reportOptTimer) clearInterval(_reportOptTimer);
  _reportOptTimer = null;
  const fill = $("rol-fill"); if (fill) fill.style.width = "100%";
}

// ── Generic dynamic chart helpers (added for the report) ───────────────

function drawPieChart(canvas, data) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const cssRect = canvas.getBoundingClientRect();
  const W = Math.max(1, Math.round(cssRect.width)), H = 220;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const entries = Object.entries(data || {}).filter(([_, v]) => v > 0);
  const legendW = 140;
  const cy = H / 2;
  const r = Math.max(1, Math.min(W - legendW, H) / 2 - 10);
  const cx = r + 10;
  const total = entries.reduce((s, [, v]) => s + v, 0) || 1;
  const palette = ["#0072bb","#a86d12","#1f7a3a","#a83232","#7c5cff","#79766f"];
  let start = -Math.PI / 2;
  entries.forEach(([, v], i) => {
    const slice = (v / total) * 2 * Math.PI;
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, start + slice);
    ctx.closePath();
    ctx.fillStyle = palette[i % palette.length]; ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
    start += slice;
  });
  // Legend on the right
  ctx.font = "12px Montserrat";
  let ly = cy - (entries.length * 18) / 2;
  entries.forEach(([key, v], i) => {
    ctx.fillStyle = palette[i % palette.length];
    ctx.fillRect(cx + r + 20, ly, 12, 12);
    ctx.fillStyle = "#1a1d22"; ctx.textBaseline = "middle";
    ctx.fillText(`${key} · ${Math.round((v / total) * 100)}%`, cx + r + 38, ly + 6);
    ly += 22;
  });
}

function drawBarChart(canvas, points, opts = {}) {
  if (!canvas || !points?.length) return;
  const dpr = window.devicePixelRatio || 1;
  const cssRect = canvas.getBoundingClientRect();
  const W = Math.max(1, Math.round(cssRect.width));
  const H = opts.height || (parseInt(canvas.getAttribute("height"), 10) || 200);
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const padL = 60, padR = 12, padT = 12, padB = 44;
  const w = W - padL - padR, h = H - padT - padB;
  const vals = points.map(p => p.value || 0);
  const yMin = opts.zeroLine ? Math.min(0, ...vals) : 0;
  const yMax = Math.max(...vals, 0.1);
  const range = (yMax - yMin) || 1;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#e2dcd1"; ctx.lineWidth = 1;
  ctx.strokeRect(padL, padT, w, h);
  // Grid + Y labels
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillStyle = "#79766f"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const yv = yMin + range * (i / 4);
    const py = padT + h - (h * i / 4);
    ctx.fillText(yv.toFixed(yv > 100 ? 0 : 2), padL - 6, py);
    ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + w, py);
    ctx.strokeStyle = "#f3efe5"; ctx.stroke();
  }
  // Zero line emphasized if signed
  if (opts.zeroLine && yMin < 0) {
    const py = padT + h - (h * (0 - yMin) / range);
    ctx.beginPath(); ctx.strokeStyle = "#1a1d22"; ctx.lineWidth = 1.5;
    ctx.moveTo(padL, py); ctx.lineTo(padL + w, py); ctx.stroke();
  }
  // Bars
  const barW = (w / points.length) * 0.7;
  const gap = (w / points.length) * 0.3;
  ctx.textBaseline = "alphabetic"; ctx.textAlign = "center";
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const x = padL + i * (barW + gap) + gap / 2;
    const v = p.value || 0;
    const yZero = padT + h - (h * (0 - yMin) / range);
    const yVal  = padT + h - (h * (v  - yMin) / range);
    const top = Math.min(yZero, yVal), bh = Math.abs(yZero - yVal);
    ctx.fillStyle = opts.colorPerBar ? (p.color || "#0072bb") : (opts.color || "#0072bb");
    if (p.highlight) {
      ctx.shadowColor = "rgba(0,114,187,0.35)"; ctx.shadowBlur = 6;
    }
    ctx.fillRect(x, top, barW, bh);
    ctx.shadowBlur = 0;
    // value labels above bar
    ctx.fillStyle = "#3d4148"; ctx.font = "10px 'IBM Plex Mono', monospace";
    ctx.fillText(v.toFixed(v > 100 ? 0 : 2), x + barW / 2, Math.min(yZero, yVal) - 4);
    // x labels
    ctx.fillStyle = "#79766f"; ctx.font = "10px Montserrat";
    const labels = String(p.label).split(/[ ·]/);
    let ly = H - padB + 12;
    labels.forEach(s => { ctx.fillText(s, x + barW / 2, ly); ly += 11; });
  }
  if (opts.yLabel) {
    ctx.fillStyle = "#3d4148"; ctx.save();
    ctx.translate(14, padT + h / 2); ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center"; ctx.fillText(opts.yLabel, 0, 0); ctx.restore();
  }
}

// drawMaterialRadar removed — the radial profile chart is gone from the
// report along with the spider chart.


function kvList(cs) {
  const fam = (cs.packaging_family || "").toLowerCase();
  let interesting;
  if (fam === "brush") {
    interesting = ["packaging_family","brush_type","brush_weight_g",
      "primary_pack_type","primary_pack_material",
      "transit_modes","road_condition","objective",
      "has_secondary_carton","carton_type","carton_board_grade","carton_pack_count","carton_stacking_config"];
  } else if (fam === "packet") {
    interesting = ["packaging_type","packet_style","laminate_structure","total_thickness_micron",
      "seal_type","fill_weight_g","transit_modes","road_condition","objective",
      "has_secondary_carton","carton_type","carton_board_grade","packets_per_carton"];
  } else {
    interesting = ["packaging_type","bottle_subtype","capacity_ml","material",
      "wall_thickness_mm","gross_weight_g","product_type","fill_level_pct",
      "transit_modes","road_condition","ship_severity","stack_height",
      "stacking_orientation","ships_loose","objective","test_standard"];
  }
  const rows = interesting.filter(k => cs[k] !== undefined && cs[k] !== null && cs[k] !== "")
    .map(k => `<tr><th>${k.replace(/_/g, " ")}</th><td>${escapeHtml(Array.isArray(cs[k]) ? cs[k].join(", ") : String(cs[k]))}</td></tr>`)
    .join("");
  return `<table>${rows}</table>`;
}
function _reportPackagingLabel(cs) {
  const fam = (cs.packaging_family || "").toLowerCase();
  if (fam === "brush") return (cs.brush_type || "brush").replace(/_/g, " ");
  if (fam === "packet") return (cs.packet_style || cs.packaging_type || "packet").replace(/_/g, " ");
  return cs.bottle_subtype
    ? cs.bottle_subtype + " · " + (cs.packaging_type || "")
    : (cs.packaging_type || "Design");
}
function reportMaterial(m) {
  if (!m) return "<p>No material data captured.</p>";
  return `<table>
    <tr><th>Name</th><td>${escapeHtml(m.name)} <span class="tag ${m.confidence}">${m.confidence}</span></td></tr>
    <tr><th>Density</th><td>${m.density_kg_m3 ?? "—"} kg/m³</td></tr>
    <tr><th>Modulus</th><td>${m.modulus_gpa ?? "—"} GPa</td></tr>
    <tr><th>Yield</th><td>${m.yield_strength_mpa ?? "—"} MPa</td></tr>
    <tr><th>Allowable</th><td>${m.allowable_stress_mpa ?? "—"} MPa</td></tr>
    <tr><th>Source</th><td>${escapeHtml(m.source)}</td></tr>
  </table>`;
}
function reportTransit(t) {
  if (!t) return "<p>No transit envelope.</p>";
  return `<table>
    <tr><th>Mode mix</th><td>${escapeHtml(JSON.stringify(t.mode_mix))}</td></tr>
    <tr><th>Vibration</th><td>${t.vibration_g_rms} g_rms</td></tr>
    <tr><th>Drop height</th><td>${t.drop_height_m} m</td></tr>
    <tr><th>Handling fraction</th><td>${t.handling_fraction}</td></tr>
    <tr><th>Dominant risks</th><td>${(t.dominant_risks || []).join(", ")}</td></tr>
    <tr><th>Suggested sequence</th><td>${(t.suggested_test_sequence || []).join(" → ")}</td></tr>
  </table>`;
}
// Raw-calculation table dropped from the report per spec — engineers see
// verdicts + charts, not formula traces.

function reportRisk(r) {
  if (!r) return "<p>No risk map.</p>";
  return `<table>
    <tr><th>Zone</th><th>Score</th><th>Rationale</th></tr>
    ${r.zones.map(z => `
      <tr>
        <td>${escapeHtml(z.zone)}</td>
        <td>${z.risk_score}</td>
        <td style="color:var(--ink-mute)">${escapeHtml(z.rationale)}</td>
      </tr>`).join("")}
  </table>
  <p style="color:var(--warn);font-size:12px;margin-top:8px">${escapeHtml(r.approximation_warning || "")}</p>`;
}
// reportIsta + chartIfPresent removed — replaced by reportIstaCombined (which
// covers ISTA 2A AND 6A together) and inline dynamic canvas charts.

// ===================== Sign-off =====================
function renderSignoffStage() {
  const has = lastSnapshot?.report;
  const state = $("signoff-state");
  if (!has) {
    set(state, "innerHTML", `<div class="empty-state">Run the analysis first.</div>`);
    $("signoff-btn").disabled = true;
    return;
  }
  set(state, "innerHTML", "");
  $("signoff-btn").disabled = false;
  // Mirror the heatmap into the sign-off mini-strip
  renderHeatmapIntoSignoff();
}
async function doSignoff() {
  const name = $("signoff-name").value.trim();
  if (!name) { alert("Approver name required"); return; }
  progressBegin("Signing and locking", 2);
  try {
    const r = await http(`/cases/${caseId}/signoff`, {
      method: "POST",
      body: JSON.stringify({ approver_name: name, notes: $("signoff-notes").value }),
    });
    progressTick();
    set($("signoff-result"), "innerHTML", `
      <div><strong>Locked.</strong> Approved by ${escapeHtml(r.signed_off_by)} on ${escapeHtml(r.signed_off_at)}</div>
      <div style="margin-top:8px">SHA-256 manifest hash:<br><code style="word-break:break-all">${r.signoff_hash}</code></div>
      <div style="margin-top:8px;color:var(--ink-mute);font-size:12px">Manifest covers every input + every analysis result + the case summary.</div>
    `);
    cls($("signoff-result"), "remove", "hidden");
    cls($("unlock-btn"), "remove", "hidden");
    $("signoff-btn").disabled = true;
    updateStageRail();
  } catch (e) {
    alert("Sign-off failed: " + e.message);
  } finally { progressEnd(); }
}

// Renders the Results stage scorecard + findings panel. Findings sit next
// to the 3 side-by-side mini viewers and explain *where* and *why* damage
// is expected for each drop orientation.
function renderResultsStage() {
  renderScorecard();
  renderFindings();
  _renderResultsViewers();
  renderSecondaryPackagingSection(lastSnapshot);
  renderDamageAnalysis(lastSnapshot);
}

// ── Secondary Packaging placeholder section ───────────────────────────────
// Shown in Results when case_summary has has_secondary_carton == "yes".
// Visualisations are placeholder images (top/bottom/side). Real BCT/ECT
// simulation is not implemented in this phase.
function renderSecondaryPackagingSection(snapshot) {
  const section = $("secondary-pkg-section");
  if (!section) return;

  // Resolve secondary_packaging — can come from snapshot directly (post-feature
  // analysis runs) or be rebuilt from case_summary flat fields (pre-feature runs).
  const cs = snapshot?.case_summary || {};
  const sp = snapshot?.secondary_packaging
    || (cs.has_secondary_carton === "yes"
        ? { enabled: true,
            carton_type:     cs.carton_type,
            board_type:      cs.carton_board_grade,
            pack_count:      cs.carton_pack_count || cs.packets_per_carton,
            stacking_config: cs.carton_stacking_config || cs.stacking_method,
            transit_mode:    cs.transit_modes,
            goal:            cs.objective,
          }
        : { enabled: false });

  // User explicitly opted out — hide the section entirely.
  if (cs.has_secondary_carton === "no") {
    section.classList.add("hidden");
    return;
  }

  // Always show the section once a snapshot exists. When secondary packaging
  // data hasn't been collected yet, show placeholder visuals and an explanation
  // so the user can see what the section will look like.
  section.classList.remove("hidden");

  const fmt = v => v ? String(v).replace(/_/g, " ") : "—";

  if (!sp?.enabled) {
    set($("sec-pkg-meta"), "textContent", "Secondary carton analysis");
    set($("sec-pkg-findings"), "innerHTML",
      `<p style="color:var(--ink-mute);font-size:0.875rem;line-height:1.55">
        Secondary packaging stress zones will populate here once carton details
        are collected during intake.
      </p>`
    );
    return;
  }

  // Full data state.
  const transitLabel = Array.isArray(sp.transit_mode)
    ? sp.transit_mode.join(", ")
    : fmt(sp.transit_mode);

  set($("sec-pkg-meta"),
    "textContent",
    `${fmt(sp.carton_type)} · ${fmt(sp.board_type)} · ${sp.pack_count || "—"} packs per carton · ${fmt(sp.stacking_config)}`
  );

  const rows = [
    ["Carton type",      fmt(sp.carton_type)],
    ["Board grade",      fmt(sp.board_type)],
    ["Packs per carton", String(sp.pack_count || "—")],
    ["Stacking",         fmt(sp.stacking_config)],
    ["Transit modes",    transitLabel],
    ["Objective",        fmt(sp.goal)],
  ];

  const tableHtml = `<table class="opt-ledger" style="margin-bottom:12px">
    <tbody>${rows.map(([k, v]) =>
      `<tr><td style="color:var(--ink-mute);font-size:0.85rem;padding-right:16px">${escapeHtml(k)}</td>
           <td style="font-weight:500">${escapeHtml(v)}</td></tr>`
    ).join("")}</tbody>
  </table>`;

  const rec = sp.recommendation || "";
  const recHtml = rec
    ? `<p style="font-size:0.875rem;line-height:1.55;margin-top:10px">${escapeHtml(rec)}</p>`
    : "";

  set($("sec-pkg-findings"), "innerHTML", tableHtml + recHtml);

  // Populate live carton heatmap viewers if data is available.
  // after2Frames ensures the vs-canvas divs have non-zero layout before
  // Three.js tries to read their dimensions.
  if (cartonHeatmapState) {
    after2Frames(renderHeatmapIntoSecondaryPkg);
  }
}

// ── 3-Level Damage Analysis ───────────────────────────────────────────────
// Client storytelling feature — engineering placeholder visuals showing where
// damage occurs at each packaging level. No real FEA or simulation.

function _dmgColor(intensity) {
  return intensity === "high" ? "#e74c3c" : intensity === "medium" ? "#e67e22" : "#f1c40f";
}

function _dmgSvgCartonTop(intensity) {
  const c = _dmgColor(intensity);
  return '<svg viewBox="0 0 200 150" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:180px">'
    + '<rect x="10" y="45" width="180" height="98" rx="3" fill="#f5f7fb" stroke="#d0d4dc" stroke-width="1.5"/>'
    + '<rect x="10" y="45" width="180" height="20" fill="' + c + '22" stroke="' + c + '" stroke-width="1.2"/>'
    + '<line x1="55" y1="8" x2="55" y2="43" stroke="' + c + '" stroke-width="2"/>'
    + '<polygon points="50,43 60,43 55,51" fill="' + c + '"/>'
    + '<line x1="100" y1="5" x2="100" y2="43" stroke="' + c + '" stroke-width="2.5"/>'
    + '<polygon points="94,43 106,43 100,53" fill="' + c + '"/>'
    + '<line x1="145" y1="8" x2="145" y2="43" stroke="' + c + '" stroke-width="2"/>'
    + '<polygon points="140,43 150,43 145,51" fill="' + c + '"/>'
    + '<text x="100" y="75" text-anchor="middle" font-size="8" font-family="monospace" fill="' + c + '" letter-spacing="0.08em">COMPRESSION ZONE</text>'
    + '<text x="100" y="128" text-anchor="middle" font-size="8" font-family="monospace" fill="#9ca3af">TOP LOAD</text>'
    + '</svg>';
}

function _dmgSvgCartonCorner(intensity) {
  const c = _dmgColor(intensity);
  return '<svg viewBox="0 0 200 150" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:180px">'
    + '<rect x="18" y="15" width="164" height="118" rx="3" fill="#f5f7fb" stroke="#d0d4dc" stroke-width="1.5"/>'
    + '<circle cx="18" cy="15" r="16" fill="' + c + '22" stroke="' + c + '" stroke-width="1"/>'
    + '<circle cx="182" cy="15" r="16" fill="' + c + '22" stroke="' + c + '" stroke-width="1"/>'
    + '<circle cx="18" cy="133" r="20" fill="' + c + '44" stroke="' + c + '" stroke-width="1.5"/>'
    + '<circle cx="182" cy="133" r="20" fill="' + c + '44" stroke="' + c + '" stroke-width="1.5"/>'
    + '<text x="100" y="80" text-anchor="middle" font-size="8" font-family="monospace" fill="' + c + '" letter-spacing="0.08em">CORNER CRUSH</text>'
    + '<text x="100" y="128" text-anchor="middle" font-size="8" font-family="monospace" fill="#9ca3af">BASE CORNERS</text>'
    + '</svg>';
}

function _dmgSvgCartonSide(intensity) {
  const c = _dmgColor(intensity);
  return '<svg viewBox="0 0 200 150" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:180px">'
    + '<rect x="28" y="10" width="144" height="130" rx="3" fill="#f5f7fb" stroke="#d0d4dc" stroke-width="1.5"/>'
    + '<rect x="28" y="10" width="16" height="130" fill="' + c + '33" stroke="' + c + '" stroke-width="1"/>'
    + '<rect x="156" y="10" width="16" height="130" fill="' + c + '33" stroke="' + c + '" stroke-width="1"/>'
    + '<line x1="4" y1="75" x2="26" y2="75" stroke="' + c + '" stroke-width="2"/>'
    + '<polygon points="22,71 30,75 22,79" fill="' + c + '"/>'
    + '<line x1="196" y1="75" x2="174" y2="75" stroke="' + c + '" stroke-width="2"/>'
    + '<polygon points="178,71 170,75 178,79" fill="' + c + '"/>'
    + '<text x="100" y="80" text-anchor="middle" font-size="8" font-family="monospace" fill="' + c + '" letter-spacing="0.08em">PANEL DEFLECTION</text>'
    + '<text x="100" y="128" text-anchor="middle" font-size="8" font-family="monospace" fill="#9ca3af">SIDE WALL</text>'
    + '</svg>';
}

function _dmgSvgPacketSeal(intensity) {
  const c = _dmgColor(intensity);
  return '<svg viewBox="0 0 200 155" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:180px">'
    + '<path d="M28,22 Q28,10 50,10 L150,10 Q172,10 172,22 L172,138 Q172,150 150,150 L50,150 Q28,150 28,138 Z" fill="#f5f7fb" stroke="#d0d4dc" stroke-width="1.5"/>'
    + '<rect x="28" y="10" width="144" height="16" fill="' + c + '33" stroke="' + c + '" stroke-width="1"/>'
    + '<rect x="28" y="132" width="144" height="18" fill="' + c + '44" stroke="' + c + '" stroke-width="1.5"/>'
    + '<text x="100" y="87" text-anchor="middle" font-size="8" font-family="monospace" fill="' + c + '" letter-spacing="0.08em">SEAL STRESS ZONE</text>'
    + '<text x="100" y="120" text-anchor="middle" font-size="8" font-family="monospace" fill="#9ca3af">SEAL REGION</text>'
    + '</svg>';
}

function _dmgSvgPacketPuncture(intensity) {
  const c = _dmgColor(intensity);
  return '<svg viewBox="0 0 200 155" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:180px">'
    + '<path d="M28,22 Q28,10 50,10 L150,10 Q172,10 172,22 L172,138 Q172,150 150,150 L50,150 Q28,150 28,138 Z" fill="#f5f7fb" stroke="#d0d4dc" stroke-width="1.5"/>'
    + '<circle cx="72" cy="72" r="11" fill="' + c + '44" stroke="' + c + '" stroke-width="1.5"/>'
    + '<circle cx="128" cy="62" r="8" fill="' + c + '33" stroke="' + c + '" stroke-width="1"/>'
    + '<circle cx="100" cy="105" r="13" fill="' + c + '55" stroke="' + c + '" stroke-width="2"/>'
    + '<text x="100" y="143" text-anchor="middle" font-size="8" font-family="monospace" fill="' + c + '" letter-spacing="0.08em">PUNCTURE ZONES</text>'
    + '</svg>';
}

function _dmgSvgPacketBarrier(intensity) {
  const c = _dmgColor(intensity);
  return '<svg viewBox="0 0 200 155" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:180px">'
    + '<path d="M28,22 Q28,10 50,10 L150,10 Q172,10 172,22 L172,138 Q172,150 150,150 L50,150 Q28,150 28,138 Z" fill="#f5f7fb" stroke="#d0d4dc" stroke-width="1.5"/>'
    + '<line x1="28" y1="52" x2="172" y2="52" stroke="#4f9dff" stroke-width="1" stroke-dasharray="5,3"/>'
    + '<line x1="28" y1="82" x2="172" y2="82" stroke="' + c + '" stroke-width="1.5" stroke-dasharray="4,2"/>'
    + '<line x1="28" y1="112" x2="172" y2="112" stroke="#27ae60" stroke-width="1" stroke-dasharray="5,3"/>'
    + '<path d="M78,52 Q90,67 78,82 Q90,97 78,112" fill="none" stroke="' + c + '" stroke-width="2" stroke-dasharray="3,2"/>'
    + '<text x="100" y="140" text-anchor="middle" font-size="8" font-family="monospace" fill="' + c + '" letter-spacing="0.06em">LAMINATE FLEX</text>'
    + '</svg>';
}

function _dmgCartonCaptions(cs) {
  const stackH = parseInt(cs.carton_stack_height || "3") || 3;
  const board = (cs.carton_board_grade || "").toLowerCase();
  const modes = [].concat(cs.transit_modes || []).map(m => (m || "").toLowerCase());
  const hasShip = modes.some(m => m.includes("ship") || m.includes("sea") || m.includes("ocean"));
  const hasRail = modes.some(m => m.includes("rail"));
  const highStack = stackH >= 5;
  const weakBoard = board.includes("1") || board.includes("single") || board.includes("b-flute") || board.includes("b flute");
  return {
    topLoad: {
      intensity: highStack ? "high" : "medium",
      caption: highStack
        ? "Critical top-load compression detected. Pallet stacking height significantly increases top panel deflection risk."
        : "Moderate top-load compression under standard pallet configuration. Top panel integrity within handling margins.",
    },
    cornerCrush: {
      intensity: weakBoard ? "high" : "medium",
      caption: weakBoard
        ? "Corner crush risk elevated. Lower board grade reduces edge compression resistance during transit handling."
        : "Corner crush resistance adequate for declared transit modes. Base corner reinforcement reduces risk.",
    },
    sideWall: {
      intensity: (hasShip || hasRail) ? "high" : "medium",
      caption: hasShip
        ? "Side-wall compression risk increases during maritime transit. Prolonged vibration and humidity may weaken panel stiffness."
        : hasRail
        ? "Side-wall compression consistent with rail shock profile. Impact events may concentrate at lower panel edges."
        : "Side-wall compression within standard road handling limits.",
    },
  };
}

function _dmgPacketCaptions(cs) {
  const seal = (cs.seal_type || "").toLowerCase();
  const lam = (cs.laminate_structure || "").toUpperCase();
  const thick = parseInt(cs.total_thickness_micron) || 80;
  const weakSeal = seal.includes("fin") || seal.includes("centre") || seal.includes("center");
  const thinLam = thick < 80;
  const hasBarrier = lam.includes("EVOH") || lam.includes("MET") || lam.includes("ALU") || lam.includes("FOIL");
  return {
    seal: {
      intensity: weakSeal ? "high" : "medium",
      caption: weakSeal
        ? "Seal region shows elevated compression risk. Fin and centre-back seals are sensitive to peel forces under side loading."
        : "Seal region pressure within expected transit range. Seal integrity maintained under standard handling conditions.",
    },
    puncture: {
      intensity: thinLam ? "high" : "medium",
      caption: thinLam
        ? "Thin laminate increases puncture sensitivity. Sharp-edge contact during transit handling may compromise barrier integrity."
        : "Puncture resistance adequate for declared laminate thickness. Monitor corner and edge contact zones.",
    },
    barrier: {
      intensity: !hasBarrier ? "medium" : "low",
      caption: !hasBarrier
        ? "Barrier layer deformation risk under prolonged compression. WVTR and OTR performance may degrade over extended transit."
        : "Barrier layer integrity supported by metallised or EVOH structure. Delamination risk low under standard transit.",
    },
  };
}

function _dmgProductInfo(cs) {
  const cat = (cs.product_category || cs.product_type || "").toLowerCase();
  if (["liquid","beverage","drink","juice","sauce","oil","water"].some(t => cat.includes(t)))
    return { label: "Liquid / Beverage", primary: "Leakage risk increases under prolonged side compression. Seal integrity is the primary failure mode for liquid-fill packets.", risks: ["Leakage under sustained side compression", "Spillage during inversion drop orientations", "Seal failure may cause product exposure in long-haul transit"] };
  if (["chip","snack","crisp","puff","extruded","biscuit"].some(t => cat.includes(t)))
    return { label: "Fragile Snack", primary: "Internal product crushing risk under vertical compression. Nitrogen flush integrity may be compromised by seal deformation.", risks: ["Product crushing during pallet compression", "Nitrogen atmosphere compromised by seal stress", "Corner drops present highest fragmentation risk"] };
  if (["powder","dust","spice","flour","granule","instant"].some(t => cat.includes(t)))
    return { label: "Powder / Granule", primary: "Seal burst risk increases under internal pressure differential. Moisture ingress through barrier deformation may cause caking.", risks: ["Seal burst under internal pressure differential", "Caking from moisture ingress through barrier deformation", "Leakage through pinhole defects under extended vibration"] };
  if (["mosquito","repel","aerosol","spray","refill"].some(t => cat.includes(t)))
    return { label: "Aerosol / Refill", primary: "Leakage risk from valve or seal deformation under impact events. Internal product deformation possible during pallet stacking.", risks: ["Leakage from valve deformation under impact", "Product deformation under sustained compression", "Seal exposure risk during high-pressure transit events"] };
  if (["pharma","medic","tablet","capsule","pill","drug"].some(t => cat.includes(t)))
    return { label: "Pharmaceutical", primary: "Barrier compromise is critical for pharmaceutical products. Moisture ingress may accelerate degradation under extended transit.", risks: ["Contamination risk from barrier compromise", "Moisture sensitivity increases degradation risk", "Impact events may cause tablet fragmentation"] };
  if (["glass","jar"].some(t => cat.includes(t)))
    return { label: "Glass / Fragile", primary: "Glass breakage risk is the primary failure mode. Carton cushioning is critical to absorb drop impact energy.", risks: ["Primary breakage risk during drop events", "Secondary packaging critical for glass protection", "Impact energy transfers through carton wall to glass surface"] };
  return { label: "Product", primary: "Internal product deformation possible during pallet stacking. Seal instability may cause product exposure during extended transit.", risks: ["Internal deformation during pallet stacking", "Seal instability may cause product exposure during transit", "Distribution handling may cause surface damage"] };
}

// `forReport` = true → use static <img> assets (Report tab / no live Three.js)
// `forReport` = false (default, Results tab) → Level 3 uses real vs-canvas divs
//   that get populated by _populateMiniStrip() with the actual FEA heatmap data.
function _buildDamageHtml(snapshot, { forReport = false } = {}) {
  const cs = snapshot?.case_summary || {};
  const fam = _effectiveFamily();
  const hasSecondary = cs.has_secondary_carton === "yes";
  const parts = [];

  // Shared helper: static image card cell (Levels 1, 2, and report/fallback Level 3)
  const imgCell = (label, src, alt, cap) =>
    '<div class="vs-cell"><span class="vs-label">' + label + '</span>'
    + '<div style="background:var(--plate-2);border:1px solid var(--rule);border-radius:var(--r-sm);padding:var(--s-3);display:flex;align-items:center;justify-content:center;min-height:160px">'
    + '<img src="' + src + '" alt="' + escapeHtml(alt) + '" style="width:100%;max-width:210px;border-radius:4px;object-fit:contain" loading="lazy" />'
    + '</div>'
    + '<span class="vs-sub">' + escapeHtml(cap) + '</span></div>';

  // Shared helper: live Three.js viewer cell (Level 3 Results tab only)
  const viewerCell = (label, id, cap) =>
    '<div class="vs-cell"><span class="vs-label">' + label + '</span>'
    + '<div id="' + id + '" class="vs-canvas"></div>'
    + '<span class="vs-sub" id="' + id + '-sub">' + escapeHtml(cap) + '</span></div>';

  parts.push('<div class="vs-head"><span class="eyebrow">Damage Analysis · 3-level structural review</span></div>');

  // Level 1: Secondary packaging — only when has_secondary_carton == "yes"
  if (hasSecondary) {
    const c = _dmgCartonCaptions(cs);
    const useCartonViewers = !forReport && !!cartonHeatmapState;
    parts.push(
      '<div style="margin-bottom:var(--s-5)">'
      + '<div class="eyebrow" style="display:block;margin-bottom:var(--s-3)">Level 1 · Secondary Packaging Damage</div>'
      + '<div class="vs-row" style="grid-template-columns:1fr 1fr 1fr;gap:var(--s-4)">'
      + (useCartonViewers
          ? viewerCell("Top Load Compression",  "dmg-sec-viewer-top",    c.topLoad.caption)
          + viewerCell("Corner Crush",          "dmg-sec-viewer-corner", c.cornerCrush.caption)
          + viewerCell("Side Wall Compression", "dmg-sec-viewer-side",   c.sideWall.caption)
          : imgCell("Top Load Compression",  "/assets/top.png",    "Carton top-load stress zone",       c.topLoad.caption)
          + imgCell("Corner Crush",          "/assets/bottom.png", "Carton base corner stress zone",    c.cornerCrush.caption)
          + imgCell("Side Wall Compression", "/assets/side.png",   "Carton side panel deflection zone", c.sideWall.caption))
      + '</div></div>'
    );
  }

  // Level 2: Primary packaging — packet or bottle
  // Assets: /assets/primary%201.png  /assets/primary%202.png  /assets/primary%203.png
  parts.push('<div style="margin-bottom:var(--s-5)"><div class="eyebrow" style="display:block;margin-bottom:var(--s-3)">Level 2 · Primary Packaging Damage</div>');
  if (fam === "packet") {
    const p = _dmgPacketCaptions(cs);
    parts.push(
      '<div class="vs-row" style="grid-template-columns:1fr 1fr 1fr;gap:var(--s-4)">'
      + imgCell("Seal Stress Zone",      "/assets/primary%201.png", "Primary packaging seal stress",          p.seal.caption)
      + imgCell("Puncture Zone",         "/assets/primary%202.png", "Primary packaging puncture zone",        p.puncture.caption)
      + imgCell("Laminate Barrier Flex", "/assets/primary%203.png", "Primary packaging laminate flex region", p.barrier.caption)
      + '</div>'
    );
  } else {
    const ista = snapshot?.ista2a || {};
    const sfMin = (ista.drops || []).length
      ? Math.min(...(ista.drops || []).map(d => d.safety_factor || 99)).toFixed(2)
      : null;
    const sfNote = sfMin ? ' Min safety factor: ' + sfMin + '.' : '';
    parts.push(
      '<div class="vs-row" style="grid-template-columns:1fr 1fr 1fr;gap:var(--s-4)">'
      + imgCell("Top Impact Stress",    "/assets/primary%201.png", "Primary packaging top impact stress",  "Maximum stress concentration near top impact zone." + sfNote)
      + imgCell("Base & Corner Stress", "/assets/primary%202.png", "Primary packaging base corner stress", "Base corner stress zones identified under drop simulation. Refer to 3D heatmap viewers above.")
      + imgCell("Side Wall Stress",     "/assets/primary%203.png", "Primary packaging side wall stress",   "Sidewall compression and shear stress observed during transit simulation.")
      + '</div>'
    );
  }
  parts.push('</div>');

  // Level 3: Product damage
  //   Results tab (forReport=false) + heatmapState available → live FEA Three.js viewers
  //     same GLB + per-vertex colors as "Stress Heatmap · Drop Orientations" above
  //   Report tab or no heatmap → static /assets/top|bottom|side.png images
  const prod = _dmgProductInfo(cs);
  const useLiveViewers = !forReport && !!heatmapState;

  parts.push('<div><div class="eyebrow" style="display:block;margin-bottom:var(--s-3)">Level 3 · Product Damage · ' + escapeHtml(prod.label) + '</div>');
  parts.push('<div class="vs-row" style="grid-template-columns:1fr 1fr 1fr;gap:var(--s-4)">');

  if (useLiveViewers) {
    // Three.js viewer divs — populated by renderDamageAnalysis() after layout settles
    parts.push(viewerCell("Top-Down Drop",    "dmg-prod-viewer-top",    prod.risks[0] || prod.primary));
    parts.push(viewerCell("Bottom-Down Drop", "dmg-prod-viewer-bottom", prod.risks[1] || prod.primary));
    parts.push(viewerCell("Side Drop",        "dmg-prod-viewer-side",   prod.risks[2] || prod.primary));
  } else {
    // Static asset fallback (Report tab, PDF, or no analysis run yet)
    parts.push(imgCell("Top-Down Drop",    "/assets/top.png",    "Top-down drop product stress",    prod.risks[0] || prod.primary));
    parts.push(imgCell("Bottom-Down Drop", "/assets/bottom.png", "Bottom-down drop product stress", prod.risks[1] || prod.primary));
    parts.push(imgCell("Side Drop",        "/assets/side.png",   "Side drop product stress",        prod.risks[2] || prod.primary));
  }

  parts.push('</div>');
  parts.push('<p style="font-size:0.85rem;color:var(--ink-mute);margin-top:var(--s-3);line-height:1.5">' + escapeHtml(prod.primary) + '</p>');
  parts.push('</div>');

  return parts.join("");
}

function renderDamageAnalysis(snapshot) {
  const el = $("damage-analysis-section");
  if (!el) return;
  if (!DAMAGE_ANALYSIS_ENABLED) { el.classList.add("hidden"); return; }
  if (!snapshot?.case_summary) { el.classList.add("hidden"); return; }
  el.innerHTML = _buildDamageHtml(snapshot, { forReport: false });
  el.classList.remove("hidden");
  // Populate Level 1 carton viewers (after layout settles)
  if (cartonHeatmapState) {
    after2Frames(() => _populateMiniStrip("dmg-sec", cartonHeatmapState, [
      ["top",    "carton_top_load",    $("dmg-sec-viewer-top"),    null],
      ["corner", "carton_corner_crush",$("dmg-sec-viewer-corner"), null],
      ["side",   "carton_side_wall",   $("dmg-sec-viewer-side"),   null],
    ]));
  }
  // Populate Level 3 product FEA viewers (after layout settles)
  if (heatmapState) {
    after2Frames(() => _populateMiniStrip("dmg-prod", heatmapState, [
      ["top",    "drop_top",    $("dmg-prod-viewer-top"),    null],
      ["bottom", "drop_bottom", $("dmg-prod-viewer-bottom"), null],
      ["side",   "drop_side",   $("dmg-prod-viewer-side"),   null],
    ]));
  }
}

// ===================== Optimisation chat =====================
function optAppend(role, content) {
  if (role !== "thinking") clearOptThinking();
  appendMsg(role, content, { box: $("opt-messages") });
}

// Optimisation chat now has its own thinking line (separate from the main
// chat's #thinking-line). Updates from the same SSE bus when the active
// agent is the optimisation agent.
function surfaceOptThinking(text) {
  const box = $("opt-messages"); if (!box) return;
  let line = $("opt-thinking-line");
  if (!line) {
    line = document.createElement("div");
    line.id = "opt-thinking-line"; line.className = "msg thinking";
    box.appendChild(line);
  }
  line.innerHTML = `<em>${escapeHtml(text)}</em><span class="th-dots"><span></span><span></span><span></span></span>`;
  box.scrollTop = box.scrollHeight;
}
function clearOptThinking() { const l = $("opt-thinking-line"); if (l) l.remove(); }

// ── Bottle optimization send/generate (unchanged) ────────────────────────
// Resolve the authoritative packaging family from the backend. The local
// _effectiveFamily() can return null (and previously fell through to bottle);
// the server resolves it deterministically from the case_summary. Falls back
// to the local guess only if the lookup fails (e.g. offline).
async function _backendFamily() {
  if (!caseId) return _effectiveFamily();
  try {
    const r = await http(`/cases/${caseId}/family`);
    return r.family || _effectiveFamily();
  } catch (_) {
    return _effectiveFamily();
  }
}

async function optSend(textOverride) {
  if (!caseId) { optAppend("system", "Start a design first."); return; }
  // Prefer the backend-resolved family so packet/brush cases never fall
  // through to the bottle optimizer.
  const fam = await _backendFamily();
  // Route to packet optimizer when current case is a packet
  if (fam === "packet") { await pktOptSend(textOverride); return; }
  // Route to brush optimizer when current case is a brush
  if (fam === "brush") { await brushOptSend(textOverride); return; }
  if (!lastSnapshot?.report) { optAppend("system", "Run the analysis first — no baseline to optimise against."); return; }
  const text = (textOverride ?? $("opt-input").value).trim();
  if (!text) return;
  $("opt-input").value = "";
  optAppend("user", text);
  if (!optIntent) {
    progressBegin("Reading goal", 1);
    surfaceOptThinking("Reading your optimisation goal");
    try {
      const r = await http(`/cases/${caseId}/optimize/intent`, {
        method: "POST", body: JSON.stringify({ message: text }) });
      optAppend("assistant", r.reply);
      if (r.ready_to_generate && r.intent) {
        optIntent = r.intent;
        await optGenerate(r.intent, r.intent_notes || "");
      }
    } catch (e) { optAppend("system", "Error: " + e.message); }
    finally { progressEnd(); }
  } else { await optGenerate(optIntent, text); }
}
async function optGenerate(intent, notes) {
  progressBegin("Generating three passing alternatives", 4);
  surfaceOptThinking("Searching the design space for variants that all pass ISTA");
  try {
    const r = await http(`/cases/${caseId}/optimize/run`, {
      method: "POST", body: JSON.stringify({ intent, intent_notes: notes }) });
    lastOptResult = r;
    // Bridge the result onto window so non-module scripts (auth-ui.js's
    // PCR Intelligence metric strip) can read it without importing.
    try { window.lastOptResult = r; } catch (_) {}
    optAppend("assistant", r.narrative || "Three alternatives ready — all pass ISTA 2A.");
    renderOptDashboard(r);
    // Build the per-variant 3D comparison panel
    renderOptCompare(r);
  } catch (e) { optAppend("system", "Optimisation error: " + e.message); }
  finally { progressEnd(); }
}

// ── Packet optimization send/generate ────────────────────────────────────
async function pktOptSend(textOverride) {
  if (!caseId) { optAppend("system", "Start a design first."); return; }
  if (!lastSnapshot?.report) { optAppend("system", "Run the analysis first — no baseline to optimise against."); return; }
  const text = (textOverride ?? $("opt-input").value).trim();
  if (!text) return;
  $("opt-input").value = "";
  optAppend("user", text);
  if (!pktOptIntent) {
    progressBegin("Reading goal", 1);
    surfaceOptThinking("Reading your flexible packet optimisation goal");
    try {
      const r = await http(`/cases/${caseId}/packet-optimize/intent`, {
        method: "POST", body: JSON.stringify({ message: text }) });
      optAppend("assistant", r.reply);
      if (r.ready_to_generate && r.intent) {
        pktOptIntent = r.intent;
        await pktOptGenerate(r.intent, r.intent_notes || "");
      }
    } catch (e) { optAppend("system", "Error: " + e.message); }
    finally { progressEnd(); }
  } else { await pktOptGenerate(pktOptIntent, text); }
}
async function pktOptGenerate(intent, notes) {
  progressBegin("Generating three flexible packet alternatives", 3);
  surfaceOptThinking("Evaluating laminate, seal, and carton combinations…");
  try {
    const r = await http(`/cases/${caseId}/packet-optimize/run`, {
      method: "POST", body: JSON.stringify({ intent, intent_notes: notes }) });
    lastPktOptResult = r;
    optAppend("assistant", r.narrative || "Three flexible packet alternatives ready — compare in the ledger below.");
    renderPacketOptDashboard(r);
    after2Frames(() => renderPacketOptCompare(r));
  } catch (e) { optAppend("system", "Flexible packet optimisation error: " + e.message); }
  finally { progressEnd(); }
}
// Spider/radar chart was removed per spec — the comparison ledger covers
// the same axes more readably. Stub kept so legacy callers don't break.
function _renderOptDashboardWithSpider(_result) { /* no-op */ }

function renderOptDashboard(result) {
  cls($("opt-dashboard"), "remove", "hidden");
  const rows = result.comparison_rows || [];
  const fmt = v => v == null ? "—" : (typeof v === "number" ? v.toLocaleString(undefined,{maximumFractionDigits:2}) : v);
  const delta = (v, goodWhen) => {
    if (v == null || v === 0) return `<span class="opt-delta flat">—</span>`;
    const isGood = (goodWhen === "down" && v < 0) || (goodWhen === "up" && v > 0);
    const arrow = v > 0 ? "▲" : "▼";
    return `<span class="opt-delta ${isGood ? "up" : "down"}">${arrow} ${Math.abs(v)}%</span>`;
  };
  const body = rows.map(r => {
    const isBaseline = r.name === "Original";
    // PCR / sustainability badge — green for any post-consumer recycled
    // material in the alternatives. The first alternative is always a PCR
    // swap, so this is normally on the first non-baseline row.
    const pcrBadge = (!isBaseline && (r.is_pcr || (r.material || "").toUpperCase().startsWith("PCR-")))
      ? `<span class="pcr-badge" title="${(r.recycled_content_pct || 100).toFixed(0)}% post-consumer recycled · ~${r.carbon_intensity_kg_co2e_per_kg ?? "—"} kg CO₂e/kg">PCR</span>`
      : "";
    return `<tr class="${isBaseline ? "opt-baseline" : "opt-row"} ${r.is_pcr ? "opt-row--pcr" : ""}">
      <td>
        <span class="opt-name">${escapeHtml(r.name)}${pcrBadge}</span>
        <span class="opt-name-meta">${escapeHtml(r.material || "—")} · ${r.wall_thickness_mm ?? "—"} mm wall</span>
      </td>
      <td><span class="opt-num">${fmt(r.mass_g)} g</span></td>
      <td><span class="opt-num">$${fmt(r.cost_per_unit)}</span>${isBaseline ? "" : delta(r.cost_delta_pct, "down")}</td>
      <td><span class="opt-num">${fmt(r.min_safety_factor)}</span>${isBaseline ? "" : delta(r.sf_delta_pct, "up")}</td>
      <td>${isBaseline ? "—" : (r.roi_pct ? `<span class="opt-num">${fmt(r.roi_pct)}%</span>` : "—")}</td>
      <td><span class="opt-verdict-cell ${r.passes_ista ? 'pass' : 'fail'}">${r.passes_ista ? "PASS" : "FAIL"}</span></td>
      <td><span class="opt-rationale">${escapeHtml(r.rationale || (isBaseline ? "Baseline" : ""))}</span></td>
    </tr>`;
  }).join("");
  set($("opt-dashboard-body"), "innerHTML", `
    <table class="opt-ledger">
      <thead><tr><th>Design</th><th>Mass</th><th>Unit cost</th><th>Min SF</th><th>ROI</th><th>ISTA</th><th>Rationale</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`);
}

// ── Packet optimization comparison ledger ────────────────────────────────
// Mirrors renderOptDashboard() exactly in DOM/CSS structure but uses
// packet-specific columns (laminate, thickness, seal, transit/barrier scores,
// cost impact %) instead of bottle columns (material, wall, ISTA SF, ROI).
function renderPacketOptDashboard(result) {
  cls($("opt-dashboard"), "remove", "hidden");
  const rows = result.comparison_rows || [];
  const fmt = v => v == null ? "—" : (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : v);
  const score = v => v == null ? "—" : `<span class="opt-num">${parseFloat(v).toFixed(1)}/10</span>`;
  const costDelta = v => {
    if (v == null || v === 0) return `<span class="opt-delta flat">—</span>`;
    const isGood = v < 0;
    const arrow = v > 0 ? "▲" : "▼";
    return `<span class="opt-delta ${isGood ? "up" : "down"}">${arrow} ${Math.abs(v).toFixed(1)}%</span>`;
  };
  const body = rows.map(r => {
    const isBaseline = r.is_baseline === true || r.name === "Original";
    const sealLabel = (r.seal_type || "—").replace(/_/g, " ");
    const cartonLabel = (r.carton || "none").replace(/_/g, " ");
    return `<tr class="${isBaseline ? "opt-baseline" : "opt-row"}">
      <td>
        <span class="opt-name">${escapeHtml(r.name)}</span>
        <span class="opt-name-meta">${escapeHtml(r.laminate || "—")} · ${fmt(r.thickness_micron)} μm</span>
      </td>
      <td><span class="opt-num">${escapeHtml(sealLabel)}</span></td>
      <td><span class="opt-num">${escapeHtml(cartonLabel)}</span></td>
      <td>${score(r.seal_score)}</td>
      <td>${score(r.transit_score)}</td>
      <td>${score(r.barrier_score)}</td>
      <td>${score(r.puncture_score)}</td>
      <td>${isBaseline ? "—" : costDelta(r.cost_impact_pct)}</td>
      <td><span class="opt-rationale">${escapeHtml(r.rationale || (isBaseline ? "Baseline" : ""))}</span></td>
    </tr>`;
  }).join("");
  set($("opt-dashboard-body"), "innerHTML", `
    <table class="opt-ledger">
      <thead><tr><th>Design</th><th>Seal Type</th><th>Carton</th><th>Seal</th><th>Transit</th><th>Barrier</th><th>Puncture</th><th>Cost Δ</th><th>Rationale</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`);
}

// ── Brush optimization send/generate ─────────────────────────────────────
let brushOptIntent = null;
async function brushOptSend(textOverride) {
  if (!caseId) { optAppend("system", "Start a design first."); return; }
  if (!lastSnapshot?.report) { optAppend("system", "Run the analysis first — no baseline to optimise against."); return; }
  const text = (textOverride ?? $("opt-input").value).trim();
  if (!text) return;
  $("opt-input").value = "";
  optAppend("user", text);
  if (!brushOptIntent) {
    progressBegin("Reading goal", 1);
    surfaceOptThinking("Reading your brush packaging optimisation goal");
    try {
      const r = await http(`/cases/${caseId}/brush-optimize/intent`, {
        method: "POST", body: JSON.stringify({ message: text }) });
      optAppend("assistant", r.reply);
      if (r.ready_to_generate && r.intent) {
        brushOptIntent = r.intent;
        await brushOptGenerate(r.intent, r.intent_notes || "");
      }
    } catch (e) { optAppend("system", "Error: " + e.message); }
    finally { progressEnd(); }
  } else { await brushOptGenerate(brushOptIntent, text); }
}
async function brushOptGenerate(intent, notes) {
  progressBegin("Generating three brush packaging alternatives", 3);
  surfaceOptThinking("Evaluating blister, material, and carton combinations…");
  try {
    const r = await http(`/cases/${caseId}/brush-optimize/run`, {
      method: "POST", body: JSON.stringify({ intent, intent_notes: notes }) });
    lastBrushOptResult = r;
    optAppend("assistant", r.narrative || "Three brush packaging alternatives ready — compare in the ledger below.");
    renderBrushOptDashboard(r);
    after2Frames(() => renderBrushOptCompare(r));
  } catch (e) { optAppend("system", "Brush packaging optimisation error: " + e.message); }
  finally { progressEnd(); }
}

// ── Brush optimization comparison ledger ──────────────────────────────────
// Mirrors renderPacketOptDashboard() in DOM/CSS structure but uses
// brush-specific columns (primary pack, material, carton, blister/transit/
// material/compression scores, cost impact %).
function renderBrushOptDashboard(result) {
  cls($("opt-dashboard"), "remove", "hidden");
  const rows = result.comparison_rows || [];
  const fmt = v => v == null ? "—" : (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : v);
  const score = v => v == null ? "—" : `<span class="opt-num">${parseFloat(v).toFixed(1)}/10</span>`;
  const costDelta = v => {
    if (v == null || v === 0) return `<span class="opt-delta flat">—</span>`;
    const isGood = v < 0;
    const arrow = v > 0 ? "▲" : "▼";
    return `<span class="opt-delta ${isGood ? "up" : "down"}">${arrow} ${Math.abs(v).toFixed(1)}%</span>`;
  };
  const body = rows.map(r => {
    const isBaseline = r.is_baseline === true || r.name === "Original";
    const packLabel   = (r.primary_pack || "—").replace(/_/g, " ");
    const cartonLabel = (r.carton || "none").replace(/_/g, " ");
    return `<tr class="${isBaseline ? "opt-baseline" : "opt-row"}">
      <td>
        <span class="opt-name">${escapeHtml(r.name)}</span>
        <span class="opt-name-meta">${escapeHtml(packLabel)} · ${escapeHtml(r.material || "—")}</span>
      </td>
      <td><span class="opt-num">${escapeHtml(cartonLabel)}</span></td>
      <td>${score(r.blister_score)}</td>
      <td>${score(r.transit_score)}</td>
      <td>${score(r.material_score)}</td>
      <td>${score(r.compression_score)}</td>
      <td>${isBaseline ? "—" : costDelta(r.cost_impact_pct)}</td>
      <td><span class="opt-rationale">${escapeHtml(r.rationale || (isBaseline ? "Baseline" : ""))}</span></td>
    </tr>`;
  }).join("");
  set($("opt-dashboard-body"), "innerHTML", `
    <table class="opt-ledger">
      <thead><tr><th>Design</th><th>Carton</th><th>Blister</th><th>Transit</th><th>Material</th><th>Compression</th><th>Cost Δ</th><th>Rationale</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`);
}

// ── Packet / brush opt compare plate (3D bare-mesh viewers) ─────────────
// Mirrors renderOptCompare / paintOptCompareScenes for bottles, but:
//  • loads the bare uploaded mesh (no ISTA stress heatmap) since ISTA 2A
//    simulation is bottle-specific
//  • derives the "score" badge from the packet/brush dimension scores
//  • "Open full page →" routes to #/variant/N which renderPacketVariantPage
//    and renderBrushVariantPage handle

function buildPktOptCell(r, altIdx) {
  const div = document.createElement("div");
  div.className = "opt-compare-cell" + (r.is_baseline ? " baseline" : "");
  div.dataset.altName = r.name;
  const avg = r.is_baseline ? null
    : ((r.seal_score + r.transit_score + r.barrier_score + r.puncture_score) / 4);
  div.innerHTML = `
    <div class="occ-name">${escapeHtml(r.name)}</div>
    <div class="occ-mat">${escapeHtml(r.laminate || "—")} · ${r.thickness_micron ?? "—"} µm</div>
    <div class="occ-canvas" data-occ-canvas></div>
    <div class="occ-foot">
      <span>Avg score</span><span>${avg != null ? avg.toFixed(1) + "/10" : "—"}</span>
    </div>
    ${!r.is_baseline ? `<div style="margin-top:8px"><a class="ghost ghost--xs" href="#/variant/${altIdx}">Open full page →</a></div>` : ""}
  `;
  return div;
}

function buildBrushOptCell(r, altIdx) {
  const div = document.createElement("div");
  div.className = "opt-compare-cell" + (r.is_baseline ? " baseline" : "");
  div.dataset.altName = r.name;
  const avg = r.is_baseline ? null
    : ((r.blister_score + r.transit_score + r.material_score + r.compression_score) / 4);
  div.innerHTML = `
    <div class="occ-name">${escapeHtml(r.name)}</div>
    <div class="occ-mat">${escapeHtml((r.primary_pack || "—").replace(/_/g," "))} · ${escapeHtml(r.material || "—")}</div>
    <div class="occ-canvas" data-occ-canvas></div>
    <div class="occ-foot">
      <span>Avg score</span><span>${avg != null ? avg.toFixed(1) + "/10" : "—"}</span>
    </div>
    ${!r.is_baseline ? `<div style="margin-top:8px"><a class="ghost ghost--xs" href="#/variant/${altIdx}">Open full page →</a></div>` : ""}
  `;
  return div;
}

async function paintBareOptCompareScenes() {
  if (!lastMeshUrl) return;
  const cells = $("opt-compare-row").querySelectorAll(".opt-compare-cell");
  for (const cell of cells) {
    const canvas = cell.querySelector("[data-occ-canvas]");
    if (!canvas || !canvas.offsetWidth || !canvas.offsetHeight) continue;
    const key = `optcompare-${cell.dataset.altName}`;
    if (!miniViewers[key]) miniViewers[key] = makeMiniViewer(canvas, { autoRotate: false });
    else miniViewers[key].resize();
    try { await miniViewers[key].loadGlb(lastMeshUrl); } catch (_) {}
  }
}

async function renderPacketOptCompare(result) {
  if (!result?.comparison_rows?.length || !lastMeshUrl) return;
  const plate = $("opt-compare-plate"); if (!plate) return;
  plate.removeAttribute("hidden");

  const row = $("opt-compare-row");
  Object.keys(miniViewers).filter(k => k.startsWith("optcompare-")).forEach(k => delete miniViewers[k]);
  set(row, "innerHTML", "");

  const baseline = result.comparison_rows.find(r => r.is_baseline);
  if (baseline) row.appendChild(buildPktOptCell(baseline, -1));
  result.comparison_rows.filter(r => !r.is_baseline)
    .forEach((r, idx) => row.appendChild(buildPktOptCell(r, idx)));

  await paintBareOptCompareScenes();
}

async function renderBrushOptCompare(result) {
  if (!result?.comparison_rows?.length || !lastMeshUrl) return;
  const plate = $("opt-compare-plate"); if (!plate) return;
  plate.removeAttribute("hidden");

  const row = $("opt-compare-row");
  Object.keys(miniViewers).filter(k => k.startsWith("optcompare-")).forEach(k => delete miniViewers[k]);
  set(row, "innerHTML", "");

  const baseline = result.comparison_rows.find(r => r.is_baseline);
  if (baseline) row.appendChild(buildBrushOptCell(baseline, -1));
  result.comparison_rows.filter(r => !r.is_baseline)
    .forEach((r, idx) => row.appendChild(buildBrushOptCell(r, idx)));

  await paintBareOptCompareScenes();
}

// ── Packet / brush full variant pages ────────────────────────────────────
// Reuses the existing #variant stage page. Scorecard shows dimension scores
// instead of ISTA drop orientations. Mesh viewer shows the bare uploaded
// geometry (no heatmap coloring — stress simulation is bottle-only).

async function renderPacketVariantPage(result, idx) {
  const alts = (result.comparison_rows || []).filter(r => !r.is_baseline);
  const r = alts[idx]; if (!r) return;

  set($("variant-title"), "textContent", r.name || `Variant ${idx + 1}`);
  set($("variant-sub"), "textContent",
    `${r.laminate || "—"} · ${r.thickness_micron ?? "—"} µm · ${(r.seal_type || "").replace(/_/g," ")} seal.`);

  const sc = $("variant-scorecard");
  sc.classList.remove("pass","fail"); sc.classList.add("pass");
  set(sc, "innerHTML", `
    <span class="eyebrow scorecard-eyebrow">Variant scores</span>
    <div class="scorecard-headline">Laminate performance</div>
    <div class="scorecard-meta">
      Seal ${(r.seal_score ?? 0).toFixed(1)}/10 &nbsp;·&nbsp;
      Transit ${(r.transit_score ?? 0).toFixed(1)}/10 &nbsp;·&nbsp;
      Barrier ${(r.barrier_score ?? 0).toFixed(1)}/10 &nbsp;·&nbsp;
      Puncture ${(r.puncture_score ?? 0).toFixed(1)}/10
    </div>
    <div class="orientation-row">
      ${[
        {k:"Seal integrity",     v:r.seal_score},
        {k:"Transit durability", v:r.transit_score},
        {k:"Barrier performance",v:r.barrier_score},
        {k:"Puncture resistance",v:r.puncture_score},
      ].map(s => `
        <div class="or-item ${(s.v||0)>=7?"or-pass":"or-fail"}">
          <span class="or-name">${s.k}</span>
          <div class="or-sf">${s.v?.toFixed(1)??"—"}/10</div>
          <span class="or-verdict">${(s.v||0)>=7?"good":"marginal"}</span>
        </div>`).join("")}
    </div>
  `);

  if (lastMeshUrl) {
    try { await populateBareMeshIntoStrip("variant", VARIANT_CELLS()); } catch (_) {}
  }

  if (!lastTransitCharts?.truck) {
    try {
      const cs = lastSnapshot?.case_summary || {};
      lastTransitCharts = await http(
        `/transit/charts?road=${encodeURIComponent(cs.road_condition || "mixed")}&modes=truck&max_points=8000`
      );
    } catch (_) {}
  }
  if (lastTransitCharts?.truck) {
    drawLineChart($("variant-chart-truck"), {
      x: lastTransitCharts.truck.t_hours, y: lastTransitCharts.truck.vibration_g,
      color: "#0072bb", fill: "rgba(0,114,187,0.10)",
      yLabel: "Vibration (g)", xLabel: "Time (hours)",
      baseline: { y: 0.54, label: "truck PSD baseline" }, hoverLabel: "Vibration (g)",
    });
  }

  const lines = [];
  if (r.carton) lines.push(`Carton: <strong>${escapeHtml(r.carton.replace(/_/g," "))}</strong>.`);
  if (r.cost_impact_pct != null)
    lines.push(`Cost delta: <strong>${r.cost_impact_pct > 0 ? "+" : ""}${r.cost_impact_pct.toFixed(1)}%</strong> vs baseline.`);
  if (r.rationale) lines.push(`<em>${escapeHtml(r.rationale)}</em>`);
  set($("variant-summary"), "innerHTML",
    `<span class="eyebrow">Why this variant</span>${lines.map(l => `<p>${l}</p>`).join("")}`);
}

async function renderBrushVariantPage(result, idx) {
  const alts = (result.comparison_rows || []).filter(r => !r.is_baseline);
  const r = alts[idx]; if (!r) return;

  set($("variant-title"), "textContent", r.name || `Variant ${idx + 1}`);
  set($("variant-sub"), "textContent",
    `${(r.primary_pack || "—").replace(/_/g," ")} · ${r.material || "—"}.`);

  const sc = $("variant-scorecard");
  sc.classList.remove("pass","fail"); sc.classList.add("pass");
  set(sc, "innerHTML", `
    <span class="eyebrow scorecard-eyebrow">Variant scores</span>
    <div class="scorecard-headline">Brush packaging performance</div>
    <div class="scorecard-meta">
      Blister ${(r.blister_score ?? 0).toFixed(1)}/10 &nbsp;·&nbsp;
      Transit ${(r.transit_score ?? 0).toFixed(1)}/10 &nbsp;·&nbsp;
      Material ${(r.material_score ?? 0).toFixed(1)}/10 &nbsp;·&nbsp;
      Compression ${(r.compression_score ?? 0).toFixed(1)}/10
    </div>
    <div class="orientation-row">
      ${[
        {k:"Blister integrity",     v:r.blister_score},
        {k:"Transit durability",    v:r.transit_score},
        {k:"Material suitability",  v:r.material_score},
        {k:"Compression resistance",v:r.compression_score},
      ].map(s => `
        <div class="or-item ${(s.v||0)>=7?"or-pass":"or-fail"}">
          <span class="or-name">${s.k}</span>
          <div class="or-sf">${s.v?.toFixed(1)??"—"}/10</div>
          <span class="or-verdict">${(s.v||0)>=7?"good":"marginal"}</span>
        </div>`).join("")}
    </div>
  `);

  if (lastMeshUrl) {
    try { await populateBareMeshIntoStrip("variant", VARIANT_CELLS()); } catch (_) {}
  }

  if (!lastTransitCharts?.truck) {
    try {
      const cs = lastSnapshot?.case_summary || {};
      lastTransitCharts = await http(
        `/transit/charts?road=${encodeURIComponent(cs.road_condition || "mixed")}&modes=truck&max_points=8000`
      );
    } catch (_) {}
  }
  if (lastTransitCharts?.truck) {
    drawLineChart($("variant-chart-truck"), {
      x: lastTransitCharts.truck.t_hours, y: lastTransitCharts.truck.vibration_g,
      color: "#0072bb", fill: "rgba(0,114,187,0.10)",
      yLabel: "Vibration (g)", xLabel: "Time (hours)",
      baseline: { y: 0.54, label: "truck PSD baseline" }, hoverLabel: "Vibration (g)",
    });
  }

  const lines = [];
  if (r.carton) lines.push(`Carton: <strong>${escapeHtml(r.carton.replace(/_/g," "))}</strong>.`);
  if (r.cost_impact_pct != null)
    lines.push(`Cost delta: <strong>${r.cost_impact_pct > 0 ? "+" : ""}${r.cost_impact_pct.toFixed(1)}%</strong> vs baseline.`);
  if (r.rationale) lines.push(`<em>${escapeHtml(r.rationale)}</em>`);
  set($("variant-summary"), "innerHTML",
    `<span class="eyebrow">Why this variant</span>${lines.map(l => `<p>${l}</p>`).join("")}`);
}

// ── Toggle optimise UI between bottle, packet, and brush modes ────────────
function _applyOptimiseUiForFamily(family) {
  const isPacket = family === "packet";
  const isBrush  = family === "brush";
  const bottleIntents = $("opt-intents-bottle");
  const packetIntents = $("opt-intents-packet");
  const brushIntents  = $("opt-intents-brush");
  const pcrIntel = $("pcr-intel");
  const comparePlate = $("opt-compare-plate");
  const pageSub = $("opt-page-sub");
  if (bottleIntents) bottleIntents.hidden = isPacket || isBrush;
  if (packetIntents) packetIntents.hidden = !isPacket;
  if (brushIntents)  brushIntents.hidden  = !isBrush;
  if (pcrIntel) pcrIntel.hidden = isPacket || isBrush;
  if (comparePlate) comparePlate.hidden = true;  // always reset; bottle code shows it when results arrive
  if (pageSub) {
    if (isPacket) pageSub.textContent = "Pick an intent — cost, survivability, or shelf life — and the optimiser proposes three flexible packet alternatives evaluated on seal integrity, transit durability, and barrier performance.";
    else if (isBrush) pageSub.textContent = "Pick an intent — cost, survivability, or sustainability — and the optimiser proposes three brush packaging alternatives evaluated on blister integrity, transit durability, material suitability, and compression resistance.";
    else pageSub.textContent = "Pick an intent — cost, strength, or your own — and the optimiser proposes three alternatives, each re-evaluated through the same ISTA 2A pipeline.";
  }
}

// ===================== Geometry upload modal =====================
//
// The geometry upload is REQUIRED — there is no skip path. The modal opens
// (a) when the bottle-flow agent emits `asking:has_geometry` and (b) when the
// user lands on #/geometry without an uploaded mesh. The chat composer's
// upload icon also pulses while this question is the live ask.

const geomModal = $("geom-modal");
let geomModalShownThisCase = false;

function openGeomModal() {
  if (!geomModal) return;
  cls(geomModal, "remove", "hidden");
  geomModalShownThisCase = true;
  pulseUploadAffordance(true);
}
function closeGeomModal() {
  cls(geomModal, "add", "hidden");
  // Pulse keeps running on the inline composer button so the user knows
  // the field is still owed even after they dismiss the modal.
}

// Pulses the chat composer's upload icon to point the user at the upload
// affordance whenever the bot is actively asking for the geometry file.
function pulseUploadAffordance(on) {
  const btn = $("upload-btn");
  if (!btn) return;
  btn.classList.toggle("pulse-needs-upload", !!on);
}

// Wire modal events
$("modal-browse").addEventListener("click", () => fileEl.click());
// geom-skip removed — geometry upload is required, no skip path.
$("geom-modal-close").addEventListener("click", closeGeomModal);

// Packaging type selector — landing page [ Bottle ] / [ Packet ] cards.
// Upload modal only opens AFTER the user selects a packaging family.
async function selectPackagingFamily(family) {
  // Set state immediately so it survives any concurrent newCase() that may
  // be in flight (page-load auto-call). resetSelection:false ensures the
  // in-flight or about-to-run newCase() does not wipe our selection.
  selectedPackagingFamily = family;
  document.querySelectorAll(".pkg-card").forEach(c =>
    c.classList.toggle("selected", c.dataset.family === family));

  if (!caseId) {
    try { await newCase({ resetSelection: false }); } catch (e) {
      appendMsg("system", `Could not start a design: ${e.message}`);
      return;
    }
  }

  // Re-assert after newCase() in case the await unlocked a concurrent call
  // that could theoretically touch the DOM (defensive, usually a no-op).
  selectedPackagingFamily = family;
  document.querySelectorAll(".pkg-card").forEach(c =>
    c.classList.toggle("selected", c.dataset.family === family));

  try {
    await http(`/cases/${caseId}/brief`, {
      method: "PATCH",
      body: JSON.stringify({ updates: { packaging_family: family } }),
    });
  } catch (e) { console.warn("packaging_family PATCH failed:", e); }
  // Brush geometry is OPTIONAL — open the modal as a soft invitation so the user
  // CAN upload a CAD file if they have one, but the flow doesn't block if they don't.
  // Bottle and packet follow the same pattern; the flow always accepts a skip/close.
  openGeomModal();
  // For brush: if the user closes the modal without uploading, the chat flow
  // starts immediately when they type their first message (same as packet flow).
}
document.querySelectorAll(".pkg-card").forEach(card => {
  card.addEventListener("click", () => selectPackagingFamily(card.dataset.family));
});
const modalDz = $("modal-dropzone");
modalDz.addEventListener("dragover", e => { e.preventDefault(); modalDz.classList.add("dragover"); });
modalDz.addEventListener("dragleave", () => modalDz.classList.remove("dragover"));
modalDz.addEventListener("drop", async e => {
  e.preventDefault(); modalDz.classList.remove("dragover");
  if (e.dataTransfer.files[0]) {
    await uploadFile(e.dataTransfer.files[0]);
    closeGeomModal();
    if (currentRoute() === "geometry") location.hash = "#/material";
  }
});
modalDz.addEventListener("click", () => fileEl.click());

// ===================== Custom material =====================
//
// Optional escape hatch for materials we don't have data for. Form is a
// modal in the Material stage; on Save we POST to /materials/custom (which
// caches it locally) and PATCH the brief with the new material name so the
// rest of the pipeline picks it up.

function openCustomMatModal() { cls($("custom-mat-modal"), "remove", "hidden"); }
function closeCustomMatModal() { cls($("custom-mat-modal"), "add", "hidden"); }

async function saveCustomMaterial() {
  const name = $("cm-name").value.trim();
  if (!name) { alert("Material name is required."); return; }
  const body = {
    name,
    density_kg_m3: parseFloat($("cm-density").value) || 1380,
    modulus_gpa:   parseFloat($("cm-modulus").value) || 2.8,
    yield_strength_mpa:    parseFloat($("cm-yield").value)   || 55,
    allowable_stress_mpa:  parseFloat($("cm-allow").value)   || 35,
    notes: $("cm-notes").value.trim() || null,
  };
  try {
    await http("/materials/custom", { method: "POST", body: JSON.stringify(body) });
    if (caseId) {
      await http(`/cases/${caseId}/brief`, {
        method: "PATCH", body: JSON.stringify({ updates: { material: name } }),
      });
      await refreshBrief();
      renderMaterialStage();
      refreshCostTile();
    }
    appendMsg("system", `Custom material "${name}" saved and applied to this design.`);
    closeCustomMatModal();
  } catch (e) { alert("Save failed: " + e.message); }
}

// Spider/radar chart implementation removed per spec — the comparison
// ledger covers the same axes more readably and the material radar in the
// report was replaced by a property table.

// ── Per-variant detail page ──────────────────────────────────────────────
// One dedicated page per alternative, accessible from "Open full page →" in
// the comparison row. Shows the verdict scorecard for that variant, three
// mini stress heatmaps (top/bottom/side), a transit chart, and a short
// summary of what changed vs the baseline.

async function renderVariantPage(result, idx) {
  const alt = result.alternatives?.[idx]; if (!alt) return;
  const matName = alt.material?.name || alt.fields?.material || "—";
  const wall = alt.fields?.wall_thickness_mm ?? "—";
  set($("variant-title"), "textContent", alt.name || `Variant ${idx + 1}`);
  set($("variant-sub"), "textContent",
    `${matName} · ${wall} mm wall · ${alt.fields?.closure_type?.replace(/_/g," ") || "default closure"}.`);

  // Scorecard with verdict pulled straight from the ISTA report
  const ista = alt.ista_report || {};
  const verdict = (ista.overall_verdict || (alt.passes_ista ? "pass" : "fail")).toLowerCase();
  const word = verdict === "pass" ? "Pass." : "Fail.";
  const passing = (ista.drops || []).filter(d => d.verdict === "pass").length;
  const sc = $("variant-scorecard");
  sc.classList.remove("pass", "fail");
  sc.classList.add(verdict === "pass" ? "pass" : "fail");
  set(sc, "innerHTML", `
    <span class="eyebrow scorecard-eyebrow">Variant verdict</span>
    <div class="scorecard-headline">${word}</div>
    <div class="scorecard-meta">
      ${passing} of ${(ista.drops || []).length} drop orientations cleared.<br>
      Min safety factor ${alt.min_safety_factor ?? "—"} · unit cost $${(alt.cost_per_unit ?? 0).toFixed?.(3) ?? alt.cost_per_unit}.
    </div>
    <div class="orientation-row">
      ${(ista.drops || []).map(d => {
        const cls = d.verdict === "pass" ? "or-pass" : "or-fail";
        return `<div class="or-item ${cls}">
          <span class="or-name">Drop · ${d.orientation}</span>
          <div class="or-sf">${d.safety_factor ?? "—"}</div>
          <div class="or-data">
            <span>v</span><span>${d.impact_velocity_m_s} m/s</span>
            <span>σ</span><span>${d.impact_pressure_mpa} MPa</span>
            <span>K_t</span><span>${d.stress_concentration_kt}</span>
          </div>
          <span class="or-verdict">${(d.verdict || "").replace(/_/g, " ")}</span>
        </div>`;
      }).join("")}
    </div>
  `);

  // Mini-viewers: fetch this variant's heatmap and paint top/bottom/side.
  // If the heatmap fetch fails, fall back to the bare uploaded mesh so the
  // page is never blank — the user still sees their geometry.
  try {
    const scenes = await http(`/cases/${caseId}/optimize/variant-heatmap`, {
      method: "POST",
      body: JSON.stringify({
        material: matName,
        wall_thickness_mm: alt.fields?.wall_thickness_mm,
        closure_type: alt.fields?.closure_type,
        fill_level_pct: alt.fields?.fill_level_pct,
      }),
    });
    paintColorbar($("variant-cb-canvas"), scenes.colormap?.lut);
    await _populateMiniStrip(`variant-${idx}`, scenes, VARIANT_CELLS());
  } catch (e) {
    appendMsg("system", "Could not load variant heatmap: " + e.message);
    if (lastMeshUrl) {
      await populateBareMeshIntoStrip(`variant-${idx}`, VARIANT_CELLS());
    }
  }

  // Transit chart — same envelope as the baseline so the user sees identical
  // conditions, just a different material under it. Reuse the last cached
  // truck telemetry, or fetch it now if the user never visited Transit.
  if (!lastTransitCharts?.truck) {
    try {
      const cs = (lastSnapshot?.case_summary) || {};
      lastTransitCharts = await http(
        `/transit/charts?road=${encodeURIComponent(cs.road_condition || "mixed")}` +
        `&modes=truck&max_points=8000`
      );
    } catch (_) {}
  }
  if (lastTransitCharts?.truck) {
    drawLineChart($("variant-chart-truck"), {
      x: lastTransitCharts.truck.t_hours,
      y: lastTransitCharts.truck.vibration_g,
      color: "#0072bb", fill: "rgba(0,114,187,0.10)",
      yLabel: "Vibration (g)", xLabel: "Time (hours)",
      baseline: { y: 0.54, label: "truck PSD baseline" },
      hoverLabel: "Vibration (g)",
    });
  }

  // Summary of what's different from the baseline.
  // baseline_summary comes from case_summary at the time the opt run was saved;
  // fall back to lastSnapshot.case_summary so demo/stub cases still show values.
  const baseline = result.baseline_summary || {};
  const cs = lastSnapshot?.case_summary || {};
  const bMat    = baseline.material          || cs.material          || "";
  const bWall   = baseline.wall_thickness_mm ?? cs.wall_thickness_mm ?? null;
  const bClosure = baseline.closure_type     || cs.closure_type      || "";
  const lines = [];
  if (alt.fields?.material && alt.fields.material !== bMat)
    lines.push(`Material swapped from <strong>${escapeHtml(bMat || "baseline")}</strong> to <strong>${escapeHtml(alt.fields.material)}</strong>.`);
  if (alt.fields?.wall_thickness_mm != null && alt.fields.wall_thickness_mm !== bWall)
    lines.push(`Wall thickness <strong>${bWall ?? "baseline"} → ${alt.fields.wall_thickness_mm} mm</strong>.`);
  if (alt.fields?.closure_type && alt.fields.closure_type !== bClosure)
    lines.push(`Closure <strong>${escapeHtml(bClosure || "baseline")} → ${escapeHtml(alt.fields.closure_type)}</strong>.`);
  const costDelta = alt.cost_per_unit && baseline.cost_per_unit
    ? Math.round(100 * (alt.cost_per_unit - baseline.cost_per_unit) / baseline.cost_per_unit)
    : null;
  if (costDelta != null) {
    const dir = costDelta < 0 ? "down" : "up";
    lines.push(`Unit cost <strong>${dir} ${Math.abs(costDelta)}%</strong> (now $${alt.cost_per_unit?.toFixed?.(3) ?? alt.cost_per_unit}).`);
  }
  if (alt.roi_pct) lines.push(`Annual ROI: <strong>${alt.roi_pct}%</strong> at 1M unit/yr.`);
  if (alt.rationale) lines.push(`<em>${escapeHtml(alt.rationale)}</em>`);
  set($("variant-summary"), "innerHTML",
    `<span class="eyebrow">Why this variant</span>
     <div style="margin-top:8px;display:flex;flex-direction:column;gap:6px">${lines.map(l => `<div>${l}</div>`).join("")}</div>`);
}

// Spider/radar chart removed — kept as a stub to preserve any external
// callers that might still reference the symbol while we're iterating.
function renderSpiderChart(_result) { /* removed per spec */ }

// ===================== Voice input =====================
let mediaRecorder = null, recordedChunks = [], recognition = null;
const micBtn = $("mic-btn");
function setupVoice() {
  const Recog = window["SpeechRecognition"] || window["webkitSpeechRecognition"];
  if (Recog) {
    recognition = new Recog();
    recognition.lang = "en-US"; recognition.interimResults = true;
    recognition.onresult = e => {
      const last = e.results[e.results.length - 1];
      inputEl.value = last[0].transcript;
      inputEl.style.borderColor = "var(--warn)";
    };
    recognition.onend = () => { micBtn.classList.remove("recording"); inputEl.style.borderColor = ""; };
  }
}
async function micPressed() {
  if (micBtn.classList.contains("recording")) {
    if (recognition) recognition.stop();
    else if (mediaRecorder?.state === "recording") mediaRecorder.stop();
    return;
  }
  if (recognition) {
    try { recognition.start(); micBtn.classList.add("recording"); return; } catch (_) {}
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    recordedChunks = [];
    mediaRecorder.ondataavailable = e => e.data.size > 0 && recordedChunks.push(e.data);
    mediaRecorder.onstop = async () => {
      micBtn.classList.remove("recording");
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(recordedChunks, { type: "audio/webm" });
      const fd = new FormData(); fd.append("audio", blob, "voice.webm"); fd.append("mime", "audio/webm");
      progressBegin("Transcribing", 1);
      try {
        const r = await fetch(`${API}/transcribe`, { method: "POST", body: fd });
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        inputEl.value = j.transcript || "";
        inputEl.style.borderColor = "var(--warn)";
      } catch (e) { appendMsg("system", "Transcription failed: " + e.message); }
      finally { progressEnd(); }
    };
    mediaRecorder.start();
    micBtn.classList.add("recording");
    setTimeout(() => { if (mediaRecorder.state === "recording") mediaRecorder.stop(); }, 12000);
  } catch (e) { appendMsg("system", "Microphone unavailable: " + e.message); }
}

// ===================== Feedback =====================
function showFeedbackToast(target) {
  fbToast.dataset.target = target;
  cls(fbToast, "remove", "hidden");
}
async function sendFeedback(rating) {
  if (!caseId) return;
  try {
    await http(`/cases/${caseId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ target: fbToast.dataset.target || "report", rating, notes: $("fb-notes").value, tags: {} }),
    });
    cls(fbToast, "add", "hidden");
    $("fb-notes").value = "";
  } catch (_) {}
}

// ===================== Threads =====================
async function refreshThreadList() {
  if (!USER_ID) return;
  try {
    const threads = await http(`/users/${USER_ID}/threads`);
    const list = $("thread-list");
    set(list, "innerHTML", "");
    for (const t of threads.slice(0, 8)) {
      const li = document.createElement("li");
      li.dataset.id = t.case_id;
      if (t.case_id === caseId) li.classList.add("active");
      li.innerHTML = `
        <span class="t-name">${escapeHtml(t.design_name || "Untitled")}</span>
        <span class="t-meta">${t.runs_count} run${t.runs_count===1?"":"s"} · ${t.stage}</span>
        <button class="t-menu" title="Options" data-open-menu>⋯</button>
      `;
      li.addEventListener("click", e => {
        if (e.target.closest("[data-open-menu]") || e.target.closest(".thread-menu")) return;
        loadCase(t.case_id);
      });
      li.querySelector("[data-open-menu]").addEventListener("click", e => {
        e.stopPropagation();
        openThreadMenu(li, t);
      });
      list.appendChild(li);
    }
  } catch (_) {}
}

function openThreadMenu(li, thread) {
  // Close any other menu
  document.querySelectorAll(".thread-menu").forEach(m => m.remove());
  document.querySelectorAll(".thread-list li.menu-open").forEach(x => x.classList.remove("menu-open"));

  const menu = document.createElement("div");
  menu.className = "thread-menu";
  menu.innerHTML = `
    <button data-act="rename">Rename…</button>
    <button data-act="delete" class="delete">Delete</button>
  `;
  // Render the menu into <body> with fixed positioning anchored to the
  // trigger button. This guarantees the menu floats above sibling list
  // items (no more "delete is hiding under the next thread") and isn't
  // clipped by the rail's overflow:auto stacking context.
  li.classList.add("menu-open");
  document.body.appendChild(menu);
  const trigger = li.querySelector("[data-open-menu]");
  const rect = trigger.getBoundingClientRect();
  Object.assign(menu.style, {
    position: "fixed",
    top:  Math.round(rect.bottom + 6) + "px",
    left: Math.round(rect.right - 160) + "px",   // 160 ≈ menu width
    zIndex: 9999,
  });
  // Flip up if the menu would clip the viewport bottom.
  requestAnimationFrame(() => {
    const mh = menu.getBoundingClientRect().height;
    if (rect.bottom + mh + 12 > window.innerHeight) {
      menu.style.top = Math.round(rect.top - mh - 6) + "px";
    }
  });

  const close = () => {
    menu.remove();
    li.classList.remove("menu-open");
    document.removeEventListener("click", outsideHandler, true);
    window.removeEventListener("scroll", close, true);
    window.removeEventListener("resize", close, true);
  };
  const outsideHandler = (e) => { if (!menu.contains(e.target) && !e.target.closest("[data-open-menu]")) close(); };
  setTimeout(() => document.addEventListener("click", outsideHandler, true), 0);
  // The menu is body-mounted at fixed coords — close on scroll/resize so it
  // never drifts away from its trigger.
  window.addEventListener("scroll", close, true);
  window.addEventListener("resize", close, true);

  menu.querySelector("[data-act='rename']").addEventListener("click", async e => {
    e.stopPropagation(); close();
    const name = prompt("Rename design:", thread.design_name || "");
    if (!name) return;
    try {
      await http(`/cases/${thread.case_id}/name`, { method: "POST",
        body: JSON.stringify({ design_name: name }) });
      if (thread.case_id === caseId) set(topbarName, "textContent", name);
      await refreshThreadList();
    } catch (err) { alert("Rename failed: " + err.message); }
  });

  menu.querySelector("[data-act='delete']").addEventListener("click", async e => {
    e.stopPropagation(); close();
    const confirmDelete = confirm(
      `Delete "${thread.design_name || "Untitled"}"? This cannot be undone.`);
    if (!confirmDelete) return;
    try {
      await http(`/cases/${thread.case_id}`, { method: "DELETE" });
      if (thread.case_id === caseId) {
        // Current thread deleted — start a fresh one
        caseId = null;
        await newCase();
      } else {
        await refreshThreadList();
      }
    } catch (err) { alert("Delete failed: " + (err.message || "")); }
  });
}

// ===================== Refresh all stages =====================
function refreshAll() {
  renderMaterialStage();
  renderScorecard();
  renderReport();
  renderSignoffStage();
}

// ===================== Rename / save =====================
async function renameDesign() {
  if (!caseId) return;
  const name = prompt("Name this design:", topbarName.textContent);
  if (!name) return;
  await http(`/cases/${caseId}/name`, { method: "POST", body: JSON.stringify({ design_name: name }) });
  set(topbarName, "textContent", name);
  refreshThreadList();
}
// Generate a friendly automatic design name like "swift-otter-4821" when
// the user saves without explicitly naming the thread. Kept short and
// memorable so the Recent list reads as a workspace, not a hash dump.
function generateThreadName() {
  const adj = ["swift","quiet","bold","amber","crisp","wired","steady",
               "polar","ember","tidy","brisk","plumb","clear","jade","calm"];
  const noun = ["otter","mantis","crane","aspen","beacon","cinder","onyx",
                "willow","kestrel","atlas","reef","cobalt","forge","prism","ridge"];
  const a = adj[Math.floor(Math.random() * adj.length)];
  const n = noun[Math.floor(Math.random() * noun.length)];
  const id = String(Math.floor(Math.random() * 9000) + 1000);
  return `${a}-${n}-${id}`;
}

async function saveDesign() {
  if (!caseId) {
    setSaveState("err", "Nothing to save");
    return;
  }
  const btn = $("save-design-btn");
  if (btn) btn.disabled = true;
  setSaveState("pending", "Saving…");
  try {
    // If the design is still "Untitled", auto-assign a friendly random
    // thread name BEFORE saving so the Recent list never fills up with
    // identical "Untitled design" rows.
    const currentName = (topbarName && topbarName.textContent || "").trim();
    if (!currentName || /^untitled/i.test(currentName)) {
      const auto = generateThreadName();
      try {
        await http(`/cases/${caseId}/name`, {
          method: "POST",
          body: JSON.stringify({ design_name: auto }),
        });
        set(topbarName, "textContent", auto);
      } catch (_) { /* don't block save on naming */ }
    }
    await http(`/cases/${caseId}/save`, { method: "POST" });
    // Snapshot the current geometry asset id locally so a hard reload can
    // restore the 3D viewer immediately (faster than waiting for the snapshot
    // round-trip).
    if (lastSnapshot?.geometry_asset_id) {
      try {
        localStorage.setItem(
          `de.geom.${caseId}`,
          JSON.stringify({
            asset_id: lastSnapshot.geometry_asset_id,
            saved_at: Date.now(),
          })
        );
      } catch (_) {}
    }
    setSaveState("ok", "Saved ✓");
    refreshThreadList();
    refreshDashboard();          // bump the "Saved" tile + ring
  } catch (err) {
    setSaveState("err", "Save failed");
    appendMsg("system", `Save failed: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
    // Clear the chip after 4s so the topbar stays clean.
    setTimeout(() => setSaveState("", ""), 4000);
  }
}
function setSaveState(kind, text) {
  const el = $("save-state");
  if (!el) return;
  el.textContent = text || "";
  el.className = "save-state" + (kind ? " save-state--" + kind : "");
}

// ===================== Wiring =====================
// ISTA 6A toggle — runs the corner-drop check on demand
$("ista6a-toggle").addEventListener("change", e => {
  if (e.target.checked) runIsta6A();
  else { cls($("ista6a-result"), "add", "hidden"); }
});

// (Brief bar was removed — no collapse handler needed.)

// Custom material modal wiring
const _addCustomMatBtn = $("add-custom-mat");
if (_addCustomMatBtn) _addCustomMatBtn.addEventListener("click", openCustomMatModal);
const _cmClose = $("cm-close"); if (_cmClose) _cmClose.addEventListener("click", closeCustomMatModal);
const _cmSave  = $("cm-save");  if (_cmSave)  _cmSave.addEventListener("click", saveCustomMaterial);

// ===================== Chat send — bulletproof wiring =====================
// The send button and Enter-key path BOTH go through `sendMessage()`. We
// wire them with explicit guards and an inline error surface so a missing
// element or a thrown handler can't leave the chat silently broken.
//
// Use event-delegation on `document` as a belt-and-braces backup so that
// even if the early `$("send-btn")` lookup ever returned null (re-render,
// cached HTML, etc.) clicks on the button still reach sendMessage.
function _safeSend() {
  try { sendMessage(); }
  catch (e) {
    console.error("[chat] sendMessage threw:", e);
    try { appendMsg("system", "⚠️ Chat error: " + (e && e.message || e)); } catch (_) {}
  }
}
function _maybeSendOnEnter(e) {
  if (e.isComposing || e.keyCode === 229) return;     // IME composition
  if (e.key !== "Enter") return;
  if (e.shiftKey) return;                              // Shift+Enter = newline
  e.preventDefault();
  _safeSend();
}

(function wireChat() {
  let directBound = false;
  const sendBtn = document.getElementById("send-btn");
  if (sendBtn) {
    sendBtn.addEventListener("click", _safeSend);
    directBound = true;
  }
  const input = document.getElementById("user-input");
  if (input) {
    input.addEventListener("keydown",  _maybeSendOnEnter);
    input.addEventListener("keypress", _maybeSendOnEnter);
  }
  // Delegation backup — ONLY if the direct binding wasn't possible (the
  // element was missing at module-load time). Prevents double-send when
  // the direct listener is also wired.
  if (!directBound) {
    document.addEventListener("click", (ev) => {
      const t = ev.target && ev.target.closest && ev.target.closest("#send-btn");
      if (t) _safeSend();
    });
  }
})();
$("upload-btn").addEventListener("click", () => fileEl.click());
fileEl.addEventListener("change", () => fileEl.files[0] && uploadFile(fileEl.files[0]));
$("approve-btn").addEventListener("click", () => approvePlan());
$("reject-btn").addEventListener("click", rejectPlan);
$("new-case-btn").addEventListener("click", newCase);
$("rename-btn").addEventListener("click", renameDesign);
$("save-btn").addEventListener("click", saveDesign);
const _saveDesignBtn = $("save-design-btn");
if (_saveDesignBtn) _saveDesignBtn.addEventListener("click", saveDesign);
// Cmd/Ctrl-S anywhere in the app saves the current design.
window.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && (e.key === "s" || e.key === "S")) {
    e.preventDefault();
    saveDesign();
  }
});
micBtn.addEventListener("click", micPressed);
$("run-btn").addEventListener("click", () => approvePlan(true));
const _apEditToggle = $("ap-edit-toggle");
if (_apEditToggle) _apEditToggle.addEventListener("click", toggleAnalysisPlanEdit);

// Drop zone
$("dz-browse").addEventListener("click", () => fileEl.click());
$("dz-demo").addEventListener("click", e => {
  e.preventDefault();
  if (caseId) uploadFile(new File([new Blob()], "demo.stp"), { demo: true });
});
const dz = $("dropzone");
dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("dragover"); });
dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
dz.addEventListener("drop", e => {
  e.preventDefault(); dz.classList.remove("dragover");
  if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]);
});
dz.addEventListener("click", () => fileEl.click());

// Optimise
$("opt-send").addEventListener("click", () => optSend());
$("opt-input").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); optSend(); } });
// Bottle intent chips
document.querySelectorAll("#opt-intents-bottle .chip").forEach(c => c.addEventListener("click", () => {
  const intent = c.dataset.intent;
  if (intent === "other") { $("opt-input").focus(); return; }
  optIntent = intent;
  optAppend("user", `Optimise for: ${intent.replace(/_/g, " ")}`);
  optGenerate(intent, "");
}));
// Packet intent chips
document.querySelectorAll("#opt-intents-packet .chip").forEach(c => c.addEventListener("click", () => {
  const intent = c.dataset.pktIntent;
  if (intent === "other") { $("opt-input").focus(); return; }
  pktOptIntent = intent;
  optAppend("user", `Optimise for: ${intent.replace(/_/g, " ")}`);
  pktOptGenerate(intent, "");
}));
// Brush intent chips
document.querySelectorAll("#opt-intents-brush .chip").forEach(c => c.addEventListener("click", () => {
  const intent = c.dataset.brushIntent;
  if (intent === "other") { $("opt-input").focus(); return; }
  brushOptIntent = intent;
  optAppend("user", `Optimise for: ${intent.replace(/_/g, " ")}`);
  brushOptGenerate(intent, "");
}));

// Signoff
$("signoff-btn").addEventListener("click", doSignoff);
$("unlock-btn").addEventListener("click", async () => {
  await http(`/cases/${caseId}/unlock`, { method: "POST" });
  cls($("unlock-btn"), "add", "hidden");
  $("signoff-btn").disabled = false;
  cls($("signoff-result"), "add", "hidden");
});

// Feedback
fbToast.querySelectorAll("button[data-rating]").forEach(b =>
  b.addEventListener("click", () => sendFeedback(parseInt(b.dataset.rating, 10))));
$("fb-send").addEventListener("click", () => sendFeedback(0));
$("fb-close").addEventListener("click", () => cls(fbToast, "add", "hidden"));

// Stage rail nav (also re-renders transit/material on entry)
window.addEventListener("hashchange", () => {
  const r = currentRoute();
  if (r === "transit") { renderTransitStage(); refreshTransitCharts(); }
  if (r === "material") renderMaterialStage();
  if (r === "results") after2Frames(() => renderResultsStage());
  if (r === "report") renderReport();
  if (r === "signoff") renderSignoffStage();
  if (r === "optimise") {
    const fam = _effectiveFamily();
    _applyOptimiseUiForFamily(fam);
    if (fam === "packet" && lastPktOptResult) { renderPacketOptDashboard(lastPktOptResult); renderPacketOptCompare(lastPktOptResult); }
    else if (fam === "brush" && lastBrushOptResult) { renderBrushOptDashboard(lastBrushOptResult); renderBrushOptCompare(lastBrushOptResult); }
    else if (fam !== "packet" && fam !== "brush" && lastOptResult) renderOptCompare(lastOptResult);
  }
  // If user navigates to Geometry without a CAD asset OR an explicit
  // "no I don't have one" answer, open the upload modal as a friendly nudge.
  if (r === "geometry" && !geomModalShownThisCase) {
    refreshBrief().then(() => {
      // Inspect the brief data already in the sidecar — render uses .br-v rows
      // but the simplest signal is: did upload happen? Check via the viewer card.
      const hasMesh = !$("viewer-card").classList.contains("hidden");
      if (!hasMesh) openGeomModal();
    });
  }
});


// Initial entrance
if (gsap) {
  gsap.from(".topbar", { y: -8, opacity: 0, duration: 0.4, ease: "power2.out" });
  gsap.from(".rail", { x: -20, opacity: 0, duration: 0.45, delay: 0.05, ease: "power2.out" });
  gsap.from(".brief", { x: 20, opacity: 0, duration: 0.45, delay: 0.05, ease: "power2.out" });
}

setupVoice();
showStage(currentRoute());
newCase();

// ── Bridge for non-module scripts (auth-ui.js) ────────────────────────────
// auth-ui.js lives outside the ES-module boundary and cannot import from here.
// Expose the one function it needs on window so the PCR apply button can
// trigger a properly-instrumented re-run instead of its own broken approve call.
window.PACKTWIN_PCR_APPLY = (candidateMaterial) => {
  approvePlan(false, { material: candidateMaterial });
};
// Also keep CURRENT_CASE_ID in sync so auth-ui can read the active case.
Object.defineProperty(window, "CURRENT_CASE_ID", { get: () => caseId });
