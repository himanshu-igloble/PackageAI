"""Authentication, user / token administration, and inline billing stub.

Endpoints:

  POST /api/auth/login                  email + password → bearer token
  POST /api/auth/logout                 revoke current bearer token
  GET  /api/auth/me                     current user + balance
  GET  /api/auth/users                  admin: list users (token balances)
  POST /api/auth/users                  admin: create a user with initial tokens
  POST /api/auth/users/{uid}/tokens     admin: grant or debit tokens
  POST /api/billing/checkout            user: purchase a token pack (PayU/Stripe stub)
  GET  /api/auth/pricing                publicly viewable token packs

The billing route is a stub: in production it would receive a verified
payment callback from PayU / Stripe before crediting tokens. The current
implementation credits the user's balance synchronously so the end-to-end
flow is exercisable.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import TokenLedger, User
from ..schemas import (
    LoginRequest,
    LoginResponse,
    TokenGrantRequest,
    TokenPurchaseRequest,
    UserCreateRequest,
    UserOut,
)
from ..services.auth import (
    adjust_tokens,
    current_user,
    hash_password,
    issue_session,
    require_admin,
    revoke_session,
    verify_password,
    _extract_token,
)


router = APIRouter()


# Publicly published token packs. Pricing is illustrative; real billing should
# be wired up to a payment processor and these numbers driven by config.
TOKEN_PACKS = {
    "starter":      {"tokens": 10,   "price_usd": 49,    "label": "Starter pack — 10 simulations"},
    "team":         {"tokens": 50,   "price_usd": 199,   "label": "Team pack — 50 simulations"},
    "enterprise":   {"tokens": 200,  "price_usd": 699,   "label": "Enterprise pack — 200 simulations"},
}


def _user_out(u: User) -> UserOut:
    return UserOut(
        user_id=u.user_id,
        email=u.email,
        name=u.name,
        role=u.role,
        token_balance=int(u.token_balance or 0),
        is_active=bool(u.is_active),
    )


# -------------------------------------------------------------------- login --

@router.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    email = (payload.email or "").lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = issue_session(db, user)
    return LoginResponse(token=token, user=_user_out(user))


@router.post("/auth/logout")
def logout(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    """Revoke the bearer token from the Authorization header (if present)."""
    token = _extract_token(authorization)
    if token:
        revoke_session(db, token)
    return {"status": "ok"}


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return _user_out(user)


# ------------------------------------------------------------ admin: users --

@router.get("/auth/users", response_model=list[UserOut])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(User).order_by(User.created_at.asc()).all()
    return [_user_out(u) for u in rows]


@router.post("/auth/users", response_model=UserOut)
def create_user(
    payload: UserCreateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    email = (payload.email or "").lower().strip()
    if not email or not payload.password:
        raise HTTPException(status_code=400, detail="email and password required")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="email already registered")
    if payload.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")
    initial = max(0, int(payload.initial_tokens or 0))

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        name=payload.name,
        role=payload.role,
        token_balance=initial,
    )
    db.add(user)
    db.flush()
    if initial:
        db.add(TokenLedger(
            user_id=user.user_id,
            delta=initial,
            balance_after=initial,
            reason="admin_grant",
            actor_user_id=admin.user_id,
            notes="Initial allocation at user creation.",
        ))
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.post("/auth/users/{uid}/tokens", response_model=UserOut)
def grant_tokens(
    uid: str,
    payload: TokenGrantRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if payload.delta == 0:
        raise HTTPException(status_code=400, detail="delta must be non-zero")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    # Refuse to drive balance negative.
    new_balance = int(user.token_balance or 0) + int(payload.delta)
    if new_balance < 0:
        raise HTTPException(status_code=400, detail="cannot drive balance below zero")
    adjust_tokens(
        db,
        user=user,
        delta=int(payload.delta),
        reason="admin_grant" if payload.delta > 0 else "admin_debit",
        actor_user_id=admin.user_id,
        notes=payload.notes,
    )
    db.refresh(user)
    return _user_out(user)


# ------------------------------------------------------- inline billing --

@router.get("/auth/pricing")
def pricing():
    """Publicly viewable token packs (no auth)."""
    return {"packs": TOKEN_PACKS}


@router.post("/billing/checkout", response_model=UserOut)
def checkout(
    payload: TokenPurchaseRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Inline payment stub.

    Production note: this endpoint should verify a callback signature from
    PayU / Stripe before crediting tokens. For the demo build it credits
    immediately so the chat-side 'buy credits' prompt is wired end-to-end.
    """
    pack = TOKEN_PACKS.get((payload.pack or "").lower())
    if not pack:
        raise HTTPException(status_code=400, detail=f"unknown pack '{payload.pack}'")
    delta = int(pack["tokens"])
    adjust_tokens(
        db,
        user=user,
        delta=delta,
        reason="purchase",
        actor_user_id=user.user_id,
        notes=f"Purchased '{payload.pack}' pack for ${pack['price_usd']}.",
    )
    db.refresh(user)
    return _user_out(user)
