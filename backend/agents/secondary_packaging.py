"""Secondary Packaging Agent — lightweight deterministic summary builder.

Reads secondary carton fields from a case_summary dict (works for both
Bottle Flow and Packet Flow field naming conventions) and returns a
normalised secondary_packaging dict.

No LLM, no FEA. All outputs are deterministic text derived from the
collected field values. Real carton compression simulation (BCT/ECT) is
out of scope for this phase — the visualisations are placeholder images.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Deterministic risk fragments — keyed by normalised field value
# ---------------------------------------------------------------------------

_CARTON_RISK: dict[str, str] = {
    "corrugated_carton":    "bottom corner compression and top-panel bow",
    "corrugated_shipper":   "bottom corner compression and lateral panel buckle",
    "master_case":          "base compression and panel deflection under pallet load",
    "mono_carton":          "top-panel deflection and edge crush at score lines",
    "display_carton":       "perforation zone weakness and panel flutter",
    "tray":                 "rim crush and stacking instability without a lid",
    "shrink_bundle":        "point-load pressure from adjacent packs; no edge protection",
    "rigid_box":            "corner crush and lid-hinge fatigue under repeated handling",
    "duplex":               "corner crush and moisture ingress at cut edges",
}

_STACKING_RISK: dict[str, str] = {
    "single_pallet_stack":  "pallet-edge stress concentration at the bottom layer",
    "double_stacked":       "top-panel deflection and intermediate-layer crush",
    "warehouse_stacking":   "cumulative compression under prolonged static load",
    "container_stacking":   "corner crush under high axial load in a shipping container",
    "palletized":           "pallet-edge stress and bottom-layer compression",
    "loose_loaded":         "racking stress and lateral impact shock",
    "mixed_load":           "point-load pressure from adjacent non-uniform packages",
    "floor_stack":          "base compression under full stack weight without a pallet",
}

_TRANSIT_RISK: dict[str, str] = {
    "ship":             "high-humidity and salt-air board strength degradation",
    "rail":             "rail vibration-induced racking and shear stress",
    "truck":            "road-vibration shock at chassis resonance frequency",
    "air":              "pressure-differential cycling and rapid temperature change",
    "manual_handling":  "impact shock at drop heights of 0.3–0.9 m during transfers",
}


class SecondaryPackagingAgent:
    """Reads carton fields from case_summary and returns a normalised dict.

    Supports both Bottle Flow naming (carton_pack_count, carton_stacking_config)
    and Packet Flow naming (packets_per_carton, stacking_method / carton_stack_height).
    """

    @staticmethod
    def build_summary(case_summary: dict[str, Any]) -> dict[str, Any]:
        """Extract and normalise secondary packaging data.

        Returns {"enabled": False} when the user opted out or the field was
        never collected, so callers can check enabled without KeyError guards.
        """
        has = str(case_summary.get("has_secondary_carton") or "").strip().lower()
        if has not in ("yes", "true", "1"):
            return {"enabled": False}

        # pack count — support both flow namings
        pack_count = (
            case_summary.get("carton_pack_count")       # bottle naming
            or case_summary.get("packets_per_carton")   # packet naming
        )

        # stacking config — support both namings; carton_stack_height is a numeric fallback
        stacking = (
            case_summary.get("carton_stacking_config")  # bottle naming
            or case_summary.get("stacking_method")      # packet field
        )
        if not stacking and case_summary.get("carton_stack_height"):
            stacking = f"{case_summary['carton_stack_height']} cartons high"

        transit = case_summary.get("transit_modes") or []
        if isinstance(transit, str):
            transit = [t.strip() for t in transit.split(",") if t.strip()]

        return {
            "enabled":        True,
            "carton_type":    case_summary.get("carton_type"),
            "board_type":     case_summary.get("carton_board_grade"),
            "pack_count":     pack_count,
            "stacking_config": stacking,
            "transit_mode":   list(transit),
            "goal":           case_summary.get("objective"),
        }

    @staticmethod
    def get_recommendation(secondary: dict[str, Any]) -> str:
        """Return a deterministic one-paragraph engineering recommendation.

        Combines carton-type risk, stacking risk, and transit risk into a
        concise action item. No hallucinated numbers — every sentence is
        derived from the lookup tables above.
        """
        if not secondary.get("enabled"):
            return ""

        risks: list[str] = []

        ct = (secondary.get("carton_type") or "").lower().replace(" ", "_").replace("-", "_")
        if ct in _CARTON_RISK:
            risks.append(_CARTON_RISK[ct])

        stk = (secondary.get("stacking_config") or "").lower().replace(" ", "_").replace("-", "_")
        for key, text in _STACKING_RISK.items():
            if key in stk:
                risks.append(text)
                break

        modes = secondary.get("transit_mode") or []
        for mode in modes:
            mk = str(mode).strip().lower()
            if mk in _TRANSIT_RISK and _TRANSIT_RISK[mk] not in risks:
                risks.append(_TRANSIT_RISK[mk])

        risk_text = "; ".join(risks[:3]) if risks else "general compression and handling loads"

        pack_count  = secondary.get("pack_count")
        board       = secondary.get("board_type") or "current board grade"
        carton_name = (secondary.get("carton_type") or "carton").replace("_", " ")
        stacking_label = (secondary.get("stacking_config") or "standard stacking").replace("_", " ")

        return (
            f"Observed risk areas: {risk_text}. "
            f"With {pack_count or 'multiple'} primary packs per {carton_name} and "
            f"a {stacking_label} configuration, the {board} construction should be "
            "validated for edge crush resistance (ECT) and box compression test (BCT) "
            "before finalising transit packaging. "
            "Consider reducing pallet stack height or upgrading board grade for export shipments."
        )
