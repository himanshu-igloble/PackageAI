"""Reasoning / Self-Check Agent — backed by Gemini (the reasoning tier).

Implements the section 9.6 second pass: takes the orchestrator's full analysis
snapshot (material, geometry, transit, calculations, surrogate risk map, draft
report) and asks Gemini to (a) flag any inconsistencies, unsupported claims,
or missing-data risks, and (b) generate a short, plain-language engineering
narrative that augments — never replaces — the deterministic results.

Outputs are stored alongside the draft report so a human reviewer can see both
the numbers and the LLM's commentary side-by-side.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..llm.gemini_client import get_gemini
from .base import GROUND_RULES


SYSTEM_PROMPT = f"""You are the Reasoning Agent. {GROUND_RULES}

You are doing a low-temperature engineering self-check on an analysis snapshot.
Do NOT invent values. Use ONLY the data in the snapshot. Your job:

1) Check for issues:
   - missing units
   - inconsistent assumptions across sections (e.g. wall thickness used in calc not matching geometry)
   - claims more confident than the underlying confidence labels allow
   - safety factors flagged as risk but downplayed in the narrative
2) Produce a short engineering narrative (max 6 sentences) that interprets the
   results for a packaging engineer. Be conservative. Reference confidence labels
   exactly as given.
3) Recommend the top 3 next steps a human should take (test, refine input, etc.).

Return STRICTLY a JSON object with keys:
{{
  "ok": boolean,                         // false if any blocking issue
  "issues":   [string, ...],             // each item is a concrete issue
  "warnings": [string, ...],             // non-blocking concerns
  "narrative": string,                   // engineering interpretation
  "recommended_next_steps": [string, ...]
}}
"""


@dataclass
class ReasoningResult:
    ok: bool = True
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    narrative: str = ""
    recommended_next_steps: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class ReasoningAgent:
    # ── ISTA verdict cross-check (Gemini 3 Pro) ─────────────────────────────

    def cross_check_ista(self, ista_report: dict, *, label: str = "ISTA 2A") -> dict:
        """Sanity-check the deterministic verdict against engineering reality.

        Returns `{"agrees": bool, "concern": str|None}`. We never silently
        flip the deterministic verdict — if the LLM disagrees, the calling
        code surfaces a warning to the engineer who reviews it."""
        gemini = get_gemini()
        if not gemini.available or not ista_report:
            return {"agrees": True, "concern": None}
        prompt = (
            f"You are sanity-checking a deterministic {label} engineering verdict. "
            "Given the test inputs and computed safety factors below, would a senior "
            "packaging engineer expect THIS result in real-world testing? "
            "Reply with STRICTLY a JSON object: "
            '{"agrees": true|false, "concern": "<one short sentence; null if agrees>"}'
        )
        try:
            raw = gemini.reason_json(prompt, json.dumps(ista_report, default=str)[:4000],
                                     temperature=0.05)
            return {
                "agrees": bool(raw.get("agrees", True)),
                "concern": (raw.get("concern") if not raw.get("agrees", True) else None),
            }
        except Exception:
            return {"agrees": True, "concern": None}

    # ── full snapshot self-check (existing) ─────────────────────────────────

    def verify(self, snapshot: dict[str, Any]) -> ReasoningResult:
        """snapshot = orchestrator.execute_approved_plan() output."""
        gemini = get_gemini()
        # Trim snapshot to the fields the reasoning pass needs (keeps prompt tight)
        trimmed = {
            "case_summary": snapshot.get("case_id") and snapshot.get("material", {}).get("name"),
            "material": snapshot.get("material"),
            "geometry": snapshot.get("geometry"),
            "transit": snapshot.get("transit"),
            "calculations": snapshot.get("calculations"),
            "risk_map": snapshot.get("risk_map"),
            "report_body_excerpt": (snapshot.get("report") or {}).get("body_markdown", "")[:2000],
        }
        user = json.dumps(trimmed, default=str, indent=2)
        # Gemini 3 Pro (reasoning role) does this verification pass.
        raw = gemini.reason_json(SYSTEM_PROMPT, user, temperature=0.05)

        # Defensive parsing
        ok = bool(raw.get("ok", True)) and not raw.get("issues")
        return ReasoningResult(
            ok=ok,
            issues=list(raw.get("issues") or []),
            warnings=list(raw.get("warnings") or []),
            narrative=str(raw.get("narrative") or raw.get("explanation") or ""),
            recommended_next_steps=list(raw.get("recommended_next_steps") or []),
            raw=raw,
        )
