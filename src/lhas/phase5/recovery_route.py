"""Recovery Route — deterministic recovery-route decision layer (route-v1).

This module answers exactly one question:

    Given REAL execution evidence, which recovery route should the runtime
    take next, and why?

Boundaries (see docs/phase5/RECOVERY_ROUTING_DESIGN_CONTRACT.md):
- Route choice is SEPARATE from budget authorization.  This layer only
  chooses a route and records evidence; ``RecoveryBudgetGate`` remains the
  budget authority and is consulted independently.
- ``REPLAN`` is an auditable decision result: it maps to a
  failure-context model turn (``RETRY_WITH_CONTEXT``).  Every tool call
  produced afterwards must pass the ExecutionAuthorizationGate.  No tool
  is ever called automatically.
- ``ESCALATE`` and ``STOP`` are distinct routes and distinct terminal
  kinds.
- No LLM calls, no randomness, no network.  Rules are deterministic,
  explainable and finite.

Route set (frozen): CONTINUE / RETRY_SAME / RETRY_CHANGED / REPLAN /
ESCALATE / STOP.

State-change honesty rule: "no observed change" is NOT "state did not
change".  When the environment exposes no state evidence, StateChange is
recorded as UNKNOWN and no-progress claims are grounded in "no new
evidence", never in "the environment is confirmed unchanged".

This module imports NOTHING from the frozen ToolMaze benchmark and no
other Phase5 runtime module (pure decision layer).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

ROUTE_POLICY_VERSION = "recovery-route-v1"

# Experiment routing modes (Experiment B only — see eval protocol §2).
# "route-v1" is the default and is the frozen behaviour under test as B1.
# "baseline" (B0) disables exactly the Q3a/Q3b/Q3d enhancement rules so
# the strategy's own proposal passes through; everything else (terminal
# safety Q0/Q1, budget gate, execution authorization gate, normalizer,
# event schema) is IDENTICAL.  The unique difference surface is the route
# rule set, nothing else.
ROUTE_MODE_V1 = "route-v1"
ROUTE_MODE_BASELINE = "baseline"


class RecoveryRoute(str, Enum):
    CONTINUE = "CONTINUE"
    RETRY_SAME = "RETRY_SAME"
    RETRY_CHANGED = "RETRY_CHANGED"
    REPLAN = "REPLAN"
    ESCALATE = "ESCALATE"
    STOP = "STOP"


class StateChange(str, Enum):
    OBSERVED_CHANGE = "OBSERVED_CHANGE"
    NO_OBSERVED_CHANGE = "NO_OBSERVED_CHANGE"
    UNKNOWN = "UNKNOWN"


class EvidenceGrade(str, Enum):
    DIRECT = "DIRECT"      # explicit result/state field
    DERIVED = "DERIVED"    # digest comparison or generic heuristic
    ABSENT = "ABSENT"      # no evidence exists on this axis
    UNKNOWN = "UNKNOWN"    # not observable — never defaulted


class TerminationKind(str, Enum):
    NONE = "NONE"
    STOP = "STOP"
    ESCALATION = "ESCALATION"
    BUDGET_EXHAUSTION = "BUDGET_EXHAUSTION"


@dataclass(frozen=True)
class AttemptEvidence:
    """One prior attempt at the same action signature."""

    step: int
    result_digest: str
    state_digest: str
    exec_status: str
    ready_status: str
    retryable: str
    affects_followups: bool
    state_evidence_available: bool


@dataclass
class RouteDecisionInputs:
    """Minimal decision inputs — all from real available evidence."""

    step: int
    proposed_action: str                      # RecoveryActionKind value
    tool_name: str = ""
    arguments_digest: str = ""
    last_exec_status: str = "UNKNOWN"
    last_ready_status: str = "UNKNOWN"
    last_retryable: str = "UNKNOWN"
    last_affects_followups: bool = False
    transient_basis: bool = False
    precondition_state: Dict[str, str] = field(default_factory=dict)
    precondition_grade: str = EvidenceGrade.ABSENT.value
    state_change: StateChange = StateChange.UNKNOWN
    state_change_grade: EvidenceGrade = EvidenceGrade.UNKNOWN
    attempts: List[AttemptEvidence] = field(default_factory=list)
    recovery_budget_remaining: Optional[int] = None   # None → unknown
    model_calls_used: Optional[int] = None
    terminal: bool = False
    prior_replan_count: int = 0


@dataclass(frozen=True)
class RouteDecision:
    """Auditable recovery-route decision result."""

    route: RecoveryRoute
    reason_code: str
    rationale: str
    evidence: Dict[str, Any]
    evidence_grade: str
    state_change: str
    decision_id: str = ""

    @property
    def mapped_recovery_action(self) -> str:
        """Map route onto the existing RecoveryActionKind vocabulary."""
        return map_route_to_recovery_action(self.route)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "route_policy_version": ROUTE_POLICY_VERSION,
            "route": self.route.value,
            "reason_code": self.reason_code,
            "rationale": self.rationale,
            "evidence": dict(self.evidence),
            "evidence_grade": self.evidence_grade,
            "state_change": self.state_change,
            "mapped_recovery_action": self.mapped_recovery_action,
        }


def map_route_to_recovery_action(route: RecoveryRoute) -> str:
    """Frozen mapping: route → existing RecoveryActionKind value.

    - CONTINUE → none
    - RETRY_SAME → retry (replay the same call)
    - RETRY_CHANGED → retry_with_context (model adjusts the action)
    - REPLAN → retry_with_context (model re-plans; route label distinguishes)
    - ESCALATE → escalate
    - STOP → stop
    """
    return {
        RecoveryRoute.CONTINUE: "none",
        RecoveryRoute.RETRY_SAME: "retry",
        RecoveryRoute.RETRY_CHANGED: "retry_with_context",
        RecoveryRoute.REPLAN: "retry_with_context",
        RecoveryRoute.ESCALATE: "escalate",
        RecoveryRoute.STOP: "stop",
    }[route]


def evaluate_state_change(
    attempts: List[AttemptEvidence],
) -> Tuple[StateChange, EvidenceGrade]:
    """Judge whether state changed between the last two attempts.

    Honesty rule: identical returned content is NOT evidence that the
    environment state is unchanged.  When no attempt exposes state
    evidence (readiness keys / explicit status), the answer is UNKNOWN.
    """
    if len(attempts) < 2:
        return StateChange.UNKNOWN, EvidenceGrade.UNKNOWN
    previous, latest = attempts[-2], attempts[-1]
    if not (previous.state_evidence_available and latest.state_evidence_available):
        return StateChange.UNKNOWN, EvidenceGrade.UNKNOWN
    if previous.state_digest != latest.state_digest:
        return StateChange.OBSERVED_CHANGE, EvidenceGrade.DIRECT
    return StateChange.NO_OBSERVED_CHANGE, EvidenceGrade.DIRECT


class RecoveryRouteDecider:
    """Deterministic recovery-route decision entry point.

    Stateless with respect to budgets (they are inputs for recording
    only).  Tracks per-signature REPLAN counts so REPLAN is bounded and
    repeated permanent failures escalate instead of looping.

    ``mode`` selects the experiment routing policy (Experiment B):
    - ``route-v1`` (default, arm B1): full Q0–Q3 rule set.
    - ``baseline`` (arm B0): Q0/Q1/Q2 identical; Q3 enhancement rules
      (Q3a permanent→REPLAN, Q3b loop→STOP/REPLAN, Q3d change→RETRY_CHANGED)
      are disabled and the strategy's proposal passes through unchanged.
    """

    def __init__(self, *, mode: str = ROUTE_MODE_V1):
        if mode not in (ROUTE_MODE_V1, ROUTE_MODE_BASELINE):
            raise ValueError(f"unknown route mode: {mode!r}")
        self._mode = mode
        self._replan_counts: Dict[str, int] = {}
        self._decision_sequence: int = 0

    @property
    def mode(self) -> str:
        return self._mode

    def reset(self) -> None:
        self._replan_counts.clear()
        self._decision_sequence = 0

    def decide(self, inputs: RouteDecisionInputs) -> RouteDecision:
        self._decision_sequence += 1
        decision_id = f"route-{self._decision_sequence}"
        signature = f"{inputs.tool_name}::{inputs.arguments_digest}"

        # Q0 — terminal / safety state: nothing may continue.
        if inputs.terminal:
            return self._decision(
                decision_id, RecoveryRoute.STOP, "TERMINAL_STATE",
                "runtime already in terminal or safety-intercept state",
                inputs,
            )

        # Q1 — strategy terminal proposals stay distinct.
        if inputs.proposed_action == "escalate":
            return self._decision(
                decision_id, RecoveryRoute.ESCALATE, "STRATEGY_ESCALATE",
                "policy strategy asked to hand the unresolved problem upward",
                inputs,
            )
        if inputs.proposed_action == "stop":
            return self._decision(
                decision_id, RecoveryRoute.STOP, "STRATEGY_STOP",
                "policy strategy asked to stop",
                inputs,
            )

        # Q2 — no recovery proposal.
        if inputs.proposed_action in ("none", ""):
            return self._decision(
                decision_id, RecoveryRoute.CONTINUE, "STRATEGY_NONE",
                "no recovery proposed and no terminal condition",
                inputs,
            )

        # Q3 — retry-family proposals judged against real evidence.
        if self._mode == ROUTE_MODE_BASELINE:
            # B0: proposal passthrough — the enhancement rules are the
            # experiment variable and are disabled here.  Everything else
            # (Q0/Q1/Q2, budget gate, authorization gate) is shared.
            if inputs.proposed_action == "retry_with_context":
                return self._decision(
                    decision_id, RecoveryRoute.RETRY_CHANGED,
                    "STRATEGY_CONTEXT_CHANGE",
                    "policy strategy asked to retry with changed context "
                    "(baseline passthrough)",
                    inputs,
                )
            return self._decision(
                decision_id, RecoveryRoute.RETRY_SAME, "STRATEGY_PASSTHROUGH",
                "baseline route mode: strategy proposal passes through unchanged",
                inputs,
            )

        attempts = inputs.attempts
        loop = self._no_progress_loop(attempts)

        # a. permanent (NOT_RETRYABLE) failure: never repeat the same
        #    action — re-plan once, then escalate.
        if (
            inputs.last_exec_status == "FAILED"
            and inputs.last_retryable == "NOT_RETRYABLE"
        ):
            if self._replan_counts.get(signature, 0) == 0:
                self._replan_counts[signature] = 1
                return self._decision(
                    decision_id, RecoveryRoute.REPLAN, "PERMANENT_FAILURE_REPLAN",
                    "last failure is explicitly NOT_RETRYABLE — repeating the same "
                    "action is unjustified; the model must re-plan",
                    inputs,
                )
            return self._decision(
                decision_id, RecoveryRoute.ESCALATE, "REPLAN_EXHAUSTED",
                "permanent failure persisted after re-planning — escalate with evidence",
                inputs,
            )

        # b. no-progress loop: same non-success outcome, no new evidence,
        #    no transient basis → stop (bounded; never loop).
        if loop:
            if inputs.proposed_action == "retry_with_context":
                # The strategy already wants changed context; route to a
                # re-plan instead of blindly looping.
                if self._replan_counts.get(signature, 0) == 0:
                    self._replan_counts[signature] = 1
                    return self._decision(
                        decision_id, RecoveryRoute.REPLAN, "NO_PROGRESS_REPLAN",
                        "repeated identical non-success outcomes — re-plan instead "
                        "of looping",
                        inputs,
                    )
                return self._decision(
                    decision_id, RecoveryRoute.ESCALATE, "REPLAN_EXHAUSTED",
                    "no-progress persisted after re-planning — escalate with evidence",
                    inputs,
                )
            return self._decision(
                decision_id, RecoveryRoute.STOP, "NO_PROGRESS_LOOP",
                "repeated identical non-success outcomes with no new evidence "
                "and no transient basis — stop instead of looping",
                inputs,
            )

        # c. strategy explicitly asked for changed context — honour it.
        if inputs.proposed_action == "retry_with_context":
            return self._decision(
                decision_id, RecoveryRoute.RETRY_CHANGED, "STRATEGY_CONTEXT_CHANGE",
                "policy strategy asked to retry with changed context",
                inputs,
            )

        # d. explicit transient basis → retry the same action.
        if inputs.transient_basis:
            return self._decision(
                decision_id, RecoveryRoute.RETRY_SAME, "TRANSIENT_BASIS",
                "explicit transient retry basis observed",
                inputs,
            )

        # d. credible change evidence → retry with a changed action.
        if inputs.state_change is StateChange.OBSERVED_CHANGE:
            return self._decision(
                decision_id, RecoveryRoute.RETRY_CHANGED, "STATE_CHANGED",
                "observed state change justifies a modified attempt",
                inputs,
            )

        # e. single uncertain failure → one bounded verification retry.
        return self._decision(
            decision_id, RecoveryRoute.RETRY_SAME, "BOUNDED_VERIFICATION",
            "single non-permanent failure without evidence against one "
            "bounded verification retry",
            inputs,
        )

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _no_progress_loop(attempts: List[AttemptEvidence]) -> bool:
        """≥2 identical non-success outcomes with no new evidence.

        An observed state change between the attempts IS progress — the
        loop rule does not apply.  Successful outcomes are legitimate
        polling and never count as no-progress loops.
        """
        if len(attempts) < 2:
            return False
        previous, latest = attempts[-2], attempts[-1]
        if previous.result_digest != latest.result_digest:
            return False  # new evidence observed
        if previous.state_digest != latest.state_digest:
            return False  # observed state change is progress
        if not latest.affects_followups:
            return False  # successful outcomes are legitimate polling
        return True

    def _decision(
        self,
        decision_id: str,
        route: RecoveryRoute,
        reason_code: str,
        rationale: str,
        inputs: RouteDecisionInputs,
    ) -> RouteDecision:
        grade = EvidenceGrade.DERIVED.value
        if inputs.state_change_grade is EvidenceGrade.DIRECT:
            grade = EvidenceGrade.DIRECT.value
        return RouteDecision(
            route=route,
            reason_code=reason_code,
            rationale=rationale,
            evidence={
                "proposed_action": inputs.proposed_action,
                "tool_name": inputs.tool_name,
                "arguments_digest": inputs.arguments_digest,
                "last_exec_status": inputs.last_exec_status,
                "last_ready_status": inputs.last_ready_status,
                "last_retryable": inputs.last_retryable,
                "transient_basis": inputs.transient_basis,
                "precondition_state": dict(inputs.precondition_state),
                "attempt_count": len(inputs.attempts),
                "recovery_budget_remaining": inputs.recovery_budget_remaining,
                "model_calls_used": inputs.model_calls_used,
                "terminal": inputs.terminal,
            },
            evidence_grade=grade,
            state_change=inputs.state_change.value
            if isinstance(inputs.state_change, StateChange)
            else str(inputs.state_change),
            decision_id=decision_id,
        )


def route_policy_fingerprint() -> str:
    """Deterministic fingerprint of the frozen route rule set."""
    spec = {
        "version": ROUTE_POLICY_VERSION,
        "routes": [r.value for r in RecoveryRoute],
        "rules": ["Q0_TERMINAL", "Q1_STRATEGY_TERMINAL", "Q2_NONE",
                  "Q3a_PERMANENT", "Q3b_LOOP", "Q3c_TRANSIENT",
                  "Q3d_CHANGED", "Q3e_BOUNDED_VERIFICATION"],
        "mapping": {r.value: map_route_to_recovery_action(r) for r in RecoveryRoute},
    }
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
