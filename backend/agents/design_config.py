"""The single canonical design configuration for one case run. Built ONCE in
the orchestrator and threaded to every module so material, flute, mass,
drop-height, transit mode, and objective cannot diverge between modules."""
from __future__ import annotations
from dataclasses import dataclass
from .flute_resolver import resolve_flute

@dataclass(frozen=True)
class DesignConfig:
    material_name: str | None
    board_grade_record: str | None
    mass_kg: float
    drop_height_m: float
    transit_modes: tuple[str, ...]
    objective: str | None

def build_design_config(case_summary: dict, *, drop_height_m: float) -> DesignConfig:
    s = case_summary or {}
    gross_g = s.get("gross_weight_g")
    filled = s.get("filled_mass_kg")
    mass_kg = float(filled) if filled else (float(gross_g) / 1000.0 if gross_g else 0.6)
    board = s.get("carton_board_grade")
    return DesignConfig(
        material_name=s.get("material"),
        board_grade_record=resolve_flute(board).record_name if board else None,
        mass_kg=round(mass_kg, 4),
        drop_height_m=float(drop_height_m),
        transit_modes=tuple(s.get("transit_modes") or ()),
        objective=s.get("objective"),
    )
