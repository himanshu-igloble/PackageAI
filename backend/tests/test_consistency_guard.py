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


def test_design_config_mass_matches_gross_first_for_dual_input():
    from backend.agents.design_config import build_design_config
    s = {"material": "PET", "gross_weight_g": 800, "filled_mass_kg": 0.5}
    cfg = build_design_config(s, drop_height_m=0.5)
    # Orchestrator uses gross-first => 0.8 kg; design_config must match so the
    # consistency gate does not false-fire.
    assert cfg.mass_kg == 0.8


def test_snapshot_collects_all_modules():
    from backend.orchestrator.orchestrator import Orchestrator
    keys = set(Orchestrator._consistency_snapshot_keys())
    assert keys == {"design_config", "deterministic", "ista2a", "ista6a", "report"}
