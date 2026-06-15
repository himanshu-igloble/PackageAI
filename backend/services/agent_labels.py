"""Translate raw orchestrator emit_status payloads into user-friendly text.

Engineering-document grammar: noun phrases, no debug strings like
`asking:bottle_subtype` or `gemini-2.5-flash`.
"""
from __future__ import annotations

from typing import Any


# Agent → friendly display name
AGENT_LABEL = {
    "orchestrator":         "Workflow",
    "intake":               "Intake",
    "intake_agent":         "Intake",
    "bottle_flow":          "Bottle intake",
    "material":             "Material lookup",
    "material_agent":       "Material lookup",
    "geometry":             "Geometry parser",
    "geometry_service":     "Geometry parser",
    "transit":              "Transit envelope",
    "transit_agent":        "Transit envelope",
    "calculation":          "Engineering calculations",
    "calculation_agent":    "Engineering calculations",
    "surrogate":            "Risk-zone surrogate",
    "surrogate_agent":      "Risk-zone surrogate",
    "ista2a":               "ISTA 2A evaluation",
    "ista2a_agent":         "ISTA 2A evaluation",
    "reasoning":            "Self-check",
    "reasoning_agent":      "Self-check",
    "report":               "Report drafter",
    "report_agent":         "Report drafter",
    "guardrail":            "Guardrail",
    "optimization":         "Optimisation",
    "optimization_agent":   "Optimisation",
    "visualization":        "3D visualisation",
    "feedback":             "Feedback",
    "user":                 "You",
}

# Action verb → friendly noun phrase
ACTION_LABEL = {
    "received_user_message":     "received your message",
    "classify":                  "classifying intent",
    "reply_sent":                "sent a reply",
    "extract_answer":            "extracting your answer",
    "conversational_extract":    "extracting details",
    "all_fields_collected":      "ready to plan",
    "asked_question":            "waiting on you",
    "stream_open":               "live",
    "plan_approved":             "plan approved",
    "execute_plan_start":        "starting analysis",
    "execute_plan_done":         "analysis complete",
    "lookup":                    "looking up properties",
    "lookup_done":               "lookup complete",
    "build_envelope":            "building envelope",
    "envelope_done":             "envelope ready",
    "drop_energy":               "computing drop energy",
    "compression_sf":            "computing compression SF",
    "thin_wall_buckling":        "checking thin-wall buckling",
    "zone_risk_map":             "mapping risk zones",
    "zone_risk_map_done":        "risk zones ready",
    "build_heatmaps":            "rendering heatmaps",
    "heatmaps_done":             "heatmaps ready",
    "draft":                     "drafting report",
    "draft_done":                "report drafted",
    "self_check":                "running self-check",
    "self_check_done":           "self-check complete",
    "self_check_skipped":        "self-check skipped",
    "evaluate":                  "running ISTA 2A",
    "evaluate_done":             "ISTA 2A complete",
    "finalized":                 "case finalized",
    "saved_design":              "design saved",
    "renamed_design":            "renamed",
    "feedback":                  "feedback received",
    "received":                  "received",
    "blocked_calc":              "blocked an unsafe calculation",
    "block_calc":                "blocked an unsafe calculation",
    "block_report":              "redacted unsupported claims",
    "exported_pdf":              "exported PDF",
    "gauge_intent":              "reading your goal",
    "generate_alternatives":     "generating alternatives",
    "alternatives_done":         "alternatives ready",
    "parsing":                   "parsing geometry",
    "parsed":                    "geometry parsed",
    "parse_failed":              "parse failed",
    "loaded_asset":              "loaded geometry",
    "upload_parsed":             "geometry uploaded",
    "upload_failed":             "upload failed",
    "message":                   "spoke",
}


# Tool ids that should NOT appear in user-facing UI
HIDDEN_TOOLS = {"gemini-2.5-flash", "gemini-3-pro", "gemini-2.0-flash"}


def humanize(evt: dict[str, Any]) -> dict[str, Any]:
    """Translate one status event into a user-safe payload.

    Hides model names, replaces verb-strings with noun phrases, keeps the
    raw payload available under `_raw` for the optional advanced view."""
    out = dict(evt)
    raw_action = evt.get("action") or ""
    # Strip "asking:bottle_subtype" → "asking for bottle subtype"
    if raw_action.startswith("asking:"):
        field = raw_action.split(":", 1)[1].replace("_", " ")
        out["friendly_action"] = f"asking for {field}"
    else:
        out["friendly_action"] = ACTION_LABEL.get(raw_action, raw_action.replace("_", " "))

    out["friendly_agent"] = AGENT_LABEL.get(evt.get("active_agent") or "", evt.get("active_agent"))

    raw_tool = evt.get("tool")
    out["tool_visible"] = (raw_tool not in HIDDEN_TOOLS) if raw_tool else False
    if not out["tool_visible"]:
        out["tool"] = None

    out["_raw"] = {k: evt.get(k) for k in ("active_agent", "action", "tool")}
    return out
