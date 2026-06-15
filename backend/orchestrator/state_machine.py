"""Explicit state machine for a case (section 6).

Stages:
    intake          -> assistant is gathering minimum required info
    clarification   -> still asking targeted follow-ups
    plan_proposed   -> a plan summary is on screen, awaiting approval
    executing       -> approved tools/agents are running
    review          -> intermediate results shown, awaiting final approval
    final_approved  -> user approved the final report
    finalized       -> report stored, case closed for new analysis
"""
from __future__ import annotations

from typing import Final


STAGES: Final = (
    "intake",
    "clarification",
    "plan_proposed",
    "executing",
    "review",
    "final_approved",
    "finalized",
)


_LEGAL_TRANSITIONS: Final[dict[str, set[str]]] = {
    "intake": {"intake", "clarification", "plan_proposed"},
    "clarification": {"clarification", "plan_proposed", "intake"},
    "plan_proposed": {"executing", "clarification", "intake"},
    "executing": {"review", "executing"},
    "review": {"executing", "final_approved", "clarification"},
    "final_approved": {"finalized"},
    "finalized": {"finalized"},
}


def can_transition(current: str, target: str) -> bool:
    return target in _LEGAL_TRANSITIONS.get(current, set())


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Illegal stage transition: {current} -> {target}")
