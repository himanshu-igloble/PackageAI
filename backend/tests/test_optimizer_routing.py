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
