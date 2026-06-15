"""Self-improving Accuracy Agent.

When a user runs a real ISTA test and reports the actual result back to the
platform, this agent:

  1. Diffs the actual outcome against the platform's predicted verdict /
     safety factor, frozen at the time of submission.
  2. Asks the reasoning LLM (Gemini 3 Pro, high temperature) for a
     root-cause hypothesis and a calibration suggestion.
  3. Persists an `AccuracyRecord` so the running calibration multiplier per
     material × packaging type is reflected in subsequent surrogate runs.

The calibration multiplier is intentionally simple: an exponentially-weighted
mean of `actual / predicted` ratios per (material, packaging_type) pair,
clamped to [0.5, 1.5]. Downstream code calls `calibration_multiplier(...)`
and multiplies the predicted safety factor by it.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..llm.gemini_client import get_gemini
from ..models import AccuracyRecord, AnalysisResult, Case


PROMPT = """You are an experienced packaging engineer reviewing a CPG drop / transit
prediction against a real ISTA test result. Your job:

1. Compute the directional error (over- or under-conservative).
2. Identify the most likely root cause from this list:
     wall_thickness_uncertainty | material_property_drift |
     stress_concentration_underestimated | fill_dynamics_ignored |
     drop_orientation_mismatch | temperature_effect_ignored | other
3. Suggest a calibration multiplier on safety factor (a single float in
   [0.5, 1.5]) that would have brought the prediction closer to reality.
4. Write a short narrative (max 3 sentences) explaining the gap and the
   takeaway for the NEXT analysis on the same material / packaging type.

INPUT:
{payload_json}

Return STRICTLY:
{{
  "root_cause": "<one of the labels above>",
  "calibration_multiplier": <float in [0.5, 1.5]>,
  "narrative": "<3 sentences>"
}}
"""


def _baseline_multiplier(predicted_sf: Optional[float], actual_verdict: str) -> float:
    """Fallback calibration multiplier when the LLM is unavailable.

    If we predicted PASS with a high SF but the actual was FAIL, the
    multiplier shrinks toward 0.6 so future predictions are flagged sooner.
    Inverse direction if we predicted FAIL but the actual passed."""
    actual_verdict = (actual_verdict or "").strip().lower()
    if actual_verdict == "fail":
        return 0.7 if predicted_sf and predicted_sf > 1.0 else 0.85
    if actual_verdict == "pass":
        return 1.15 if predicted_sf and predicted_sf < 1.0 else 1.05
    return 1.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class LearningAgent:

    def record_actual(
        self,
        db: Session,
        *,
        case: Case,
        actual_verdict: str,
        actual_failure_mode: Optional[str] = None,
        actual_drop_height_m: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> AccuracyRecord:
        """Persist an actual-test record and run the LLM root-cause pass."""
        s = case.case_summary or {}
        # Pull the latest ISTA report on the case to freeze its predictions.
        ista_row = (
            db.query(AnalysisResult)
            .filter(
                AnalysisResult.case_id == case.case_id,
                AnalysisResult.method_type.in_(("ista2a", "ista6a")),
            )
            .order_by(desc(AnalysisResult.created_at))
            .first()
        )
        predicted_verdict = None
        predicted_min_sf: Optional[float] = None
        if ista_row and isinstance(ista_row.outputs_json, dict):
            predicted_verdict = ista_row.outputs_json.get("overall_verdict")
            # Min SF across drop sequence + compression.
            sfs: list[float] = []
            for d in ista_row.outputs_json.get("drops") or []:
                if isinstance(d, dict) and d.get("safety_factor") is not None:
                    try: sfs.append(float(d["safety_factor"]))
                    except (TypeError, ValueError): pass
            t = ista_row.outputs_json.get("transit") or {}
            if t.get("compression_safety_factor") is not None:
                try: sfs.append(float(t["compression_safety_factor"]))
                except (TypeError, ValueError): pass
            predicted_min_sf = min(sfs) if sfs else None

        # LLM-driven root cause + calibration suggestion (high-temp reasoning).
        gemini = get_gemini()
        narrative = ""
        root_cause = "other"
        calibration = _baseline_multiplier(predicted_min_sf, actual_verdict)

        if gemini.available:
            payload = json.dumps({
                "material": s.get("material"),
                "packaging_type": s.get("packaging_type"),
                "test_standard": s.get("test_standard") or "ISTA 2A",
                "predicted_verdict": predicted_verdict,
                "predicted_min_safety_factor": predicted_min_sf,
                "actual_verdict": actual_verdict,
                "actual_failure_mode": actual_failure_mode,
                "actual_drop_height_m": actual_drop_height_m,
                "notes": notes,
            }, default=str, indent=2)
            raw = gemini.reason_json(PROMPT.format(payload_json=payload), "",
                                     temperature=0.9)
            if isinstance(raw, dict):
                if isinstance(raw.get("root_cause"), str):
                    root_cause = raw["root_cause"].strip().lower().replace(" ", "_")
                if isinstance(raw.get("narrative"), str) and raw["narrative"]:
                    narrative = raw["narrative"]
                try:
                    calibration = _clamp(float(raw.get("calibration_multiplier") or calibration),
                                         0.5, 1.5)
                except (TypeError, ValueError):
                    pass

        delta_sf: Optional[float] = None
        if predicted_min_sf is not None:
            implied_actual_sf = predicted_min_sf * calibration
            delta_sf = round(implied_actual_sf - predicted_min_sf, 3)

        rec = AccuracyRecord(
            case_id=case.case_id,
            material_name=s.get("material"),
            packaging_type=s.get("packaging_type"),
            test_standard=s.get("test_standard"),
            predicted_verdict=predicted_verdict,
            predicted_min_sf=predicted_min_sf,
            actual_verdict=actual_verdict,
            actual_failure_mode=actual_failure_mode,
            actual_drop_height_m=actual_drop_height_m,
            notes=notes,
            delta_min_sf=delta_sf,
            calibration_multiplier=round(calibration, 3),
            root_cause=root_cause,
            learning_narrative=narrative,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec

    def calibration_multiplier(
        self,
        db: Session,
        *,
        material_name: Optional[str],
        packaging_type: Optional[str],
    ) -> float:
        """Running calibration multiplier for the (material, packaging_type)
        pair, computed as the exponentially-weighted geometric mean of the
        last few accuracy records' multipliers. Clamped to [0.5, 1.5]."""
        q = db.query(AccuracyRecord).order_by(desc(AccuracyRecord.created_at))
        if material_name:
            q = q.filter(AccuracyRecord.material_name == material_name)
        if packaging_type:
            q = q.filter(AccuracyRecord.packaging_type == packaging_type)
        rows: list[AccuracyRecord] = q.limit(8).all()
        if not rows:
            return 1.0
        # Decay weights: most recent weighted 1.0, then 0.6, 0.4 ...
        log_sum = 0.0
        weight_sum = 0.0
        import math
        for i, r in enumerate(rows):
            m = float(r.calibration_multiplier or 1.0)
            w = 0.6 ** i
            log_sum += math.log(max(m, 1e-3)) * w
            weight_sum += w
        return round(_clamp(math.exp(log_sum / weight_sum), 0.5, 1.5), 3)

    def summary(self, db: Session, *, case_id: Optional[str] = None) -> dict[str, Any]:
        """Lightweight summary the UI uses on the sign-off / results pages."""
        q = db.query(AccuracyRecord).order_by(desc(AccuracyRecord.created_at))
        if case_id:
            q = q.filter(AccuracyRecord.case_id == case_id)
        rows = q.limit(20).all()
        return {
            "count": len(rows),
            "records": [
                {
                    "record_id": r.record_id,
                    "material_name": r.material_name,
                    "packaging_type": r.packaging_type,
                    "predicted_verdict": r.predicted_verdict,
                    "predicted_min_sf": r.predicted_min_sf,
                    "actual_verdict": r.actual_verdict,
                    "actual_failure_mode": r.actual_failure_mode,
                    "delta_min_sf": r.delta_min_sf,
                    "calibration_multiplier": r.calibration_multiplier,
                    "root_cause": r.root_cause,
                    "learning_narrative": r.learning_narrative,
                    "created_at": r.created_at.isoformat(),
                } for r in rows
            ],
        }
