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


def canonical_flute_name(name: str | None) -> str | None:
    """Return the canonical virgin corrugated record name for a flute/corrugated
    board grade, or None if `name` is not a (virgin) flute grade.

    Excludes recycled/PCR names (e.g. 'PCR-Corrugated-E') so they are NOT
    rewritten to a virgin record — callers should leave those to their own
    alias handling / passthrough.
    """
    if not name:
        return None
    low = name.strip().lower()
    if "pcr" in low or "recycl" in low:
        return None
    if "flute" in low or "corrugat" in low:
        return resolve_flute(name).record_name
    return None
