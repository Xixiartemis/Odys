"""Execution-path effect authorization for Phase 4 recovery experiments.

The frozen benchmark does not use this policy.  An experiment may opt in by
placing a :class:`PhaseEffectPolicy` instance in the execution-local config.
The policy is then installed by the normal benchmark tool-registry builder,
so qualification and real-provider construction share the same authority
boundary.
"""

from __future__ import annotations

from typing import Any

from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus


class PhaseEffectPolicy:
    """Authorize state mutations according to the current recovery phase."""

    policy_id = "phase4-effect-policy-v1"

    def __init__(self) -> None:
        self.phase = "initial"
        self.replanned = False
        self.denied: list[dict[str, Any]] = []
        self.allowed: list[dict[str, Any]] = []
        self.replan_reservations: list[dict[str, Any]] = []
        self.replan_results: list[dict[str, Any]] = []
        self.replan_signal_reasons: list[dict[str, Any]] = []
        self.recovery_detections: list[dict[str, Any]] = []
        self.registry_install_count = 0

    def bind_provider_phase(self, phase: str) -> None:
        if phase == "initial":
            self.replanned = False
            self.phase = "initial"
        elif phase in {"recovery", "repair"}:
            self.phase = "post_replan" if self.replanned else "local_repair"

    def mark_replan_accepted(self) -> None:
        self.replanned = True
        self.phase = "post_replan"

    @staticmethod
    def _is_alternate(arguments: Any) -> bool:
        if not isinstance(arguments, dict):
            return False
        if arguments.get("new_string"):
            value = str(arguments["new_string"])
        else:
            value = str(arguments.get("content", ""))
        return "alternate" in value or "verified" in value

    def authorize(self, capability: str, arguments: Any) -> bool:
        alternate = self._is_alternate(arguments)
        allowed = not alternate or self.phase == "post_replan"
        event = {
            "phase": self.phase,
            "capability": capability,
            "alternate_effect": alternate,
            "allowed": allowed,
        }
        (self.allowed if allowed else self.denied).append(event)
        return allowed

    def record_detection(self, *, run_id: str, reason: str, escalation_policy: str) -> None:
        self.recovery_detections.append(
            {
                "reason": str(reason),
                "run_id": str(run_id),
                "escalation_policy": str(escalation_policy),
            }
        )

    def record_signal(self, *, run_id: str, reason: str, escalation_policy: str) -> None:
        self.replan_signal_reasons.append(
            {
                "reason": str(reason),
                "run_id": str(run_id),
                "escalation_policy": str(escalation_policy),
            }
        )

    def record_replan_result(
        self,
        *,
        run_id: str,
        plan_id: str,
        accepted: bool,
        error_type: str | None = None,
        signal_count: int | None = None,
        signal_reasons: list[str] | None = None,
        signal_run_ids: list[str] | None = None,
    ) -> None:
        self.replan_results.append(
            {
                "run_id": str(run_id),
                "plan_id": str(plan_id),
                "accepted": bool(accepted),
                "error_type": error_type,
                "signal_count": signal_count,
                "signal_reasons": list(signal_reasons or []),
                "signal_run_ids": list(signal_run_ids or []),
            }
        )

    def record_replan_reservation(
        self, *, accepted: bool, snapshot: dict[str, Any]
    ) -> None:
        self.replan_reservations.append(
            {"accepted": bool(accepted), "snapshot": dict(snapshot)}
        )

    def record_registry_install(self) -> None:
        self.registry_install_count += 1


class PhaseGuardedTool:
    """Preserve a concrete tool contract while enforcing the phase policy."""

    def __init__(self, inner: Any, policy: PhaseEffectPolicy):
        self._inner = inner
        self._policy = policy

    @property
    def capability(self) -> Any:
        return self._inner.capability

    async def execute(self, request: ToolRequest) -> ToolResult:
        if not self._policy.authorize(request.capability_id, request.arguments):
            return ToolResult(
                # A denied alternate effect is an observable no-op, not a
                # transport/tool contract failure. This lets the real
                # convergence controller classify repeated no-progress.
                status=ToolResultStatus.SUCCESS,
                output={"path": request.arguments.get("path"), "replaced": False},
                metadata={
                    "effect_policy": "DENIED_BY_PHASE_EFFECT_POLICY",
                    "phase": self._policy.phase,
                    "observed_mutation": False,
                    "effect_policy_id": self._policy.policy_id,
                },
            )
        return await self._inner.execute(request)


def apply_phase_effect_policy(registry: Any, policy: PhaseEffectPolicy) -> Any:
    """Wrap the mutable benchmark capabilities in *registry* in place."""
    policy.record_registry_install()
    for capability in ("workspace.edit", "workspace.edit_lines"):
        registry._tools[capability] = PhaseGuardedTool(  # type: ignore[attr-defined]
            registry.resolve(capability), policy
        )
    return registry


__all__ = ["PhaseEffectPolicy", "PhaseGuardedTool", "apply_phase_effect_policy"]
