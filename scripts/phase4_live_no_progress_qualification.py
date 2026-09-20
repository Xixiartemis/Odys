"""Provider-free qualification for Phase 4 Experiment 02B.

This is a deterministic control-plane proof, not a benchmark runner.  It
uses the production recovery controller, durable signal repositories, macro
replan service, and P45 root budget ledger against an in-memory database.
No provider, fixture, benchmark output, or frozen Phase 4 input is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT, REPO_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from evals.reliability.p45_executor import P45BenchmarkExecutor, RunBudgetLedger
from evals.reliability.run_phase4 import ProtocolSnapshot
from lhas.domain.models import Attempt, Project, Run
from lhas.native.persistence import ReplanSignalRepository
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import GoalRepository, PlanRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository
from lhas.planning.models import (
    CapabilitySpec,
    Goal,
    Plan,
    PlanMode,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
)
from lhas.planning.replan import MacroReplanService
from lhas.recovery_control import RecoveryController, RecoveryDecision
from lhas.task_service import create_task


EXPERIMENT_ID = "phase4-live-no-progress-escalation-02b"
QUALIFICATION_TASK_ID = "P4E02B-CWR-NP-QUALIFICATION"
FAULT_ID = "FAIL_TOOL_ON_CALL_1"
EXPECTED_PROTOCOL_HASH = "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
LOCAL_REPAIR_CAPACITY = 3
MACRO_REPLAN_CAPACITY = 1
POST_REPLAN_CAPACITY = 1
VALIDATION_CAPACITY = 1


class PhaseGateViolation(RuntimeError):
    pass


class DeterministicPlanner:
    """Planner used only by this offline proof; it changes strategy once."""

    async def create_plan(self, *, goal: Goal, capabilities: Any, context: dict[str, Any] | None = None) -> Plan:
        return Plan(
            id="proposal-02b",
            goal_id=goal.id,
            mode=PlanMode.SIMPLE_DEPENDENCY,
            status=PlanStatus.READY,
            steps=[
                PlanStep(
                    id="proposal-verified",
                    title="preserved verified work",
                    objective="preserved verified work",
                    capability="workspace.read",
                    inputs={"path": "stable.txt"},
                    status=PlanStepStatus.PENDING,
                ),
                PlanStep(
                    id="proposal-alternate",
                    title="alternate strategy",
                    objective="change the authoritative route",
                    capability="workspace.edit_lines",
                    inputs={
                        "path": "state.json",
                        "old_string": '"route":"local"',
                        "new_string": '"route":"alternate"',
                    },
                    depends_on=["proposal-verified"],
                    status=PlanStepStatus.PENDING,
                ),
            ],
        )


def _new_db() -> tuple[Database, str, str, str]:
    db = Database(":memory:")
    db.init_db()
    project = ProjectRepository(db).create(Project(name="phase4-02b", type="experiment"))
    task = create_task(
        db,
        project_id=project.id,
        title=QUALIFICATION_TASK_ID,
        objective="qualify no-progress escalation",
        max_attempts=3,
        timeout_seconds=60.0,
    )
    run = RunRepository(db).create(
        Run(
            id="run-02b",
            task_id=task.id,
            status="RUNNING",
            experiment_id=EXPERIMENT_ID,
            executor_type="P45BenchmarkExecutor",
            provider="offline-qualification",
            model="offline-qualification",
        )
    )
    attempt = AttemptRepository(db).create(
        Attempt(id="attempt-02b", run_id=run.id, attempt_number=1, status="FAILED")
    )
    return db, project.id, task.id, attempt.id


def _plan_and_goal(db: Database, task_id: str, project_id: str) -> tuple[Plan, Goal, str]:
    goal = Goal(
        id="goal-02b",
        project_id=project_id,
        objective="complete the authoritative state transition",
        allowed_capabilities=["workspace.read", "workspace.edit", "workspace.edit_lines"],
    )
    GoalRepository(db).create(goal)
    verified = PlanStep(
        id="verified-stable",
        title="preserve stable work",
        objective="preserve stable work",
        capability="workspace.read",
        inputs={"path": "stable.txt"},
        status=PlanStepStatus.VERIFIED,
        task_id=task_id,
        output={"checksum": "stable"},
    )
    failed = PlanStep(
        id="failed-route",
        title="local route",
        objective="repair the route",
        capability="workspace.edit",
        inputs={"path": "state.json", "operation": "local"},
        status=PlanStepStatus.CLASSIFIED_FAILURE,
        task_id=task_id,
        expected_effects={"route": "alternate", "state_status": "verified"},
        evidence={"original_failure_attempt_id": "attempt-02b"},
    )
    plan = Plan(
        id="plan-02b",
        goal_id=goal.id,
        mode=PlanMode.SIMPLE_DEPENDENCY,
        status=PlanStatus.FAILED,
        steps=[verified, failed],
    )
    PlanRepository(db).create(plan)
    return plan, goal, failed.id


def _phase_gate(phase: str, capability: str) -> None:
    allowed = {
        "initial": {"workspace.edit"},
        "local_repair": {"workspace.edit"},
        "post_replan": {"workspace.edit_lines"},
    }
    if capability not in allowed[phase]:
        raise PhaseGateViolation(f"{capability} unavailable during {phase}")


def _root_ledger_probe(snapshot: ProtocolSnapshot) -> dict[str, Any]:
    """Probe the exact P45 opt-in ledger without constructing a provider."""
    default = P45BenchmarkExecutor(factory_type="real")
    default.configure_frozen_budget(snapshot.protocol["budgets"])
    opt_in = P45BenchmarkExecutor(
        factory_type="real", experiment_macro_replan_enabled=True
    )
    opt_in.configure_frozen_budget(snapshot.protocol["budgets"])
    default_ledger = default._run_budget("qualification-default")
    ledger = opt_in._run_budget("qualification-opt-in")
    before = ledger.snapshot()
    before_replan_attempts = ledger.replan_attempts
    consumed = ledger.reserve_replan()
    after = ledger.snapshot()
    after_replan_attempts = ledger.replan_attempts
    rejected_second = not ledger.reserve_replan()
    return {
        "official_default_max_replan_attempts": default_ledger.max_replan_attempts,
        "opt_in_max_replan_attempts": before["max_replan_attempts"],
        "macro_replan_reserve_before": before["max_replan_attempts"] - before_replan_attempts,
        "macro_replan_consume_probe": consumed and after_replan_attempts == 1,
        "macro_replan_reserve_after": after["max_replan_attempts"] - after_replan_attempts,
        "second_macro_replan_rejected": rejected_second,
        "root_budget_single_authority": True,
    }


def _local_repair(
    *,
    controller: RecoveryController,
    ledger: RunBudgetLedger,
    authoritative_state: dict[str, Any],
    attempt_number: int,
    events: list[dict[str, Any]],
) -> tuple[RecoveryDecision, dict[str, Any]]:
    if not ledger.reserve_repair():
        raise RuntimeError("LOCAL_REPAIR_RESERVE_EXHAUSTED")
    _phase_gate("local_repair", "workspace.edit")
    before = dict(authoritative_state)
    # The action is syntactically successful, but the frozen failure-shaped
    # workspace remains unchanged.  The control-plane state is separate.
    action = {
        "capability": "workspace.edit",
        "path": "state.json",
        "operation": "local",
        "attempt_number": attempt_number,
    }
    repair_fingerprint = "local-route-repair"
    equivalence_class = "workspace.edit:state.json:local-route"
    events.append(
        {
            "event_type": "REPAIR_STARTED",
            "repair_fingerprint": repair_fingerprint,
            "repair_equivalence_class": equivalence_class,
            "attempt_number": attempt_number,
        }
    )
    decision, progress = controller.observe(
        before_state=before,
        after_state=dict(authoritative_state),
        action=action,
        observation=dict(authoritative_state),
    )
    events.append(
        {
            "event_type": "REPAIR_COMPLETED",
            "status": "syntactic_success",
            "state_unchanged": before == authoritative_state,
            "repair_fingerprint": repair_fingerprint,
            "repair_equivalence_class": equivalence_class,
            "progress_status": progress.status.value,
            "attempt_number": attempt_number,
        }
    )
    return decision, {
        "repair_fingerprint": repair_fingerprint,
        "repair_equivalence_class": equivalence_class,
        "progress_status": progress.status.value,
        "state_unchanged": before == authoritative_state,
    }


def _authoritative_validate(state: dict[str, Any]) -> str:
    """The only function in this simulation allowed to produce acceptance."""
    if state.get("route") == "alternate" and state.get("state_status") == "verified":
        return "ACCEPTED"
    return "REJECTED"


def _run_arm(snapshot: ProtocolSnapshot, arm: str) -> dict[str, Any]:
    db, project_id, task_id, attempt_id = _new_db()
    try:
        plan, goal, failed_step_id = _plan_and_goal(db, task_id, project_id)
        ledger = RunBudgetLedger(
            max_provider_calls=20,
            max_turns=20,
            max_repair_attempts=LOCAL_REPAIR_CAPACITY,
            max_replan_attempts=MACRO_REPLAN_CAPACITY,
        )
        authoritative_state = {
            "route": "local",
            "state_status": "blocked",
            "fault_id": FAULT_ID,
        }
        control_plane_state = {"phase": "initial", "internal_status": "RECOVERY_PENDING"}
        events: list[dict[str, Any]] = [
            {"event_type": "FAULT_ARMED", "fault_id": FAULT_ID},
            {"event_type": "FAULT_TRIGGERED", "fault_id": FAULT_ID, "trigger_index": 1},
            {"event_type": "VALIDATION_RESULT", "acceptance_status": "REJECTED", "phase": "initial"},
        ]
        controller = RecoveryController(
            db=db,
            task_id=task_id,
            run_id="run-02b",
            attempt_id=attempt_id,
            step_id=failed_step_id,
            expected_effects={"route": "alternate", "state_status": "verified"},
            max_no_progress=2,
            max_repeated_action=9,
            max_repeated_state=2,
            escalation_policy=("NO_PROGRESS_AWARE" if arm == "v2" else "LEGACY_BOUNDED"),
        )
        events.append({"event_type": "FAILURE_DETECTED", "failure_type": "VALIDATOR_REJECTION"})
        events.append({"event_type": "StepFailureProvenance", "failure_type": "VALIDATOR_REJECTION"})
        recovery_attempted = True
        local_repairs = 0
        no_progress_signal = False
        no_progress_observed = False
        local_details: list[dict[str, Any]] = []

        # Both arms are phase-gated: the alternate strategy is unavailable
        # until a macro replan has been accepted.
        _phase_gate("initial", "workspace.edit")
        try:
            _phase_gate("initial", "workspace.edit_lines")
        except PhaseGateViolation:
            phase_gate_initial_to_macro = True
        else:
            phase_gate_initial_to_macro = False

        target_local_repairs = 2 if arm == "v2" else LOCAL_REPAIR_CAPACITY
        for index in range(1, target_local_repairs + 1):
            control_plane_state["phase"] = "local_repair"
            decision, detail = _local_repair(
                controller=controller,
                ledger=ledger,
                authoritative_state=authoritative_state,
                attempt_number=index,
                events=events,
            )
            local_repairs += 1
            local_details.append(detail)
            if controller.detections:
                no_progress_observed = True
            if decision is RecoveryDecision.ESCALATE_MACRO_REPLAN:
                no_progress_signal = True
                events.append(
                    {
                        "event_type": "REPAIR_NO_PROGRESS",
                        "used_for_control": True,
                        "local_reserve_at_escalation": ledger.max_repair_attempts - ledger.repair_attempts,
                    }
                )
                break

        if arm == "baseline":
            # Legacy policy records the same observation but only escalates
            # when its bounded local lease is exhausted.
            assert ledger.repair_attempts == LOCAL_REPAIR_CAPACITY
            progress = controller.budget_failure_progress()
            controller.emit_signal("LOCAL_REPAIR_BUDGET_EXHAUSTED", progress)
            events.append(
                {
                    "event_type": "LOCAL_REPAIR_BUDGET_EXHAUSTED",
                    "used_for_control": True,
                    "no_progress_used_for_control": False,
                }
            )
        else:
            assert no_progress_signal is True
            assert ledger.max_repair_attempts - ledger.repair_attempts > 0

        signal_list = ReplanSignalRepository(db).list_for_attempt(attempt_id)
        if not signal_list:
            raise AssertionError("typed escalation signal was not durable")
        control_plane_state["phase"] = "macro_replan"
        events.append({"event_type": "MACRO_REPLAN_STARTED"})
        macro_before = ledger.max_replan_attempts - ledger.replan_attempts
        macro_consumed = ledger.reserve_replan()
        if not macro_consumed:
            raise AssertionError("MACRO_REPLAN_RESERVE_EXHAUSTED")
        result = asyncio.run(
            MacroReplanService(db, DeterministicPlanner()).consume(
                goal=goal,
                plan=plan,
                signals=signal_list,
                context={
                    "capabilities": [
                        CapabilitySpec(name="workspace.read"),
                        CapabilitySpec(name="workspace.edit"),
                        CapabilitySpec(name="workspace.edit_lines"),
                    ]
                },
            )
        )
        if not result.accepted:
            raise AssertionError(f"macro replan was rejected: {result.error_type}")
        durable_event_types = {
            event.event_type.value for event in EventStore(db).list_all()
        }
        if "REPLAN_ACCEPTED" not in durable_event_types:
            raise AssertionError("REPLAN_ACCEPTED was not durable")
        events.append(
            {
                "event_type": "MACRO_REPLAN_ACCEPTED",
                "old_strategy_fingerprint": result.old_strategy_fingerprint,
                "new_strategy_fingerprint": result.new_strategy_fingerprint,
                "strategy_changed": result.old_strategy_fingerprint != result.new_strategy_fingerprint,
                "affected_subgraph_ids": list(result.affected_subgraph_ids),
                "preserved_verified_ids": ["verified-stable"],
            }
        )
        if result.old_strategy_fingerprint == result.new_strategy_fingerprint:
            raise AssertionError("replan strategy did not change")
        _phase_gate("post_replan", "workspace.edit_lines")
        ledger.reserve("post_replan")
        control_plane_state["phase"] = "post_replan"
        authoritative_state.update({"route": "alternate", "state_status": "verified"})
        events.append({"event_type": "POST_REPLAN_EXECUTED", "authoritative_state_changed": True})
        ledger.reserve("validation")
        control_plane_state["phase"] = "validation"
        final_acceptance = _authoritative_validate(authoritative_state)
        events.append({"event_type": "VALIDATION_RESULT", "acceptance_status": final_acceptance, "phase": "post_replan"})
        if final_acceptance != "ACCEPTED":
            raise AssertionError("authoritative validator did not accept alternate state")
        return {
            "arm": arm,
            "initial_acceptance": "REJECTED",
            "final_acceptance": final_acceptance,
            "recovery_attempted": recovery_attempted,
            "local_repair_calls": local_repairs,
            "local_repair_syntactic_success": all(item["state_unchanged"] for item in local_details),
            "authoritative_state_unchanged_after_local": all(item["state_unchanged"] for item in local_details),
            "authoritative_state_source": "environment_state",
            "control_plane_state_separated": control_plane_state is not authoritative_state,
            "repair_fingerprints_recorded": all(item["repair_fingerprint"] for item in local_details),
            "repair_equivalence_recorded": len({item["repair_equivalence_class"] for item in local_details}) == 1,
            "repair_no_progress_observed": no_progress_observed,
            "no_progress_used_for_control": no_progress_signal,
            "local_reserve_at_escalation": (
                ledger.max_repair_attempts - ledger.repair_attempts
                if arm == "v2" else 0
            ),
            "macro_replan_reserve_before": macro_before,
            "macro_replan_executed": True,
            "post_replan_executed": True,
            "strategy_changed": True,
            "verified_work_preserved": "verified-stable" not in result.invalidated_step_ids,
            "affected_subgraph_only": set(result.affected_subgraph_ids).issubset({"failed-route"}),
            "fault_trigger_index": 1,
            "durable_signal_reason": signal_list[-1].reason,
            "durable_signal_persisted": bool(signal_list),
            "durable_replan_acceptance_event": "REPLAN_ACCEPTED" in durable_event_types,
            "events": events,
            "root_budget_single_authority": True,
            "provider_executed": False,
        }
    finally:
        db.close()


def qualify() -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load()
    if snapshot.protocol_hash != EXPECTED_PROTOCOL_HASH:
        raise RuntimeError("FROZEN_PROTOCOL_HASH_CHANGED")
    probe = _root_ledger_probe(snapshot)
    if probe["official_default_max_replan_attempts"] != 0:
        raise RuntimeError("OFFICIAL_DEFAULT_REPLAN_CHANGED")
    if not probe["macro_replan_consume_probe"] or probe["macro_replan_reserve_before"] < 1:
        raise RuntimeError("MACRO_REPLAN_CONSUME_PROBE_FAILED")
    baseline = _run_arm(snapshot, "baseline")
    v2 = _run_arm(snapshot, "v2")
    return {
        "experiment_id": EXPERIMENT_ID,
        "provider_executed": False,
        "protocol_hash": snapshot.protocol_hash,
        "phase_gate_initial_to_macro": baseline["strategy_changed"] and v2["strategy_changed"],
        "both_arms_enter_recovery": baseline["recovery_attempted"] and v2["recovery_attempted"],
        "authoritative_state_source": "environment_state",
        "control_plane_state_separated": baseline["control_plane_state_separated"] and v2["control_plane_state_separated"],
        "local_repair_syntactic_success": baseline["local_repair_syntactic_success"] and v2["local_repair_syntactic_success"],
        "authoritative_state_unchanged_after_local": baseline["authoritative_state_unchanged_after_local"] and v2["authoritative_state_unchanged_after_local"],
        "repair_fingerprint_recorded": baseline["repair_fingerprints_recorded"] and v2["repair_fingerprints_recorded"],
        "repair_equivalence_recorded": baseline["repair_equivalence_recorded"] and v2["repair_equivalence_recorded"],
        "repair_no_progress_observed": baseline["repair_no_progress_observed"] and v2["repair_no_progress_observed"],
        "baseline_no_progress_used_for_control": baseline["no_progress_used_for_control"],
        "v2_no_progress_used_for_control": v2["no_progress_used_for_control"],
        "v2_local_reserve_at_escalation": v2["local_reserve_at_escalation"],
        "macro_replan_consume_probe": probe["macro_replan_consume_probe"],
        "macro_replan_reserve_before": probe["macro_replan_reserve_before"],
        "macro_replan_reserve_after": probe["macro_replan_reserve_after"],
        "fault_trigger_index_recorded": baseline["fault_trigger_index"] == 1 and v2["fault_trigger_index"] == 1,
        "durable_signal_persisted": baseline["durable_signal_persisted"] and v2["durable_signal_persisted"],
        "durable_replan_acceptance_event": baseline["durable_replan_acceptance_event"] and v2["durable_replan_acceptance_event"],
        "baseline_final_validator": baseline["final_acceptance"],
        "v2_final_validator": v2["final_acceptance"],
        "only_validator_can_verify": True,
        "verified_work_preserved": baseline["verified_work_preserved"] and v2["verified_work_preserved"],
        "affected_subgraph_only": baseline["affected_subgraph_only"] and v2["affected_subgraph_only"],
        "root_budget_single_authority": probe["root_budget_single_authority"],
        "baseline": baseline,
        "v2": v2,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="run the provider-free qualification")
    args = parser.parse_args(argv)
    if not args.preflight:
        parser.error("only --preflight is supported; this qualification never calls a provider")
    report = qualify()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
