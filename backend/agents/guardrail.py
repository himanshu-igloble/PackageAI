"""Safety & Guardrail Agent (5.10).

Runs after every agent output. Blocks unsupported claims, missing units,
impossible values, and inconsistent assumptions before they reach the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GuardrailReport:
    ok: bool = True
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_BANNED_PHRASES = (
    "guaranteed",
    "certified",
    "ista compliant",         # cannot claim compliance without an actual referenced test
    "passes ista",
    "fea-validated",
    "fea validated",
)


class GuardrailAgent:
    def review_text(self, text: str) -> GuardrailReport:
        report = GuardrailReport()
        lower = text.lower()
        for phrase in _BANNED_PHRASES:
            if phrase in lower:
                report.ok = False
                report.blocks.append(f"Disallowed claim: '{phrase}'.")
        if "%" in text and "approxim" not in lower and "estimat" not in lower and "verified" not in lower:
            report.warnings.append("Numeric percentages present without confidence labeling.")
        return report

    def review_calculation(self, payload: dict) -> GuardrailReport:
        report = GuardrailReport()
        units = payload.get("units")
        if not units:
            report.ok = False
            report.blocks.append("Calculation missing units field.")
        value = payload.get("value")
        if isinstance(value, (int, float)):
            if value != value:  # NaN
                report.ok = False
                report.blocks.append("Calculation returned NaN.")
            if abs(value) > 1e15:
                report.ok = False
                report.blocks.append("Calculation magnitude is implausibly large.")
        if "formula" not in payload:
            report.ok = False
            report.blocks.append("Calculation missing formula reference.")
        if "inputs" not in payload or not payload.get("inputs"):
            report.ok = False
            report.blocks.append("Calculation missing input trace.")
        return report

    def review_material(self, payload: dict) -> GuardrailReport:
        report = GuardrailReport()
        if payload.get("confidence") == "insufficient_data":
            report.warnings.append("Material was not found in the verified DB; downstream calcs should be blocked.")
        for field_name in ("density_kg_m3", "modulus_gpa", "yield_strength_mpa"):
            v = payload.get(field_name)
            if isinstance(v, (int, float)) and v <= 0:
                report.ok = False
                report.blocks.append(f"Material field '{field_name}' has non-positive value.")
        return report
