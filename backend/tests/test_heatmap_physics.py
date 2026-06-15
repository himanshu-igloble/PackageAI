import numpy as np
import trimesh

from backend.agents.ista2a import Ista2AAgent
from backend.services import heatmap as hm
from backend.services.heatmap import build_carton_scenes, mckee_bct_n


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


def test_mckee_bct_increases_with_ect_and_caliper():
    # BCT = 5.87 * ECT * sqrt(perimeter_mm * caliper_mm)
    weak = mckee_bct_n(ect_kn_m=4.0, caliper_mm=1.5, perimeter_mm=1000)   # E-flute
    strong = mckee_bct_n(ect_kn_m=7.7, caliper_mm=4.0, perimeter_mm=1000) # C-flute
    assert strong > weak > 0


def test_build_carton_scenes_bct_provenance_and_ordering():
    base = {"carton_dimensions_mm": [400, 300, 250], "carton_stack_height": 5,
            "content_mass_kg": 2.0}
    e_scenes, _ = build_carton_scenes({**base, "carton_board_grade": "E-flute"})
    c_scenes, _ = build_carton_scenes({**base, "carton_board_grade": "C-flute"})
    # Same load, stronger board (C) => lower utilization than E.
    e_scale = e_scenes[0]["scale"]
    c_scale = c_scenes[0]["scale"]
    assert e_scale["mode"] == "mckee_bct" and c_scale["mode"] == "mckee_bct"
    assert e_scale["units"] == "applied_load/BCT"   # single, coherent units string
    assert c_scale["max_utilization"] < e_scale["max_utilization"]
    # empty case_summary must not crash and returns a 2-tuple
    scenes, glb = build_carton_scenes({})
    assert len(scenes) == 3
