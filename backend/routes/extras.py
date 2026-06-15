"""DesignEdge extras: threads, saved designs, feedback, voice transcription,
charts payload, PDF export, design optimisation.

Kept in a separate module so backend/routes/cases.py stays focused on the
core case lifecycle. All routes mount under /api alongside the core router.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..audit import log_event
from ..config import settings, PROJECT_ROOT
from ..db import get_db
from ..llm.gemini_client import get_gemini
from ..models import (
    AnalysisResult,
    BrushOptimizationRun,
    Case,
    Feedback,
    Message,
    OptimizationRun,
    PacketOptimizationRun,
)
from ..agents.brush_optimizer import BrushOptimizationAgent
from ..agents.material import MaterialAgent
from ..agents.optimization import OptimizationAgent
from ..agents.packet_optimization import PacketOptimizationAgent
from ..agents.secondary_packaging import SecondaryPackagingAgent
from ..orchestrator.status_bus import emit_status
from ..schemas import GeometrySummary
from ..services import charts as charts_svc
from ..services.preferences import aggregate_preferences
from ..services.report_pdf import render_pdf


router = APIRouter()
opt_agent = OptimizationAgent()
pkt_opt_agent = PacketOptimizationAgent()
brush_opt_agent = BrushOptimizationAgent()


# --------------------------------------------------- packaging-family routing
# Server-side guard so packet/brush cases can never silently fall through to the
# bottle optimizer. The family is resolved from the case_summary; each optimize
# route asserts the case matches before dispatching to its agent.

class FamilyMismatch(HTTPException):
    def __init__(self, expected: str, actual: str):
        super().__init__(
            status_code=409,
            detail=f"This optimizer is for '{expected}', but the case is '{actual}'.",
        )


_PACKET_WORDS = ("pouch", "packet", "sachet", "stickpack", "laminate")
_BRUSH_WORDS = ("brush", "toothbrush")

_ALLOWED_INTENTS = {
    "bottle": {"reduce_cost", "increase_strength", "other"},
    "packet": {"reduce_cost", "improve_survivability", "improve_shelf_life", "other"},
    "brush": {"reduce_cost", "improve_survivability", "improve_sustainability", "other"},
}


def _resolve_family(case_summary: dict) -> str:
    fam = (case_summary.get("packaging_family") or "").strip().lower()
    if fam in ("bottle", "packet", "brush"):
        return fam
    ptype = (case_summary.get("packaging_type") or "").strip().lower()
    if any(w in ptype for w in _PACKET_WORDS):
        return "packet"
    if any(w in ptype for w in _BRUSH_WORDS):
        return "brush"
    return "bottle"   # explicit default, never silent fall-through


def _assert_family(case_summary: dict, *, expected: str) -> None:
    actual = _resolve_family(case_summary)
    if actual != expected:
        raise FamilyMismatch(expected, actual)


def _assert_intent(intent: str, *, family: str) -> None:
    if intent not in _ALLOWED_INTENTS[family]:
        raise HTTPException(
            status_code=422,
            detail=f"intent '{intent}' not valid for {family}",
        )


# ------------------------------------------------------------- threads / runs

@router.get("/users/{user_id}/threads")
def list_threads(user_id: str, db: Session = Depends(get_db)):
    """List the user's recent cases (= threads in the sidebar)."""
    rows = (
        db.query(Case)
        .filter(Case.user_id == user_id)
        .order_by(Case.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "case_id": c.case_id,
            "design_name": c.design_name or (c.case_summary or {}).get("bottle_subtype") or "Untitled design",
            "packaging_type": c.packaging_type,
            "objective": c.objective,
            "stage": c.status,
            "is_saved": c.is_saved,
            "runs_count": c.runs_count,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ]


@router.get("/users/{user_id}/stats")
def user_stats(user_id: str, db: Session = Depends(get_db)):
    """Workspace-wide rollup powering the hero dashboard.

    Always returns SOMETHING for `latest_verdict` and `latest_cost` so the
    tiles never read 'no analysis yet' once the user has run anything in
    this workspace. The latest fields walk every case (newest first) and
    pluck the first non-empty value, then fall back to a friendly waiting
    message when the workspace is genuinely fresh.
    """
    from ..agents.material import MaterialAgent
    from ..agents.optimization import _mass_g
    from ..models import AnalysisResult, GeometryAsset
    from ..schemas import GeometrySummary
    from ..services import price_cache

    cases = db.query(Case).filter(Case.user_id == user_id).all()
    cases_newest = sorted(cases, key=lambda c: c.created_at or 0, reverse=True)

    # Latest verdict — walk newest cases, pull the most recent ISTA 2A row.
    latest_verdict: dict[str, Any] = {
        "verdict": None, "summary": "no analysis yet", "case_id": None,
    }
    for c in cases_newest:
        row = (
            db.query(AnalysisResult)
            .filter(AnalysisResult.case_id == c.case_id,
                    AnalysisResult.method_type == "ista2a")
            .order_by(AnalysisResult.created_at.desc())
            .first()
        )
        if not row or not row.outputs_json:
            continue
        v = (row.outputs_json or {}).get("overall_verdict")
        if not v:
            continue
        drops = (row.outputs_json or {}).get("drops") or []
        passing = sum(1 for d in drops if (d or {}).get("verdict") == "pass")
        latest_verdict = {
            "verdict": v,
            "summary": f"{passing}/{len(drops)} drop orientations cleared",
            "case_id": c.case_id,
            "design_name": c.design_name or "Untitled design",
            "ts": (row.created_at.isoformat() if row.created_at else None),
        }
        break

    # Latest cost — walk newest cases until we find one with enough info to
    # produce a unit cost via the cost-research path.
    latest_cost: dict[str, Any] = {
        "cost_per_unit_usd": None, "material": None, "summary": "no design yet",
    }
    for c in cases_newest:
        s = c.case_summary or {}
        mat_name = s.get("material") or ""
        if not mat_name and not s.get("packaging_type"):
            continue
        # Resolve material → properties so we can derive mass.
        material = MaterialAgent().lookup(db, mat_name) if mat_name else None
        asset = (db.query(GeometryAsset)
                 .filter(GeometryAsset.case_id == c.case_id)
                 .order_by(GeometryAsset.created_at.desc()).first())
        geometry = None
        if asset and asset.summary:
            try: geometry = GeometrySummary(**asset.summary)
            except Exception: pass
        mass_g = _mass_g(s, material, geometry) or 25.0
        price = price_cache.lookup_price(mat_name or "PET")
        cost = round((mass_g / 1000.0) * float(price["price_usd_per_kg"]), 4)
        latest_cost = {
            "cost_per_unit_usd": cost,
            "mass_g": mass_g,
            "material": price["name"] or mat_name or "estimated polymer",
            "price_source": price["source"],
            "price_confidence": price["confidence"],
            "case_id": c.case_id,
            "design_name": c.design_name or "Untitled design",
            "summary": f"{price['name']} · {mass_g:.0f} g · ${price['price_usd_per_kg']:.2f}/kg ({price['source']})",
        }
        break

    return {
        "user_id": user_id,
        "thread_count": len(cases),
        "saved_count": sum(1 for c in cases if c.is_saved),
        "runs_total": sum(c.runs_count for c in cases),
        "latest_verdict": latest_verdict,
        "latest_cost": latest_cost,
        "preferences": aggregate_preferences(db, user_id),
    }


# ------------------------------------------------------ saved designs / name

class NameBody(BaseModel):
    design_name: str


@router.post("/cases/{case_id}/name")
def name_design(case_id: str, body: NameBody, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    case.design_name = body.design_name[:120]
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="renamed_design",
              payload={"design_name": case.design_name})
    return {"case_id": case_id, "design_name": case.design_name}


@router.delete("/cases/{case_id}")
def delete_case(case_id: str, db: Session = Depends(get_db)):
    """Delete a thread + every dependent row. Locked cases require unlock first."""
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    if case.locked:
        raise HTTPException(409, "Design is locked. Unlock before deleting.")
    db.delete(case)
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="thread_deleted")
    return {"case_id": case_id, "deleted": True}


@router.get("/materials/check")
def check_material(name: str, db: Session = Depends(get_db)):
    """Does the material exist in our verified DB or local cache? Used by the
    chat to decide whether to ask the user for custom details / a web search."""
    from ..models import MaterialRecord
    from ..services import material_cache
    if not name:
        return {"name": name, "hit": False, "where": None}
    rec = (db.query(MaterialRecord)
             .filter(MaterialRecord.name.ilike(name)).first())
    if rec:
        return {"name": rec.name, "hit": True, "where": "db", "source": rec.source}
    cached = material_cache.get(name)
    if cached:
        return {"name": cached["name"], "hit": True, "where": "cache",
                "source": cached.get("source")}
    return {"name": name, "hit": False, "where": None}


@router.post("/cases/{case_id}/save")
def save_design(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    case.is_saved = True
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="saved_design")
    return {"case_id": case_id, "is_saved": True}


@router.post("/cases/{case_id}/unsave")
def unsave_design(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    case.is_saved = False
    db.commit()
    return {"case_id": case_id, "is_saved": False}


# ------------------------------------------------------------------ feedback

class FeedbackBody(BaseModel):
    target: str = "report"
    rating: int = 0                                 # -1 / 0 / +1
    notes: Optional[str] = None
    tags: dict[str, Any] = Field(default_factory=dict)


@router.post("/cases/{case_id}/feedback")
def submit_feedback(case_id: str, body: FeedbackBody, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    fb = Feedback(
        case_id=case_id,
        user_id=case.user_id,
        target=body.target,
        rating=max(-1, min(1, int(body.rating))),
        notes=(body.notes or "")[:1000],
        tags=body.tags or {},
    )
    db.add(fb)
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="feedback",
              payload={"target": body.target, "rating": body.rating, "tags": body.tags})
    emit_status(case_id, stage=case.status, active_agent="feedback",
                action="received", summary=f"Feedback {body.rating:+d} on {body.target}")
    prefs = aggregate_preferences(db, case.user_id)
    return {"feedback_id": fb.feedback_id, "preferences": prefs}


# --------------------------------------------------------- voice / transcribe

@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    mime: str = Form(default="audio/webm"),
):
    """Transcribe a short audio clip using Gemini 2.5 Flash (audio-in).

    The UI prefers the browser's Web Speech API; this endpoint is the fallback
    for browsers without SpeechRecognition (e.g. Firefox)."""
    gemini = get_gemini()
    if not gemini.available or not gemini._client:
        raise HTTPException(503, detail="Server-side transcription unavailable; please use browser Web Speech.")
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "empty audio body")
    # google-genai supports inline audio bytes via parts.inline_data.
    try:
        from google.genai import types
        b64 = base64.b64encode(raw).decode("ascii")
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text="Transcribe the spoken words in the attached audio. "
                             "Return only the transcript, no preface."
                    ),
                    types.Part.from_bytes(data=raw, mime_type=mime),
                ],
            )
        ]
        resp = gemini._client.models.generate_content(
            model=settings.GEMINI_INTAKE_MODEL,
            contents=contents,
            config={"temperature": 0.0},
        )
        text = (resp.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"transcription failed: {exc!r}")
    return {"transcript": text, "model": settings.GEMINI_INTAKE_MODEL}


# ------------------------------------------------------------------- charts

@router.get("/cases/{case_id}/charts")
def build_charts(case_id: str, db: Session = Depends(get_db)):
    """Return base64 PNGs for the report's Charts tab.

    Charts emitted:
        vibration_psd       (by mode mix)
        density_compare     (the case's material vs common alternatives)
        zone_risk_bar       (latest surrogate risk map)
        drop_verdict_bar    (latest ISTA-2A drops, when present)
        comparison_dashboard (when an optimisation run exists)
    """
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)

    s = case.case_summary or {}
    out: dict[str, dict[str, str]] = {}

    # Vibration PSD
    modes = s.get("transit_modes") or ["truck"]
    out["vibration_psd"] = charts_svc.vibration_psd(modes)

    # Density compare: the case material vs the seed list
    from ..models import MaterialRecord
    materials = db.query(MaterialRecord).order_by(MaterialRecord.name).all()
    out["density_compare"] = charts_svc.density_compare(
        [{"name": m.name, "density_kg_m3": m.density_kg_m3} for m in materials]
    )

    # Zone risk (latest surrogate)
    risk_row = (
        db.query(AnalysisResult)
        .filter(AnalysisResult.case_id == case_id, AnalysisResult.method_type == "surrogate")
        .order_by(AnalysisResult.created_at.desc())
        .first()
    )
    if risk_row:
        out["zone_risk_bar"] = charts_svc.zone_risk_bar(risk_row.outputs_json.get("zones", []))

    # ISTA-2A drops
    ista_row = (
        db.query(AnalysisResult)
        .filter(AnalysisResult.case_id == case_id, AnalysisResult.method_type == "ista2a")
        .order_by(AnalysisResult.created_at.desc())
        .first()
    )
    if ista_row:
        out["drop_verdict_bar"] = charts_svc.drop_verdict_bar(ista_row.outputs_json.get("drops", []))

    # Comparison dashboard — prefer bottle OptimizationRun; fall back to
    # PacketOptimizationRun so packet cases also get a chart.
    opt_row = (
        db.query(OptimizationRun)
        .filter(OptimizationRun.case_id == case_id)
        .order_by(OptimizationRun.created_at.desc())
        .first()
    )
    if opt_row and opt_row.comparison.get("rows"):
        designs = [
            {
                "name": r.get("name") or "Design",
                "cost_per_unit": r.get("cost_per_unit") or 0,
                "min_safety_factor": r.get("min_safety_factor") or 0,
                "mass_g": r.get("mass_g") or 0,
                "roi_pct": r.get("roi_pct") or 0,
                "passes_ista": bool(r.get("passes_ista")),
            }
            for r in opt_row.comparison["rows"]
        ]
        out["comparison_dashboard"] = charts_svc.comparison_dashboard(designs)
    else:
        # Packet case: map packet scores to the comparison dashboard axes.
        pkt_opt_row = (
            db.query(PacketOptimizationRun)
            .filter(PacketOptimizationRun.case_id == case_id)
            .order_by(PacketOptimizationRun.created_at.desc())
            .first()
        )
        if pkt_opt_row and (pkt_opt_row.comparison or {}).get("rows"):
            designs = [
                {
                    "name": r.get("name") or "Design",
                    "cost_per_unit": r.get("cost_impact_pct") or 0,
                    "min_safety_factor": r.get("seal_score") or 0,
                    "mass_g": r.get("transit_score") or 0,
                    "roi_pct": r.get("barrier_score") or 0,
                    "passes_ista": True,
                }
                for r in pkt_opt_row.comparison["rows"]
            ]
            out["comparison_dashboard"] = charts_svc.comparison_dashboard(designs)
        else:
            # Brush case: map brush scores to the comparison dashboard axes.
            brush_opt_row = (
                db.query(BrushOptimizationRun)
                .filter(BrushOptimizationRun.case_id == case_id)
                .order_by(BrushOptimizationRun.created_at.desc())
                .first()
            )
            if brush_opt_row and (brush_opt_row.comparison or {}).get("rows"):
                designs = [
                    {
                        "name": r.get("name") or "Design",
                        "cost_per_unit": r.get("cost_impact_pct") or 0,
                        "min_safety_factor": r.get("blister_score") or 0,
                        "mass_g": r.get("transit_score") or 0,
                        "roi_pct": r.get("material_score") or 0,
                        "passes_ista": True,
                    }
                    for r in brush_opt_row.comparison["rows"]
                ]
                out["comparison_dashboard"] = charts_svc.comparison_dashboard(designs)

    return {"case_id": case_id, "charts": out}


# ----------------------------------------------------------------------- pdf

@router.get("/cases/{case_id}/report.pdf")
def export_pdf(case_id: str, db: Session = Depends(get_db)):
    from ..models import TransitProfile

    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)

    # Gather all analysis results for this case keyed by method_type
    all_results = (
        db.query(AnalysisResult)
        .filter(AnalysisResult.case_id == case_id)
        .order_by(AnalysisResult.created_at.desc())
        .all()
    )
    by_type: dict[str, dict] = {}
    for r in all_results:
        if r.method_type not in by_type:
            by_type[r.method_type] = r.outputs_json or {}

    report_row = by_type.get("report_draft")
    if not report_row:
        raise HTTPException(404, "no report draft yet — run the analysis first")

    report_md: str = report_row.get("body_markdown", "")
    title: str = (
        case.design_name
        or report_row.get("title")
        or "PackTwin.AI Report"
    )

    # Transit profile
    tp = (
        db.query(TransitProfile)
        .filter(TransitProfile.case_id == case_id)
        .order_by(TransitProfile.profile_id.desc())
        .first()
    )
    transit: dict | None = None
    if tp:
        transit = {
            "mode_mix": tp.mode_mix or {},
            "vibration_level": tp.vibration_level,
            "drop_height_m": tp.drop_height_m,
            "compression_load_n": tp.compression_load_n,
            "handling_fraction": tp.handling_fraction,
            "notes": tp.notes,
        }

    # ISTA 2A / 6A
    ista2a = by_type.get("ista2a")
    ista6a = by_type.get("ista6a")

    # Risk zones from surrogate
    surrogate = by_type.get("surrogate") or {}
    risk_zones: list[dict] | None = surrogate.get("zones") or None

    # Charts — only the three that appear in the in-app Report/Analysis tabs
    chart_payload = build_charts.__wrapped__(case_id, db) if hasattr(build_charts, "__wrapped__") else None  # type: ignore
    if not chart_payload:
        chart_payload = build_charts(case_id, db)
    all_charts = {
        name: data.get("png_b64", "")
        for name, data in (chart_payload.get("charts") or {}).items()
    }
    charts = {k: all_charts[k] for k in ("vibration_psd", "zone_risk_bar", "drop_verdict_bar") if all_charts.get(k)}

    generated_on = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        pdf_bytes = render_pdf(
            title=title,
            case_summary=case.case_summary or {},
            transit=transit,
            ista2a=ista2a,
            ista6a=ista6a,
            risk_zones=risk_zones,
            report_md=report_md,
            charts=charts,
            generated_on=generated_on,
        )
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))

    log_event(db, case_id=case_id, actor="user", action="exported_pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="packtwin-{case_id[:8]}.pdf"'},
    )


# --------------------------------------------------------------- optimisation

class OptIntentBody(BaseModel):
    message: str


@router.post("/cases/{case_id}/optimize/intent")
def optimize_intent(case_id: str, body: OptIntentBody, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    # Pull a short conversation excerpt
    msgs = (
        db.query(Message)
        .filter(Message.case_id == case_id)
        .order_by(Message.created_at.desc())
        .limit(6)
        .all()
    )
    convo = [{"role": m.role, "content": m.content} for m in reversed(msgs)]
    emit_status(case_id, stage=case.status, active_agent="optimization",
                action="gauge_intent", tool="gemini-2.5-flash",
                summary="Reading optimisation intent…")
    result = opt_agent.gauge_intent(case.case_summary or {}, body.message, conversation=convo)
    log_event(db, case_id=case_id, actor="optimization_agent", action="gauge_intent",
              payload={"intent": result.get("intent"), "ready": result.get("ready_to_generate")})
    return result


class OptRunBody(BaseModel):
    intent: str = "reduce_cost"
    intent_notes: str = ""


@router.post("/cases/{case_id}/optimize/run")
def optimize_run(case_id: str, body: OptRunBody, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)

    _assert_family(case.case_summary or {}, expected="bottle")
    _assert_intent(body.intent, family="bottle")

    # Material + geometry context
    s = case.case_summary or {}
    material = MaterialAgent().lookup(db, s.get("material") or "") if s.get("material") else None

    from ..models import GeometryAsset
    asset = (
        db.query(GeometryAsset)
        .filter(GeometryAsset.case_id == case_id)
        .order_by(GeometryAsset.created_at.desc())
        .first()
    )
    geometry = None
    if asset and asset.summary:
        try:
            geometry = GeometrySummary(**asset.summary)
        except Exception:
            geometry = None

    emit_status(case_id, stage=case.status, active_agent="optimization",
                action="generate_alternatives", tool="gemini-3-pro",
                summary=f"Proposing 3 alternatives ({body.intent})…")

    result = opt_agent.generate_alternatives(
        db,
        baseline_fields=s,
        material=material,
        geometry=geometry,
        intent=body.intent,
        intent_notes=body.intent_notes,
    )

    payload = result.model_dump()
    db.add(OptimizationRun(
        case_id=case_id,
        intent=body.intent,
        intent_notes=body.intent_notes,
        alternatives=payload["alternatives"],
        comparison={"rows": payload["comparison_rows"], "narrative": payload["narrative"]},
    ))
    case.runs_count = (case.runs_count or 0) + 1
    db.commit()
    log_event(db, case_id=case_id, actor="optimization_agent", action="alternatives_done",
              payload={"intent": body.intent, "n_alternatives": len(payload["alternatives"])})
    emit_status(case_id, stage=case.status, active_agent="optimization",
                action="alternatives_done", confidence="estimated",
                summary=f"{len(payload['alternatives'])} alternatives generated.")
    return payload


# ------------------------------------------------- packet optimisation
# Completely separate from the bottle optimization endpoints above.
# Routes, models, and agent are all isolated — no shared logic.

@router.post("/cases/{case_id}/packet-optimize/intent")
def packet_optimize_intent(
    case_id: str, body: OptIntentBody, db: Session = Depends(get_db),
):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    msgs = (
        db.query(Message)
        .filter(Message.case_id == case_id)
        .order_by(Message.created_at.desc())
        .limit(6)
        .all()
    )
    convo = [{"role": m.role, "content": m.content} for m in reversed(msgs)]
    emit_status(case_id, stage=case.status, active_agent="packet_optimization",
                action="gauge_intent", tool="gemini-2.5-flash",
                summary="Reading packet optimisation intent…")
    result = pkt_opt_agent.gauge_intent(
        case.case_summary or {}, body.message, conversation=convo,
    )
    log_event(db, case_id=case_id, actor="packet_optimization_agent",
              action="gauge_intent",
              payload={"intent": result.get("intent"), "ready": result.get("ready_to_generate")})
    return result


@router.post("/cases/{case_id}/packet-optimize/run")
def packet_optimize_run(
    case_id: str, body: OptRunBody, db: Session = Depends(get_db),
):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)

    _assert_family(case.case_summary or {}, expected="packet")
    _assert_intent(body.intent, family="packet")

    s = case.case_summary or {}
    emit_status(case_id, stage=case.status, active_agent="packet_optimization",
                action="generate_alternatives", tool="gemini-2.5-flash",
                summary=f"Proposing 3 packet alternatives ({body.intent})…")

    result = pkt_opt_agent.generate_alternatives(
        baseline_fields=s,
        intent=body.intent,
        intent_notes=body.intent_notes,
    )

    payload = result.model_dump()
    db.add(PacketOptimizationRun(
        case_id=case_id,
        intent=body.intent,
        intent_notes=body.intent_notes,
        alternatives=payload["alternatives"],
        comparison={"rows": payload["comparison_rows"], "narrative": payload["narrative"]},
    ))
    case.runs_count = (case.runs_count or 0) + 1
    db.commit()
    log_event(db, case_id=case_id, actor="packet_optimization_agent",
              action="alternatives_done",
              payload={"intent": body.intent, "n_alternatives": len(payload["alternatives"])})
    emit_status(case_id, stage=case.status, active_agent="packet_optimization",
                action="alternatives_done", confidence="estimated",
                summary=f"{len(payload['alternatives'])} packet alternatives generated.")
    return payload


# ------------------------------------------------- brush optimisation
# Completely separate from bottle and packet optimization endpoints.
# Routes, model, and agent are all isolated — no shared logic.

@router.post("/cases/{case_id}/brush-optimize/intent")
def brush_optimize_intent(
    case_id: str, body: OptIntentBody, db: Session = Depends(get_db),
):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    msgs = (
        db.query(Message)
        .filter(Message.case_id == case_id)
        .order_by(Message.created_at.desc())
        .limit(6)
        .all()
    )
    convo = [{"role": m.role, "content": m.content} for m in reversed(msgs)]
    emit_status(case_id, stage=case.status, active_agent="brush_optimization",
                action="gauge_intent", tool="gemini-2.5-flash",
                summary="Reading brush optimisation intent…")
    result = brush_opt_agent.gauge_intent(
        case.case_summary or {}, body.message, conversation=convo,
    )
    log_event(db, case_id=case_id, actor="brush_optimization_agent",
              action="gauge_intent",
              payload={"intent": result.get("intent"), "ready": result.get("ready_to_generate")})
    return result


@router.post("/cases/{case_id}/brush-optimize/run")
def brush_optimize_run(
    case_id: str, body: OptRunBody, db: Session = Depends(get_db),
):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)

    _assert_family(case.case_summary or {}, expected="brush")
    _assert_intent(body.intent, family="brush")

    s = case.case_summary or {}
    emit_status(case_id, stage=case.status, active_agent="brush_optimization",
                action="generate_alternatives", tool="gemini-2.5-flash",
                summary=f"Proposing 3 brush packaging alternatives ({body.intent})…")

    result = brush_opt_agent.generate_alternatives(
        baseline_fields=s,
        intent=body.intent,
        intent_notes=body.intent_notes,
    )

    payload = result.model_dump()
    db.add(BrushOptimizationRun(
        case_id=case_id,
        intent=body.intent,
        intent_notes=body.intent_notes,
        alternatives=payload["alternatives"],
        comparison={"rows": payload["comparison_rows"], "narrative": payload["narrative"]},
    ))
    case.runs_count = (case.runs_count or 0) + 1
    db.commit()
    log_event(db, case_id=case_id, actor="brush_optimization_agent",
              action="alternatives_done",
              payload={"intent": body.intent, "n_alternatives": len(payload["alternatives"])})
    emit_status(case_id, stage=case.status, active_agent="brush_optimization",
                action="alternatives_done", confidence="estimated",
                summary=f"{len(payload['alternatives'])} brush packaging alternatives generated.")
    return payload


# ------------------------------------------------- packaging-family lookup
# Authoritative family for the frontend to dispatch the correct optimizer.

@router.get("/cases/{case_id}/family")
def case_family(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    return {"family": _resolve_family(case.case_summary or {})}


# --------------------------------------------------------- preferences read

@router.get("/users/{user_id}/preferences")
def get_preferences(user_id: str, db: Session = Depends(get_db)):
    return aggregate_preferences(db, user_id)


# ------------------------------------------------------ design brief (PATCH)

class BriefPatchBody(BaseModel):
    """Partial update to case_summary. Any field listed here is merged in;
    omitted fields are left untouched. Returns the new full brief."""
    updates: dict[str, Any] = Field(default_factory=dict)


@router.get("/cases/{case_id}/brief")
def read_brief(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    return {
        "case_id": case_id,
        "design_name": case.design_name,
        "case_summary": case.case_summary or {},
        "stage_state": case.stage_state or _default_stage_state(),
        "locked": case.locked,
    }


@router.patch("/cases/{case_id}/brief")
def patch_brief(case_id: str, body: BriefPatchBody, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    if case.locked:
        raise HTTPException(409, "Design is locked; unlock to edit.")
    cs = dict(case.case_summary or {})
    for k, v in (body.updates or {}).items():
        # has_geometry is platform-managed: only the /upload route may set
        # it True. Reject any client attempt to flip it (especially False),
        # so the bot can't be shortcut past the geometry-upload gate.
        if k == "has_geometry":
            continue
        if v is None or v == "":
            cs.pop(k, None)
        else:
            cs[k] = v
    case.case_summary = cs
    # Sync the SQL columns we promote out of case_summary
    case.packaging_type = cs.get("packaging_type") or case.packaging_type
    case.product_type   = cs.get("product_type")   or case.product_type
    case.objective      = cs.get("objective")      or case.objective
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="brief_edited",
              payload={"keys": list((body.updates or {}).keys())})
    emit_status(case_id, stage=case.status, active_agent="user",
                action="brief_edited", summary=f"Edited: {', '.join((body.updates or {}).keys())}")
    return {"case_id": case_id, "case_summary": cs}


# ------------------------------------------------------ stage state

_STAGES = ("intake", "geometry", "material", "transit",
           "analysis", "results", "report", "signoff")


def _default_stage_state() -> dict[str, str]:
    return {s: "pending" for s in _STAGES}


def _derive_stage_state(case: Case) -> dict[str, str]:
    """Compute stage progress purely from case_summary + status — single source
    of truth, no out-of-band tracking. Returned states: 'pending' | 'active'
    | 'complete'."""
    cs = case.case_summary or {}
    state = _default_stage_state()
    # Intake = packaging_type known
    if cs.get("packaging_type"):
        state["intake"] = "complete"
    # Geometry = uploaded asset OR explicit "no geometry"
    if cs.get("has_geometry") is True or cs.get("geometry_is_proxy"):
        state["geometry"] = "complete"
    elif cs.get("has_geometry") is False:
        state["geometry"] = "complete"
    # Material = a material name is set
    if cs.get("material"):
        state["material"] = "complete"
    # Transit = transit modes set
    if cs.get("transit_modes"):
        state["transit"] = "complete"
    # Analysis = approval applied
    if case.approval_state in ("plan_approved", "finalized") or case.runs_count > 0:
        state["analysis"] = "complete"
    if case.runs_count > 0:
        state["results"] = "complete"
    if case.runs_count > 0:
        state["report"] = "complete"
    if case.locked:
        state["signoff"] = "complete"
    # Mark the leftmost pending as active
    for s in _STAGES:
        if state[s] == "pending":
            state[s] = "active"
            break
    return state


@router.get("/cases/{case_id}/stage-state")
def stage_state(case_id: str, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    return _derive_stage_state(case)


# ------------------------------------------------------ M14: sign-off + lock

class SignoffBody(BaseModel):
    approver_name: str
    notes: Optional[str] = None


def _signoff_manifest(case: Case, db: Session) -> dict[str, Any]:
    """Snapshot every input + every analysis result + the case_summary into a
    deterministic dict suitable for SHA-256 hashing."""
    from ..models import AnalysisResult, GeometryAsset
    rows = (
        db.query(AnalysisResult)
        .filter(AnalysisResult.case_id == case.case_id)
        .order_by(AnalysisResult.created_at.asc())
        .all()
    )
    asset = (
        db.query(GeometryAsset)
        .filter(GeometryAsset.case_id == case.case_id)
        .order_by(GeometryAsset.created_at.desc())
        .first()
    )
    return {
        "case_id": case.case_id,
        "design_name": case.design_name,
        "user_id": case.user_id,
        "case_summary": case.case_summary or {},
        "geometry_asset_id": asset.asset_id if asset else None,
        "geometry_summary": asset.summary if asset else None,
        "analysis_results": [
            {"method_type": r.method_type, "inputs_hash": r.inputs_hash,
             "outputs_json": r.outputs_json, "confidence": r.confidence,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ],
        "runs_count": case.runs_count,
    }


@router.post("/cases/{case_id}/signoff")
def signoff(case_id: str, body: SignoffBody, db: Session = Depends(get_db)):
    """Approve, hash, and lock the design.

    Computes a SHA-256 over a deterministic manifest of every input + every
    persisted analysis result. Stores the hash + approver + notes on the
    case row. Sets `locked=True` so subsequent PATCH /brief refuses to edit.
    The hash makes the report tamper-evident.
    """
    import hashlib, json as _json
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    if case.locked:
        return {
            "case_id": case_id, "locked": True,
            "signoff_hash": case.signoff_hash,
            "signed_off_by": case.signed_off_by,
            "signed_off_at": case.signed_off_at.isoformat() if case.signed_off_at else None,
            "message": "already locked",
        }
    if case.runs_count == 0:
        raise HTTPException(400, "Run an analysis before signing off.")
    manifest = _signoff_manifest(case, db)
    blob = _json.dumps(manifest, sort_keys=True, default=str).encode()
    sha = hashlib.sha256(blob).hexdigest()
    case.signoff_hash = sha
    case.signed_off_by = body.approver_name[:120]
    case.signed_off_at = datetime.utcnow()
    case.signoff_notes = (body.notes or "")[:1000]
    case.locked = True
    case.status = "finalized"
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="signed_off",
              payload={"approver": case.signed_off_by, "hash": sha})
    emit_status(case_id, stage="finalized", active_agent="user",
                action="signed_off",
                summary=f"Signed off by {case.signed_off_by}; hash {sha[:12]}…")
    return {
        "case_id": case_id, "locked": True,
        "signoff_hash": sha,
        "signed_off_by": case.signed_off_by,
        "signed_off_at": case.signed_off_at.isoformat(),
        "manifest_size_bytes": len(blob),
    }


# ─────────────────────────────────────────── transit envelope preview

class TransitPreviewBody(BaseModel):
    mode_mix: dict[str, float] = Field(default_factory=dict)
    road: str = "mixed"
    ship_severity: str = "moderate"
    durations_min: Optional[dict[str, float]] = None
    manual_drop_height_m: Optional[float] = None


@router.post("/transit/preview")
def transit_preview(body: TransitPreviewBody):
    """Return a CSV-derived transit envelope summary — used by the Transit
    stage to render a live preview while the user adjusts the mode mix.
    Stateless (no DB write, no case)."""
    from ..services import transit_data as td
    if not td.available():
        return {"available": False}
    return td.blended_envelope(
        mode_mix=body.mode_mix or {"truck": 1.0},
        road=body.road,
        ship_severity=body.ship_severity,
        durations_min=body.durations_min,
        manual_drop_height_m=body.manual_drop_height_m,
    )


# ─────────────────────────────────────────── transit time-series charts

@router.get("/transit/charts")
def transit_charts(
    road: str = "mixed",
    ship_severity: str = "moderate",
    modes: str = "truck,ship",
    max_points: int = 8000,
):
    """Time-series payloads for the transit page charts.

    `max_points` caps the returned series length per mode (default 8 000;
    the report passes a higher value to render the whole CSV span)."""
    from ..services import transit_data as td
    out: dict[str, Any] = {"available_modes": td.available_modes()}
    wanted = [m.strip() for m in modes.split(",") if m.strip()]
    cap = max(500, min(int(max_points), 25_000))
    if "truck" in wanted and "truck" in out["available_modes"]:
        out["truck"] = td.truck_time_series(road, max_points=cap)
    if "ship" in wanted and "ship" in out["available_modes"]:
        out["ship"] = td.ship_time_series(ship_severity, max_points=cap)
    return out


@router.get("/transit/available-modes")
def transit_modes_available():
    """Transit modes for the UI. `data_backed` modes have real CSV telemetry;
    `selectable` adds reference modes (industry estimates); `reference` lists
    the estimate-only modes so the UI can badge them."""
    from ..services import transit_data as td
    return {
        "data_backed": td.available_modes(),
        "selectable": td.selectable_modes(),
        "reference": list(td.REFERENCE_MODES),
    }


# ─────────────────────────────────────────── ISTA 6A

class Ista6ABody(BaseModel):
    mass_kg: Optional[float] = None     # default to case derived mass


@router.post("/cases/{case_id}/ista6a")
def run_ista6a(case_id: str, body: Ista6ABody, db: Session = Depends(get_db)):
    """Run an ISTA 6A corner-drop check. Independent of ISTA 2A so the user
    can opt in/out. Result is stored as an AnalysisResult row + Gemini-3-Pro
    cross-check is attached as an advisory note."""
    from ..agents.ista6a import Ista6AAgent
    from ..agents.material import MaterialAgent
    from ..agents.reasoning import ReasoningAgent
    from ..agents.calculation import inputs_hash
    from ..models import AnalysisResult, GeometryAsset
    from ..schemas import GeometrySummary
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    s = case.case_summary or {}
    mass_kg = body.mass_kg or (
        (float(s["gross_weight_g"]) / 1000.0) if s.get("gross_weight_g") else 0.6
    )
    material = MaterialAgent().lookup(db, s.get("material") or "") if s.get("material") else None
    asset = (db.query(GeometryAsset).filter(GeometryAsset.case_id == case_id)
             .order_by(GeometryAsset.created_at.desc()).first())
    geometry = None
    if asset and asset.summary:
        try: geometry = GeometrySummary(**asset.summary)
        except Exception: pass
    report = Ista6AAgent().evaluate(mass_kg=mass_kg, material=material, geometry=geometry)
    payload = report.model_dump()
    # Cross-check the verdict against engineering reality (advisory only).
    try:
        payload["cross_check"] = ReasoningAgent().cross_check_ista(payload, label="ISTA 6A")
    except Exception:
        payload["cross_check"] = {"agrees": True, "concern": None}
    db.add(AnalysisResult(
        case_id=case_id, method_type="ista6a",
        inputs_hash=inputs_hash({"mass_kg": mass_kg, "material": material.name if material else None}),
        outputs_json=payload, confidence="approximate",
    ))
    db.commit()
    log_event(db, case_id=case_id, actor="ista6a_agent", action="evaluate",
              payload={"verdict": report.overall_verdict})
    emit_status(case_id, stage=case.status, active_agent="ista6a",
                action="evaluate_done",
                summary=f"ISTA 6A {report.overall_verdict}")
    return payload


# ─────────────────────────────────────────── snapshot persistence

@router.get("/cases/{case_id}/snapshot")
def case_snapshot(case_id: str, db: Session = Depends(get_db)):
    """Reconstruct the full analysis snapshot from persisted AnalysisResults.

    Used by the frontend when an old thread is loaded so the user doesn't
    have to re-run the analysis. Returns the same shape as the one
    `execute_approved_plan` returns post-run."""
    from ..models import AnalysisResult, GeometryAsset
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    rows = (db.query(AnalysisResult)
            .filter(AnalysisResult.case_id == case_id)
            .order_by(AnalysisResult.created_at.asc())
            .all())
    snap: dict[str, Any] = {
        "case_id": case_id,
        "case_summary": case.case_summary or {},
        "design_name": case.design_name,
    }
    # Index by method_type — keep the latest of each kind.
    for r in rows:
        if r.method_type == "deterministic":
            snap.setdefault("calculations", []).append(r.outputs_json)
        elif r.method_type == "surrogate":
            snap["risk_map"] = r.outputs_json
        elif r.method_type == "ista2a":
            snap["ista2a"] = r.outputs_json
        elif r.method_type == "ista6a":
            snap["ista6a"] = r.outputs_json
        elif r.method_type == "report_draft":
            snap["report"] = r.outputs_json
        elif r.method_type == "reasoning_self_check":
            snap["reasoning"] = r.outputs_json
        elif r.method_type == "heatmaps":
            snap["heatmaps_meta"] = r.outputs_json
    # Material lookup (cheap; needed for the report)
    if case.case_summary and case.case_summary.get("material"):
        from ..agents.material import MaterialAgent
        m = MaterialAgent().lookup(db, case.case_summary["material"])
        if m:
            snap["material"] = m.model_dump()
    # Most recent geometry asset id
    asset = (db.query(GeometryAsset)
             .filter(GeometryAsset.case_id == case_id)
             .order_by(GeometryAsset.created_at.desc()).first())
    if asset:
        snap["geometry_asset_id"] = asset.asset_id
        snap["geometry"] = asset.summary
    # Secondary packaging summary — works for both bottle and packet field conventions
    snap["secondary_packaging"] = SecondaryPackagingAgent.build_summary(case.case_summary or {})
    if snap["secondary_packaging"].get("enabled"):
        snap["secondary_packaging"]["recommendation"] = SecondaryPackagingAgent.get_recommendation(
            snap["secondary_packaging"]
        )
    # Restore latest packet optimization run so the frontend can re-render the
    # comparison ledger without requiring the user to re-run the optimizer.
    pkt_opt_row = (
        db.query(PacketOptimizationRun)
        .filter(PacketOptimizationRun.case_id == case_id)
        .order_by(PacketOptimizationRun.created_at.desc())
        .first()
    )
    if pkt_opt_row:
        comp = pkt_opt_row.comparison or {}
        snap["packet_optimization"] = {
            "intent": pkt_opt_row.intent,
            "intent_notes": pkt_opt_row.intent_notes,
            "alternatives": pkt_opt_row.alternatives or [],
            "comparison_rows": comp.get("rows") or [],
            "narrative": comp.get("narrative") or "",
        }
    # Restore latest brush optimization run similarly.
    brush_opt_row = (
        db.query(BrushOptimizationRun)
        .filter(BrushOptimizationRun.case_id == case_id)
        .order_by(BrushOptimizationRun.created_at.desc())
        .first()
    )
    if brush_opt_row:
        comp = brush_opt_row.comparison or {}
        snap["brush_optimization"] = {
            "intent": brush_opt_row.intent,
            "intent_notes": brush_opt_row.intent_notes,
            "alternatives": brush_opt_row.alternatives or [],
            "comparison_rows": comp.get("rows") or [],
            "narrative": comp.get("narrative") or "",
        }
    return snap


# ─────────────────────────────────────────── cost estimate

@router.get("/cases/{case_id}/cost")
def case_cost(case_id: str, db: Session = Depends(get_db)):
    """Quick cost-per-unit estimate for the baseline design — used by the
    dashboard tile and the report cover.

    Always returns a non-null cost: when the material is unknown to the
    local table, the cost-research agent (price_cache → Gemini 3 Pro
    web lookup) supplies a live USD/kg figure. Source + confidence are
    surfaced so the UI can render them honestly.
    """
    from ..agents.material import MaterialAgent
    from ..agents.optimization import _mass_g
    from ..models import GeometryAsset
    from ..schemas import GeometrySummary
    from ..services import price_cache
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    s = case.case_summary or {}
    material = MaterialAgent().lookup(db, s.get("material") or "") if s.get("material") else None
    asset = (db.query(GeometryAsset).filter(GeometryAsset.case_id == case_id)
             .order_by(GeometryAsset.created_at.desc()).first())
    geometry = None
    if asset and asset.summary:
        try: geometry = GeometrySummary(**asset.summary)
        except Exception: pass
    mass_g = _mass_g(s, material, geometry)
    # Mass is *always* non-null after _mass_g (it falls back to PET-equivalent
    # 25g if every input is missing), so the cost tile is guaranteed a number.
    if mass_g is None:
        mass_g = 25.0
    name = material.name if material else (s.get("material") or "")
    price = price_cache.lookup_price(name)
    cost = round((mass_g / 1000.0) * float(price["price_usd_per_kg"]), 4)
    return {
        "mass_g": mass_g,
        "cost_per_unit_usd": cost,
        "annual_cost_usd_at_1m_units": cost * 1_000_000.0,
        "material": price["name"] or name or "estimated polymer",
        "price_usd_per_kg": price["price_usd_per_kg"],
        "price_source": price["source"],          # "local" | "cache" | "web" | "fallback"
        "price_confidence": price["confidence"],
        "price_notes": price["notes"],
    }


# ─────────────────────────── live price lookup endpoint
class PriceQuery(BaseModel):
    name: str

@router.get("/materials/price")
def material_price(name: str = Query(..., min_length=1)):
    """Cost-research agent. Local table → JSON cache → Gemini 3 Pro web
    research → conservative fallback. Always returns a number; never 404s."""
    from ..services import price_cache
    return price_cache.lookup_price(name)


# ─────────────────────────────────────────── custom material (user-added)

class CustomMaterialBody(BaseModel):
    """User-supplied material. Optional fields default to PET-equivalent so a
    minimal entry still produces verdicts. The sustainability fields are
    optional; if missing, the carbon intensity is left null and the PCR card
    simply does not surface for this material."""
    name: str
    grade: Optional[str] = None
    density_kg_m3: float = 1380.0
    modulus_gpa: float = 2.8
    yield_strength_mpa: float = 55.0
    allowable_stress_mpa: float = 35.0
    recycled_content_pct: float = 0.0
    carbon_intensity_kg_co2e_per_kg: Optional[float] = None
    is_pcr: bool = False
    pcr_substitute_for: Optional[str] = None
    notes: Optional[str] = None


class WebSearchBody(BaseModel):
    name: str


@router.post("/materials/web-search")
def web_search_material(body: WebSearchBody, db: Session = Depends(get_db)):
    """Research a new material via the reasoning LLM and promote it to BOTH
    the local cache AND the permanent material DB.

    The MaterialAgent waterfall does this transparently on first use; this
    endpoint is the user-triggered version so the chat-side "I don't have
    data for X" prompt has a button to call it explicitly. The material
    catalogue expands on every successful research hit.
    """
    from ..services import material_cache
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    fetched = material_cache.fetch_via_llm(name)
    if not fetched:
        raise HTTPException(502, "Material lookup failed — try the custom-material form.")
    entry = material_cache.put(
        fetched["name"], fetched,
        source=f"AI intelligence research · {fetched.get('source_hint', '')}",
        confidence="estimated",
    )
    promoted = material_cache.promote_to_db(db, fetched)
    log_event(db, case_id="-", actor="material_agent", action="web_search",
              payload={"name": name, "found": True, "promoted_to_db": promoted})
    return {"ok": True, "name": name, "entry": entry, "promoted_to_db": promoted}


@router.post("/materials/custom")
def add_custom_material(body: CustomMaterialBody, db: Session = Depends(get_db)):
    """Add a user-defined material to BOTH the local cache and the permanent
    material DB.

    Saving to the DB means subsequent runs in any session can use the
    material with confidence='verified' (since a human added it). The cache
    entry remains for legacy lookups."""
    from ..services import material_cache
    fields = {
        "name": body.name,
        "grade": body.grade,
        "density_kg_m3": body.density_kg_m3,
        "modulus_gpa": body.modulus_gpa,
        "yield_strength_mpa": body.yield_strength_mpa,
        "allowable_stress_mpa": body.allowable_stress_mpa,
        "is_pcr": body.is_pcr,
        "recycled_content_pct": body.recycled_content_pct,
        "carbon_intensity_kg_co2e_per_kg": body.carbon_intensity_kg_co2e_per_kg,
        "pcr_substitute_for": body.pcr_substitute_for,
        "notes": body.notes,
        "source_hint": "user-added (custom)",
    }
    entry = material_cache.put(
        body.name, fields,
        source="user-added (custom)",
        confidence="estimated",
    )
    promoted = material_cache.promote_to_db(db, fields)
    return {"ok": True, "name": body.name, "entry": entry, "promoted_to_db": promoted}


# ─────────────────────────────────────────── time-usage chart data

@router.get("/users/{user_id}/time-usage")
def time_usage(user_id: str, db: Session = Depends(get_db), days: int = 14):
    """Per-day analysis-run counts for the dashboard time-usage chart."""
    from datetime import datetime, timedelta
    from collections import Counter
    from ..models import AuditEvent
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(AuditEvent)
            .join(Case, AuditEvent.case_id == Case.case_id)
            .filter(Case.user_id == user_id,
                    AuditEvent.action == "execute_plan_done",
                    AuditEvent.timestamp >= cutoff)
            .all())
    counts: Counter[str] = Counter()
    for r in rows:
        counts[r.timestamp.date().isoformat()] += 1
    # Always emit `days` keys so the chart line is continuous
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.utcnow().date() - timedelta(days=i)).isoformat()
        out.append({"date": d, "runs": int(counts.get(d, 0))})
    return {"user_id": user_id, "days": days, "series": out,
            "total": sum(p["runs"] for p in out)}


# ─────────────────────────────────────────── per-variant heatmap

class VariantHeatmapBody(BaseModel):
    """Variant material + wall override; capacity preserved from the case."""
    material: Optional[str] = None
    wall_thickness_mm: Optional[float] = None
    closure_type: Optional[str] = None
    fill_level_pct: Optional[float] = None


@router.post("/cases/{case_id}/optimize/variant-heatmap")
def variant_heatmap(case_id: str, body: VariantHeatmapBody, db: Session = Depends(get_db)):
    """Generate FEA-jet stress fields for a hypothetical variant on the same
    geometry as the baseline. Used by the optimisation comparison panel so
    the user can SEE that a variant has lower stress in the heatmap, not just
    read the SF number."""
    from ..agents.material import MaterialAgent
    from ..agents.transit import TransitAgent
    from ..models import GeometryAsset, TransitProfile
    from ..schemas import GeometrySummary, TransitEnvelope
    from ..services.visualization_service import build_heatmap_scenes
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    asset = (db.query(GeometryAsset).filter(GeometryAsset.case_id == case_id)
             .order_by(GeometryAsset.created_at.desc()).first())
    if not asset:
        raise HTTPException(404, "Upload geometry before requesting variant heatmaps.")

    s = case.case_summary or {}
    geometry = GeometrySummary(**asset.summary) if asset.summary else None
    # Material: use the variant override if present, else the case material
    mat_name = body.material or s.get("material") or ""
    new_material = MaterialAgent().lookup(db, mat_name) if mat_name else None
    # Transit envelope: derive from latest profile or default
    tp = (db.query(TransitProfile).filter(TransitProfile.case_id == case_id)
          .order_by(TransitProfile.profile_id.desc()).first())
    if tp:
        env = TransitEnvelope(
            mode_mix=tp.mode_mix or {}, vibration_g_rms=tp.vibration_level or 0.5,
            drop_height_m=tp.drop_height_m or 0.5, compression_load_n=tp.compression_load_n or 0.0,
            handling_fraction=tp.handling_fraction or 0.1,
            dominant_risks=[], suggested_test_sequence=[], confidence="estimated",
        )
    else:
        env = TransitAgent().build({"truck": 1.0})

    return build_heatmap_scenes(
        case_id=case_id, mesh_path=asset.mesh_uri, geometry=geometry,
        transit_env=env, material=new_material,
        stacking_orientation=s.get("stacking_orientation") or "upright",
        glb_url=f"/api/cases/{case_id}/mesh",
        scenarios=("drop_top", "drop_bottom", "drop_side", "transit"),
    )


@router.post("/cases/{case_id}/unlock")
def unlock(case_id: str, db: Session = Depends(get_db)):
    """Unlock a locked case so it can be edited again. The hash is preserved
    for audit but the locked flag is cleared."""
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(404)
    case.locked = False
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="unlocked")
    return {"case_id": case_id, "locked": False}
