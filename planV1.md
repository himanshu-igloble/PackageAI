# PackEdge AI — V1 Feature-Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden six existing subsystems — transit coverage, transit heuristics, the optimization engine, PCR/flute input mapping, physics-based heatmaps, and cross-module consistency — so that estimates are objective-specific, product-specific, input-faithful, and engineering-credible.

**Architecture:** The backend is a FastAPI app with a `case_summary` JSON config blob driving a fan-out of stateless agents (`backend/agents/*`) and services (`backend/services/*`) orchestrated by `Orchestrator.execute_approved_plan`. This plan keeps that architecture but (1) extends the transit data layer with new modes + user-supplied duration/drop-height, (2) inserts an explicit objective-ranking and family-guard stage into the three optimizers, (3) replaces flute alias-collapse with per-flute material records + a validated resolver, (4) re-grounds the heatmap on the mechanics already computed in `ista2a.py`/`calculation.py`, and (5) introduces a single typed `DesignConfig` snapshot plus a deterministic `GuardrailAgent.review_consistency` gate.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.x (declarative), Pydantic v2, pandas, numpy, trimesh, Google Gemini (`backend/llm/gemini_client.py`), vanilla-JS frontend (`frontend/app.js`), pytest.

**Conventions:**
- All paths are relative to the project root (the folder containing `README.md` and `backend/`).
- Run backend tests with: `python -m pytest backend/tests/<file>::<test> -v` from the project root.
- TDD per task: write failing test → run (fail) → minimal implementation → run (pass) → commit.
- Pricing/material data lives in `backend/data/materials.json` and `MATERIAL_PRICE_PER_KG` (`backend/agents/optimization.py:42-54`).

---

## Part-by-Part Index

| Part | Subsystem | Primary files |
|------|-----------|---------------|
| A | Expand transit mode coverage (Pickup / Air / Rail) | `services/transit_data.py`, `agents/transit.py`, `schemas.py`, `frontend/app.js` |
| B | Improve transit heuristics (durations + drop heights) | `services/transit_data.py`, `orchestrator.py`, `agents/ista2a.py`, `routes/extras.py`, `frontend` |
| C | Optimization engine: objective + product correctness | `agents/optimization.py`, `agents/packet_optimization.py`, `agents/brush_optimizer.py`, `routes/extras.py` |
| D | PCR input mapping / flute fidelity | `data/materials.json`, `agents/pcr.py`, `agents/material.py`, `routes/cases.py`, `schemas.py` |
| E | Physics-based stress heatmaps | `services/heatmap.py`, `agents/ista2a.py`, `agents/calculation.py`, `services/visualization_service.py` |
| F | Cross-module consistency validation | `agents/design_config.py` (new), `agents/guardrail.py`, `orchestrator.py`, `agents/report.py` |

Parts are independent at the file level except: **Part B depends on Part A** (new modes must exist before durations attach to them); **Part F depends on D** (flute provenance) and benefits from B/C/E being landed first. Recommended order: A → B → C → D → E → F.

---

# PART A — Expand Transit Mode Coverage

**Current state (verified):**
- `backend/services/transit_data.py:32-38` `FILES` has keys `truck, pickup, ship_clean, ship_moderate, ship_severe`. The `pickup` CSV (`pickup_truck_simulation_dataset.csv`, same 33 columns as truck) is loaded into `FILES` but **never consumed** by any envelope function.
- `available_modes()` (`transit_data.py:246-253`) returns only a subset of `["truck","ship"]`. This is the gatekeeper the UI uses to enable modes.
- `air_envelope()` (`:256-268`) exists with hardcoded reference values but `air` is never returned by `available_modes()`.
- No `rail` envelope exists. `blended_envelope` (`:284-350`) mode dispatch (`:303-310`) handles only `truck/ship/air/manual_handling` — **no `rail`, no `pickup`**.
- Frontend `MODES` (`frontend/app.js:1853`) = `["truck","ship","air","rail","manual_handling"]`; `rail` already shows but is greyed because the backend never lists it.

**File structure for Part A:**
- Modify `backend/services/transit_data.py` — add `pickup_envelope`, `rail_envelope`, extend `available_modes`, extend `blended_envelope` dispatch.
- Modify `backend/agents/transit.py` — extend `_sequence_for` with pickup/rail branches.
- Create `backend/tests/test_transit_modes.py` — coverage for the new modes.

---

### Task A1: Pickup-truck envelope (data-backed)

**Files:**
- Modify: `backend/services/transit_data.py` (add `pickup_envelope`, extend `available_modes`, `blended_envelope`)
- Test: `backend/tests/test_transit_modes.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_transit_modes.py
import pytest
from backend.services import transit_data as td


@pytest.mark.skipif(not td.available(), reason="transit CSVs not present")
def test_pickup_envelope_is_data_backed_and_distinct():
    env = td.pickup_envelope(road="rough_secondary")
    assert env["mode"] == "pickup"
    assert env["source_file"].endswith(".csv")          # real CSV, not industry_reference
    assert 0.0 < env["g_rms"] < 5.0
    assert "g_p95" in env and "shock_risk_p95" in env


def test_pickup_listed_in_available_modes(monkeypatch):
    monkeypatch.setattr(td, "_exists", lambda key: True)  # pretend all CSVs present
    modes = td.available_modes()
    assert "pickup" in modes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_transit_modes.py -v`
Expected: FAIL — `AttributeError: module 'backend.services.transit_data' has no attribute 'pickup_envelope'` (and `_exists`).

- [ ] **Step 3: Add a `_exists` helper and `pickup_envelope`, extend `available_modes`**

In `backend/services/transit_data.py`, add near `available()` (`:55-57`):

```python
def _exists(key: str) -> bool:
    """True if the CSV backing `key` is present on disk."""
    fname = FILES.get(key)
    return bool(fname) and (DATA_DIR / fname).exists()
```

Add `pickup_envelope` immediately after `truck_envelope` (after `:117`). It reuses the truck pipeline against the `pickup` CSV:

```python
def pickup_envelope(road: str = "mixed") -> dict[str, Any]:
    """Pickup-truck vibration/shock envelope. Same telemetry schema as truck,
    but pickups ride stiffer (lighter payload) so we keep the truck math and
    only swap the source CSV via the `pickup` key."""
    df = _load("pickup")
    rtm = {
        "smooth_highway": {"motorway"},
        "mixed": {"rural", "motorway", "urban"},
        "rough_secondary": {"rural", "urban"},
        "off_road": {"rural"},
    }
    env = _summarise_road_df(df, rtm.get(road, rtm["mixed"]))   # see Step 4
    env.update(mode="pickup", road=road, source_file=FILES["pickup"])
    return env
```

- [ ] **Step 4: Extract the shared road-summary so truck + pickup don't drift**

Refactor the body of `truck_envelope` (`:63-117`) into a private `_summarise_road_df(df, road_types: set[str]) -> dict` that returns `{n_rows, g_rms, g_p95, shock_risk_p95, rough_road_prob, handling_risk_mean, psd_bins}`. Then make `truck_envelope` call it:

```python
def truck_envelope(road: str = "mixed") -> dict[str, Any]:
    df = _load("truck")
    rtm = { ... }  # unchanged map from :72-77
    env = _summarise_road_df(df, rtm.get(road, rtm["mixed"]))
    env.update(mode="truck", road=road, source_file=FILES["truck"])
    return env
```

Update `available_modes` (`:246-253`) to enumerate data-backed modes generically:

```python
def available_modes() -> list[str]:
    modes: list[str] = []
    if _exists("truck"):
        modes.append("truck")
    if _exists("pickup"):
        modes.append("pickup")
    if any(_exists(k) for k in ("ship_clean", "ship_moderate", "ship_severe")):
        modes.append("ship")
    return modes
```

- [ ] **Step 5: Add the `pickup` branch to `blended_envelope` dispatch**

In `blended_envelope` (`:303-310`), add alongside the existing `truck`/`ship`/`air`/`manual_handling` branches:

```python
elif mode == "pickup":
    per_mode[mode] = pickup_envelope(road=road)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_transit_modes.py -v`
Expected: PASS (pickup test skipped only if CSVs absent; the `available_modes` monkeypatched test passes regardless).

- [ ] **Step 7: Commit**

```bash
git add backend/services/transit_data.py backend/tests/test_transit_modes.py
git commit -m "feat(transit): data-backed pickup-truck envelope + generic available_modes"
```

---

### Task A2: Air-freight + Rail reference envelopes (selectable)

**Files:**
- Modify: `backend/services/transit_data.py` (add `rail_envelope`, make `air`/`rail` selectable)
- Modify: `backend/agents/transit.py` (`_sequence_for` rail/pickup branches)
- Test: `backend/tests/test_transit_modes.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_transit_modes.py
from backend.agents.transit import TransitAgent


def test_rail_envelope_reference_values():
    env = td.rail_envelope()
    assert env["mode"] == "rail"
    assert env["source_file"] == "industry_reference"
    # Rail: low high-frequency vibration, dominant low-freq coupling shock.
    assert env["g_rms"] < td.air_envelope()["g_rms"]
    assert env["coupling_shock_g"] > 0


def test_available_modes_includes_reference_modes():
    modes = td.selectable_modes()        # data-backed + reference
    assert {"air", "rail"} <= set(modes)


def test_sequence_for_rail_and_pickup():
    agent = TransitAgent()
    seq_rail = agent._sequence_for(["rail"])
    seq_pickup = agent._sequence_for(["pickup"])
    assert any("rail" in s.lower() or "coupling" in s.lower() for s in seq_rail)
    assert seq_pickup  # non-empty, reuses truck-style PSD sequence
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_transit_modes.py -k "rail or reference or sequence" -v`
Expected: FAIL — `rail_envelope`, `selectable_modes`, and the rail/pickup sequence branches don't exist.

- [ ] **Step 3: Add `rail_envelope` and `selectable_modes`**

In `transit_data.py`, after `air_envelope` (`:268`):

```python
def rail_envelope() -> dict[str, Any]:
    """Rail reference envelope (no telemetry CSV yet). Rail freight is
    low high-frequency vibration but exposes goods to longitudinal
    coupling/humping shocks (AAR/ASTM D4169 DC-13). Values are conservative
    industry references, not measured data."""
    return {
        "mode": "rail",
        "g_rms": 0.30,
        "g_p95": 0.80,
        "coupling_shock_g": 5.0,        # longitudinal hump/coupling impact
        "shock_risk_p95": 0.55,
        "handling_risk_mean": 0.40,
        "source_file": "industry_reference",
    }
```

Add a selectable-mode list that separates data-backed from reference modes (UI uses this to *offer* modes; `available_modes` stays the data-backed truth):

```python
REFERENCE_MODES = ("air", "rail", "manual_handling")

def selectable_modes() -> list[str]:
    """All modes a user may select: data-backed + reference. The UI labels
    reference modes as estimate-only via `is_reference_mode`."""
    return available_modes() + list(REFERENCE_MODES)

def is_reference_mode(mode: str) -> bool:
    return mode in REFERENCE_MODES
```

- [ ] **Step 4: Add `rail` to `blended_envelope` dispatch**

In `blended_envelope` (`:303-310`):

```python
elif mode == "rail":
    per_mode[mode] = rail_envelope()
```

Ensure the composite `g_rms` weighting (`:316`) and `shock_risk_p95` (it should also fold `coupling_shock_g` for rail) tolerate the new keys — use `.get(...)` reads so missing keys default to 0.0.

- [ ] **Step 5: Extend `_sequence_for` in `transit.py`**

In `backend/agents/transit.py:15-27`, add branches:

```python
if "rail" in dominant:
    seq.append("ASTM D4169 DC-13 rail coupling/humping longitudinal shock")
if "pickup" in dominant:
    seq.append("ISTA-2A random vibration (pickup PSD, light-payload profile)")
```

(Place before the final `return` so they merge with existing truck/ship/air branches; de-dup the list with `list(dict.fromkeys(seq))`.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_transit_modes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/services/transit_data.py backend/agents/transit.py backend/tests/test_transit_modes.py
git commit -m "feat(transit): air+rail reference envelopes, selectable_modes, rail/pickup test sequences"
```

---

### Task A3: Surface new modes in the frontend

**Files:**
- Modify: `frontend/app.js` (`renderTransitStage` `:1862-1920`, `transitState` `:1854-1861`)
- Modify: `backend/routes/extras.py` (the `/transit/available-modes` route)

- [ ] **Step 1: Update the available-modes route to return selectable modes + reference flags**

Find the `available-modes` route in `extras.py` (the one the UI calls at `app.js:1867`) and change its body to:

```python
@router.get("/transit/available-modes")
def transit_available_modes():
    from backend.services import transit_data as td
    return {
        "data_backed": td.available_modes(),
        "selectable": td.selectable_modes(),
        "reference": list(td.REFERENCE_MODES),
    }
```

- [ ] **Step 2: Update `transitState` default mix to include pickup**

`app.js:1854-1861` — add `pickup: 0` to `mode_mix`:

```js
mode_mix: { truck: 50, ship: 30, air: 20, pickup: 0, rail: 0, manual_handling: 0 },
```

- [ ] **Step 3: Update `MODES` and the enable/disable logic**

`app.js:1853`:

```js
const MODES = ["truck", "pickup", "ship", "air", "rail", "manual_handling"];
```

In `renderTransitStage` (`:1862-1920`), replace the single `available` array read with the new payload shape: a mode slider is **enabled** if it is in `selectable`; if it is in `reference` (not `data_backed`), render the existing greyed style but keep it interactive and append a `"(estimate)"` badge instead of `"(no data)"`:

```js
const resp = await api("/transit/available-modes");
const selectable = new Set(resp.selectable);
const reference = new Set(resp.reference);
// per mode:
const enabled = selectable.has(mode);
const isRef = reference.has(mode);
const badge = isRef ? "(estimate)" : "";
slider.disabled = !enabled;
slider.parentElement.style.opacity = enabled ? "1" : "0.4";
```

- [ ] **Step 4: Manual verification**

Run the app (`README.md` start instructions), open the Transit stage. Expected: `pickup` now toggles on; `air` and `rail` are selectable with an `(estimate)` badge; `/transit/preview` returns a blended `g_rms` that changes when you add rail/air weight.

- [ ] **Step 5: Commit**

```bash
git add frontend/app.js backend/routes/extras.py
git commit -m "feat(transit-ui): expose pickup/air/rail as selectable modes with reference badge"
```

---

# PART B — Improve Transit Heuristics (durations + drop heights)

**Current state (verified):**
- There is **no duration concept anywhere** in the transit path. The CSV `duration_s` column is used only to build chart x-axes (`transit_data.py:183`), never to set test duration.
- `ista2a._vibration_fatigue(g_rms, duration_min, ...)` (`:334-358`) *does* consume `duration_min` for cycle count (`n_cycles = 80*60*duration_min`, `:348`), but the orchestrator calls `ista2a.evaluate` **without** `vibration_duration_min` (`orchestrator.py:1061-1070`), so it is pinned to the default 60 (`ista2a.py:465`).
- `manual_handling_envelope()` (`transit_data.py:271-279`) hardcodes `drop_height_m=0.91`. User cannot set it. The only `#acc-drop` field (`index.html:539-540`) belongs to the accuracy-feedback form, not transit.

**File structure for Part B:**
- Modify `backend/services/transit_data.py` — `manual_handling_envelope(drop_height_m=...)`, per-mode `durations` into `blended_envelope`, composite `vibration_duration_min`.
- Modify `backend/schemas.py` — `TransitEnvelope` gains `vibration_duration_min`, `drop_height_m` already exists.
- Modify `backend/agents/transit.py` — thread durations + drop height.
- Modify `backend/orchestrator/orchestrator.py` — pass `vibration_duration_min` and user drop height into `ista2a.evaluate`.
- Modify `frontend/index.html` + `frontend/app.js` — duration presets + manual-handling drop-height selector.

---

### Task B1: User-supplied manual-handling drop height

**Files:**
- Modify: `backend/services/transit_data.py` (`manual_handling_envelope`, `blended_envelope`)
- Test: `backend/tests/test_transit_heuristics.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_transit_heuristics.py
from backend.services import transit_data as td


def test_manual_handling_drop_height_is_parameterised():
    assert td.manual_handling_envelope()["drop_height_m"] == 0.91          # default kept
    assert td.manual_handling_envelope(drop_height_m=0.5)["drop_height_m"] == 0.5
    assert td.manual_handling_envelope(drop_height_m=1.5)["drop_height_m"] == 1.5


def test_blended_envelope_uses_user_drop_height():
    env = td.blended_envelope(
        mode_mix={"manual_handling": 1.0},
        manual_drop_height_m=1.0,
    )
    assert env["drop_height_m"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_transit_heuristics.py -v`
Expected: FAIL — `manual_handling_envelope` takes no args; `blended_envelope` has no `manual_drop_height_m`.

- [ ] **Step 3: Parameterise `manual_handling_envelope`**

`transit_data.py:271-279`:

```python
def manual_handling_envelope(drop_height_m: float = 0.91) -> dict[str, Any]:
    return {
        "mode": "manual_handling",
        "drop_height_m": float(drop_height_m),
        "handling_risk_mean": 0.85,
        "source_file": "industry_reference",
    }
```

- [ ] **Step 4: Thread the drop height through `blended_envelope`**

Add a keyword arg and pass it to the manual-handling branch (`:309-310`); keep the `max(...)` semantics for `drop_height_m` (`:317`) so an explicit manual drop height wins:

```python
def blended_envelope(*, mode_mix: dict[str, float], road: str = "mixed",
                     ship_severity: str = "moderate",
                     manual_drop_height_m: float | None = None,
                     durations_min: dict[str, float] | None = None) -> dict[str, Any]:
    ...
    elif mode == "manual_handling":
        per_mode[mode] = manual_handling_envelope(
            drop_height_m=manual_drop_height_m if manual_drop_height_m is not None else 0.91
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_transit_heuristics.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/services/transit_data.py backend/tests/test_transit_heuristics.py
git commit -m "feat(transit): user-supplied manual-handling drop height"
```

---

### Task B2: Per-mode durations → composite vibration duration

**Files:**
- Modify: `backend/services/transit_data.py` (`blended_envelope` durations)
- Modify: `backend/schemas.py` (`TransitEnvelope.vibration_duration_min`)
- Modify: `backend/agents/transit.py` (`build` propagates duration)
- Test: `backend/tests/test_transit_heuristics.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_transit_heuristics.py
from backend.agents.transit import TransitAgent


def test_durations_accumulate_into_composite_minutes():
    env = td.blended_envelope(
        mode_mix={"truck": 0.5, "rail": 0.5},
        durations_min={"truck": 8 * 60, "rail": 12 * 60},   # 8h truck + 12h rail
    )
    # Composite vibration exposure is the weighted sum of per-mode minutes.
    assert env["vibration_duration_min"] == pytest.approx(0.5 * 480 + 0.5 * 720)


def test_transit_agent_carries_duration():
    agent = TransitAgent()
    te = agent.build({"truck": 1.0}, durations_min={"truck": 240})
    assert te.vibration_duration_min == 240
```

(Add `import pytest` at the top.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_transit_heuristics.py -k duration -v`
Expected: FAIL — no `vibration_duration_min` in the envelope or on `TransitEnvelope`.

- [ ] **Step 3: Compute composite duration in `blended_envelope`**

After weights are re-normalised (`:316` region), add:

```python
durations_min = durations_min or {}
vib_minutes = 0.0
for mode, w in weights.items():            # weights = normalised mode_mix
    default = _DEFAULT_DURATION_MIN.get(mode, 60.0)
    vib_minutes += w * float(durations_min.get(mode, default))
result["vibration_duration_min"] = round(vib_minutes, 1)
```

Add the default table near the top of `transit_data.py`:

```python
_DEFAULT_DURATION_MIN = {
    "truck": 480.0,        # 8 h
    "pickup": 120.0,       # 2 h
    "ship": 7 * 24 * 60.0, # 7 days
    "air": 6 * 60.0,       # 6 h
    "rail": 24 * 60.0,     # 24 h
    "manual_handling": 5.0,
}
```

- [ ] **Step 4: Add the schema field**

`backend/schemas.py`, in `TransitEnvelope` (around `:137-145`):

```python
vibration_duration_min: float = 60.0
```

- [ ] **Step 5: Propagate in `TransitAgent.build`**

`backend/agents/transit.py:31-51` — add `durations_min` and `manual_drop_height_m` params and map the new field:

```python
def build(self, mode_mix: dict[str, float], *, road: str = "mixed",
          ship_severity: str = "moderate",
          durations_min: dict[str, float] | None = None,
          manual_drop_height_m: float | None = None) -> TransitEnvelope:
    env = td.blended_envelope(
        mode_mix=mode_mix, road=road, ship_severity=ship_severity,
        durations_min=durations_min, manual_drop_height_m=manual_drop_height_m,
    )
    return TransitEnvelope(
        mode_mix=mode_mix,
        vibration_g_rms=env["g_rms"],
        vibration_duration_min=env.get("vibration_duration_min", 60.0),
        drop_height_m=env["drop_height_m"],
        compression_load_n=env["compression_load_n"],
        handling_fraction=env["handling_fraction"],
        dominant_risks=env["dominant_modes"],
        suggested_test_sequence=self._sequence_for(env["dominant_modes"]),
        confidence="estimated",
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_transit_heuristics.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/services/transit_data.py backend/schemas.py backend/agents/transit.py backend/tests/test_transit_heuristics.py
git commit -m "feat(transit): per-mode durations accumulate into composite vibration_duration_min"
```

---

### Task B3: Wire duration + drop height into the ISTA-2A call (the bug fix)

**Files:**
- Modify: `backend/orchestrator/orchestrator.py:1051-1070` (the `ista2a.evaluate` call site)
- Test: `backend/tests/test_transit_heuristics.py`

This closes the verified defect: `vibration_duration_min` is never passed (`orchestrator.py:1061-1070`), so transit vibration always assumes 60 min regardless of the user's transit time.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_transit_heuristics.py
from backend.agents.ista2a import Ista2AAgent


def test_longer_vibration_duration_lowers_fatigue_margin():
    """More transit hours => more vibration cycles => weaker fatigue verdict."""
    agent = Ista2AAgent()
    short = agent._vibration_fatigue(g_rms=0.9, duration_min=60)
    long = agent._vibration_fatigue(g_rms=0.9, duration_min=12 * 60)
    # Same g_rms, 12x the cycles: the long case must be no safer than the short.
    assert long["n_cycles"] > short["n_cycles"]
    assert long["passes"] is False or long["passes"] == short["passes"]
```

- [ ] **Step 2: Run test to verify it fails (or is vacuous)**

Run: `python -m pytest backend/tests/test_transit_heuristics.py -k fatigue -v`
Expected: PASS for the helper in isolation **but** the orchestrator integration is still broken — proceed to wire it (this test guards the helper contract the wiring relies on).

- [ ] **Step 3: Pass duration + user drop height at the orchestrator call site**

`backend/orchestrator/orchestrator.py:1061-1070` — add the two arguments that are currently omitted:

```python
ista2a_report = self.ista2a.evaluate(
    mass_kg=mass_kg,
    material=material,
    geometry=geometry,
    vibration_g_rms=transit_env.vibration_g_rms,
    vibration_duration_min=transit_env.vibration_duration_min,   # NEW: was defaulting to 60
    user_drop_height_m=transit_env.drop_height_m,                # NEW: see Step 4
    stacking_orientation=s.get("stacking_orientation", "upright"),
    stack_height=s.get("stack_height", 4),
    ships_loose=s.get("ships_loose", False),
)
```

- [ ] **Step 4: Let a user drop height optionally override the ISTA weight-class drop**

`backend/agents/ista2a.py` — `evaluate` (signature near `:460-466`) gains `user_drop_height_m: float | None = None`. In `_drop_verdict` (`:468-475`) the drop height is currently `drop_h` from `ISTA_2A_DROP_HEIGHTS_M` by weight class. Change selection to:

```python
drop_h = user_drop_height_m if user_drop_height_m else ISTA_2A_DROP_HEIGHTS_M[weight_class]
```

Record provenance in the returned assumption list so the report can show which height was used:

```python
assumptions.append(Assumption(
    field="drop_height_m",
    value=drop_h,
    basis="user_specified" if user_drop_height_m else f"ISTA-2A weight class {weight_class}",
))
```

- [ ] **Step 5: Run the full transit suite**

Run: `python -m pytest backend/tests/test_transit_heuristics.py backend/tests/test_ista_realism.py -v`
Expected: PASS. (`test_ista_realism.py` must still pass — the default path with `user_drop_height_m=None` is unchanged.)

- [ ] **Step 6: Commit**

```bash
git add backend/orchestrator/orchestrator.py backend/agents/ista2a.py backend/tests/test_transit_heuristics.py
git commit -m "fix(transit): thread vibration duration + user drop height into ISTA-2A (was pinned to 60min/weight-class)"
```

---

### Task B4: Frontend controls for durations + manual drop height

**Files:**
- Modify: `frontend/index.html` (Transit stage, around `:315-336`)
- Modify: `frontend/app.js` (`transitState`, `pushTransitToBrief` `:1958+`, preview)
- Modify: `backend/routes/extras.py` (`/transit/preview` accepts new params)

- [ ] **Step 1: Add duration + drop-height controls to the Transit stage**

In `frontend/index.html` after the ship-severity segmented control (`:323-325`), add:

```html
<div class="transit-row">
  <label>Truck/Pickup duration</label>
  <select id="transit-truck-dur">
    <option value="240">4 hours</option>
    <option value="480" selected>8 hours</option>
    <option value="720">12 hours</option>
  </select>
</div>
<div class="transit-row">
  <label>Air/Rail/Ship duration (hours)</label>
  <input id="transit-other-dur" type="number" min="0" step="0.5" placeholder="user-defined" />
</div>
<div class="transit-row" id="manual-drop-row" hidden>
  <label>Manual-handling drop height</label>
  <select id="transit-drop-h">
    <option value="0.5">0.5 m</option>
    <option value="1.0" selected>1.0 m</option>
    <option value="1.5">1.5 m</option>
  </select>
</div>
```

- [ ] **Step 2: Read the controls into `transitState` and into the preview/brief payload**

In `app.js`, extend `transitState` (`:1854-1861`) with `durations_min: {}` and `manual_drop_height_m: 1.0`. In `_doPreview` (`:1927-1956`) and `pushTransitToBrief` (`:1958+`) build a `durations_min` map:

```js
const truckDur = Number(document.getElementById("transit-truck-dur").value);
const otherHrs = Number(document.getElementById("transit-other-dur").value || 0);
const durations_min = {
  truck: truckDur, pickup: truckDur,
  air: otherHrs * 60, rail: otherHrs * 60, ship: otherHrs * 60,
};
const manual_drop_height_m = Number(document.getElementById("transit-drop-h").value);
// include in POST /transit/preview body and in the PATCH brief body
```

Show `#manual-drop-row` only when the manual_handling slider > 0 (toggle `hidden` in the slider `input` handler).

- [ ] **Step 3: Accept the params in `/transit/preview` and the brief PATCH**

In `backend/routes/extras.py` `transit_preview` (`:966-978`), read `durations_min` and `manual_drop_height_m` from the request body and forward to `td.blended_envelope(...)`. Add `transit_durations_min` and `manual_drop_height_m` to the brief fields the case PATCH accepts (the orchestrator already reads `s.get(...)` for transit config at `:891-894`; add reads for these two and pass to `TransitAgent.build`).

- [ ] **Step 4: Manual verification**

Run the app. On the Transit stage, set truck = 12 h, add manual handling with 1.5 m drop, Preview. Expected: the preview `drop_height_m` shows 1.5 and the resulting ISTA-2A vibration verdict (after running analysis) reflects the longer duration (more cycles).

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html frontend/app.js backend/routes/extras.py backend/orchestrator/orchestrator.py
git commit -m "feat(transit-ui): duration presets (4/8/12h), user-defined air/rail/ship duration, manual drop-height selector"
```

---

# PART C — Optimization Engine: Objective-Specific + Product-Specific

**Current state (verified):**
- None of the three optimizers rank/select by the chosen objective. Selection is "first N that pass the gate, de-duplicated":
  - Bottle `optimization.py:547-561` (gate = `passes_ista`).
  - Packet `packet_optimization.py:543-555` (gate = unique signature).
  - Brush `brush_optimizer.py:482-490` (gate = unique signature).
- `intent` only reaches the LLM prompt and offline fallback branches; cost/ROI/scores are computed **after** the slate is frozen (`optimization.py:599-600`, `packet:599`, `brush:529`).
- Bottle force-inserts a PCR-first variant (`optimization.py:537-541`) and Aluminum-heavy fallbacks (`:480-483`), both of which bias *against* `reduce_cost`.
- No server-side family guard: the three endpoints (`extras.py:563/649/718`) never check `case.case_summary` family; routing is frontend-only (`app.js:3664-3669`) and `_effectiveFamily()` returns `null` for unrecognized families, falling through to the bottle path.
- Charts mapping (`extras.py:398-431`) coerces packet/brush scores into bottle/ISTA-named axes (`min_safety_factor`, `mass_g`, `passes_ista=True`).

**File structure for Part C:**
- Create `backend/agents/objective_ranking.py` — one pure, tested ranking function shared by all three optimizers.
- Modify the three optimizers to call it before truncating to N.
- Modify `backend/routes/extras.py` — add a family guard decorator/helper and fix the charts axis leakage.

---

### Task C1: Shared objective-ranking function

**Files:**
- Create: `backend/agents/objective_ranking.py`
- Test: `backend/tests/test_objective_ranking.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_objective_ranking.py
from backend.agents.objective_ranking import rank_variants, objective_metric


def test_reduce_cost_orders_by_ascending_cost():
    variants = [
        {"name": "A", "cost_per_unit": 0.30},
        {"name": "B", "cost_per_unit": 0.10},
        {"name": "C", "cost_per_unit": 0.20},
    ]
    ranked = rank_variants(variants, intent="reduce_cost")
    assert [v["name"] for v in ranked] == ["B", "C", "A"]


def test_increase_strength_orders_by_descending_safety_factor():
    variants = [
        {"name": "A", "min_safety_factor": 1.2},
        {"name": "B", "min_safety_factor": 2.0},
    ]
    ranked = rank_variants(variants, intent="increase_strength")
    assert [v["name"] for v in ranked] == ["B", "A"]


def test_reduce_cost_drops_variants_worse_than_baseline_when_strict():
    variants = [
        {"name": "cheaper", "cost_impact_pct": -10},
        {"name": "pricier", "cost_impact_pct": +15},
    ]
    ranked = rank_variants(variants, intent="reduce_cost",
                           baseline_relative_key="cost_impact_pct", strict=True)
    assert [v["name"] for v in ranked] == ["cheaper"]   # pricier dropped


def test_unknown_intent_is_stable_passthrough():
    variants = [{"name": "A"}, {"name": "B"}]
    assert rank_variants(variants, intent="other") == variants
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_objective_ranking.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the ranker**

```python
# backend/agents/objective_ranking.py
"""Objective-aware ranking shared by bottle/packet/brush optimizers.

Selection MUST be driven by the user's objective. Each optimizer scores its
variants on its own metrics; this module turns the chosen objective into a
(key, direction) and sorts. Direction: "min" => ascending, "max" => descending.
"""
from __future__ import annotations
from typing import Any

# intent -> (metric_key, direction). Keys must exist on the variant dicts the
# optimizers already produce (see optimization.py DesignVariant, etc.).
_OBJECTIVE_MAP: dict[str, tuple[str, str]] = {
    "reduce_cost": ("cost_per_unit", "min"),
    "reduce_weight": ("mass_g", "min"),
    "increase_strength": ("min_safety_factor", "max"),
    "improve_survivability": ("transit_score", "max"),
    "improve_shelf_life": ("barrier_score", "max"),
    "improve_sustainability": ("material_score", "max"),
}
# Packet/brush use cost_impact_pct instead of absolute cost_per_unit.
_COST_FALLBACK_KEYS = ("cost_per_unit", "cost_impact_pct")


def objective_metric(intent: str) -> tuple[str, str] | None:
    return _OBJECTIVE_MAP.get(intent)


def _value(variant: dict[str, Any], key: str) -> float | None:
    if key == "cost_per_unit":
        for k in _COST_FALLBACK_KEYS:
            if variant.get(k) is not None:
                return float(variant[k])
        return None
    v = variant.get(key)
    return None if v is None else float(v)


def rank_variants(
    variants: list[dict[str, Any]],
    *,
    intent: str,
    baseline_relative_key: str | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return variants ordered best-first for `intent`.

    - Stable for unknown intents (returns the input order).
    - Variants missing the metric sort last (never silently first).
    - strict=True drops variants that are worse-than-baseline on
      `baseline_relative_key` (e.g. cost_impact_pct > 0 for reduce_cost).
    """
    spec = objective_metric(intent)
    if spec is None:
        return list(variants)
    key, direction = spec

    pool = list(variants)
    if strict and baseline_relative_key:
        worse = (lambda x: x > 0) if direction == "min" else (lambda x: x < 0)
        pool = [v for v in pool if not worse(float(v.get(baseline_relative_key, 0) or 0))]

    missing = [v for v in pool if _value(v, key) is None]
    present = [v for v in pool if _value(v, key) is not None]
    present.sort(key=lambda v: _value(v, key), reverse=(direction == "max"))
    return present + missing
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_objective_ranking.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/objective_ranking.py backend/tests/test_objective_ranking.py
git commit -m "feat(optimize): shared objective-aware variant ranker"
```

---

### Task C2: Apply ranking in all three optimizers (fix "reduce cost returns higher cost")

**Files:**
- Modify: `backend/agents/optimization.py:537-561` (bottle)
- Modify: `backend/agents/packet_optimization.py:543-555`
- Modify: `backend/agents/brush_optimizer.py:482-490`
- Test: `backend/tests/test_optimizer_objective.py`

- [ ] **Step 1: Write the failing test (bottle)**

```python
# backend/tests/test_optimizer_objective.py
from backend.agents.optimization import OptimizationAgent


def test_bottle_reduce_cost_never_ranks_pricier_above_cheaper(monkeypatch):
    """Given a slate of ISTA-passing variants, reduce_cost must order the
    cheapest first regardless of PCR-first insertion."""
    agent = OptimizationAgent()
    # Build synthetic variants (bypass LLM): two pass ISTA, differing on cost.
    cheap = {"name": "cheap", "cost_per_unit": 0.10, "passes_ista": True, "mass_g": 20}
    dear = {"name": "dear", "cost_per_unit": 0.40, "passes_ista": True, "mass_g": 18}
    out = agent._finalise_slate([dear, cheap], intent="reduce_cost", target_passing=2)
    assert [v["name"] for v in out][0] == "cheap"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_optimizer_objective.py -v`
Expected: FAIL — `_finalise_slate` does not exist (selection is inline in `generate_alternatives`).

- [ ] **Step 3: Extract + rank in the bottle optimizer**

Refactor the selection loop (`optimization.py:537-561`) into a `_finalise_slate(self, candidates, *, intent, target_passing)` that (a) keeps the ISTA gate + dedup, then (b) ranks by objective before truncating:

```python
from backend.agents.objective_ranking import rank_variants

def _finalise_slate(self, candidates, *, intent, target_passing):
    seen, passing = set(), []
    for v in candidates:
        d = v if isinstance(v, dict) else v.model_dump()
        if not d.get("passes_ista"):
            continue
        sig = self._signature(v)
        if sig in seen:
            continue
        seen.add(sig)
        passing.append(d)
    # Objective-aware ranking BEFORE truncation (was: first-N).
    ranked = rank_variants(passing, intent=intent,
                           baseline_relative_key="roi_pct", strict=False)
    return ranked[:target_passing]
```

Replace the inline loop in `generate_alternatives` with a call to `_finalise_slate(all_candidates, intent=intent, target_passing=target_passing)`. **Stop force-prepending the PCR-first variant unconditionally** (`:537-541`): instead add the PCR-first candidate into `all_candidates` so it competes on the objective like any other (still guaranteed present, no longer guaranteed slot 1).

- [ ] **Step 4: Apply the same pattern to packet + brush**

`packet_optimization.py:543-555` — after building `alternatives` (dedup), rank:

```python
from backend.agents.objective_ranking import rank_variants
alternatives = rank_variants(
    [a.model_dump() for a in alternatives], intent=intent,
    baseline_relative_key="cost_impact_pct",
    strict=(intent == "reduce_cost"),
)[:3]
```

`brush_optimizer.py:482-490` — identical, with `baseline_relative_key="cost_impact_pct"`.

(Because packet/brush variants are pydantic models, rank on `model_dump()` dicts and re-wrap or keep dicts consistently with how `result.model_dump()` is built at `packet:599`/`brush:529`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_optimizer_objective.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/agents/optimization.py backend/agents/packet_optimization.py backend/agents/brush_optimizer.py backend/tests/test_optimizer_objective.py
git commit -m "fix(optimize): rank variants by objective before truncation (reduce_cost now returns cheapest)"
```

---

### Task C3: Server-side family guard (fix "packet returns bottle logic")

**Files:**
- Modify: `backend/routes/extras.py:563-615` (bottle), `:649-684` (packet), `:718-753` (brush)
- Create: helper `_assert_family` in `backend/routes/extras.py`
- Test: `backend/tests/test_optimizer_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_optimizer_routing.py
import pytest
from backend.routes.extras import _resolve_family, FamilyMismatch


def test_resolve_family_from_case_summary():
    assert _resolve_family({"packaging_family": "packet"}) == "packet"
    assert _resolve_family({"packaging_type": "pouch"}) == "packet"
    assert _resolve_family({"packaging_type": "bottle"}) == "bottle"
    assert _resolve_family({"packaging_type": "toothbrush"}) == "brush"


def test_family_guard_rejects_wrong_endpoint():
    with pytest.raises(FamilyMismatch):
        _assert_family({"packaging_type": "pouch"}, expected="bottle")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_optimizer_routing.py -v`
Expected: FAIL — helpers don't exist.

- [ ] **Step 3: Implement a deterministic family resolver + guard**

In `backend/routes/extras.py`, near the top:

```python
from fastapi import HTTPException

class FamilyMismatch(HTTPException):
    def __init__(self, expected: str, actual: str):
        super().__init__(status_code=409,
            detail=f"This optimizer is for '{expected}', but the case is '{actual}'.")

_PACKET_WORDS = ("pouch", "packet", "sachet", "stickpack", "laminate")
_BRUSH_WORDS = ("brush", "toothbrush")

def _resolve_family(case_summary: dict) -> str:
    fam = (case_summary.get("packaging_family") or "").strip().lower()
    if fam in ("bottle", "packet", "brush"):
        return fam
    ptype = (case_summary.get("packaging_type") or "").strip().lower()
    if any(w in ptype for w in _PACKET_WORDS):
        return "packet"
    if any(w in ptype for w in _BRUSH_WORDS):
        return "brush"
    return "bottle"   # explicit default, never silent fall-through

def _assert_family(case_summary: dict, *, expected: str) -> None:
    actual = _resolve_family(case_summary)
    if actual != expected:
        raise FamilyMismatch(expected, actual)
```

- [ ] **Step 4: Call the guard in each endpoint**

At the top of each optimize route body, after loading the case:
- `/optimize/run` (`:563`): `_assert_family(case.case_summary or {}, expected="bottle")`
- `/packet-optimize/run` (`:649`): `_assert_family(..., expected="packet")`
- `/brush-optimize/run` (`:718`): `_assert_family(..., expected="brush")`

Also validate `intent` against the family's allowed set before dispatch (allowed sets: bottle `optimization.py:355`, packet `:359`, brush `:322`); on mismatch raise `HTTPException(422, ...)`.

- [ ] **Step 5: Make the frontend route by the resolved family**

`frontend/app.js:3664-3669` — replace the `_effectiveFamily()` fall-through (which returns `null` → bottle) with a call to a `/cases/{id}/family` lookup (add a tiny route returning `_resolve_family(case.case_summary)`), so the UI dispatches to the same family the backend will accept. On a `409 FamilyMismatch`, surface the server message instead of silently hitting the bottle path.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_optimizer_routing.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/routes/extras.py frontend/app.js backend/tests/test_optimizer_routing.py
git commit -m "fix(optimize): server-side family guard + intent validation (packet no longer falls through to bottle)"
```

---

### Task C4: De-leak bottle/ISTA vocabulary from packet/brush charts

**Files:**
- Modify: `backend/routes/extras.py:371-431` (`build_charts`)
- Test: `backend/tests/test_optimizer_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_optimizer_routing.py
from backend.routes.extras import build_charts


def test_packet_charts_use_packet_axes_not_bottle():
    rows = [{"name": "v1", "cost_impact_pct": -5, "seal_score": 0.8,
             "transit_score": 0.7, "barrier_score": 0.9}]
    charts = build_charts(rows, family="packet")
    keys = set(charts["comparison"][0].keys())
    assert "min_safety_factor" not in keys      # bottle/ISTA term must be gone
    assert "passes_ista" not in keys
    assert {"seal_score", "transit_score", "barrier_score"} <= keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_optimizer_routing.py -k charts -v`
Expected: FAIL — current mapping (`:398-410`) forces packet scores into `min_safety_factor`/`mass_g`/`passes_ista`.

- [ ] **Step 3: Branch the chart mapping by family**

In `build_charts` (`:371-431`), replace the coerce-to-bottle-axes blocks with family-native axes:

```python
if family == "packet":
    series = [{"name": r["name"], "cost_impact_pct": r.get("cost_impact_pct") or 0,
               "seal_score": r.get("seal_score") or 0,
               "transit_score": r.get("transit_score") or 0,
               "barrier_score": r.get("barrier_score") or 0,
               "puncture_score": r.get("puncture_score") or 0} for r in rows]
elif family == "brush":
    series = [{"name": r["name"], "cost_impact_pct": r.get("cost_impact_pct") or 0,
               "blister_score": r.get("blister_score") or 0,
               "transit_score": r.get("transit_score") or 0,
               "material_score": r.get("material_score") or 0,
               "compression_score": r.get("compression_score") or 0} for r in rows]
else:  # bottle
    series = [{"name": r["name"], "cost_per_unit": r.get("cost_per_unit") or 0,
               "min_safety_factor": r.get("min_safety_factor") or 0,
               "mass_g": r.get("mass_g") or 0, "roi_pct": r.get("roi_pct") or 0,
               "passes_ista": bool(r.get("passes_ista"))} for r in rows]
```

Pass `family` from each optimize route into `build_charts` (it already knows the family from the guard in C3).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_optimizer_routing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/routes/extras.py backend/tests/test_optimizer_routing.py
git commit -m "fix(optimize): family-native chart axes (no bottle/ISTA terms on packet/brush dashboards)"
```

---

# PART D — PCR Input Mapping / Flute Fidelity

**Current state (verified):**
- Only one corrugated record exists: `Corrugated B-flute`, grade `32 ECT` (`backend/data/materials.json:185-197`). No E-flute or C-flute record.
- E/C-flute are hard-aliased to B-flute in **four** places: `pcr.py:51-53`, `material.py:50-51`, `optimization.py:68-69`, `price_cache.py:78-79`; and the route mapper `_carton_type_to_material()` (`cases.py:649-669`, esp. `:660`) returns `"Corrugated B-flute"` for E/B/C alike.
- The flute letter changes a number in exactly **one** place — `_estimate_carton_volume_mm3` (`cases.py:694-721`): E→3.0 mm, B/C→5.0 mm (B and C conflated; real E-flute ≈1.5 mm) — and only affects PCR carbon/mass, never strength/ECT.
- `carton_board_grade` is free text (`bottle_flow.py:58` etc., `options: None`); no schema field, no enum, no provenance.

**File structure for Part D:**
- Modify `backend/data/materials.json` — add E-flute and C-flute records (+ PCR analogues).
- Create `backend/agents/flute_resolver.py` — single source of truth: flute string → canonical record name + board caliper + ECT.
- Modify `pcr.py`, `material.py`, `optimization.py`, `price_cache.py`, `cases.py` to call the resolver instead of hard aliases.
- Modify `backend/schemas.py` — validated `carton_board_grade` enum + provenance on calc output.

---

### Task D1: Per-flute material records

**Files:**
- Modify: `backend/data/materials.json`
- Test: `backend/tests/test_flute_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_flute_resolver.py
import json
from pathlib import Path

MATERIALS = json.loads((Path(__file__).parents[1] / "data" / "materials.json").read_text())


def _by_name(name):
    return next((m for m in MATERIALS if m["name"] == name), None)


def test_three_flute_grades_exist_with_distinct_properties():
    e, b, c = _by_name("Corrugated E-flute"), _by_name("Corrugated B-flute"), _by_name("Corrugated C-flute")
    assert e and b and c
    # Distinct ECT and caliper — they must NOT be identical.
    ects = {m["grade"] for m in (e, b, c)}
    calipers = {m["caliper_mm"] for m in (e, b, c)}
    assert len(ects) == 3 and len(calipers) == 3
    assert e["caliper_mm"] < b["caliper_mm"] < c["caliper_mm"]   # E≈1.5 < B≈3 < C≈4 mm
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_flute_resolver.py -v`
Expected: FAIL — E/C records and `caliper_mm` field are missing.

- [ ] **Step 3: Add E-flute and C-flute records (+ `caliper_mm` on B-flute)**

In `backend/data/materials.json`, add a `caliper_mm` field to the existing B-flute record (`:185-197`) and append two records (representative single-wall values):

```json
{
  "name": "Corrugated E-flute", "grade": "23 ECT",
  "density_kg_m3": 140, "modulus_gpa": 0.55,
  "yield_strength_mpa": 3.2, "allowable_stress_mpa": 1.1,
  "caliper_mm": 1.5, "ect_kn_m": 4.0,
  "carbon_intensity_kg_per_kg": 0.94
},
{
  "name": "Corrugated C-flute", "grade": "44 ECT",
  "density_kg_m3": 125, "modulus_gpa": 0.65,
  "yield_strength_mpa": 4.6, "allowable_stress_mpa": 1.8,
  "caliper_mm": 4.0, "ect_kn_m": 7.7,
  "carbon_intensity_kg_per_kg": 0.94
}
```

Set the B-flute record's `caliper_mm` to `3.0` and add `ect_kn_m: 5.6`. Add PCR analogues (`PCR-Corrugated-E`, `PCR-Corrugated-C`) mirroring the existing `PCR-Corrugated` (`:213-225`) with `pcr_substitute_for` set to the matching flute name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_flute_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/data/materials.json backend/tests/test_flute_resolver.py
git commit -m "feat(pcr): per-flute corrugated records (E/B/C) with distinct ECT + caliper"
```

---

### Task D2: Single flute resolver replaces the hard aliases

**Files:**
- Create: `backend/agents/flute_resolver.py`
- Modify: `backend/agents/pcr.py:30-61`, `backend/agents/material.py:50-51`, `backend/agents/optimization.py:68-69`, `backend/services/price_cache.py:78-79`, `backend/routes/cases.py:649-721`
- Test: `backend/tests/test_flute_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_flute_resolver.py
from backend.agents.flute_resolver import resolve_flute, FluteSpec


def test_resolver_distinguishes_e_b_c():
    assert resolve_flute("E-flute").record_name == "Corrugated E-flute"
    assert resolve_flute("b flute").record_name == "Corrugated B-flute"
    assert resolve_flute("3-ply C-Flute").record_name == "Corrugated C-flute"


def test_resolver_caliper_matches_record():
    assert resolve_flute("E-flute").caliper_mm == 1.5
    assert resolve_flute("C-flute").caliper_mm == 4.0


def test_unknown_flute_falls_back_with_flag():
    spec = resolve_flute("mystery board")
    assert spec.record_name == "Corrugated B-flute"
    assert spec.is_fallback is True       # never silently pretend it was exact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_flute_resolver.py -k resolver -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the resolver**

```python
# backend/agents/flute_resolver.py
"""Single source of truth: a user's free-text board grade -> the exact
corrugated MaterialRecord name + physical board params. Replaces the
scattered '*-flute -> Corrugated B-flute' hard aliases."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class FluteSpec:
    record_name: str
    caliper_mm: float
    ect_kn_m: float
    is_fallback: bool

# Order matters: check E and C before the bare 'flute'/'corrugated' fallback.
_FLUTE_TABLE = [
    (("e-flute", "e flute", "eflute"), FluteSpec("Corrugated E-flute", 1.5, 4.0, False)),
    (("c-flute", "c flute", "cflute"), FluteSpec("Corrugated C-flute", 4.0, 7.7, False)),
    (("b-flute", "b flute", "bflute"), FluteSpec("Corrugated B-flute", 3.0, 5.6, False)),
]
_FALLBACK = FluteSpec("Corrugated B-flute", 3.0, 5.6, True)

def resolve_flute(board_grade: str | None) -> FluteSpec:
    g = (board_grade or "").strip().lower()
    for needles, spec in _FLUTE_TABLE:
        if any(n in g for n in needles):
            return spec
    return _FALLBACK
```

- [ ] **Step 4: Replace the hard aliases with resolver calls**

- `routes/cases.py:_carton_type_to_material` (`:649-669`): replace the `:660-661` block with `return resolve_flute(board_grade).record_name`.
- `routes/cases.py:_estimate_carton_volume_mm3` (`:694-721`): replace the `board_t` if/elif ladder (`:711-720`) with `board_t = resolve_flute(s.get("carton_board_grade")).caliper_mm`.
- `pcr.py`: delete the `e-flute/b-flute/c-flute → Corrugated B-flute` rows from `_NAME_ALIASES` (`:51-53`); in `_canonicalise` (`:57-61`) call `resolve_flute(name).record_name` first when the string contains "flute"/"corrugat".
- `material.py:50-51`, `optimization.py:68-69`, `price_cache.py:78-79`: route corrugated/flute strings through `resolve_flute(...).record_name` instead of the literal `"Corrugated B-flute"`.

- [ ] **Step 5: Run the resolver + PCR tests**

Run: `python -m pytest backend/tests/test_flute_resolver.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/agents/flute_resolver.py backend/agents/pcr.py backend/agents/material.py backend/agents/optimization.py backend/services/price_cache.py backend/routes/cases.py backend/tests/test_flute_resolver.py
git commit -m "fix(pcr): single flute resolver — E/C-flute no longer collapse to B-flute"
```

---

### Task D3: Validate the input + carry flute provenance into calculations

**Files:**
- Modify: `backend/schemas.py` (add `carton_board_grade` normaliser + `board_grade_used` provenance on calc/analysis output)
- Modify: `backend/agents/calculation.py` (echo the resolved board grade into `inputs`)
- Modify: `backend/agents/pcr.py` (include resolved record in `PCRSubstitution`)
- Test: `backend/tests/test_flute_provenance.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_flute_provenance.py
from backend.agents.pcr import PCRAgent
from backend.agents.flute_resolver import resolve_flute


def test_pcr_records_which_board_was_used():
    # When E-flute is the input, the PCR result must say E-flute drove it.
    spec = resolve_flute("E-flute")
    assert spec.record_name == "Corrugated E-flute"
    # Provenance contract: the substitution carries the resolved baseline name.
    # (asserted against PCRAgent.evaluate output shape in integration)
    assert spec.is_fallback is False
```

- [ ] **Step 2: Run test to verify it fails / is incomplete**

Run: `python -m pytest backend/tests/test_flute_provenance.py -v`
Expected: PASS for the unit contract; the provenance field on `PCRSubstitution` is what we now add.

- [ ] **Step 3: Add a validated normaliser on the input**

In `backend/schemas.py`, add to the case-summary-bearing model a normaliser that tags the canonical grade (do not reject free text — normalise + flag):

```python
from pydantic import field_validator

# wherever carton_board_grade is accepted (or add to IntakeFields ~:54-84)
carton_board_grade: str | None = None
board_grade_canonical: str | None = None   # filled by validator

@field_validator("board_grade_canonical", mode="before")
@classmethod
def _canon_board(cls, _v, info):
    from backend.agents.flute_resolver import resolve_flute
    raw = info.data.get("carton_board_grade")
    return resolve_flute(raw).record_name if raw else None
```

- [ ] **Step 4: Echo provenance through calc + PCR outputs**

- `calculation.py`: every method that builds an `inputs` dict for guardrail (`CalculationOutput`) must include `board_grade_used` when a carton is in play, sourced from `resolve_flute(...).record_name`.
- `pcr.py:evaluate` (`:102-206`): add `baseline_record_used` and `board_grade_used` to the returned `PCRSubstitution` (schema `:186-219`) so the report can show "PCR computed against Corrugated E-flute".

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_flute_provenance.py backend/tests/test_flute_resolver.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/schemas.py backend/agents/calculation.py backend/agents/pcr.py backend/tests/test_flute_provenance.py
git commit -m "feat(pcr): validate + record board-grade provenance through calc and PCR outputs"
```

---

# PART E — Physics-Based Stress Heatmaps

**Current state (verified):**
- `heatmap.compute_field` (`backend/services/heatmap.py:167-296`) is purely positional/geometric. It reads only `transit_env.vibration_g_rms`, `transit_env.compression_load_n` (via `getattr`), and material `name`/`modulus_gpa`. The `impact_velocity_m_s` param (`:174`) is accepted but **never used**. `material.yield_strength_mpa` and `wall_thickness` are unused.
- Normalization is **per-scene min–max** (`_normalise`, `:135-139`), so absolute stress / safety-factor magnitude is invisible and scenes aren't comparable.
- The real mechanics already exist and are unused by the heatmap: `ista2a._drop_verdict` (`:240-250`) computes `v=√(2gh)`, `a_peak`, `F_peak`, `σ_local = F/A · Kt`; `KT_BY_ORIENTATION` (`:99-104`); `CONTACT_AREA_MM2` (`:89-94`); `calculation.thin_wall_buckling_check` (`:82-124`) computes `σ_cr`.
- Carton field (`_compute_carton_field`, `:303-383`) cites McKee/ECT only in comments; it has no board input (now available via Part D's `ect_kn_m`/`caliper_mm`).

**Design:** Do **not** invent a fourth stress model. Plumb the per-orientation `σ_local` and `σ_y` that `ista2a` already computes into `compute_field`, scale the existing positional pattern by the physical **utilization** `u = σ_local/σ_y`, and switch normalization from per-scene min–max to a **fixed yield-referenced scale** (0 → 1.0 = yield, clamp at 1.5) so red means "at yield" and scenes are comparable. For cartons, compute **McKee BCT** from the now-available ECT/caliper and reference the applied column load.

**File structure for Part E:**
- Modify `backend/agents/ista2a.py` — expose a small `stress_field_inputs()` returning per-orientation `{sigma_local_mpa, sigma_yield_mpa, kt}`.
- Modify `backend/services/heatmap.py` — `compute_field` consumes those, scales by utilization, fixed-scale normalize; `_compute_carton_field` consumes McKee BCT.
- Modify `backend/services/visualization_service.py` — pass material yield + ista2a outputs through; report real `scale` units.

---

### Task E1: Expose ISTA-2A per-orientation stresses for the heatmap

**Files:**
- Modify: `backend/agents/ista2a.py` (add `stress_field_inputs`)
- Test: `backend/tests/test_heatmap_physics.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_heatmap_physics.py
from backend.agents.ista2a import Ista2AAgent


def test_stress_field_inputs_returns_physical_utilisation():
    agent = Ista2AAgent()
    out = agent.stress_field_inputs(
        mass_kg=0.5, drop_height_m=0.61,
        material={"yield_strength_mpa": 55.0, "allowable_stress_mpa": 35.0},
    )
    for orient in ("top", "bottom", "side", "corner"):
        assert out[orient]["sigma_local_mpa"] > 0
        assert out[orient]["sigma_yield_mpa"] == 55.0
        assert out[orient]["utilisation"] == out[orient]["sigma_local_mpa"] / 55.0
    # Corner concentrates more than side (Kt 2.5 vs 1.8).
    assert out["corner"]["utilisation"] > out["side"]["utilisation"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_heatmap_physics.py -v`
Expected: FAIL — `stress_field_inputs` doesn't exist.

- [ ] **Step 3: Implement `stress_field_inputs` by reusing the existing impulse math**

In `ista2a.py`, factor the σ_local computation already inside `_drop_verdict` (`:240-250`) into a reusable method, then:

```python
def stress_field_inputs(self, *, mass_kg, drop_height_m, material):
    sy = float(material.get("yield_strength_mpa") or FALLBACK_YIELD_MPA)
    out = {}
    for orient in ("top", "bottom", "side", "corner"):
        v = (2.0 * GRAVITY * drop_height_m) ** 0.5
        stop = STOPPING_DISTANCE_M[orient]
        a_peak = (v * v) / (2.0 * stop) * PULSE_SHAPE_FACTOR
        f_peak = mass_kg * a_peak
        area_m2 = CONTACT_AREA_MM2[orient] * 1e-6
        kt = KT_BY_ORIENTATION[orient]
        sigma_local_mpa = (f_peak / area_m2) * kt / 1e6
        out[orient] = {
            "sigma_local_mpa": sigma_local_mpa,
            "sigma_yield_mpa": sy,
            "kt": kt,
            "utilisation": sigma_local_mpa / sy if sy else 0.0,
        }
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_heatmap_physics.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/ista2a.py backend/tests/test_heatmap_physics.py
git commit -m "feat(heatmap): expose ISTA-2A per-orientation sigma_local/yield utilisation"
```

---

### Task E2: Scale the heatmap field by physical utilization + fixed yield-referenced scale

**Files:**
- Modify: `backend/services/heatmap.py:167-296` (`compute_field`), `:135-139` (`_normalise` → fixed-scale variant)
- Modify: `backend/services/visualization_service.py:70-151` (pass ista2a inputs + material yield)
- Test: `backend/tests/test_heatmap_physics.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_heatmap_physics.py
import numpy as np
import trimesh
from backend.services import heatmap as hm


def test_field_scale_is_yield_referenced_not_minmax():
    mesh = trimesh.creation.box(extents=(40, 40, 120))
    field = hm.compute_field(
        mesh, "drop_corner",
        material={"yield_strength_mpa": 55.0, "modulus_gpa": 2.0, "name": "PET"},
        stress_inputs={"corner": {"utilisation": 0.8}},  # 80% of yield
    )
    # Fixed scale: max color intensity corresponds to utilisation, not 1.0 always.
    assert field.scale["mode"] == "yield_referenced"
    assert field.scale["max_utilisation"] == 0.8
    assert field.scale["units"] == "sigma_local/sigma_yield"


def test_two_scenes_are_comparable_under_fixed_scale():
    mesh = trimesh.creation.box(extents=(40, 40, 120))
    weak = hm.compute_field(mesh, "drop_corner",
        material={"yield_strength_mpa": 55.0, "modulus_gpa": 2.0, "name": "PET"},
        stress_inputs={"corner": {"utilisation": 0.3}})
    strong = hm.compute_field(mesh, "drop_corner",
        material={"yield_strength_mpa": 55.0, "modulus_gpa": 2.0, "name": "PET"},
        stress_inputs={"corner": {"utilisation": 0.9}})
    # Higher utilisation => higher peak color index (comparable across scenes).
    assert np.max(strong.per_face_stress) > np.max(weak.per_face_stress)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_heatmap_physics.py -k "scale or comparable" -v`
Expected: FAIL — `compute_field` has no `stress_inputs` param and uses min–max normalization.

- [ ] **Step 3: Add a fixed-scale normalizer**

In `heatmap.py`, add beside `_normalise` (`:135-139`):

```python
def _scale_to_yield(stress_pattern: np.ndarray, utilisation: float,
                    clamp: float = 1.5) -> np.ndarray:
    """Map a 0..1 positional pattern onto absolute utilisation, then to a
    FIXED 0..clamp colour scale (1.0 == yield). Scenes become comparable."""
    pat = _normalise(stress_pattern)            # shape only, 0..1
    absolute = pat * float(utilisation)         # physical utilisation per face
    return np.clip(absolute / clamp, 0.0, 1.0)  # fixed scale, not per-scene
```

- [ ] **Step 4: Consume `stress_inputs` in `compute_field`**

Extend the signature and replace the final normalization (`:275-279`):

```python
def compute_field(mesh, scenario, *, transit_env=None, material=None,
                  stacking_orientation="upright", impact_velocity_m_s=None,
                  stress_inputs=None) -> StressField:
    ...
    # existing positional pattern -> `stress` (unchanged, gives the SHAPE)
    orient_key = {"drop_top": "top", "drop_bottom": "bottom",
                  "drop_side": "side", "drop_corner": "corner"}.get(scenario)
    util = 1.0
    if stress_inputs and orient_key in (stress_inputs or {}):
        util = float(stress_inputs[orient_key]["utilisation"])
    stress_norm = _scale_to_yield(stress * brittle_amp, util)
    field_colors = _stress_to_color(stress_norm)
    scale = {"mode": "yield_referenced", "units": "sigma_local/sigma_yield",
             "max_utilisation": round(util, 3), "yield_at": 1.0, "clamp": 1.5}
```

Keep the `transit` scenario branch (`:242-269`) but replace the magic `comp_n/4000` with a utilization derived from `compression_load_n` vs the material's compressive allowable (or McKee BCT for cartons, Task E3); pass that as the transit `utilisation`.

- [ ] **Step 5: Plumb the inputs from `visualization_service`**

`visualization_service.build_heatmap_scenes` (`:70-151`) — before the scenario loop, call `Ista2AAgent().stress_field_inputs(mass_kg=..., drop_height_m=transit_env.drop_height_m, material=material_dict)` and pass `stress_inputs=...` plus `material` into each `hm.compute_field` call (`:119-125`). Emit the real `scale` per scene (replace the inconsistent `"viridis"/250` vs `"fea-jet"` reporting at `heatmap.py:292-293` / `:379-380` with the `scale` dict from Step 4).

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_heatmap_physics.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/services/heatmap.py backend/services/visualization_service.py backend/tests/test_heatmap_physics.py
git commit -m "feat(heatmap): yield-referenced fixed scale driven by ISTA-2A utilisation (scenes now comparable)"
```

---

### Task E3: McKee BCT for carton compression heatmap

**Files:**
- Modify: `backend/services/heatmap.py:303-428` (`_compute_carton_field`, `build_carton_scenes`)
- Test: `backend/tests/test_heatmap_physics.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_heatmap_physics.py
from backend.services.heatmap import mckee_bct_n


def test_mckee_bct_increases_with_ect_and_caliper():
    # BCT = 5.87 * ECT * sqrt(perimeter_mm * caliper_mm)
    weak = mckee_bct_n(ect_kn_m=4.0, caliper_mm=1.5, perimeter_mm=1000)   # E-flute
    strong = mckee_bct_n(ect_kn_m=7.7, caliper_mm=4.0, perimeter_mm=1000) # C-flute
    assert strong > weak > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_heatmap_physics.py -k mckee -v`
Expected: FAIL — `mckee_bct_n` doesn't exist.

- [ ] **Step 3: Implement McKee + consume the flute spec**

In `heatmap.py`:

```python
def mckee_bct_n(*, ect_kn_m: float, caliper_mm: float, perimeter_mm: float) -> float:
    """McKee box compression estimate. ECT in kN/m, caliper & perimeter in mm.
    Returns BCT in newtons."""
    ect_n_per_mm = ect_kn_m            # kN/m == N/mm
    return 5.87 * ect_n_per_mm * (perimeter_mm * caliper_mm) ** 0.5
```

In `build_carton_scenes` (`:386-428`), resolve the board via Part D's resolver from `case_summary["carton_board_grade"]`, compute `bct = mckee_bct_n(...)` and the applied column load (stack mass · g), then pass `utilisation = applied_load / bct` into `_compute_carton_field` so the carton field is scaled by the real compression margin (replace the comment-only McKee at `:329-331`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_heatmap_physics.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/heatmap.py backend/tests/test_heatmap_physics.py
git commit -m "feat(heatmap): McKee BCT-driven carton compression field (consumes E/B/C-flute ECT)"
```

---

# PART F — Cross-Module Consistency Validation

**Current state (verified):**
- `AgentContext` (`backend/agents/base.py:14-19`) carries only identity — no design config. Every module re-reads `case.case_summary` ad-hoc via `.get(...)`.
- Divergence points: mass computed twice (`orchestrator.py:923-926` vs `:1051-1053`); ISTA-2A re-derives its own drop heights independent of `transit_env.drop_height_m`; PCR runs in a separate flow with its own aliasing; report gets `ista2a` but **not** `ista6a` (`:1023`); `Case` columns vs `case_summary` drift via `or`-only updates (`:197-199`).
- `GuardrailAgent` (`guardrail.py`) validates single payloads only (`review_text/calculation/material`); there is no `review_consistency`. `ReasoningAgent.cross_check_ista`/`verify` are advisory LLM passes wrapped in failure-swallowing `try/except` (`:1084`, `:1183`).

**File structure for Part F:**
- Create `backend/agents/design_config.py` — a typed `DesignConfig` snapshot built once.
- Modify `backend/orchestrator/orchestrator.py` — build the snapshot once, thread it, hand `ista6a` to the report.
- Modify `backend/agents/guardrail.py` — add deterministic `review_consistency(snapshot)`.
- Modify `backend/agents/report.py` — render from the snapshot, show provenance.

---

### Task F1: Typed `DesignConfig` snapshot

**Files:**
- Create: `backend/agents/design_config.py`
- Test: `backend/tests/test_design_config.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_design_config.py
from backend.agents.design_config import DesignConfig, build_design_config


def test_build_design_config_from_case_summary():
    s = {"material": "PET", "carton_board_grade": "E-flute",
         "gross_weight_g": 500, "transit_modes": ["truck"], "objective": "reduce_cost"}
    cfg = build_design_config(s, drop_height_m=0.61)
    assert cfg.material_name == "PET"
    assert cfg.board_grade_record == "Corrugated E-flute"   # via flute_resolver
    assert cfg.mass_kg == 0.5                                # single canonical mass
    assert cfg.objective == "reduce_cost"
    assert cfg.drop_height_m == 0.61


def test_design_config_is_frozen():
    cfg = build_design_config({"material": "PET", "gross_weight_g": 100}, drop_height_m=0.3)
    import dataclasses, pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.mass_kg = 9.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_design_config.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the snapshot**

```python
# backend/agents/design_config.py
"""The single canonical design configuration for one case run. Built ONCE in
the orchestrator and threaded to every module so material, flute, mass,
drop-height, transit mode, and objective cannot diverge between modules."""
from __future__ import annotations
from dataclasses import dataclass
from backend.agents.flute_resolver import resolve_flute

@dataclass(frozen=True)
class DesignConfig:
    material_name: str | None
    board_grade_record: str | None
    mass_kg: float
    drop_height_m: float
    transit_modes: tuple[str, ...]
    objective: str | None

def build_design_config(case_summary: dict, *, drop_height_m: float) -> DesignConfig:
    s = case_summary or {}
    gross_g = s.get("gross_weight_g")
    filled = s.get("filled_mass_kg")
    mass_kg = float(filled) if filled else (float(gross_g) / 1000.0 if gross_g else 0.6)
    board = s.get("carton_board_grade")
    return DesignConfig(
        material_name=s.get("material"),
        board_grade_record=resolve_flute(board).record_name if board else None,
        mass_kg=round(mass_kg, 4),
        drop_height_m=float(drop_height_m),
        transit_modes=tuple(s.get("transit_modes") or ()),
        objective=s.get("objective"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_design_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/design_config.py backend/tests/test_design_config.py
git commit -m "feat(consistency): typed canonical DesignConfig snapshot"
```

---

### Task F2: Deterministic `review_consistency` guardrail

**Files:**
- Modify: `backend/agents/guardrail.py` (add `review_consistency`)
- Test: `backend/tests/test_consistency_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_consistency_guard.py
from backend.agents.guardrail import GuardrailAgent


def test_review_consistency_flags_material_divergence():
    g = GuardrailAgent()
    snapshot = {
        "design_config": {"material_name": "PET", "board_grade_record": "Corrugated E-flute",
                          "mass_kg": 0.5, "drop_height_m": 0.61},
        "deterministic": {"material_name": "PET", "mass_kg": 0.5},
        "ista2a": {"material_name": "HDPE", "mass_kg": 0.5, "drop_height_m": 0.61},  # WRONG material
        "report": {"material_name": "PET", "drop_height_m": 0.61},
    }
    report = g.review_consistency(snapshot)
    assert report.ok is False
    assert any("material" in b.lower() for b in report.blocks)


def test_review_consistency_flags_drop_height_divergence():
    g = GuardrailAgent()
    snapshot = {
        "design_config": {"material_name": "PET", "mass_kg": 0.5, "drop_height_m": 0.61},
        "ista2a": {"material_name": "PET", "mass_kg": 0.5, "drop_height_m": 0.46},  # mismatched
    }
    report = g.review_consistency(snapshot)
    assert report.ok is False
    assert any("drop" in b.lower() for b in report.blocks)


def test_review_consistency_passes_when_aligned():
    g = GuardrailAgent()
    base = {"material_name": "PET", "mass_kg": 0.5, "drop_height_m": 0.61}
    snapshot = {"design_config": base, "deterministic": base, "ista2a": base, "report": base}
    assert g.review_consistency(snapshot).ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_consistency_guard.py -v`
Expected: FAIL — `review_consistency` doesn't exist.

- [ ] **Step 3: Implement `review_consistency`**

In `backend/agents/guardrail.py`, add (reusing the existing `GuardrailReport` dataclass at `:11-15`):

```python
# fields that MUST be identical across modules, with absolute tolerance
_CONSISTENCY_FIELDS = {
    "material_name": None,        # exact match
    "board_grade_record": None,   # exact match
    "mass_kg": 1e-3,              # kg tolerance
    "drop_height_m": 1e-3,        # m tolerance
}

def review_consistency(self, snapshot: dict) -> "GuardrailReport":
    """Deterministically assert design params are identical across modules.
    snapshot = {module_name: {field: value, ...}, ..., 'design_config': {...}}"""
    blocks, warnings = [], []
    canonical = snapshot.get("design_config", {})
    for module, payload in snapshot.items():
        if module == "design_config" or not isinstance(payload, dict):
            continue
        for field, tol in _CONSISTENCY_FIELDS.items():
            if field not in payload or field not in canonical:
                continue
            a, b = payload[field], canonical[field]
            if tol is None:
                if a != b:
                    blocks.append(f"{module}.{field}={a!r} != design_config.{field}={b!r}")
            else:
                if a is not None and b is not None and abs(float(a) - float(b)) > tol:
                    blocks.append(f"{module}.{field}={a} != design_config.{field}={b}")
    return GuardrailReport(ok=not blocks, blocks=blocks, warnings=warnings)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_consistency_guard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/guardrail.py backend/tests/test_consistency_guard.py
git commit -m "feat(consistency): deterministic GuardrailAgent.review_consistency"
```

---

### Task F3: Build the snapshot once, gate the case, fix the report inputs

**Files:**
- Modify: `backend/orchestrator/orchestrator.py:836-1196` (`execute_approved_plan`)
- Modify: `backend/agents/report.py:25-35` (`draft` accepts ista6a + provenance)
- Test: `backend/tests/test_consistency_guard.py` (integration-style with a stub case)

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_consistency_guard.py
def test_snapshot_collects_all_modules(monkeypatch):
    """The orchestrator must assemble a consistency snapshot that includes
    design_config + deterministic + ista2a + ista6a + report."""
    from backend.orchestrator.orchestrator import Orchestrator
    keys = Orchestrator._consistency_snapshot_keys()   # class-level contract
    assert {"design_config", "deterministic", "ista2a", "ista6a", "report"} <= set(keys)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_consistency_guard.py -k snapshot -v`
Expected: FAIL — no `_consistency_snapshot_keys`.

- [ ] **Step 3: Build the canonical config once and thread it**

In `execute_approved_plan` (`:850` region), immediately after `s = case.case_summary or {}`:

```python
from backend.agents.design_config import build_design_config
design_cfg = build_design_config(s, drop_height_m=transit_env.drop_height_m)
```

Replace the second independent mass computation (`:1051-1053`) with `mass_kg = design_cfg.mass_kg` so mass is single-sourced. Pass `design_cfg.drop_height_m` as the `user_drop_height_m` to `ista2a.evaluate` (already wired in Task B3) so ISTA and transit agree.

- [ ] **Step 4: Assemble the snapshot and gate before leaving `executing`**

After ISTA-2A, ISTA-6A, and the report draft are produced, build and check:

```python
snapshot = {
    "design_config": design_cfg.__dict__,
    "deterministic": {"material_name": material.name, "mass_kg": mass_kg},
    "ista2a": {"material_name": material.name, "mass_kg": mass_kg,
               "drop_height_m": ista2a_report.get("drop_height_m")},
    "ista6a": {"material_name": material.name, "mass_kg": mass_kg},
    "report": {"material_name": material.name, "drop_height_m": ista2a_report.get("drop_height_m")},
}
consistency = self.guardrail.review_consistency(snapshot)
if not consistency.ok:
    self._emit_status(case_id, "consistency_check",
        f"Cross-module mismatch: {'; '.join(consistency.blocks)}")
    # Persist as an AnalysisResult(method_type="consistency_check") and block sign-off.
```

Add the class contract used by the test:

```python
@staticmethod
def _consistency_snapshot_keys():
    return ("design_config", "deterministic", "ista2a", "ista6a", "report")
```

- [ ] **Step 5: Hand ISTA-6A to the report**

`orchestrator.py:1016-1024` — add `ista6a=snapshot.get("ista6a")` to the `report.draft(...)` call. Update `ReportAgent.draft` (`report.py:25-35`) to accept `ista6a: dict | None = None` and render its verdict + the `board_grade_used` provenance from Part D so the report cannot silently disagree with the persisted ISTA-6A row.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest backend/tests/ -v`
Expected: PASS (all new suites + existing `test_ista_realism.py`).

- [ ] **Step 7: Commit**

```bash
git add backend/orchestrator/orchestrator.py backend/agents/report.py backend/tests/test_consistency_guard.py
git commit -m "feat(consistency): single canonical config, consistency gate, ISTA-6A into report"
```

---

## Self-Review (run after implementation, before sign-off)

**1. Spec coverage**

| Spec requirement | Task(s) |
|---|---|
| Add Pickup Truck, Air Freight, Rail Transport modes | A1, A2, A3 |
| Mode/route-specific transit behavior | A2 (test sequences), B2 (durations), B1 (drop heights) |
| Truck duration 4/8/12 hr; pickup/air/rail user-defined | B2 (defaults + composite), B4 (UI presets + free input) |
| Manual-handling user drop heights 0.5/1/1.5 m | B1 (envelope), B3 (into ISTA), B4 (UI) |
| Optimization aligns with selected objective | C1, C2 |
| Reduce Cost no longer returns higher-cost solutions | C2 |
| Packet optimization no longer uses bottle logic | C3 (server guard), C4 (chart axes) |
| Validate optimizer across bottle/pouch/brush | C2, C3 (per-family) |
| PCR input mapping (E-flute vs B-flute) correct | D1, D2 |
| Input-to-calculation mapping verified across components | D2 (resolver), D3 (provenance), F2/F3 (cross-check) |
| Physics-based stress heatmaps | E1, E2, E3 |
| Cross-module consistency checks | F1, F2, F3 |

**2. Placeholder scan:** Re-read every step; confirm no "TBD/handle edge cases/add validation" without concrete code. The transit `blended_envelope` weighting edits (A1 Step 4, B2 Step 3) must use `.get(key, default)` reads so new keys never KeyError.

**3. Type consistency:** Confirm the variant-dict keys used by `objective_ranking` (`cost_per_unit`, `mass_g`, `min_safety_factor`, `transit_score`, `barrier_score`, `material_score`, `cost_impact_pct`) match the fields each optimizer's `model_dump()` actually emits (bottle `DesignVariant` `optimization.py:88-99`; packet variant `:274-286`; brush variant `:237-249`). Confirm `DesignConfig` field names match the snapshot keys asserted in `review_consistency`. Confirm `stress_field_inputs` orientation keys (`top/bottom/side/corner`) match the `compute_field` scenario→orientation map.

---

## Execution Handoff

Plan complete and saved to `planV1.md` (project root). Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
2. **Inline Execution** — execute tasks in this session with checkpoints. REQUIRED SUB-SKILL: superpowers:executing-plans.

Recommended landing order: **A → B → C → D → E → F** (B depends on A; F depends on D).
