"""ORM entities mirroring the data model in section 11 of the architecture doc."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class Case(Base):
    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), default="anon")
    design_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    packaging_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    product_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    objective: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="intake")
    approval_state: Mapped[str] = mapped_column(String(32), default="pending")
    case_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    is_saved: Mapped[bool] = mapped_column(default=False)
    runs_count: Mapped[int] = mapped_column(default=0)
    # Per-stage workflow progress (intake/geometry/material/transit/analysis/results/report/signoff)
    stage_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # Sign-off + locking (M14)
    signed_off_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    signed_off_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    signoff_hash:  Mapped[Optional[str]]  = mapped_column(String(64), nullable=True)
    signoff_notes: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    locked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages: Mapped[list["Message"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    geometry: Mapped[list["GeometryAsset"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    transit: Mapped[list["TransitProfile"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    results: Mapped[list["AnalysisResult"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    events: Mapped[list["AuditEvent"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant" | "system"
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    case: Mapped[Case] = relationship(back_populates="messages")


class GeometryAsset(Base):
    __tablename__ = "geometry_assets"

    asset_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    file_type: Mapped[str] = mapped_column(String(16))   # step, stl, obj, glb
    storage_uri: Mapped[str] = mapped_column(String(512))
    mesh_uri: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    bounding_box: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    critical_zones: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    case: Mapped[Case] = relationship(back_populates="geometry")


class MaterialRecord(Base):
    """Verified material entry. Seeded from data/materials.json on startup."""
    __tablename__ = "materials"

    material_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), index=True)
    grade: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    density_kg_m3: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    modulus_gpa: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yield_strength_mpa: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    allowable_stress_mpa: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # PCR / sustainability metadata. Optional so legacy DB rows remain valid.
    is_pcr: Mapped[bool] = mapped_column(Boolean, default=False)
    recycled_content_pct: Mapped[float] = mapped_column(Float, default=0.0)
    carbon_intensity_kg_co2e_per_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pcr_substitute_for: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(128), default="seed:materials.json")


class TransitProfile(Base):
    __tablename__ = "transit_profiles"

    profile_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    mode_mix: Mapped[dict] = mapped_column(JSON, default=dict)         # {truck:0.6, ship:0.3, ...}
    vibration_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # g_rms
    drop_height_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    compression_load_n: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    handling_fraction: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    case: Mapped[Case] = relationship(back_populates="transit")


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    result_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    method_type: Mapped[str] = mapped_column(String(64))  # "deterministic", "surrogate", ...
    inputs_hash: Mapped[str] = mapped_column(String(64))
    outputs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[str] = mapped_column(String(32), default="estimated")
    approved_by_user: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    case: Mapped[Case] = relationship(back_populates="results")


class Feedback(Base):
    """User feedback on a case turn, report, or alternative design.

    Aggregated per user_id into a lightweight preference profile that nudges
    intake / report verbosity, depth, and optimisation defaults on subsequent
    runs."""
    __tablename__ = "feedback"

    feedback_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[Optional[str]] = mapped_column(ForeignKey("cases.case_id"), nullable=True)
    user_id: Mapped[str] = mapped_column(String(64), default="anon", index=True)
    target: Mapped[str] = mapped_column(String(64))   # "report" | "intake" | "optimization" | "viewer"
    rating: Mapped[int] = mapped_column(default=0)    # -1, 0, +1
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)   # e.g. {"too_verbose": true}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OptimizationRun(Base):
    """One optimization session run on a base case. Holds the user-stated
    intent and the 3 alternative designs produced (with their evaluations)."""
    __tablename__ = "optimization_runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    intent: Mapped[str] = mapped_column(String(64))   # "reduce_cost" | "increase_strength" | "other"
    intent_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alternatives: Mapped[dict] = mapped_column(JSON, default=dict)   # list of design dicts
    comparison: Mapped[dict] = mapped_column(JSON, default=dict)     # dashboard payload
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PacketOptimizationRun(Base):
    """One packet optimisation session. Holds the user's intent and the 3
    alternative packet designs produced (with their heuristic evaluations).
    Parallel to OptimizationRun — completely separate table, no shared logic."""
    __tablename__ = "packet_optimization_runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    intent: Mapped[str] = mapped_column(String(64))   # "reduce_cost" | "improve_survivability" | "improve_shelf_life" | "other"
    intent_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alternatives: Mapped[dict] = mapped_column(JSON, default=dict)   # list of PacketDesignVariant dicts
    comparison: Mapped[dict] = mapped_column(JSON, default=dict)     # {"rows": [...], "narrative": "..."}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BrushOptimizationRun(Base):
    """One brush optimisation session. Holds the user's intent and the 3
    alternative brush packaging designs produced (with their heuristic evaluations).
    Parallel to PacketOptimizationRun — completely separate table, no shared logic."""
    __tablename__ = "brush_optimization_runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    intent: Mapped[str] = mapped_column(String(64))   # "reduce_cost" | "improve_survivability" | "improve_sustainability" | "other"
    intent_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alternatives: Mapped[dict] = mapped_column(JSON, default=dict)   # list of BrushDesignVariant dicts
    comparison: Mapped[dict] = mapped_column(JSON, default=dict)     # {"rows": [...], "narrative": "..."}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"))
    actor: Mapped[str] = mapped_column(String(64))   # "user", "orchestrator", agent name
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    case: Mapped[Case] = relationship(back_populates="events")


class User(Base):
    """Platform user. Each user holds a token balance; 1 token = 1 simulation
    run (one approved-plan execution). Admins can allocate tokens to other
    users via the admin endpoints."""
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")   # "admin" | "user"
    # Every new user starts with a 20-token allowance — enough to run a
    # handful of simulations before they need to top up.
    token_balance: Mapped[int] = mapped_column(Integer, default=20)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TokenLedger(Base):
    """Append-only ledger of token movements. `delta` is +N for allocations /
    purchases and −N for consumed simulations. `balance_after` is recorded so
    audit can verify the running balance without recomputing from scratch."""
    __tablename__ = "token_ledger"

    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))   # "admin_grant", "simulation_run", "purchase", "signup_bonus"
    case_id: Mapped[Optional[str]] = mapped_column(ForeignKey("cases.case_id"), nullable=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # who triggered the change
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AccuracyRecord(Base):
    """A retroactive comparison between what the platform predicted and what
    a real-world ISTA test actually showed. The LearningAgent reads these to
    produce a calibration multiplier per material / packaging type, so
    subsequent surrogate runs nudge their safety factors toward observed
    reality. Crucially: the predicted numbers are frozen at the time of
    submission — re-running the simulation later does NOT overwrite history.
    """
    __tablename__ = "accuracy_records"

    record_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), index=True)
    material_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    packaging_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    test_standard: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Predicted (frozen at submission time)
    predicted_verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    predicted_min_sf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Actual (entered by user)
    actual_verdict: Mapped[str] = mapped_column(String(16))   # pass | fail | partial
    actual_failure_mode: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    actual_drop_height_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Computed
    delta_min_sf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    calibration_multiplier: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    learning_narrative: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    """Lightweight server-side session. Avoids pulling in a full JWT library;
    the token is a 32-byte random hex string, looked up on every authed call.
    Sessions don't expire automatically — they last until logout or until the
    user row is deactivated."""
    __tablename__ = "auth_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
