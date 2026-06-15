import streamlit as st
import trimesh
import numpy as np
import plotly.graph_objects as go
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Minimal reimplementation of required schemas & identification logic
# ---------------------------------------------------------------------------
@dataclass
class GeometrySummary:
    overall_dims_mm: Optional[Dict[str, float]] = None
    critical_zones: Optional[List[str]] = None

    def __post_init__(self):
        if self.overall_dims_mm is None:
            self.overall_dims_mm = {}
        if self.critical_zones is None:
            self.critical_zones = []


@dataclass
class PackagingIdentification:
    routing_target: str
    packaging_class: str
    confidence: float
    reason: str
    signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "routing_target": self.routing_target,
            "packaging_class": self.packaging_class,
            "confidence": self.confidence,
            "reason": self.reason,
        }


_ROUTING: dict[str, str] = {
    "bottle_like": "bottle_flow",
    "flexible_packaging": "packet_flow",
    "unknown": "intake",
}


def _sorted_dims(dims: dict) -> tuple[float, float, float]:
    vals = sorted([
        float(dims.get("length_mm") or 0),
        float(dims.get("width_mm") or 0),
        float(dims.get("height_mm") or 0),
    ])
    return vals[0], vals[1], vals[2]


def identify_packaging(
    geometry: GeometrySummary,
    *,
    user_packaging_hint: Optional[str] = None,
) -> PackagingIdentification:
    dims = geometry.overall_dims_mm or {}
    zones: set[str] = set(geometry.critical_zones or [])

    d_min, d_mid, d_max = _sorted_dims(dims)

    if d_max <= 0 or d_mid <= 0 or d_min <= 0:
        return PackagingIdentification(
            routing_target="intake",
            packaging_class="unknown",
            confidence=0.0,
            reason="Degenerate bounding box; cannot classify.",
        )

    elongation = round(d_max / d_mid, 3)
    circularity = round(d_min / d_mid, 3)
    flatness = round(d_min / d_max, 3)

    has_neck = "neck" in zones
    has_shoulder = "shoulder" in zones

    signals = {
        "has_neck": has_neck,
        "has_shoulder": has_shoulder,
        "elongation": elongation,
        "circularity": circularity,
        "flatness": flatness,
        "d_min_mm": round(d_min, 1),
        "d_mid_mm": round(d_mid, 1),
        "d_max_mm": round(d_max, 1),
        "critical_zones": sorted(zones),
    }

    # =========================================================================
    # HARD OVERRIDES
    # =========================================================================
    if flatness < 0.12:
        signals.update(bottle_score=0, flexible_score=10)
        return PackagingIdentification(
            routing_target="packet_flow",
            packaging_class="flexible_packaging",
            confidence=0.90,
            reason=(
                f"Extreme flatness (flatness={flatness}): geometry is too thin "
                f"to be a bottle. Routing to flexible packaging."
            ),
            signals=signals,
        )

    if flatness < 0.25 and circularity < 0.55:
        signals.update(bottle_score=0, flexible_score=8)
        return PackagingIdentification(
            routing_target="packet_flow",
            packaging_class="flexible_packaging",
            confidence=0.85,
            reason=(
                f"Flat, non-cylindrical geometry (flatness={flatness}, "
                f"circularity={circularity}): characteristic of flexible "
                f"packaging; neck/shoulder zones ignored."
            ),
            signals=signals,
        )

    # =========================================================================
    # SCORING
    # =========================================================================
    bottle_score = 0
    flexible_score = 0

    if circularity >= 0.65:
        bottle_score += 2
    elif circularity >= 0.55:
        bottle_score += 1

    if has_neck and circularity >= 0.60:
        bottle_score += 3
    if has_shoulder and circularity >= 0.60:
        bottle_score += 2

    if elongation > 1.5 and circularity >= 0.60:
        bottle_score += 2
    if elongation > 2.0:
        bottle_score += 1

    if flatness >= 0.35:
        bottle_score += 1

    if circularity < 0.55:
        flexible_score += 2
    elif circularity < 0.65:
        flexible_score += 1

    if flatness < 0.30:
        flexible_score += 1

    if not has_neck and not has_shoulder:
        flexible_score += 2
    elif not has_neck or not has_shoulder:
        flexible_score += 1

    signals["bottle_score"] = bottle_score
    signals["flexible_score"] = flexible_score

    # =========================================================================
    # CLASSIFICATION
    # =========================================================================
    if bottle_score >= 5 and bottle_score > flexible_score + 1:
        packaging_class = "bottle_like"
        confidence = min(0.92, 0.65 + 0.05 * bottle_score)
        reason = (
            f"Bottle-like geometry: neck={has_neck}, shoulder={has_shoulder}, "
            f"elongation={elongation}, circularity={circularity}."
        )
    elif flexible_score >= 4 and flexible_score >= bottle_score:
        packaging_class = "flexible_packaging"
        confidence = min(0.88, 0.65 + 0.05 * flexible_score)
        reason = (
            f"Flexible geometry: flatness={flatness}, circularity={circularity}, "
            f"no dominant bottle signals."
        )
    elif flexible_score >= 2 and flexible_score > bottle_score:
        packaging_class = "flexible_packaging"
        confidence = 0.60
        reason = (
            f"Moderate flexible signals (flexible_score={flexible_score}, "
            f"flatness={flatness})."
        )
    elif bottle_score >= 4 and bottle_score > flexible_score:
        packaging_class = "bottle_like"
        confidence = 0.60
        reason = f"Moderate bottle-like signals (bottle_score={bottle_score})."
    else:
        packaging_class = "unknown"
        confidence = 0.30
        reason = (
            f"Insufficient geometry signals "
            f"(bottle_score={bottle_score}, flexible_score={flexible_score})."
        )

    # User hint override
    if user_packaging_hint and confidence < 0.70:
        hint = user_packaging_hint.lower()
        is_bottle_hint = any(t in hint for t in ("bottle", "jar", "can", "tube"))
        is_flexible_hint = any(
            t in hint for t in ("pouch", "sachet", "packet", "flexible", "flow_wrap", "flow wrap")
        )
        if is_bottle_hint and packaging_class != "bottle_like":
            packaging_class = "bottle_like"
            confidence = max(confidence, 0.60)
            reason += f" User hint '{user_packaging_hint}' applied."
        elif is_flexible_hint and packaging_class != "flexible_packaging":
            packaging_class = "flexible_packaging"
            confidence = max(confidence, 0.60)
            reason += f" User hint '{user_packaging_hint}' applied."

    routing_target = _ROUTING.get(packaging_class, "intake")

    return PackagingIdentification(
        routing_target=routing_target,
        packaging_class=packaging_class,
        confidence=round(confidence, 3),
        reason=reason,
        signals=signals,
    )


# ---------------------------------------------------------------------------
# Helper: compute bounding box from mesh
# ---------------------------------------------------------------------------
def compute_bounding_box(mesh: trimesh.Trimesh) -> Tuple[Dict[str, float], Tuple[float, float, float]]:
    """Returns (dims_mm, (x_range, y_range, z_range)) where dims_mm are length/width/height in mm."""
    bounds = mesh.bounds  # (min, max) shape (2,3)
    extents = bounds[1] - bounds[0]  # (dx, dy, dz)
    # Convert to mm (trimesh works in meters by default, but we'll assume mesh units are mm)
    # Actually trimesh loads in whatever units the file uses. We'll keep as is and assume mm.
    # If you know your files are in meters, multiply by 1000.
    length_mm = float(extents[0])
    width_mm = float(extents[1])
    height_mm = float(extents[2])
    dims = {
        "length_mm": length_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
    }
    return dims, (bounds[0], bounds[1])


def create_3d_plot(mesh: trimesh.Trimesh) -> go.Figure:
    """Create a Plotly Mesh3d figure from a trimesh object."""
    vertices = mesh.vertices
    faces = mesh.faces

    # Simple figure
    fig = go.Figure(data=[
        go.Mesh3d(
            x=vertices[:, 0],
            y=vertices[:, 1],
            z=vertices[:, 2],
            i=faces[:, 0],
            j=faces[:, 1],
            k=faces[:, 2],
            opacity=0.8,
            color='lightblue',
            lighting=dict(ambient=0.5, diffuse=0.8),
        )
    ])
    fig.update_layout(
        scene=dict(
            xaxis_title="X",
            yaxis_title="Y",
            zaxis_title="Z",
            aspectmode='data'
        ),
        margin=dict(l=0, r=0, b=0, t=30),
        height=500,
    )
    return fig


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Packaging Identifier", layout="wide")
st.title("📦 Packaging Geometry Classifier")
st.markdown("Upload a 3D model (GLB/GLTF) to classify it as **bottle-like**, **flexible packaging**, or **unknown**.")

uploaded_file = st.file_uploader("Choose a file", type=["glb", "gltf"])

if uploaded_file is not None:
    # Load the mesh
    with st.spinner("Loading 3D model..."):
        try:
            # Trimesh can load from file-like object
            mesh = trimesh.load(uploaded_file, force='mesh')
            if not isinstance(mesh, trimesh.Trimesh):
                st.error("Could not load a valid mesh from the file.")
                st.stop()
        except Exception as e:
            st.error(f"Error loading file: {e}")
            st.stop()

    # Compute bounding box
    dims_mm, bounds = compute_bounding_box(mesh)
    length = dims_mm["length_mm"]
    width = dims_mm["width_mm"]
    height = dims_mm["height_mm"]

    st.success(f"Loaded mesh with {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")

    # Display in two columns
    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("3D Preview")
        fig = create_3d_plot(mesh)
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.subheader("Bounding Box Dimensions (mm)")
        st.metric("Length (X)", f"{length:.1f}")
        st.metric("Width (Y)", f"{width:.1f}")
        st.metric("Height (Z)", f"{height:.1f}")

        st.divider()
        st.subheader("Manual Zone Overrides")
        st.caption("The geometry service would normally detect 'neck' and 'shoulder' zones. Here you can manually enable them for testing.")
        has_neck = st.checkbox("Has neck zone")
        has_shoulder = st.checkbox("Has shoulder zone")

        st.divider()
        st.subheader("Packaging Hint (optional)")
        st.caption("Applied only when confidence < 0.70")
        hint = st.text_input("e.g., 'bottle', 'pouch', 'sachet'", value="")

    # Build GeometrySummary
    critical_zones = []
    if has_neck:
        critical_zones.append("neck")
    if has_shoulder:
        critical_zones.append("shoulder")

    geom = GeometrySummary(
        overall_dims_mm=dims_mm,
        critical_zones=critical_zones,
    )

    # Run identification
    with st.spinner("Classifying..."):
        result = identify_packaging(geom, user_packaging_hint=hint if hint else None)

    # Show results
    st.divider()
    st.header("🔍 Classification Result")

    # Color coding
    if result.packaging_class == "bottle_like":
        st.success(f"**Routing target:** {result.routing_target} (bottle_flow)")
    elif result.packaging_class == "flexible_packaging":
        st.info(f"**Routing target:** {result.routing_target} (packet_flow)")
    else:
        st.warning(f"**Routing target:** {result.routing_target} (intake)")

    col_a, col_b = st.columns(2)
    with col_a:
        st.metric("Packaging class", result.packaging_class)
        st.metric("Confidence", f"{result.confidence:.0%}")
    with col_b:
        st.metric("Bottle score", result.signals.get("bottle_score", 0))
        st.metric("Flexible score", result.signals.get("flexible_score", 0))

    st.subheader("Reason")
    st.write(result.reason)

    with st.expander("📊 Detailed signals"):
        st.json(result.signals)

    # Optional: suggest improvement if misclassification is likely
    st.divider()
    st.caption("Note: This classifier uses only bounding box ratios and manually provided zones. Automatic neck/shoulder detection would require additional geometric analysis.")

else:
    st.info("👈 Upload a GLB or GLTF file to get started.")
    st.markdown("""
    **How it works:**
    1. The bounding box dimensions (length, width, height) are extracted.
    2. Derived metrics: flatness (min/max), circularity (min/mid), elongation (max/mid).
    3. A rule‑based scorer (with hard overrides) decides between `bottle_like`, `flexible_packaging`, or `unknown`.
    4. You can manually add `neck`/`shoulder` zones and provide a text hint to influence the result.
    """)