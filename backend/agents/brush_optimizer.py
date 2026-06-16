"""Brush Optimization Agent.

Parallel to PacketOptimizationAgent — same architectural pattern, same
orchestration style, same UI/results structure, but evaluates brush-specific
engineering variables instead of flexible-packet variables.

Flow mirrors packet optimization exactly:
    1. gauge_intent (Gemini Flash) — classify user goal
    2. generate_alternatives (Gemini Flash) — propose 3 brush packaging variants
    3. Deterministic heuristic scoring — blister integrity, transit, material, compression
    4. Return comparison_rows mirroring the packet ledger format

Variables optimised:
    - primary_pack_type (blister_pack, clamshell, backer_card, pouch, carton)
    - primary_pack_material (PET, RPET, PVC, Paperboard, Mixed)
    - carton (none, corrugated_carton, corrugated_shipper, 3_ply, 5_ply)
    - carton_board_grade (E-flute, B-flute, C-flute)

Scoring is heuristic and deterministic — no hallucinated physics, no FEA.
All scores are explainable from the lookup tables below.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm.gemini_client import get_gemini
from .objective_ranking import rank_objects


# Canonical set of optimisation intents this agent accepts. The route guard
# in routes/extras.py imports this so there is a single source of truth.
ALLOWED_INTENTS = frozenset(
    {"reduce_cost", "improve_survivability", "improve_sustainability", "other"}
)


# ---------------------------------------------------------------------------
# Blister integrity score (0–10): higher = better product protection.
# Based on pack format — rigid shells protect better than soft pouches.
# ---------------------------------------------------------------------------
BLISTER_SCORES: dict[str, float] = {
    "blister_pack":   9.0,   # rigid PET thermoform — industry gold standard for brushes
    "clamshell":      8.5,   # hinged rigid shell — excellent protection
    "carton":         7.5,   # folded paperboard carton — good compression resistance
    "backer_card":    6.5,   # paperboard + partial clamshell / skin pack
    "pouch":          5.0,   # flexible pouch — least rigid, highest puncture risk
    "other":          6.0,
}

# ---------------------------------------------------------------------------
# Primary pack material suitability (0–10).
# Based on clarity, recyclability, barrier, and transit durability.
# ---------------------------------------------------------------------------
MATERIAL_SCORES: dict[str, float] = {
    "pet":        8.5,   # clear, strong, recyclable
    "rpet":       8.0,   # recycled PET — sustainability benefit, slightly lower clarity
    "paperboard": 7.5,   # sustainable, good compression, moisture-sensitive
    "pvc":        6.5,   # good clarity but difficult to recycle
    "mixed":      5.5,   # variable performance; avoid where compliance matters
}

# ---------------------------------------------------------------------------
# Carton transit bonus: secondary packaging absorbs vibration + compression.
# ---------------------------------------------------------------------------
CARTON_TRANSIT_BONUS: dict[str, float] = {
    "none":               0.0,
    "no_carton":          0.0,
    "corrugated_carton":  1.0,
    "tray":               0.5,
    "shrink_bundle":      0.3,
    "rigid_box":          0.8,
    "corrugated_shipper": 1.5,
    "3_ply":              1.0,
    "5_ply":              2.0,
}

# Relative carton cost index (fraction of total unit cost).
CARTON_COST_INDEX: dict[str, float] = {
    "none": 0.00, "no_carton": 0.00, "corrugated_carton": 0.04,
    "tray": 0.03, "shrink_bundle": 0.02, "rigid_box": 0.06,
    "corrugated_shipper": 0.06, "3_ply": 0.05, "5_ply": 0.10,
}

# ---------------------------------------------------------------------------
# Heuristic scoring helpers
# ---------------------------------------------------------------------------

def _score_blister(pack_type: str) -> float:
    return BLISTER_SCORES.get((pack_type or "other").strip().lower(), 6.0)


def _score_material(pack_material: str) -> float:
    if not pack_material:
        return 6.0
    key = pack_material.strip().lower()
    # Try exact match first, then substring.
    if key in MATERIAL_SCORES:
        return MATERIAL_SCORES[key]
    for k, v in MATERIAL_SCORES.items():
        if k in key:
            return v
    return 6.0


def _score_transit(
    pack_type: str,
    carton: str,
    transit_modes: list[str],
) -> float:
    base = _score_blister(pack_type) * 0.5
    ck = (carton or "none").lower().replace(" ", "_").replace("-", "_")
    carton_bonus = CARTON_TRANSIT_BONUS.get(ck, 0.5)
    modes = [m.lower() for m in (transit_modes or [])]
    harsh_penalty = -0.5 if any(m in modes for m in ("ship", "rail")) else 0.0
    return min(10.0, round(base + carton_bonus + harsh_penalty + 3.0, 1))


def _score_compression(carton: str, board_grade: str) -> float:
    ck = (carton or "none").lower().replace(" ", "_").replace("-", "_")
    base = {
        "none": 3.0, "no_carton": 3.0, "corrugated_carton": 6.0,
        "tray": 5.0, "shrink_bundle": 4.0, "rigid_box": 7.0,
        "corrugated_shipper": 7.5, "3_ply": 6.5, "5_ply": 8.5,
    }.get(ck, 5.0)
    # Board grade bonus
    grade = (board_grade or "").lower()
    if "5" in grade:
        base = min(10.0, base + 1.0)
    elif "b_flute" in grade or "b flute" in grade or "b-flute" in grade:
        base = min(10.0, base + 0.5)
    elif "c_flute" in grade or "c flute" in grade or "c-flute" in grade:
        base = min(10.0, base + 0.3)
    return round(base, 1)


def _cost_impact_pct(
    baseline_pack: str, baseline_material: str, baseline_carton: str,
    new_pack: str, new_material: str, new_carton: str,
) -> float:
    """Estimated cost change % vs baseline (heuristic, not exact).

    Primary pack cost ≈ 60% of unit cost; carton cost ≈ 40%.
    """
    _PACK_COST = {
        "blister_pack": 1.0, "clamshell": 0.95, "backer_card": 0.55,
        "pouch": 0.40, "carton": 0.60, "other": 0.70,
    }
    _MAT_COST = {
        "pet": 1.0, "rpet": 1.05, "paperboard": 0.70,
        "pvc": 0.85, "mixed": 0.90,
    }

    def _pack_cost(p: str) -> float:
        return _PACK_COST.get((p or "other").lower(), 0.70)

    def _mat_cost(m: str) -> float:
        key = (m or "pet").strip().lower()
        return _MAT_COST.get(key, 1.0)

    b_primary = _pack_cost(baseline_pack) * _mat_cost(baseline_material)
    n_primary = _pack_cost(new_pack) * _mat_cost(new_material)
    primary_delta = (n_primary / max(b_primary, 0.01) - 1.0) * 60.0

    bc_key = (baseline_carton or "none").lower().replace(" ", "_").replace("-", "_")
    nc_key = (new_carton or "none").lower().replace(" ", "_").replace("-", "_")
    carton_delta = (CARTON_COST_INDEX.get(nc_key, 0.0) - CARTON_COST_INDEX.get(bc_key, 0.0)) * 100.0

    return round(primary_delta + carton_delta, 1)


def _carton_from_fields(fields: dict[str, Any]) -> str:
    if fields.get("has_secondary_carton") == "no":
        return "none"
    if fields.get("carton_type"):
        return fields["carton_type"]
    if fields.get("has_secondary_carton") == "yes":
        return "corrugated_carton"
    return "none"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

BRUSH_INTENT_PROMPT = """You are the Brush Packaging Optimisation agent for a CPG packaging engineering platform.

The user has finished the intake flow and wants to optimise their brush packaging design.
Your only job in this turn: gauge their intent and return JSON. Be friendly and concise.

Allowed intents:
  "reduce_cost"           — reduce primary pack or carton cost
  "improve_survivability" — improve transit durability and blister integrity
  "improve_sustainability" — improve recyclability or reduce plastic use
  "other"                 — something else the user specifies

Return STRICTLY:
{
  "reply": "<a single short conversational reply, max 2 sentences>",
  "intent": "<one of the allowed values, or null if not yet clear>",
  "intent_notes": "<short summary of any constraints they mentioned>",
  "ready_to_generate": <true if intent is clear, else false>
}
"""

BRUSH_GENERATE_PROMPT = """You are an experienced brush packaging engineer.
Propose THREE alternative brush packaging designs to meet the user's optimisation intent.

HARD CONSTRAINTS:
1. Keep brush_type and brush_weight_g unchanged — only change packaging.
2. primary_pack_type must be one of:
   blister_pack | clamshell | backer_card | pouch | carton | other
3. primary_pack_material must be a real material:
   PET | RPET | PVC | Paperboard | Mixed
4. carton must be one of:
   none | corrugated_carton | tray | shrink_bundle | rigid_box | corrugated_shipper | 3_ply | 5_ply
5. Each variant must differ from the baseline in at least two parameters.
6. Keep variants FMCG-realistic.

Return STRICTLY a JSON object:
{
  "alternatives": [
    {
      "name": "<short label, e.g. 'RPET blister · 5-ply carton'>",
      "rationale": "<one sentence — the engineering trade-off>",
      "changes": {
        "primary_pack_type": "<new pack type or null to keep baseline>",
        "primary_pack_material": "<new material or null>",
        "carton": "<new carton or null>"
      }
    },
    ... 3 total
  ],
  "narrative": "<one paragraph on the trade-off space for this intent>"
}
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BrushDesignVariant:
    name: str
    rationale: str
    changes: dict[str, Any]
    fields: dict[str, Any]
    blister_score: Optional[float] = None
    transit_score: Optional[float] = None
    material_score: Optional[float] = None
    compression_score: Optional[float] = None
    cost_impact_pct: Optional[float] = None

    def model_dump(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BrushOptimizationResult:
    intent: str
    intent_notes: str
    baseline_summary: dict
    alternatives: list[BrushDesignVariant] = field(default_factory=list)
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


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class BrushOptimizationAgent:
    """Brush-specific optimisation agent.

    Architecturally identical to PacketOptimizationAgent — same two-phase
    flow (intent gauge → generate_alternatives), same comparison_rows output
    format — but evaluates brush packaging parameters instead.
    """

    # ------------------------------------------------------------------ intent

    def gauge_intent(
        self,
        baseline_summary: dict,
        user_message: str,
        conversation: list[dict] | None = None,
    ) -> dict[str, Any]:
        gemini = get_gemini()
        if not gemini.available:
            t = (user_message or "").lower()
            intent = None
            if any(w in t for w in ("cost", "cheap", "save", "reduce", "price")):
                intent = "reduce_cost"
            elif any(w in t for w in ("transit", "survive", "strong", "durabi", "blister", "protect")):
                intent = "improve_survivability"
            elif any(w in t for w in ("sustain", "recycl", "eco", "green", "plastic")):
                intent = "improve_sustainability"
            elif user_message.strip():
                intent = "other"
            return {
                "reply": (
                    "Got it — I'll generate three brush packaging alternatives."
                    if intent else
                    "Would you like to reduce cost, improve transit survivability, "
                    "improve sustainability, or something else?"
                ),
                "intent": intent,
                "intent_notes": user_message.strip(),
                "ready_to_generate": bool(intent),
            }

        payload = json.dumps({
            "baseline_summary": baseline_summary,
            "user_message": user_message,
            "conversation_excerpt": (conversation or [])[-6:],
        }, default=str, indent=2)
        raw = gemini.intake_json(BRUSH_INTENT_PROMPT, payload, temperature=0.7)
        return {
            "reply": str(raw.get("reply") or "What would you like to optimise in this brush packaging?"),
            "intent": raw.get("intent") if raw.get("intent") in ALLOWED_INTENTS else None,
            "intent_notes": str(raw.get("intent_notes") or ""),
            "ready_to_generate": bool(raw.get("ready_to_generate", False)),
        }

    # ------------------------------------------------------- alternatives

    def _propose_variants(
        self, baseline: dict, intent: str, intent_notes: str,
    ) -> tuple[list[dict[str, Any]], str]:
        gemini = get_gemini()
        if not gemini.available:
            return self._fallback_variants(baseline, intent), ""

        payload = json.dumps({
            "baseline": baseline,
            "intent": intent,
            "intent_notes": intent_notes or "(none)",
        }, indent=2, default=str)
        raw = gemini.intake_json(BRUSH_GENERATE_PROMPT, payload, temperature=0.85)
        return list(raw.get("alternatives") or []), str(raw.get("narrative") or "")

    def _evaluate_variant(self, baseline: dict, spec: dict) -> BrushDesignVariant:
        changes = {
            k: v for k, v in (spec.get("changes") or {}).items()
            if v is not None and k in ("primary_pack_type", "primary_pack_material", "carton")
        }
        merged = {**baseline, **changes}

        pack_type  = merged.get("primary_pack_type") or baseline.get("primary_pack_type") or "blister_pack"
        material   = merged.get("primary_pack_material") or baseline.get("primary_pack_material") or "PET"
        carton     = merged.get("carton") or baseline.get("carton") or "none"
        board      = merged.get("carton_board_grade") or baseline.get("carton_board_grade") or ""
        modes      = list(merged.get("transit_modes") or baseline.get("transit_modes") or [])

        return BrushDesignVariant(
            name=str(spec.get("name") or "Alternative"),
            rationale=str(spec.get("rationale") or ""),
            changes=changes,
            fields=merged,
            blister_score=_score_blister(pack_type),
            transit_score=_score_transit(pack_type, carton, modes),
            material_score=_score_material(material),
            compression_score=_score_compression(carton, board),
        )

    def _fallback_variants(self, baseline: dict, intent: str) -> list[dict[str, Any]]:
        """Engineering-safe fallbacks when Gemini is unavailable."""
        pack = baseline.get("primary_pack_type") or "blister_pack"
        if intent == "reduce_cost":
            return [
                {
                    "name": "Backer card · corrugated carton",
                    "rationale": "Backer card uses less material than a full blister; corrugated carton adds system protection.",
                    "changes": {"primary_pack_type": "backer_card", "carton": "corrugated_carton"},
                },
                {
                    "name": "Paperboard carton · no secondary",
                    "rationale": "Folded paperboard carton reduces plastic use and secondary packaging cost.",
                    "changes": {"primary_pack_type": "carton", "primary_pack_material": "Paperboard", "carton": "none"},
                },
                {
                    "name": "RPET blister · tray",
                    "rationale": "Recycled PET blister reduces resin cost; tray provides basic secondary protection.",
                    "changes": {"primary_pack_material": "RPET", "carton": "tray"},
                },
            ]
        if intent == "improve_survivability":
            return [
                {
                    "name": "PET clamshell · 5-ply corrugated",
                    "rationale": "Rigid clamshell and 5-ply corrugated deliver maximum transit protection.",
                    "changes": {"primary_pack_type": "clamshell", "carton": "5_ply"},
                },
                {
                    "name": "PET blister · corrugated shipper",
                    "rationale": "Standard blister with corrugated shipper balances integrity and cost.",
                    "changes": {"primary_pack_type": "blister_pack", "primary_pack_material": "PET", "carton": "corrugated_shipper"},
                },
                {
                    "name": "Rigid box · PET blister",
                    "rationale": "Rigid box around a PET blister maximises corner crush resistance.",
                    "changes": {"primary_pack_type": "blister_pack", "carton": "rigid_box"},
                },
            ]
        if intent == "improve_sustainability":
            return [
                {
                    "name": "Paperboard carton · RPET blister",
                    "rationale": "Paperboard outer and RPET primary reduce virgin plastic content significantly.",
                    "changes": {"primary_pack_type": "backer_card", "primary_pack_material": "RPET", "carton": "corrugated_carton"},
                },
                {
                    "name": "RPET clamshell · corrugated carton",
                    "rationale": "100% RPET clamshell with mono-material recyclable corrugated secondary.",
                    "changes": {"primary_pack_type": "clamshell", "primary_pack_material": "RPET", "carton": "corrugated_carton"},
                },
                {
                    "name": "Paperboard only · no secondary",
                    "rationale": "Fully paperboard pack eliminates plastic; suitable for retail-friendly display.",
                    "changes": {"primary_pack_type": "carton", "primary_pack_material": "Paperboard", "carton": "none"},
                },
            ]
        # "other"
        return [
            {
                "name": "PET blister · corrugated carton",
                "rationale": "Standard PET blister with corrugated secondary — balanced performance baseline.",
                "changes": {"primary_pack_type": "blister_pack", "primary_pack_material": "PET", "carton": "corrugated_carton"},
            },
            {
                "name": "RPET clamshell · 3-ply",
                "rationale": "RPET clamshell with 3-ply secondary — improved sustainability at moderate cost increase.",
                "changes": {"primary_pack_type": "clamshell", "primary_pack_material": "RPET", "carton": "3_ply"},
            },
            {
                "name": "Backer card · 5-ply corrugated",
                "rationale": "Lightweight backer with heavy secondary — cost-effective for high-stack transit.",
                "changes": {"primary_pack_type": "backer_card", "carton": "5_ply"},
            },
        ]

    # --------------------------------------------------- main entry point

    def generate_alternatives(
        self,
        *,
        baseline_fields: dict[str, Any],
        intent: str,
        intent_notes: str = "",
    ) -> BrushOptimizationResult:
        """Generate 3 brush packaging alternatives with deterministic heuristic scoring."""
        baseline = {
            "primary_pack_type":     baseline_fields.get("primary_pack_type") or "blister_pack",
            "primary_pack_material": baseline_fields.get("primary_pack_material") or "PET",
            "carton":                _carton_from_fields(baseline_fields),
            "carton_board_grade":    baseline_fields.get("carton_board_grade") or "",
            "transit_modes":         list(baseline_fields.get("transit_modes") or []),
            "brush_type":            baseline_fields.get("brush_type"),
            "brush_weight_g":        baseline_fields.get("brush_weight_g"),
        }

        b_pack   = baseline["primary_pack_type"]
        b_mat    = baseline["primary_pack_material"]
        b_carton = baseline["carton"]
        b_board  = baseline["carton_board_grade"]
        b_modes  = baseline["transit_modes"]

        baseline_blister     = _score_blister(b_pack)
        baseline_transit     = _score_transit(b_pack, b_carton, b_modes)
        baseline_material    = _score_material(b_mat)
        baseline_compression = _score_compression(b_carton, b_board)

        raw_variants, narrative = self._propose_variants(baseline, intent, intent_notes)
        alternatives: list[BrushDesignVariant] = []
        seen: set[tuple] = set()

        def _cost_for(v: BrushDesignVariant) -> float:
            """Cost impact vs baseline. Computed HERE (not only in the
            comparison loop below) so the objective ranker can see it before
            truncation."""
            a_pack   = v.fields.get("primary_pack_type") or b_pack
            a_mat    = v.fields.get("primary_pack_material") or b_mat
            a_carton = v.fields.get("carton") or b_carton
            return _cost_impact_pct(b_pack, b_mat, b_carton, a_pack, a_mat, a_carton)

        for spec in raw_variants[:6]:
            v = self._evaluate_variant(baseline, spec)
            sig = (v.fields.get("primary_pack_type"), v.fields.get("primary_pack_material"), v.fields.get("carton"))
            if sig in seen:
                continue
            seen.add(sig)
            v.cost_impact_pct = _cost_for(v)
            alternatives.append(v)

        # Top up with fallbacks (do NOT pre-truncate to 3 — rank first).
        for spec in self._fallback_variants(baseline, intent):
            if len(alternatives) >= 6:
                break
            v = self._evaluate_variant(baseline, spec)
            sig = (v.fields.get("primary_pack_type"), v.fields.get("primary_pack_material"), v.fields.get("carton"))
            if sig in seen:
                continue
            seen.add(sig)
            v.cost_impact_pct = _cost_for(v)
            alternatives.append(v)

        # Rank by the user's objective BEFORE truncating to 3. rank_objects
        # re-maps by object identity, so variants sharing a `name` (the LLM's
        # default "Alternative" label) are never confused / dropped.
        alternatives = rank_objects(
            alternatives, intent=intent,
            baseline_relative_key="cost_impact_pct",
            strict=(intent == "reduce_cost"),
        )[:3]

        if not narrative:
            narrative = (
                "Three brush packaging alternatives generated — compare blister integrity, "
                "transit durability, material sustainability, and compression resistance in the ledger below."
            )

        # Build comparison rows (baseline first, then alternatives)
        comparison_rows: list[dict] = [{
            "name":             "Original",
            "primary_pack":     b_pack,
            "material":         b_mat,
            "carton":           b_carton,
            "blister_score":    baseline_blister,
            "transit_score":    baseline_transit,
            "material_score":   baseline_material,
            "compression_score": baseline_compression,
            "cost_impact_pct":  0.0,
            "rationale":        "Baseline",
            "is_baseline":      True,
        }]

        for a in alternatives:
            a_pack   = a.fields.get("primary_pack_type") or b_pack
            a_mat    = a.fields.get("primary_pack_material") or b_mat
            a_carton = a.fields.get("carton") or b_carton
            # Read the value already set by _cost_for (used for ranking) so the
            # ranked-on value and displayed value can't diverge.
            cost_pct = a.cost_impact_pct
            comparison_rows.append({
                "name":              a.name,
                "primary_pack":      a_pack,
                "material":          a_mat,
                "carton":            a_carton,
                "blister_score":     a.blister_score,
                "transit_score":     a.transit_score,
                "material_score":    a.material_score,
                "compression_score": a.compression_score,
                "cost_impact_pct":   cost_pct,
                "rationale":         a.rationale,
                "changes":           a.changes,
                "is_baseline":       False,
            })

        return BrushOptimizationResult(
            intent=intent,
            intent_notes=intent_notes,
            baseline_summary={
                **baseline,
                "blister_score":     baseline_blister,
                "transit_score":     baseline_transit,
                "material_score":    baseline_material,
                "compression_score": baseline_compression,
            },
            alternatives=alternatives,
            comparison_rows=comparison_rows,
            narrative=narrative,
        )
