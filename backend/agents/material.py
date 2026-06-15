"""Material Intelligence Agent (5.3).

Lookup waterfall (architecture directive M5):

    1. Local DB           — verified, fastest, highest confidence.
    2. Local JSON cache   — previously fetched, persisted across sessions.
    3. Gemini-3-Pro fetch — canonical published values from the reasoning LLM,
                             written through to the cache on first hit.

Confidence labelling reflects the source:
    db          → "verified"
    cache       → "estimated" (we trust the cache, but it originated from #3)
    web/llm     → "estimated"
    not found   → "insufficient_data"
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ..models import MaterialRecord
from ..schemas import MaterialLookupResult
from ..services import material_cache
from .flute_resolver import resolve_flute


_SYNONYMS = {
    "pet": "PET",
    "polyethylene terephthalate": "PET",
    "rpet": "PCR-PET",
    "r-pet": "PCR-PET",
    "pcr pet": "PCR-PET",
    "pcr-pet": "PCR-PET",
    "hdpe": "HDPE",
    "high-density polyethylene": "HDPE",
    "rhdpe": "PCR-HDPE",
    "pcr hdpe": "PCR-HDPE",
    "pcr-hdpe": "PCR-HDPE",
    "ldpe": "LDPE",
    "pp": "PP",
    "polypropylene": "PP",
    "pcr pp": "PCR-PP",
    "pcr-pp": "PCR-PP",
    "pvc": "PVC",
    "ps": "PS",
    "polystyrene": "PS",
    "glass": "Glass",
    "aluminum": "Aluminium",
    "aluminium": "Aluminium",
    "cardboard": "Corrugated B-flute",
    "kraft": "Kraft Paperboard",
}


def canonicalize(name: str) -> str:
    lower = name.strip().lower()
    # Flute/corrugated grades resolve via the single flute resolver so that
    # E/C-flute keep their own records instead of collapsing to B-flute.
    if "flute" in lower or "corrugat" in lower:
        return resolve_flute(name).record_name
    return _SYNONYMS.get(lower, name.strip())


class MaterialAgent:
    def lookup(self, db: Session, name: str) -> Optional[MaterialLookupResult]:
        if not name:
            return MaterialLookupResult(name="(none)", source="material_db",
                                        confidence="insufficient_data",
                                        caveats=["No material name provided."])
        canonical = canonicalize(name)

        # 1) Verified DB hit
        rec = (
            db.query(MaterialRecord)
            .filter(MaterialRecord.name.ilike(canonical))
            .first()
        )
        if rec:
            return MaterialLookupResult(
                name=rec.name,
                grade=rec.grade,
                density_kg_m3=rec.density_kg_m3,
                modulus_gpa=rec.modulus_gpa,
                yield_strength_mpa=rec.yield_strength_mpa,
                allowable_stress_mpa=rec.allowable_stress_mpa,
                is_pcr=bool(getattr(rec, "is_pcr", False)),
                recycled_content_pct=float(getattr(rec, "recycled_content_pct", 0) or 0),
                carbon_intensity_kg_co2e_per_kg=getattr(rec, "carbon_intensity_kg_co2e_per_kg", None),
                pcr_substitute_for=getattr(rec, "pcr_substitute_for", None),
                notes=rec.notes,
                source=rec.source,
                confidence="verified",
                caveats=[],
            )

        # 2) Local cache hit
        cached = material_cache.get(canonical)
        if cached:
            return MaterialLookupResult(
                name=cached["name"],
                grade=cached.get("grade"),
                density_kg_m3=cached.get("density_kg_m3"),
                modulus_gpa=cached.get("modulus_gpa"),
                yield_strength_mpa=cached.get("yield_strength_mpa"),
                allowable_stress_mpa=cached.get("allowable_stress_mpa"),
                notes=cached.get("notes"),
                source=f"cache · {cached.get('source','llm')} · {cached.get('ts','')}",
                confidence="estimated",
                caveats=[
                    "Values served from local cache; originated from Gemini-3-Pro reference extraction.",
                    "Promote to the DB once a packaging engineer has verified the numbers.",
                ],
            )

        # 3) Gemini-3-Pro reference extraction (write through cache)
        fetched = material_cache.fetch_via_llm(canonical)
        if fetched:
            entry = material_cache.put(
                fetched["name"], fetched,
                source="gemini-3-pro extraction",
                confidence="estimated",
            )
            return MaterialLookupResult(
                name=entry["name"],
                grade=entry.get("grade"),
                density_kg_m3=entry.get("density_kg_m3"),
                modulus_gpa=entry.get("modulus_gpa"),
                yield_strength_mpa=entry.get("yield_strength_mpa"),
                allowable_stress_mpa=entry.get("allowable_stress_mpa"),
                notes=entry.get("notes"),
                source=f"gemini-3-pro · {entry.get('source_hint', 'published values')} · cached {entry.get('ts','')}",
                confidence="estimated",
                caveats=[
                    "First-time lookup: values fetched from the reasoning LLM and cached locally.",
                    "Promote to the DB after a packaging engineer signs them off.",
                ],
            )

        # 4) Not found anywhere — block downstream
        return MaterialLookupResult(
            name=canonical,
            source="material_db + cache + llm (all miss)",
            confidence="insufficient_data",
            caveats=[
                f"No verified record, cached entry, or LLM-derived reference for '{name}'.",
                "Add it manually to data/materials.json (preferred) or to data/material_cache.json before proceeding.",
            ],
        )
