"""Pydantic schemas for API I/O and agent contracts.

Every agent returns a typed schema so we can validate before display (section 9.2).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------- Case / message API schemas ----------

class CaseCreate(BaseModel):
    user_id: str = "anon"


class CaseRead(BaseModel):
    case_id: str
    status: str
    approval_state: str
    packaging_type: Optional[str]
    product_type: Optional[str]
    objective: Optional[str]
    case_summary: dict
    created_at: datetime

    class Config:
        from_attributes = True


class MessageIn(BaseModel):
    content: str


class MessageOut(BaseModel):
    role: str
    content: str
    metadata: dict = Field(default_factory=dict)
    created_at: datetime


class ApprovalDecision(BaseModel):
    approve: bool
    edits: dict[str, Any] = Field(default_factory=dict)


# ---------- Agent I/O schemas ----------

ConfidenceLabel = Literal["verified", "estimated", "approximate", "insufficient_data"]


class IntakeFields(BaseModel):
    """Structured fields the Intake + BottleFlow agents extract from chat.

    Generic packaging fields apply to every case. Bottle-flow fields are only
    asked when packaging_type is bottle/bottle_like."""
    # ---- generic (any packaging) ----
    packaging_type: Optional[str] = None     # bottle, bottle_like, crate, carton, pouch, secondary_pack
    product_type: Optional[str] = None       # liquid, powder, viscous, fragile, pressurized
    objective: Optional[str] = None          # concept_check, ista_planning, transit_survivability, geometry_risk
    material: Optional[str] = None
    wall_thickness_mm: Optional[float] = None
    transit_modes: list[str] = Field(default_factory=list)
    has_geometry: Optional[bool] = None
    test_standard: Optional[str] = None

    # ---- bottle-specific (only populated in the bottle flow) ----
    bottle_subtype: Optional[str] = None         # water, soda, oil, medicine, cosmetic, beer, juice, milk, other
    capacity_ml: Optional[float] = None
    gross_weight_g: Optional[float] = None
    empty_weight_g: Optional[float] = None
    fill_level_pct: Optional[float] = None       # 0..100
    closure_type: Optional[str] = None           # screw_cap, sports_cap, cork, crown, snap_on, push_pull
    road_condition: Optional[str] = None         # smooth_highway, mixed, rough_secondary, off_road
    stacking_orientation: Optional[str] = None   # upright, on_side, inverted (transit stack orientation)
    stack_height: Optional[int] = None           # number of bottles in a stacked transit unit
    ships_loose: Optional[bool] = None           # True when bottle ships unwrapped on a pallet
    ship_severity: Optional[str] = None          # ship route severity: clean | moderate | severe

    # ---- meta ----
    confidence: float = 0.0
    missing_fields: list[str] = Field(default_factory=list)


class IntakeResponse(BaseModel):
    """Wraps a chat reply alongside extracted state."""
    reply: str
    fields: IntakeFields
    next_questions: list[str] = Field(default_factory=list)
    ready_for_plan: bool = False


class PlanStep(BaseModel):
    agent: str
    action: str
    rationale: str


class ProposedPlan(BaseModel):
    """The summary the user must approve before any analysis runs."""
    case_summary: dict
    assumptions: list[str]
    steps: list[PlanStep]
    confidence: ConfidenceLabel = "estimated"


class MaterialLookupResult(BaseModel):
    name: str
    grade: Optional[str] = None
    density_kg_m3: Optional[float] = None
    modulus_gpa: Optional[float] = None
    yield_strength_mpa: Optional[float] = None
    allowable_stress_mpa: Optional[float] = None
    is_pcr: bool = False
    recycled_content_pct: float = 0.0
    carbon_intensity_kg_co2e_per_kg: Optional[float] = None
    pcr_substitute_for: Optional[str] = None
    notes: Optional[str] = None
    source: str
    confidence: ConfidenceLabel = "verified"
    caveats: list[str] = Field(default_factory=list)


class GeometrySummary(BaseModel):
    file_type: str
    bbox_mm: dict[str, float]                     # min/max per axis
    overall_dims_mm: dict[str, float]             # length, width, height
    volume_mm3: Optional[float] = None
    surface_area_mm2: Optional[float] = None
    critical_zones: list[str] = Field(default_factory=list)
    confidence: ConfidenceLabel = "approximate"
    notes: list[str] = Field(default_factory=list)


class TransitEnvelope(BaseModel):
    mode_mix: dict[str, float]
    vibration_g_rms: float
    vibration_duration_min: float = 60.0
    drop_height_m: float
    compression_load_n: float
    handling_fraction: float
    dominant_risks: list[str]
    suggested_test_sequence: list[str]
    confidence: ConfidenceLabel = "estimated"


class CalculationOutput(BaseModel):
    label: str                       # "drop_energy", "compression_safety_factor", etc.
    value: float
    units: str
    formula: str
    inputs: dict[str, Any]
    safety_factor: Optional[float] = None
    risk_flag: bool = False
    confidence: ConfidenceLabel = "estimated"


class ZoneRisk(BaseModel):
    zone: str                        # base, shoulder, neck, side_wall, closure, corner
    risk_score: float                # 0..1
    rationale: str


class SurrogateRiskMap(BaseModel):
    zones: list[ZoneRisk]
    approximation_warning: str = (
        "Heuristic surrogate analysis. Treat values as risk indicators, not as validated FEA."
    )
    confidence: ConfidenceLabel = "approximate"


class ReportDraft(BaseModel):
    title: str
    case_summary: dict
    assumptions: list[str]
    findings: list[str]
    risks: list[str]
    next_steps: list[str]
    overall_confidence: ConfidenceLabel = "estimated"
    body_markdown: str


# ---------- PCR / Sustainability ----------

class PCRSubstitution(BaseModel):
    """Result of a virgin → PCR substitution analysis.

    All numbers are derived from grounded inputs (material DB densities,
    carbon intensities, and the part mass from CAD) — no LLM-supplied values.
    """
    baseline_material: str
    baseline_density_kg_m3: float
    baseline_carbon_kg_co2e_per_kg: Optional[float] = None
    baseline_part_mass_g: float
    baseline_part_carbon_kg_co2e: Optional[float] = None

    candidate_material: str
    candidate_density_kg_m3: float
    candidate_recycled_content_pct: float
    candidate_carbon_kg_co2e_per_kg: Optional[float] = None
    candidate_part_mass_g: float
    candidate_part_carbon_kg_co2e: Optional[float] = None

    mass_delta_pct: float           # negative = candidate is lighter
    carbon_delta_pct: Optional[float] = None    # negative = candidate has lower footprint
    annual_carbon_savings_kg_co2e: Optional[float] = None
    annual_units: int = 1_000_000

    mechanical_delta: dict = Field(default_factory=dict)   # e.g. {"modulus_pct": -3.5}
    caveats: list[str] = Field(default_factory=list)
    formula: str = (
        "part_mass = part_volume × density; "
        "part_carbon = part_mass × carbon_intensity; "
        "annual_savings = (baseline_part_carbon − candidate_part_carbon) × annual_units"
    )
    # Which packaging component is being compared. Defaults to bottle-shell so
    # existing bottle responses are unaffected; set explicitly for packet/brush.
    pcr_component: str = "Bottle shell"


# ---------- Auth / Tokens ----------

class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: "UserOut"


class UserOut(BaseModel):
    user_id: str
    email: str
    name: Optional[str] = None
    role: str
    token_balance: int
    is_active: bool

    class Config:
        from_attributes = True


class UserCreateRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    role: str = "user"             # admin can create either role
    initial_tokens: int = 20       # everyone starts with 20 tokens


class TokenGrantRequest(BaseModel):
    delta: int                     # positive grants, negative debits
    notes: Optional[str] = None


class TokenPurchaseRequest(BaseModel):
    """Inline payment stub. In production, swap for a real PayU/Stripe callback."""
    pack: str                      # "10" | "50" | "200"
    payment_token: Optional[str] = None    # ignored in stub mode


LoginResponse.model_rebuild()
