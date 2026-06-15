"""Intake & Classification Agent (5.2).

Drives the user-facing conversation. The flow has three phases:

  1. SCOPE — natural conversation. Classify packaging_type, product_type,
     objective from whatever the user says. No pressure to upload yet.
  2. GEOMETRY — once we have a confident enough scope, ask for the CAD
     model. The reply MUST be phrased so the frontend recognises it and
     opens the upload modal automatically (it looks for a `request_upload`
     flag in the response payload).
  3. DETAILS — after the CAD is parsed (`has_geometry` flips True from
     the upload route), ask only for the facts CAD cannot tell us:
     material grade, fill volume, transit modes, environmental constraints.

Temperatures are intentionally HIGH for this role (Flash, 0.6–0.9): the
classifier needs latitude to commit to a packaging type from informal
language ("a 500 ml shampoo squeeze bottle") instead of stalling on
confirmation questions. Material is NEVER inferred from the CAD parse —
that is a one-way ratchet from the user (or from the explicit web/custom
material flow).
"""
from __future__ import annotations

import json
from typing import Any

from ..llm.gemini_client import get_gemini
from ..schemas import IntakeFields, IntakeResponse
from .base import GROUND_RULES


REQUIRED_FIELDS = ("packaging_type", "product_type", "objective", "material", "transit_modes")

# Upload is the primary routing mechanism — geometry identification determines
# the flow. We do NOT gate the upload behind scope questions.
PRE_CAD_FIELDS = ()

# Broader packaging-type vocabulary (the user asked us to bring this back).
# Anything not in this list maps to "secondary_pack" downstream.
PACKAGING_TYPES = [
    "bottle", "bottle_like", "jar", "can", "tube", "pouch", "sachet",
    "blister", "carton", "crate", "tray", "secondary_pack", "tertiary_pack",
    "pallet",
]
PRODUCT_TYPES = ["liquid", "viscous", "powder", "granular", "fragile",
                 "pressurized", "frozen", "perishable"]
OBJECTIVES = ["concept_check", "ista_planning", "transit_survivability",
              "geometry_risk", "sustainability_review", "cost_reduction"]


SYSTEM_PROMPT = f"""You are the Intake Agent for a packaging engineering platform.
{GROUND_RULES}

Your primary job is to get the geometry file uploaded. The platform identifies
packaging type automatically from the CAD geometry — do NOT ask the user to
declare bottle vs packet conversationally before the upload.

PHASE 1 — GEOMETRY (until upload).
  Your very first reply MUST ask for the geometry file. Set request_upload=true.
  Do NOT ask "is this a bottle or pouch?" or any packaging subtype question.
  If the user volunteers packaging details, absorb them into fields quietly
  without asking follow-up questions about them.
  Keep the upload ask front-and-centre until has_geometry becomes true.

  Use this phrasing (adapt slightly for natural flow):
  "Please upload the CAD, dieline, or geometry file so I can identify the
   packaging structure and route the correct engineering workflow.
   STEP, STL, OBJ, and GLB files are supported."

PHASE 2 — DETAILS (after has_geometry is true).
  Ask only for engineering facts that cannot come from geometry:
    • material — grade name (e.g. "PET", "HDPE", "PCR-PET"). NEVER infer
      from the geometry; if the user has not given a material, ASK.
    • transit_modes — truck, rail, ship, air, manual_handling.
    • environmental constraints (cold-chain, humidity, etc.).

Reply rules:
- Ask at most ONE question per turn, the most important one.
- Be conversational, not bureaucratic. Use British English.
- Never name the underlying model or any company; speak as "the analysis".

Return STRICTLY a JSON object with keys:
  reply             — your conversational reply (max 2 sentences)
  fields            — any fields you confidently extracted this turn
  next_questions    — list with the single follow-up you intend
  ready_for_plan    — true ONLY when all of {", ".join(REQUIRED_FIELDS)}
                      are populated, has_geometry is true, and confidence >= 0.6
  request_upload    — true when this reply is asking for the upload
"""


def _merge_fields(prior: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Newer non-null values win; lists are unioned."""
    merged: dict[str, Any] = dict(prior or {})
    for k, v in new.items():
        if v is None:
            continue
        if isinstance(v, list):
            merged[k] = sorted(set([*merged.get(k, []), *v]))
        else:
            merged[k] = v
    return merged


def _phase(fields: dict[str, Any]) -> str:
    if not all(fields.get(f) for f in PRE_CAD_FIELDS):
        return "scope"
    if not fields.get("has_geometry"):
        return "geometry"
    return "details"


def _heuristic_fallback(conversation: list[dict[str, str]], prior: dict[str, Any]) -> IntakeResponse:
    """Used when the LLM is in stub mode. Keyword-based intake so the demo
    flow still works end-to-end. Mirrors the three-phase logic of the LLM
    prompt above."""
    text = " ".join(m["content"].lower() for m in conversation if m["role"] == "user")
    fields: dict[str, Any] = dict(prior or {})

    pack_map = {
        "bottle": "bottle", "jar": "jar", "can": "can", "tube": "tube",
        "pouch": "pouch", "sachet": "sachet", "blister": "blister",
        "carton": "carton", "crate": "crate", "tray": "tray",
        "secondary": "secondary_pack", "case": "secondary_pack",
        "pallet": "pallet", "tertiary": "tertiary_pack",
    }
    for k, v in pack_map.items():
        if k in text and not fields.get("packaging_type"):
            fields["packaging_type"] = v
    for k in PRODUCT_TYPES:
        if k in text and not fields.get("product_type"):
            fields["product_type"] = k
    objective_map = {
        "concept":         "concept_check",
        "ista":            "ista_planning",
        "transit":         "transit_survivability",
        "drop":            "transit_survivability",
        "geometry":        "geometry_risk",
        "sustain":         "sustainability_review",
        "carbon":          "sustainability_review",
        "pcr":             "sustainability_review",
        "cost":            "cost_reduction",
        "cheap":           "cost_reduction",
    }
    for k, v in objective_map.items():
        if k in text and not fields.get("objective"):
            fields["objective"] = v
    # ONLY recognise material when the user explicitly gave one in chat.
    # We never derive it from CAD or geometry hints.
    for mat in ("pcr-pet", "pcr-hdpe", "pcr-pp", "pcr-aluminium",
                "rpet", "pet", "hdpe", "ldpe", "pp", "pvc", "ps",
                "glass", "aluminum", "aluminium", "corrugated"):
        if mat in text and not fields.get("material"):
            fields["material"] = mat.upper() if "-" not in mat else mat.upper()
    modes = set(fields.get("transit_modes") or [])
    for m in ("truck", "rail", "ship", "air", "manual"):
        if m in text:
            modes.add("manual_handling" if m == "manual" else m)
    if not modes and any(w in text for w in (
        "mixed", "any mode", "all modes", "various", "everything", "multi", "combo",
    )):
        modes.update(["truck", "ship"])
    if modes:
        fields["transit_modes"] = sorted(modes)
    road_opts = ("smooth_highway", "mixed", "rough_secondary", "off_road", "smooth", "rough")
    for w in road_opts:
        if w in text and not fields.get("road_condition"):
            mapped = {"smooth": "smooth_highway", "rough": "rough_secondary"}.get(w, w)
            fields["road_condition"] = mapped
            break

    missing = [f for f in REQUIRED_FIELDS if not fields.get(f)]
    confidence = round(1.0 - len(missing) / len(REQUIRED_FIELDS), 2)
    fields["confidence"] = confidence
    fields["missing_fields"] = missing

    phase = _phase(fields)
    request_upload = False
    if phase == "geometry":
        reply = (
            "Please upload the CAD, dieline, or geometry file so I can identify "
            "the packaging structure and route the correct engineering workflow. "
            "STEP, STL, OBJ, and GLB files are supported."
        )
        next_qs = ["Please upload the geometry file for the primary pack."]
        ready = False
        request_upload = True
    elif missing:
        # Phase 3: details CAD can't tell us
        prompts = {
            "material":      "What material is the pack made of? I'll never guess this from the geometry — please give me the grade (e.g. PET, HDPE, PCR-PET).",
            "transit_modes": "Which transport modes will it actually see — truck, rail, ship, air, manual handling?",
        }
        next_field = missing[0]
        reply = prompts.get(next_field, f"Could you tell me the {next_field.replace('_', ' ')}?")
        next_qs = [reply]
        ready = False
    else:
        reply = ("I have enough to propose a plan. Review the proposal on the right "
                 "and approve to proceed; the run will consume one simulation token.")
        next_qs = []
        ready = True

    try:
        fields_obj = IntakeFields(**{k: v for k, v in fields.items() if k in IntakeFields.model_fields})
    except Exception:
        fields_obj = IntakeFields()
    resp = IntakeResponse(reply=reply, fields=fields_obj, next_questions=next_qs, ready_for_plan=ready)
    # request_upload travels alongside the response via a metadata dict the
    # orchestrator copies into the chat message metadata.
    resp.__dict__["request_upload"] = request_upload
    return resp


class IntakeAgent:
    def run(self, *, conversation: list[dict[str, str]], prior_fields: dict[str, Any] | None) -> IntakeResponse:
        gemini = get_gemini()
        if not gemini.available:
            return _heuristic_fallback(conversation, prior_fields or {})

        convo_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in conversation[-10:])
        prior_json = json.dumps(prior_fields or {}, indent=2)
        phase = _phase(prior_fields or {})
        user = (
            f"Current phase: {phase}\n\n"
            f"Conversation so far:\n{convo_text}\n\n"
            f"Prior extracted fields:\n{prior_json}"
        )
        # Higher temperature so Flash commits to a packaging-type classification
        # from informal user phrasing instead of looping for confirmation.
        raw = gemini.intake_json(SYSTEM_PROMPT, user, temperature=0.7)

        reply = str(raw.get("reply") or "Could you tell me what packaging type and product you're working with?")
        fields_in = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
        # Hard guarantee: NEVER let the LLM set has_geometry from chat; only
        # the /upload route may flip it true.
        if isinstance(fields_in, dict) and "has_geometry" in fields_in:
            fields_in.pop("has_geometry", None)
        # Hard guarantee: NEVER let the LLM derive material from a CAD parse.
        # If the user hasn't said it in chat, leave it null.
        user_text = " ".join(m["content"].lower() for m in conversation if m["role"] == "user")
        if (
            isinstance(fields_in, dict)
            and fields_in.get("material")
            and not _material_mentioned_by_user(user_text, fields_in["material"])
        ):
            fields_in.pop("material", None)

        merged = _merge_fields(prior_fields, fields_in)
        missing = [f for f in REQUIRED_FIELDS if not merged.get(f)]
        merged["missing_fields"] = missing
        merged.setdefault("confidence", float(raw.get("confidence", fields_in.get("confidence", 0.0)) or 0.0))

        ready = bool(raw.get("ready_for_plan")) and not missing and merged.get("confidence", 0) >= 0.6
        nq_raw = raw.get("next_questions")
        if isinstance(nq_raw, str):
            next_qs = [nq_raw]
        elif isinstance(nq_raw, list):
            next_qs = [str(x) for x in nq_raw if x]
        else:
            next_qs = [f"What is the {missing[0].replace('_', ' ')}?"] if missing else []

        try:
            fields_obj = IntakeFields(**{k: v for k, v in merged.items() if k in IntakeFields.model_fields})
        except Exception:
            fields_obj = IntakeFields()

        resp = IntakeResponse(reply=reply, fields=fields_obj, next_questions=next_qs, ready_for_plan=ready)
        # Echo the upload-request flag so the UI can auto-open the upload
        # modal. We also force it true when we *should* be in the geometry
        # phase regardless of whether the LLM remembered to set it.
        request_upload = bool(raw.get("request_upload")) or (_phase(merged) == "geometry")
        resp.__dict__["request_upload"] = request_upload
        return resp


def _material_mentioned_by_user(user_text: str, material_name: str) -> bool:
    """Return True only if the material name (or a close substring) appears
    in the user's own text. Guards against the LLM inferring a material from
    CAD shape hints."""
    if not user_text or not material_name:
        return False
    needle = material_name.strip().lower()
    if needle in user_text:
        return True
    # Allow short token forms like "pet", "hdpe" inside fuller phrases.
    return any(tok in user_text for tok in needle.replace("-", " ").split())
