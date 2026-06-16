"""Task D3 — provenance: record WHICH board grade / MaterialRecord actually
drove the PCR and calculation outputs, so an auditor can detect a flute
substitution (the flute-collapse bug class)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.agents.calculation import CalculationAgent
from backend.agents.flute_resolver import canonical_flute_name, resolve_flute
from backend.agents.pcr import PCRAgent
from backend.models import Base, MaterialRecord
from backend.schemas import PCRSubstitution


# --- Step 1: helper-level guarantees (unit) --------------------------------

def test_resolver_provenance_for_e_flute():
    spec = resolve_flute("E-flute")
    assert spec.record_name == "Corrugated E-flute"
    assert spec.is_fallback is False


def test_canonical_flute_name_for_pcr_passthrough():
    assert canonical_flute_name("PCR-Corrugated-E") is None   # PCR not rewritten
    assert canonical_flute_name("E-flute") == "Corrugated E-flute"


# --- Step 2: schema carries the new provenance fields ----------------------

def test_pcr_substitution_has_provenance_fields():
    sub = PCRSubstitution(
        baseline_material="Corrugated E-flute",
        baseline_density_kg_m3=150.0,
        baseline_part_mass_g=1.0,
        candidate_material="PCR-Corrugated-E",
        candidate_density_kg_m3=150.0,
        candidate_recycled_content_pct=80.0,
        candidate_part_mass_g=1.0,
        mass_delta_pct=0.0,
        baseline_record_used="Corrugated E-flute",
        board_grade_used="Corrugated E-flute",
    )
    assert sub.baseline_record_used == "Corrugated E-flute"
    assert sub.board_grade_used == "Corrugated E-flute"


# --- Step 2: real PCRAgent.evaluate records the resolved provenance ---------

def _seeded_session():
    """In-memory SQLite seeded with a virgin E-flute board and its PCR analogue."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add_all([
        MaterialRecord(
            name="Corrugated E-flute", grade="E", density_kg_m3=150.0,
            allowable_stress_mpa=2.0, carbon_intensity_kg_co2e_per_kg=1.2,
            is_pcr=False,
        ),
        MaterialRecord(
            name="PCR-Corrugated-E", grade="E", density_kg_m3=150.0,
            allowable_stress_mpa=1.9, carbon_intensity_kg_co2e_per_kg=0.7,
            is_pcr=True, recycled_content_pct=80.0,
            pcr_substitute_for="Corrugated E-flute",
        ),
    ])
    db.commit()
    return db


def test_evaluate_records_baseline_and_board_grade_for_e_flute():
    db = _seeded_session()
    try:
        sub = PCRAgent().evaluate(
            db,
            baseline_material_name="E-flute",   # free-text grade, NOT the record name
            part_volume_mm3=1000.0,
        )
    finally:
        db.close()
    assert sub is not None
    # The recorded baseline must be the REAL E-flute record, never collapsed to B.
    assert sub.baseline_record_used == "Corrugated E-flute"
    assert sub.board_grade_used == "Corrugated E-flute"
    assert sub.baseline_material == "Corrugated E-flute"


# --- Step 3: calculation records which flute drove the compression result --

def test_compression_calc_records_board_grade_used():
    out = CalculationAgent().compression_safety_factor(
        applied_load_n=500.0,
        allowable_stress_mpa=2.0,
        load_bearing_area_mm2=2500.0,
        board_grade="E-flute",
    )
    assert out.inputs["board_grade_used"] == "Corrugated E-flute"


def test_compression_calc_without_board_grade_omits_provenance():
    out = CalculationAgent().compression_safety_factor(
        applied_load_n=500.0,
        allowable_stress_mpa=2.0,
        load_bearing_area_mm2=2500.0,
    )
    assert "board_grade_used" not in out.inputs   # bottle path unaffected
