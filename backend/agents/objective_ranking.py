"""Objective-aware ranking shared by bottle/packet/brush optimizers.

Selection MUST be driven by the user's objective. Each optimizer scores its
variants on its own metrics; this module turns the chosen objective into a
(key, direction) and sorts. Direction: "min" => ascending, "max" => descending.
"""
from __future__ import annotations
from typing import Any

# intent -> (metric_key, direction). Keys must exist on the variant dicts the
# optimizers already produce.
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
