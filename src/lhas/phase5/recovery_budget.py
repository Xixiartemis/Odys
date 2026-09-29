"""Recovery Budget Gate — controls intervention-level recovery budget.

A3/A4: RecoveryBudgetGate active (tracks intervention budget)
A5: PassThroughRecoveryBudgetGate (always allows, root budget still enforced)

Root budget (max_model_calls, max_turns) is identical across all arms.
Only intervention-specific recovery policy differs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class BudgetDecision(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


@dataclass
class BudgetLedgerEntry:
    step: int
    candidate_action: str
    authorization: str
    remaining_intervention_budget: int
    reason: str


class RecoveryBudgetGate:
    """Active recovery budget gate for A3/A4.

    Tracks intervention-level recovery attempts. When exhausted,
    escalates instead of allowing more recovery.
    """

    def __init__(self, max_recovery_attempts: int = 3):
        self._max_recovery_attempts = max_recovery_attempts
        self._used: int = 0
        self._ledger: list[BudgetLedgerEntry] = []

    def authorize(self, *, step: int, candidate_action: str) -> BudgetDecision:
        """Check if a recovery intervention is allowed."""
        if self._used >= self._max_recovery_attempts:
            decision = BudgetDecision.ESCALATE
        else:
            self._used += 1
            decision = BudgetDecision.ALLOW

        self._ledger.append(BudgetLedgerEntry(
            step=step,
            candidate_action=candidate_action,
            authorization=decision.value,
            remaining_intervention_budget=max(0, self._max_recovery_attempts - self._used),
            reason=f"intervention {self._used}/{self._max_recovery_attempts}",
        ))
        return decision

    @property
    def ledger(self) -> list[Dict[str, Any]]:
        return [
            {"step": e.step, "candidate_action": e.candidate_action,
             "authorization": e.authorization, "remaining": e.remaining_intervention_budget,
             "reason": e.reason}
            for e in self._ledger
        ]

    @property
    def remaining(self) -> int:
        return max(0, self._max_recovery_attempts - self._used)


class PassThroughRecoveryBudgetGate:
    """A5: always allows recovery (no intervention budget gating).

    Root budget (max_model_calls, max_turns) is still enforced identically.
    """

    def __init__(self):
        self._ledger: list[BudgetLedgerEntry] = []

    def authorize(self, *, step: int, candidate_action: str) -> BudgetDecision:
        self._ledger.append(BudgetLedgerEntry(
            step=step,
            candidate_action=candidate_action,
            authorization="ALLOW",
            remaining_intervention_budget=-1,  # unlimited
            reason="pass-through (A5 ablation)",
        ))
        return BudgetDecision.ALLOW

    @property
    def ledger(self) -> list[Dict[str, Any]]:
        return [
            {"step": e.step, "candidate_action": e.candidate_action,
             "authorization": e.authorization, "remaining": e.remaining_intervention_budget,
             "reason": e.reason}
            for e in self._ledger
        ]

    @property
    def remaining(self) -> int:
        return -1  # unlimited
