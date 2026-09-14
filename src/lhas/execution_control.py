"""Odys-owned root execution deadline and cancellation contract.

The control token is deliberately small and transport agnostic.  It owns the
root run authority; provider, tool, process, MCP, recovery, and child layers
only derive a bounded view from it.  Component timeout values are ceilings and
can never extend the root deadline.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar


CancellationReason = str
VALID_CANCELLATION_REASONS = frozenset(
    {
        "USER_CANCEL",
        "ROOT_DEADLINE_EXCEEDED",
        "PARENT_CANCELLED",
        "ATTEMPT_TERMINAL",
        "SHUTDOWN",
        "WORKFLOW_ABORT",
        "REPLAN_INVALIDATION",
        "BUDGET_TERMINAL",
    }
)


class ExecutionControlError(RuntimeError):
    """A root control boundary rejected an operation."""

    def __init__(
        self,
        failure_type: str,
        *,
        run_id: str,
        attempt_id: str | None,
        reason: str,
        source: str,
        absolute_deadline: float | None = None,
        terminal: bool = True,
    ) -> None:
        self.failure_type = failure_type
        self.run_id = str(run_id)
        self.attempt_id = str(attempt_id) if attempt_id is not None else None
        self.reason = str(reason)
        self.source = str(source)
        self.absolute_deadline = absolute_deadline
        self.terminal = bool(terminal)
        super().__init__(failure_type)

    def evidence(self) -> dict[str, Any]:
        """Return bounded, secret-free terminal evidence."""
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "reason": self.reason,
            "source": self.source,
            "failure_type": self.failure_type,
            "timestamp": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "absolute_deadline": self.absolute_deadline,
            "terminal": self.terminal,
        }


class ExecutionLayerTimeout(TimeoutError, ExecutionControlError):
    """A component ceiling expired while the root token was still active."""

    def __init__(
        self,
        failure_type: str,
        *,
        run_id: str,
        attempt_id: str | None,
        reason: str,
        source: str,
        absolute_deadline: float | None = None,
    ) -> None:
        ExecutionControlError.__init__(
            self,
            failure_type,
            run_id=run_id,
            attempt_id=attempt_id,
            reason=reason,
            source=source,
            absolute_deadline=absolute_deadline,
            terminal=False,
        )


class ExecutionControlToken:
    """One root-scoped deadline/cancellation authority.

    ``absolute_deadline`` is a value from the monotonic clock supplied at
    construction.  A token can be derived for an attempt/component, but a
    child deadline is always ``min(parent deadline, local ceiling)`` and a
    child cannot outlive parent cancellation.
    """

    def __init__(
        self,
        run_id: str,
        *,
        attempt_id: str | None = None,
        timeout_seconds: float | None = None,
        absolute_deadline: float | None = None,
        parent: "ExecutionControlToken | None" = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if timeout_seconds is not None and float(timeout_seconds) < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if absolute_deadline is not None and timeout_seconds is not None:
            raise ValueError("choose timeout_seconds or absolute_deadline")
        self.run_id = str(run_id)
        self.root_run_id = parent.root_run_id if parent is not None else self.run_id
        self.attempt_id = str(attempt_id) if attempt_id is not None else None
        self.parent = parent
        self._clock = clock
        self._wall_clock = wall_clock
        local_deadline = (
            None
            if absolute_deadline is None
            else float(absolute_deadline)
        )
        if timeout_seconds is not None:
            local_deadline = self._clock() + float(timeout_seconds)
        if parent is not None:
            parent_deadline = parent.absolute_deadline
            if parent_deadline is not None:
                local_deadline = (
                    parent_deadline
                    if local_deadline is None
                    else min(parent_deadline, local_deadline)
                )
            parent._children.append(self)
        self.absolute_deadline = local_deadline
        self.wall_clock_deadline = (
            None
            if local_deadline is None
            else self._wall_clock() + max(0.0, local_deadline - self._clock())
        )
        self._cancelled = False
        self._reason: str | None = None
        self._source: str | None = None
        self._cancelled_at: str | None = None
        self._children: list[ExecutionControlToken] = []
        self._waiters: list[asyncio.Event] = []

    @property
    def cancelled(self) -> bool:
        self._sync_parent()
        return self._cancelled

    @property
    def terminal(self) -> bool:
        self._sync_parent()
        return self._cancelled or self._deadline_passed()

    @property
    def cancellation_reason(self) -> str | None:
        self._sync_parent()
        return self._reason

    @property
    def cancellation_timestamp(self) -> str | None:
        self._sync_parent()
        return self._cancelled_at

    @property
    def terminal_reason(self) -> str | None:
        self._sync_parent()
        if self._reason:
            return self._reason
        if self._deadline_passed():
            return "ROOT_DEADLINE_EXCEEDED"
        return None

    def _deadline_passed(self) -> bool:
        return self.absolute_deadline is not None and self._clock() >= self.absolute_deadline

    def _sync_parent(self) -> None:
        if self.parent is not None:
            try:
                self.parent.check()
            except ExecutionControlError as exc:
                self._terminate(
                    "PARENT_CANCELLED"
                    if exc.failure_type != "ROOT_DEADLINE_EXCEEDED"
                    else "ROOT_DEADLINE_EXCEEDED",
                    source="parent",
                )

    def _notify(self) -> None:
        for event in tuple(self._waiters):
            event.set()
        for child in tuple(self._children):
            child._notify()

    def _terminate(self, reason: str, *, source: str) -> bool:
        if self._cancelled:
            return False
        self._cancelled = True
        self._reason = str(reason)
        self._source = str(source)
        self._cancelled_at = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        self._notify()
        return True

    def cancel(
        self,
        reason: CancellationReason = "USER_CANCEL",
        *,
        source: str = "caller",
    ) -> bool:
        """Cancel this token once; repeated calls are no-ops."""
        reason = str(reason)
        if reason not in VALID_CANCELLATION_REASONS:
            raise ValueError(f"unknown cancellation reason: {reason}")
        return self._terminate(reason, source=source)

    def _expire_if_needed(self) -> None:
        if self._cancelled:
            return
        if self._deadline_passed():
            self._terminate("ROOT_DEADLINE_EXCEEDED", source="deadline")

    def check(self) -> None:
        """Raise if this token is at a terminal control boundary."""
        self._sync_parent()
        self._expire_if_needed()
        if not self._cancelled:
            return
        failure_type = self._reason or "USER_CANCEL"
        raise ExecutionControlError(
            failure_type,
            run_id=self.root_run_id,
            attempt_id=self.attempt_id,
            reason=self._reason or failure_type,
            source=self._source or "control",
            absolute_deadline=self.absolute_deadline,
        )

    def remaining_time(self, local_ceiling: float | None = None) -> float:
        """Return remaining root time clamped by an optional local ceiling."""
        self._sync_parent()
        self._expire_if_needed()
        self.check()
        remaining = float("inf")
        if self.absolute_deadline is not None:
            remaining = max(0.0, self.absolute_deadline - self._clock())
        if local_ceiling is not None:
            if float(local_ceiling) <= 0:
                raise ValueError("local_ceiling must be positive")
            remaining = min(remaining, float(local_ceiling))
        return remaining

    def effective_timeout(self, local_ceiling: float | None = None) -> float | None:
        """Return the only timeout a child operation may use."""
        value = self.remaining_time(local_ceiling)
        return None if value == float("inf") else value

    def derive(
        self,
        *,
        attempt_id: str | None = None,
        run_id: str | None = None,
        local_ceiling: float | None = None,
    ) -> "ExecutionControlToken":
        """Derive an attempt/component token without a new root authority."""
        self.check()
        return ExecutionControlToken(
            run_id=run_id or self.root_run_id,
            attempt_id=attempt_id,
            absolute_deadline=(
                None
                if local_ceiling is None
                else self._clock() + float(local_ceiling)
            ),
            parent=self,
            clock=self._clock,
            wall_clock=self._wall_clock,
        )

    async def wait_terminal(self) -> None:
        """Wait until cancellation/deadline becomes observable."""
        try:
            self.check()
        except ExecutionControlError:
            return
        event = asyncio.Event()
        self._waiters.append(event)
        try:
            # A deadline has no external event, so poll only when a deadline
            # exists.  The operation timeout remains authoritative.
            if self.absolute_deadline is None:
                await event.wait()
            else:
                while True:
                    self.check()
                    remaining = max(0.0, self.absolute_deadline - self._clock())
                    if remaining <= 0:
                        self._expire_if_needed()
                        return
                    try:
                        await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.05))
                    except asyncio.TimeoutError:
                        continue
                    return
        except ExecutionControlError:
            return
        finally:
            if event in self._waiters:
                self._waiters.remove(event)

    def evidence(self) -> dict[str, Any]:
        """Return current secret-free control evidence."""
        self._sync_parent()
        self._expire_if_needed()
        return {
            "root_run_id": self.root_run_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "absolute_deadline": self.absolute_deadline,
            "wall_clock_deadline": self.wall_clock_deadline,
            "remaining_seconds": (
                0.0
                if self._cancelled
                else (
                    None
                    if self.absolute_deadline is None
                    else max(0.0, self.absolute_deadline - self._clock())
                )
            ),
            "cancelled": self._cancelled,
            "reason": self._reason,
            "cancelled_at": self._cancelled_at,
        }


T = TypeVar("T")


async def await_with_control(
    awaitable: Awaitable[T],
    *,
    control: ExecutionControlToken | None,
    local_ceiling: float | None = None,
    timeout_failure_type: str = "OPERATION_TIMEOUT",
    source: str = "execution",
) -> T:
    """Await work under one control token and one local timeout ceiling.

    A late result is never returned after ``control.check()`` observes a
    terminal boundary.  On a component timeout the operation is cancelled but
    the root token remains active, allowing the caller to classify a local
    timeout without silently cancelling the run.
    """
    if control is None:
        if local_ceiling is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=float(local_ceiling))

    try:
        control.check()
    except ExecutionControlError:
        # Callers commonly construct the coroutine inline (for example
        # provider.generate(...)). Do not leave that coroutine pending when
        # the root was already cancelled before dispatch.
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise
    root_remaining = control.effective_timeout()
    effective = control.effective_timeout(local_ceiling)
    root_deadline_wins = (
        root_remaining is not None
        and (local_ceiling is None or root_remaining <= float(local_ceiling))
    )
    if effective is not None and effective <= 0:
        control.check()
        raise ExecutionLayerTimeout(
            timeout_failure_type,
            run_id=control.root_run_id,
            attempt_id=control.attempt_id,
            reason="LOCAL_CEILING_EXCEEDED",
            source=source,
            absolute_deadline=control.absolute_deadline,
        )

    operation = asyncio.ensure_future(awaitable)
    terminal_wait = asyncio.create_task(control.wait_terminal())
    try:
        done, _ = await asyncio.wait(
            {operation, terminal_wait},
            timeout=effective,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            # asyncio.wait() may wake a few clock ticks before the monotonic
            # deadline. If the root deadline was the selected ceiling, the
            # root authority still owns this terminal classification; do not
            # relabel it as a component timeout due to scheduler jitter.
            if root_deadline_wins:
                control.cancel("ROOT_DEADLINE_EXCEEDED", source="deadline")
            if control.terminal:
                control.check()
            raise ExecutionLayerTimeout(
                timeout_failure_type,
                run_id=control.root_run_id,
                attempt_id=control.attempt_id,
                reason="LOCAL_CEILING_EXCEEDED",
                source=source,
                absolute_deadline=control.absolute_deadline,
            )
        if terminal_wait in done:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            control.check()
            raise AssertionError("terminal waiter returned without terminal control state")
        result = operation.result()
        control.check()
        return result
    finally:
        if not terminal_wait.done():
            terminal_wait.cancel()
        await asyncio.gather(terminal_wait, return_exceptions=True)
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


__all__ = [
    "CancellationReason",
    "ExecutionControlError",
    "ExecutionControlToken",
    "ExecutionLayerTimeout",
    "VALID_CANCELLATION_REASONS",
    "await_with_control",
]
