from backend.agents.ista2a import Ista2AAgent


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
