from backend.agents.guardrail import GuardrailAgent


def test_review_consistency_flags_material_divergence():
    g = GuardrailAgent()
    snapshot = {
        "design_config": {"material_name": "PET", "board_grade_record": "Corrugated E-flute",
                          "mass_kg": 0.5, "drop_height_m": 0.61},
        "deterministic": {"material_name": "PET", "mass_kg": 0.5},
        "ista2a": {"material_name": "HDPE", "mass_kg": 0.5, "drop_height_m": 0.61},  # WRONG material
        "report": {"material_name": "PET", "drop_height_m": 0.61},
    }
    report = g.review_consistency(snapshot)
    assert report.ok is False
    assert any("material" in b.lower() for b in report.blocks)


def test_review_consistency_flags_drop_height_divergence():
    g = GuardrailAgent()
    snapshot = {
        "design_config": {"material_name": "PET", "mass_kg": 0.5, "drop_height_m": 0.61},
        "ista2a": {"material_name": "PET", "mass_kg": 0.5, "drop_height_m": 0.46},  # mismatched
    }
    report = g.review_consistency(snapshot)
    assert report.ok is False
    assert any("drop" in b.lower() for b in report.blocks)


def test_review_consistency_passes_when_aligned():
    g = GuardrailAgent()
    base = {"material_name": "PET", "mass_kg": 0.5, "drop_height_m": 0.61}
    snapshot = {"design_config": base, "deterministic": base, "ista2a": base, "report": base}
    assert g.review_consistency(snapshot).ok is True
