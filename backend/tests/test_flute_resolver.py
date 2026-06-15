import json
from pathlib import Path

from backend.agents.flute_resolver import canonical_flute_name, resolve_flute

# materials.json lives at the PROJECT ROOT under data/, i.e. parents[2] from
# backend/tests/test_flute_resolver.py (tests -> backend -> project root).
MATERIALS = json.loads((Path(__file__).parents[2] / "data" / "materials.json").read_text())


def _by_name(name):
    return next((m for m in MATERIALS if m["name"] == name), None)


def test_three_flute_grades_exist_with_distinct_properties():
    e, b, c = _by_name("Corrugated E-flute"), _by_name("Corrugated B-flute"), _by_name("Corrugated C-flute")
    assert e and b and c
    # Distinct ECT grade strings and caliper — they must NOT be identical.
    grades = {m["grade"] for m in (e, b, c)}
    calipers = {m["caliper_mm"] for m in (e, b, c)}
    assert len(grades) == 3 and len(calipers) == 3
    assert e["caliper_mm"] < b["caliper_mm"] < c["caliper_mm"]   # E≈1.5 < B≈3 < C≈4 mm
    # The headline numeric field of this task: distinct, physically ordered ECT.
    ect_values = {m["ect_kn_m"] for m in (e, b, c)}
    assert len(ect_values) == 3
    assert e["ect_kn_m"] < b["ect_kn_m"] < c["ect_kn_m"]         # E < B < C stiffness


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


def test_pcr_corrugated_names_are_not_rewritten_to_virgin():
    # PCR/recycled corrugated names must NOT be collapsed to a virgin flute record.
    assert canonical_flute_name("PCR-Corrugated-E") is None
    assert canonical_flute_name("PCR-Corrugated") is None
    # virgin still resolves:
    assert canonical_flute_name("E-flute") == "Corrugated E-flute"
    assert canonical_flute_name("corrugated") == "Corrugated B-flute"


def test_pcr_canonicalise_preserves_pcr_corrugated():
    from backend.agents.pcr import _canonicalise
    assert _canonicalise("PCR-Corrugated-E") == "PCR-Corrugated-E"
    assert _canonicalise("E-flute") == "Corrugated E-flute"
