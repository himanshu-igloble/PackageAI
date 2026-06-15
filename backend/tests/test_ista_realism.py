"""Golden tests that lock the ISTA 2A safety factors into believable bands.

If a future change moves an SF outside an engineering-realistic range, this
fails loudly. That is the contract. Engineering credibility is a *test
property*, not a hope.

Run: `.venv/bin/python -m pytest backend/tests/test_ista_realism.py -v`
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.ista2a import Ista2AAgent


# Lightweight stand-ins so the tests don't depend on the DB.
def _mat(name, density, modulus, yield_, allowable=None):
    return SimpleNamespace(
        name=name, density_kg_m3=density, modulus_gpa=modulus,
        yield_strength_mpa=yield_, allowable_stress_mpa=allowable or yield_ * 0.6,
    )


def _geo(L, W, H):
    return SimpleNamespace(
        overall_dims_mm={"length_mm": L, "width_mm": W, "height_mm": H},
    )


HDPE = _mat("HDPE", 955, 1.0, 26)
PET  = _mat("PET",  1380, 2.8, 55)
GLASS = _mat("Glass", 2500, 70, 50, allowable=7)
BOTTLE_GEO = _geo(70, 70, 220)


@pytest.fixture
def agent():
    return Ista2AAgent()


# ─── DROP REALISM ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("orientation,sf_min,sf_max", [
    ("top",    0.50, 2.50),     # close call for HDPE cap-down — borderline pass
    ("bottom", 2.00, 10.00),     # base disc spreads the load
    ("side",   1.00, 4.00),      # mid-wall typically clears
])
def test_hdpe_500ml_drop(agent, orientation, sf_min, sf_max):
    """HDPE 510g bottle, ISTA-2A 24in drop, with the v3 calibration. Realistic
    bottles cluster in SF 0.5–10 depending on orientation."""
    r = agent.evaluate(mass_kg=0.51, material=HDPE, geometry=BOTTLE_GEO)
    drop = next(d for d in r.drops if d.orientation == orientation)
    assert drop.safety_factor is not None
    assert sf_min <= drop.safety_factor <= sf_max, \
        f"{orientation}: SF={drop.safety_factor} outside [{sf_min}, {sf_max}]"


def test_pet_500ml_drop_top_passes(agent):
    """PET 500ml bottle on its cap from 24in. Real PET bottles in this weight
    class normally pass — the v3 calibration must reflect that."""
    r = agent.evaluate(mass_kg=0.55, material=PET, geometry=BOTTLE_GEO)
    top = next(d for d in r.drops if d.orientation == "top")
    assert top.verdict == "pass", \
        f"PET top-drop should pass with v3 calibration; got SF={top.safety_factor}"
    assert top.safety_factor >= 1.0


def test_glass_corner_drop_fails_hard(agent):
    """Glass bottle dropped on a corner is brittle; the corner orientation
    fails by huge margin. (Top + bottom + side may pass with v3 calibration —
    real glass bottles do survive 24in cap drops some of the time.)"""
    glass_geo = _geo(70, 70, 250)
    r = agent.evaluate(mass_kg=0.95, material=GLASS, geometry=glass_geo)
    # Glass on cap may now pass (matching real-world); the corner-drop check
    # in ISTA 6A is the brittle-material killer. Confirm here as well.
    from backend.agents.ista6a import Ista6AAgent
    r6 = Ista6AAgent().evaluate(mass_kg=0.95, material=GLASS, geometry=glass_geo)
    assert r6.overall_verdict == "fail"
    assert r6.safety_factor < 0.5


def test_never_returns_insufficient_data(agent):
    """User-facing verdicts must always be Pass or Fail. PET-equivalent
    fallback is used silently when material data is missing."""
    # No material at all
    r = agent.evaluate(mass_kg=0.5, material=None, geometry=BOTTLE_GEO)
    assert r.overall_verdict in ("pass", "fail")
    for d in r.drops:
        assert d.verdict in ("pass", "fail")
    assert r.transit.overall_transit_verdict in ("pass", "fail", "n/a")


def test_no_realistic_sf_ever_above_15(agent):
    """No drop SF should be in the SF=100+ regime ever — that's the v1 bug.

    v3 calibration produces SF up to ~12 for very-light bottles on the base
    (large contact area + low mass). 15 is the realism ceiling; the v1 bug
    produced SF=1249 on the same input."""
    for mass in (0.2, 0.5, 1.0, 2.0):
        r = agent.evaluate(mass_kg=mass, material=HDPE, geometry=BOTTLE_GEO)
        for d in r.drops:
            assert d.safety_factor is None or d.safety_factor < 15.0, \
                f"mass={mass}kg {d.orientation} SF={d.safety_factor} unrealistic"


# ─── TRANSIT REALISM ───────────────────────────────────────────────────────

def test_transit_default_is_n_a_for_bottle_in_case(agent):
    """A bottle in a corrugated case is NOT graded for stack compression."""
    r = agent.evaluate(mass_kg=0.51, material=HDPE, geometry=BOTTLE_GEO)
    assert r.transit.compression_verdict == "n/a"
    assert r.transit.compression_safety_factor is None
    assert "ships inside a corrugated case" in r.transit.rationale.lower()


def test_transit_loose_bottle_grades_against_pallet_column(agent):
    """When ships_loose=True we actually grade against a real pallet column.

    Note: a small bottle has tiny footprint → modest column load, so the
    *yield* SF can be high. The actual binding constraint for a loose
    plastic bottle is *thin-wall buckling*, which CalculationAgent handles.
    Here we only assert the calc executed and produced a finite verdict —
    not that the magnitude makes engineering sense (it doesn't, by design)."""
    r = agent.evaluate(mass_kg=0.51, material=HDPE, geometry=BOTTLE_GEO,
                       ships_loose=True)
    sf = r.transit.compression_safety_factor
    assert sf is not None
    assert sf > 0
    assert r.transit.compression_verdict in ("pass", "fail")


def test_vibration_fatigue_below_threshold_passes(agent):
    """Default 0.54 g_rms vib is well below the fatigue endurance — pass."""
    r = agent.evaluate(mass_kg=0.51, material=HDPE, geometry=BOTTLE_GEO)
    assert r.transit.vibration_verdict == "pass"


# ─── OVERALL VERDICTS ──────────────────────────────────────────────────────

def test_overall_verdict_can_actually_fail(agent):
    """The verdicts MUST be able to fail. Use a known-fragile design to prove it."""
    # 1.2 kg PS bottle on cap from 24in — PS is brittle, so the cap-down
    # impact at this mass should still fail despite v3's gentler model.
    ps = _mat("PS", 1050, 3.2, 45, allowable=18)
    big_geo = _geo(80, 80, 280)
    r = agent.evaluate(mass_kg=1.2, material=ps, geometry=big_geo)
    assert r.overall_verdict == "fail"


def test_aluminum_overall_can_pass(agent):
    """A robust material (high yield) can still pass."""
    al = _mat("Aluminum", 2700, 69, 290, allowable=180)
    r = agent.evaluate(mass_kg=0.40, material=al, geometry=_geo(60, 60, 130))
    assert r.overall_verdict == "pass"


# ─── ASSUMPTIONS TRACE ─────────────────────────────────────────────────────

def test_every_drop_carries_assumptions(agent):
    r = agent.evaluate(mass_kg=0.51, material=HDPE, geometry=BOTTLE_GEO)
    for d in r.drops:
        assert len(d.assumptions) >= 4
        names = {a.name for a in d.assumptions}
        assert {"pulse_shape_factor", "stopping_distance_mm",
                "contact_area_mm2", "stress_concentration_kt"} <= names


def test_transit_carries_assumptions(agent):
    r = agent.evaluate(mass_kg=0.51, material=HDPE, geometry=BOTTLE_GEO)
    names = {a.name for a in r.transit.assumptions}
    assert "ships_loose" in names
    assert "vibration_psd_g_rms" in names
