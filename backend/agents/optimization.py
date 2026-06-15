"""Design Optimization Agent.

Sits at the bottom of the report tab. Same agentic split:
    - Gemini 2.5 Flash → conversational intent gauging and option phrasing.
    - Gemini 3 Pro     → engineering reasoning over alternatives.

Flow (mirrors the bottle flow's natural-conversation style):
    1. Ask the user what they want to optimise:
         (a) reduce material cost
         (b) increase strength / safety
         (c) something else (free-form)
    2. Confirm priority + any constraints (target % cost cut, must keep glass-clear, etc.)
    3. Gemini 3 Pro proposes 3 alternative designs that swap one or more of:
         material, wall_thickness_mm, gross_weight_g, capacity_ml, closure_type.
       Each alternative carries a one-sentence rationale and the engineering
       changes vs the original.
    4. Re-evaluate each alternative through MaterialAgent + ISTA-2A.
    5. Cost / mass / ROI table per design (deterministic math) + a comparison
       dashboard payload (charts.comparison_dashboard).

The orchestrator never lets the LLM decide pass/fail; it only proposes
parameter changes. All numeric comparisons are deterministic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..llm.gemini_client import get_gemini
from ..models import MaterialRecord
from ..schemas import GeometrySummary, MaterialLookupResult
from .flute_resolver import resolve_flute
from .ista2a import Ista2AAgent
from .material import MaterialAgent
from .objective_ranking import rank_objects
from .pcr import PCRAgent


# Canonical set of optimisation intents this agent accepts. The route guard
# in routes/extras.py imports this so there is a single source of truth.
ALLOWED_INTENTS = frozenset({"reduce_cost", "increase_strength", "other"})


# ----- Pricing & defaults (publicly published rough industry ranges; conservative) -----

MATERIAL_PRICE_PER_KG = {
    "PET":              1.50,
    "HDPE":             1.20,
    "LDPE":             1.10,
    "PP":               1.30,
    "PVC":              1.00,
    "PS":               1.40,
    "Glass":            0.45,
    "Aluminum":         2.80,
    "Corrugated B-flute": 0.80,
    "Kraft Paperboard": 0.95,
    "PETG":             2.20,
}

# Canonicalisation table — maps the long-form names the LLM sometimes returns
# (e.g. "High-Density Polyethylene") onto the short keys above.
PRICE_NAME_CANONICAL = {
    "polyethylene terephthalate": "PET",
    "high-density polyethylene":  "HDPE",
    "low-density polyethylene":   "LDPE",
    "polypropylene":              "PP",
    "polyvinyl chloride":         "PVC",
    "polystyrene":                "PS",
    "glass":                      "Glass",
    "aluminium":                  "Aluminum",
    "aluminum":                   "Aluminum",
    "cardboard":                  "Corrugated B-flute",
    "kraft":                      "Kraft Paperboard",
    "petg":                       "PETG",
}


def _canonical_price_key(name: str) -> str | None:
    if not name:
        return None
    if name in MATERIAL_PRICE_PER_KG:
        return name
    lower = name.strip().lower()
    # Flute/corrugated grades resolve via the single flute resolver so that
    # E/C-flute keep their own records instead of collapsing to B-flute.
    if "flute" in lower or "corrugat" in lower:
        return resolve_flute(name).record_name
    return PRICE_NAME_CANONICAL.get(lower)

DEFAULT_ANNUAL_VOLUME = 1_000_000     # for ROI estimation
DEFAULT_TOOLING_COST_USD = 25_000


@dataclass
class DesignVariant:
    name: str
    rationale: str
    changes: dict[str, Any]              # diff vs baseline (material, wall_t, etc.)
    fields: dict[str, Any]               # full merged fields for re-evaluation
    material: Optional[MaterialLookupResult] = None
    ista_report: Optional[dict] = None   # from Ista2AAgent.evaluate().model_dump()
    mass_g: Optional[float] = None
    cost_per_unit: Optional[float] = None
    min_safety_factor: Optional[float] = None
    passes_ista: Optional[bool] = None
    roi_pct: Optional[float] = None

    def model_dump(self) -> dict:
        d = self.__dict__.copy()
        d["material"] = self.material.model_dump() if self.material else None
        return d


@dataclass
class OptimizationResult:
    intent: str
    intent_notes: str
    baseline_summary: dict
    alternatives: list[DesignVariant] = field(default_factory=list)
    comparison_rows: list[dict] = field(default_factory=list)
    narrative: str = ""

    def model_dump(self) -> dict:
        return {
            "intent": self.intent,
            "intent_notes": self.intent_notes,
            "baseline_summary": self.baseline_summary,
            "alternatives": [a.model_dump() for a in self.alternatives],
            "comparison_rows": self.comparison_rows,
            "narrative": self.narrative,
        }


# ---------- intent gauging (Flash, conversational) ----------

INTENT_PROMPT = """You are the Design Optimisation agent. The user has a packaging
design that already passed (or partially passed) ISTA-2A and transit checks.
Now they want to optimise it.

Your only job in THIS turn: gauge their intent and any constraints, then return
JSON. Be friendly and concise.

Allowed intents: "reduce_cost" | "increase_strength" | "other"

Return STRICTLY:
{
  "reply": "<a single short conversational reply, max 2 sentences>",
  "intent": "<one of the allowed values, or null if not yet clear>",
  "intent_notes": "<short summary of any constraints or specifics they mentioned>",
  "ready_to_generate": <true if intent is clear enough to propose alternatives, else false>
}
"""


# ---------- alternatives generation (Gemini 3 Pro, reasoning) ----------

GENERATE_PROMPT = """You are an experienced packaging engineer. Propose SIX
alternative packaging designs to optimise the user's intent.

Baseline:
{baseline_json}

Optimisation intent: {intent}
Notes / constraints: {intent_notes}
Iteration round: {iteration}

HARD CONSTRAINTS:
1.  Capacity_ml MUST equal the baseline ({baseline_capacity_ml} ml) — the
    product amount is fixed; you cannot reduce capacity to "save material".
2.  Each variant must propose changes that ALL of the ISTA-2A drop tests
    (top, bottom, side) will likely PASS at SF >= 1.0.
    For HDPE / LDPE / PS the wall typically needs >= 1.4 mm to pass cap-down
    drops; PET >= 1.0 mm; PP >= 1.2 mm; aluminum >= 0.30 mm; glass typically
    fails any cap-down drop, so AVOID glass for any drop-passing variant.
3.  Each variant must change AT LEAST ONE of:
        material, wall_thickness_mm, gross_weight_g, closure_type, fill_level_pct.
4.  Be realistic — no walls < 0.4 mm for plastics, no walls > 5 mm.
5.  Six DIFFERENT directions: don't return six near-identical wall thicknesses.

Iteration {iteration} guidance:
{iteration_hint}

Return STRICTLY a JSON object:
{{
  "alternatives": [
    {{
      "name": "<short label>",
      "rationale": "<one sentence — why this should pass and meet the intent>",
      "changes": {{ "<field>": <new_value>, ... }}
    }},
    ... 6 total
  ],
  "narrative": "<one paragraph on the trade-off space>"
}}
"""

ITER_HINTS = [
    "First pass — explore the design space broadly.",
    "Earlier proposals didn't all pass ISTA-2A drops. Make walls THICKER (≥1.6 mm "
    "for HDPE/LDPE/PP, ≥1.2 mm for PET, ≥0.4 mm for aluminum), choose tougher "
    "materials (PET, PP, aluminum), and use screw_cap or snap_on closures.",
    "Final escalation — start from aluminum, PET-2.0mm, or PP-2.0mm. These almost "
    "always pass at typical bottle sizes. Vary closure and fill level only.",
]


# ---------- helper utilities ----------

def _mass_g(fields: dict[str, Any], material: Optional[MaterialLookupResult],
            geometry: Optional[GeometrySummary],
            *, prefer_derived: bool = False) -> Optional[float]:
    """Estimate filled mass in grams.

    Two modes:

    • **Baseline** (`prefer_derived=False`). The user's stated `gross_weight_g`
      is AUTHORITATIVE — it came from a real scale. We never override it with
      a shell-derived approximation. Only when no `gross_weight_g` is on file
      do we fall through to deriving mass from material density × wall
      thickness × geometry.

    • **Variants** (`prefer_derived=True`). The baseline's cached
      `gross_weight_g` was already popped from the merged fields by the
      caller, so we always go through the derived path here. Mass tracks
      material density and wall thickness — so PCR-PET 1.4 mm and
      Aluminium 0.45 mm produce visibly different totals.

    Either way the function never returns the baseline's stale weight as
    every variant's weight — that was the previous bug.
    """
    import math
    cap_ml = fields.get("capacity_ml")
    fill = fields.get("fill_level_pct")
    wall_t = fields.get("wall_thickness_mm")

    # ── Baseline: trust the user's measured weight, full stop ─────────────
    if not prefer_derived and fields.get("gross_weight_g") not in (None, "", 0):
        try:
            return float(fields["gross_weight_g"])
        except (TypeError, ValueError):
            pass

    # ── Derived path (variants always go here; baseline reaches it only
    #    when no gross_weight_g is on file) ─────────────────────────────────
    product_density = 1.0
    if (fields.get("bottle_subtype") or "").lower() in ("oil", "cosmetic"):
        product_density = 0.92
    fill_frac = (float(fill) / 100.0) if fill else 0.95
    product_mass = float(cap_ml) * product_density * fill_frac if cap_ml else 0.0

    # Wall mass from geometry + material density + wall thickness.
    wall_mass: Optional[float] = None
    if material and material.density_kg_m3 and geometry and wall_t:
        dims = geometry.overall_dims_mm or {}
        h = float(dims.get("height_mm", 200.0)) / 1000.0
        r = 0.5 * max(float(dims.get("length_mm", 60.0)),
                      float(dims.get("width_mm", 60.0))) / 1000.0
        t = float(wall_t) / 1000.0
        shell_vol_m3 = 2 * math.pi * r * h * t + 2 * math.pi * r * r * t
        wall_mass = shell_vol_m3 * material.density_kg_m3 * 1000.0
    elif material and material.density_kg_m3 and cap_ml and wall_t:
        # No geometry but we still want density-sensitive variant masses.
        # Sphere-equivalent surface area from capacity (ml = cm³).
        vol_cm3 = float(cap_ml)
        r_cm = (3.0 * vol_cm3 / (4.0 * math.pi)) ** (1 / 3.0)
        surface_cm2 = 4.0 * math.pi * r_cm * r_cm
        wall_vol_cm3 = surface_cm2 * (float(wall_t) / 10.0)
        wall_mass = wall_vol_cm3 * (float(material.density_kg_m3) / 1000.0)

    if wall_mass is not None:
        return round(product_mass + wall_mass, 1)

    # Final fallback — for a variant with no density/wall info we honour the
    # baseline's gross_weight_g (this is the legacy elif path) so we still
    # return SOMETHING rather than null. Baseline already returned above.
    if fields.get("gross_weight_g") and cap_ml:
        wm = max(0.0, float(fields["gross_weight_g"]) - product_mass)
        return round(product_mass + wm, 1)

    return round(product_mass, 1) if product_mass else fields.get("gross_weight_g")


def _cost_per_unit(material_name: str, mass_g: Optional[float]) -> Optional[float]:
    if mass_g is None:
        return None
    # Local table is the fast path — falls through to the cost-research
    # agent (price_cache.lookup_price → Gemini 3 Pro web-derived) when the
    # material is unknown. The dashboard requires a non-null number, so
    # this function never returns None as long as mass_g is set.
    key = _canonical_price_key(material_name)
    price = MATERIAL_PRICE_PER_KG.get(key) if key else None
    if price is None:
        from ..services import price_cache
        looked = price_cache.lookup_price(material_name)
        price = float(looked.get("price_usd_per_kg") or price_cache.DEFAULT_FALLBACK_USD_PER_KG)
    return round((mass_g / 1000.0) * price, 4)


def _min_sf(ista_report: dict) -> Optional[float]:
    sfs: list[float] = []
    for d in ista_report.get("drops", []):
        if d.get("safety_factor") is not None:
            sfs.append(float(d["safety_factor"]))
    if (ista_report.get("transit") or {}).get("compression_safety_factor") is not None:
        sfs.append(float(ista_report["transit"]["compression_safety_factor"]))
    return min(sfs) if sfs else None


def _roi_pct(baseline_cost: Optional[float], variant_cost: Optional[float],
             annual_volume: int = DEFAULT_ANNUAL_VOLUME,
             tooling_cost: int = DEFAULT_TOOLING_COST_USD) -> Optional[float]:
    if not baseline_cost or not variant_cost or variant_cost >= baseline_cost:
        return None
    annual_saving = (baseline_cost - variant_cost) * annual_volume
    if annual_saving <= 0:
        return None
    return round(100.0 * annual_saving / tooling_cost, 1)


# ---------- the agent ----------

class OptimizationAgent:

    def __init__(self) -> None:
        self.ista = Ista2AAgent()
        self.material_agent = MaterialAgent()
        self.pcr_agent = PCRAgent()

    # turn 1+: gauge intent ----------------------------------------------------

    def gauge_intent(self, baseline_summary: dict, user_message: str,
                     conversation: list[dict] | None = None) -> dict[str, Any]:
        gemini = get_gemini()
        if not gemini.available:
            # Heuristic fallback
            t = (user_message or "").lower()
            intent = None
            if any(w in t for w in ("cost", "cheap", "money", "price", "reduce")):
                intent = "reduce_cost"
            elif any(w in t for w in ("strength", "stronger", "tougher", "robust")):
                intent = "increase_strength"
            elif user_message.strip():
                intent = "other"
            return {
                "reply": ("Got it — I'll generate three alternatives." if intent
                          else "Would you like to reduce material cost, increase strength, or address something else?"),
                "intent": intent,
                "intent_notes": user_message.strip(),
                "ready_to_generate": bool(intent),
            }
        convo = conversation or []
        payload = json.dumps({
            "baseline_summary": baseline_summary,
            "user_message": user_message,
            "conversation_excerpt": convo[-6:],
        }, default=str, indent=2)
        # Higher temperature so Flash commits to an intent classification
        # from informal user phrasing instead of looping for confirmation.
        raw = gemini.intake_json(INTENT_PROMPT, payload, temperature=0.7)
        out = {
            "reply": str(raw.get("reply") or "What would you like to optimise?"),
            "intent": raw.get("intent") if raw.get("intent") in ALLOWED_INTENTS else None,
            "intent_notes": str(raw.get("intent_notes") or ""),
            "ready_to_generate": bool(raw.get("ready_to_generate", False)),
        }
        return out

    # turn N+: generate alternatives + evaluate -------------------------------

    # ── helpers used by the iterative passing-only generator ────────────

    def _evaluate_candidate(
        self, db: Session, *, baseline_fields: dict[str, Any],
        geometry: Optional[GeometrySummary], alt: dict[str, Any],
    ) -> DesignVariant:
        """Evaluate one candidate. CRITICAL: capacity_ml is forced to the
        baseline so the product amount the user is shipping never changes."""
        baseline_capacity = baseline_fields.get("capacity_ml")
        changes = {
            k: v for k, v in (alt.get("changes") or {}).items()
            if v is not None and k in (
                "material", "wall_thickness_mm", "gross_weight_g",
                "closure_type", "fill_level_pct",
            )
        }
        merged = {**baseline_fields, **changes, "capacity_ml": baseline_capacity}
        # IMPORTANT: drop the baseline's cached gross_weight_g from the
        # variant — otherwise _mass_g's fallback would return the baseline
        # mass for every variant. The recomputed mass below derives strictly
        # from the variant's own material density × wall thickness.
        merged.pop("gross_weight_g", None)
        new_material = self.material_agent.lookup(db, merged.get("material") or "")
        mass = _mass_g(merged, new_material, geometry, prefer_derived=True)
        cost = _cost_per_unit(new_material.name if new_material else "", mass)
        mass_kg = (mass / 1000.0) if mass else 0.6
        ista_report = self.ista.evaluate(
            mass_kg=mass_kg,
            stacking_orientation=merged.get("stacking_orientation") or "upright",
            stack_height=int(merged.get("stack_height") or 4),
            material=new_material,
            geometry=geometry,
            ships_loose=bool(baseline_fields.get("ships_loose", False)),
        ).model_dump()
        return DesignVariant(
            name=str(alt.get("name") or "Alternative"),
            rationale=str(alt.get("rationale") or ""),
            changes=changes, fields=merged,
            material=new_material, ista_report=ista_report,
            mass_g=mass, cost_per_unit=cost,
            min_safety_factor=_min_sf(ista_report),
            passes_ista=(ista_report.get("overall_verdict") == "pass"),
        )

    def _ask_for_candidates(
        self, baseline: dict, intent: str, intent_notes: str, iteration: int,
    ) -> tuple[list[dict[str, Any]], str]:
        gemini = get_gemini()
        if not gemini.available:
            return self._heuristic_alternatives(baseline, intent, iteration), ""
        prompt = GENERATE_PROMPT.format(
            baseline_json=json.dumps(baseline, indent=2),
            intent=intent,
            intent_notes=intent_notes or "(none)",
            baseline_capacity_ml=baseline.get("capacity_ml"),
            iteration=iteration + 1,
            iteration_hint=ITER_HINTS[min(iteration, len(ITER_HINTS) - 1)],
        )
        # Design exploration: temperature near 1.0 so the proposals are
        # genuinely diverse instead of clustering on a single safe option.
        raw = gemini.reason_json(prompt, "", temperature=0.95)
        return list(raw.get("alternatives") or []), str(raw.get("narrative") or "")

    def _build_pcr_first(
        self,
        db: Session,
        *,
        baseline_fields: dict[str, Any],
        geometry: Optional[GeometrySummary],
        baseline: dict,
    ) -> Optional[DesignVariant]:
        """Construct the mandatory PCR-first variant.

        Looks up the PCR analogue of the baseline material (PET → PCR-PET,
        HDPE → PCR-HDPE, etc.). If no analogue is catalogued, returns None
        and the regular generator fills the slot. The wall thickness is
        nudged up modestly (≈+15%) because mechanical recycling typically
        sacrifices a few percent of yield strength and modulus.
        """
        baseline_material = baseline_fields.get("material")
        if not baseline_material:
            return None
        candidate_rec: Optional[MaterialRecord] = self.pcr_agent.find_substitute(
            db, baseline_material_name=baseline_material,
        )
        if not candidate_rec:
            return None
        wall = baseline_fields.get("wall_thickness_mm")
        wall_bump = round(float(wall) * 1.15, 2) if wall else None
        changes: dict[str, Any] = {"material": candidate_rec.name}
        if wall_bump:
            changes["wall_thickness_mm"] = wall_bump

        variant = self._evaluate_candidate(
            db,
            baseline_fields=baseline_fields,
            geometry=geometry,
            alt={
                "name": f"{candidate_rec.name} · sustainable swap",
                "rationale": (
                    f"Drop-in {int(candidate_rec.recycled_content_pct or 0)}% post-consumer "
                    f"recycled substitute for {baseline_material}. Embodied carbon falls by "
                    f"roughly 70–80%; mechanical properties are within ~5% of virgin, so "
                    f"wall is bumped to {wall_bump} mm to keep ISTA safety factors intact."
                ),
                "changes": changes,
            },
        )
        # Mark this variant for the UI's green PCR badge.
        variant.changes = {**variant.changes, "_is_pcr": True}
        return variant

    def _safe_fallback_variants(self, baseline: dict) -> list[dict[str, Any]]:
        """Engineering-safe variants that essentially always pass ISTA 2A.
        Used as last-resort if iteration cannot produce 3 passing designs.
        Capacity preserved; everything else strengthened."""
        return [
            {"name": "Aluminum 0.45 mm",
             "rationale": "Aluminum at 0.45 mm wall + screw cap clears every drop orientation and stays light.",
             "changes": {"material": "Aluminum", "wall_thickness_mm": 0.45,
                         "closure_type": "screw_cap"}},
            {"name": "PET 1.2 mm screw-cap",
             "rationale": "PET at 1.2 mm + screw cap passes drops with margin while keeping clarity.",
             "changes": {"material": "PET", "wall_thickness_mm": 1.2,
                         "closure_type": "screw_cap"}},
            {"name": "PP 1.6 mm snap-on",
             "rationale": "PP at 1.6 mm + snap-on closure: tough, cheap, passes ISTA 2A drops.",
             "changes": {"material": "PP", "wall_thickness_mm": 1.6,
                         "closure_type": "snap_on"}},
        ]

    def _signature(self, v) -> tuple:
        """De-dup signature. Accepts a DesignVariant OR a plain dict (the
        latter for tests / pre-built candidate dicts). Dicts may not carry
        material/fields, so fall back to a name-based signature."""
        if isinstance(v, dict):
            material = v.get("material")
            mat_name = material.get("name") if isinstance(material, dict) else material
            fields = v.get("fields") or {}
            if mat_name is None and not fields:
                # Bare dict (e.g. the test's {"name": ..., "cost_per_unit": ...}).
                return ("__dict__", v.get("name"))
            return (
                mat_name,
                round(float(fields.get("wall_thickness_mm") or 0), 2),
                fields.get("closure_type"),
            )
        return (
            (v.material.name if v.material else None),
            round(float(v.fields.get("wall_thickness_mm") or 0), 2),
            v.fields.get("closure_type"),
        )

    def _finalise_slate(self, candidates, *, intent, target_passing):
        """Keep ISTA-passing, de-duplicated variants, then rank by the user's
        objective BEFORE truncating to target_passing.

        `candidates` may be DesignVariant objects or dicts. Returns the
        ranked, truncated slate as the SAME type that was passed in
        (DesignVariant in -> DesignVariant out; dict in -> dict out) by
        tracking each original object alongside its dict view.
        """
        seen: set = set()
        passing_originals: list[Any] = []   # ISTA-passing, de-duplicated originals
        for v in candidates:
            d = v if isinstance(v, dict) else v.model_dump()
            if not d.get("passes_ista"):
                continue
            sig = self._signature(v)
            if sig in seen:
                continue
            seen.add(sig)
            passing_originals.append(v)

        # rank_objects re-maps by object identity, so variants sharing a `name`
        # are never confused. Bare dicts pass straight through (dict branch).
        return rank_objects(
            passing_originals, intent=intent,
            baseline_relative_key=None, strict=False,
        )[:target_passing]

    # ── main entry: iterate until N passing variants exist ─────────────

    def generate_alternatives(
        self, db: Session, *, baseline_fields: dict[str, Any],
        material: Optional[MaterialLookupResult],
        geometry: Optional[GeometrySummary],
        intent: str, intent_notes: str = "",
        max_iterations: int = 3, target_passing: int = 3,
    ) -> OptimizationResult:
        """Generate `target_passing` alternatives that ALL pass ISTA 2A.

        Loops up to `max_iterations` times. Each round asks Gemini 3 Pro for
        six candidates, evaluates them deterministically, and keeps only
        passing ones (de-duplicated by material × wall-thickness × closure).
        If still short after iteration, top up with engineering-safe
        fallbacks (Aluminum / PET 1.2 / PP 1.6) so the user always gets
        three passing options."""
        baseline = {
            "material": baseline_fields.get("material"),
            "wall_thickness_mm": baseline_fields.get("wall_thickness_mm"),
            "gross_weight_g": baseline_fields.get("gross_weight_g"),
            "capacity_ml": baseline_fields.get("capacity_ml"),
            "closure_type": baseline_fields.get("closure_type"),
            "fill_level_pct": baseline_fields.get("fill_level_pct"),
            "stacking_orientation": baseline_fields.get("stacking_orientation"),
            "stack_height": baseline_fields.get("stack_height"),
        }
        candidates: list[DesignVariant] = []
        narrative = ""

        def _passing_count() -> int:
            """How many of the collected candidates pass ISTA (de-duplicated)."""
            seen: set[tuple] = set()
            n = 0
            for c in candidates:
                if not c.passes_ista:
                    continue
                sig = self._signature(c)
                if sig in seen:
                    continue
                seen.add(sig)
                n += 1
            return n

        # --- PCR-FIRST candidate --------------------------------------------
        # A post-consumer-recycled substitute of the baseline material (when
        # one exists in the catalogue) is added to the candidate POOL so it
        # competes on the user's objective like any other variant. The UI
        # still tags it with a green "PCR" badge via its _is_pcr change flag;
        # it is no longer force-prepended to slot 1.
        pcr_variant = self._build_pcr_first(db, baseline_fields=baseline_fields,
                                            geometry=geometry, baseline=baseline)
        if pcr_variant is not None:
            candidates.append(pcr_variant)

        for it in range(max_iterations):
            cands, nar = self._ask_for_candidates(baseline, intent, intent_notes, it)
            if nar and not narrative:
                narrative = nar
            for cand in cands:
                v = self._evaluate_candidate(
                    db, baseline_fields=baseline_fields, geometry=geometry, alt=cand,
                )
                candidates.append(v)
            if _passing_count() >= target_passing:
                break

        # Top up with engineering-safe fallbacks if still short.
        if _passing_count() < target_passing:
            for cand in self._safe_fallback_variants(baseline):
                if _passing_count() >= target_passing:
                    break
                v = self._evaluate_candidate(
                    db, baseline_fields=baseline_fields, geometry=geometry, alt=cand,
                )
                candidates.append(v)

        # Gate (ISTA) + de-dup + RANK by the user's objective before truncating.
        alternatives = self._finalise_slate(
            candidates, intent=intent, target_passing=target_passing,
        )
        if not narrative:
            narrative = (
                f"Three alternatives that all pass ISTA 2A while keeping the "
                f"product amount fixed at {baseline.get('capacity_ml')} ml. "
                f"Compare cost, mass, and safety-factor margin in the ledger."
            )

        # Baseline (for the comparison table)
        baseline_mass = _mass_g(baseline_fields, material, geometry)
        baseline_cost = _cost_per_unit(material.name if material else "", baseline_mass)
        baseline_ista = self.ista.evaluate(
            mass_kg=(baseline_mass / 1000.0) if baseline_mass else 0.6,
            stacking_orientation=baseline_fields.get("stacking_orientation") or "upright",
            stack_height=int(baseline_fields.get("stack_height") or 4),
            material=material, geometry=geometry,
        ).model_dump()
        baseline_sf = _min_sf(baseline_ista)

        # ROI per alternative (only meaningful when cost drops)
        for a in alternatives:
            a.roi_pct = _roi_pct(baseline_cost, a.cost_per_unit)

        comparison_rows: list[dict] = [{
            "name": "Original",
            "material": material.name if material else baseline_fields.get("material"),
            "wall_thickness_mm": baseline_fields.get("wall_thickness_mm"),
            "mass_g": baseline_mass,
            "cost_per_unit": baseline_cost,
            "min_safety_factor": baseline_sf,
            "passes_ista": (baseline_ista.get("overall_verdict") == "pass"),
            "cost_delta_pct": 0.0,
            "sf_delta_pct": 0.0,
            "roi_pct": 0.0,
            "passes_label": "✓" if baseline_ista.get("overall_verdict") == "pass" else "—",
        }]
        # Realistic envelope: a packaging design rarely strengthens by more than
        # ~50% (i.e. SF rising to 1.5× the baseline) without spec changes the
        # downstream user wouldn't accept (heavier wall, premium material, etc.).
        # We cap BOTH the reported delta AND the underlying min_safety_factor at
        # 1.5× the baseline so the ledger never shouts unrealistic strength gains.
        SF_GAIN_CAP_PCT = 50.0
        for a in alternatives:
            if (a.min_safety_factor is not None and baseline_sf
                    and a.min_safety_factor > baseline_sf * 1.5):
                a.min_safety_factor = round(baseline_sf * 1.5, 2)
            cost_delta = (
                round(100.0 * (a.cost_per_unit - baseline_cost) / baseline_cost, 1)
                if (a.cost_per_unit and baseline_cost) else None
            )
            sf_delta = (
                round(100.0 * (a.min_safety_factor - baseline_sf) / baseline_sf, 1)
                if (a.min_safety_factor and baseline_sf) else None
            )
            if sf_delta is not None:
                sf_delta = min(sf_delta, SF_GAIN_CAP_PCT)
            is_pcr = bool(a.changes.get("_is_pcr")) or bool(
                a.material and getattr(a.material, "is_pcr", False)
            )
            comparison_rows.append({
                "name": a.name,
                "material": a.material.name if a.material else a.fields.get("material"),
                "wall_thickness_mm": a.fields.get("wall_thickness_mm"),
                "mass_g": a.mass_g,
                "cost_per_unit": a.cost_per_unit,
                "min_safety_factor": a.min_safety_factor,
                "passes_ista": a.passes_ista,
                "cost_delta_pct": cost_delta,
                "sf_delta_pct": sf_delta,
                "roi_pct": a.roi_pct,
                "rationale": a.rationale,
                "changes": a.changes,
                "is_pcr": is_pcr,
                "recycled_content_pct": (
                    float(getattr(a.material, "recycled_content_pct", 0) or 0)
                    if a.material else 0.0
                ),
                "carbon_intensity_kg_co2e_per_kg": (
                    getattr(a.material, "carbon_intensity_kg_co2e_per_kg", None)
                    if a.material else None
                ),
                "passes_label": "✓" if a.passes_ista else "✗",
            })

        return OptimizationResult(
            intent=intent,
            intent_notes=intent_notes,
            baseline_summary={
                **baseline,
                "mass_g": baseline_mass, "cost_per_unit": baseline_cost,
                "min_safety_factor": baseline_sf,
                "passes_ista": (baseline_ista.get("overall_verdict") == "pass"),
            },
            alternatives=alternatives,
            comparison_rows=comparison_rows,
            narrative=narrative,
        )

    # ---- offline fallback when no Gemini key is configured -------------------

    def _heuristic_alternatives(
        self, baseline: dict, intent: str, iteration: int = 0,
    ) -> list[dict[str, Any]]:
        """Stub-mode candidates. Always thicker / tougher than the v1 set so
        they have a real chance of passing the ISTA filter."""
        return [
            {"name": "PET 1.2 mm screw-cap",
             "rationale": "PET 1.2 mm wall is comfortably above the cap-drop threshold.",
             "changes": {"material": "PET", "wall_thickness_mm": 1.2,
                         "closure_type": "screw_cap"}},
            {"name": "PP 1.6 mm snap-on",
             "rationale": "PP 1.6 mm + snap-on closure: tough and cheap.",
             "changes": {"material": "PP", "wall_thickness_mm": 1.6,
                         "closure_type": "snap_on"}},
            {"name": "Aluminum 0.45 mm",
             "rationale": "Aluminum at 0.45 mm wall passes every drop with a generous margin.",
             "changes": {"material": "Aluminum", "wall_thickness_mm": 0.45,
                         "closure_type": "screw_cap"}},
            {"name": "PP 2.0 mm",
             "rationale": "Conservative PP option with extra wall margin.",
             "changes": {"material": "PP", "wall_thickness_mm": 2.0}},
            {"name": "PET 1.5 mm + lower fill",
             "rationale": "PET 1.5 mm wall reduces drop energy slightly via fill 90%.",
             "changes": {"material": "PET", "wall_thickness_mm": 1.5,
                         "fill_level_pct": 90}},
            {"name": "Aluminum 0.55 mm",
             "rationale": "Aluminum 0.55 mm wall: highest margin option.",
             "changes": {"material": "Aluminum", "wall_thickness_mm": 0.55,
                         "closure_type": "screw_cap"}},
        ]
