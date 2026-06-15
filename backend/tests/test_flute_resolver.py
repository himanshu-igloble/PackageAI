import json
from pathlib import Path

# materials.json lives at the PROJECT ROOT under data/, i.e. parents[2] from
# backend/tests/test_flute_resolver.py (tests -> backend -> project root).
MATERIALS = json.loads((Path(__file__).parents[2] / "data" / "materials.json").read_text())


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
