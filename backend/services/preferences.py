"""User preference loop — derived from accumulated Feedback rows.

Every time the user rates / comments on an output, we update a lightweight
preference profile keyed by user_id. The orchestrator queries this at intake
to nudge prompts (verbosity, optimisation defaults, tone).

This is intentionally simple: a deterministic aggregator, no ML. The point is
that the next session for the same user feels customised, not pristine.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import Feedback


def aggregate_preferences(db: Session, user_id: str) -> dict[str, Any]:
    rows = (
        db.query(Feedback)
        .filter(Feedback.user_id == user_id)
        .order_by(Feedback.created_at.desc())
        .limit(50)
        .all()
    )
    if not rows:
        return {"verbosity": "medium", "tone": "neutral",
                "optimisation_default": None, "samples": 0}

    pos = sum(1 for r in rows if r.rating > 0)
    neg = sum(1 for r in rows if r.rating < 0)

    tags: dict[str, int] = {}
    for r in rows:
        for k, v in (r.tags or {}).items():
            if v:
                tags[k] = tags.get(k, 0) + 1

    verbosity = "medium"
    if tags.get("too_verbose", 0) >= 2 and tags.get("too_terse", 0) < tags.get("too_verbose", 0):
        verbosity = "low"
    elif tags.get("too_terse", 0) >= 2:
        verbosity = "high"

    tone = "neutral"
    if tags.get("more_technical", 0) >= 2:
        tone = "technical"
    elif tags.get("more_friendly", 0) >= 2:
        tone = "friendly"

    optimisation_default = None
    cost_pref = tags.get("prefer_cost", 0)
    strength_pref = tags.get("prefer_strength", 0)
    if cost_pref >= 2 and cost_pref >= strength_pref:
        optimisation_default = "reduce_cost"
    elif strength_pref >= 2:
        optimisation_default = "increase_strength"

    return {
        "verbosity": verbosity,
        "tone": tone,
        "optimisation_default": optimisation_default,
        "pos_count": pos,
        "neg_count": neg,
        "samples": len(rows),
    }


def preference_brief(prefs: dict[str, Any]) -> str:
    """Short human-readable hint that can be appended to LLM system prompts."""
    if not prefs or prefs.get("samples", 0) == 0:
        return ""
    parts = []
    v = prefs.get("verbosity")
    if v == "low":
        parts.append("Keep replies short and avoid long lists.")
    elif v == "high":
        parts.append("This user prefers thorough, longer explanations.")
    t = prefs.get("tone")
    if t == "technical":
        parts.append("Tone: technical, engineering-grade.")
    elif t == "friendly":
        parts.append("Tone: friendly and approachable.")
    if not parts:
        return ""
    return "User preferences (carry into your reply): " + " ".join(parts)
