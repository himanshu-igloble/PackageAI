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


# fields that MUST be identical across modules, with absolute tolerance (None = exact match)
_CONSISTENCY_FIELDS = {
    "material_name": None,        # exact match
    "board_grade_record": None,   # exact match
    "mass_kg": 1e-3,             # kg tolerance
    "drop_height_m": 1e-3,       # m tolerance
}


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

    def review_consistency(self, snapshot: dict) -> GuardrailReport:
        """Deterministically assert design params are identical across modules.

        snapshot = {module_name: {field: value, ...}, ..., 'design_config': {...}}
        The 'design_config' entry is the canonical reference; every other module's
        overlapping fields must match it within tolerance.
        """
        report = GuardrailReport()
        canonical = snapshot.get("design_config", {})
        for module, payload in snapshot.items():
            if module == "design_config" or not isinstance(payload, dict):
                continue
            for field_name, tol in _CONSISTENCY_FIELDS.items():
                if field_name not in payload or field_name not in canonical:
                    continue
                a, b = payload[field_name], canonical[field_name]
                if tol is None:
                    if a != b:
                        report.ok = False
                        report.blocks.append(
                            f"{module}.{field_name}={a!r} != design_config.{field_name}={b!r}"
                        )
                else:
                    if a is not None and b is not None and abs(float(a) - float(b)) > tol:
                        report.ok = False
                        report.blocks.append(
                            f"{module}.{field_name}={a} != design_config.{field_name}={b}"
                        )
        return report
