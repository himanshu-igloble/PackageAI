"""Bottle Flow Agent — natural conversation, structured outcome.

Architecture directive: "the question for the bottle flow should not look
sequential — make the conversation flowing and natural."

Implementation:

- We still own a *fixed list* of bottle fields (the schema), but the user
  experience is conversational. On every user turn we:
    1. Run Gemini 2.5 Flash with a NATURAL, multi-field extraction prompt
       — it absorbs every relevant field the user mentioned, not just the
       one we last asked about.
    2. Validate / coerce each extracted value against the schema.
    3. Decide what to ask next by picking the MOST important still-missing
       field and weaving it into a friendly conversational reply that also
       acknowledges what we just learned.

Fixed list of fields (twelve, plus optional empty_weight_g and stack_height):
    bottle_subtype, capacity_ml, product_type, fill_level_pct, material,
    wall_thickness_mm, gross_weight_g, closure_type, transit_modes,
    road_condition (only when truck in modes), has_geometry, objective,
    stacking_orientation (defaults to 'upright'), stack_height (defaults to 4).

The orchestrator routes here whenever packaging_type is 'bottle' / 'bottle_like'
and any required bottle field is still missing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..llm.gemini_client import get_gemini
from ..schemas import IntakeFields, IntakeResponse  # noqa: F401


# Field meta: (field, hint, allowed-values, importance 1..10)
FIELD_META: dict[str, dict[str, Any]] = {
    "bottle_subtype":       {"options": ["water", "soda", "oil", "medicine", "cosmetic", "beer", "juice", "milk", "other"], "hint": "subtype shapes ISTA test profile and material defaults", "importance": 8},
    "capacity_ml":          {"options": None, "hint": "nominal capacity in millilitres", "importance": 9},
    "product_type":         {"options": ["liquid", "viscous", "powder", "pressurized", "fragile"], "hint": "product behavior under shock and vibration", "importance": 9},
    "fill_level_pct":       {"options": None, "hint": "fill % at shipping; affects gross weight and slosh", "importance": 7},
    "material":             {"options": ["PET", "HDPE", "LDPE", "PP", "PVC", "PS", "Glass", "Aluminum", "other"], "hint": "looked up in verified material DB", "importance": 10},
    "wall_thickness_mm":    {"options": None, "hint": "drives thin-wall buckling check", "importance": 7},
    "gross_weight_g":       {"options": None, "hint": "filled mass — drop energy + stack load", "importance": 9},
    "closure_type":         {"options": ["screw_cap", "sports_cap", "pump", "cork", "crown", "snap_on", "flip_top", "dropper"], "hint": "leak risk and handling impact", "importance": 5},
    "transit_modes":        {"options": ["truck", "rail", "ship", "air", "manual_handling"], "hint": "any modes the package will see", "importance": 9},
    "road_condition":       {"options": ["smooth_highway", "mixed", "rough_secondary", "off_road"], "hint": "applies when truck is in the mix", "importance": 6},
    "has_geometry":         {"options": ["yes", "no"], "hint": "STEP/STP/STL/GLB available for upload", "importance": 100},
    "objective":            {"options": ["concept_check", "transit_survivability", "ista_planning", "geometry_risk"], "hint": "controls which agents run and what the report emphasises", "importance": 9},
    "stacking_orientation":   {"options": ["upright", "on_side", "inverted"], "hint": "how bottles are stacked in transit; affects compression path", "importance": 5},
    "stack_height":           {"options": None, "hint": "number of bottles stacked vertically (default 4)", "importance": 4},

    # Secondary carton — conditional on has_secondary_carton == "yes"
    "has_secondary_carton":   {"options": ["yes", "no"], "hint": "whether the product ships inside a secondary carton or corrugated box", "importance": 4},
    "carton_type":            {"options": ["corrugated_carton", "tray", "shrink_bundle", "rigid_box", "corrugated_shipper"], "hint": "secondary carton format determines compression load path", "importance": 3},
    "carton_board_grade":     {"options": None, "hint": "e.g. 3-ply, 5-ply, E-flute, B-flute, C-flute", "importance": 3},
    "carton_pack_count":      {"options": None, "hint": "number of primary packs in each carton", "importance": 3},
    "carton_stacking_config": {"options": ["single_pallet_stack", "double_stacked", "warehouse_stacking", "container_stacking"], "hint": "stacking arrangement during storage or transit", "importance": 3},
}


# Required for plan readiness. road_condition and stack_height are conditionally required.
REQUIRED_FIELDS = [
    "bottle_subtype", "capacity_ml", "product_type", "fill_level_pct",
    "material", "wall_thickness_mm", "gross_weight_g", "closure_type",
    "transit_modes", "has_geometry", "objective",
    "has_secondary_carton",
]

# Required only when has_secondary_carton == "yes".
_CARTON_REQUIRED = ["carton_type", "carton_board_grade", "carton_pack_count", "carton_stacking_config"]


# Defaults that the agent will apply silently if the user never mentions them.
SILENT_DEFAULTS = {
    "stacking_orientation": "upright",
    "stack_height": 4,
}


NATURAL_EXTRACT_PROMPT = """You are the BottleFlow conversational agent for a CPG packaging copilot.

Your job has two parts:

1. EXTRACT: From the user's most recent message (and the conversation so far),
   pull any bottle-flow fields they mention. The user may mention several at
   once. Only extract fields that are clearly stated; do NOT invent.

   The fields you may extract (and their allowed values, when constrained):

   - bottle_subtype: water | soda | oil | medicine | cosmetic | beer | juice | milk | other
   - capacity_ml: number (ml)
   - product_type: liquid | viscous | powder | pressurized | fragile
   - fill_level_pct: number 0..100
   - material: short string (PET, HDPE, glass, etc.)
   - wall_thickness_mm: number
   - gross_weight_g: number (grams)
   - closure_type: screw_cap | sports_cap | pump | cork | crown | snap_on | flip_top | dropper
   - transit_modes: subset of [truck, rail, ship, air, manual_handling]
   - road_condition: smooth_highway | mixed | rough_secondary | off_road
   - has_geometry: NEVER extract this field. It is set automatically only
     when the user actually uploads a STEP/STL file via the upload route.
     Do not infer or set it in either direction from chat text.
   - objective: concept_check | transit_survivability | ista_planning | geometry_risk
   - stacking_orientation: upright | on_side | inverted
   - stack_height: integer
   - has_secondary_carton: yes | no  (whether the product ships inside a secondary carton)
   - carton_type: corrugated_carton | tray | shrink_bundle | rigid_box | corrugated_shipper
     (only extract when has_secondary_carton is "yes")
   - carton_board_grade: string (e.g. "3-ply", "5-ply", "E-flute", "B-flute", "C-flute")
     (only extract when has_secondary_carton is "yes")
   - carton_pack_count: integer  (primary packs per carton)
     (only extract when has_secondary_carton is "yes")
   - carton_stacking_config: single_pallet_stack | double_stacked | warehouse_stacking | container_stacking
     (only extract when has_secondary_carton is "yes")

2. CONVERSE: Write ONE friendly reply that:
   - acknowledges what you just learned (one short clause),
   - asks for the MOST important still-missing field next (one question),
   - feels like a conversation, not a form. Avoid listing steps or progress
     numbers. Avoid "Step X of Y". Don't repeat questions verbatim — paraphrase.

You will receive `missing_ranked` — the still-missing fields in priority order.
Pick the first one. If a field has allowed options, mention them as choices
in the reply but do not dump them as a bullet list.

Return STRICTLY a JSON object:
{
  "extracted": { "<field>": <value>, ... },   // only fields you confidently extracted
  "reply": "<your single friendly reply>",
  "asks_field": "<the field name you're asking about, or null if none>"
}
"""


# ----------------------------- helpers -----------------------------

def _coerce(field: str, raw: Any) -> Any:
    if raw is None:
        return None
    if field in ("capacity_ml", "gross_weight_g", "empty_weight_g", "fill_level_pct", "wall_thickness_mm"):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if field in ("stack_height", "carton_pack_count"):
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError):
            return None
    if field == "has_geometry":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("y", "yes", "true", "1", "available", "uploaded")
    if field == "transit_modes":
        if isinstance(raw, list):
            return sorted({str(x).strip().lower().replace(" ", "_") for x in raw if x})
        return sorted({s.strip().lower().replace(" ", "_") for s in str(raw).split(",") if s.strip()})
    return str(raw).strip()


def _is_filled(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, list) and not value:
        return False
    return True


def _missing_ranked(fields: dict[str, Any]) -> list[str]:
    """Return the still-missing required fields, ordered by importance (highest first).

    has_geometry is special: the field only counts as satisfied when it is
    *exactly* True — i.e. the user actually uploaded a STEP/STL/GLB and the
    /upload route flipped the flag. False or None both mean we still need
    the file. The bot must never accept "skip / no / later" for this slot;
    the analysis is meaningless without real geometry.

    Carton sub-fields are required only when has_secondary_carton == "yes".
    """
    missing = []
    for f in REQUIRED_FIELDS:
        if f == "has_geometry":
            if fields.get("has_geometry") is not True:
                missing.append(f)
        elif not _is_filled(fields.get(f)):
            missing.append(f)
    # road_condition is required only when truck is in transit modes
    modes = fields.get("transit_modes") or []
    if "truck" in modes and not _is_filled(fields.get("road_condition")):
        missing.append("road_condition")
    # secondary carton sub-fields required only when user said yes
    if str(fields.get("has_secondary_carton") or "").lower() == "yes":
        for f in _CARTON_REQUIRED:
            if not _is_filled(fields.get(f)):
                missing.append(f)
    return sorted(missing, key=lambda f: -FIELD_META.get(f, {}).get("importance", 0))


def _apply_silent_defaults(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    for k, v in SILENT_DEFAULTS.items():
        if not _is_filled(out.get(k)):
            out[k] = v
    return out


# ----------------------------- agent --------------------------------

@dataclass
class BottleTurn:
    """Result of one bottle-flow turn."""
    reply: str
    fields: dict[str, Any]
    asks_field: Optional[str]
    options: Optional[list[str]]
    missing: list[str]
    ready_for_plan: bool


class BottleFlowAgent:
    """Conversational, multi-field bottle intake."""

    @staticmethod
    def is_bottle(case_summary: dict[str, Any]) -> bool:
        return (case_summary or {}).get("packaging_type") in {"bottle", "bottle_like"}

    @staticmethod
    def missing_required(case_summary: dict[str, Any]) -> list[str]:
        return _missing_ranked(case_summary or {})

    # ---- one-shot helper: when the user JUST said it's a bottle and we
    # need to kick off the bottle conversation without absorbing anything yet
    def opener(self, fields: dict[str, Any]) -> BottleTurn:
        fields = _apply_silent_defaults(fields)
        missing = _missing_ranked(fields)
        if not missing:
            return BottleTurn(reply="Got everything I need — ready to propose a plan.",
                              fields=fields, asks_field=None, options=None,
                              missing=[], ready_for_plan=True)
        target = missing[0]
        meta = FIELD_META[target]
        # Phrase via Flash if available, otherwise a templated friendly question.
        gemini = get_gemini()
        question = self._template_question(target, fields)
        if gemini.available:
            payload = json.dumps({
                "known_fields": fields,
                "missing_ranked": missing[:6],
                "user_message": "",
                "conversation_excerpt": [],
            }, default=str)
            raw = gemini.intake_json(NATURAL_EXTRACT_PROMPT, payload, temperature=0.7)
            if isinstance(raw.get("reply"), str) and raw["reply"]:
                question = raw["reply"]
        return BottleTurn(reply=question, fields=fields, asks_field=target,
                          options=meta.get("options"), missing=missing, ready_for_plan=False)

    def step(self, fields: dict[str, Any], user_message: str,
             conversation: list[dict[str, str]] | None = None) -> BottleTurn:
        """Absorb the user's free-form reply, then phrase the next conversational ask."""
        fields = _apply_silent_defaults(fields)
        conv = conversation or []

        # ---------- 1. Try a cheap regex/match first (covers single-token replies) ----------
        cheap = self._cheap_extract(fields, user_message)
        for k, v in cheap.items():
            fields[k] = v

        # ---------- 2. Always run the natural-extract LLM pass; it may pick up more ----------
        missing = _missing_ranked(fields)
        gemini = get_gemini()
        reply = None
        if gemini.available:
            payload = json.dumps({
                "known_fields": fields,
                "missing_ranked": missing,
                "user_message": user_message,
                "conversation_excerpt": conv[-6:],
            }, default=str)
            raw = gemini.intake_json(NATURAL_EXTRACT_PROMPT, payload, temperature=0.7)
            extracted = raw.get("extracted") if isinstance(raw, dict) else None
            if isinstance(extracted, dict):
                for k, v in extracted.items():
                    if k in FIELD_META and v is not None:
                        coerced = _coerce(k, v)
                        # has_geometry can only be set TRUE, and only by the
                        # upload route — never by an LLM hallucination of
                        # what the user "intended" in chat.
                        if k == "has_geometry":
                            continue
                        if _is_filled(coerced):
                            fields[k] = coerced
            if isinstance(raw.get("reply"), str) and raw["reply"]:
                reply = raw["reply"]

        # ---------- 3. Recompute missing AFTER both extraction passes ----------
        missing = _missing_ranked(fields)
        if not missing:
            return BottleTurn(
                reply=(reply or "Got it — I have everything I need now. Review the proposed plan on the right and approve to run the analysis."),
                fields=fields,
                asks_field=None,
                options=None,
                missing=[],
                ready_for_plan=True,
            )

        target = missing[0]
        meta = FIELD_META[target]
        if not reply:
            reply = self._template_question(target, fields)
        return BottleTurn(
            reply=reply,
            fields=fields,
            asks_field=target,
            options=meta.get("options"),
            missing=missing,
            ready_for_plan=False,
        )

    # ---- internals -------------------------------------------------------

    def _template_question(self, field: str, fields: dict[str, Any]) -> str:
        meta = FIELD_META[field]
        # Friendly, varied phrasings — used as fallback when Flash isn't available
        templates = {
            "bottle_subtype":       "What is this bottle for — water, soda, oil, medicine, beer, juice, or something else?",
            "capacity_ml":          "And the size — what's the nominal capacity in millilitres?",
            "product_type":         "Got it. Is the product inside a liquid, viscous, powder, pressurised, or fragile?",
            "fill_level_pct":       "About how full does it ship — roughly what percentage?",
            "material":             "What's it made of? PET, HDPE, glass, PP, aluminum, or something else?",
            "wall_thickness_mm":    "Do you know the typical wall thickness in mm? (Skip if not — we'll flag it as estimated.)",
            "gross_weight_g":       "Approximately how much does a filled unit weigh, in grams?",
            "closure_type":         "What kind of closure does it use — screw cap, sports cap, pump, cork, crown, snap-on, flip-top, or dropper?",
            "transit_modes":        "Which transport modes will it actually see — truck, rail, ship, air, manual handling? You can list more than one.",
            "road_condition":       "Since trucks are involved, what road condition fits best — smooth highway, mixed, rough secondary, or off-road?",
            "has_geometry":         "I need your bottle's CAD geometry to run the analysis on the real shape. Please attach a STEP / STP / STL / GLB file using the paperclip icon below — this is required, the analysis can't proceed without it.",
            "objective":            "What are we trying to learn — a concept check, transit survivability, ISTA planning, or geometry risk?",
            "stacking_orientation":   "How are the bottles stacked in transit — upright, on their side, or inverted? (default: upright)",
            "stack_height":           "How many bottles tall is each transit stack (default 4)?",
            "has_secondary_carton":   "Will the product ship inside a secondary carton or corrugated box?",
            "carton_type":            "What type of secondary packaging — corrugated carton, tray, shrink bundle, rigid box, or corrugated shipper?",
            "carton_board_grade":     "What board construction is being used — 3-ply, 5-ply, E-flute, B-flute, or C-flute?",
            "carton_pack_count":      "How many primary packs go into each carton?",
            "carton_stacking_config": "How will these cartons be stacked during storage or transit — single pallet stack, double stacked, warehouse stacking, or container stacking?",
        }
        return templates.get(field, f"What is the {field.replace('_', ' ')}? — {meta.get('hint', '')}")

    def _cheap_extract(self, prior_fields: dict[str, Any], text: str) -> dict[str, Any]:
        """Cover the common single-token replies without an LLM call.

        Walks EVERY still-missing field (not just the first) so that a reply
        like "mixed" — which is unambiguous for road_condition but invalid
        for transit_modes — still gets routed to the right slot. Also
        handles common synonyms ("any", "all", "various") so the user is
        never trapped re-answering the same question.
        """
        out: dict[str, Any] = {}
        t = (text or "").strip().lower()
        if not t:
            return out
        missing = _missing_ranked(prior_fields)
        if not missing:
            return out
        norm = t.replace(" ", "_").replace("-", "_")

        # Pass 1 — exact-option match against any missing categorical field.
        # Order: walk missing in priority order; first match wins.
        # has_geometry is platform-managed, never settable from chat — skip.
        for target in missing:
            if target == "has_geometry":
                continue
            opts = FIELD_META[target].get("options")
            if not opts:
                continue
            low_opts = [o.lower() for o in opts]
            if norm in low_opts:
                out[target] = _coerce(target, opts[low_opts.index(norm)])
                return out

        target0 = missing[0]

        # Pass 2 — numeric reply for a numeric target.
        if target0 in ("capacity_ml", "gross_weight_g", "fill_level_pct",
                       "wall_thickness_mm", "stack_height", "carton_pack_count"):
            m = re.search(r"-?\d+(\.\d+)?", text)
            if m:
                out[target0] = _coerce(target0, m.group(0))
                return out

        # Pass 3 — transit_modes synonyms. The user will say "mixed", "all",
        # "any", "various", "everything", "multi", or describe a route in
        # plain English ("truck and ship") — we always extract SOMETHING so
        # the conversation never loops on this field.
        if target0 == "transit_modes":
            modes = []
            for word in ("truck", "rail", "ship", "air", "manual_handling", "manual"):
                if word in t:
                    modes.append("manual_handling" if word == "manual" else word)
            if not modes and any(w in t for w in (
                "mixed", "any", "all", "various", "everything", "multi", "multiple", "combo",
            )):
                modes = ["truck", "ship"]   # most common CPG mix
            if modes:
                out["transit_modes"] = sorted(set(modes))
                return out

        # Pass 4 — has_geometry can ONLY be set to True via an actual upload
        # (the /upload route flips the brief). Any chat reply here is
        # ignored: we never let the user shortcut the geometry gate.

        return out

    # ---- shim for legacy orchestrator callers --------------------------------
    def absorb_answer(self, case_summary: dict[str, Any], user_text: str) -> dict[str, Any]:
        turn = self.step(dict(case_summary or {}), user_text)
        return {
            "updated": turn.fields,
            "extracted_field": turn.asks_field,
            "value": None,
            "confidence": 0.9,
            "turn": turn,
        }

    def next_question(self, case_summary: dict[str, Any]) -> Optional[dict[str, Any]]:
        turn = self.opener(dict(case_summary or {}))
        if turn.asks_field is None:
            return None
        return {
            "field": turn.asks_field,
            "question": turn.reply,
            "options": turn.options,
            "hint": FIELD_META[turn.asks_field].get("hint", ""),
            "progress": {"step": len(REQUIRED_FIELDS) - len(turn.missing) + 1,
                         "total": len(REQUIRED_FIELDS)},
        }
