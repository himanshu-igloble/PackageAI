"""Audit log helper. Every important action passes through here (section 18)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def log_event(db: Session, *, case_id: str, actor: str, action: str, payload: dict[str, Any] | None = None) -> AuditEvent:
    ev = AuditEvent(case_id=case_id, actor=actor, action=action, payload=payload or {})
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev
