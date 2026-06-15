"""Common base for all specialist agents.

Constraints from section 5/22:
- low temperature
- narrow prompt scope
- structured outputs
- never invent values; tools/db only
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentContext:
    case_id: str
    actor: str          # used in audit log
    user_id: str = "anon"


GROUND_RULES = (
    "Operate at low temperature. Stay within your single role. "
    "Never invent material properties, test outcomes, dimensions, or compliance claims. "
    "If data is missing, return that fact instead of guessing. "
    "Label every output as one of: verified | estimated | approximate | insufficient_data. "
    "Write in British English. Do not mention any company names or the underlying model "
    "provider; the user only sees results, not which LLM produced them."
)
