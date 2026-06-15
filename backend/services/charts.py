"""Server-side chart generation (matplotlib Agg, base64 PNG).

Charts produced for the tabbed report:
    vibration_psd         — PSD by transport mode (truck/rail/ship/air)
    density_compare       — material density bar chart
    zone_risk_bar         — surrogate zone risk scores
    drop_verdict_bar      — ISTA-2A per-orientation safety factors
    comparison_dashboard  — original vs 3 alternatives (cost / SF / mass)

Every function returns {"png_b64": str, "csv": str} so the UI can render
inline and the user can download underlying data.
"""
from __future__ import annotations

import base64
import csv
import io
from typing import Iterable, Optional

# Force a headless backend before importing pyplot.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# Dark-mode-friendly palette to match the DesignEdge UI
plt.rcParams.update({
    "figure.facecolor":  "#161a22",
    "axes.facecolor":    "#1c212c",
    "axes.edgecolor":    "#232a36",
    "axes.labelcolor":   "#e6e8ee",
    "xtick.color":       "#8a93a6",
    "ytick.color":       "#8a93a6",
    "text.color":        "#e6e8ee",
    "axes.titlecolor":   "#e6e8ee",
    "axes.grid":         True,
    "grid.color":        "#232a36",
    "grid.linestyle":    "--",
    "grid.alpha":        0.6,
    "axes.titlesize":    11,
    "axes.labelsize":    9,
    "xtick.labelsize":   8,
    "ytick.labelsize":   8,
    "legend.fontsize":   8,
    "font.size":         9,
})

ACCENT = "#4f9dff"
ACCENT_2 = "#2ecc71"
WARN = "#f1c40f"
BAD = "#e74c3c"


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _csv(headers: list[str], rows: list[list]) -> str:
    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return sio.getvalue()


# ---------------------------------------------------------------- vibration

# Approximate g²/Hz PSD signatures by mode. These are conservative coarse
# envelopes for visualisation only — never used in calcs.
_VIB_PROFILES = {
    "truck":           [(2, 0.04), (10, 0.012), (50, 0.005), (100, 0.0015), (200, 0.0005)],
    "rail":            [(2, 0.06), (10, 0.020), (50, 0.008), (100, 0.0025), (200, 0.0008)],
    "ship":            [(2, 0.02), (10, 0.006), (50, 0.002), (100, 0.0008), (200, 0.0003)],
    "air":             [(2, 0.03), (10, 0.010), (50, 0.004), (100, 0.0012), (200, 0.0004)],
    "manual_handling": [(2, 0.015), (10, 0.005), (50, 0.0015), (100, 0.0005), (200, 0.0002)],
}


def vibration_psd(modes: Iterable[str]) -> dict:
    fig, ax = plt.subplots(figsize=(6, 3.2))
    rows = []
    for m in modes:
        prof = _VIB_PROFILES.get(m)
        if not prof:
            continue
        f = np.array([p[0] for p in prof])
        psd = np.array([p[1] for p in prof])
        ax.loglog(f, psd, marker="o", label=m, linewidth=1.5)
        for fi, pi in prof:
            rows.append([m, fi, pi])
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("PSD (g²/Hz)")
    ax.set_title("Random vibration PSD by transport mode")
    ax.legend(loc="upper right")
    return {"png_b64": _fig_to_b64(fig), "csv": _csv(["mode", "freq_hz", "psd_g2_hz"], rows)}


# ---------------------------------------------------------------- density

def density_compare(materials: list[dict]) -> dict:
    """materials: [{"name": ..., "density_kg_m3": ...}, ...]"""
    materials = [m for m in materials if m.get("density_kg_m3")]
    if not materials:
        return {"png_b64": "", "csv": ""}
    fig, ax = plt.subplots(figsize=(6, 3.2))
    names = [m["name"] for m in materials]
    dens = [m["density_kg_m3"] for m in materials]
    ax.bar(names, dens, color=ACCENT, edgecolor="#232a36")
    ax.set_ylabel("Density (kg/m³)")
    ax.set_title("Material density comparison")
    for i, v in enumerate(dens):
        ax.text(i, v, f"{v:.0f}", ha="center", va="bottom", fontsize=8)
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    rows = list(zip(names, dens))
    return {"png_b64": _fig_to_b64(fig), "csv": _csv(["material", "density_kg_m3"], rows)}


# ---------------------------------------------------------------- zone risk

def zone_risk_bar(zones: list[dict]) -> dict:
    """zones: [{"zone": str, "risk_score": 0..1}, ...]"""
    if not zones:
        return {"png_b64": "", "csv": ""}
    fig, ax = plt.subplots(figsize=(6, 3.0))
    names = [z["zone"] for z in zones]
    scores = [z["risk_score"] for z in zones]
    colors = [BAD if s >= 0.66 else WARN if s >= 0.33 else ACCENT_2 for s in scores]
    ax.barh(names, scores, color=colors, edgecolor="#232a36")
    ax.set_xlabel("Surrogate risk score")
    ax.set_xlim(0, 1)
    ax.set_title("Surrogate risk by zone (approximate)")
    for i, v in enumerate(scores):
        ax.text(v + 0.01, i, f"{v:.2f}", va="center", fontsize=8)
    rows = list(zip(names, scores))
    return {"png_b64": _fig_to_b64(fig), "csv": _csv(["zone", "risk_score"], rows)}


# ---------------------------------------------------------------- ista 2a

def drop_verdict_bar(drops: list[dict]) -> dict:
    """drops: [{"orientation": str, "safety_factor": float, "verdict": str}, ...]"""
    if not drops:
        return {"png_b64": "", "csv": ""}
    fig, ax = plt.subplots(figsize=(6, 3.0))
    names = [d["orientation"] for d in drops]
    sfs = [d.get("safety_factor") or 0 for d in drops]
    colors = [
        ACCENT_2 if (d.get("verdict") == "pass") else
        BAD      if (d.get("verdict") == "fail") else
        WARN
        for d in drops
    ]
    ax.bar(names, sfs, color=colors, edgecolor="#232a36")
    ax.axhline(1.0, color=BAD, linestyle="--", linewidth=1, label="SF = 1.0 (pass threshold)")
    ax.set_ylabel("Safety factor")
    ax.set_title("ISTA 2A drop test — SF by orientation")
    for i, (sf, d) in enumerate(zip(sfs, drops)):
        ax.text(i, sf, f"{sf:.1f}\n{d.get('verdict','')}", ha="center", va="bottom", fontsize=8)
    ax.legend(loc="upper right")
    rows = [[d["orientation"], d.get("safety_factor"), d.get("verdict")] for d in drops]
    return {"png_b64": _fig_to_b64(fig), "csv": _csv(["orientation", "safety_factor", "verdict"], rows)}


# ---------------------------------------------------------------- comparison

def comparison_dashboard(designs: list[dict]) -> dict:
    """Three or four designs side-by-side: cost, safety_factor, mass, ROI.

    designs: [{"name", "cost_per_unit", "min_safety_factor", "mass_g", "roi_pct", "passes_ista"}, ...]
    """
    if not designs:
        return {"png_b64": "", "csv": ""}
    names = [d["name"] for d in designs]
    cost = [d.get("cost_per_unit") or 0 for d in designs]
    sf   = [d.get("min_safety_factor") or 0 for d in designs]
    mass = [d.get("mass_g") or 0 for d in designs]
    roi  = [d.get("roi_pct") or 0 for d in designs]

    fig, axes = plt.subplots(1, 4, figsize=(11, 3.2))
    titles = ["Unit cost ($)", "Min safety factor", "Unit mass (g)", "ROI (%)"]
    series = [cost, sf, mass, roi]
    palettes = [ACCENT, ACCENT_2, WARN, "#9b59b6"]
    for ax, t, ser, c in zip(axes, titles, series, palettes):
        bars = ax.bar(names, ser, color=c, edgecolor="#232a36")
        ax.set_title(t)
        plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
        for i, v in enumerate(ser):
            ax.text(i, v, f"{v:.2f}", ha="center", va="bottom", fontsize=7)
        # Mark passing-ISTA designs with a green outline
        for i, d in enumerate(designs):
            if d.get("passes_ista"):
                bars[i].set_edgecolor(ACCENT_2)
                bars[i].set_linewidth(2)
    fig.suptitle("Original vs Optimised designs", fontsize=11)

    rows = [
        [n, c, s, m, r, d.get("passes_ista")]
        for n, c, s, m, r, d in zip(names, cost, sf, mass, roi, designs)
    ]
    return {
        "png_b64": _fig_to_b64(fig),
        "csv": _csv(["design", "cost_per_unit", "min_safety_factor", "mass_g", "roi_pct", "passes_ista"], rows),
    }
