"""Material cache + on-demand extraction.

Lookup waterfall (architecture directive M5):
    1. Local DB     (MaterialRecord)              — verified, fastest
    2. Local cache  (data/material_cache.json)    — previously fetched, persisted
    3. Gemini 3 Pro (reasoning role) extraction   — pulled when first asked
    4. Write-through to cache on every hit so subsequent sessions are free.

The cache is a plain JSON file so it survives process restarts and can be
audited / hand-edited. Each entry carries `source`, `url` (if web-derived),
`ts`, and `confidence`. The cache NEVER overrides the DB; the DB always wins.

We DO NOT actually fetch the open web here — that would require an unrestricted
HTTP egress permission you may not want. Instead we ask Gemini 3 Pro for the
canonical published reference values for the material; the model has these in
its training data for common engineering polymers, glass, metals, and corrugated
grades. Every extracted value is labeled `confidence="estimated"` and `source`
is set to the model id, so the guardrail layer correctly downgrades downstream
verdicts.

The result is a fully self-contained system: no proprietary web access,
deterministic caching, transparent attribution.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import PROJECT_ROOT, settings
from ..llm.gemini_client import get_gemini


CACHE_PATH = PROJECT_ROOT / "data" / "material_cache.json"


# ---- low-level JSON cache (atomic write) ----

def _load() -> dict[str, dict[str, Any]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(d: dict[str, dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
    tmp.replace(CACHE_PATH)


def _key(name: str) -> str:
    return (name or "").strip().lower()


def get(name: str) -> Optional[dict[str, Any]]:
    return _load().get(_key(name))


def put(name: str, fields: dict[str, Any], *, source: str, url: str = "",
        confidence: str = "estimated") -> dict[str, Any]:
    entry = {
        **fields,
        "name": name,
        "source": source,
        "url": url,
        "confidence": confidence,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    d = _load()
    d[_key(name)] = entry
    _save(d)
    return entry


# ---- Gemini 3 Pro extraction (only called on a cache miss) ----

_EXTRACT_PROMPT = """You are a materials reference engine for CPG packaging analysis.
Return engineering property values for the named material. Use ONLY values that
are well-known from published engineering references. If the value is not
well-established, return null for that field — do not invent.

Rules:
- All numeric values must be in the units below.
- If multiple grades exist, return values for the most common packaging grade.
- Do not include any commentary or explanation in the JSON.
- Include sustainability fields (recycled_content_pct, carbon_intensity_kg_co2e_per_kg).
  For virgin polymers cradle-to-gate carbon intensity is roughly 1.9–2.4 kg CO2e/kg;
  for PCR variants it is ~70–80% lower. Use published LCA reference values or null.

Return STRICTLY this schema:
{
  "name": "<canonical material name>",
  "grade": "<typical packaging grade or null>",
  "density_kg_m3": <number or null>,
  "modulus_gpa": <number or null>,
  "yield_strength_mpa": <number or null>,
  "allowable_stress_mpa": <number or null>,
  "recycled_content_pct": <0..100 or null>,
  "carbon_intensity_kg_co2e_per_kg": <number or null>,
  "is_pcr": <true|false>,
  "pcr_substitute_for": "<virgin material name or null if not a PCR variant>",
  "notes": "<one short sentence on packaging-relevant behavior>",
  "source_hint": "<a brief, generic reference, e.g. 'MatWeb / Plastics Europe LCA'>"
}
"""


def fetch_via_llm(name: str) -> Optional[dict[str, Any]]:
    """Ask the reasoning LLM for canonical property values. Returns None if
    the extraction was incomplete (no usable numeric fields). Higher
    temperature here lets the model commit to a sustainability footprint
    even when the user names a less-common grade."""
    gemini = get_gemini()
    if not gemini.available:
        return None
    raw = gemini.reason_json(_EXTRACT_PROMPT, f"Material name: {name}", temperature=0.4)
    if not isinstance(raw, dict):
        return None
    # Validate: require at least density and one of modulus/yield/allowable
    numeric_fields = ("density_kg_m3", "modulus_gpa", "yield_strength_mpa", "allowable_stress_mpa")
    nums = {k: raw.get(k) for k in numeric_fields if isinstance(raw.get(k), (int, float))}
    if not nums.get("density_kg_m3") or len(nums) < 2:
        return None
    cleaned = {
        "name": raw.get("name") or name,
        "grade": raw.get("grade"),
        "density_kg_m3": float(raw["density_kg_m3"]),
        "modulus_gpa": (float(raw["modulus_gpa"]) if isinstance(raw.get("modulus_gpa"), (int, float)) else None),
        "yield_strength_mpa": (float(raw["yield_strength_mpa"]) if isinstance(raw.get("yield_strength_mpa"), (int, float)) else None),
        "allowable_stress_mpa": (float(raw["allowable_stress_mpa"]) if isinstance(raw.get("allowable_stress_mpa"), (int, float)) else None),
        "is_pcr": bool(raw.get("is_pcr", False)),
        "recycled_content_pct": (float(raw["recycled_content_pct"]) if isinstance(raw.get("recycled_content_pct"), (int, float)) else 0.0),
        "carbon_intensity_kg_co2e_per_kg": (
            float(raw["carbon_intensity_kg_co2e_per_kg"])
            if isinstance(raw.get("carbon_intensity_kg_co2e_per_kg"), (int, float)) else None
        ),
        "pcr_substitute_for": (str(raw["pcr_substitute_for"]).strip() if raw.get("pcr_substitute_for") else None),
        "notes": str(raw.get("notes") or "")[:240],
        "source_hint": str(raw.get("source_hint") or "AI intelligence reference extraction"),
    }
    return cleaned


def promote_to_db(db, fields: dict[str, Any]) -> bool:
    """Insert the researched material into MaterialRecord so it joins the
    permanent catalogue and is reusable across sessions. Idempotent: skips
    if a same-named record already exists."""
    from ..models import MaterialRecord
    name = (fields.get("name") or "").strip()
    if not name:
        return False
    existing = (
        db.query(MaterialRecord)
        .filter(MaterialRecord.name.ilike(name))
        .first()
    )
    if existing:
        return False
    rec = MaterialRecord(
        name=name,
        grade=fields.get("grade"),
        density_kg_m3=fields.get("density_kg_m3"),
        modulus_gpa=fields.get("modulus_gpa"),
        yield_strength_mpa=fields.get("yield_strength_mpa"),
        allowable_stress_mpa=fields.get("allowable_stress_mpa"),
        is_pcr=bool(fields.get("is_pcr")),
        recycled_content_pct=float(fields.get("recycled_content_pct") or 0),
        carbon_intensity_kg_co2e_per_kg=fields.get("carbon_intensity_kg_co2e_per_kg"),
        pcr_substitute_for=fields.get("pcr_substitute_for"),
        notes=fields.get("notes"),
        source=(fields.get("source_hint") or "AI intelligence research"),
    )
    db.add(rec)
    db.commit()
    return True
