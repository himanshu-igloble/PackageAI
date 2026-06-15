"""Report Agent (5.9).

Builds an audit-friendly markdown report from approved analysis outputs. Pure
templating — no LLM in the critical path.
"""
from __future__ import annotations

from typing import Iterable

from ..schemas import (
    CalculationOutput,
    GeometrySummary,
    MaterialLookupResult,
    ReportDraft,
    SurrogateRiskMap,
    TransitEnvelope,
)


def _fmt(v):
    return "—" if v is None else v


class ReportAgent:
    def draft(
        self,
        *,
        case_summary: dict,
        material: MaterialLookupResult | None,
        geometry: GeometrySummary | None,
        transit: TransitEnvelope | None,
        calcs: Iterable[CalculationOutput] | None,
        risk_map: SurrogateRiskMap | None,
        ista2a: dict | None = None,
    ) -> ReportDraft:
        title = (
            f"Draft Engineering Review — {case_summary.get('packaging_type', 'package').title()} "
            f"({case_summary.get('objective', 'analysis').replace('_', ' ')})"
        )

        assumptions: list[str] = []
        findings: list[str] = []
        risks: list[str] = []
        next_steps: list[str] = []

        # ---- Material section ----
        body = [f"# {title}\n", "## Case Summary\n"]
        for k, v in case_summary.items():
            body.append(f"- **{k}**: {_fmt(v)}")
        body.append("")

        if material:
            body.append("## Material\n")
            body.append(f"- Name: **{material.name}** (grade: {_fmt(material.grade)})")
            body.append(f"- Density: {_fmt(material.density_kg_m3)} kg/m³")
            body.append(f"- Modulus: {_fmt(material.modulus_gpa)} GPa")
            body.append(f"- Yield strength: {_fmt(material.yield_strength_mpa)} MPa")
            body.append(f"- Allowable stress: {_fmt(material.allowable_stress_mpa)} MPa")
            body.append(f"- Source: {material.source}  ·  Confidence: **{material.confidence}**")
            for cav in material.caveats:
                body.append(f"  - ⚠️ {cav}")
            if material.confidence != "verified":
                risks.append(f"Material data is {material.confidence}; verify before any pass/fail decision.")
            body.append("")

        # ---- Geometry section ----
        if geometry:
            body.append("## Geometry\n")
            body.append(f"- File type: {geometry.file_type}")
            body.append(f"- Overall dims (mm): {geometry.overall_dims_mm}")
            body.append(f"- Volume: {_fmt(geometry.volume_mm3)} mm³  ·  Surface area: {_fmt(geometry.surface_area_mm2)} mm²")
            body.append(f"- Critical zones flagged: {', '.join(geometry.critical_zones) or 'none'}")
            body.append(f"- Confidence: **{geometry.confidence}**")
            for n in geometry.notes:
                body.append(f"  - {n}")
            assumptions.append(f"Geometry summary is {geometry.confidence}; bounding box and zones are computed from the uploaded mesh.")
            body.append("")

        # ---- Transit envelope ----
        if transit:
            body.append("## Transit Envelope\n")
            body.append(f"- Mode mix: {transit.mode_mix}")
            body.append(f"- Vibration: **{transit.vibration_g_rms} g_rms**")
            body.append(f"- Drop height: **{transit.drop_height_m} m**")
            body.append(f"- Compression load: **{transit.compression_load_n} N**")
            body.append(f"- Handling fraction: {transit.handling_fraction}")
            body.append(f"- Dominant risks: {', '.join(transit.dominant_risks)}")
            body.append(f"- Suggested test sequence: {' → '.join(transit.suggested_test_sequence)}")
            body.append(f"- Confidence: **{transit.confidence}**")
            assumptions.append("Transit envelope is derived from a coarse, conservative mode-mix mapping; tune per real lane data.")
            body.append("")

        # ---- Calculations ----
        calcs_list = list(calcs or [])
        if calcs_list:
            body.append("## Engineering Calculations\n")
            for c in calcs_list:
                tag = "🔴 RISK" if c.risk_flag else "✅"
                body.append(f"- {tag} **{c.label}** = {c.value} {c.units}  (confidence: {c.confidence})")
                body.append(f"  - Formula: `{c.formula}`")
                body.append(f"  - Inputs: {c.inputs}")
                if c.safety_factor is not None:
                    body.append(f"  - Safety factor: {c.safety_factor}")
                    if c.risk_flag:
                        risks.append(f"{c.label} safety factor {c.safety_factor} below threshold.")
                findings.append(f"{c.label} = {c.value} {c.units}.")
            body.append("")

        # ---- ISTA 2A verdicts (when applicable) ----
        if ista2a:
            body.append("## ISTA 2A — Partial Simulation Verdicts\n")
            body.append(f"- Weight class: **{ista2a.get('weight_class')}**  ·  Drop height: **{ista2a.get('drop_height_m')} m**")
            body.append(f"- **Overall verdict: {ista2a.get('overall_verdict','').upper()}**")
            body.append("")
            body.append("### Drop tests — three orientations\n")
            body.append("| Orientation | Drop height (m) | Energy (J) | Impact pressure (MPa) | Allowable (MPa) | SF | Verdict |")
            body.append("|---|---|---|---|---|---|---|")
            for d in ista2a.get("drops", []):
                v = (d.get("verdict") or "").upper()
                badge = "🟢" if v == "PASS" else ("🔴" if v == "FAIL" else "⚪")
                body.append(
                    f"| {d.get('orientation')} | {d.get('drop_height_m')} | {d.get('drop_energy_j')} | "
                    f"{d.get('impact_pressure_mpa')} | {d.get('allowable_mpa')} | "
                    f"{d.get('safety_factor')} | {badge} **{v}** |"
                )
            for d in ista2a.get("drops", []):
                body.append(f"  - *{d.get('orientation')}*: {d.get('rationale')}")
                if (d.get('verdict') or '').lower() == "fail":
                    risks.append(f"ISTA 2A {d.get('orientation')}-drop FAIL (SF={d.get('safety_factor')}).")
            body.append("")
            body.append("### Transit (stacked) — single orientation\n")
            t = ista2a.get("transit") or {}
            v = (t.get('verdict') or '').upper()
            badge = "🟢" if v == "PASS" else ("🔴" if v == "FAIL" else "⚪")
            body.append(
                f"- Stacking: **{t.get('stacking_orientation')}** (×{t.get('stack_height')})  "
                f"·  Vibration: **{t.get('vibration_g_rms')} g_rms** for {t.get('vibration_duration_min')} min  "
                f"·  Compression: **{t.get('compression_load_n')} N**  ·  SF: **{t.get('compression_safety_factor')}**  "
                f"·  {badge} **{v}**"
            )
            body.append(f"  - Rationale: {t.get('rationale')}")
            for n in ista2a.get("notes", []):
                body.append(f"  - *Note:* {n}")
            body.append("")

        # ---- Surrogate risk map ----
        if risk_map:
            body.append("## Surrogate Risk Map (Approximate)\n")
            body.append(f"> {risk_map.approximation_warning}")
            body.append("")
            body.append("| Zone | Risk score | Rationale |")
            body.append("|------|------------|-----------|")
            for z in risk_map.zones:
                body.append(f"| {z.zone} | {z.risk_score} | {z.rationale} |")
            high = [z for z in risk_map.zones if z.risk_score >= 0.6]
            for z in high:
                risks.append(f"Zone '{z.zone}' shows elevated approximate risk ({z.risk_score}).")
            body.append("")

        # ---- Next steps ----
        next_steps.extend([
            "Have a packaging engineer review every approximate value above.",
            "If proceeding, validate critical risks with a physical ISTA drop/vibration test.",
            "Replace surrogate risk map with verified FEA before any compliance claim.",
        ])

        body.append("## Next Steps\n")
        for s in next_steps:
            body.append(f"- {s}")
        body.append("")

        body.append("## Confidence & Disclaimers\n")
        body.append(
            "- This report is a **draft engineering review**, not a certification or compliance statement.\n"
            "- All values labeled *approximate* or *estimated* must be verified before manufacturing or shipping decisions."
        )

        return ReportDraft(
            title=title,
            case_summary=case_summary,
            assumptions=assumptions,
            findings=findings,
            risks=risks,
            next_steps=next_steps,
            overall_confidence="approximate" if (risk_map or any(c.confidence == "approximate" for c in calcs_list)) else "estimated",
            body_markdown="\n".join(body),
        )
