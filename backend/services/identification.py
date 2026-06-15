"""Deterministic geometry-based packaging routing.

Classifies uploaded geometry into one of three routing targets using only
rule-based signals — no LLM, no probabilistic model.

Signals (priority order):
  1. Flatness — primary signal; very flat geometry cannot be a bottle
  2. Circularity — secondary; bottles have circular/oval cross-sections
  3. Neck / shoulder zones — tertiary; only trusted when circularity is high
  4. Elongation — supporting signal for both bottle and flexible
  5. User packaging hint — applied only when confidence is low (< 0.70)

Routing targets:
  bottle_flow   — bottle-like rigid packaging (cylindrical, neck, shoulder)
  packet_flow   — flexible / flat packaging (planar, non-cylindrical)
  intake        — unknown (insufficient geometry signal)

Design bias:
  False flexible → bottle is worse than false bottle → flexible.
  The classifier biases toward flexible_packaging when geometry is
  sufficiently flat or non-cylindrical, and only asserts bottle_like when
  circularity is clearly high.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..schemas import GeometrySummary


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class PackagingIdentification:
    routing_target: str   # "bottle_flow" | "packet_flow" | "intake"
    packaging_class: str  # "bottle_like" | "flexible_packaging" | "unknown"
    confidence: float     # 0.0 .. 1.0
    reason: str
    signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "routing_target": self.routing_target,
            "packaging_class": self.packaging_class,
            "confidence": self.confidence,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Routing map
# ---------------------------------------------------------------------------

_ROUTING: dict[str, str] = {
    "bottle_like":        "bottle_flow",
    "flexible_packaging": "packet_flow",
    "unknown":            "intake",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sorted_dims(dims: dict) -> tuple[float, float, float]:
    """Return (smallest, middle, largest) of the three bounding-box extents."""
    vals = sorted([
        float(dims.get("length_mm") or 0),
        float(dims.get("width_mm") or 0),
        float(dims.get("height_mm") or 0),
    ])
    return vals[0], vals[1], vals[2]


# ---------------------------------------------------------------------------
# Core identification
# ---------------------------------------------------------------------------

def identify_packaging(
    geometry: GeometrySummary,
    *,
    user_packaging_hint: Optional[str] = None,
) -> PackagingIdentification:
    """Classify packaging as bottle_like, flexible_packaging, or unknown.

    Decision order:
      1. Hard overrides — deterministic, high-confidence, returned immediately
      2. Scoring — for geometry that doesn't hit a hard override
      3. User hint — applied only when scored confidence < 0.70

    Never raises — returns routing_target 'intake' when signals are insufficient.
    """
    dims = geometry.overall_dims_mm or {}
    zones: set[str] = set(geometry.critical_zones or [])

    d_min, d_mid, d_max = _sorted_dims(dims)

    if d_max <= 0 or d_mid <= 0 or d_min <= 0:
        return PackagingIdentification(
            routing_target="intake",
            packaging_class="unknown",
            confidence=0.0,
            reason="Degenerate bounding box; cannot classify.",
        )

    # --- Derived proportions ---
    elongation  = round(d_max / d_mid, 3)   # > 1 → elongated
    circularity = round(d_min / d_mid, 3)   # ≈ 1.0 → circular cross-section
    flatness    = round(d_min / d_max, 3)   # < 0.25 → flat pack

    has_neck     = "neck" in zones
    has_shoulder = "shoulder" in zones

    signals = {
        "has_neck": has_neck,
        "has_shoulder": has_shoulder,
        "elongation": elongation,
        "circularity": circularity,
        "flatness": flatness,
        "d_min_mm": round(d_min, 1),
        "d_mid_mm": round(d_mid, 1),
        "d_max_mm": round(d_max, 1),
        "critical_zones": sorted(zones),
    }

    # =========================================================================
    # HARD OVERRIDES — deterministic, no scoring required
    # =========================================================================

    # Override 1: extreme flatness → always flexible.
    # No real bottle has flatness < 0.12; this is paper-thin geometry.
    # Catches flat sachets, dieline geometries, very thin flexible packs.
    if flatness < 0.12:
        signals.update(bottle_score=0, flexible_score=10)
        return PackagingIdentification(
            routing_target="packet_flow",
            packaging_class="flexible_packaging",
            confidence=0.90,
            reason=(
                f"Extreme flatness (flatness={flatness}): geometry is too thin "
                f"to be a bottle. Routing to flexible packaging."
            ),
            signals=signals,
        )

    # Override 2: flat + non-cylindrical → flexible.
    # Real bottles are either circular (circularity ≈ 1.0) or only mildly oval
    # (circularity ≥ 0.55). Geometry that is simultaneously flat (< 0.25) and
    # non-cylindrical (circularity < 0.55) can only be a flat flexible pack —
    # snack bags, standup pouches, side-gusset pouches, flow-wraps, sachets.
    # Note: neck/shoulder signals are IGNORED here because geometry_service
    # marks the top 25 % of any mesh as a potential neck zone, so sealed fins
    # and crimped tops on flexible packs frequently trigger false positives.
    if flatness < 0.25 and circularity < 0.55:
        signals.update(bottle_score=0, flexible_score=8)
        return PackagingIdentification(
            routing_target="packet_flow",
            packaging_class="flexible_packaging",
            confidence=0.85,
            reason=(
                f"Flat, non-cylindrical geometry (flatness={flatness}, "
                f"circularity={circularity}): characteristic of flexible "
                f"packaging; neck/shoulder zones ignored."
            ),
            signals=signals,
        )

    # =========================================================================
    # SCORING — for geometry that doesn't trigger a hard override
    # After the overrides above, any geometry reaching here satisfies:
    #   flatness ≥ 0.12   AND   (flatness ≥ 0.25 OR circularity ≥ 0.55)
    # =========================================================================

    bottle_score   = 0
    flexible_score = 0

    # --- Bottle signals ---

    # Circular cross-section is the most reliable bottle indicator.
    if circularity >= 0.65:
        bottle_score += 2
    elif circularity >= 0.55:
        bottle_score += 1

    # Neck/shoulder contribute only when cross-section is clearly not
    # rectangular (circularity ≥ 0.60). Below that threshold the zones are
    # treated as mesh artefacts (sealed fins, crimped ends, fold lines).
    if has_neck and circularity >= 0.60:
        bottle_score += 3
    if has_shoulder and circularity >= 0.60:
        bottle_score += 2

    # Elongation reward — only meaningful when cross-section is also circular.
    if elongation > 1.5 and circularity >= 0.60:
        bottle_score += 2
    if elongation > 2.0:
        bottle_score += 1

    # Non-flat geometry is mildly supportive of bottle classification.
    if flatness >= 0.35:
        bottle_score += 1

    # --- Flexible signals ---

    # Non-circular cross-section is a strong indicator of non-bottle geometry.
    if circularity < 0.55:
        flexible_score += 2
    elif circularity < 0.65:
        flexible_score += 1

    # Moderate flatness (0.12–0.30) suggests a flat-ish, non-bottle form.
    if flatness < 0.30:
        flexible_score += 1

    # Absence of neck/shoulder is weak positive evidence for flexible packs.
    if not has_neck and not has_shoulder:
        flexible_score += 2
    elif not has_neck or not has_shoulder:
        flexible_score += 1

    signals["bottle_score"]   = bottle_score
    signals["flexible_score"] = flexible_score

    # =========================================================================
    # CLASSIFICATION
    # Bottle requires a higher score threshold than flexible (design bias).
    # =========================================================================

    packaging_class: str
    confidence: float
    reason: str

    if bottle_score >= 5 and bottle_score > flexible_score + 1:
        packaging_class = "bottle_like"
        confidence = min(0.92, 0.65 + 0.05 * bottle_score)
        reason = (
            f"Bottle-like geometry: neck={has_neck}, shoulder={has_shoulder}, "
            f"elongation={elongation}, circularity={circularity}."
        )

    elif flexible_score >= 4 and flexible_score >= bottle_score:
        packaging_class = "flexible_packaging"
        confidence = min(0.88, 0.65 + 0.05 * flexible_score)
        reason = (
            f"Flexible geometry: flatness={flatness}, circularity={circularity}, "
            f"no dominant bottle signals."
        )

    elif flexible_score >= 2 and flexible_score > bottle_score:
        packaging_class = "flexible_packaging"
        confidence = 0.60
        reason = (
            f"Moderate flexible signals (flexible_score={flexible_score}, "
            f"flatness={flatness})."
        )

    elif bottle_score >= 4 and bottle_score > flexible_score:
        packaging_class = "bottle_like"
        confidence = 0.60
        reason = f"Moderate bottle-like signals (bottle_score={bottle_score})."

    else:
        packaging_class = "unknown"
        confidence = 0.30
        reason = (
            f"Insufficient geometry signals "
            f"(bottle_score={bottle_score}, flexible_score={flexible_score})."
        )

    # --- User hint: applied only when geometry signals are ambiguous ---
    if user_packaging_hint and confidence < 0.70:
        hint = user_packaging_hint.lower()
        is_bottle_hint = any(t in hint for t in ("bottle", "jar", "can", "tube"))
        is_flexible_hint = any(
            t in hint for t in ("pouch", "sachet", "packet", "flexible", "flow_wrap", "flow wrap")
        )
        if is_bottle_hint and packaging_class != "bottle_like":
            packaging_class = "bottle_like"
            confidence = max(confidence, 0.60)
            reason += f" User hint '{user_packaging_hint}' applied."
        elif is_flexible_hint and packaging_class != "flexible_packaging":
            packaging_class = "flexible_packaging"
            confidence = max(confidence, 0.60)
            reason += f" User hint '{user_packaging_hint}' applied."

    routing_target = _ROUTING.get(packaging_class, "intake")

    return PackagingIdentification(
        routing_target=routing_target,
        packaging_class=packaging_class,
        confidence=round(confidence, 3),
        reason=reason,
        signals=signals,
    )
