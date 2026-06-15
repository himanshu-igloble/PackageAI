import numpy as np
import trimesh

from backend.agents.ista2a import Ista2AAgent
from backend.services import heatmap as hm


def test_stress_field_inputs_returns_physical_utilization():
    agent = Ista2AAgent()
    out = agent.stress_field_inputs(
        mass_kg=0.5, drop_height_m=0.61,
        material={"yield_strength_mpa": 55.0, "allowable_stress_mpa": 35.0},
    )
    for orient in ("top", "bottom", "side", "corner"):
        assert out[orient]["sigma_local_mpa"] > 0
        assert out[orient]["sigma_yield_mpa"] == 55.0
        assert out[orient]["utilization"] == out[orient]["sigma_local_mpa"] / 55.0
    # Corner concentrates more than side (Kt 2.5 vs 1.8, smaller area + stop dist).
    assert out["corner"]["utilization"] > out["side"]["utilization"]


def test_field_scale_is_yield_referenced_not_minmax():
    mesh = trimesh.creation.box(extents=(40, 40, 120))
    field = hm.compute_field(
        mesh, "drop_corner",
        material={"yield_strength_mpa": 55.0, "modulus_gpa": 2.0, "name": "PET"},
        stress_inputs={"corner": {"utilization": 0.8}},  # 80% of yield
    )
    assert field.scale["mode"] == "yield_referenced"
    assert field.scale["max_utilization"] == 0.8
    assert field.scale["units"] == "sigma_local/sigma_yield"


def test_two_scenes_are_comparable_under_fixed_scale():
    mesh = trimesh.creation.box(extents=(40, 40, 120))
    weak = hm.compute_field(mesh, "drop_corner",
        material={"yield_strength_mpa": 55.0, "modulus_gpa": 2.0, "name": "PET"},
        stress_inputs={"corner": {"utilization": 0.3}})
    strong = hm.compute_field(mesh, "drop_corner",
        material={"yield_strength_mpa": 55.0, "modulus_gpa": 2.0, "name": "PET"},
        stress_inputs={"corner": {"utilization": 0.9}})
    assert np.max(strong.per_face_stress) > np.max(weak.per_face_stress)
