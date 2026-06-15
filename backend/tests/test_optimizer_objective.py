from backend.agents.optimization import OptimizationAgent
from backend.agents.objective_ranking import rank_objects


def test_bottle_reduce_cost_never_ranks_pricier_above_cheaper():
    """Given a slate of ISTA-passing variants, reduce_cost must order the
    cheapest first regardless of PCR-first insertion."""
    agent = OptimizationAgent()
    cheap = {"name": "cheap", "cost_per_unit": 0.10, "passes_ista": True, "mass_g": 20}
    dear = {"name": "dear", "cost_per_unit": 0.40, "passes_ista": True, "mass_g": 18}
    out = agent._finalise_slate([dear, cheap], intent="reduce_cost", target_passing=2)
    assert [v["name"] for v in out][0] == "cheap"


def test_rank_objects_handles_duplicate_names():
    class V:
        def __init__(self, name, cost):
            self.name = name
            self.cost_impact_pct = cost
    a = V("Alternative", 5.0)
    b = V("Alternative", -8.0)   # same name, distinct object, cheaper
    ranked = rank_objects([a, b], intent="reduce_cost",
                          baseline_relative_key="cost_impact_pct", strict=False)
    assert ranked[0] is b and ranked[1] is a   # cheaper first, NO object lost
    assert len(ranked) == 2


def test_rank_objects_preserves_object_identity():
    """rank_objects must return the SAME original objects (by identity), not
    copies, and must not lose or duplicate any of them."""
    class V:
        def __init__(self, name, cost):
            self.name = name
            self.cost_impact_pct = cost
    objs = [V("a", 3.0), V("b", -1.0), V("c", -5.0)]
    ranked = rank_objects(objs, intent="reduce_cost",
                          baseline_relative_key="cost_impact_pct", strict=False)
    # cheapest-first order, every original object present exactly once
    assert [o.name for o in ranked] == ["c", "b", "a"]
    assert {id(o) for o in ranked} == {id(o) for o in objs}
