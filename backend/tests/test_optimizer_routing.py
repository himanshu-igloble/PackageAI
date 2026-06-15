import pytest
from backend.routes.extras import _resolve_family, _assert_family, FamilyMismatch


def test_resolve_family_from_case_summary():
    assert _resolve_family({"packaging_family": "packet"}) == "packet"
    assert _resolve_family({"packaging_type": "pouch"}) == "packet"
    assert _resolve_family({"packaging_type": "bottle"}) == "bottle"
    assert _resolve_family({"packaging_type": "toothbrush"}) == "brush"


def test_family_guard_rejects_wrong_endpoint():
    with pytest.raises(FamilyMismatch):
        _assert_family({"packaging_type": "pouch"}, expected="bottle")
