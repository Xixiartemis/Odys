"""Generic typed boundaries for benchmark-scoped attempt termination.

The boundary is deliberately independent of benchmark configuration.  The
official adapter may install it when a frozen fault declares an
``attempt_terminal`` type; runtime factories only translate the typed signal
into a normal failed attempt, leaving cross-attempt recovery to the existing
RecoveryAuthority.
"""

from __future__ import annotations

from typing import Any

from lhas.native.models import NativeFaultPoint


class AttemptTerminalFailure(RuntimeError):
    """A controlled failure that ends the current attempt before tool commit."""

    terminal_type = "ATTEMPT_TERMINAL"
    failure_type = "TOOL_ERROR"

    def __init__(self, fault_id: str):
        self.fault_id = str(fault_id)
        super().__init__(self.terminal_type)


class AttemptTerminalFaultInjector:
    """Fire one pre-commit terminal fault at the native tool boundary."""

    def __init__(self, fault: Any):
        self.fault = fault
        self.fired = False
        self.fired_point: str | None = None

    def hit(self, point: Any, **context: Any) -> None:
        if self.fired or getattr(self.fault, "fault_type", None) != "attempt_terminal":
            return
        point_value = point.value if hasattr(point, "value") else str(point)
        if point_value != NativeFaultPoint.AFTER_TOOL_REQUESTED.value:
            return
        self.fired = True
        self.fired_point = point_value
        raise AttemptTerminalFailure(getattr(self.fault, "fault_id", "unknown"))
