"""FEA / Surrogate Analysis Agent (5.7).

Heuristic risk zoning for bottle-class geometry. Output is explicitly labeled
'approximate' per the architecture's non-negotiable constraints.
"""
from __future__ import annotations

from ..schemas import GeometrySummary, SurrogateRiskMap, TransitEnvelope, ZoneRisk, MaterialLookupResult


# Base zone susceptibilities (relative). Tuned from a generic bottle archetype.
_ZONE_BASELINE = {
    "base":      {"drop": 0.85, "compression": 0.65, "vibration": 0.20, "handling": 0.30},
    "shoulder":  {"drop": 0.55, "compression": 0.45, "vibration": 0.40, "handling": 0.50},
    "neck":      {"drop": 0.40, "compression": 0.20, "vibration": 0.60, "handling": 0.65},
    "side_wall": {"drop": 0.45, "compression": 0.85, "vibration": 0.55, "handling": 0.40},
    "closure":   {"drop": 0.70, "compression": 0.30, "vibration": 0.30, "handling": 0.80},
    "corner":    {"drop": 0.95, "compression": 0.55, "vibration": 0.35, "handling": 0.55},
}


def _safe(x: float) -> float:
    return max(0.0, min(1.0, x))


class SurrogateAgent:
    def zone_risk_map(
        self,
        *,
        geometry: GeometrySummary | None,
        transit: TransitEnvelope,
        material: MaterialLookupResult | None,
    ) -> SurrogateRiskMap:
        # Mechanism weights normalized from the transit envelope
        # (handling and drop are coupled — a corner drop after rough handling)
        weights = {
            "drop":        _safe(transit.drop_height_m / 1.2),
            "compression": _safe(transit.compression_load_n / 4500.0),
            "vibration":   _safe(transit.vibration_g_rms / 2.0),
            "handling":    _safe(transit.handling_fraction),
        }
        # Material modifier: brittle materials shift drop/handling risk up
        material_mod = 1.0
        notes = []
        if material and material.name and material.name.lower() in {"glass", "ps", "polystyrene"}:
            material_mod = 1.25
            notes.append(f"Material '{material.name}' is brittle; drop/handling risk increased by 25%.")
        if material and material.modulus_gpa and material.modulus_gpa < 1.5:
            material_mod *= 0.9  # very flexible plastics often shed drop energy
            notes.append("Low modulus material: drop response slightly reduced.")

        zones: list[ZoneRisk] = []
        for zone_name, susc in _ZONE_BASELINE.items():
            score = sum(susc[m] * weights[m] for m in weights) / sum(susc.values())
            score *= material_mod
            score = round(_safe(score), 3)
            top_mech = max(weights, key=lambda m: susc[m] * weights[m])
            rationale = (
                f"Dominant mechanism for this zone is {top_mech.replace('_', ' ')} "
                f"(susceptibility {susc[top_mech]:.2f}, transit weight {weights[top_mech]:.2f})."
            )
            zones.append(ZoneRisk(zone=zone_name, risk_score=score, rationale=rationale))

        # Sort highest-risk first for UI convenience
        zones.sort(key=lambda z: z.risk_score, reverse=True)

        warning = (
            "Heuristic surrogate analysis. Treat values as risk indicators only — "
            "they are not validated FEA. Final pass/fail must come from a verified solver "
            "or a physical ISTA test."
        )
        if notes:
            warning += " " + " ".join(notes)

        return SurrogateRiskMap(zones=zones, approximation_warning=warning, confidence="approximate")
