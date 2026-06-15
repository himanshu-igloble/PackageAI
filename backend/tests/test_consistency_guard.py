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


def test_review_consistency_flags_board_grade_divergence():
    g = GuardrailAgent()
    snapshot = {
        "design_config": {"board_grade_record": "Corrugated E-flute"},
        "ista2a": {"board_grade_record": "Corrugated B-flute"},  # collapsed flute
    }
    report = g.review_consistency(snapshot)
    assert report.ok is False
    assert any("board_grade_record" in b for b in report.blocks)


def test_review_consistency_reports_multiple_blocks():
    g = GuardrailAgent()
    snapshot = {
        "design_config": {"material_name": "PET", "drop_height_m": 0.61},
        "ista2a": {"material_name": "HDPE", "drop_height_m": 0.46},  # both wrong
    }
    report = g.review_consistency(snapshot)
    assert report.ok is False
    assert len(report.blocks) == 2


def test_review_consistency_empty_and_no_canonical_are_safe():
    g = GuardrailAgent()
    assert g.review_consistency({}).ok is True
    # No design_config => nothing to compare against => ok.
    assert g.review_consistency({"ista2a": {"material_name": "PET"}}).ok is True
