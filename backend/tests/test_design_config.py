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
