"""Packet Optimization Agent.

Parallel to optimization.py (bottle optimizer) — same architectural pattern,
same orchestration style, same UI/results structure, but evaluates packet-
specific engineering variables instead of bottle-specific ones.

Flow mirrors bottle optimization exactly:
    1. gauge_intent (Gemini Flash) — classify user goal
    2. generate_alternatives (Gemini Flash) — propose 3 packet variants
    3. Deterministic heuristic scoring — seal, transit, barrier, puncture, cost
    4. Return comparison_rows mirroring the bottle ledger format

Variables optimised:
    - laminate_structure (e.g. PET/MetPET/LDPE, BOPP/MetBOPP/LDPE)
    - total_thickness_micron (50–120 μm range)
    - seal_type (center_back_seal, fin_seal, lap_seal, three_side, four_side)
    - secondary carton (none → mono_carton → corrugated_shipper → 5_ply)
    - transit assumptions (truck, rail, ship, mixed)

Scoring is heuristic and deterministic — no hallucinated physics, no FEA.
All scores are explainable from the lookup tables below.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm.gemini_client import get_gemini
from .objective_ranking import rank_objects


# ---------------------------------------------------------------------------
# Seal survivability score (0–10): higher = better seam integrity under load.
# Based on published flexible-packaging failure mode data — lap seal is the
# weakest common seam; four-side distributes peel force most evenly.
# ---------------------------------------------------------------------------
SEAL_SCORES: dict[str, float] = {
    "four_side":        8.5,
    "three_side":       8.0,
    "fin_seal":         7.5,
    "side_seal":        7.0,
    "center_back_seal": 6.0,
    "lap_seal":         5.5,
}

# ---------------------------------------------------------------------------
# Barrier: additive bonus per laminate token.
# MetPET/MetBOPP/Al provide metalised-layer WVTR/OTR improvement.
# EVOH is the best oxygen barrier; Nylon/PA adds moisture resistance.
# ---------------------------------------------------------------------------
_BARRIER_BONUS: dict[str, float] = {
    "al": 3.5, "foil": 3.5, "metpet": 3.0, "metbopp": 2.5,
    "evoh": 2.5, "nylon": 1.5, "pa": 1.5, "pet": 1.0,
    "cpp": 0.5, "bopp": 0.2, "ldpe": 0.0, "pe": 0.0,
    "lldpe": 0.0, "pp": 0.0,
}

# ---------------------------------------------------------------------------
# Puncture resistance: additive bonus per laminate token + thickness.
# Nylon/PA adds the most; LDPE/PE add the least.
# ---------------------------------------------------------------------------
_PUNCTURE_BONUS: dict[str, float] = {
    "nylon": 2.5, "pa": 2.5, "pet": 1.5, "metpet": 1.5,
    "bopp": 1.0, "metbopp": 1.2, "al": 1.0, "foil": 1.0,
    "ldpe": 0.3, "lldpe": 0.5, "pe": 0.3, "cpp": 0.4,
    "evoh": 0.8,
}

# ---------------------------------------------------------------------------
# Carton transit bonus: secondary packaging absorbs vibration and compression.
# ---------------------------------------------------------------------------
CARTON_TRANSIT_BONUS: dict[str, float] = {
    "none":               0.0,
    "no_carton":          0.0,
    "mono_carton":        0.5,
    "corrugated_shipper": 1.5,
    "3_ply":              1.0,
    "5_ply":              2.0,
    "duplex":             0.8,
    "display_carton":     0.3,
    "master_case":        1.5,
}

# Relative film-cost contribution of carton (fraction of total unit cost).
CARTON_COST_INDEX: dict[str, float] = {
    "none": 0.00, "no_carton": 0.00, "mono_carton": 0.03,
    "corrugated_shipper": 0.06, "3_ply": 0.05, "5_ply": 0.10,
    "duplex": 0.04, "display_carton": 0.04, "master_case": 0.07,
}


# ---------------------------------------------------------------------------
# Heuristic scoring helpers
# ---------------------------------------------------------------------------

def _seal_key(seal_type: str) -> str:
    return (seal_type or "").strip().lower().replace(" ", "_").replace("-", "_")


def _laminate_tokens(laminate: str) -> list[str]:
    """Split a slash-separated laminate string into normalised tokens."""
    if not laminate:
        return []
    return [
        t.strip().lower().replace("-", "").replace(" ", "")
        for t in laminate.split("/")
    ]


def _score_seal(seal_type: str) -> float:
    return SEAL_SCORES.get(_seal_key(seal_type), 5.0)


def _score_barrier(laminate: str) -> float:
    tokens = _laminate_tokens(laminate)
    score = 3.0  # base
    for tok in tokens:
        for key, bonus in _BARRIER_BONUS.items():
            if key in tok:
                score += bonus
                break
    return min(10.0, round(score, 1))


def _score_puncture(laminate: str, thickness_micron: Optional[float]) -> float:
    tokens = _laminate_tokens(laminate)
    score = 2.0  # base
    for tok in tokens:
        for key, bonus in _PUNCTURE_BONUS.items():
            if key in tok:
                score += bonus
                break
    t = float(thickness_micron or 75)
    score += (t - 60) / 30.0  # +1 per 30 μm above 60
    return min(10.0, round(score, 1))


def _score_transit(
    seal_type: str,
    thickness_micron: Optional[float],
    carton: str,
    transit_modes: list[str],
) -> float:
    t = float(thickness_micron or 75)
    base = 4.0 + (t - 60) / 20.0           # 4.0 at 60 μm → 5.75 at 95 μm
    seal_contrib = _score_seal(seal_type) * 0.3
    ck = (carton or "none").lower().replace(" ", "_").replace("-", "_")
    carton_bonus = CARTON_TRANSIT_BONUS.get(ck, 0.5)
    modes = [m.lower() for m in (transit_modes or [])]
    harsh_penalty = -0.5 if any(m in modes for m in ("ship", "rail")) else 0.0
    return min(10.0, round(base + seal_contrib + carton_bonus + harsh_penalty, 1))


def _cost_impact_pct(
    baseline_laminate: str, baseline_thickness: float, baseline_carton: str,
    new_laminate: str, new_thickness: float, new_carton: str,
) -> float:
    """Estimated cost change % vs baseline (heuristic, not exact).

    Film cost ≈ 70% of unit cost; carton cost ≈ 30%.
    Film cost scales linearly with thickness and laminate complexity
    (number of layers + metalised-layer premium).
    """
    def _lam_complexity(lam: str) -> float:
        if not lam:
            return 1.5  # assume a generic 2-layer if unknown
        layers = [l.strip() for l in lam.split("/") if l.strip()]
        n = len(layers)
        has_metal = any(
            any(m in l.lower() for m in ("met", "al", "foil", "evoh"))
            for l in layers
        )
        return n * 0.6 + (1.2 if has_metal else 0.0)

    bt = max(float(baseline_thickness), 1.0)
    nt = max(float(new_thickness), 1.0)
    thickness_ratio = nt / bt

    blc = _lam_complexity(baseline_laminate or "")
    nlc = _lam_complexity(new_laminate or "")
    lam_ratio = nlc / max(blc, 0.1)

    film_impact = (thickness_ratio * lam_ratio - 1.0) * 70.0  # film is ~70%

    bc_key = (baseline_carton or "none").lower().replace(" ", "_").replace("-", "_")
    nc_key = (new_carton or "none").lower().replace(" ", "_").replace("-", "_")
    carton_delta = CARTON_COST_INDEX.get(nc_key, 0.0) - CARTON_COST_INDEX.get(bc_key, 0.0)
    carton_impact = carton_delta * 100.0

    return round(film_impact + carton_impact, 1)


def _carton_from_fields(fields: dict[str, Any]) -> str:
    """Extract a normalised carton string from packet case_summary fields."""
    if fields.get("has_secondary_carton") == "no":
        return "none"
    if fields.get("carton_type"):
        return fields["carton_type"]
    grade = (fields.get("carton_board_grade") or "").lower()
    if "5" in grade:
        return "5_ply"
    if "3" in grade:
        return "3_ply"
    if fields.get("has_secondary_carton") == "yes":
        return "corrugated_shipper"
    return "corrugated_shipper"  # safe default for packets


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PACKET_INTENT_PROMPT = """You are the Packet Design Optimisation agent for a
flexible packaging engineering platform.

The user has finished the intake flow and wants to optimise their packet design.
Your only job in this turn: gauge their intent and return JSON. Be friendly and concise.

Allowed intents:
  "reduce_cost"          — reduce laminate/carton cost
  "improve_survivability" — improve transit durability and seal integrity
  "improve_shelf_life"   — improve barrier properties for longer shelf life
  "other"                — something else the user specifies

Return STRICTLY:
{
  "reply": "<a single short conversational reply, max 2 sentences>",
  "intent": "<one of the allowed values, or null if not yet clear>",
  "intent_notes": "<short summary of any constraints they mentioned>",
  "ready_to_generate": <true if intent is clear, else false>
}
"""

PACKET_GENERATE_PROMPT = """You are an experienced flexible packaging engineer.
Propose THREE alternative packet designs to meet the user's optimisation intent.

HARD CONSTRAINTS:
1. Keep product_category and fill_weight_g unchanged — only change packaging.
2. laminate_structure must be a real slash-separated structure
   (e.g. PET/MetPET/LDPE, BOPP/MetBOPP/LDPE, Nylon/LDPE, PET/EVOH/PE).
3. total_thickness_micron must be between 50 and 120.
4. seal_type must be one of:
   center_back_seal | fin_seal | lap_seal | side_seal | three_side | four_side
5. carton must be one of:
   none | mono_carton | corrugated_shipper | 3_ply | 5_ply | duplex | master_case
6. Each variant must differ from the baseline in at least two parameters.
7. Keep variants FMCG-realistic — no exotic or laboratory-only structures.

Return STRICTLY a JSON object:
{
  "alternatives": [
    {
      "name": "<short label, e.g. 'MetPET upgrade · fin seal'>",
      "rationale": "<one sentence — the engineering trade-off>",
      "changes": {
        "laminate_structure": "<new laminate or null to keep baseline>",
        "total_thickness_micron": <number or null>,
        "seal_type": "<new seal type or null>",
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
class PacketDesignVariant:
    name: str
    rationale: str
    changes: dict[str, Any]
    fields: dict[str, Any]           # merged baseline + changes
    seal_score: Optional[float] = None
    transit_score: Optional[float] = None
    barrier_score: Optional[float] = None
    puncture_score: Optional[float] = None
    cost_impact_pct: Optional[float] = None

    def model_dump(self) -> dict:
        return self.__dict__.copy()


@dataclass
class PacketOptimizationResult:
    intent: str
    intent_notes: str
    baseline_summary: dict
    alternatives: list[PacketDesignVariant] = field(default_factory=list)
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

class PacketOptimizationAgent:
    """Packet-specific optimisation agent.

    Architecturally identical to OptimizationAgent (bottle) — same two-phase
    flow (intent gauge → generate_alternatives), same comparison_rows output
    format — but evaluates packet engineering variables instead.
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
            elif any(w in t for w in ("transit", "survive", "strong", "durabi", "seal")):
                intent = "improve_survivability"
            elif any(w in t for w in ("shelf", "barrier", "moisture", "oxygen", "life")):
                intent = "improve_shelf_life"
            elif user_message.strip():
                intent = "other"
            return {
                "reply": (
                    "Got it — I'll generate three packet alternatives."
                    if intent else
                    "Would you like to reduce cost, improve transit survivability, "
                    "improve shelf life, or something else?"
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
        raw = gemini.intake_json(PACKET_INTENT_PROMPT, payload, temperature=0.7)
        _ALLOWED = {"reduce_cost", "improve_survivability", "improve_shelf_life", "other"}
        return {
            "reply": str(raw.get("reply") or "What would you like to optimise in this packet design?"),
            "intent": raw.get("intent") if raw.get("intent") in _ALLOWED else None,
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
        raw = gemini.intake_json(PACKET_GENERATE_PROMPT, payload, temperature=0.85)
        return list(raw.get("alternatives") or []), str(raw.get("narrative") or "")

    def _evaluate_variant(
        self, baseline: dict, spec: dict,
    ) -> PacketDesignVariant:
        changes = {
            k: v for k, v in (spec.get("changes") or {}).items()
            if v is not None and k in (
                "laminate_structure", "total_thickness_micron", "seal_type", "carton",
            )
        }
        merged = {**baseline, **changes}

        lam = merged.get("laminate_structure") or baseline.get("laminate_structure") or ""
        thickness = float(merged.get("total_thickness_micron") or baseline.get("total_thickness_micron") or 75)
        seal = merged.get("seal_type") or baseline.get("seal_type") or ""
        carton = merged.get("carton") or baseline.get("carton") or "none"
        modes = list(merged.get("transit_modes") or baseline.get("transit_modes") or [])

        return PacketDesignVariant(
            name=str(spec.get("name") or "Alternative"),
            rationale=str(spec.get("rationale") or ""),
            changes=changes,
            fields=merged,
            seal_score=_score_seal(seal),
            transit_score=_score_transit(seal, thickness, carton, modes),
            barrier_score=_score_barrier(lam),
            puncture_score=_score_puncture(lam, thickness),
        )

    def _fallback_variants(self, baseline: dict, intent: str) -> list[dict[str, Any]]:
        """Engineering-safe fallbacks used when Gemini is unavailable."""
        thickness = float(baseline.get("total_thickness_micron") or 75)
        if intent == "reduce_cost":
            return [
                {
                    "name": f"Thinner film {int(max(55, thickness - 15))} μm",
                    "rationale": "Reduce film thickness to cut material cost; transit performance slightly lower.",
                    "changes": {
                        "total_thickness_micron": max(55, thickness - 15),
                        "carton": "corrugated_shipper",
                    },
                },
                {
                    "name": "BOPP/PE · simplified laminate",
                    "rationale": "Two-layer BOPP/PE at current thickness; lower film cost, moderate barrier.",
                    "changes": {"laminate_structure": "BOPP/PE", "total_thickness_micron": thickness},
                },
                {
                    "name": "Three-side seal · no secondary carton",
                    "rationale": "Three-side seal reduces seam material; removing carton cuts system cost.",
                    "changes": {"seal_type": "three_side", "carton": "none"},
                },
            ]
        if intent == "improve_survivability":
            return [
                {
                    "name": f"Thicker film {int(min(110, thickness + 15))} μm · four-side seal",
                    "rationale": "Extra film thickness and four-side seal distribute transit stress evenly.",
                    "changes": {
                        "total_thickness_micron": min(110, thickness + 15),
                        "seal_type": "four_side",
                    },
                },
                {
                    "name": "PET/MetPET/LDPE · fin seal",
                    "rationale": "MetPET barrier with fin seal improves moisture resistance and seam integrity.",
                    "changes": {"laminate_structure": "PET/MetPET/LDPE", "seal_type": "fin_seal"},
                },
                {
                    "name": "Four-side seal · 5-ply carton",
                    "rationale": "Four-side seam and 5-ply corrugated carton maximise compression protection.",
                    "changes": {"seal_type": "four_side", "carton": "5_ply"},
                },
            ]
        if intent == "improve_shelf_life":
            return [
                {
                    "name": "PET/EVOH/PE · high-barrier",
                    "rationale": "EVOH layer provides best-in-class oxygen barrier for extended shelf life.",
                    "changes": {
                        "laminate_structure": "PET/EVOH/PE",
                        "total_thickness_micron": max(70, int(thickness)),
                    },
                },
                {
                    "name": "Nylon/LDPE · moisture-resistant",
                    "rationale": "Nylon barrier with fin seal — excellent moisture resistance for dry products.",
                    "changes": {
                        "laminate_structure": "Nylon/LDPE",
                        "seal_type": "fin_seal",
                        "total_thickness_micron": 80,
                    },
                },
                {
                    "name": "PET/Al/PE · premium barrier",
                    "rationale": "Aluminium foil provides maximum moisture and oxygen barrier for shelf life.",
                    "changes": {"laminate_structure": "PET/Al/PE", "total_thickness_micron": 90},
                },
            ]
        # "other"
        return [
            {
                "name": "PET/MetPET/LDPE at current thickness",
                "rationale": "MetPET improves barrier at the same thickness — cost-neutral performance upgrade.",
                "changes": {"laminate_structure": "PET/MetPET/LDPE"},
            },
            {
                "name": "Three-side seal · corrugated carton",
                "rationale": "Three-side seal with corrugated shipper gives better transit integrity at low cost.",
                "changes": {"seal_type": "three_side", "carton": "corrugated_shipper"},
            },
            {
                "name": "BOPP/MetBOPP/CPP · balanced",
                "rationale": "MetBOPP with CPP sealing layer balances moisture barrier, heat-seal, and cost.",
                "changes": {"laminate_structure": "BOPP/MetBOPP/CPP", "total_thickness_micron": 70},
            },
        ]

    # --------------------------------------------------- main entry point

    def generate_alternatives(
        self,
        *,
        baseline_fields: dict[str, Any],
        intent: str,
        intent_notes: str = "",
    ) -> PacketOptimizationResult:
        """Generate 3 packet alternatives with deterministic heuristic scoring.

        Mirrors OptimizationAgent.generate_alternatives() in structure but
        evaluates packet parameters (laminate, thickness, seal, carton) instead
        of bottle parameters (material, wall_thickness, ISTA 2A).
        """
        baseline = {
            "laminate_structure":   baseline_fields.get("laminate_structure"),
            "total_thickness_micron": baseline_fields.get("total_thickness_micron"),
            "seal_type":            baseline_fields.get("seal_type"),
            "carton":               _carton_from_fields(baseline_fields),
            "transit_modes":        list(baseline_fields.get("transit_modes") or []),
            "product_category":     baseline_fields.get("product_category"),
            "fill_weight_g":        baseline_fields.get("fill_weight_g"),
        }

        # Score the baseline
        b_lam      = baseline.get("laminate_structure") or ""
        b_thick    = float(baseline.get("total_thickness_micron") or 75)
        b_seal     = baseline.get("seal_type") or ""
        b_carton   = baseline.get("carton") or "none"
        b_modes    = list(baseline.get("transit_modes") or [])

        baseline_seal     = _score_seal(b_seal)
        baseline_transit  = _score_transit(b_seal, b_thick, b_carton, b_modes)
        baseline_barrier  = _score_barrier(b_lam)
        baseline_puncture = _score_puncture(b_lam, b_thick)

        # Generate and evaluate variants
        raw_variants, narrative = self._propose_variants(baseline, intent, intent_notes)
        alternatives: list[PacketDesignVariant] = []
        seen: set[tuple] = set()

        def _cost_for(v: PacketDesignVariant) -> float:
            """Cost impact vs baseline. Computed HERE (not only in the
            comparison loop below) so the objective ranker can see it before
            truncation."""
            a_lam    = v.fields.get("laminate_structure") or b_lam
            a_thick  = float(v.fields.get("total_thickness_micron") or b_thick)
            a_carton = v.fields.get("carton") or b_carton
            return _cost_impact_pct(b_lam, b_thick, b_carton, a_lam, a_thick, a_carton)

        for spec in raw_variants[:6]:   # process up to 6 LLM proposals
            v = self._evaluate_variant(baseline, spec)
            sig = (
                v.fields.get("laminate_structure"),
                v.fields.get("total_thickness_micron"),
                v.fields.get("seal_type"),
            )
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
            sig = (
                v.fields.get("laminate_structure"),
                v.fields.get("total_thickness_micron"),
                v.fields.get("seal_type"),
            )
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
                "Three packet alternatives generated — compare seal integrity, "
                "transit durability, and barrier performance in the ledger below."
            )

        # Build comparison rows (baseline first, then alternatives)
        comparison_rows: list[dict] = [{
            "name":              "Original",
            "laminate":          b_lam or "—",
            "thickness_micron":  b_thick,
            "seal_type":         b_seal or "—",
            "carton":            b_carton,
            "seal_score":        baseline_seal,
            "transit_score":     baseline_transit,
            "barrier_score":     baseline_barrier,
            "puncture_score":    baseline_puncture,
            "cost_impact_pct":   0.0,
            "rationale":         "Baseline",
            "is_baseline":       True,
        }]

        for a in alternatives:
            a_lam    = a.fields.get("laminate_structure") or b_lam
            a_thick  = float(a.fields.get("total_thickness_micron") or b_thick)
            a_carton = a.fields.get("carton") or b_carton
            # Read the value already set by _cost_for (used for ranking) so the
            # ranked-on value and displayed value can't diverge.
            cost_pct = a.cost_impact_pct
            comparison_rows.append({
                "name":             a.name,
                "laminate":         a_lam or "—",
                "thickness_micron": a_thick,
                "seal_type":        a.fields.get("seal_type") or b_seal or "—",
                "carton":           a_carton,
                "seal_score":       a.seal_score,
                "transit_score":    a.transit_score,
                "barrier_score":    a.barrier_score,
                "puncture_score":   a.puncture_score,
                "cost_impact_pct":  cost_pct,
                "rationale":        a.rationale,
                "changes":          a.changes,
                "is_baseline":      False,
            })

        return PacketOptimizationResult(
            intent=intent,
            intent_notes=intent_notes,
            baseline_summary={
                **baseline,
                "seal_score":     baseline_seal,
                "transit_score":  baseline_transit,
                "barrier_score":  baseline_barrier,
                "puncture_score": baseline_puncture,
            },
            alternatives=alternatives,
            comparison_rows=comparison_rows,
            narrative=narrative,
        )
