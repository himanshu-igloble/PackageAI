"""Geometry parsing service.

Honesty contract (non-negotiable #9 in build directive):
- STL / OBJ / PLY / GLB / GLTF: parsed by trimesh into a real mesh.
- STEP / STP: full B-rep meshing requires pythonocc-core or cadquery. If
  those are not installed (default), we DO NOT silently swap in a bottle
  proxy. Instead we raise GeometryParseError with the precise reason and
  a hint. The user can:
    1. install the optional CAD dependency and retry,
    2. upload an STL/OBJ/GLB of the same model,
    3. explicitly opt into demo mode (?demo=true on the upload endpoint),
       in which case the proxy is used and every downstream result is
       labeled `demo_geometry`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh

from ..schemas import GeometrySummary


MESH_EXTENSIONS = {".stl", ".obj", ".ply", ".glb", ".gltf"}
STEP_EXTENSIONS = {".step", ".stp"}


# Optional CAD backends (only used when actually present). We try import once at
# module load so the absence is a fast, structured failure.
_PYOCC_AVAILABLE = False
_CADQUERY_AVAILABLE = False
try:
    import cadquery  # noqa: F401
    _CADQUERY_AVAILABLE = True
except Exception:
    pass
try:
    from OCC.Core import STEPControl  # noqa: F401
    _PYOCC_AVAILABLE = True
except Exception:
    pass


class GeometryParseError(Exception):
    """Structured parse failure. The route turns this into HTTP 422 with the
    fields below so the UI can render a precise error banner."""

    def __init__(self, *, reason: str, hint: str = "", header_excerpt: dict | None = None,
                 file_type: str = "", stderr_excerpt: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.header_excerpt = header_excerpt or {}
        self.file_type = file_type
        self.stderr_excerpt = stderr_excerpt

    def as_dict(self) -> dict:
        return {
            "error": "geometry_parse_failed",
            "reason": self.reason,
            "hint": self.hint,
            "file_type": self.file_type,
            "header_excerpt": self.header_excerpt,
            "stderr_excerpt": self.stderr_excerpt[:800] if self.stderr_excerpt else "",
        }


@dataclass
class ParsedGeometry:
    summary: GeometrySummary
    glb_bytes: bytes
    is_proxy: bool          # true only when caller explicitly requested demo mode


def _heuristic_critical_zones(mesh: trimesh.Trimesh) -> list[str]:
    """Coarse zoning along the tallest axis; reports zones with unusual curvature
    or thin radial extents."""
    bounds = mesh.bounds
    extents = bounds[1] - bounds[0]
    tall_axis = int(np.argmax(extents))
    zones: list[str] = []

    span = extents[tall_axis]
    if span <= 0:
        return zones

    lo, hi = bounds[0][tall_axis], bounds[1][tall_axis]
    third = span / 3.0
    cuts = {
        "base":      (lo, lo + third),
        "side_wall": (lo + third, lo + 2 * third),
        "shoulder":  (lo + 2 * third, hi - third * 0.25),
        "neck":      (hi - third * 0.25, hi),
    }
    verts = mesh.vertices
    normals = mesh.vertex_normals if (mesh.vertex_normals is not None and len(mesh.vertex_normals) == len(verts)) else None

    for name, (a, b) in cuts.items():
        in_zone = (verts[:, tall_axis] >= a) & (verts[:, tall_axis] < b)
        if not np.any(in_zone):
            continue
        curvature_proxy = float(np.var(normals[in_zone])) if normals is not None else 0.0
        radial = np.delete(verts[in_zone], tall_axis, axis=1)
        radial_extent = float(np.linalg.norm(radial.max(axis=0) - radial.min(axis=0)))
        if curvature_proxy > 0.05 or radial_extent < 0.4 * float(np.linalg.norm(extents) - span):
            zones.append(name)
    if not zones:
        zones.append("side_wall")
    return zones


def _build_bottle_proxy() -> trimesh.Trimesh:
    """Procedural bottle proxy — used ONLY in opt-in demo mode."""
    z = np.linspace(0, 220, 60)
    r = np.where(z < 10, 35,
        np.where(z < 30, 35 - (z - 10) * 0.2,
        np.where(z < 170, 32,
        np.where(z < 195, 32 - (z - 170) * 0.6,
        np.where(z < 210, 17, 12)))))
    sections = list(zip(z, r))
    angles = np.linspace(0, 2 * math.pi, 36, endpoint=False)
    verts = np.array([[ri * math.cos(a), ri * math.sin(a), zi]
                      for zi, ri in sections for a in angles])
    n_ang = len(angles)
    faces = []
    for i in range(len(sections) - 1):
        for j in range(n_ang):
            j2 = (j + 1) % n_ang
            a = i * n_ang + j; b = i * n_ang + j2
            c = (i + 1) * n_ang + j2; d = (i + 1) * n_ang + j
            faces.append([a, b, c]); faces.append([a, c, d])
    return trimesh.Trimesh(vertices=verts, faces=faces, process=True)


def _summarize_mesh(mesh: trimesh.Trimesh, *, file_type: str, notes: list[str], confidence: str) -> GeometrySummary:
    bounds = mesh.bounds
    extents = bounds[1] - bounds[0]
    bbox = {
        "min_x": float(bounds[0][0]), "min_y": float(bounds[0][1]), "min_z": float(bounds[0][2]),
        "max_x": float(bounds[1][0]), "max_y": float(bounds[1][1]), "max_z": float(bounds[1][2]),
    }
    dims = {
        "length_mm": float(extents[0]),
        "width_mm":  float(extents[1]),
        "height_mm": float(extents[2]),
    }
    try:
        volume = float(mesh.volume) if mesh.is_volume else None
    except Exception:
        volume = None
    try:
        area = float(mesh.area)
    except Exception:
        area = None

    return GeometrySummary(
        file_type=file_type,
        bbox_mm=bbox,
        overall_dims_mm=dims,
        volume_mm3=volume,
        surface_area_mm2=area,
        critical_zones=_heuristic_critical_zones(mesh),
        confidence=confidence,
        notes=notes,
    )


def _parse_step_header(path: Path) -> dict[str, str]:
    """Pull a few human-readable fields from a STEP/STP file's text header."""
    info: dict[str, str] = {}
    try:
        with path.open("r", errors="ignore") as f:
            head = f.read(8192)
    except Exception as exc:
        info["read_error"] = repr(exc)
        return info
    if "ISO-10303" not in head:
        info["warning"] = "File does not begin with an ISO-10303 STEP header."
    m = re.search(r"FILE_DESCRIPTION\s*\(\s*\(\s*'([^']*)'", head)
    if m:
        info["description"] = m.group(1)
    m = re.search(r"FILE_NAME\s*\(\s*'([^']*)'", head)
    if m:
        info["file_name"] = m.group(1)
    m = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", head)
    if m:
        info["schema"] = m.group(1)
    return info


def _step_to_mesh_via_cadquery(path: Path) -> trimesh.Trimesh:
    """Try cadquery if available. Raises on failure with stderr-grade detail."""
    import cadquery as cq
    from cadquery import exporters

    workplane = cq.importers.importStep(str(path))
    tmp_stl = path.with_suffix(".__tmp.stl")
    try:
        exporters.export(workplane, str(tmp_stl), exportType="STL", tolerance=0.1, angularTolerance=0.2)
        mesh = trimesh.load(str(tmp_stl), force="mesh")
        if isinstance(mesh, trimesh.Scene):
            mesh = trimesh.util.concatenate(tuple(mesh.geometry.values()))
        if mesh is None or len(mesh.faces) == 0:
            raise GeometryParseError(
                reason="cadquery produced an empty mesh from this STEP",
                hint="Try a higher tolerance or repair the STEP B-rep in your CAD tool.",
            )
        return mesh
    finally:
        try:
            tmp_stl.unlink()
        except FileNotFoundError:
            pass


def _step_to_mesh_via_pyocc(path: Path) -> trimesh.Trimesh:
    """Try pythonocc-core if available."""
    from OCC.Core.STEPControl import STEPControl_Reader
    from OCC.Core.IFSelect import IFSelect_RetDone
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.StlAPI import StlAPI_Writer

    reader = STEPControl_Reader()
    status = reader.ReadFile(str(path))
    if status != IFSelect_RetDone:
        raise GeometryParseError(reason=f"pythonocc STEPControl_Reader returned status {status}")
    reader.TransferRoots()
    shape = reader.OneShape()
    BRepMesh_IncrementalMesh(shape, 0.1, False, 0.5, True)
    tmp_stl = path.with_suffix(".__tmp.stl")
    writer = StlAPI_Writer()
    if not writer.Write(shape, str(tmp_stl)):
        raise GeometryParseError(reason="pythonocc StlAPI_Writer failed")
    try:
        mesh = trimesh.load(str(tmp_stl), force="mesh")
        if isinstance(mesh, trimesh.Scene):
            mesh = trimesh.util.concatenate(tuple(mesh.geometry.values()))
        if mesh is None or len(mesh.faces) == 0:
            raise GeometryParseError(reason="pythonocc produced an empty mesh")
        return mesh
    finally:
        try:
            tmp_stl.unlink()
        except FileNotFoundError:
            pass


def parse(path: Path, *, demo_mode: bool = False) -> ParsedGeometry:
    """Parse an uploaded geometry file. Raises GeometryParseError on failure.

    demo_mode=True is the ONLY way to fall back to the bottle proxy, and even
    then the returned summary carries confidence='approximate' and file_type
    'demo_proxy'."""
    suffix = path.suffix.lower()

    if suffix in MESH_EXTENSIONS:
        try:
            loaded = trimesh.load(str(path), force="mesh")
            mesh = (trimesh.util.concatenate(tuple(loaded.geometry.values()))
                    if isinstance(loaded, trimesh.Scene) else loaded)
            if mesh is None or len(mesh.faces) == 0:
                raise GeometryParseError(
                    reason=f"Mesh file '{path.name}' loaded but contained no faces.",
                    hint="Re-export with triangulated faces, ASCII or binary STL.",
                    file_type=suffix.lstrip("."),
                )
            summary = _summarize_mesh(
                mesh,
                file_type=suffix.lstrip("."),
                notes=["Mesh parsed directly from uploaded file."],
                confidence="estimated",
            )
            return ParsedGeometry(summary=summary, glb_bytes=mesh.export(file_type="glb"), is_proxy=False)
        except GeometryParseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GeometryParseError(
                reason=f"trimesh could not parse the mesh file: {exc!r}",
                hint="Try re-exporting as binary STL or GLB.",
                file_type=suffix.lstrip("."),
                stderr_excerpt=repr(exc),
            )

    if suffix in STEP_EXTENSIONS:
        header = _parse_step_header(path)

        # Attempt real STEP meshing if a CAD backend is installed.
        last_err: Exception | None = None
        if _CADQUERY_AVAILABLE:
            try:
                mesh = _step_to_mesh_via_cadquery(path)
                summary = _summarize_mesh(
                    mesh,
                    file_type="step",
                    notes=["Parsed via cadquery; tessellation tolerance 0.1 mm.",
                           f"STEP header: {header}" if header else ""],
                    confidence="estimated",
                )
                return ParsedGeometry(summary=summary, glb_bytes=mesh.export(file_type="glb"), is_proxy=False)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if _PYOCC_AVAILABLE:
            try:
                mesh = _step_to_mesh_via_pyocc(path)
                summary = _summarize_mesh(
                    mesh,
                    file_type="step",
                    notes=["Parsed via pythonocc; tessellation deflection 0.1 mm.",
                           f"STEP header: {header}" if header else ""],
                    confidence="estimated",
                )
                return ParsedGeometry(summary=summary, glb_bytes=mesh.export(file_type="glb"), is_proxy=False)
            except Exception as exc:  # noqa: BLE001
                last_err = exc

        if demo_mode:
            # Opt-in proxy: must be clearly labeled in every downstream result.
            mesh = _build_bottle_proxy()
            summary = _summarize_mesh(
                mesh,
                file_type="demo_proxy",
                notes=[
                    "DEMO MODE: original STEP could not be meshed without a CAD backend.",
                    "All results below are based on a bottle-shaped proxy, not your file.",
                    f"STEP header (kept for reference): {header}",
                ],
                confidence="approximate",
            )
            return ParsedGeometry(summary=summary, glb_bytes=mesh.export(file_type="glb"), is_proxy=True)

        # Default: hard fail with a precise reason.
        if not (_CADQUERY_AVAILABLE or _PYOCC_AVAILABLE):
            raise GeometryParseError(
                reason="STEP/STP files require a CAD backend that is not installed.",
                hint=(
                    "Install one of: `pip install cadquery` (preferred) or "
                    "`pip install pythonocc-core` (via conda). "
                    "Or re-upload the model as STL / OBJ / GLB. "
                    "Or re-upload with ?demo=true to use a labeled proxy."
                ),
                header_excerpt=header,
                file_type=suffix.lstrip("."),
            )
        raise GeometryParseError(
            reason=f"STEP parsing failed with installed backend: {last_err!r}",
            hint="Repair the STEP B-rep in your CAD tool or export as STL/GLB.",
            header_excerpt=header,
            file_type=suffix.lstrip("."),
            stderr_excerpt=repr(last_err),
        )

    raise GeometryParseError(
        reason=f"Unsupported geometry file type: {suffix}",
        hint="Accepted: .stl, .obj, .ply, .glb, .gltf, .step, .stp",
        file_type=suffix.lstrip("."),
    )
