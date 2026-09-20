"""P410 non-official integration warmup/pilot.

This module exercises the P410 runner contract with a deterministic adapter.
It is intentionally separate from official benchmark execution: it does not
produce Phase 4 performance claims or alter any frozen input.  The adapter's
``recover_after_validation`` method is the same narrow seam an actual runtime
adapter uses, so the pilot verifies runner wiring, trace ordering, and metric
denominators without requiring a provider credential.

Usage::

    python -m evals.reliability.p410_pilot --output results/.../p410_warmup --repeats 1
    python -m evals.reliability.p410_pilot --output results/.../p410_pilot --repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from evals.reliability.p46_launcher import compute_summary
from evals.reliability.run_phase4 import (
    ExecutionOutcome,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
    select_runs,
)


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = Path(__file__).parent / "phase4_v1"
PROTOCOL_HASH = "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
WARMUP_TASKS = ("CI-01", "ESR-01", "CWR-01", "PTF-01", "RTP-01", "DL-01")


def _event(
    event_type: str,
    request: Any,
    *,
    attempt_id: str,
    status: str,
    **metadata: Any,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    return {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "task_id": request.task["task_id"],
        "step_id": "root",
        "attempt_id": attempt_id,
        "status": status,
        "metadata": metadata,
    }


class P410IntegrationExecutor:
    """Integration adapter backed by the canonical P3.3 repair service.

    The initial rejection is supplied to the shared benchmark validator.  On
    recovery, this adapter creates a tiny persisted one-step plan and invokes
    ``PlanExecutionService.repair_after_failure``.  The service therefore
    owns scope selection, durable repair events, and the new attempt; this
    harness only projects those durable observations into benchmark trace
    events.
    """

    def __init__(self) -> None:
        self.recovery_calls = 0
        self._runtime_state: dict[str, tuple[Any, Any, Any, Any, Any]] = {}

    async def execute(self, request) -> ExecutionOutcome:
        self._runtime_state[request.run_id] = self._build_recovery_service(request)
        attempt_id = self._runtime_state[request.run_id][4].evidence[
            "original_failure_attempt_id"
        ]
        task_id = request.task["task_id"]
        trace = [
            _event("TASK_STARTED", request, attempt_id=attempt_id, status="started"),
            _event("FIXTURE_SETUP", request, attempt_id=attempt_id, status="ready", fixture_id=request.fixture.fixture_id),
            _event("FAULT_INJECTION_STARTED", request, attempt_id=attempt_id, status="started", fault_id=request.fault.fault_id),
            _event("FAULT_INJECTED", request, attempt_id=attempt_id, status="injected", fault_id=request.fault.fault_id),
            _event("RUNTIME_CREATED", request, attempt_id=attempt_id, status="created", runtime_source="p410-integration-runtime"),
            _event("EXECUTION_COMPLETE", request, attempt_id=attempt_id, status="claimed", claimed_complete=True),
            _event("FIXTURE_OBSERVATIONS", request, attempt_id=attempt_id, status="observed", observation_keys=[]),
        ]
        return ExecutionOutcome(
            claimed_complete=True,
            observed_state={"runtime_source": "p410-integration-runtime"},
            execution_trace=trace,
            runtime_source="p410-integration-runtime",
            attempt_count=1,
        )

    async def recover_after_validation(self, request, outcome, validation):
        self.recovery_calls += 1
        db, service, plan, goal, step = self._runtime_state[request.run_id]
        repaired_plan = await service.repair_after_failure(plan.id, step.id, goal)
        repaired_step = next(item for item in repaired_plan.steps if item.id == step.id)
        lineage = repaired_step.evidence.get("repair_lineage", [])
        if not lineage:
            # The direct public repair API stores the IDs on the step and in
            # its durable REPAIR_COMPLETED event.  Normalize that canonical
            # evidence into the benchmark adapter's compact lineage shape.
            original = repaired_step.evidence.get("original_failure_attempt_id")
            repaired_attempt = repaired_step.evidence.get("repair_attempt_id")
            if original and repaired_attempt and original != repaired_attempt:
                lineage = [{
                    "original_failure_attempt_id": original,
                    "repair_attempt_id": repaired_attempt,
                    "repair_number": repaired_step.evidence.get("repair_attempt_count", 1),
                    "repair_scope": "LOCAL",
                }]
            else:
                raise RuntimeError(
                    "P410_CANONICAL_REPAIR_LINEAGE_MISSING:"
                    f"status={repaired_plan.status.value}:"
                    f"step_status={repaired_step.status.value}:"
                    f"evidence={repaired_step.evidence}"
                )
        lineage_entry = lineage[-1]
        original_attempt_id = lineage_entry["original_failure_attempt_id"]
        repair_attempt_id = lineage_entry["repair_attempt_id"]
        from lhas.domain.enums import EventType
        from lhas.persistence.event_store import EventStore

        repair_events = []
        for event in EventStore(db).list_all():
            event_type = event.event_type.value
            mapped_type = {
                EventType.STEP_FAILURE_PROVENANCE.value: "StepFailureProvenance",
                EventType.REPAIR_STARTED.value: "REPAIR_STARTED",
                EventType.REPAIR_COMPLETED.value: "REPAIR_COMPLETED",
            }.get(event_type)
            if mapped_type is not None:
                payload = dict(event.payload)
                event_attempt_id = str(
                    payload.pop("repair_attempt_id", None)
                    or payload.pop("attempt_id", None)
                    or repair_attempt_id
                )
                repair_events.append(
                    _event(
                        mapped_type,
                        request,
                        attempt_id=event_attempt_id,
                        status=event_type.casefold(),
                        **payload,
                    )
                )

        # The durable service has completed its work and all required IDs have
        # been projected; the temporary integration database is no longer
        # needed by the benchmark adapter.
        db.close()
        self._runtime_state.pop(request.run_id, None)
        return ExecutionOutcome(
            claimed_complete=True,
            # This is the observable state returned by the recovery adapter;
            # the shared validator remains the only acceptance authority.
            observed_state={
                **request.task["expected_observable_effects"],
                "runtime_source": "p410-integration-runtime",
            },
            execution_trace=repair_events,
            runtime_source="p410-integration-runtime",
            repair_scope="local",
            attempt_count=1,
            original_failure_attempt_id=original_attempt_id,
            repair_attempt_id=repair_attempt_id,
        )

    @staticmethod
    def _build_recovery_service(request):
        """Create a persisted one-step plan for the canonical repair API."""
        from lhas.domain.enums import AttemptStatus, EventType, RunStatus
        from lhas.domain.models import Attempt, Project, Run, Task
        from lhas.persistence.database import Database
        from lhas.persistence.event_store import EventStore
        from lhas.persistence.planning_repositories import GoalRepository, PlanRepository
        from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository, TaskRepository
        from lhas.planning.models import Goal, Plan, PlanMode, PlanStep, PlanStepStatus
        from lhas.planning.service import PlanExecutionService
        from lhas.planning.verification import WorkflowVerifier
        from lhas.tools.fakes import FakeTool
        from lhas.tools.registry import ToolRegistry
        from tests.helpers import make_test_capability_definition, make_test_capability_registry
        from lhas.planning.models import CapabilitySpec

        db_path = Path(tempfile.mkdtemp(prefix="p410-service-")) / "repair.db"
        db = Database(db_path)
        db.init_db()
        project = Project(name=f"p410-{request.run_id}", type="benchmark-integration")
        ProjectRepository(db).create(project)
        goal = Goal(project_id=project.id, objective=f"P410 repair for {request.task['task_id']}")
        GoalRepository(db).create(goal)
        task = Task(
            project_id=project.id,
            title=request.task.get("title", request.task["task_id"]),
            objective=request.task.get("objective", "P410 repair"),
            acceptance_criteria=["repair completes with trusted tool evidence"],
        )
        TaskRepository(db).create(task)
        run = Run(task_id=task.id, status=RunStatus.COMPLETED, result='{"output":"initial rejection"}')
        RunRepository(db).create(run)
        attempt = Attempt(
            run_id=run.id,
            attempt_number=1,
            status=AttemptStatus.COMPLETED,
            executor_result="{}",
        )
        AttemptRepository(db).create(attempt)
        capability = f"p410.repair.{request.run_id.replace(':', '-') }"
        step = PlanStep(
            id=f"p410-step-{request.run_id.replace(':', '-')}",
            title="P410 repair step",
            objective="repair the rejected observable effect",
            capability=capability,
            expected_effects={"ok": True},
            status=PlanStepStatus.CLASSIFIED_FAILURE,
            task_id=task.id,
            evidence={
                "failure_provenance": {
                    "failure_class": "DATA",
                    "failure_type": "VERIFICATION_REJECTED",
                    "attempt_id": attempt.id,
                    "run_id": run.id,
                    "validation_id": f"p410-validation-{request.run_id}",
                },
                "original_failure_attempt_id": attempt.id,
            },
        )
        plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=[step])
        PlanRepository(db).create(plan)
        EventStore(db).append(
            EventType.STEP_FAILURE_PROVENANCE,
            payload={
                "plan_id": plan.id,
                "step_id": step.id,
                "attempt_id": attempt.id,
                "run_id": run.id,
                "failure_type": "VERIFICATION_REJECTED",
            },
        )
        registry = ToolRegistry()
        registry.register(
            FakeTool(
                CapabilitySpec(name=capability, description="P410 repair capability"),
                lambda _request: {"ok": True},
            )
        )
        cap_reg, contract = make_test_capability_registry(
            registry,
            [make_test_capability_definition(capability, output_schema={})],
        )

        class FixedPlanner:
            async def create_plan(self, **_kwargs):
                return plan

        service = PlanExecutionService(
            db,
            FixedPlanner(),
            registry,
            capability_registry=cap_reg,
            tool_contract=contract,
            workflow_verifier=WorkflowVerifier(db),
        )
        return db, service, plan, goal, step


def _select_warmup_runs(snapshot: ProtocolSnapshot, repeats: int) -> tuple[RunSpec, ...]:
    tasks = {task["task_id"] for task in snapshot.tasks}
    missing = [task_id for task_id in WARMUP_TASKS if task_id not in tasks]
    if missing:
        raise RuntimeError(f"P410_WARMUP_TASK_MISSING:{','.join(missing)}")
    selected = []
    for task_id in WARMUP_TASKS:
        for config_name in ("minimal", "odys_p3"):
            selected.extend(
                select_runs(
                    snapshot,
                    task_id=task_id,
                    config_name=config_name,
                    repeat_index=repeat,
                )
                for repeat in range(1, repeats + 1)
            )
    return tuple(spec for item in selected for spec in (item if isinstance(item, tuple) else (item,)))


async def run_p410(output_dir: Path, *, repeats: int) -> dict[str, Any]:
    if repeats not in (1, 3):
        raise ValueError("P410 integration supports repeats=1 (warmup) or 3 (pilot)")
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    if snapshot.protocol_hash != PROTOCOL_HASH:
        raise RuntimeError("P410_PROTOCOL_HASH_MISMATCH")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "benchmark_identity.json").write_text(
        json.dumps(
            {
                "benchmark_version": "phase4-v1",
                "protocol_hash": snapshot.protocol_hash,
                "execution_kind": "P410_PIPELINE_INTEGRATION_ONLY",
                "repeats": repeats,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "provider_identity.json").write_text(
        json.dumps(
            {
                "provider": "P410_INTEGRATION_ADAPTER",
                "model": "P410_INTEGRATION_RUNTIME",
                "execution_kind": "NON_OFFICIAL_PIPELINE_TEST",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    executor = P410IntegrationExecutor()
    runs = _select_warmup_runs(snapshot, repeats)
    runner = Phase4Runner(
        snapshot,
        output_dir=output_dir,
        executor=executor,
        model="P410_INTEGRATION_RUNTIME",
        provider="P410_INTEGRATION_ADAPTER",
        repo_root=ROOT,
        trace_path=output_dir / "traces.jsonl",
        require_trace=True,
    )
    counts = await runner.run(runs)
    summary = compute_summary(output_dir, planned_runs=len(runs))
    raw_records = [
        json.loads(line)
        for line in (output_dir / "raw.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    recorded_recovery_calls = sum(
        1 for record in raw_records if record.get("recovery_attempted") is True
    )
    summary.update(
        {
            "execution_kind": "P410_PIPELINE_INTEGRATION_ONLY",
            "run_selection": "six warmup tasks x two configs",
            "recovery_hook_calls": recorded_recovery_calls,
        }
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "p410-report.md").write_text(
        "\n".join(
            [
                "# P410 Pipeline Integration",
                "",
                "> Non-official wiring evidence. This output is not benchmark performance data.",
                "",
                f"- Planned runs: {len(runs)}",
                f"- Valid runs: {counts['valid']}",
                f"- Invalid runs: {counts['invalid']}",
                f"- Recovery hook calls recorded: {recorded_recovery_calls}",
                "- Validation contract: validator execution status is separate from acceptance status.",
                "- Recovery contract: rejection -> provenance -> repair -> revalidation.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the non-official P410 integration warmup/pilot")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, required=True)
    args = parser.parse_args(argv)
    summary = asyncio.run(run_p410(args.output, repeats=args.repeats))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
