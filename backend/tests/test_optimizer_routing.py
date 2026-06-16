import pytest
from backend.routes.extras import (
    _resolve_family,
    _assert_family,
    _assert_intent,
    _ALLOWED_INTENTS,
    FamilyMismatch,
)


def test_resolve_family_from_case_summary():
    assert _resolve_family({"packaging_family": "packet"}) == "packet"
    assert _resolve_family({"packaging_type": "pouch"}) == "packet"
    assert _resolve_family({"packaging_type": "bottle"}) == "bottle"
    assert _resolve_family({"packaging_type": "toothbrush"}) == "brush"


def test_family_guard_rejects_wrong_endpoint():
    with pytest.raises(FamilyMismatch):
        _assert_family({"packaging_type": "pouch"}, expected="bottle")


def test_resolve_family_defaults_to_bottle_on_unknown():
    assert _resolve_family({}) == "bottle"
    assert _resolve_family({"packaging_type": "widget"}) == "bottle"


def test_assert_intent_rejects_cross_family_intent():
    # improve_shelf_life is packet-only; invalid for bottle
    with pytest.raises(Exception):
        _assert_intent("improve_shelf_life", family="bottle")
    _assert_intent("reduce_cost", family="bottle")   # valid, no raise


def test_route_allowed_intents_match_agent_constants():
    from backend.agents.optimization import ALLOWED_INTENTS as B
    assert _ALLOWED_INTENTS["bottle"] == B


# --- C4: family-native comparison dashboard (no bottle/ISTA leakage) --------
#
# build_charts() is a FastAPI route (case_id, db) that builds family-native
# `designs` dicts and renders them via charts_svc.comparison_dashboard(...).
# The bottle/ISTA vocabulary leakage lives in that renderer's CSV header /
# axis titles, so we assert against comparison_dashboard's real return shape:
#   {"png_b64": str, "csv": str}  where the CSV first row is the header.
from backend.services.charts import comparison_dashboard


def _csv_header(out: dict) -> set[str]:
    csv_text = out["csv"]
    first_line = csv_text.splitlines()[0]
    return {c.strip() for c in first_line.split(",")}


def test_packet_charts_use_packet_axes_not_bottle():
    rows = [{"name": "v1", "cost_impact_pct": -5, "seal_score": 0.8,
             "transit_score": 0.7, "barrier_score": 0.9, "puncture_score": 0.6}]
    out = comparison_dashboard(rows, family="packet")
    keys = _csv_header(out)
    # bottle / ISTA terms must be gone
    assert "min_safety_factor" not in keys
    assert "passes_ista" not in keys
    assert "mass_g" not in keys
    assert "cost_per_unit" not in keys
    # packet-native score columns present
    assert {"seal_score", "transit_score", "barrier_score"} <= keys


def test_brush_charts_use_brush_axes_not_bottle():
    rows = [{"name": "v1", "cost_impact_pct": -3, "blister_score": 0.8,
             "transit_score": 0.7, "material_score": 0.9, "compression_score": 0.6}]
    out = comparison_dashboard(rows, family="brush")
    keys = _csv_header(out)
    assert "min_safety_factor" not in keys
    assert "passes_ista" not in keys
    assert {"blister_score", "transit_score", "material_score"} <= keys


def test_bottle_charts_unchanged():
    rows = [{"name": "v1", "cost_per_unit": 0.42, "min_safety_factor": 1.8,
             "mass_g": 23.0, "roi_pct": 12.0, "passes_ista": True}]
    out = comparison_dashboard(rows)  # default family="bottle"
    keys = _csv_header(out)
    assert {"cost_per_unit", "min_safety_factor", "mass_g",
            "roi_pct", "passes_ista"} <= keys
