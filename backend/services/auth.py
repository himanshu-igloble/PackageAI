"""Authentication primitives.

Password hashing uses PBKDF2-HMAC-SHA256 from the standard library, so no
extra dependency is required. Session tokens are 32-byte random hex strings
stored in the `auth_sessions` table; the API accepts them in the
`Authorization: Bearer <token>` header.

This is deliberately minimal — adequate for the platform's small admin /
analyst user base and trivially replaceable with JWT or an external IdP
later.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuthSession, User


_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """Return a stored-hash string: `pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>`."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algo != _ALGO:
        return False
    try:
        iters_n = int(iters)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters_n)
    return hmac.compare_digest(dk, expected)


def issue_session(db: Session, user: User) -> str:
    """Create and persist a session token for the given user."""
    token = secrets.token_hex(32)
    db.add(AuthSession(token=token, user_id=user.user_id))
    db.commit()
    return token


def revoke_session(db: Session, token: str) -> None:
    db.query(AuthSession).filter(AuthSession.token == token).delete()
    db.commit()


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


def current_user_optional(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """FastAPI dependency: returns the authenticated user or None.

    Used for endpoints that *attribute* actions to a user when one is present
    (e.g. case creation) but don't strictly require auth — so the existing
    smoke flow without a login still works."""
    token = _extract_token(authorization)
    if not token:
        return None
    sess = db.get(AuthSession, token)
    if not sess:
        return None
    user = db.get(User, sess.user_id)
    if not user or not user.is_active:
        return None
    return user


def current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: requires a logged-in user. Raises 401 otherwise."""
    user = current_user_optional(authorization=authorization, db=db)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="administrator privileges required")
    return user


# ---- Token-balance helpers --------------------------------------------------

def adjust_tokens(
    db: Session,
    *,
    user: User,
    delta: int,
    reason: str,
    case_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Apply a balance change atomically and write a ledger row. Returns the
    new balance. Caller is responsible for any 402-style refusal *before*
    calling this."""
    user.token_balance = int(user.token_balance or 0) + int(delta)
    db.add(user)
    db.add(TokenLedgerProxy(
        user_id=user.user_id,
        delta=int(delta),
        balance_after=int(user.token_balance),
        reason=reason,
        case_id=case_id,
        actor_user_id=actor_user_id,
        notes=notes,
    ))
    db.commit()
    return int(user.token_balance)


# Lazy import to avoid circular dependency at module import time.
def TokenLedgerProxy(**kwargs):  # noqa: N802 - factory-style helper
    from ..models import TokenLedger
    return TokenLedger(**kwargs)
