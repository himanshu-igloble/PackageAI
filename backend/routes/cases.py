"""Case lifecycle: create, read, message, approve plan, execute, retrieve report."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..orchestrator.status_bus import emit_status, status_bus, sse_format
from ..services.agent_labels import humanize

from ..audit import log_event
from ..config import settings
from ..db import get_db
from ..models import Case, GeometryAsset, Message, User
from ..orchestrator.orchestrator import Orchestrator
from ..schemas import ApprovalDecision, CaseCreate, CaseRead, MessageIn
from ..agents.flute_resolver import canonical_flute_name, resolve_flute
from ..agents.secondary_packaging import SecondaryPackagingAgent
from ..services import geometry_service
from ..services.auth import adjust_tokens, current_user_optional
from ..services.geometry_service import GeometryParseError
from ..services.identification import identify_packaging
from ..services.visualization_service import build_scene


router = APIRouter()
orch = Orchestrator()


# ----- helpers --------------------------------------------------------------

def _get_case(db: Session, case_id: str) -> Case:
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return case


# ----- endpoints ------------------------------------------------------------

@router.post("/cases", response_model=CaseRead)
def create_case(
    payload: CaseCreate,
    db: Session = Depends(get_db),
    me: User | None = Depends(current_user_optional),
):
    # If the caller is authenticated, attribute the case to them; otherwise
    # keep the legacy "anon" attribution so the smoke flow still works.
    user_id = me.user_id if me else (payload.user_id or "anon")
    case = Case(user_id=user_id)
    db.add(case)
    db.commit()
    db.refresh(case)
    log_event(db, case_id=case.case_id, actor="orchestrator", action="case_created",
              payload={"user_id": user_id})
    db.commit()
    return case


@router.get("/cases/{case_id}", response_model=CaseRead)
def read_case(case_id: str, db: Session = Depends(get_db)):
    return _get_case(db, case_id)


@router.get("/cases/{case_id}/messages")
def list_messages(case_id: str, db: Session = Depends(get_db)):
    _get_case(db, case_id)
    msgs = (
        db.query(Message)
        .filter(Message.case_id == case_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return [
        {"role": m.role, "content": m.content, "metadata": m.metadata_json, "created_at": m.created_at.isoformat()}
        for m in msgs
    ]


@router.post("/cases/{case_id}/messages")
def send_message(case_id: str, payload: MessageIn, db: Session = Depends(get_db)):
    case = _get_case(db, case_id)
    return orch.handle_user_message(db, case, payload.content)


@router.get("/cases/{case_id}/plan")
def get_plan(case_id: str, db: Session = Depends(get_db)):
    case = _get_case(db, case_id)
    return orch.get_proposed_plan(case).model_dump()


SIMULATION_TOKEN_COST = 1


@router.post("/cases/{case_id}/approve")
def approve_plan(
    case_id: str,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    me: User | None = Depends(current_user_optional),
):
    case = _get_case(db, case_id)
    if not decision.approve:
        case.approval_state = "rejected"
        db.commit()
        log_event(db, case_id=case_id, actor="user", action="plan_rejected", payload=decision.model_dump())
        return {"status": "rejected"}
    # Apply edits to case_summary if provided
    if decision.edits:
        case.case_summary = {**(case.case_summary or {}), **decision.edits}

    # Token gate: 1 token per simulation run. Authenticated users must have
    # enough balance before we execute. Anonymous sessions bypass the gate so
    # the existing smoke flow keeps working — production should remove that
    # branch and require auth.
    if me is not None:
        balance = int(me.token_balance or 0)
        if balance < SIMULATION_TOKEN_COST:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_tokens",
                    "balance": balance,
                    "required": SIMULATION_TOKEN_COST,
                    "message": (
                        "This simulation will consume 1 token. "
                        f"Your balance is {balance}. Purchase a token pack to proceed."
                    ),
                },
            )

    case.approval_state = "plan_approved"
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="plan_approved",
              payload={**decision.model_dump(), "user_id": me.user_id if me else None})
    emit_status(case_id, stage=case.status, active_agent="orchestrator",
                action="plan_approved", summary="User approved the plan.")

    if me is not None:
        new_balance = adjust_tokens(
            db,
            user=me,
            delta=-SIMULATION_TOKEN_COST,
            reason="simulation_run",
            case_id=case_id,
            actor_user_id=me.user_id,
            notes=f"Approved plan run for case {case_id}.",
        )
        emit_status(case_id, stage=case.status, active_agent="orchestrator",
                    action="token_debited",
                    summary=f"1 token consumed; {new_balance} remaining.")

    snapshot = orch.execute_approved_plan(db, case)
    if me is not None:
        snapshot["token_balance_after"] = int(me.token_balance or 0)
    # Inject secondary_packaging so the fresh-analysis result matches the
    # snapshot endpoint shape — frontend reads it from both paths.
    sp = SecondaryPackagingAgent.build_summary(case.case_summary or {})
    if sp.get("enabled"):
        sp["recommendation"] = SecondaryPackagingAgent.get_recommendation(sp)
    snapshot["secondary_packaging"] = sp
    snapshot["case_summary"] = case.case_summary or {}
    return snapshot


@router.post("/cases/{case_id}/upload")
async def upload_geometry(
    case_id: str,
    file: UploadFile = File(...),
    demo: bool = False,
    packaging_family: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Upload a geometry file. Returns 422 with a structured error if the file
    cannot be parsed. Pass ?demo=true to explicitly opt into the labeled proxy
    fallback for STEP files when no CAD backend is installed."""
    case = _get_case(db, case_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".step", ".stp", ".stl", ".obj", ".ply", ".glb", ".gltf"}:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")

    case_dir = settings.storage_path / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    dest = case_dir / f"upload{suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    emit_status(case_id, stage=case.status, active_agent="geometry",
                action="parsing", tool="trimesh+cad_backend",
                summary=f"Parsing {dest.name}…")
    try:
        parsed = geometry_service.parse(dest, demo_mode=demo)
    except GeometryParseError as gpe:
        # Persist the failure so the case timeline shows it.
        log_event(db, case_id=case_id, actor="geometry_service", action="upload_failed",
                  payload=gpe.as_dict())
        emit_status(case_id, stage=case.status, active_agent="geometry",
                    action="parse_failed", awaiting="user_action",
                    summary=f"Parse failed: {gpe.reason}")
        raise HTTPException(status_code=422, detail=gpe.as_dict())

    glb_path = case_dir / "mesh.glb"
    glb_path.write_bytes(parsed.glb_bytes)

    asset = GeometryAsset(
        case_id=case_id,
        file_type=parsed.summary.file_type,
        storage_uri=str(dest),
        mesh_uri=str(glb_path),
        bounding_box=parsed.summary.bbox_mm,
        critical_zones={"zones": parsed.summary.critical_zones},
        summary=parsed.summary.model_dump(),
    )
    db.add(asset)

    # --- CAD-derived mass cross-check ---------------------------------------
    # If the case already has a declared material, derive the polymer mass
    # from CAD volume and the material density. Compare with any user-stated
    # gross_weight_g; if they disagree by more than 20% the orchestrator will
    # surface a clarifying message on the next chat turn.
    mass_check: dict | None = None
    prior_summary = dict(case.case_summary or {})
    # packaging_family sent directly with the upload takes precedence over any
    # prior PATCH — this eliminates the race condition where advance_after_upload
    # runs before the separate PATCH to /brief completes.
    if packaging_family in ("bottle", "packet"):
        prior_summary["packaging_family"] = packaging_family
        case.case_summary = {**(case.case_summary or {}), "packaging_family": packaging_family}
        db.commit()
    declared_material = prior_summary.get("material")
    if declared_material and parsed.summary.volume_mm3:
        from ..agents.material import MaterialAgent
        mat = MaterialAgent().lookup(db, declared_material)
        if mat and mat.density_kg_m3:
            # m[g] = V[mm³] × ρ[kg/m³] × 1e-6
            mass_from_cad_g = round(parsed.summary.volume_mm3 * mat.density_kg_m3 * 1e-6, 2)
            user_mass = prior_summary.get("gross_weight_g")
            mismatch = None
            if user_mass:
                rel = abs(float(user_mass) - mass_from_cad_g) / max(1e-6, float(user_mass))
                mismatch = round(rel * 100, 1)
            mass_check = {
                "mass_from_cad_g": mass_from_cad_g,
                "user_stated_mass_g": user_mass,
                "mismatch_pct": mismatch,
                "density_kg_m3": mat.density_kg_m3,
                "volume_mm3": parsed.summary.volume_mm3,
                "formula": "mass_g = volume_mm3 × density_kg_per_m3 × 1e-6",
                "needs_user_review": bool(mismatch and mismatch > 20),
                "is_proxy_volume": bool(parsed.is_proxy or parsed.summary.confidence == "approximate"),
            }

    # Deterministic packaging identification from geometry proportions and zones.
    # Result is stored alongside has_geometry and used by the orchestrator as
    # a routing preference over conversational assumptions.
    user_hint = prior_summary.get("packaging_type")
    ident = identify_packaging(parsed.summary, user_packaging_hint=user_hint)
    emit_status(case_id, stage=case.status, active_agent="geometry",
                action="identified",
                confidence=str(ident.confidence),
                summary=f"Identified as {ident.packaging_class} → {ident.routing_target}: {ident.reason}")

    case.case_summary = {
        **prior_summary,
        "has_geometry": True,
        "geometry_is_proxy": parsed.is_proxy,
        "identified_packaging": ident.packaging_class,
        "identification_confidence": ident.confidence,
        "routing_target": ident.routing_target,
        **({"cad_mass_check": mass_check} if mass_check else {}),
    }
    db.commit()
    db.refresh(asset)
    log_event(db, case_id=case_id, actor="geometry_service", action="upload_parsed",
              payload={"asset_id": asset.asset_id, "file_type": parsed.summary.file_type,
                       "is_proxy": parsed.is_proxy, "mass_check": mass_check})

    # If we detected a meaningful mass discrepancy, drop a clarification
    # message into the chat so the user notices on the next refresh.
    if mass_check and mass_check.get("needs_user_review") and not mass_check.get("is_proxy_volume"):
        msg = (
            f"I have re-derived the part mass from the CAD geometry as "
            f"{mass_check['mass_from_cad_g']} g (volume {parsed.summary.volume_mm3:.0f} mm³ × "
            f"{mass_check['density_kg_m3']:.0f} kg/m³). Your stated gross weight is "
            f"{mass_check['user_stated_mass_g']} g — a {mass_check['mismatch_pct']}% difference. "
            "I'll use the CAD-derived value for the simulation unless you confirm the stated weight is correct."
        )
        db.add(Message(case_id=case_id, role="assistant", content=msg,
                       metadata_json={"agent": "geometry", "kind": "mass_cross_check"}))
        db.commit()

    emit_status(case_id, stage=case.status, active_agent="geometry",
                action="parsed", confidence=parsed.summary.confidence,
                source=parsed.summary.file_type,
                summary=("Demo proxy in use." if parsed.is_proxy
                         else f"Mesh parsed; {len(parsed.summary.critical_zones)} critical zones flagged."))

    # Auto-advance: send the first flow-specific question immediately after upload.
    # This is saved to the DB as an assistant message; no user message required.
    advance = orch.advance_after_upload(db, case)

    return {
        "asset_id": asset.asset_id,
        "summary": parsed.summary.model_dump(),
        "is_proxy": parsed.is_proxy,
        "glb_url": f"/api/cases/{case_id}/mesh",
        "mass_check": mass_check,
        "identification": ident.as_dict(),
        "advance": advance,
    }


@router.post("/cases/{case_id}/enter-flow")
def enter_flow(case_id: str, db: Session = Depends(get_db)):
    """Enter the flow selected by packaging_family after routing conflict resolution."""
    case = _get_case(db, case_id)
    result = orch.enter_flow(db, case)
    return result or {"reply": "Ready to continue.", "active_flow": "intake"}


@router.get("/cases/{case_id}/mesh")
def get_mesh(case_id: str, db: Session = Depends(get_db)):
    _get_case(db, case_id)
    case_dir = settings.storage_path / case_id
    glb = case_dir / "mesh.glb"
    if not glb.exists():
        raise HTTPException(status_code=404, detail="no mesh available")
    return FileResponse(glb, media_type="model/gltf-binary", filename="mesh.glb")


@router.get("/cases/{case_id}/heatmaps")
def get_heatmaps(case_id: str, db: Session = Depends(get_db)):
    """Return the most recent heatmap scenes payload (4-scene viridis stress
    fields) computed during execute_approved_plan. Used by the viewer's scene
    switcher."""
    from ..models import AnalysisResult
    _get_case(db, case_id)
    # Find the case's latest geometry asset; recompute on demand if heatmaps
    # weren't persisted (we kept only summaries in the analysis result row).
    from ..models import GeometryAsset
    from ..schemas import GeometrySummary, TransitEnvelope
    from ..services.visualization_service import build_heatmap_scenes

    case = _get_case(db, case_id)
    asset = (
        db.query(GeometryAsset)
        .filter(GeometryAsset.case_id == case_id)
        .order_by(GeometryAsset.created_at.desc())
        .first()
    )
    if not asset:
        raise HTTPException(status_code=404, detail="no geometry asset")

    # Pull the most recent surrogate / material rows for context
    s = case.case_summary or {}
    # Reconstruct a transit envelope from the last persisted TransitProfile
    from ..models import TransitProfile
    tp = (
        db.query(TransitProfile)
        .filter(TransitProfile.case_id == case_id)
        .order_by(TransitProfile.profile_id.desc())
        .first()
    )
    transit_env = None
    if tp:
        transit_env = TransitEnvelope(
            mode_mix=tp.mode_mix or {},
            vibration_g_rms=tp.vibration_level or 0.5,
            drop_height_m=tp.drop_height_m or 0.5,
            compression_load_n=tp.compression_load_n or 0.0,
            handling_fraction=tp.handling_fraction or 0.1,
            dominant_risks=[],
            suggested_test_sequence=[],
            confidence="estimated",
        )
    geometry = None
    if asset.summary:
        try:
            geometry = GeometrySummary(**asset.summary)
        except Exception:
            geometry = None
    # Material — lookup from case
    from ..agents.material import MaterialAgent
    material = MaterialAgent().lookup(db, s.get("material") or "") if s.get("material") else None

    scenes = build_heatmap_scenes(
        case_id=case_id,
        mesh_path=asset.mesh_uri,
        geometry=geometry,
        transit_env=transit_env,
        material=material,
        stacking_orientation=s.get("stacking_orientation") or "upright",
        glb_url=f"/api/cases/{case_id}/mesh",
        scenarios=("drop_top", "drop_bottom", "drop_side", "transit"),
        case_summary=s,
    )
    return scenes


@router.get("/cases/{case_id}/visualization")
def get_visualization(case_id: str, db: Session = Depends(get_db)):
    case = _get_case(db, case_id)
    asset = (
        db.query(GeometryAsset)
        .filter(GeometryAsset.case_id == case.case_id)
        .order_by(GeometryAsset.created_at.desc())
        .first()
    )
    geometry = None
    if asset and asset.summary:
        from ..schemas import GeometrySummary
        try:
            geometry = GeometrySummary(**asset.summary)
        except Exception:
            geometry = None

    # Pull most recent surrogate risk map
    from ..models import AnalysisResult
    from ..schemas import SurrogateRiskMap
    risk_row = (
        db.query(AnalysisResult)
        .filter(AnalysisResult.case_id == case_id, AnalysisResult.method_type == "surrogate")
        .order_by(AnalysisResult.created_at.desc())
        .first()
    )
    risk_map = None
    if risk_row:
        try:
            risk_map = SurrogateRiskMap(**risk_row.outputs_json)
        except Exception:
            pass

    glb_url = f"/api/cases/{case_id}/mesh" if asset else None
    return build_scene(case_id=case_id, glb_url=glb_url, geometry=geometry, risk_map=risk_map)


@router.get("/cases/{case_id}/report")
def get_report(case_id: str, db: Session = Depends(get_db)):
    from ..models import AnalysisResult
    _get_case(db, case_id)
    row = (
        db.query(AnalysisResult)
        .filter(AnalysisResult.case_id == case_id, AnalysisResult.method_type == "report_draft")
        .order_by(AnalysisResult.created_at.desc())
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="no report draft yet")
    return JSONResponse(row.outputs_json)


@router.post("/cases/{case_id}/finalize")
def finalize(case_id: str, db: Session = Depends(get_db)):
    case = _get_case(db, case_id)
    if case.status != "review":
        raise HTTPException(status_code=400, detail=f"cannot finalize from stage {case.status}")
    case.approval_state = "finalized"
    case.status = "final_approved"
    db.commit()
    log_event(db, case_id=case_id, actor="user", action="finalized")
    case.status = "finalized"
    db.commit()
    emit_status(case_id, stage=case.status, active_agent="orchestrator",
                action="finalized", summary="Case finalized.")
    return {"status": case.status}


@router.get("/cases/{case_id}/status/stream")
async def status_stream(case_id: str, request: Request):
    """SSE endpoint. UI opens an EventSource here while the case is active.

    Emits a steady stream of user-safe status events (active agent, current
    action, awaiting state). Never emits raw model thoughts."""
    async def gen():
        q = await status_bus.subscribe(case_id)
        try:
            # Initial hello so the UI can tell the channel is alive.
            yield sse_format(humanize({"ts": "init", "stage": "subscribed",
                                        "active_agent": "orchestrator",
                                        "action": "stream_open",
                                        "summary": "live status connected"}))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield sse_format(humanize(evt))
                except asyncio.TimeoutError:
                    # Heartbeat so proxies don't close the connection.
                    yield ": ping\n\n"
        finally:
            await status_bus.unsubscribe(case_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",       # tells nginx not to buffer
    })


@router.get("/cases/{case_id}/status")
def status_history(case_id: str):
    """Polling fallback: returns the buffered status history for a case."""
    _ = case_id
    return {"events": [humanize(e) for e in status_bus.history(case_id)]}


@router.get("/cases/{case_id}/audit")
def list_audit(case_id: str, db: Session = Depends(get_db)):
    from ..models import AuditEvent
    rows = (
        db.query(AuditEvent)
        .filter(AuditEvent.case_id == case_id)
        .order_by(AuditEvent.timestamp.asc())
        .all()
    )
    return [
        {"actor": r.actor, "action": r.action, "payload": r.payload, "timestamp": r.timestamp.isoformat()}
        for r in rows
    ]


class ActualIstaBody(BaseModel):
    actual_verdict: str            # "pass" | "fail" | "partial"
    actual_failure_mode: Optional[str] = None
    actual_drop_height_m: Optional[float] = None
    notes: Optional[str] = None


@router.post("/cases/{case_id}/actual-ista")
def record_actual_ista(case_id: str, body: ActualIstaBody, db: Session = Depends(get_db)):
    """Record a real-world ISTA test outcome for this case. The LearningAgent
    diffs the actual result against the platform's prediction, asks the
    reasoning LLM for a root cause, and persists a calibration multiplier
    that biases subsequent runs on the same (material, packaging_type)
    pair toward observed reality."""
    from ..agents.learning import LearningAgent
    case = _get_case(db, case_id)
    if body.actual_verdict not in ("pass", "fail", "partial"):
        raise HTTPException(400, "actual_verdict must be pass | fail | partial")
    rec = LearningAgent().record_actual(
        db, case=case,
        actual_verdict=body.actual_verdict,
        actual_failure_mode=body.actual_failure_mode,
        actual_drop_height_m=body.actual_drop_height_m,
        notes=body.notes,
    )
    log_event(db, case_id=case_id, actor="learning_agent", action="record_actual_ista",
              payload={"record_id": rec.record_id, "delta_min_sf": rec.delta_min_sf,
                       "calibration_multiplier": rec.calibration_multiplier,
                       "root_cause": rec.root_cause})
    return {
        "record_id": rec.record_id,
        "predicted_verdict": rec.predicted_verdict,
        "predicted_min_sf": rec.predicted_min_sf,
        "actual_verdict": rec.actual_verdict,
        "delta_min_sf": rec.delta_min_sf,
        "calibration_multiplier": rec.calibration_multiplier,
        "root_cause": rec.root_cause,
        "learning_narrative": rec.learning_narrative,
    }


@router.get("/cases/{case_id}/learning")
def case_learning_summary(case_id: str, db: Session = Depends(get_db)):
    """All accuracy / learning records that apply to this case (i.e. the case
    itself, plus any other records on the same material × packaging_type
    pair that are biasing the calibration multiplier on this run)."""
    from ..agents.learning import LearningAgent
    case = _get_case(db, case_id)
    agent = LearningAgent()
    cal = agent.calibration_multiplier(
        db,
        material_name=(case.case_summary or {}).get("material"),
        packaging_type=(case.case_summary or {}).get("packaging_type"),
    )
    return {
        "calibration_multiplier": cal,
        "history": agent.summary(db, case_id=case_id),
    }


def _pcr_target_for_case(s: dict, asset) -> dict | None:
    """Return PCR target info dict for a case, family-aware.

    Returns a dict with keys:
        material_name   — baseline material to look up in the DB
        volume_mm3      — estimated component volume (None → per-kg reference)
        display_label   — human-readable component name for the UI
    Returns None when no actionable PCR target can be determined.

    Bottle behaviour is unchanged: uses case_summary.material + CAD volume.
    Packet: prefers secondary carton board; falls back to primary laminate.
    Brush:  targets primary pack material (backer card / paperboard).
    """
    packaging_family = s.get("packaging_family") or ""

    if packaging_family == "packet":
        # Prefer secondary carton board when available.
        if str(s.get("has_secondary_carton") or "").lower() == "yes":
            carton_material = _carton_type_to_material(
                s.get("carton_type") or "", s.get("carton_board_grade") or ""
            )
            if carton_material:
                return {
                    "material_name": carton_material,
                    "volume_mm3": _estimate_carton_volume_mm3(s),
                    "display_label": "Secondary carton board",
                }
        # Fallback: primary laminate — use the outer-web material from the laminate.
        lam_mat = _primary_laminate_material(s.get("laminate_structure") or "")
        if lam_mat:
            return {
                "material_name": lam_mat,
                "volume_mm3": _estimate_film_volume_mm3(s),
                "display_label": "Primary laminate film",
            }
        return None

    if packaging_family == "brush":
        # PCR target: primary pack material (backer card / paperboard).
        pack_mat = (s.get("primary_pack_material") or "").strip()
        if pack_mat:
            return {
                "material_name": pack_mat,
                "volume_mm3": _estimate_brush_pack_volume_mm3(s),
                "display_label": "Primary pack (backer card / board)",
            }
        return None

    # Default / bottle: use the declared shell material + CAD volume.
    material_name = s.get("material") or ""
    if not material_name:
        return None
    part_volume_mm3 = None
    if asset and asset.summary:
        part_volume_mm3 = (asset.summary or {}).get("volume_mm3")
    return {
        "material_name": material_name,
        "volume_mm3": part_volume_mm3,
        "display_label": "Bottle shell",
    }


def _carton_type_to_material(carton_type: str, board_grade: str) -> str | None:
    """Map a carton_type / board_grade string to a DB material name."""
    ct = carton_type.lower().replace("-", "_").replace(" ", "_")
    bg = board_grade.lower()
    # Corrugated formats map to Corrugated B-flute
    if ct in ("corrugated_carton", "corrugated_shipper", "tray"):
        return "Corrugated B-flute"
    # Rigid box / mono carton use paperboard
    if ct in ("rigid_box", "mono_carton", "display_carton", "master_case"):
        return "Kraft Paperboard"
    # Grade-based fallback: flute → corrugated, ply → paperboard or corrugated
    if (c := canonical_flute_name(board_grade)):
        return c
    if "5" in bg or "3" in bg:
        return "Corrugated B-flute"
    if "ply" in bg:
        return "Corrugated B-flute"
    # Shrink bundle has no board to substitute
    if ct == "shrink_bundle":
        return None
    return "Corrugated B-flute"


def _primary_laminate_material(laminate: str) -> str | None:
    """Extract the outer web material from a slash-separated laminate string.

    Returns a canonical material name (PET, LDPE, PP, etc.) only if the
    first token is a simple polymer we hold a PCR record for.
    """
    if not laminate:
        return None
    outer = laminate.split("/")[0].strip().lower().replace("-", "")
    # Map common outer-web tokens to DB material names
    _MAP = {
        "pet": "PET", "rpet": "PET",
        "bopp": "PP", "pp": "PP",
        "ldpe": "LDPE", "pe": "LDPE", "lldpe": "LDPE",
        "hdpe": "HDPE",
        "nylon": None, "pa": None,   # no PCR-Nylon in DB → return None
        "metpet": "PET", "metbopp": "PP",
        "al": None, "foil": None,   # aluminium foil — no film PCR analogue
    }
    return _MAP.get(outer)


def _estimate_carton_volume_mm3(s: dict) -> float | None:
    """Estimate carton board volume from carton_dimensions_mm + board thickness.

    Uses carton_dimensions_mm (L×W×H in mm) when present.
    Board thickness defaults: 3 mm for E-flute, 5 mm for B/C-flute, 4 mm fallback.
    Volume = outer surface area × board thickness.
    """
    dims = s.get("carton_dimensions_mm")
    if not isinstance(dims, dict):
        return None
    L = float(dims.get("length") or 0)
    W = float(dims.get("width") or 0)
    H = float(dims.get("height") or dims.get("gusset") or 0)
    if L <= 0 or W <= 0 or H <= 0:
        return None
    surface_area_mm2 = 2.0 * (L * W + L * H + W * H)
    bg = (s.get("carton_board_grade") or "").lower()
    # Flute-worded grades use the single flute resolver's real per-flute caliper
    # (E 1.5 / B 3.0 / C 4.0 mm). Ply-only grades keep their legacy thickness.
    if "flute" in bg:
        board_t = resolve_flute(s.get("carton_board_grade")).caliper_mm
    elif "5" in bg:
        board_t = 5.0
    elif "3" in bg:
        board_t = 3.0
    else:
        board_t = 4.0
    return surface_area_mm2 * board_t


def _estimate_film_volume_mm3(s: dict) -> float | None:
    """Estimate laminate film volume from packet_dimensions_mm × total_thickness_micron.

    Surface area ≈ 2 × (length × width) for a simple pillow pouch.
    Thickness in mm = total_thickness_micron / 1000.
    """
    dims = s.get("packet_dimensions_mm")
    thickness_micron = s.get("total_thickness_micron")
    if not isinstance(dims, dict) or not thickness_micron:
        return None
    L = float(dims.get("length") or 0)
    W = float(dims.get("width") or 0)
    if L <= 0 or W <= 0:
        return None
    gusset = float(dims.get("gusset") or 0)
    surface_area_mm2 = 2.0 * (L * W) + 4.0 * gusset * W  # simplified two-panel + gusset
    thickness_mm = float(thickness_micron) / 1000.0
    return surface_area_mm2 * thickness_mm


def _estimate_brush_pack_volume_mm3(s: dict) -> float | None:
    """Estimate brush primary pack board volume.

    No dedicated dimension field exists in the brush flow spec;
    return None so the agent falls back to the per-kg reference.
    """
    return None


@router.get("/cases/{case_id}/pcr-substitution")
def pcr_substitution(case_id: str, db: Session = Depends(get_db)):
    """Suggest a PCR (post-consumer recycled) substitute for the case's
    declared material/component and report mass / carbon savings, plus the
    mechanical impact the user should re-validate.

    Family-aware:
      bottle → bottle shell material + CAD volume (unchanged behaviour)
      packet → secondary carton board if present, else primary laminate
      brush  → primary pack material (backer card / paperboard)

    Returns 404 if no PCR target or analogue is available; the UI hides
    the PCR card in that case.
    """
    from ..agents.pcr import PCRAgent
    case = _get_case(db, case_id)
    s = case.case_summary or {}

    # Fetch geometry asset once (used by bottle target; packet/brush ignore it).
    asset = (
        db.query(GeometryAsset)
        .filter(GeometryAsset.case_id == case_id)
        .order_by(GeometryAsset.created_at.desc())
        .first()
    )

    target = _pcr_target_for_case(s, asset)
    if not target:
        raise HTTPException(status_code=404, detail="no PCR target material determined for this case")

    material_name = target["material_name"]
    part_volume_mm3 = target.get("volume_mm3")
    pcr_component = target["display_label"]

    annual_units = int(s.get("annual_units") or 1_000_000)
    result = PCRAgent().evaluate(
        db,
        baseline_material_name=material_name,
        part_volume_mm3=part_volume_mm3,
        annual_units=annual_units,
    )
    if not result:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_pcr_analogue",
                "baseline_material": material_name,
                "pcr_component": pcr_component,
                "message": (
                    f"No PCR substitute is currently catalogued for '{material_name}'. "
                    "Add a PCR variant to data/materials.json (set pcr_substitute_for "
                    "to the baseline material name) and restart the service."
                ),
            },
        )
    out = result.model_dump()
    out["pcr_component"] = pcr_component
    log_event(db, case_id=case_id, actor="pcr_agent", action="suggest",
              payload={"baseline": material_name, "candidate": result.candidate_material,
                       "pcr_component": pcr_component,
                       "annual_savings_kg_co2e": result.annual_carbon_savings_kg_co2e})
    return out
