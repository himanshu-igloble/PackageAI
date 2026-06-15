"""Visualization service: builds the 3D scene payload the Three.js viewer
consumes, including high-resolution stress heatmaps.

For ISTA 2A workflows we emit FOUR scenes:
    • drop_top      ─ 3D heatmap of a top-first drop
    • drop_bottom   ─ 3D heatmap of a bottom-first drop
    • drop_side     ─ 3D heatmap of a side-first drop
    • transit       ─ 3D heatmap for the user-chosen stacking orientation

For non-ISTA workflows we still emit a transit scene plus optional drops if
the geometry / mass are available.

Each scene carries:
    glb_url, scenario, label, summary,
    n_cells, per_vertex_color (uint8 RGB triples),
    scale {min, max, units, colormap, stops}
"""
from __future__ import annotations

from typing import Optional

import trimesh

from ..schemas import GeometrySummary, MaterialLookupResult, SurrogateRiskMap, TransitEnvelope
from . import heatmap as hm


def _color_for_risk(score: float) -> str:
    if score < 0.33:
        return "#2ecc71"
    if score < 0.66:
        return "#f1c40f"
    return "#e74c3c"


def build_scene_basic(
    *,
    case_id: str,
    glb_url: Optional[str],
    geometry: GeometrySummary | None,
    risk_map: SurrogateRiskMap | None,
) -> dict:
    """Legacy zone-overlay scene (used when full heatmaps aren't computed)."""
    scene: dict = {
        "case_id": case_id,
        "glb_url": glb_url,
        "bbox": geometry.bbox_mm if geometry else None,
        "overall_dims_mm": geometry.overall_dims_mm if geometry else None,
        "is_proxy": (geometry is not None and "proxy" in geometry.file_type),
        "annotations": [],
        "zone_overlays": [],
    }
    if risk_map:
        for z in risk_map.zones:
            scene["zone_overlays"].append({
                "zone": z.zone,
                "risk_score": z.risk_score,
                "color": _color_for_risk(z.risk_score),
                "rationale": z.rationale,
            })
        scene["legend"] = {
            "low":  {"color": _color_for_risk(0.1), "label": "low risk (< 0.33)"},
            "med":  {"color": _color_for_risk(0.5), "label": "moderate risk (0.33–0.66)"},
            "high": {"color": _color_for_risk(0.8), "label": "high risk (> 0.66)"},
        }
        scene["approximation_warning"] = risk_map.approximation_warning
    return scene


def build_heatmap_scenes(
    *,
    case_id: str,
    mesh_path: str,
    geometry: GeometrySummary | None,
    transit_env: TransitEnvelope | None,
    material: MaterialLookupResult | None,
    stacking_orientation: str = "upright",
    glb_url: Optional[str] = None,
    scenarios: tuple[str, ...] = ("drop_top", "drop_bottom", "drop_side", "transit"),
    case_summary: Optional[dict] = None,
) -> dict:
    """Compute high-resolution viridis stress fields for each scenario over
    the uploaded (or proxy) mesh. Returns a payload the UI can swap between
    via its scene tabs.

    When case_summary declares has_secondary_carton == "yes", carton heatmap
    scenes (3 scenarios) are generated procedurally and embedded as
    carton_scenes + carton_glb_b64 (base64 GLB) — no extra API route needed.

    NOTE: the viewer keeps the same GLB url for all scenes — it just swaps
    the vertex-color buffer client-side. This keeps the network payload light.
    """
    out: dict = {
        "case_id": case_id,
        "glb_url": glb_url,
        "bbox": geometry.bbox_mm if geometry else None,
        "overall_dims_mm": geometry.overall_dims_mm if geometry else None,
        "is_proxy": (geometry is not None and "proxy" in geometry.file_type),
        "colormap": {"name": hm.colormap_name(), "stops": hm.LUT_SIZE, "lut": hm.colormap_lut()},
        "scenes": [],
        "active_scene": scenarios[0] if scenarios else None,
        "stacking_orientation": stacking_orientation,
    }

    if not mesh_path:
        return out
    try:
        loaded = trimesh.load(mesh_path, force="mesh")
        mesh = (trimesh.util.concatenate(tuple(loaded.geometry.values()))
                if isinstance(loaded, trimesh.Scene) else loaded)
        if mesh is None or len(mesh.faces) == 0:
            return out
    except Exception as exc:
        out["mesh_error"] = repr(exc)
        return out

    # ── Physics-grounded scale inputs ───────────────────────────────────────
    # Derive per-orientation ISTA-2A yield utilization ONCE so every drop scene
    # is coloured against the same fixed yield-referenced scale (comparable).
    stress_inputs = None
    try:
        from ..agents.ista2a import Ista2AAgent

        # Drop height: prefer the configured transit envelope; else ISTA-2A
        # default 24-in / 0.61 m.
        drop_h = float(getattr(transit_env, "drop_height_m", None) or 0.61)

        # Mass: derive from geometry volume × material density when available,
        # otherwise fall back to a sensible 0.5 kg consumer-bottle default.
        # (Exact mass can be refined later — this wires the physics pathway.)
        mass_kg = 0.5
        try:
            vol_mm3 = getattr(geometry, "volume_mm3", None) if geometry else None
            dens = getattr(material, "density_kg_m3", None) if material else None
            if vol_mm3 and dens:
                mass_kg = max((vol_mm3 / 1.0e9) * float(dens), 1e-3)
        except Exception:
            mass_kg = 0.5

        stress_inputs = Ista2AAgent().stress_field_inputs(
            mass_kg=mass_kg, drop_height_m=drop_h, material=material,
        )
    except Exception as exc:
        out["stress_inputs_error"] = repr(exc)

    for sc in scenarios:
        try:
            field = hm.compute_field(
                mesh,
                sc,
                transit_env=transit_env,
                material=material,
                stacking_orientation=stacking_orientation,
                stress_inputs=stress_inputs,
            )
            out["scenes"].append({
                "scenario": field.scenario,
                "label": field.label,
                "summary": field.summary,
                "n_cells": field.n_cells,
                "per_vertex_color": field.per_vertex_color,
                "scale": field.scale,
            })
        except Exception as exc:
            out["scenes"].append({
                "scenario": sc, "error": repr(exc),
            })

    # Carton heatmap extension — only when secondary carton is declared.
    # Generates a procedural box mesh + 3 heuristic stress scenes and embeds
    # the GLB as base64 so no additional route is required.
    if case_summary and str(case_summary.get("has_secondary_carton") or "").lower() == "yes":
        try:
            import base64 as _b64
            carton_scenes, glb_bytes = hm.build_carton_scenes(case_summary)
            out["carton_scenes"] = carton_scenes
            out["carton_glb_b64"] = _b64.b64encode(glb_bytes).decode("ascii")
        except Exception as exc:
            out["carton_error"] = repr(exc)

    return out


# Back-compat for existing routes/cases.py that imports `build_scene`.
build_scene = build_scene_basic
