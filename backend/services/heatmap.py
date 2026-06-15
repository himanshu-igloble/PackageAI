"""High-resolution stress heatmap service.

For each scenario the user wants visualised, we compute a normalised stress
field over every face of the mesh and map it to the **FEA-standard jet
spectrum** (blue → cyan → green → yellow → red). For a bottle proxy this
produces ~4000 coloured faces (comfortably beyond the user's "500 boxes"
target); for a real STL it scales with the uploaded geometry.

Why FEA jet (and not viridis): commercial FEA tools (ANSYS, Abaqus, NX)
display von-Mises and principal-stress fields with a rainbow scheme so
engineers can read low → high at a glance. We match that convention.

Scenarios produced:
    transit       — vibration + stacking compression on a fixed orientation
    drop_top      — drop with neck/closure hitting first
    drop_bottom   — drop with base hitting first
    drop_side     — drop with side wall hitting first
    drop_corner   — corner-first drop (optional, used in ISTA 2A edge variant)

Each scenario returns a payload the frontend can ship straight to Three.js
as vertex colors plus a colorbar definition for the legend on the left.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import trimesh


# Legacy 250-stop viridis (kept for parity / power users); the production
# colormap is now FEA_JET (built programmatically below).
_VIRIDIS_LEGACY = np.array([
    [68, 1, 84],   [68, 2, 86],   [69, 4, 87],   [69, 5, 89],   [70, 7, 90],
    [70, 8, 92],   [70, 10, 93],  [70, 11, 94],  [71, 13, 96],  [71, 14, 97],
    [71, 16, 99],  [71, 17, 100], [71, 19, 101], [72, 20, 103], [72, 22, 104],
    [72, 23, 105], [72, 24, 106], [72, 26, 108], [72, 27, 109], [72, 28, 110],
    [72, 29, 111], [72, 31, 112], [72, 32, 113], [72, 33, 115], [72, 35, 116],
    [72, 36, 117], [72, 37, 118], [72, 38, 119], [72, 40, 120], [72, 41, 121],
    [71, 42, 122], [71, 44, 122], [71, 45, 123], [71, 46, 124], [71, 47, 125],
    [70, 48, 126], [70, 50, 126], [70, 51, 127], [70, 52, 128], [69, 53, 129],
    [69, 55, 129], [69, 56, 130], [68, 57, 131], [68, 58, 131], [68, 59, 132],
    [67, 61, 132], [67, 62, 133], [66, 63, 133], [66, 64, 134], [66, 65, 134],
    [65, 66, 135], [65, 68, 135], [64, 69, 136], [64, 70, 136], [63, 71, 136],
    [63, 72, 137], [62, 73, 137], [62, 74, 137], [62, 76, 138], [61, 77, 138],
    [61, 78, 138], [60, 79, 138], [60, 80, 139], [59, 81, 139], [59, 82, 139],
    [58, 83, 139], [58, 84, 140], [57, 85, 140], [57, 86, 140], [56, 88, 140],
    [56, 89, 140], [55, 90, 140], [55, 91, 141], [54, 92, 141], [54, 93, 141],
    [53, 94, 141], [53, 95, 141], [53, 96, 141], [52, 97, 141], [52, 98, 141],
    [51, 99, 141], [51, 100, 142], [51, 101, 142], [50, 102, 142], [50, 103, 142],
    [49, 104, 142], [49, 105, 142], [49, 106, 142], [48, 107, 142], [48, 108, 142],
    [47, 109, 142], [47, 110, 142], [47, 111, 142], [46, 112, 142], [46, 113, 142],
    [46, 114, 142], [45, 115, 142], [45, 116, 142], [44, 117, 142], [44, 118, 142],
    [44, 119, 142], [43, 120, 142], [43, 121, 142], [43, 122, 142], [42, 123, 142],
    [42, 124, 142], [42, 125, 142], [41, 126, 142], [41, 127, 142], [41, 128, 142],
    [40, 129, 142], [40, 130, 142], [40, 131, 142], [39, 132, 142], [39, 133, 142],
    [39, 134, 142], [38, 135, 142], [38, 136, 142], [38, 137, 141], [37, 138, 141],
    [37, 139, 141], [37, 140, 141], [36, 141, 141], [36, 142, 140], [36, 143, 140],
    [35, 144, 140], [35, 145, 140], [35, 146, 139], [35, 147, 139], [34, 148, 139],
    [34, 149, 138], [34, 150, 138], [34, 151, 138], [33, 152, 137], [33, 153, 137],
    [33, 154, 137], [33, 155, 136], [33, 156, 136], [33, 157, 135], [33, 158, 135],
    [33, 159, 134], [33, 160, 134], [33, 161, 133], [33, 162, 133], [33, 163, 132],
    [34, 164, 131], [34, 165, 131], [34, 166, 130], [35, 167, 130], [35, 168, 129],
    [36, 169, 128], [36, 170, 127], [37, 171, 127], [38, 172, 126], [39, 173, 125],
    [40, 174, 124], [41, 175, 123], [42, 176, 122], [43, 177, 122], [44, 178, 121],
    [46, 179, 120], [47, 180, 119], [49, 181, 118], [50, 182, 117], [52, 183, 115],
    [54, 184, 114], [56, 185, 113], [57, 186, 112], [59, 187, 111], [61, 187, 109],
    [63, 188, 108], [65, 189, 107], [68, 190, 105], [70, 191, 104], [72, 192, 102],
    [74, 193, 101], [77, 194, 99],  [79, 194, 97],  [82, 195, 96],  [84, 196, 94],
    [87, 197, 92],  [89, 198, 91],  [92, 198, 89],  [94, 199, 87],  [97, 200, 85],
    [100, 201, 83], [103, 201, 81], [106, 202, 79], [108, 203, 77], [111, 204, 75],
    [114, 204, 73], [117, 205, 71], [120, 206, 69], [123, 206, 67], [126, 207, 64],
    [129, 207, 62], [132, 208, 60], [135, 209, 57], [138, 209, 55], [142, 210, 52],
    [145, 210, 50], [148, 211, 47], [151, 211, 44], [155, 212, 41], [158, 212, 38],
    [161, 212, 36], [165, 213, 33], [168, 213, 30], [171, 213, 27], [175, 214, 24],
    [178, 214, 21], [181, 214, 18], [185, 215, 15], [188, 215, 13], [191, 215, 12],
    [195, 215, 11], [198, 216, 11], [201, 216, 11], [205, 216, 13], [208, 216, 15],
    [211, 217, 18], [214, 217, 21], [218, 217, 25], [221, 217, 30], [224, 218, 34],
    [227, 218, 39], [230, 218, 44], [233, 219, 49], [236, 219, 53], [240, 219, 58],
    [243, 220, 64], [246, 220, 69], [249, 221, 74], [251, 222, 80], [253, 224, 85],
    [253, 226, 90], [253, 228, 95], [253, 230, 101], [253, 232, 106], [253, 234, 112],
    [253, 236, 117], [253, 238, 122], [253, 240, 128], [253, 242, 133], [253, 244, 139],
    [253, 245, 144], [253, 247, 150], [253, 249, 156], [253, 250, 161], [253, 252, 167],
    [253, 253, 172], [253, 254, 178], [254, 254, 184], [254, 255, 190], [253, 255, 198],
], dtype=np.uint8)


# --- FEA jet (rainbow) — the standard ANSYS/Abaqus stress colour scheme.
# Built programmatically from RGB anchor stops + linear interpolation, so we
# can tune resolution without hand-typing the LUT.
def _build_jet_lut(n: int = 256) -> np.ndarray:
    """Classical jet colormap with 9 anchor points: deep-blue → blue → cyan →
    green → yellow → orange → red → dark-red."""
    stops = np.array([
        [0.000,   0,   0, 143],     # deep blue
        [0.125,   0,   0, 255],     # blue
        [0.250,   0, 127, 255],     # azure
        [0.375,   0, 255, 255],     # cyan
        [0.500, 127, 255, 127],     # green-mint
        [0.625, 255, 255,   0],     # yellow
        [0.750, 255, 127,   0],     # orange
        [0.875, 255,   0,   0],     # red
        [1.000, 143,   0,   0],     # dark-red
    ])
    t = np.linspace(0.0, 1.0, n)
    r = np.interp(t, stops[:, 0], stops[:, 1])
    g = np.interp(t, stops[:, 0], stops[:, 2])
    b = np.interp(t, stops[:, 0], stops[:, 3])
    return np.stack([r, g, b], axis=1).astype(np.uint8)


FEA_JET = _build_jet_lut(256)
# Keep the public symbol stable for callers; FEA_JET is now the production LUT.
VIRIDIS = FEA_JET           # noqa: F841 (back-compat alias)
LUT_SIZE = FEA_JET.shape[0]


def colormap_lut() -> list[list[int]]:
    """Return the active colormap as a JS-shipable list of [r,g,b] in 0..255."""
    return FEA_JET.tolist()


def colormap_name() -> str:
    return "fea-jet"


def _stress_to_color(stress: np.ndarray) -> np.ndarray:
    """Map normalized stress [0,1] → RGB via FEA jet."""
    s = np.clip(stress, 0.0, 1.0)
    idx = np.clip((s * (LUT_SIZE - 1)).astype(np.int32), 0, LUT_SIZE - 1)
    return FEA_JET[idx]    # N x 3 uint8


def _normalise(x: np.ndarray) -> np.ndarray:
    lo, hi = float(np.min(x)), float(np.max(x))
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)


@dataclass
class StressField:
    scenario: str
    label: str
    n_cells: int
    per_face_stress: list[float]       # 0..1 normalised
    per_face_color: list[list[int]]    # N × [r, g, b]
    per_vertex_color: list[list[int]]  # M × [r, g, b]   (for Three.js convenience)
    scale: dict                         # {min, max, units, colormap}
    summary: str


def _per_vertex_from_faces(mesh: trimesh.Trimesh, face_colors: np.ndarray) -> np.ndarray:
    """Average face colors to vertices for smooth vertex-coloured rendering."""
    n_v = len(mesh.vertices)
    acc = np.zeros((n_v, 3), dtype=np.float64)
    count = np.zeros(n_v, dtype=np.int32)
    for fi, tri in enumerate(mesh.faces):
        for vi in tri:
            acc[vi] += face_colors[fi]
            count[vi] += 1
    count = np.maximum(count, 1).reshape(-1, 1)
    return (acc / count).astype(np.uint8)


def compute_field(
    mesh: trimesh.Trimesh,
    scenario: str,
    *,
    transit_env=None,
    material=None,
    stacking_orientation: str = "upright",
    impact_velocity_m_s: Optional[float] = None,
) -> StressField:
    """Compute a per-face stress field for the given scenario.

    All scenarios share the same logical pipeline:
    1. compute a positional stress factor (function of face center position)
    2. modulate by a material stress concentration factor
    3. (transit) overlay a stacking/compression term
    4. normalise to [0, 1]; map via viridis
    """
    centers = mesh.triangles_center                       # N x 3
    bounds = mesh.bounds
    lo, hi = bounds[0], bounds[1]
    extents = hi - lo
    tall_axis = int(np.argmax(extents))
    radial_axes = [a for a in (0, 1, 2) if a != tall_axis]

    # Normalised height along tall axis (0 = base, 1 = top)
    t = (centers[:, tall_axis] - lo[tall_axis]) / max(extents[tall_axis], 1e-9)
    # Radial coord (distance from central axis)
    r_xy = np.sqrt(
        (centers[:, radial_axes[0]] - 0.5 * (lo[radial_axes[0]] + hi[radial_axes[0]])) ** 2 +
        (centers[:, radial_axes[1]] - 0.5 * (lo[radial_axes[1]] + hi[radial_axes[1]])) ** 2,
    )
    r_norm = r_xy / max(np.max(r_xy), 1e-9)
    # x-component for side-impact orientation (we treat +x as impact face)
    x = centers[:, radial_axes[0]]
    x_norm = (x - x.min()) / max(x.max() - x.min(), 1e-9)   # 0 = far, 1 = near impact

    # Material modifier: brittle materials amplify stress concentrations.
    brittle_amp = 1.0
    if material:
        try:
            mat_name = (material.name or "").lower()
            if mat_name in ("glass", "ps", "polystyrene"):
                brittle_amp = 1.25
            if material.modulus_gpa and material.modulus_gpa > 50:  # rigid metal/glass
                brittle_amp *= 1.1
        except Exception:
            pass

    if scenario == "drop_bottom":
        # Bottom-first drop: max stress at base, concentrated at corners (high r_norm, low t).
        base = (1.0 - t) ** 1.4
        corner_boost = 0.6 * (1.0 - t) * r_norm
        stress = base + corner_boost
        label = "Bottom-first drop"
        summary = "Stress concentrates at the base, especially at the base corners."
    elif scenario == "drop_top":
        # Top-first drop: max stress at neck/closure, with shoulder participation.
        top_band = np.where(t > 0.75, t, 0.0)
        stress = (t ** 1.4) + 0.4 * top_band * r_norm
        label = "Top-first drop"
        summary = "Stress concentrates at the neck/closure region; shoulder participates."
    elif scenario == "drop_side":
        # Side-first drop: high stress band on the side that hits, peaking mid-height.
        impact_band = x_norm                                       # 0..1
        mid_emphasis = 1.0 - np.abs(t - 0.5) * 1.5                 # peak mid-height
        mid_emphasis = np.clip(mid_emphasis, 0.0, 1.0)
        stress = 0.7 * impact_band + 0.3 * mid_emphasis * impact_band
        label = "Side-first drop"
        summary = "Stress concentrates on the impact-side wall at mid-height."
    elif scenario == "drop_corner":
        # Corner-first: high stress at base+side intersection on impact side.
        impact_band = x_norm
        stress = (1.0 - t) ** 1.2 * impact_band + 0.3 * (1.0 - t) * r_norm
        label = "Corner-first drop"
        summary = "Stress concentrates at the impact-side bottom corner."
    elif scenario == "transit":
        # Transit (vibration + stacking compression) for the fixed stacking orientation:
        #   upright   → compression along tall axis, side-wall mid-height vibration peak
        #   on_side   → compression across radial; base no longer load-bearing
        #   inverted  → compression along tall axis but neck/cap loaded
        vib_g = float(getattr(transit_env, "vibration_g_rms", 0.5))
        comp_n = float(getattr(transit_env, "compression_load_n", 1500.0))
        if stacking_orientation == "on_side":
            # Compression across radial; side wall (impact direction) carries load
            radial_load = 0.4 + 0.5 * np.abs(np.cos(np.arctan2(centers[:, radial_axes[1]],
                                                                 centers[:, radial_axes[0]])))
            vib = 0.35 * (1.0 - np.abs(t - 0.5))
            stress = (comp_n / 4000.0) * radial_load + vib
        elif stacking_orientation == "inverted":
            # Cap loaded; neck region stressed
            cap_loaded = np.where(t > 0.8, t, 0.0)
            vib = 0.3 * (1.0 - np.abs(t - 0.5))
            stress = (comp_n / 4000.0) * cap_loaded + vib + 0.2 * (vib_g / 1.0)
        else:
            # default upright
            base_load = (1.0 - t) * (comp_n / 4000.0)
            side_vib  = 0.45 * (1.0 - np.abs(t - 0.5)) * (vib_g / 1.0)
            stress = base_load + side_vib + 0.15 * r_norm * (1.0 - t)
        label = f"Transit ({stacking_orientation})"
        summary = (
            f"Vibration {vib_g:.2f} g_rms + stacking compression {comp_n:.0f} N "
            f"with stacking orientation '{stacking_orientation}'."
        )
    else:
        stress = np.full(len(centers), 0.5)
        label = scenario
        summary = "Unknown scenario; flat mid-range field returned."

    stress = stress * brittle_amp
    stress_norm = _normalise(stress)

    face_colors = _stress_to_color(stress_norm)
    vertex_colors = _per_vertex_from_faces(mesh, face_colors)

    return StressField(
        scenario=scenario,
        label=label,
        n_cells=int(len(centers)),
        per_face_stress=[round(float(x), 4) for x in stress_norm],
        per_face_color=face_colors.tolist(),
        per_vertex_color=vertex_colors.tolist(),
        scale={
            "min": 0.0,
            "max": 1.0,
            "units": "normalised stress (0=low, 1=peak)",
            "colormap": "viridis",
            "stops": 250,
        },
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Carton (secondary packaging) heatmap — same philosophy as product heatmaps
# ---------------------------------------------------------------------------

def _compute_carton_field(mesh: trimesh.Trimesh, scenario: str) -> StressField:
    """Heuristic stress field for a rectangular carton box.

    Uses the same pipeline as compute_field() — positional face-center math,
    FEA-jet colourmap, per-vertex averaging — adapted for box geometry and
    three carton-specific failure modes.
    """
    centers = mesh.triangles_center
    bounds = mesh.bounds
    lo, hi = bounds[0], bounds[1]
    extents = hi - lo

    # Height axis: Z is the tall axis for an upright carton created by
    # trimesh.creation.box(). 0 = base, 1 = top.
    h_norm = (centers[:, 2] - lo[2]) / max(extents[2], 1e-9)

    # Corner proximity in the XY plane: how close each face center is to a
    # vertical edge column (0 = panel centre, ~1 = corner column).
    cx = 0.5 * (lo[0] + hi[0])
    cy = 0.5 * (lo[1] + hi[1])
    dx = np.abs(centers[:, 0] - cx) / max(extents[0] * 0.5, 1e-9)
    dy = np.abs(centers[:, 1] - cy) / max(extents[1] * 0.5, 1e-9)
    corner_prox = np.clip(np.sqrt((dx ** 2 + dy ** 2) / 2.0), 0.0, 1.0)

    if scenario == "carton_top_load":
        # Pallet stacking: base corners carry the compression column load
        # (McKee BCT formula — load path runs through vertical column fibres).
        # Stress decays going up (load transfers through corrugated columns)
        # and inward toward panel centres (ECT column logic).
        stress = (1.0 - h_norm) ** 1.5 + 0.5 * (1.0 - h_norm) * corner_prox
        label = "Top Load Compression"
        summary = (
            "Base corner columns carry peak compression under pallet stacking. "
            "Stress decays toward panel centres and diminishes up the carton height."
        )
    elif scenario == "carton_corner_crush":
        # Handling/corner impacts: vertical edge columns stressed at mid-height
        # (the column buckles at its weakest point, not at the ends where it
        # is constrained by the top and bottom flaps).
        stress = corner_prox ** 1.2 * np.sin(np.pi * np.clip(h_norm, 0.0, 1.0))
        label = "Corner Crush"
        summary = (
            "Vertical corner columns stressed at mid-height under handling impacts. "
            "Peak stress at mid-column; large face panels remain relatively low."
        )
    elif scenario == "carton_side_wall":
        # Lateral transit squeeze: large face panels deflect inward in a
        # single half-wave (classic panel buckling mode — Euler column analogy
        # applied to thin-walled flat panel).
        panel_centre = 1.0 - corner_prox      # 1 = face centre, 0 = corner
        stress = panel_centre * np.sin(np.pi * np.clip(h_norm, 0.0, 1.0))
        label = "Side Wall Compression"
        summary = (
            "Panel centre deflects most under lateral transit compression. "
            "Stress peaks at mid-height face centre; corner columns remain stiffer."
        )
    else:
        stress = np.full(len(centers), 0.5)
        label = scenario.replace("_", " ").title()
        summary = "Carton stress field."

    stress_norm = _normalise(stress)
    face_colors = _stress_to_color(stress_norm)
    vertex_colors = _per_vertex_from_faces(mesh, face_colors)

    return StressField(
        scenario=scenario,
        label=label,
        n_cells=int(len(centers)),
        per_face_stress=[round(float(x), 4) for x in stress_norm],
        per_face_color=face_colors.tolist(),
        per_vertex_color=vertex_colors.tolist(),
        scale={
            "min": 0.0,
            "max": 1.0,
            "units": "normalised stress (0=low, 1=peak)",
            "colormap": "fea-jet",
            "stops": LUT_SIZE,
        },
        summary=summary,
    )


def build_carton_scenes(case_summary: dict) -> tuple[list[dict], bytes]:
    """Generate 3 engineering-style carton heatmap scenes.

    Creates a parametric box mesh proportional to the declared carton
    (or a 400 × 300 × 250 mm proxy), subdivides it 3× for smooth gradient
    rendering, runs 3 heuristic stress scenarios, and returns
    (scenes_list, glb_bytes).

    The scenes list follows the same dict schema as product scenes, so the
    existing _populateMiniStrip() / makeMiniViewer() pipeline on the frontend
    can render them without modification.
    """
    # Derive carton dimensions from case_summary fields.
    dims = case_summary.get("carton_dimensions_mm")
    if isinstance(dims, dict):
        L = float(dims.get("length") or 400)
        W = float(dims.get("width") or 300)
        H = float(dims.get("height") or dims.get("gusset") or 250)
    else:
        # Proxy dimensions for a common retail corrugated shipper (mm).
        L, W, H = 400.0, 300.0, 250.0

    # Build and subdivide — 3 subdivisions gives 768 faces / ~386 vertices,
    # enough for smooth heatmap gradients across all face panels.
    mesh = trimesh.creation.box(extents=[L, W, H])
    for _ in range(3):
        mesh = mesh.subdivide()

    scenes: list[dict] = []
    for sc in ("carton_top_load", "carton_corner_crush", "carton_side_wall"):
        field = _compute_carton_field(mesh, sc)
        scenes.append({
            "scenario": field.scenario,
            "label": field.label,
            "summary": field.summary,
            "n_cells": field.n_cells,
            "per_vertex_color": field.per_vertex_color,
            "scale": field.scale,
        })

    # Export the same mesh as GLB bytes; the frontend creates a blob URL.
    glb_bytes: bytes = mesh.export(file_type="glb")
    return scenes, glb_bytes
