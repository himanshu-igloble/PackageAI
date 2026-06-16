"""Engineering Calculation Agent (5.6).

Bounded, deterministic. Every output carries inputs, units, formula, and a
labelled confidence. The LLM never does the math.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Optional

from ..schemas import CalculationOutput
from .flute_resolver import resolve_flute


GRAVITY = 9.80665  # m/s^2


def inputs_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class CalculationAgent:
    """Tier-1 deterministic engineering checks (section 15)."""

    def drop_energy(self, *, mass_kg: float, height_m: float) -> CalculationOutput:
        if mass_kg <= 0 or height_m < 0:
            raise ValueError("mass_kg must be > 0, height_m must be >= 0")
        energy = mass_kg * GRAVITY * height_m
        return CalculationOutput(
            label="drop_energy",
            value=round(energy, 4),
            units="J",
            formula="E = m * g * h",
            inputs={"mass_kg": mass_kg, "height_m": height_m, "g_m_s2": GRAVITY},
            confidence="verified",
        )

    def impact_velocity(self, *, height_m: float) -> CalculationOutput:
        if height_m < 0:
            raise ValueError("height_m must be >= 0")
        v = math.sqrt(2 * GRAVITY * height_m)
        return CalculationOutput(
            label="impact_velocity",
            value=round(v, 4),
            units="m/s",
            formula="v = sqrt(2 * g * h)",
            inputs={"height_m": height_m, "g_m_s2": GRAVITY},
            confidence="verified",
        )

    def compression_safety_factor(
        self,
        *,
        applied_load_n: float,
        allowable_stress_mpa: float,
        load_bearing_area_mm2: float,
        threshold: float = 1.5,
        board_grade: Optional[str] = None,
    ) -> CalculationOutput:
        """Compares applied compressive stress against material allowable stress.

        When `board_grade` is supplied (e.g. a carton/secondary-packaging board
        like "E-flute"), the resolved corrugated MaterialRecord name is recorded
        in the inputs trace as `board_grade_used` for provenance (Task D3), so an
        auditor can confirm WHICH flute drove the compression result.
        """
        if applied_load_n <= 0 or allowable_stress_mpa <= 0 or load_bearing_area_mm2 <= 0:
            raise ValueError("All inputs must be > 0")
        applied_mpa = applied_load_n / load_bearing_area_mm2     # N / mm^2 == MPa
        sf = allowable_stress_mpa / applied_mpa
        inputs = {
            "applied_load_n": applied_load_n,
            "allowable_stress_mpa": allowable_stress_mpa,
            "load_bearing_area_mm2": load_bearing_area_mm2,
            "applied_stress_mpa": round(applied_mpa, 4),
            "threshold": threshold,
        }
        if board_grade:
            inputs["board_grade_used"] = resolve_flute(board_grade).record_name
        return CalculationOutput(
            label="compression_safety_factor",
            value=round(sf, 3),
            units="dimensionless",
            formula="SF = allowable_stress / (applied_load / area)",
            inputs=inputs,
            safety_factor=round(sf, 3),
            risk_flag=sf < threshold,
            confidence="estimated",  # depends on area estimation upstream
        )

    def thin_wall_buckling_check(
        self,
        *,
        modulus_gpa: float,
        wall_thickness_mm: float,
        radius_mm: float,
        applied_axial_load_n: float,
        threshold: float = 2.0,
    ) -> CalculationOutput:
        """Approximate critical axial buckling for a thin cylindrical shell.

        Theoretical critical stress: sigma_cr ≈ (E * t) / (R * sqrt(3 * (1 - nu^2)))
        Using nu = 0.4 (typical thermoplastic) and a knockdown of 0.3 (real shells
        buckle far below classical theory). This is an approximation and labeled so.
        """
        if modulus_gpa <= 0 or wall_thickness_mm <= 0 or radius_mm <= 0 or applied_axial_load_n <= 0:
            raise ValueError("All inputs must be > 0")
        E_mpa = modulus_gpa * 1000.0
        nu = 0.4
        knockdown = 0.3
        sigma_cr_mpa = knockdown * (E_mpa * wall_thickness_mm) / (radius_mm * math.sqrt(3 * (1 - nu ** 2)))
        area_mm2 = 2 * math.pi * radius_mm * wall_thickness_mm
        applied_mpa = applied_axial_load_n / area_mm2
        sf = sigma_cr_mpa / applied_mpa
        return CalculationOutput(
            label="thin_wall_buckling_safety_factor",
            value=round(sf, 3),
            units="dimensionless",
            formula="SF = (knockdown * E*t / (R*sqrt(3*(1-nu^2)))) / (P / (2*pi*R*t))",
            inputs={
                "modulus_gpa": modulus_gpa,
                "wall_thickness_mm": wall_thickness_mm,
                "radius_mm": radius_mm,
                "applied_axial_load_n": applied_axial_load_n,
                "nu_assumed": nu,
                "knockdown_assumed": knockdown,
                "sigma_cr_mpa": round(sigma_cr_mpa, 3),
                "applied_stress_mpa": round(applied_mpa, 4),
                "threshold": threshold,
            },
            safety_factor=round(sf, 3),
            risk_flag=sf < threshold,
            confidence="approximate",
        )

    def vibration_acceleration_response(
        self,
        *,
        input_g_rms: float,
        amplification_factor: float = 3.0,
    ) -> CalculationOutput:
        """Conservative response approximation for unknown transmissibility."""
        response = input_g_rms * amplification_factor
        return CalculationOutput(
            label="vibration_response",
            value=round(response, 3),
            units="g_rms",
            formula="g_response = g_input * amplification_factor",
            inputs={"input_g_rms": input_g_rms, "amplification_factor": amplification_factor},
            confidence="approximate",
        )
