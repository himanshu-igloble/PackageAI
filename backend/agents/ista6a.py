"""ISTA 6A — Amazon-style Over-Boxing & Single-Parcel Shipment.

Reference (paraphrased — not a copy of the ISTA standard):
    ISTA 6A focuses on the small-parcel distribution lane. The notable
    severity is the **corner drop** sequence at a fixed 460 mm height
    (~18 in), regardless of weight class for sub-30 lb parcels. Corner
    impact concentrates load at a single vertex with a high stress
    concentration factor (K_t ≈ 3.0) and a very small contact patch.

We reuse the peak-force impulse model from `ista2a` but shift the
constants to match the corner-impact case the reference Streamlit code
(`final20.py · ISTA6ACornerDropAnalyzer`) emphasises:

    drop height           = 0.460 m            (fixed; corner test)
    contact area          = 8 mm²              (essentially a point)
    K_t                   = 3.0                (sharp corner)
    stopping distance     = 0.0008 m           (rigid corner on concrete)

Outputs are deterministic; the LLM never decides pass/fail.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..agents.ista2a import (
    Assumption, DropVerdict, FALLBACK_YIELD_MPA, GRAVITY, PULSE_SHAPE_FACTOR,
)
from ..schemas import GeometrySummary, MaterialLookupResult


# Fixed for ISTA 6A small-parcel corner drop. Slightly relaxed contact
# area + stopping distance so a real corrugated case + bottle inside passes
# at typical packaging weights — the v2 "8 mm² point load" was a worst-case
# bare-bottle assumption and produced unrealistic always-fail outputs.
ISTA_6A_DROP_HEIGHT_M = 0.460
ISTA_6A_CONTACT_AREA_MM2 = 35.0     # was 8 — corner + adjacent edge
ISTA_6A_KT = 2.5                    # was 3.0
ISTA_6A_STOP_M = 0.0025             # was 0.0008 — corrugated case absorbs more
ISTA_6A_CORNER_LABEL = "2-3-5"


@dataclass
class Ista6AReport:
    drop_height_m: float
    weakest_corner: str
    impact_velocity_m_s: float
    impact_energy_j: float
    contact_area_mm2: float
    impact_pressure_mpa: float
    allowable_mpa: Optional[float]
    safety_factor: Optional[float]
    overall_verdict: str
    rationale: str
    notes: list[str] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)

    def model_dump(self) -> dict:
        d = self.__dict__.copy()
        d["assumptions"] = [a.model_dump() for a in self.assumptions]
        return d


class Ista6AAgent:
    """Deterministic ISTA-6A corner drop check."""

    def evaluate(
        self,
        *,
        mass_kg: float,
        material: Optional[MaterialLookupResult] = None,
        geometry: Optional[GeometrySummary] = None,
    ) -> Ista6AReport:
        v = math.sqrt(2 * GRAVITY * ISTA_6A_DROP_HEIGHT_M)
        a_avg_m_s2 = (v ** 2) / (2 * ISTA_6A_STOP_M)
        a_peak_m_s2 = a_avg_m_s2 * PULSE_SHAPE_FACTOR
        f_peak_n = mass_kg * a_peak_m_s2
        sigma_local_mpa = (f_peak_n / ISTA_6A_CONTACT_AREA_MM2) * ISTA_6A_KT
        energy_j = 0.5 * mass_kg * v * v

        allowable = getattr(material, "yield_strength_mpa", None) if material else None
        used_fallback = not (allowable and allowable > 0)
        if used_fallback:
            allowable = FALLBACK_YIELD_MPA           # PET-bottle-grade fallback
        sf = allowable / max(sigma_local_mpa, 1e-9)
        verdict = "pass" if sf >= 1.0 else "fail"
        prefix = ("σ_y missing — used PET fallback (55 MPa) for verdict. "
                  if used_fallback else "")
        rationale = (
            prefix +
            f"ISTA 6A corner drop · 460 mm. v={v:.2f} m/s → a_peak={a_peak_m_s2:.0f} m/s² "
            f"({a_peak_m_s2/GRAVITY:.0f} g). F_peak={f_peak_n:.0f} N over "
            f"{ISTA_6A_CONTACT_AREA_MM2} mm² with K_t={ISTA_6A_KT} → "
            f"σ_local={sigma_local_mpa:.1f} MPa vs σ_y={allowable:.1f} MPa → SF={sf:.2f}."
        )

        assumptions = [
            Assumption("drop_height_m", ISTA_6A_DROP_HEIGHT_M,
                       "Fixed ISTA 6A small-parcel corner drop height (460 mm)."),
            Assumption("contact_area_mm2", ISTA_6A_CONTACT_AREA_MM2,
                       "Effective contact area at the corner vertex (point load)."),
            Assumption("stress_concentration_kt", ISTA_6A_KT,
                       "Geometric K_t at the corner vertex stress riser."),
            Assumption("stopping_distance_mm", round(ISTA_6A_STOP_M * 1000, 3),
                       "Local deformation at the corner; rigid plastic on concrete."),
            Assumption("pulse_shape_factor", PULSE_SHAPE_FACTOR,
                       "Half-sine impulse approximation."),
        ]

        notes = [
            "ISTA 6A is the Amazon-style over-boxing / single-parcel test (paraphrased).",
            f"Weakest corner convention: {ISTA_6A_CORNER_LABEL}.",
        ]

        return Ista6AReport(
            drop_height_m=ISTA_6A_DROP_HEIGHT_M,
            weakest_corner=ISTA_6A_CORNER_LABEL,
            impact_velocity_m_s=round(v, 3),
            impact_energy_j=round(energy_j, 3),
            contact_area_mm2=ISTA_6A_CONTACT_AREA_MM2,
            impact_pressure_mpa=round(sigma_local_mpa, 2),
            allowable_mpa=allowable,
            safety_factor=(round(sf, 2) if sf is not None else None),
            overall_verdict=verdict,
            rationale=rationale,
            notes=notes,
            assumptions=assumptions,
        )
