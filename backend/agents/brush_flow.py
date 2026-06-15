"""Brush Flow Agent — natural conversation, structured outcome.

Architecture mirrors PacketFlowAgent exactly. On every user turn:
  1. Cheap regex/match extraction — handles single-token replies without LLM.
  2. LLM natural-extract — absorbs all fields the user mentioned at once.
  3. Validate / coerce each extracted value against the schema.
  4. Ask the highest-priority still-missing field in a conversational reply.

Fixed fields (seven, plus optional carton sub-fields):
    brush_type, brush_weight_g, primary_pack_type, primary_pack_material,
    transit_modes, objective, has_secondary_carton.

Geometry upload is OPTIONAL — flow continues without it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..llm.gemini_client import get_gemini


# ---------------------------------------------------------------------------
# SECTION A — FIELD SCHEMA
# ---------------------------------------------------------------------------

FIELD_META: dict[str, dict[str, Any]] = {
    # A — Brush product
    "brush_type":             {"options": ["toothbrush", "electric_toothbrush_head", "cosmetic_brush", "industrial_brush", "other"],                                  "hint": "brush type determines primary packaging requirements and transit risk profile",  "importance": 100},
    "brush_weight_g":         {"options": None,                                                                                                                         "hint": "gross weight of one packaged unit in grams",                                      "importance": 95},
    "primary_pack_type":      {"options": ["blister_pack", "clamshell", "backer_card", "pouch", "carton", "other"],                                                    "hint": "primary pack format determines blister integrity and load path",                   "importance": 90},
    "primary_pack_material":  {"options": None,                                                                                                                         "hint": "material of primary pack (e.g. PET, RPET, PVC, Paperboard, Mixed)",              "importance": 85},

    # B — Transit
    "transit_modes":          {"options": ["truck", "rail", "ship", "air", "manual_handling"],                                                                         "hint": "all transit modes the shipment will see",                                         "importance": 88},
    "road_condition":         {"options": ["smooth_highway", "mixed", "rough_secondary", "off_road"],                                                                   "hint": "applies when truck is in transit mix",                                            "importance": 68},

    # C — Objective
    "objective":              {"options": ["transit_survivability", "ista_planning", "drop_resistance", "compression_risk", "geometry_risk"],                          "hint": "controls which analysis agents run and what the report emphasises",               "importance": 80},

    # D — Geometry (optional)
    "has_geometry":           {"options": ["yes", "no"],                                                                                                               "hint": "CAD, dieline, or GLB file for exact geometry extraction",                         "importance": 50},

    # E — Secondary carton (conditional on has_secondary_carton == "yes")
    "has_secondary_carton":   {"options": ["yes", "no"],                                                                                                               "hint": "whether the product ships inside a secondary carton or corrugated box",           "importance": 60},
    "carton_type":            {"options": ["corrugated_carton", "tray", "shrink_bundle", "rigid_box", "corrugated_shipper"],                                           "hint": "secondary carton format determines compression load path",                         "importance": 55},
    "carton_board_grade":     {"options": None,                                                                                                                         "hint": "e.g. 3-ply, 5-ply, E-flute, B-flute, C-flute",                                   "importance": 50},
    "carton_pack_count":      {"options": None,                                                                                                                         "hint": "number of primary packs in each carton",                                          "importance": 50},
    "carton_stacking_config": {"options": ["single_pallet_stack", "double_stacked", "warehouse_stacking", "container_stacking"],                                       "hint": "stacking arrangement during storage or transit",                                   "importance": 48},
}


# ---------------------------------------------------------------------------
# SECTION B — REQUIRED FIELDS
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "brush_type",
    "brush_weight_g",
    "primary_pack_type",
    "primary_pack_material",
    "transit_modes",
    "objective",
    "has_secondary_carton",
]

# Required only when has_secondary_carton == "yes".
_CARTON_REQUIRED = ["carton_type", "carton_board_grade", "carton_pack_count", "carton_stacking_config"]


# ---------------------------------------------------------------------------
# SECTION C — SILENT DEFAULTS
# ---------------------------------------------------------------------------

SILENT_DEFAULTS: dict[str, Any] = {
    "has_geometry": False,
}


# ---------------------------------------------------------------------------
# SECTION D — NATURAL EXTRACTION PROMPT
# ---------------------------------------------------------------------------

NATURAL_EXTRACT_PROMPT = """You are the BrushFlow conversational agent for a CPG packaging engineering platform.

Your job has two parts:

1. EXTRACT: From the user's most recent message (and conversation so far),
   pull any brush-flow fields they clearly and explicitly state.
   Only extract what is stated — do NOT invent, infer, or assume.

   Fields you may extract:
   - brush_type: toothbrush | electric_toothbrush_head | cosmetic_brush | industrial_brush | other
   - brush_weight_g: number (grams)
   - primary_pack_type: blister_pack | clamshell | backer_card | pouch | carton | other
   - primary_pack_material: string (e.g. "PET", "RPET", "PVC", "Paperboard", "Mixed")
   - transit_modes: subset of [truck, rail, ship, air, manual_handling]
   - road_condition: smooth_highway | mixed | rough_secondary | off_road
   - objective: transit_survivability | ista_planning | drop_resistance | compression_risk | geometry_risk
   - has_secondary_carton: yes | no
   - carton_type: corrugated_carton | tray | shrink_bundle | rigid_box | corrugated_shipper
     (only extract when has_secondary_carton is "yes")
   - carton_board_grade: string (e.g. "3-ply", "5-ply", "E-flute", "B-flute", "C-flute")
     (only extract when has_secondary_carton is "yes")
   - carton_pack_count: integer (primary packs per carton)
     (only extract when has_secondary_carton is "yes")
   - carton_stacking_config: single_pallet_stack | double_stacked | warehouse_stacking | container_stacking
     (only extract when has_secondary_carton is "yes")
   - has_geometry: NEVER extract from chat. Set only by the upload route.

2. CONVERSE: Write ONE friendly reply that:
   - Acknowledges only what the user explicitly said (one short clause).
   - Asks the MOST important still-missing field (one question).
   - Sounds like a natural engineering conversation — not a form, not a checklist.
   - Mentions allowed options conversationally where helpful.

You receive `missing_ranked` — the still-missing required fields in priority order.
Ask about the first one. Do not repeat exact phrasing from prior questions.

Return STRICTLY a JSON object:
{
  "extracted": { "<field>": <value>, ... },
  "reply": "<your single conversational reply>",
  "asks_field": "<field name you are asking about, or null>"
}
"""


# ---------------------------------------------------------------------------
# SECTION E — HELPERS
# ---------------------------------------------------------------------------

def _coerce(field: str, raw: Any) -> Any:
    if raw is None:
        return None
    if field in ("brush_weight_g",):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if field in ("carton_pack_count",):
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
    """Return still-missing required fields ordered by importance (highest first).

    Geometry is NOT required — flow continues without it.
    road_condition required only when truck is in transit_modes.
    Carton fields required only when has_secondary_carton is 'yes'.
    """
    missing: list[str] = []

    for f in REQUIRED_FIELDS:
        if not _is_filled(fields.get(f)):
            missing.append(f)

    if str(fields.get("has_secondary_carton") or "").lower() == "yes":
        for f in _CARTON_REQUIRED:
            if not _is_filled(fields.get(f)):
                missing.append(f)

    modes = fields.get("transit_modes") or []
    if "truck" in modes and not _is_filled(fields.get("road_condition")):
        missing.append("road_condition")

    return sorted(
        missing,
        key=lambda f: -(FIELD_META.get(f, {}).get("importance", 0)),
    )


def _apply_silent_defaults(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    for k, v in SILENT_DEFAULTS.items():
        if not _is_filled(out.get(k)):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# SECTION F — AGENT
# ---------------------------------------------------------------------------

@dataclass
class BrushTurn:
    """Result of one brush-flow conversation turn."""
    reply: str
    fields: dict[str, Any]
    asks_field: Optional[str]
    options: Optional[list[str]]
    missing: list[str]
    ready_for_plan: bool


class BrushFlowAgent:
    """Conversational, multi-field brush intake."""

    @staticmethod
    def is_brush(case_summary: dict[str, Any]) -> bool:
        return (case_summary or {}).get("packaging_family") == "brush"

    @staticmethod
    def missing_required(case_summary: dict[str, Any]) -> list[str]:
        return _missing_ranked(case_summary or {})

    # ------------------------------------------------------------------

    def opener(self, fields: dict[str, Any]) -> BrushTurn:
        """Initial turn — no user input to absorb; phrase the first question."""
        fields = _apply_silent_defaults(fields)
        missing = _missing_ranked(fields)
        if not missing:
            return BrushTurn(
                reply="Got everything I need — ready to propose a plan.",
                fields=fields, asks_field=None, options=None,
                missing=[], ready_for_plan=True,
            )

        target = missing[0]
        meta = FIELD_META[target]
        question = self._template_question(target, fields)
        gemini = get_gemini()
        if gemini.available:
            payload = json.dumps({
                "known_fields": {k: v for k, v in fields.items()
                                 if k not in ("geometry_upload_prompted",)},
                "missing_ranked": missing[:6],
                "user_message": "",
                "conversation_excerpt": [],
            }, default=str)
            raw = gemini.intake_json(NATURAL_EXTRACT_PROMPT, payload, temperature=0.7)
            if isinstance(raw.get("reply"), str) and raw["reply"]:
                question = raw["reply"]
        return BrushTurn(
            reply=question, fields=fields, asks_field=target,
            options=meta.get("options"), missing=missing, ready_for_plan=False,
        )

    def step(self, fields: dict[str, Any], user_message: str,
             conversation: list[dict[str, str]] | None = None) -> BrushTurn:
        """Absorb the user's free-form reply, then phrase the next conversational ask."""
        fields = _apply_silent_defaults(fields)
        conv = conversation or []

        # Pass 1 — cheap regex/match layer
        cheap = self._cheap_extract(fields, user_message)
        for k, v in cheap.items():
            fields[k] = v

        # Pass 2 — LLM natural-extract; may surface additional fields
        missing = _missing_ranked(fields)
        gemini = get_gemini()
        reply = None
        if gemini.available:
            payload = json.dumps({
                "known_fields": {k: v for k, v in fields.items()
                                 if k not in ("geometry_upload_prompted",)},
                "missing_ranked": missing,
                "user_message": user_message,
                "conversation_excerpt": conv[-6:],
            }, default=str)
            raw = gemini.intake_json(NATURAL_EXTRACT_PROMPT, payload, temperature=0.7)
            extracted = raw.get("extracted") if isinstance(raw, dict) else None
            if isinstance(extracted, dict):
                for k, v in extracted.items():
                    if k in FIELD_META and v is not None:
                        if k == "has_geometry":
                            continue  # only the upload route may flip this
                        coerced = _coerce(k, v)
                        if _is_filled(coerced):
                            fields[k] = coerced
            if isinstance(raw.get("reply"), str) and raw["reply"]:
                reply = raw["reply"]

        # Pass 3 — recompute missing after both extraction passes
        missing = _missing_ranked(fields)
        if not missing:
            return BrushTurn(
                reply=(reply or "Got it — I have everything I need. Review the proposed plan and approve to run the analysis."),
                fields=fields, asks_field=None, options=None,
                missing=[], ready_for_plan=True,
            )

        target = missing[0]
        meta = FIELD_META[target]
        if not reply:
            reply = self._template_question(target, fields)
        return BrushTurn(
            reply=reply, fields=fields, asks_field=target,
            options=meta.get("options"), missing=missing, ready_for_plan=False,
        )

    # ---- internals -----------------------------------------------------------

    def _template_question(self, field: str, fields: dict[str, Any]) -> str:
        """Friendly fallback questions — used when LLM is unavailable."""
        templates: dict[str, str] = {
            "brush_type":             "What type of brush are you packaging — toothbrush, electric toothbrush head, cosmetic brush, industrial brush, or something else?",
            "brush_weight_g":         "What is the approximate weight of the packaged brush in grams?",
            "primary_pack_type":      "What type of primary packaging is used — blister pack, clamshell, backer card, pouch, or carton?",
            "primary_pack_material":  "What material is the primary packaging made from — for example PET, RPET, PVC, Paperboard, or a mixed structure?",
            "transit_modes":          "Which transport modes will the shipment see — truck, rail, ship, air, manual handling? You can list more than one.",
            "road_condition":         "Since trucks are involved, what road condition fits best — smooth highway, mixed, rough secondary, or off-road?",
            "objective":              "What is the primary objective — transit survivability, ISTA planning, drop resistance, compression risk, or geometry risk assessment?",
            "has_secondary_carton":   "Will the product ship inside a secondary carton or corrugated box?",
            "carton_type":            "What type of secondary packaging — corrugated carton, tray, shrink bundle, rigid box, or corrugated shipper?",
            "carton_board_grade":     "What board construction is being used — 3-ply, 5-ply, E-flute, B-flute, or C-flute?",
            "carton_pack_count":      "How many primary packs go into each carton?",
            "carton_stacking_config": "How will these cartons be stacked during storage or transit — single pallet stack, double stacked, warehouse stacking, or container stacking?",
        }
        meta = FIELD_META[field]
        return templates.get(field, f"What is the {field.replace('_', ' ')}? — {meta.get('hint', '')}")

    def _cheap_extract(self, prior_fields: dict[str, Any], text: str) -> dict[str, Any]:
        """Lightweight extraction before the LLM call.

        Handles yes/no, numerics, categorical options, and transit modes
        without an LLM round-trip.
        """
        out: dict[str, Any] = {}
        t = (text or "").strip().lower()
        if not t:
            return out
        missing = _missing_ranked(prior_fields)
        if not missing:
            return out
        norm = t.replace(" ", "_").replace("-", "_")

        # Pass 1 — exact option match against any missing categorical field.
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

        # Pass 2 — bare yes/no: route to the first missing binary field.
        if t in ("yes", "no", "y", "n"):
            val = "yes" if t in ("yes", "y") else "no"
            for target in missing:
                if FIELD_META[target].get("options") == ["yes", "no"]:
                    out[target] = val
                    return out

        target0 = missing[0]

        # Pass 3 — numeric reply for numeric targets.
        if target0 in ("brush_weight_g", "carton_pack_count"):
            m = re.search(r"-?\d+(?:\.\d+)?", text)
            if m:
                out[target0] = _coerce(target0, m.group(0))
                return out

        # Pass 4 — transit_modes: named modes + common synonyms.
        if target0 == "transit_modes":
            modes: list[str] = []
            for word in ("truck", "rail", "ship", "air", "manual_handling", "manual"):
                if word in t:
                    modes.append("manual_handling" if word == "manual" else word)
            if not modes and any(w in t for w in (
                "mixed", "any", "all", "various", "everything", "multi", "multiple", "combo",
            )):
                modes = ["truck", "ship"]
            if modes:
                out["transit_modes"] = sorted(set(modes))
                return out

        return out

    # ---- shims for orchestrator compatibility --------------------------------

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
            "hint": FIELD_META.get(turn.asks_field, {}).get("hint", ""),
            "progress": {
                "step": len(REQUIRED_FIELDS) - len(turn.missing) + 1,
                "total": len(REQUIRED_FIELDS),
            },
        }
