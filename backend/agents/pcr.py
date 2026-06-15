"""PCR (Post-Consumer Recycled) substitution agent.

Given the baseline material and the part's geometry-derived volume, find the
matching PCR analogue from the material DB and compute a transparent set of
deltas: mass, carbon intensity, mechanical-property impact, and (optionally)
projected annual carbon savings at the user's annual volume.

All numbers are computed, never invented:
    part_volume_m3 × density_kg_m3       → part_mass_kg
    part_mass_kg   × carbon_intensity    → cradle-to-gate kg CO₂e per part
    Δcarbon × annual_units               → annual GHG savings

If no PCR analogue exists for the baseline material the agent returns None
with an explicit caveat rather than fabricating one.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ..models import MaterialRecord
from ..schemas import PCRSubstitution


# Names whose virgin grade and PCR grade live under different canonical names.
# Kept narrow on purpose — the materials.json `pcr_substitute_for` column is
# the source of truth; this alias map only handles trivial spelling drift.
# Packet/brush aliases map common user-facing material names onto DB entries.
_NAME_ALIASES = {
    # Metal
    "aluminum": "Aluminium",
    # Paperboard / board — brush backer card and packet carton
    "paperboard":          "Kraft Paperboard",
    "backer_card":         "Kraft Paperboard",
    "backer card":         "Kraft Paperboard",
    "sbs":                 "Kraft Paperboard",
    "kraft":               "Kraft Paperboard",
    "folding boxboard":    "Kraft Paperboard",
    "coated board":        "Kraft Paperboard",
    "fbb":                 "Kraft Paperboard",
    "grb":                 "Kraft Paperboard",
    "crb":                 "Kraft Paperboard",
    # Corrugated — packet secondary carton
    "corrugated":          "Corrugated B-flute",
    "corrugated board":    "Corrugated B-flute",
    "corrugated_carton":   "Corrugated B-flute",
    "corrugated_shipper":  "Corrugated B-flute",
    "corrugated carton":   "Corrugated B-flute",
    "corrugated shipper":  "Corrugated B-flute",
    "e-flute":             "Corrugated B-flute",
    "b-flute":             "Corrugated B-flute",
    "c-flute":             "Corrugated B-flute",
}


def _canonicalise(name: str) -> str:
    if not name:
        return ""
    lower = name.strip().lower()
    return _NAME_ALIASES.get(lower, name.strip())


def _pct_delta(new: float, base: float) -> Optional[float]:
    if base in (0, None) or new is None:
        return None
    return round(100.0 * (new - base) / base, 2)


class PCRAgent:
    """DB-grounded PCR substitution recommender."""

    def find_substitute(
        self,
        db: Session,
        *,
        baseline_material_name: str,
    ) -> Optional[MaterialRecord]:
        """Return the best PCR analogue for the given virgin material name,
        or None if no PCR variant has been seeded."""
        canonical = _canonicalise(baseline_material_name)
        if not canonical:
            return None

        # Prefer the highest recycled content; tie-break on lowest carbon.
        candidates = (
            db.query(MaterialRecord)
            .filter(MaterialRecord.is_pcr.is_(True))
            .filter(MaterialRecord.pcr_substitute_for.ilike(canonical))
            .all()
        )
        if not candidates:
            return None
        candidates.sort(
            key=lambda m: (
                -(m.recycled_content_pct or 0),
                m.carbon_intensity_kg_co2e_per_kg or float("inf"),
            )
        )
        return candidates[0]

    def evaluate(
        self,
        db: Session,
        *,
        baseline_material_name: str,
        part_volume_mm3: Optional[float],
        annual_units: int = 1_000_000,
    ) -> Optional[PCRSubstitution]:
        """Build the substitution delta. `part_volume_mm3` is the CAD-derived
        polymer volume of the part (wall volume, not fill volume); if not
        available the function still returns the per-kg comparison with mass
        fields filled in for a 1 g reference part."""
        baseline = (
            db.query(MaterialRecord)
            .filter(MaterialRecord.name.ilike(_canonicalise(baseline_material_name)))
            .first()
        )
        if not baseline:
            return None
        candidate = self.find_substitute(db, baseline_material_name=baseline.name)
        if not candidate:
            return None

        # Convert mm³ → m³ (1 m³ = 1e9 mm³). Reference 1 g when volume is unknown.
        if part_volume_mm3 and part_volume_mm3 > 0:
            volume_m3 = part_volume_mm3 * 1e-9
            base_mass_kg = volume_m3 * (baseline.density_kg_m3 or 0)
            cand_mass_kg = volume_m3 * (candidate.density_kg_m3 or 0)
        else:
            base_mass_kg = 0.001
            cand_mass_kg = 0.001 * (candidate.density_kg_m3 or 1) / (baseline.density_kg_m3 or 1)

        base_carbon = (
            base_mass_kg * baseline.carbon_intensity_kg_co2e_per_kg
            if baseline.carbon_intensity_kg_co2e_per_kg is not None else None
        )
        cand_carbon = (
            cand_mass_kg * candidate.carbon_intensity_kg_co2e_per_kg
            if candidate.carbon_intensity_kg_co2e_per_kg is not None else None
        )

        mass_delta_pct = _pct_delta(cand_mass_kg, base_mass_kg) or 0.0
        carbon_delta_pct = (
            _pct_delta(cand_carbon, base_carbon)
            if (cand_carbon is not None and base_carbon is not None) else None
        )

        annual_savings = None
        if base_carbon is not None and cand_carbon is not None and annual_units > 0:
            annual_savings = round((base_carbon - cand_carbon) * annual_units, 1)

        # Mechanical-property impact: report % change so the user can judge
        # whether downstream simulations need a wall-thickness compensation.
        mechanical_delta = {}
        for prop, attr in (
            ("modulus_pct",            "modulus_gpa"),
            ("yield_strength_pct",     "yield_strength_mpa"),
            ("allowable_stress_pct",   "allowable_stress_mpa"),
        ):
            base_v = getattr(baseline, attr, None)
            cand_v = getattr(candidate, attr, None)
            d = _pct_delta(cand_v, base_v) if (base_v and cand_v) else None
            if d is not None:
                mechanical_delta[prop] = d

        caveats: list[str] = []
        if any(
            (mechanical_delta.get(k) or 0) <= -8
            for k in ("modulus_pct", "yield_strength_pct", "allowable_stress_pct")
        ):
            caveats.append(
                "Candidate is more than 8% weaker on at least one mechanical property — "
                "re-run the drop / compression checks before specifying."
            )
        if part_volume_mm3 in (None, 0):
            caveats.append(
                "Part volume not available from CAD; mass figures shown are per-kg references only."
            )
        if candidate.recycled_content_pct and candidate.recycled_content_pct < 100:
            caveats.append(
                f"Candidate is a blended PCR grade ({int(candidate.recycled_content_pct)}% recycled); "
                "100% PCR grades exist with higher carbon savings if supply allows."
            )

        return PCRSubstitution(
            baseline_material=baseline.name,
            baseline_density_kg_m3=float(baseline.density_kg_m3 or 0),
            baseline_carbon_kg_co2e_per_kg=baseline.carbon_intensity_kg_co2e_per_kg,
            baseline_part_mass_g=round(base_mass_kg * 1000.0, 3),
            baseline_part_carbon_kg_co2e=(round(base_carbon, 4) if base_carbon is not None else None),

            candidate_material=candidate.name,
            candidate_density_kg_m3=float(candidate.density_kg_m3 or 0),
            candidate_recycled_content_pct=float(candidate.recycled_content_pct or 0),
            candidate_carbon_kg_co2e_per_kg=candidate.carbon_intensity_kg_co2e_per_kg,
            candidate_part_mass_g=round(cand_mass_kg * 1000.0, 3),
            candidate_part_carbon_kg_co2e=(round(cand_carbon, 4) if cand_carbon is not None else None),

            mass_delta_pct=mass_delta_pct,
            carbon_delta_pct=carbon_delta_pct,
            annual_carbon_savings_kg_co2e=annual_savings,
            annual_units=annual_units,
            mechanical_delta=mechanical_delta,
            caveats=caveats,
        )
