from backend.agents.design_config import DesignConfig, build_design_config


def test_build_design_config_from_case_summary():
    s = {"material": "PET", "carton_board_grade": "E-flute",
         "gross_weight_g": 500, "transit_modes": ["truck"], "objective": "reduce_cost"}
    cfg = build_design_config(s, drop_height_m=0.61)
    assert cfg.material_name == "PET"
    assert cfg.board_grade_record == "Corrugated E-flute"   # via flute_resolver
    assert cfg.mass_kg == 0.5                                # single canonical mass
    assert cfg.objective == "reduce_cost"
    assert cfg.drop_height_m == 0.61


def test_design_config_is_frozen():
    cfg = build_design_config({"material": "PET", "gross_weight_g": 100}, drop_height_m=0.3)
    import dataclasses, pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.mass_kg = 9.9


def test_mass_priority_filled_over_gross():
    # filled_mass_kg wins over gross_weight_g when both present.
    cfg = build_design_config(
        {"filled_mass_kg": 1.25, "gross_weight_g": 500}, drop_height_m=0.3)
    assert cfg.mass_kg == 1.25


def test_mass_default_when_nothing_provided():
    cfg = build_design_config({"material": "PET"}, drop_height_m=0.3)
    assert cfg.mass_kg == 0.6


def test_no_board_grade_yields_none_record():
    cfg = build_design_config({"material": "PET", "gross_weight_g": 200}, drop_height_m=0.3)
    assert cfg.board_grade_record is None


def test_missing_transit_modes_is_empty_tuple():
    cfg = build_design_config({"material": "PET"}, drop_height_m=0.3)
    assert cfg.transit_modes == ()
