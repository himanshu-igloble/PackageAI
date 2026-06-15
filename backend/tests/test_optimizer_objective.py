from backend.agents.optimization import OptimizationAgent


def test_bottle_reduce_cost_never_ranks_pricier_above_cheaper():
    """Given a slate of ISTA-passing variants, reduce_cost must order the
    cheapest first regardless of PCR-first insertion."""
    agent = OptimizationAgent()
    cheap = {"name": "cheap", "cost_per_unit": 0.10, "passes_ista": True, "mass_g": 20}
    dear = {"name": "dear", "cost_per_unit": 0.40, "passes_ista": True, "mass_g": 18}
    out = agent._finalise_slate([dear, cheap], intent="reduce_cost", target_passing=2)
    assert [v["name"] for v in out][0] == "cheap"
