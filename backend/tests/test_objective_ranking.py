from backend.agents.objective_ranking import rank_variants, objective_metric


def test_reduce_cost_orders_by_ascending_cost():
    variants = [
        {"name": "A", "cost_per_unit": 0.30},
        {"name": "B", "cost_per_unit": 0.10},
        {"name": "C", "cost_per_unit": 0.20},
    ]
    ranked = rank_variants(variants, intent="reduce_cost")
    assert [v["name"] for v in ranked] == ["B", "C", "A"]


def test_increase_strength_orders_by_descending_safety_factor():
    variants = [
        {"name": "A", "min_safety_factor": 1.2},
        {"name": "B", "min_safety_factor": 2.0},
    ]
    ranked = rank_variants(variants, intent="increase_strength")
    assert [v["name"] for v in ranked] == ["B", "A"]


def test_reduce_cost_drops_variants_worse_than_baseline_when_strict():
    variants = [
        {"name": "cheaper", "cost_impact_pct": -10},
        {"name": "pricier", "cost_impact_pct": +15},
    ]
    ranked = rank_variants(variants, intent="reduce_cost",
                           baseline_relative_key="cost_impact_pct", strict=True)
    assert [v["name"] for v in ranked] == ["cheaper"]   # pricier dropped


def test_unknown_intent_is_stable_passthrough():
    variants = [{"name": "A"}, {"name": "B"}]
    assert rank_variants(variants, intent="other") == variants


def test_cost_fallback_orders_by_cost_impact_pct_when_no_absolute_cost():
    # No cost_per_unit present -> reduce_cost ranking falls back to cost_impact_pct.
    variants = [
        {"name": "A", "cost_impact_pct": 5},
        {"name": "B", "cost_impact_pct": -8},
        {"name": "C", "cost_impact_pct": 0},
    ]
    ranked = rank_variants(variants, intent="reduce_cost")
    assert [v["name"] for v in ranked] == ["B", "C", "A"]


def test_missing_metric_variants_sort_last():
    variants = [
        {"name": "has", "cost_per_unit": 0.20},
        {"name": "missing"},
        {"name": "cheap", "cost_per_unit": 0.05},
    ]
    ranked = rank_variants(variants, intent="reduce_cost")
    assert [v["name"] for v in ranked] == ["cheap", "has", "missing"]


def test_strict_polarity_is_independent_of_objective_direction():
    # strict with a 'max' objective must NOT drop cheaper (negative cost_impact) variants.
    variants = [
        {"name": "cheaper_strong", "min_safety_factor": 2.0, "cost_impact_pct": -10},
        {"name": "pricier_strong", "min_safety_factor": 2.5, "cost_impact_pct": +20},
    ]
    ranked = rank_variants(variants, intent="increase_strength",
                           baseline_relative_key="cost_impact_pct", strict=True)
    names = [v["name"] for v in ranked]
    assert "cheaper_strong" in names          # not wrongly dropped
    assert "pricier_strong" not in names       # positive cost impact dropped
