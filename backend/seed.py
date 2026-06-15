"""Startup seeding.

Two responsibilities, both idempotent:

1. Seed (and *update*) materials from data/materials.json. We do an upsert by
   `name` so newly added PCR / carbon fields populate on existing rows from
   older DB snapshots.
2. Ensure the admin user exists. The platform requires one administrator who
   can allocate simulation tokens to other users. Default credentials are read
   from environment variables and fall back to the spec's seed account.
"""
from __future__ import annotations

import json
import os

from sqlalchemy.orm import Session

from .config import PROJECT_ROOT
from .models import MaterialRecord, TokenLedger, User
from .services.auth import hash_password


MATERIAL_UPDATABLE_FIELDS = (
    "grade",
    "density_kg_m3",
    "modulus_gpa",
    "yield_strength_mpa",
    "allowable_stress_mpa",
    "is_pcr",
    "recycled_content_pct",
    "carbon_intensity_kg_co2e_per_kg",
    "pcr_substitute_for",
    "notes",
    "source",
)


def seed_materials(db: Session) -> int:
    """Upsert by name. Returns number of *new* rows inserted; updates to
    existing rows are applied silently."""
    path = PROJECT_ROOT / "data" / "materials.json"
    if not path.exists():
        return 0
    items = json.loads(path.read_text())
    inserted = 0
    for item in items:
        existing = (
            db.query(MaterialRecord)
            .filter(MaterialRecord.name.ilike(item["name"]))
            .first()
        )
        if existing:
            changed = False
            for field in MATERIAL_UPDATABLE_FIELDS:
                if field in item and getattr(existing, field, None) != item[field]:
                    setattr(existing, field, item[field])
                    changed = True
            if changed:
                db.add(existing)
            continue
        db.add(MaterialRecord(**item))
        inserted += 1
    db.commit()
    return inserted


UNIVERSAL_TOKEN_FLOOR = 20


def topup_all_users(db: Session) -> int:
    """Make sure every active user has at least UNIVERSAL_TOKEN_FLOOR tokens.

    Idempotent — anyone already at or above the floor is untouched. For each
    user we lift, we write a TokenLedger row so the change is auditable. The
    admin account is excluded (it already has a much larger seed allocation).
    Returns the number of users credited."""
    floor = UNIVERSAL_TOKEN_FLOOR
    credited = 0
    admins = {a.user_id for a in db.query(User).filter(User.role == "admin").all()}
    rows = db.query(User).filter(User.is_active.is_(True)).all()
    for u in rows:
        if u.user_id in admins:
            continue
        current = int(u.token_balance or 0)
        if current >= floor:
            continue
        delta = floor - current
        u.token_balance = floor
        db.add(u)
        db.add(TokenLedger(
            user_id=u.user_id,
            delta=delta,
            balance_after=floor,
            reason="universal_floor_topup",
            actor_user_id=None,
            notes=f"Auto top-up to {floor} tokens.",
        ))
        credited += 1
    if credited:
        db.commit()
    return credited


def seed_admin(db: Session) -> bool:
    """Ensure the seed admin user exists. Returns True if newly created."""
    email = os.environ.get("ADMIN_EMAIL", "mayank.divakar@example.com").lower().strip()
    password = os.environ.get("ADMIN_PASSWORD", "Deemo1234")
    name = os.environ.get("ADMIN_NAME", "Mayank Divakar")
    starting_tokens = int(os.environ.get("ADMIN_TOKENS", "999"))

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        # Keep admin role and ensure account stays active.
        changed = False
        if existing.role != "admin":
            existing.role = "admin"
            changed = True
        if not existing.is_active:
            existing.is_active = True
            changed = True
        if changed:
            db.add(existing)
            db.commit()
        return False

    admin = User(
        email=email,
        password_hash=hash_password(password),
        name=name,
        role="admin",
        token_balance=starting_tokens,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    db.add(TokenLedger(
        user_id=admin.user_id,
        delta=starting_tokens,
        balance_after=starting_tokens,
        reason="admin_seed",
        actor_user_id=admin.user_id,
        notes="Initial admin allocation at first boot.",
    ))
    db.commit()
    return True
