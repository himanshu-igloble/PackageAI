"""Deterministic packaging type classifier.

Rule-based classification using geometry signals, file extension, and
user-stated packaging_type. No LLM calls. All decisions are derived from
explicit, reproducible signals.

Priority chain:
  1. Flat dieline file extension (DXF / AI / SVG) → flexible_packaging
  2. 3D geometry proportions (elongation + cross-section) → bottle or flexible
  3. Zone labels in geometry summary → flexible if seam/gusset/fin present
  4. User-stated packaging_type string → bottle or flexible
  5. Fallback: return packaging_type as-is

Routing output:
  'bottle_flow'  — cylindrical / rigid geometry
  'packet_flow'  — flat / flexible / laminate geometry
"""
from __future__ import annotations

from typing import Optional


# Vocabulary sets for packaging_type string normalisation.
_BOTTLE_VOCAB = frozenset({
    "bottle", "bottle_like", "jar", "can", "tube", "vial",
    "carafe", "barrel", "cylinder", "ampoule",
})

_FLEXIBLE_VOCAB = frozenset({
    "pouch", "packet", "sachet", "standup_pouch", "centre_seal_pouch",
    "center_seal_pouch", "flow_wrap", "flow-wrap", "flexible",
    "flexible_packaging", "laminate_pack", "pillow_pouch", "gusset_pouch",
    "quad_seal", "vacuum_pack",
})

# File extensions that unambiguously indicate a flat dieline, not a 3D solid.
_FLAT_DIELINE_EXTS = frozenset({".dxf", ".ai", ".svg"})

# Geometry proportion thresholds.
_ELONGATION_BOTTLE_MIN = 2.0      # max_dim / min_dim for a typical bottle
_CROSS_ASPECT_BOTTLE_MIN = 0.65   # mid_dim / min_dim — near-circular cross-section
_FLAT_THICKNESS_RATIO_MAX = 0.15  # min_dim / max_dim — very thin → flexible pack

# Zone label fragments that imply flexible packaging construction.
_FLEX_ZONE_KEYWORDS = ("seal", "seam", "gusset", "crimp", "fin", "hem")


# ---------------------------------------------------------------------------
# Sub-classifiers
# ---------------------------------------------------------------------------

def _from_file_extension(ext: str) -> Optional[str]:
    """Return 'flexible_packaging' for flat dieline formats; None otherwise."""
    if ext.strip().lower() in _FLAT_DIELINE_EXTS:
        return "flexible_packaging"
    return None


def _from_geometry(dims_mm: dict[str, float],
                   critical_zones: list[str]) -> Optional[str]:
    """Rule-based classification from parsed 3D geometry dimensions.

    Returns 'bottle', 'flexible_packaging', or None when ambiguous.
    """
    l = dims_mm.get("length_mm", 0.0)
    w = dims_mm.get("width_mm", 0.0)
    h = dims_mm.get("height_mm", 0.0)
    if not (l > 0 and w > 0 and h > 0):
        return None

    min_d, mid_d, max_d = sorted([l, w, h])

    # Flat / thin pack — one dimension very small relative to the largest.
    if min_d / max_d < _FLAT_THICKNESS_RATIO_MAX:
        return "flexible_packaging"

    # Cylindrical bottle — elongated body with near-circular cross-section.
    elongation = max_d / max(1.0, min_d)
    cross_aspect = mid_d / max(1.0, min_d)
    if elongation >= _ELONGATION_BOTTLE_MIN and cross_aspect >= _CROSS_ASPECT_BOTTLE_MIN:
        return "bottle"

    # Zone labels that imply flexible construction even without clear flat ratio.
    zone_text = " ".join(critical_zones).lower()
    if any(kw in zone_text for kw in _FLEX_ZONE_KEYWORDS):
        return "flexible_packaging"

    return None


def _from_packaging_type_string(packaging_type: str) -> Optional[str]:
    """Map a user-stated or LLM-classified packaging_type to canonical form."""
    pt = (packaging_type or "").lower().strip().replace("-", "_").replace(" ", "_")
    if any(bt in pt for bt in _BOTTLE_VOCAB):
        return "bottle"
    if any(ft in pt for ft in _FLEXIBLE_VOCAB):
        return "flexible_packaging"
    return None


# ---------------------------------------------------------------------------
# Primary API
# ---------------------------------------------------------------------------

def classify_packaging(
    *,
    packaging_type: Optional[str] = None,
    file_extension: str = "",
    dims_mm: Optional[dict[str, float]] = None,
    critical_zones: Optional[list[str]] = None,
) -> str:
    """Deterministic packaging classifier. Returns one of:
      'bottle', 'flexible_packaging', or the raw packaging_type string.

    Never calls an LLM. All rules are explicit and reproducible.
    """
    result = _from_file_extension(file_extension)
    if result:
        return result

    if dims_mm:
        result = _from_geometry(dims_mm, critical_zones or [])
        if result:
            return result

    if packaging_type:
        result = _from_packaging_type_string(packaging_type)
        if result:
            return result

    return packaging_type or "unknown"


def route_to_agent(
    *,
    packaging_type: Optional[str] = None,
    file_extension: str = "",
    dims_mm: Optional[dict[str, float]] = None,
    critical_zones: Optional[list[str]] = None,
) -> str:
    """Return 'bottle_flow' or 'packet_flow'.

    Defaults to 'packet_flow' for flexible / unknown types.
    """
    kind = classify_packaging(
        packaging_type=packaging_type,
        file_extension=file_extension,
        dims_mm=dims_mm,
        critical_zones=critical_zones,
    )
    return "bottle_flow" if kind == "bottle" else "packet_flow"
