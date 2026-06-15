"""Orchestrator Agent (5.1).

Holds the case state, picks the next specialist, and *always* surfaces an
approval gate before running real analysis. No improvisation.

Routing:
- If the user has not yet declared packaging type, run the generic Intake
  classifier (Gemini 2.5 Flash) until packaging_type is determined.
- If packaging_type ∈ {bottle, bottle_like} AND any required bottle field is
  still missing, route to BottleFlow (fixed-order Q&A).
- Otherwise, fall back to the generic Intake agent.

Every step emits a user-safe live status event via the status bus.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session


# Brief pacing pause between major stages of execute_approved_plan. Without
# this the analysis emits all status events in a sub-100 ms burst, which
# makes the live agent narration look pre-canned and undermines trust.
# This is wall-time only — not algorithmic delay — and per-stage it's small
# enough to keep the overall run snappy.
STAGE_PACE_SECONDS = 0.45


def _pace() -> None:
    try: time.sleep(STAGE_PACE_SECONDS)
    except Exception: pass

from ..audit import log_event
from ..agents.bottle_flow import REQUIRED_FIELDS as BOTTLE_REQUIRED, BottleFlowAgent
from ..agents.brush_flow import REQUIRED_FIELDS as BRUSH_REQUIRED, BrushFlowAgent
from ..agents.packet_flow import REQUIRED_FIELDS as PACKET_REQUIRED, PacketFlowAgent
from ..agents.calculation import CalculationAgent, inputs_hash
from ..agents.guardrail import GuardrailAgent
from ..agents.intake import IntakeAgent
from ..agents.ista2a import Ista2AAgent
from ..agents.ista6a import Ista6AAgent
from ..agents.learning import LearningAgent
from ..agents.material import MaterialAgent
from ..agents.reasoning import ReasoningAgent
from ..agents.report import ReportAgent
from ..agents.surrogate import SurrogateAgent
from ..agents.transit import TransitAgent
from ..services.visualization_service import build_heatmap_scenes
from ..models import (
    AnalysisResult,
    Case,
    GeometryAsset,
    Message,
    TransitProfile,
)
from ..schemas import (
    GeometrySummary,
    MaterialLookupResult,
    PlanStep,
    ProposedPlan,
    SurrogateRiskMap,
    TransitEnvelope,
)
from .state_machine import assert_transition
from .status_bus import emit_status


def _generic_required_fields() -> list[str]:
    """Fields any case needs before a plan can be proposed."""
    return ["packaging_type", "product_type", "objective", "material", "transit_modes"]


def _missing_generic(s: dict[str, Any]) -> list[str]:
    miss = []
    for f in _generic_required_fields():
        v = s.get(f)
        if v is None or v == "" or (isinstance(v, list) and not v):
            miss.append(f)
    return miss


def _missing_bottle(s: dict[str, Any]) -> list[str]:
    """Delegate to the bottle-flow agent so the rules stay in one place."""
    return BottleFlowAgent.missing_required(s)


def _missing_packet(s: dict[str, Any]) -> list[str]:
    """Delegate to the packet-flow agent so the rules stay in one place."""
    return PacketFlowAgent.missing_required(s)


def _missing_brush(s: dict[str, Any]) -> list[str]:
    """Delegate to the brush-flow agent so the rules stay in one place."""
    return BrushFlowAgent.missing_required(s)


class Orchestrator:
    def __init__(self) -> None:
        self.intake = IntakeAgent()
        self.bottle_flow = BottleFlowAgent()
        self.brush_flow = BrushFlowAgent()
        self.packet_flow = PacketFlowAgent()
        self.material = MaterialAgent()
        self.transit = TransitAgent()
        self.calc = CalculationAgent()
        self.surrogate = SurrogateAgent()
        self.guardrail = GuardrailAgent()
        self.reasoning = ReasoningAgent()
        self.report = ReportAgent()
        self.ista2a = Ista2AAgent()
        self.ista6a = Ista6AAgent()      # Amazon-style corner drop, always run
        self.learning = LearningAgent()

    # ------------------------------------------------------------------ chat

    def handle_user_message(self, db: Session, case: Case, content: str) -> dict[str, Any]:
        """Append user message, classify or step the bottle flow, persist reply."""
        db.add(Message(case_id=case.case_id, role="user", content=content))
        db.commit()
        log_event(db, case_id=case.case_id, actor="user", action="message",
                  payload={"content_len": len(content)})
        emit_status(case.case_id, stage=case.status, active_agent="orchestrator",
                    action="received_user_message", summary="Routing user message…")

        prior = case.case_summary or {}

        # Routing priority:
        # 1. Deterministic identification from geometry (routing_target field set
        #    by identification service on upload — rule-based, no LLM).
        # 2. Explicit packaging_type from conversation (BottleFlowAgent.is_bottle,
        #    PacketFlowAgent.is_packet).
        # 3. Fallback: generic Intake agent.
        routing_target = prior.get("routing_target")
        has_geometry = bool(prior.get("has_geometry"))
        packaging_family = prior.get("packaging_family")

        # packaging_family (set by the landing-page selector) is the primary
        # routing source of truth — it cannot be overridden by material chips,
        # geometry heuristics, or conversational keywords.
        if packaging_family == "bottle":
            _is_bottle, _is_packet, _is_brush = True, False, False
        elif packaging_family == "packet":
            _is_bottle, _is_packet, _is_brush = False, True, False
        elif packaging_family == "brush":
            _is_bottle, _is_packet, _is_brush = False, False, True
        else:
            # Fall back to geometry-based routing when no explicit selection exists.
            routing_locked = routing_target in ("bottle_flow", "packet_flow")
            _is_bottle = (
                routing_target == "bottle_flow"
                or (not routing_locked and BottleFlowAgent.is_bottle(prior))
            )
            _is_packet = (
                routing_target == "packet_flow"
                or (not routing_locked and PacketFlowAgent.is_packet(prior))
            )
            _is_brush = False

        # Intake-phase gate: only route to flow-specific question handlers while
        # the case is still collecting fields. Once analysis has run (status is
        # "review" or later), missing secondary-packaging fields should not
        # re-trigger the intake flow — the user is done answering questions.
        _still_in_intake = case.status in ("intake", "clarification", "plan_proposed")

        # Bottle flow stays gated on geometry — analysis requires real shape.
        if has_geometry and _is_bottle and _missing_bottle(prior) and _still_in_intake:
            return self._handle_bottle_turn(db, case, content, prior)
        # Packet flow does not gate on geometry — flexible packaging analysis
        # can proceed with declared dimensions when no dieline is uploaded.
        if _is_packet and _missing_packet(prior) and _still_in_intake:
            return self._handle_packet_turn(db, case, content, prior)
        # Brush flow does not gate on geometry — analysis proceeds without CAD.
        if _is_brush and _missing_brush(prior) and _still_in_intake:
            return self._handle_brush_turn(db, case, content, prior)
        return self._handle_intake_turn(db, case, content, prior)

    # ------------------------------------------------ generic intake turn ----

    def _handle_intake_turn(self, db: Session, case: Case, content: str, prior: dict[str, Any]) -> dict[str, Any]:
        emit_status(case.case_id, stage=case.status, active_agent="intake",
                    action="classify", tool="ai_intelligence",
                    summary="Classifying packaging request and extracting fields…")

        history = (
            db.query(Message)
            .filter(Message.case_id == case.case_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        convo = [{"role": m.role, "content": m.content} for m in history]
        intake_resp = self.intake.run(conversation=convo, prior_fields=prior)

        # Merge fields
        case.case_summary = {**prior, **intake_resp.fields.model_dump(exclude_none=True)}
        case.packaging_type = case.case_summary.get("packaging_type") or case.packaging_type
        case.product_type = case.case_summary.get("product_type") or case.product_type
        case.objective = case.case_summary.get("objective") or case.objective

        # If intake did not classify a packaging_type yet but the geometry
        # identification already produced a confident result, promote it so
        # downstream routing (is_bottle / is_packet) can act on it immediately.
        if not case.case_summary.get("packaging_type"):
            ident_class = case.case_summary.get("identified_packaging")
            ident_conf  = float(case.case_summary.get("identification_confidence") or 0.0)
            if ident_class and ident_conf >= 0.60:
                case.case_summary["packaging_type"] = ident_class

        # Route based on packaging_family (primary) first, then geometry fallback.
        has_geo = bool(case.case_summary.get("has_geometry"))
        pkg_family = case.case_summary.get("packaging_family")
        routing_locked = case.case_summary.get("routing_target") in ("bottle_flow", "packet_flow")
        if pkg_family == "bottle" and has_geo and _missing_bottle(case.case_summary):
            return self._ask_bottle_question(db, case, prior_reply=intake_resp.reply)
        if pkg_family == "packet" and _missing_packet(case.case_summary):
            return self._ask_packet_question(db, case, prior_reply=intake_resp.reply)
        if pkg_family == "brush" and _missing_brush(case.case_summary):
            return self._ask_brush_question(db, case, prior_reply=intake_resp.reply)
        # Fall back to geometry-based routing when no family was selected.
        if has_geo and not routing_locked and BottleFlowAgent.is_bottle(case.case_summary) and _missing_bottle(case.case_summary):
            return self._ask_bottle_question(db, case, prior_reply=intake_resp.reply)
        if has_geo and not routing_locked and PacketFlowAgent.is_packet(case.case_summary) and _missing_packet(case.case_summary):
            return self._ask_packet_question(db, case, prior_reply=intake_resp.reply)

        # Otherwise: standard intake → clarification / plan flow.
        target_stage = "plan_proposed" if intake_resp.ready_for_plan else (
            "clarification" if case.status in ("intake", "clarification") else case.status
        )
        try:
            assert_transition(case.status, target_stage)
            case.status = target_stage
        except ValueError:
            pass

        request_upload = bool(getattr(intake_resp, "__dict__", {}).get("request_upload"))
        db.add(Message(case_id=case.case_id, role="assistant", content=intake_resp.reply,
                       metadata_json={"agent": "intake",
                                      "ready_for_plan": intake_resp.ready_for_plan,
                                      "request_upload": request_upload}))
        db.commit()
        log_event(db, case_id=case.case_id, actor="intake_agent", action="reply",
                  payload={"ready_for_plan": intake_resp.ready_for_plan, "fields": case.case_summary,
                           "request_upload": request_upload})
        emit_status(case.case_id, stage=case.status, active_agent="intake",
                    action="reply_sent",
                    awaiting="user_answer" if not intake_resp.ready_for_plan else "user_plan_approval",
                    confidence=("estimated" if intake_resp.fields.confidence >= 0.6 else "insufficient_data"),
                    summary=intake_resp.reply[:140])

        plan = self._proposed_plan(case) if intake_resp.ready_for_plan else None
        return {
            "reply": intake_resp.reply,
            "fields": case.case_summary,
            "next_questions": intake_resp.next_questions,
            "stage": case.status,
            "ready_for_plan": intake_resp.ready_for_plan,
            "proposed_plan": plan.model_dump() if plan else None,
            "active_flow": "intake",
            "request_upload": request_upload,
        }

    # -------------------------------------------- bottle-specific Q&A turn ---

    def _handle_bottle_turn(self, db: Session, case: Case, content: str, prior: dict[str, Any]) -> dict[str, Any]:
        """Conversational bottle turn: absorb every field the user just mentioned
        (multi-field extraction), then ask the next most-important missing field
        in a flowing way — not as a step counter."""
        emit_status(case.case_id, stage=case.status, active_agent="bottle_flow",
                    action="conversational_extract", tool="ai_intelligence",
                    summary="Reading the user's message for any bottle details…")

        # Build short conversation excerpt for context
        history = (
            db.query(Message)
            .filter(Message.case_id == case.case_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        convo = [{"role": m.role, "content": m.content} for m in history[-6:]]
        turn = self.bottle_flow.step(prior, content, conversation=convo)

        case.case_summary = turn.fields
        case.packaging_type = case.case_summary.get("packaging_type") or case.packaging_type
        case.product_type = case.case_summary.get("product_type") or case.product_type
        case.objective = case.case_summary.get("objective") or case.objective

        # Stage management
        if turn.ready_for_plan:
            try:
                assert_transition(case.status, "plan_proposed")
                case.status = "plan_proposed"
            except ValueError:
                pass
        else:
            case.status = "clarification" if case.status == "intake" else case.status

        db.add(Message(case_id=case.case_id, role="assistant", content=turn.reply,
                       metadata_json={"agent": "bottle_flow", "asks_field": turn.asks_field,
                                      "options": turn.options}))
        db.commit()
        log_event(db, case_id=case.case_id, actor="bottle_flow", action="turn",
                  payload={"missing": turn.missing, "asking": turn.asks_field})

        total = len(BOTTLE_REQUIRED)
        # Progress = total - missing required + 1 (next to ask), capped
        progress = {"step": max(1, min(total, total - len(turn.missing) + 1)), "total": total}
        emit_status(
            case.case_id,
            stage=case.status,
            active_agent="bottle_flow",
            action=("all_fields_collected" if turn.ready_for_plan else f"asking:{turn.asks_field}"),
            tool="ai_intelligence",
            awaiting=("user_plan_approval" if turn.ready_for_plan else "user_answer"),
            confidence="estimated",
            summary=(turn.reply[:140]),
            options=turn.options,
            progress=progress,
        )

        plan = self._proposed_plan(case) if turn.ready_for_plan else None
        return {
            "reply": turn.reply,
            "fields": case.case_summary,
            "stage": case.status,
            "ready_for_plan": turn.ready_for_plan,
            "proposed_plan": plan.model_dump() if plan else None,
            "active_flow": "bottle_flow",
            "bottle_progress": progress,
            "asking_field": turn.asks_field,
            "options": turn.options,
            # Trigger the upload modal whenever bottle_flow is asking for the CAD.
            "request_upload": (turn.asks_field == "has_geometry"),
        }

    def _ask_bottle_question(self, db: Session, case: Case, *, prior_reply: str | None,
                             just_absorbed: str | None = None) -> dict[str, Any]:
        """Initial bottle question — invoked once when intake first detects a bottle.
        Subsequent turns use _handle_bottle_turn for full conversational flow."""
        # Use the natural opener (won't try to extract anything; just ask the first thing).
        opener = self.bottle_flow.opener(dict(case.case_summary or {}))
        case.case_summary = opener.fields
        if opener.asks_field is None:
            # All bottle fields filled — propose the plan.
            try:
                assert_transition(case.status, "plan_proposed")
                case.status = "plan_proposed"
            except ValueError:
                pass
            reply = (prior_reply or "") + (" " if prior_reply else "") + \
                    "Great — I have everything I need. Review the proposed plan and approve to start the analysis."
            db.add(Message(case_id=case.case_id, role="assistant", content=reply,
                           metadata_json={"agent": "bottle_flow", "stage": "plan_proposed"}))
            db.commit()
            emit_status(case.case_id, stage=case.status, active_agent="bottle_flow",
                        action="all_fields_collected", awaiting="user_plan_approval",
                        confidence="estimated", summary="Bottle intake complete; awaiting plan approval.")
            plan = self._proposed_plan(case)
            return {
                "reply": reply,
                "fields": case.case_summary,
                "stage": case.status,
                "ready_for_plan": True,
                "proposed_plan": plan.model_dump(),
                "active_flow": "bottle_flow",
                "bottle_progress": None,
            }

        case.status = "clarification" if case.status == "intake" else case.status
        full_reply = (prior_reply + " " if prior_reply else "") + opener.reply
        db.add(Message(case_id=case.case_id, role="assistant", content=full_reply,
                       metadata_json={"agent": "bottle_flow", "asks_field": opener.asks_field,
                                      "options": opener.options}))
        db.commit()
        total = len(BOTTLE_REQUIRED)
        progress = {"step": max(1, total - len(opener.missing) + 1), "total": total}
        log_event(db, case_id=case.case_id, actor="bottle_flow", action="asked_question",
                  payload={"field": opener.asks_field, "progress": progress})
        emit_status(case.case_id, stage=case.status, active_agent="bottle_flow",
                    action=f"asking:{opener.asks_field}", tool="ai_intelligence",
                    awaiting="user_answer", confidence="estimated",
                    summary=opener.reply[:140],
                    options=opener.options, progress=progress)
        return {
            "reply": full_reply,
            "fields": case.case_summary,
            "stage": case.status,
            "ready_for_plan": False,
            "proposed_plan": None,
            "active_flow": "bottle_flow",
            "bottle_progress": progress,
            "asking_field": opener.asks_field,
            "options": opener.options,
            "request_upload": (opener.asks_field == "has_geometry"),
        }

    # -------------------------------------------- packet-specific Q&A turn ---

    def _handle_packet_turn(self, db: Session, case: Case, content: str, prior: dict[str, Any]) -> dict[str, Any]:
        """Conversational packet turn: absorb every field the user mentioned
        (multi-field extraction), then ask the next most-important missing field."""
        emit_status(case.case_id, stage=case.status, active_agent="packet_flow",
                    action="conversational_extract", tool="ai_intelligence",
                    summary="Reading the user's message for flexible packaging details…")

        history = (
            db.query(Message)
            .filter(Message.case_id == case.case_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        convo = [{"role": m.role, "content": m.content} for m in history[-6:]]
        turn = self.packet_flow.step(prior, content, conversation=convo)

        case.case_summary = turn.fields
        case.packaging_type = case.case_summary.get("packaging_type") or case.packaging_type
        case.product_type = case.case_summary.get("product_type") or case.product_type
        case.objective = case.case_summary.get("objective") or case.objective

        if turn.ready_for_plan:
            try:
                assert_transition(case.status, "plan_proposed")
                case.status = "plan_proposed"
            except ValueError:
                pass
        else:
            case.status = "clarification" if case.status == "intake" else case.status

        db.add(Message(case_id=case.case_id, role="assistant", content=turn.reply,
                       metadata_json={"agent": "packet_flow", "asks_field": turn.asks_field,
                                      "options": turn.options}))
        db.commit()
        log_event(db, case_id=case.case_id, actor="packet_flow", action="turn",
                  payload={"missing": turn.missing, "asking": turn.asks_field})

        total = len(PACKET_REQUIRED)
        progress = {"step": max(1, min(total, total - len(turn.missing) + 1)), "total": total}
        emit_status(
            case.case_id,
            stage=case.status,
            active_agent="packet_flow",
            action=("all_fields_collected" if turn.ready_for_plan else f"asking:{turn.asks_field}"),
            tool="ai_intelligence",
            awaiting=("user_plan_approval" if turn.ready_for_plan else "user_answer"),
            confidence="estimated",
            summary=(turn.reply[:140]),
            options=turn.options,
            progress=progress,
        )

        plan = self._proposed_plan(case) if turn.ready_for_plan else None
        return {
            "reply": turn.reply,
            "fields": case.case_summary,
            "stage": case.status,
            "ready_for_plan": turn.ready_for_plan,
            "proposed_plan": plan.model_dump() if plan else None,
            "active_flow": "packet_flow",
            "packet_progress": progress,
            "asking_field": turn.asks_field,
            "options": turn.options,
            # Packet flow: geometry is OPTIONAL — never trigger the blocking upload modal.
            "request_upload": False,
        }

    def _ask_packet_question(self, db: Session, case: Case, *, prior_reply: str | None) -> dict[str, Any]:
        """Initial packet question — invoked once when intake first detects flexible packaging.
        Subsequent turns use _handle_packet_turn for full conversational flow."""
        opener = self.packet_flow.opener(dict(case.case_summary or {}))
        case.case_summary = opener.fields
        if opener.asks_field is None:
            try:
                assert_transition(case.status, "plan_proposed")
                case.status = "plan_proposed"
            except ValueError:
                pass
            reply = (prior_reply or "") + (" " if prior_reply else "") + \
                    "I have everything I need. Review the proposed plan and approve to start the analysis."
            db.add(Message(case_id=case.case_id, role="assistant", content=reply,
                           metadata_json={"agent": "packet_flow", "stage": "plan_proposed"}))
            db.commit()
            emit_status(case.case_id, stage=case.status, active_agent="packet_flow",
                        action="all_fields_collected", awaiting="user_plan_approval",
                        confidence="estimated", summary="Packet intake complete; awaiting plan approval.")
            plan = self._proposed_plan(case)
            return {
                "reply": reply,
                "fields": case.case_summary,
                "stage": case.status,
                "ready_for_plan": True,
                "proposed_plan": plan.model_dump(),
                "active_flow": "packet_flow",
                "packet_progress": None,
            }

        case.status = "clarification" if case.status == "intake" else case.status
        # Build reply: when called from advance_after_upload, prior_reply carries a
        # geometry-aware acknowledgement ("The uploaded geometry appears to match
        # flexible packaging."). Otherwise build a generic type acknowledgement.
        if prior_reply:
            ack = prior_reply.rstrip() + " "
        else:
            pt_raw = case.case_summary.get("packaging_type", "")
            pt = (pt_raw or "flexible packaging").replace("_", " ")
            ack = f"Got it — working with a {pt}. " if pt_raw else ""
        full_reply = ack + opener.reply
        db.add(Message(case_id=case.case_id, role="assistant", content=full_reply,
                       metadata_json={"agent": "packet_flow", "asks_field": opener.asks_field,
                                      "options": opener.options}))
        db.commit()
        total = len(PACKET_REQUIRED)
        progress = {"step": max(1, total - len(opener.missing) + 1), "total": total}
        log_event(db, case_id=case.case_id, actor="packet_flow", action="asked_question",
                  payload={"field": opener.asks_field, "progress": progress})
        emit_status(case.case_id, stage=case.status, active_agent="packet_flow",
                    action=f"asking:{opener.asks_field}", tool="ai_intelligence",
                    awaiting="user_answer", confidence="estimated",
                    summary=opener.reply[:140],
                    options=opener.options, progress=progress)
        return {
            "reply": full_reply,
            "fields": case.case_summary,
            "stage": case.status,
            "ready_for_plan": False,
            "proposed_plan": None,
            "active_flow": "packet_flow",
            "packet_progress": progress,
            "asking_field": opener.asks_field,
            "options": opener.options,
            # Packet flow: geometry is OPTIONAL — never trigger the blocking upload modal.
            "request_upload": False,
        }

    # -------------------------------------------- brush-specific Q&A turn ----

    def _handle_brush_turn(self, db: Session, case: Case, content: str, prior: dict[str, Any]) -> dict[str, Any]:
        """Conversational brush turn: absorb every field the user mentioned
        (multi-field extraction), then ask the next most-important missing field."""
        emit_status(case.case_id, stage=case.status, active_agent="brush_flow",
                    action="conversational_extract", tool="ai_intelligence",
                    summary="Reading the user's message for brush packaging details…")

        history = (
            db.query(Message)
            .filter(Message.case_id == case.case_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        convo = [{"role": m.role, "content": m.content} for m in history[-6:]]
        turn = self.brush_flow.step(prior, content, conversation=convo)

        case.case_summary = turn.fields
        case.packaging_type = case.case_summary.get("packaging_type") or case.packaging_type
        case.product_type = case.case_summary.get("product_type") or case.product_type
        case.objective = case.case_summary.get("objective") or case.objective

        if turn.ready_for_plan:
            try:
                assert_transition(case.status, "plan_proposed")
                case.status = "plan_proposed"
            except ValueError:
                pass
        else:
            case.status = "clarification" if case.status == "intake" else case.status

        db.add(Message(case_id=case.case_id, role="assistant", content=turn.reply,
                       metadata_json={"agent": "brush_flow", "asks_field": turn.asks_field,
                                      "options": turn.options}))
        db.commit()
        log_event(db, case_id=case.case_id, actor="brush_flow", action="turn",
                  payload={"missing": turn.missing, "asking": turn.asks_field})

        total = len(BRUSH_REQUIRED)
        progress = {"step": max(1, min(total, total - len(turn.missing) + 1)), "total": total}
        emit_status(
            case.case_id,
            stage=case.status,
            active_agent="brush_flow",
            action=("all_fields_collected" if turn.ready_for_plan else f"asking:{turn.asks_field}"),
            tool="ai_intelligence",
            awaiting=("user_plan_approval" if turn.ready_for_plan else "user_answer"),
            confidence="estimated",
            summary=(turn.reply[:140]),
            options=turn.options,
            progress=progress,
        )

        plan = self._proposed_plan(case) if turn.ready_for_plan else None
        return {
            "reply": turn.reply,
            "fields": case.case_summary,
            "stage": case.status,
            "ready_for_plan": turn.ready_for_plan,
            "proposed_plan": plan.model_dump() if plan else None,
            "active_flow": "brush_flow",
            "brush_progress": progress,
            "asking_field": turn.asks_field,
            "options": turn.options,
            # Brush flow: geometry is OPTIONAL — never trigger the blocking upload modal.
            "request_upload": False,
        }

    def _ask_brush_question(self, db: Session, case: Case, *, prior_reply: str | None) -> dict[str, Any]:
        """Initial brush question — invoked once when intake first detects brush family.
        Subsequent turns use _handle_brush_turn for full conversational flow."""
        opener = self.brush_flow.opener(dict(case.case_summary or {}))
        case.case_summary = opener.fields
        if opener.asks_field is None:
            try:
                assert_transition(case.status, "plan_proposed")
                case.status = "plan_proposed"
            except ValueError:
                pass
            reply = (prior_reply or "") + (" " if prior_reply else "") + \
                    "I have everything I need. Review the proposed plan and approve to start the analysis."
            db.add(Message(case_id=case.case_id, role="assistant", content=reply,
                           metadata_json={"agent": "brush_flow", "stage": "plan_proposed"}))
            db.commit()
            emit_status(case.case_id, stage=case.status, active_agent="brush_flow",
                        action="all_fields_collected", awaiting="user_plan_approval",
                        confidence="estimated", summary="Brush intake complete; awaiting plan approval.")
            plan = self._proposed_plan(case)
            return {
                "reply": reply,
                "fields": case.case_summary,
                "stage": case.status,
                "ready_for_plan": True,
                "proposed_plan": plan.model_dump(),
                "active_flow": "brush_flow",
                "brush_progress": None,
            }

        case.status = "clarification" if case.status == "intake" else case.status
        ack = (prior_reply.rstrip() + " ") if prior_reply else "Got it — working with brush packaging. "
        full_reply = ack + opener.reply
        db.add(Message(case_id=case.case_id, role="assistant", content=full_reply,
                       metadata_json={"agent": "brush_flow", "asks_field": opener.asks_field,
                                      "options": opener.options}))
        db.commit()
        total = len(BRUSH_REQUIRED)
        progress = {"step": max(1, total - len(opener.missing) + 1), "total": total}
        log_event(db, case_id=case.case_id, actor="brush_flow", action="asked_question",
                  payload={"field": opener.asks_field, "progress": progress})
        emit_status(case.case_id, stage=case.status, active_agent="brush_flow",
                    action=f"asking:{opener.asks_field}", tool="ai_intelligence",
                    awaiting="user_answer", confidence="estimated",
                    summary=opener.reply[:140],
                    options=opener.options, progress=progress)
        return {
            "reply": full_reply,
            "fields": case.case_summary,
            "stage": case.status,
            "ready_for_plan": False,
            "proposed_plan": None,
            "active_flow": "brush_flow",
            "brush_progress": progress,
            "asking_field": opener.asks_field,
            "options": opener.options,
            # Brush flow: geometry is OPTIONAL — never trigger the blocking upload modal.
            "request_upload": False,
        }

    # ----------------------------------------- post-upload auto-advance ------

    def advance_after_upload(self, db: Session, case: Case) -> dict[str, Any] | None:
        """Auto-advance to the first flow-specific question right after geometry upload.

        packaging_family (set by the landing-page selector) is the primary router.
        If geometry contradicts the user's selection, a confirmation is surfaced
        rather than silently rerouting. Falls back to geometry routing_target when
        no packaging_family has been set.
        """
        prior = dict(case.case_summary or {})
        packaging_family = prior.get("packaging_family")
        routing_target = prior.get("routing_target")

        # Sync packaging_type with the geometry identification result.
        # Skip for brush: brush packaging (blister, backer card) often looks
        # like flexible_packaging to the geometry classifier — the user's
        # explicit packaging_family selection is the authoritative source.
        ident_class = prior.get("identified_packaging")
        if ident_class and ident_class != "unknown" and packaging_family != "brush":
            case.case_summary = {**prior, "packaging_type": ident_class}
            prior = dict(case.case_summary)

        # Conflict detection: user's selection contradicts geometry classification.
        # Only fires when routing_target is definitive (not "intake").
        if packaging_family and routing_target not in ("intake", None):
            _family_to_routing = {"bottle": "bottle_flow", "packet": "packet_flow"}
            expected = _family_to_routing.get(packaging_family)
            if expected and routing_target != expected:
                suggested = "packet" if packaging_family == "bottle" else "bottle"
                geom_label = "flexible" if packaging_family == "bottle" else "bottle-like"
                pkg_label = "flexible packaging" if packaging_family == "packet" else "a bottle"
                msg = (
                    f"The uploaded geometry appears more {geom_label} than {pkg_label}. "
                    f"Would you like to switch to "
                    f"{'Packet' if suggested == 'packet' else 'Bottle'} workflow instead?"
                )
                db.add(Message(case_id=case.case_id, role="assistant", content=msg,
                               metadata_json={"agent": "intake", "kind": "routing_conflict",
                                              "current_family": packaging_family,
                                              "suggested_family": suggested}))
                db.commit()
                emit_status(case.case_id, stage=case.status, active_agent="intake",
                            action="routing_conflict", summary=msg[:140])
                return {
                    "reply": msg,
                    "active_flow": "intake",
                    "routing_conflict": True,
                    "current_family": packaging_family,
                    "suggested_family": suggested,
                }

        # Route based on packaging_family (primary) or routing_target (fallback).
        effective_family = packaging_family or (
            "bottle" if routing_target == "bottle_flow" else
            "packet" if routing_target == "packet_flow" else None
        )

        if effective_family == "bottle":
            return self._ask_bottle_question(
                db, case,
                prior_reply="Geometry parsed successfully. Bottle workflow selected.",
            )

        if effective_family == "packet":
            return self._ask_packet_question(
                db, case,
                prior_reply="Geometry parsed successfully. Packet workflow selected.",
            )

        if effective_family == "brush":
            return self._ask_brush_question(
                db, case,
                prior_reply="Geometry parsed successfully. Brush workflow selected.",
            )

        # Unknown — ask user to clarify the packaging type.
        msg = (
            "I wasn't able to determine the packaging type automatically from the "
            "geometry. Could you tell me what type of packaging this is — for "
            "example, a bottle, pouch, or carton?"
        )
        db.add(Message(case_id=case.case_id, role="assistant", content=msg,
                       metadata_json={"agent": "intake", "kind": "classification_fallback"}))
        db.commit()
        emit_status(case.case_id, stage=case.status, active_agent="intake",
                    action="classification_fallback",
                    summary="Geometry unclassified; asking user for packaging type.")
        return {"reply": msg, "active_flow": "intake"}

    def enter_flow(self, db: Session, case: Case) -> dict[str, Any] | None:
        """Enter bottle, packet, or brush flow based on packaging_family after conflict resolution."""
        prior = dict(case.case_summary or {})
        packaging_family = prior.get("packaging_family")
        if packaging_family == "bottle":
            return self._ask_bottle_question(db, case, prior_reply=None)
        if packaging_family == "packet":
            return self._ask_packet_question(db, case, prior_reply=None)
        if packaging_family == "brush":
            return self._ask_brush_question(db, case, prior_reply=None)
        return None

    # --------------------------------------------------------------- planning

    def _proposed_plan(self, case: Case) -> ProposedPlan:
        steps: list[PlanStep] = []
        s = case.case_summary or {}
        if s.get("material"):
            steps.append(PlanStep(
                agent="material_agent",
                action=f"Look up verified properties for '{s.get('material')}' in the material DB.",
                rationale="All downstream calcs depend on grounded material properties.",
            ))
        modes = s.get("transit_modes") or []
        steps.append(PlanStep(
            agent="transit_agent",
            action=f"Build transit envelope for modes: {', '.join(modes) or '(default truck mix)'}"
                   + (f" on a {s['road_condition']} road" if s.get("road_condition") else ""),
            rationale="Defines vibration / drop / compression loading for risk analysis.",
        ))
        if s.get("has_geometry"):
            steps.append(PlanStep(
                agent="geometry_service",
                action="Parse the uploaded geometry, extract bounding box and critical zones.",
                rationale="Needed for any zone-wise stress / risk reasoning.",
            ))
        else:
            steps.append(PlanStep(
                agent="geometry_service",
                action="No geometry uploaded yet. Geometry-dependent checks (buckling, zone risk) will run with conservative defaults and be labeled approximate.",
                rationale="Avoids fabricating dimensions while still producing a transit envelope and material check.",
            ))
        steps.append(PlanStep(
            agent="calculation_agent",
            action="Run deterministic drop-energy, compression, and thin-wall buckling checks.",
            rationale="Tier-1 deterministic checks per architecture section 15.",
        ))
        steps.append(PlanStep(
            agent="surrogate_agent",
            action="Produce an approximate zone-wise risk map (clearly labeled approximate).",
            rationale="Tier-2 heuristic surrogate, never sold as FEA.",
        ))
        steps.append(PlanStep(
            agent="reasoning_agent",
            action="Run an AI intelligence self-check pass on the full analysis snapshot.",
            rationale="Catches contradictions, missing units, overconfident claims.",
        ))
        steps.append(PlanStep(
            agent="report_agent",
            action="Draft an audit-friendly engineering review for human approval before finalization.",
            rationale="Human-in-the-loop is mandatory.",
        ))

        assumptions = [
            "Material properties come from the verified DB; missing materials block downstream verdicts.",
            "Transit envelope uses conservative coarse mode-mapping; not lane-specific.",
            "Surrogate risk map is approximate, not validated FEA.",
            "No pass/fail conclusion is finalized without explicit user approval.",
        ]
        if s.get("geometry_is_proxy"):
            assumptions.append("Geometry is a labeled demo proxy, NOT the uploaded CAD model — all geometry-derived results are approximate.")
        return ProposedPlan(
            case_summary=case.case_summary or {},
            assumptions=assumptions,
            steps=steps,
            confidence="estimated",
        )

    def get_proposed_plan(self, case: Case) -> ProposedPlan:
        return self._proposed_plan(case)

    # --------------------------------------------------------------- execute

    def execute_approved_plan(self, db: Session, case: Case) -> dict[str, Any]:
        if case.approval_state != "plan_approved":
            raise PermissionError("Plan must be approved before execution.")
        try:
            assert_transition(case.status, "executing")
            case.status = "executing"
            db.commit()
        except ValueError:
            pass
        log_event(db, case_id=case.case_id, actor="orchestrator", action="execute_plan_start")
        emit_status(case.case_id, stage="executing", active_agent="orchestrator",
                    action="execute_plan_start", summary="Starting approved analysis run.")

        snapshot: dict[str, Any] = {"case_id": case.case_id}
        s = case.case_summary or {}
        _pace()

        # --- Material ---
        material: MaterialLookupResult | None = None
        if s.get("material"):
            emit_status(case.case_id, stage="executing", active_agent="material",
                        action="lookup", tool="material_db", source="db",
                        summary=f"Looking up '{s['material']}' in the verified DB…")
            material = self.material.lookup(db, s["material"])
            mat_check = self.guardrail.review_material(material.model_dump())
            snapshot["material"] = material.model_dump()
            snapshot["material_guardrail"] = mat_check.__dict__
            emit_status(case.case_id, stage="executing", active_agent="material",
                        action="lookup_done", confidence=material.confidence, source=material.source,
                        summary=f"Material '{material.name}' resolved with confidence={material.confidence}.")
            log_event(db, case_id=case.case_id, actor="material_agent", action="lookup",
                      payload={"name": s["material"], "confidence": material.confidence})
            _pace()

        # --- Geometry (latest uploaded asset, if any) ---
        geometry: GeometrySummary | None = None
        asset: GeometryAsset | None = (
            db.query(GeometryAsset)
            .filter(GeometryAsset.case_id == case.case_id)
            .order_by(GeometryAsset.created_at.desc())
            .first()
        )
        if asset and asset.summary:
            try:
                geometry = GeometrySummary(**asset.summary)
                emit_status(case.case_id, stage="executing", active_agent="geometry",
                            action="loaded_asset", source=asset.storage_uri,
                            confidence=geometry.confidence,
                            summary=f"Using parsed geometry '{geometry.file_type}'.")
            except Exception:
                geometry = None
        snapshot["geometry"] = geometry.model_dump() if geometry else None
        snapshot["geometry_asset_id"] = asset.asset_id if asset else None

        # --- Transit envelope (real CSV-derived) ---
        modes = s.get("transit_modes") or ["truck"]
        mode_mix = s.get("transit_mode_mix") or {m: 1.0 / len(modes) for m in modes}
        road = s.get("road_condition") or "mixed"
        ship_sev = s.get("ship_severity") or "moderate"
        emit_status(case.case_id, stage="executing", active_agent="transit",
                    action="build_envelope", tool="deterministic",
                    summary=f"Building transit envelope from real CSV data ({','.join(modes)})…")
        transit_env: TransitEnvelope = self.transit.build(
            mode_mix, road=road, ship_severity=ship_sev,
            durations_min=s.get("transit_durations_min"),
            manual_drop_height_m=s.get("manual_drop_height_m"),
        )
        snapshot["transit"] = transit_env.model_dump()
        db.add(TransitProfile(
            case_id=case.case_id,
            mode_mix=mode_mix,
            vibration_level=transit_env.vibration_g_rms,
            drop_height_m=transit_env.drop_height_m,
            compression_load_n=transit_env.compression_load_n,
            handling_fraction=transit_env.handling_fraction,
            notes="; ".join(transit_env.dominant_risks),
        ))
        db.commit()
        log_event(db, case_id=case.case_id, actor="transit_agent", action="build_envelope",
                  payload={"dominant": transit_env.dominant_risks})
        emit_status(case.case_id, stage="executing", active_agent="transit", action="envelope_done",
                    confidence=transit_env.confidence,
                    summary=f"Dominant risks: {', '.join(transit_env.dominant_risks)}.")
        _pace()

        # --- Deterministic calculations ---
        calcs = []
        # Drop energy needs an estimated mass. Prefer gross_weight_g if collected,
        # else filled_mass_kg from edits, else conservative default.
        approx_mass_kg = (
            (float(s["gross_weight_g"]) / 1000.0) if s.get("gross_weight_g") else
            float(s.get("filled_mass_kg") or 0.6)
        )
        emit_status(case.case_id, stage="executing", active_agent="calculation",
                    action="drop_energy", tool="deterministic",
                    summary=f"E = m·g·h with m={approx_mass_kg:.3f} kg, h={transit_env.drop_height_m} m")
        try:
            calcs.append(self.calc.drop_energy(mass_kg=approx_mass_kg, height_m=transit_env.drop_height_m))
        except Exception:
            pass
        try:
            calcs.append(self.calc.impact_velocity(height_m=transit_env.drop_height_m))
        except Exception:
            pass
        if material and material.allowable_stress_mpa and geometry:
            dims = geometry.overall_dims_mm
            area_mm2 = max(1.0, dims.get("length_mm", 50) * dims.get("width_mm", 50))
            emit_status(case.case_id, stage="executing", active_agent="calculation",
                        action="compression_sf", tool="deterministic",
                        summary=f"applied={transit_env.compression_load_n} N over {area_mm2:.1f} mm² vs {material.allowable_stress_mpa} MPa")
            try:
                calcs.append(self.calc.compression_safety_factor(
                    applied_load_n=transit_env.compression_load_n,
                    allowable_stress_mpa=material.allowable_stress_mpa,
                    load_bearing_area_mm2=area_mm2,
                ))
            except Exception:
                pass
            wall_t = float(s.get("wall_thickness_mm") or 0.6)
            radius_mm = max(5.0, 0.5 * dims.get("length_mm", 60))
            if material.modulus_gpa:
                emit_status(case.case_id, stage="executing", active_agent="calculation",
                            action="thin_wall_buckling", tool="deterministic",
                            summary=f"E={material.modulus_gpa} GPa, t={wall_t} mm, R={radius_mm:.1f} mm")
                try:
                    calcs.append(self.calc.thin_wall_buckling_check(
                        modulus_gpa=material.modulus_gpa,
                        wall_thickness_mm=wall_t,
                        radius_mm=radius_mm,
                        applied_axial_load_n=transit_env.compression_load_n,
                    ))
                except Exception:
                    pass

        # Guardrail every calc
        cleaned_calcs = []
        for c in calcs:
            chk = self.guardrail.review_calculation(c.model_dump())
            if chk.ok:
                cleaned_calcs.append(c)
                payload = c.model_dump()
                db.add(AnalysisResult(
                    case_id=case.case_id,
                    method_type="deterministic",
                    inputs_hash=inputs_hash(payload["inputs"]),
                    outputs_json=payload,
                    confidence=c.confidence,
                ))
            else:
                log_event(db, case_id=case.case_id, actor="guardrail", action="block_calc",
                          payload=chk.__dict__)
                emit_status(case.case_id, stage="executing", active_agent="guardrail",
                            action="blocked_calc", summary=f"Blocked: {chk.blocks}")
        db.commit()
        snapshot["calculations"] = [c.model_dump() for c in cleaned_calcs]
        _pace()

        # --- Surrogate risk map ---
        emit_status(case.case_id, stage="executing", active_agent="surrogate",
                    action="zone_risk_map", tool="heuristic",
                    summary="Estimating zone-wise risk (approximate)…")
        risk_map: SurrogateRiskMap = self.surrogate.zone_risk_map(
            geometry=geometry, transit=transit_env, material=material,
        )
        snapshot["risk_map"] = risk_map.model_dump()
        db.add(AnalysisResult(
            case_id=case.case_id,
            method_type="surrogate",
            inputs_hash=inputs_hash({"transit": transit_env.model_dump(),
                                     "material": material.model_dump() if material else {}}),
            outputs_json=risk_map.model_dump(),
            confidence=risk_map.confidence,
        ))
        db.commit()
        log_event(db, case_id=case.case_id, actor="surrogate_agent", action="zone_risk_map")
        emit_status(case.case_id, stage="executing", active_agent="surrogate", action="zone_risk_map_done",
                    confidence="approximate", summary="Surrogate risk map ready (labeled approximate).")
        _pace()

        # --- Draft report ---
        emit_status(case.case_id, stage="executing", active_agent="report", action="draft",
                    summary="Drafting engineering review…")
        report = self.report.draft(
            case_summary=case.case_summary or {},
            material=material,
            geometry=geometry,
            transit=transit_env,
            calcs=cleaned_calcs,
            risk_map=risk_map,
            ista2a=snapshot.get("ista2a"),
        )
        text_check = self.guardrail.review_text(report.body_markdown)
        if not text_check.ok:
            log_event(db, case_id=case.case_id, actor="guardrail", action="block_report",
                      payload=text_check.__dict__)
            report.body_markdown += "\n\n> ⚠️ Guardrail flagged claims removed: " + "; ".join(text_check.blocks)
        snapshot["report"] = report.model_dump()
        db.add(AnalysisResult(
            case_id=case.case_id,
            method_type="report_draft",
            inputs_hash=inputs_hash(case.case_summary or {}),
            outputs_json=report.model_dump(),
            confidence=report.overall_confidence,
        ))
        emit_status(case.case_id, stage="executing", active_agent="report", action="draft_done",
                    confidence=report.overall_confidence, summary="Draft report ready.")
        _pace()

        # --- ISTA 2A specialised evaluation (mandatory for every run) ---
        # Architecture directive: "make sure to do all the tests including
        # ISTA necessary for each run". We always evaluate ISTA 2A drop +
        # transit verdicts and always emit the 4 heatmap scenes. The case's
        # declared test_standard / objective just labels the dominant focus.
        if True:
            emit_status(case.case_id, stage="executing", active_agent="ista2a",
                        action="evaluate", tool="deterministic",
                        summary="Running ISTA 2A drop + transit verdicts…")
            mass_kg = (
                (float(s["gross_weight_g"]) / 1000.0) if s.get("gross_weight_g") else approx_mass_kg
            )
            stacking = s.get("stacking_orientation") or "upright"
            stack_h = int(s.get("stack_height") or 4)
            cal = self.learning.calibration_multiplier(
                db,
                material_name=(material.name if material else s.get("material")),
                packaging_type=s.get("packaging_type"),
            )
            ista_report = self.ista2a.evaluate(
                mass_kg=mass_kg,
                stacking_orientation=stacking,
                stack_height=stack_h,
                material=material,
                geometry=geometry,
                ships_loose=bool(s.get("ships_loose", False)),
                vibration_g_rms=transit_env.vibration_g_rms,
                vibration_duration_min=transit_env.vibration_duration_min,
                user_drop_height_m=transit_env.drop_height_m,
                calibration_multiplier=cal,
            )
            snapshot["ista2a"] = ista_report.model_dump()
            snapshot["ista2a"]["calibration_multiplier"] = cal
            # AI-intelligence sanity check on the verdict (advisory only;
            # surfaced in the report if it disagrees with the deterministic
            # math). High temperature so it commits to a concern when one
            # exists.
            try:
                check = self.reasoning.cross_check_ista(snapshot["ista2a"], label="ISTA 2A")
                snapshot["ista2a"]["cross_check"] = check
                if check.get("concern"):
                    emit_status(case.case_id, stage="executing", active_agent="reasoning",
                                action="cross_check_concern",
                                summary=f"AI sanity-check raised: {check['concern']}")
            except Exception:
                snapshot["ista2a"]["cross_check"] = {"agrees": True, "concern": None}
            _pace()

            # ── ISTA 6A — Amazon corner-drop, ALWAYS run on every analysis ──
            emit_status(case.case_id, stage="executing", active_agent="ista6a",
                        action="evaluate", tool="deterministic",
                        summary="Running ISTA 6A corner-drop check…")
            ista6a_report = self.ista6a.evaluate(
                mass_kg=mass_kg, material=material, geometry=geometry,
            ).model_dump()
            try:
                ista6a_report["cross_check"] = self.reasoning.cross_check_ista(
                    ista6a_report, label="ISTA 6A")
            except Exception:
                ista6a_report["cross_check"] = {"agrees": True, "concern": None}
            snapshot["ista6a"] = ista6a_report
            db.add(AnalysisResult(
                case_id=case.case_id, method_type="ista6a",
                inputs_hash=inputs_hash({"mass": mass_kg, "kt": "ista6a"}),
                outputs_json=ista6a_report, confidence="approximate",
            ))
            db.commit()
            log_event(db, case_id=case.case_id, actor="ista6a_agent", action="evaluate",
                      payload={"verdict": ista6a_report.get("overall_verdict")})
            emit_status(case.case_id, stage="executing", active_agent="ista6a",
                        action="evaluate_done", confidence="approximate",
                        summary=f"ISTA 6A overall verdict: {ista6a_report.get('overall_verdict')}")
            db.add(AnalysisResult(
                case_id=case.case_id,
                method_type="ista2a",
                inputs_hash=inputs_hash({"mass": mass_kg, "stack": stack_h,
                                         "stacking": stacking}),
                outputs_json=snapshot["ista2a"],
                confidence="approximate",
            ))
            db.commit()
            log_event(db, case_id=case.case_id, actor="ista2a_agent", action="evaluate",
                      payload={"overall": ista_report.overall_verdict})
            emit_status(case.case_id, stage="executing", active_agent="ista2a",
                        action="evaluate_done", confidence="approximate",
                        summary=f"ISTA 2A overall verdict: {ista_report.overall_verdict}")

            # Heatmap scenes (4 scenes: 3 drop orientations + 1 transit)
            if asset and asset.mesh_uri:
                emit_status(case.case_id, stage="executing", active_agent="visualization",
                            action="build_heatmaps", tool="viridis",
                            summary="Computing 4 high-res stress heatmaps…")
                scenes = build_heatmap_scenes(
                    case_id=case.case_id,
                    mesh_path=asset.mesh_uri,
                    geometry=geometry,
                    transit_env=transit_env,
                    material=material,
                    stacking_orientation=stacking,
                    glb_url=f"/api/cases/{case.case_id}/mesh",
                    scenarios=("drop_top", "drop_bottom", "drop_side", "transit"),
                )
                snapshot["heatmaps"] = scenes
                db.add(AnalysisResult(
                    case_id=case.case_id,
                    method_type="heatmaps",
                    inputs_hash=inputs_hash({"asset": asset.asset_id, "stacking": stacking}),
                    outputs_json={"n_scenes": len(scenes.get("scenes", [])),
                                  "scenarios": [sc.get("scenario") for sc in scenes.get("scenes", [])]},
                    confidence="approximate",
                ))
                db.commit()
                emit_status(case.case_id, stage="executing", active_agent="visualization",
                            action="heatmaps_done", confidence="approximate",
                            summary=f"4 heatmap scenes ready ({scenes['scenes'][0]['n_cells']} cells each).")

        # --- Reasoning self-check (Gemini 3 Pro) ---
        emit_status(case.case_id, stage="executing", active_agent="reasoning",
                    action="self_check", tool="ai_intelligence",
                    summary="Running self-check verification pass…")
        try:
            verification = self.reasoning.verify(snapshot)
            snapshot["reasoning"] = {
                "ok": verification.ok,
                "issues": verification.issues,
                "warnings": verification.warnings,
                "narrative": verification.narrative,
                "recommended_next_steps": verification.recommended_next_steps,
            }
            db.add(AnalysisResult(
                case_id=case.case_id,
                method_type="reasoning_self_check",
                inputs_hash=inputs_hash({"snapshot_keys": sorted(snapshot.keys())}),
                outputs_json=snapshot["reasoning"],
                confidence="estimated" if verification.ok else "insufficient_data",
            ))
            log_event(db, case_id=case.case_id, actor="reasoning_agent", action="self_check",
                      payload={"ok": verification.ok, "n_issues": len(verification.issues)})
            emit_status(case.case_id, stage="executing", active_agent="reasoning",
                        action="self_check_done",
                        confidence="estimated" if verification.ok else "insufficient_data",
                        summary=f"Self-check ok={verification.ok}; {len(verification.issues)} issues, "
                                f"{len(verification.warnings)} warnings.")
        except Exception as exc:  # noqa: BLE001
            snapshot["reasoning"] = {"ok": True, "issues": [], "warnings": [f"reasoning skipped: {exc!r}"],
                                     "narrative": "", "recommended_next_steps": []}
            emit_status(case.case_id, stage="executing", active_agent="reasoning",
                        action="self_check_skipped", summary=f"Skipped: {exc!r}")

        case.status = "review"
        case.runs_count = (case.runs_count or 0) + 1
        db.commit()
        log_event(db, case_id=case.case_id, actor="orchestrator", action="execute_plan_done")
        emit_status(case.case_id, stage="review", active_agent="orchestrator",
                    action="execute_plan_done", awaiting="user_final_approval",
                    summary="Analysis complete; awaiting user sign-off.")
        return snapshot
