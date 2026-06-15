"""Packet Flow Agent — natural conversation, structured outcome.

Architecture mirrors BottleFlowAgent. On every user turn:
  1. Geometry-first gate — request CAD/dieline upload before any field questions.
  2. Cheap regex/match extraction — handles single-token replies without LLM.
  3. LLM natural-extract — absorbs all fields the user mentioned at once.
  4. Validate / coerce each extracted value against the schema.
  5. Ask the highest-priority still-missing field in a conversational reply.

Covers three packaging tiers:
  PRIMARY   — pouch / packet / sachet
  SECONDARY — carton / master case / shipper
  TERTIARY  — pallet / transit stack

Geometry upload is requested first but NOT mandatory. Flow continues without it.
Priority order adapts dynamically based on product_category (chips → nitrogen
flush moves up; namkeen → oil content moves up; powder → moisture sensitivity
moves up).
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

    # A — Product
    "product_category":         {"options": ["namkeen", "chips", "powder", "biscuit", "noodles", "frozen", "granular", "liquid", "paste", "other"], "hint": "determines barrier and seal risk profile",                    "importance": 100},
    "fill_weight_g":            {"options": None,                                                                                                    "hint": "net fill weight in grams",                                    "importance": 98},
    "product_fragility":        {"options": ["low", "medium", "high"],                                                                               "hint": "breakage risk under transit vibration",                       "importance": 65},
    "headspace_pct":            {"options": None,                                                                                                    "hint": "gas headspace as percentage of pack volume",                  "importance": 58},
    "contains_oil":             {"options": ["yes", "no"],                                                                                           "hint": "oils accelerate laminate delamination and seal failure",      "importance": 63},

    # B — Packet Construction
    # packet_style describes pack FORMAT; seal_type describes SEAM TYPE. No overlap.
    "packet_style":             {"options": ["pillow_pouch", "standup_pouch", "gusset_pouch", "sachet", "quad_seal", "flow_wrap", "vacuum_pack"],    "hint": "pack format determines seam geometry and load path",           "importance": 96},
    "packet_dimensions_mm":     {"options": None,                                                                                                    "hint": "length x width x gusset in mm",                               "importance": 75},
    "laminate_structure":       {"options": None,                                                                                                    "hint": "e.g. PET/MetPET/LDPE — drives barrier and puncture resistance","importance": 94},
    "total_thickness_micron":   {"options": None,                                                                                                    "hint": "overall laminate thickness in microns",                       "importance": 92},
    "seal_type":                {"options": ["center_back_seal", "fin_seal", "lap_seal", "side_seal", "three_side", "four_side"],                    "hint": "seam type — weakest path for burst and delamination",         "importance": 90},
    "seal_width_mm":            {"options": None,                                                                                                    "hint": "seal land width in mm — affects peel strength",               "importance": 52},

    # C — Barrier
    "moisture_sensitivity":     {"options": ["low", "medium", "high"],                                                                               "hint": "determines MVTR requirement",                                 "importance": 68},
    "oxygen_sensitivity":       {"options": ["low", "medium", "high"],                                                                               "hint": "determines OTR requirement",                                  "importance": 68},
    "nitrogen_flush":           {"options": ["yes", "no"],                                                                                           "hint": "affects headspace pressure and burst risk during transit",    "importance": 72},
    "target_shelf_life_months": {"options": None,                                                                                                    "hint": "shelf-life target to back-calculate barrier specification",   "importance": 63},

    # D — Secondary Carton
    # Importance 60 — asked AFTER all primary required fields (which range 80–100)
    # and after the conditional road_condition (68). This ensures the secondary
    # carton question appears as the LAST question, not in the middle of primary intake.
    "has_secondary_carton":     {"options": ["yes", "no"],                                                                                           "hint": "whether packets ship inside a master carton",                 "importance": 60},
    "carton_type":              {"options": ["mono_carton", "corrugated_shipper", "duplex", "display_carton", "master_case"],                        "hint": "board type drives compression modelling",                     "importance": 75},
    "packets_per_carton":       {"options": None,                                                                                                    "hint": "number of packets per master case",                           "importance": 83},
    "carton_board_grade":       {"options": None,                                                                                                    "hint": "e.g. 3 ply, 5 ply, E flute, B flute",                        "importance": 60},
    "carton_dimensions_mm":     {"options": None,                                                                                                    "hint": "carton L x W x H in mm",                                      "importance": 55},
    "carton_stack_height":      {"options": None,                                                                                                    "hint": "number of cartons stacked in transit",                        "importance": 78},

    # E — Transit
    "transit_modes":            {"options": ["truck", "rail", "ship", "air", "manual_handling"],                                                     "hint": "all modes the shipment will encounter",                       "importance": 88},
    "road_condition":           {"options": ["smooth_highway", "mixed", "rough_secondary", "off_road"],                                              "hint": "applies when truck is in the transit mix",                    "importance": 68},
    "climate_exposure":         {"options": ["dry", "humid", "refrigerated", "hot"],                                                                 "hint": "affects flex crack, nitrogen retention, and barrier loss",    "importance": 62},
    "stacking_method":          {"options": ["palletized", "loose_loaded", "mixed_load", "floor_stack"],                                             "hint": "determines compression load model",                           "importance": 58},

    # F — Geometry (optional — requested first, not required to proceed)
    "has_geometry":             {"options": ["yes", "no"],                                                                                           "hint": "dieline or CAD file for exact dimension extraction",          "importance": 50},

    # G — Objective
    "objective":                {"options": ["seal_failure_risk", "transit_survivability", "compression_risk", "puncture_risk", "shelf_life_validation", "ista_planning", "cost_optimization", "laminate_optimization"], "hint": "controls which analysis agents run", "importance": 80},
}


# ---------------------------------------------------------------------------
# SECTION B — REQUIRED FIELDS
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "product_category",
    "fill_weight_g",
    "packet_style",
    "laminate_structure",
    "total_thickness_micron",
    "seal_type",
    "transit_modes",
    "objective",
    "has_secondary_carton",   # asked last — importance 60, after all primary fields
]

# Required only when has_secondary_carton == "yes".
_CARTON_REQUIRED = ["carton_type", "packets_per_carton", "carton_stack_height", "carton_board_grade"]


# ---------------------------------------------------------------------------
# SECTION C — SILENT DEFAULTS
# ---------------------------------------------------------------------------

# Applied internally only. NEVER referenced in conversation text.
SILENT_DEFAULTS = {
    "stacking_method": "palletized",
    "has_geometry":    False,
}


# ---------------------------------------------------------------------------
# SECTION D — GEOMETRY-FIRST GATE
# ---------------------------------------------------------------------------

# Shown once at the opening of PacketFlow before any field questions.
# Geometry is OPTIONAL for packet flow — soft ask, no blocking modal.
_GEOMETRY_GATE_REPLY = (
    "Do you have a CAD file or dieline for this pack? "
    "DXF, STEP, STL, and GLB are all accepted — I'll use it to extract exact dimensions. "
    "If not, no problem; just say so and I'll ask for the dimensions later."
)


def _needs_geometry_request(fields: dict[str, Any]) -> bool:
    """True when the geometry gate should fire — exactly once per session."""
    return (
        not fields.get("has_geometry")
        and not fields.get("geometry_upload_prompted")
    )


# ---------------------------------------------------------------------------
# SECTION E — PRODUCT-AWARE PRIORITY BOOST
# ---------------------------------------------------------------------------

# Additive importance boost applied per product_category.
# Makes the conversation adapt without LLM guessing.
_PRODUCT_PRIORITY_BOOST: dict[str, dict[str, int]] = {
    "chips":    {"nitrogen_flush": 22, "oxygen_sensitivity": 10},
    "namkeen":  {"contains_oil": 17, "nitrogen_flush": 12},
    "powder":   {"moisture_sensitivity": 14, "seal_type": 6},
    "frozen":   {"moisture_sensitivity": 12, "climate_exposure": 18},
    "liquid":   {"seal_type": 10, "moisture_sensitivity": 8},
    "paste":    {"seal_type": 8, "contains_oil": 8},
    "biscuit":  {"product_fragility": 15, "moisture_sensitivity": 10},
}


# ---------------------------------------------------------------------------
# SECTION F — NATURAL EXTRACTION PROMPT
# ---------------------------------------------------------------------------

NATURAL_EXTRACT_PROMPT = """You are the PacketFlow conversational agent for a CPG flexible packaging engineering platform.

Your job has two parts:

1. EXTRACT: From the user's most recent message (and conversation so far),
   pull any packet-flow fields they clearly and explicitly state.
   The user may mention several fields at once. Extract only what is stated —
   do NOT invent, infer, or assume ANY value from context.

   STRICT RULES:
   - NEVER assume packet_style from the word "packet" or "pouch" alone.
   - NEVER assume stacking_method from transit context.
   - NEVER assume nitrogen_flush, laminate, or seal_type from product category.
   - NEVER reference or surface silent defaults (like stacking_method=palletized).
   - Only extract a value if the user stated it explicitly and unambiguously.

   Fields you may extract:
   - product_category: namkeen | chips | powder | biscuit | noodles | frozen | granular | liquid | paste | other
   - fill_weight_g: number (grams)
   - product_fragility: low | medium | high
   - headspace_pct: number 0..100
   - contains_oil: yes | no
   - packet_style: pillow_pouch | standup_pouch | gusset_pouch | sachet | quad_seal | flow_wrap | vacuum_pack
   - packet_dimensions_mm: object {length, width, gusset} in mm
   - laminate_structure: string (e.g. "PET/MetPET/LDPE", "BOPP/MetBOPP/LDPE", "Nylon/PE")
   - total_thickness_micron: number
   - seal_type: center_back_seal | fin_seal | lap_seal | side_seal | three_side | four_side
   - seal_width_mm: number
   - moisture_sensitivity: low | medium | high
   - oxygen_sensitivity: low | medium | high
   - nitrogen_flush: yes | no
   - target_shelf_life_months: number
   - has_secondary_carton: yes | no
   - carton_type: mono_carton | corrugated_shipper | duplex | display_carton | master_case
   - packets_per_carton: number
   - carton_board_grade: string (e.g. "3 ply", "5 ply", "E flute", "B flute")
   - carton_dimensions_mm: object {length, width, height} in mm
   - carton_stack_height: number
   - transit_modes: subset of [truck, rail, ship, air, manual_handling]
   - road_condition: smooth_highway | mixed | rough_secondary | off_road
   - climate_exposure: dry | humid | refrigerated | hot
   - stacking_method: palletized | loose_loaded | mixed_load | floor_stack
   - has_geometry: NEVER extract from chat. Set only by the upload route.
   - objective: seal_failure_risk | transit_survivability | compression_risk | puncture_risk | shelf_life_validation | ista_planning | cost_optimization | laminate_optimization

2. CONVERSE: Write ONE friendly reply that:
   - Acknowledges only what the user explicitly said (one short clause).
   - Asks the MOST important still-missing field (one question).
   - Sounds like a natural conversation — not a form, not a checklist.
   - Does NOT mention step numbers, progress bars, or form fields by name.
   - Does NOT surface or reference silent defaults (stacking, palletized, etc.).
   - Mentions allowed options conversationally where helpful.
   - Asks about only information the user has NOT already provided.

You receive `missing_ranked` — the still-missing required fields in priority order.
Ask about the first one. Do not repeat the exact phrasing of a prior question.

Return STRICTLY a JSON object:
{
  "extracted": { "<field>": <value>, ... },
  "reply": "<your single conversational reply>",
  "asks_field": "<field name you are asking about, or null>"
}
"""


# ---------------------------------------------------------------------------
# SECTION G — HELPERS
# ---------------------------------------------------------------------------

# Separator pattern for dimension parsing.
_DIM_SEP = r"[\s]*(?:x|\*|by|×)[\s]*"
_NUM = r"(\d+(?:\.\d+)?)"


def _parse_dimensions_mm(text: str) -> Optional[dict[str, float]]:
    """Extract 2D or 3D dimensions from natural text.

    Handles: 120x180, 120 x 180, 120x180x40, 120*180*40, 120 by 180 by 40.
    Returns a dict or None if nothing found.
    """
    m3 = re.search(_NUM + _DIM_SEP + _NUM + _DIM_SEP + _NUM, text, re.IGNORECASE)
    if m3:
        return {
            "length": float(m3.group(1)),
            "width":  float(m3.group(2)),
            "gusset": float(m3.group(3)),
        }
    m2 = re.search(_NUM + _DIM_SEP + _NUM, text, re.IGNORECASE)
    if m2:
        return {
            "length": float(m2.group(1)),
            "width":  float(m2.group(2)),
        }
    return None


def _parse_laminate(text: str) -> Optional[str]:
    """Extract laminate structure from text.

    Matches slash-separated sequences of material tokens (e.g. PET/MetPET/LDPE).
    Returns the first valid match or None.
    """
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:/[A-Za-z][A-Za-z0-9]*){1,6}", text)
    for candidate in candidates:
        tokens = candidate.split("/")
        if len(tokens) < 2:
            continue
        # Validate: at least one token must match a known laminate material.
        upper_tokens = [t.upper() for t in tokens]
        if any(
            any(known in ut for known in {"PET", "PP", "PE", "PA", "BOPP", "LDPE",
                                          "CPP", "EVOH", "NYLON", "FOIL", "LLDPE"})
            for ut in upper_tokens
        ):
            return candidate
    return None


def _coerce(field: str, raw: Any) -> Any:
    if raw is None:
        return None
    if field in ("fill_weight_g", "headspace_pct", "seal_width_mm",
                 "target_shelf_life_months", "total_thickness_micron"):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if field in ("packets_per_carton", "carton_stack_height"):
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
    if field in ("packet_dimensions_mm", "carton_dimensions_mm"):
        if isinstance(raw, dict):
            return {k: float(v) for k, v in raw.items() if v is not None}
        return None
    return str(raw).strip()


def _is_filled(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, list) and not value:
        return False
    if isinstance(value, dict) and not value:
        return False
    return True


def _missing_ranked(fields: dict[str, Any]) -> list[str]:
    """Return still-missing required fields ordered by importance (highest first).

    Geometry is NOT required — flow continues without it.
    road_condition required only when truck is in transit_modes.
    Carton fields required only when has_secondary_carton is 'yes'.
    Priority boosts applied per product_category for product-aware flow.
    """
    missing: list[str] = []

    for f in REQUIRED_FIELDS:
        if not _is_filled(fields.get(f)):
            missing.append(f)

    if fields.get("has_secondary_carton") == "yes":
        for f in _CARTON_REQUIRED:
            if not _is_filled(fields.get(f)):
                missing.append(f)

    modes = fields.get("transit_modes") or []
    if "truck" in modes and not _is_filled(fields.get("road_condition")):
        missing.append("road_condition")

    product = (fields.get("product_category") or "").lower()
    boost = _PRODUCT_PRIORITY_BOOST.get(product, {})

    return sorted(
        missing,
        key=lambda f: -(FIELD_META.get(f, {}).get("importance", 0) + boost.get(f, 0)),
    )


def _apply_silent_defaults(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    for k, v in SILENT_DEFAULTS.items():
        if not _is_filled(out.get(k)):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# SECTION H — AGENT
# ---------------------------------------------------------------------------

@dataclass
class PacketTurn:
    """Result of one packet-flow conversation turn."""
    reply: str
    fields: dict[str, Any]
    asks_field: Optional[str]
    options: Optional[list[str]]
    missing: list[str]
    ready_for_plan: bool


class PacketFlowAgent:
    """Conversational, multi-field flexible-packaging intake."""

    @staticmethod
    def is_packet(case_summary: dict[str, Any]) -> bool:
        _PACKET_TYPES = {
            "pouch", "packet", "sachet", "standup_pouch", "centre_seal_pouch",
            "center_seal_pouch", "flow_wrap", "flexible", "flexible_packaging",
            "pillow_pouch", "gusset_pouch", "quad_seal", "vacuum_pack",
        }
        pt = str((case_summary or {}).get("packaging_type") or "").lower()
        return bool(pt) and any(t in pt for t in _PACKET_TYPES)

    @staticmethod
    def missing_required(case_summary: dict[str, Any]) -> list[str]:
        return _missing_ranked(case_summary or {})

    # ------------------------------------------------------------------

    def opener(self, fields: dict[str, Any]) -> PacketTurn:
        """Initial turn — no user input to absorb; phrase the first question.

        Always asks for geometry upload first (geometry-first gate), then
        proceeds to field questions on subsequent turns.
        """
        fields = _apply_silent_defaults(fields)

        # Geometry-first gate: fires exactly once per session.
        if _needs_geometry_request(fields):
            fields["geometry_upload_prompted"] = True
            return PacketTurn(
                reply=_GEOMETRY_GATE_REPLY,
                fields=fields,
                asks_field="has_geometry",
                options=None,
                missing=_missing_ranked(fields),
                ready_for_plan=False,
            )

        missing = _missing_ranked(fields)
        if not missing:
            return PacketTurn(
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
        return PacketTurn(
            reply=question, fields=fields, asks_field=target,
            options=meta.get("options"), missing=missing, ready_for_plan=False,
        )

    def step(self, fields: dict[str, Any], user_message: str,
             conversation: list[dict[str, str]] | None = None) -> PacketTurn:
        """Absorb the user's free-form reply, then phrase the next conversational ask."""
        fields = _apply_silent_defaults(fields)
        # Mark geometry as prompted if not already — prevents repeated geometry asks
        # when the user's first message contains field data rather than a file upload.
        if not fields.get("geometry_upload_prompted"):
            fields["geometry_upload_prompted"] = True
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
            return PacketTurn(
                reply=(reply or "Got it — I have everything I need. Review the proposed plan and approve to run the analysis."),
                fields=fields, asks_field=None, options=None,
                missing=[], ready_for_plan=True,
            )

        target = missing[0]
        meta = FIELD_META[target]
        if not reply:
            reply = self._template_question(target, fields)
        return PacketTurn(
            reply=reply, fields=fields, asks_field=target,
            options=meta.get("options"), missing=missing, ready_for_plan=False,
        )

    # ---- internals -----------------------------------------------------------

    def _template_question(self, field: str, fields: dict[str, Any]) -> str:
        """Friendly fallback questions — used when LLM is unavailable.

        No mention of defaults. No assumption language. No step references.
        """
        product = (fields.get("product_category") or "").lower()
        # Product-aware variants for key fields.
        nitrogen_q = (
            "Since chips can be nitrogen-flushed to protect texture and extend shelf life, is this pack nitrogen-flushed?"
            if product == "chips" else
            "Is this pack nitrogen-flushed?"
        )
        oil_q = (
            "Namkeen often contains oil — does this product contain oil? It affects laminate and seal compatibility."
            if product == "namkeen" else
            "Does the product contain oil?"
        )

        templates: dict[str, str] = {
            "product_category":         "What product category best describes what's inside — namkeen, chips, powder, biscuit, noodles, frozen, granular, liquid, or paste?",
            "fill_weight_g":            "What is the net fill weight per pack in grams?",
            "product_fragility":        "How fragile is the product under vibration — low, medium, or high?",
            "headspace_pct":            "Roughly what percentage of the pack volume is headspace gas?",
            "contains_oil":             oil_q,
            "packet_style":             "What pack format is this — pillow pouch, standup pouch, gusset pouch, sachet, flow-wrap, quad-seal, or vacuum pack?",
            "packet_dimensions_mm":     "What are the pack dimensions in mm — length, width, and gusset if applicable?",
            "laminate_structure":       "What laminate structure are you using — for example PET/MetPET/LDPE, BOPP/MetBOPP/LDPE, or Nylon/PE?",
            "total_thickness_micron":   "What is the total laminate thickness in microns?",
            "seal_type":                "What seam type does this pack use — center-back seal, fin seal, lap seal, side seal, three-side, or four-side?",
            "seal_width_mm":            "What is the seal land width in mm?",
            "moisture_sensitivity":     "How moisture-sensitive is this product — low, medium, or high?",
            "oxygen_sensitivity":       "What is the oxygen sensitivity — low, medium, or high?",
            "nitrogen_flush":           nitrogen_q,
            "target_shelf_life_months": "What is the target shelf life in months?",
            "has_secondary_carton":     "Do the packs ship inside a secondary carton or master case?",
            "carton_type":              "What type of carton — mono carton, corrugated shipper, duplex, display carton, or master case?",
            "packets_per_carton":       "How many packs go into each carton?",
            "carton_board_grade":       "What is the board grade — for example 3 ply, 5 ply, E flute, or B flute?",
            "carton_dimensions_mm":     "What are the carton dimensions — length, width, and height in mm?",
            "carton_stack_height":      "How many cartons are stacked in transit?",
            "transit_modes":            "Which transport modes will the shipment see — truck, rail, ship, air, manual handling? You can list more than one.",
            "road_condition":           "Since trucks are involved, what road condition fits best — smooth highway, mixed, rough secondary, or off-road?",
            "climate_exposure":         "What climate exposure should the analysis cover — dry, humid, refrigerated, or hot?",
            "stacking_method":          "How are the cartons stacked during transit — palletized, loose-loaded, mixed load, or floor-stacked?",
            "objective":                "What is the primary objective — seal failure risk, transit survivability, compression risk, puncture risk, shelf-life validation, ISTA planning, cost optimisation, or laminate optimisation?",
        }
        meta = FIELD_META[field]
        return templates.get(field, f"What is the {field.replace('_', ' ')}? — {meta.get('hint', '')}")

    def _cheap_extract(self, prior_fields: dict[str, Any], text: str) -> dict[str, Any]:
        """Lightweight extraction before the LLM call.

        Handles yes/no, numerics, categorical options, transit modes,
        laminate structure, and dimensions without an LLM round-trip.
        Walks ALL missing fields so unambiguous single-token replies are
        routed to the correct slot rather than the top-of-queue field.
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
        # has_geometry is platform-managed; always skip.
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
        if target0 in ("fill_weight_g", "total_thickness_micron", "seal_width_mm",
                       "target_shelf_life_months", "headspace_pct",
                       "packets_per_carton", "carton_stack_height"):
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

        # Pass 5 — dimension extraction (packet or carton, whichever is missing first).
        if target0 in ("packet_dimensions_mm", "carton_dimensions_mm"):
            dims = _parse_dimensions_mm(text)
            if dims:
                out[target0] = dims
                return out

        # Pass 6 — laminate structure: validate slash-separated material string.
        if target0 == "laminate_structure":
            lam = _parse_laminate(text)
            if lam:
                out["laminate_structure"] = lam
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
