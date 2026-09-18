"""Collect demo-only traces from the real Odys execution paths.

This module is deliberately outside the frozen Phase 4 inputs.  It drives the
existing :class:`OdysRuntimeExecutor` with a small demo kernel that delegates
to the production ``PlanExecutionService``.  The JSONL files are projections
of persisted EventStore events and real ``WorkflowVerifier`` calls; no story
event is inserted merely because a GIF wants to display it.

Run from the repository root::

    .venv\\Scripts\\python.exe evals/reliability/demo_trace_collection.py

Outputs are written below ``results/demo_traces`` and the GIFs are generated
only after all three real timelines pass the trace contract.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

if __package__ in {None, ""}:
    # Direct script invocation is part of the documented collection command.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.reliability.demo_gif.generate_gifs import TraceEventError, generate_gifs
from evals.reliability.odys_executor import OdysRuntimeExecutor
from evals.reliability.run_phase4 import (
    DEFAULT_PROTOCOL_ROOT,
    ExecutionRequest,
    FaultContext,
    FaultPlan,
    FixtureHandle,
    ProtocolSnapshot,
)
from lhas.agent.models import AgentResult, AgentStatus
from lhas.domain.enums import EventType
from lhas.domain.models import Project
from lhas.executors.protocol import ExecutionResult
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.repositories import ProjectRepository
from lhas.planning.models import (
    CapabilitySpec,
    Goal,
    Plan,
    PlanMode,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    transition_step,
)
from lhas.planning.service import PlanExecutionService, _ToolExecutor
from lhas.planning.verification import WorkflowVerifier
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from tests.helpers import make_test_capability_definition, make_test_capability_registry


REQUIRED_TRACE_FIELDS = (
    "timestamp",
    "event_type",
    "task_id",
    "step_id",
    "attempt_id",
    "status",
    "metadata",
)


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _FixedPlanner:
    def __init__(self, plan: Plan):
        self.plan = plan

    async def create_plan(self, **kwargs: Any) -> Plan:
        return self.plan


class _PlanServiceKernel:
    """Kernel-shaped bridge used only to drive an existing Odys executor.

    The bridge does not create trace events.  ``PlanExecutionService`` owns
    all plan/step/run transitions and the collector later reads those durable
    events from the database.
    """

    def __init__(self, service: PlanExecutionService, goal: Goal, *, resume_plan_id: str | None = None):
        self.service = service
        self.goal = goal
        self.resume_plan_id = resume_plan_id
        self.fault_injector: Any = None
        self.plan: Plan | None = None

    async def run(self, request: Any) -> AgentResult:
        self.plan = await self.service.execute_goal(
            self.goal,
            resume_plan_id=self.resume_plan_id,
        )
        terminal = self.plan.status in {PlanStatus.COMPLETED, PlanStatus.WAITING_FOR_VERIFICATION}
        return AgentResult(
            status=AgentStatus.COMPLETED if terminal else AgentStatus.FAILED,
            final_output=self.plan.status.value,
            completion_claim=terminal,
            turn_count=1,
            tool_call_count=0,
            safe_trace=[],
            error_type=None if terminal else "PLAN_EXECUTION_FAILED",
        )


@dataclass
class _DemoRuntime:
    db: Database
    service: PlanExecutionService
    goal: Goal
    plan: Plan
    registry: ToolRegistry
    tool_calls: dict[str, int]


def _make_runtime(
    db: Database,
    *,
    plan: Plan,
    goal: Goal,
    handlers: dict[str, Callable[[Any], Any]],
    agent_claim: bool = False,
    verifier: Any = None,
) -> _DemoRuntime:
    registry = ToolRegistry()
    definitions = []
    calls: dict[str, int] = {step.capability: 0 for step in plan.steps}
    for capability in sorted(calls):
        spec = CapabilitySpec(name=capability, description=f"P43 demo capability {capability}")
        handler = handlers[capability]

        def wrapped(request: Any, *, _handler=handler, _capability=capability) -> Any:
            calls[_capability] += 1
            return _handler(request)

        registry.register(FakeTool(spec, wrapped))
        definitions.append(make_test_capability_definition(capability, output_schema={}))

    cap_registry, contract = make_test_capability_registry(registry, definitions)
    factory = None
    if agent_claim:
        # This is the canonical PlanExecutionService agent-executor seam.  It
        # still reaches the ToolContract; the service labels the resulting
        # step evidence AGENT_CLAIM, so WorkflowVerifier cannot trust it.
        factory = lambda step: _ToolExecutor(registry, step, db, {}, contract)
    service = PlanExecutionService(
        db,
        _FixedPlanner(plan),
        registry,
        agent_executor_factory=factory,
        capability_registry=cap_registry,
        tool_contract=contract,
        workflow_verifier=verifier,
    )
    return _DemoRuntime(db=db, service=service, goal=goal, plan=plan, registry=registry, tool_calls=calls)


def _new_db(label: str) -> Database:
    path = Path(tempfile.mkdtemp(prefix=f"odys-p43-{label}-")) / "demo.db"
    db = Database(path)
    db.init_db()
    return db


def _new_goal_plan(db: Database, name: str, steps: list[PlanStep]) -> tuple[Goal, Plan]:
    project = ProjectRepository(db).create(Project(name=f"p43-demo-{name}", type="demo"))
    goal = Goal(project_id=project.id, objective=f"P43 real trace demo: {name}")
    plan = Plan(goal_id=goal.id, mode=PlanMode.SIMPLE_DEPENDENCY, steps=steps)
    return goal, plan


def _request(snapshot: ProtocolSnapshot, *, run_id: str, task_id: str, config_name: str = "odys_p3") -> ExecutionRequest:
    config = snapshot.configs[config_name]
    fault = FaultPlan(
        fault_id=f"P43-DEMO-{task_id}",
        fault_type="tool_failure",
        trigger="first tool_call",
        trigger_count=1,
        deterministic_seed=43,
        definition={"demo_only": True, "task_id": task_id},
    )
    fixture = FixtureHandle(
        fixture_id=f"p43-demo-{task_id}",
        version="demo-v1",
        initial_state="demo-initial",
        fixture_hash=hashlib.sha256(task_id.encode()).hexdigest(),
        metadata={"demo_only": True},
    )
    task = {
        "task_id": task_id,
        "title": f"P43 demo {task_id}",
        "objective": "collect real Odys execution trace",
        "acceptance_criteria": [],
        "max_turns": 1,
        "max_model_calls": 1,
    }
    return ExecutionRequest(
        run_id=run_id,
        repeat_index=1,
        task=task,
        config=config,
        fixture=fixture,
        fault=fault,
        fault_context=FaultContext(fault),
    )


async def _run_through_odys(
    runtime: _DemoRuntime,
    snapshot: ProtocolSnapshot,
    *,
    task_id: str,
    run_id: str,
    config_name: str = "odys_p3",
    resume_plan_id: str | None = None,
) -> None:
    kernel = _PlanServiceKernel(runtime.service, runtime.goal, resume_plan_id=resume_plan_id)
    executor = OdysRuntimeExecutor(
        kernel=kernel,
        config=snapshot.configs[config_name],
        snapshot=snapshot,
        db=runtime.db,
    )
    await executor.execute(_request(snapshot, run_id=run_id, task_id=task_id, config_name=config_name))


def _ids_for_step(db: Database, step_id: str) -> tuple[str, str]:
    plan = next(
        plan
        for plan_id in _plan_ids(db)
        for plan in [PlanRepository(db).get(plan_id)]
        if plan is not None and any(step.id == step_id for step in plan.steps)
    )
    step = next(step for step in plan.steps if step.id == step_id)
    if not step.task_id:
        return "", ""
    from lhas.persistence.repositories import AttemptRepository, RunRepository

    runs = RunRepository(db).list_for_task(step.task_id)
    if not runs:
        return step.task_id, ""
    attempts = AttemptRepository(db).list_for_run(runs[-1].id)
    return step.task_id, attempts[-1].id if attempts else ""


def _plan_ids(db: Database) -> list[str]:
    # PlanRepository has no list-all API; event payloads are the durable index
    # for this short-lived demo database.
    ids: list[str] = []
    for event in EventStore(db).list_all():
        candidate = event.payload.get("plan_id")
        if candidate and candidate not in ids:
            ids.append(candidate)
    return ids


def _event_record(
    *,
    event_type: str,
    task_id: str,
    step_id: str,
    attempt_id: str,
    status: str,
    metadata: dict[str, Any],
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": _timestamp(timestamp),
        "event_type": event_type,
        "task_id": task_id,
        "step_id": step_id,
        "attempt_id": attempt_id,
        "status": status,
        "metadata": metadata,
    }


def _project_events(
    db: Database,
    *,
    story: str,
    config: str,
    after_event_id: int = 0,
    agent_claim: bool = False,
) -> list[dict[str, Any]]:
    """Project only observed EventStore transitions into GIF trace schema."""
    events = EventStore(db).list_all()
    records: list[dict[str, Any]] = []
    plan_cache: dict[str, Plan | None] = {}

    def step_identity(step_id: str) -> tuple[str, str]:
        for plan_id in _plan_ids(db):
            if plan_id not in plan_cache:
                plan_cache[plan_id] = PlanRepository(db).get(plan_id)
            plan = plan_cache[plan_id]
            if plan is not None and any(step.id == step_id for step in plan.steps):
                return _ids_for_step(db, step_id)
        return "", ""

    for event in events:
        if int(event.id or 0) <= after_event_id:
            continue
        payload = dict(event.payload or {})
        source_type = event.event_type.value
        step_id = str(payload.get("step_id") or "")
        task_id = str(event.task_id or payload.get("task_id") or "")
        attempt_id = str(event.attempt_id or payload.get("attempt_id") or "")
        if step_id and (not task_id or not attempt_id):
            step_task, step_attempt = step_identity(step_id)
            task_id = task_id or step_task
            attempt_id = attempt_id or step_attempt
        base = {
            "story": story,
            "config": config,
            "source_event_id": event.id,
            "source_event_type": source_type,
            "plan_id": payload.get("plan_id"),
        }

        mapped: list[tuple[str, str]] = []
        if source_type == EventType.PLAN_STEP_STARTED.value:
            mapped.append(("STEP_DISPATCHED", "RUNNING"))
        elif source_type == EventType.PLAN_STEP_FAILED.value:
            mapped.append(("FAILURE_DETECTED", "FAILED"))
        elif source_type == EventType.STEP_FAILURE_PROVENANCE.value:
            mapped.append(("StepFailureProvenance", "RECORDED"))
        elif source_type in {EventType.REPAIR_STARTED.value, EventType.REPAIR_COMPLETED.value}:
            if not step_id and payload.get("repair_step_ids"):
                step_id = str(payload["repair_step_ids"][0])
                step_task, step_attempt = step_identity(step_id)
                task_id = task_id or step_task
                attempt_id = attempt_id or step_attempt
            mapped.append((source_type, str(payload.get("outcome") or "RECORDED")))
        elif source_type == EventType.STEP_STATE_TRANSITION.value:
            new_status = str(payload.get("new_status") or "")
            if new_status == PlanStepStatus.CLAIMED_COMPLETE.value and agent_claim:
                mapped.append(("AGENT_CLAIM", new_status))
            elif new_status == PlanStepStatus.WAITING_FOR_VERIFICATION.value:
                mapped.append(("WAITING_FOR_VERIFICATION", new_status))
            elif new_status == PlanStepStatus.CLASSIFIED_FAILURE.value:
                mapped.append(("FAILURE_DETECTED", new_status))
            elif new_status == PlanStepStatus.VERIFIED.value:
                # Both names are useful to the demo consumer, and both are
                # projections of the same durable VERIFIED transition.
                mapped.extend((("VERIFICATION_PASSED", new_status), ("STEP_VERIFIED", new_status)))
        # VALIDATION_* belongs to the lower-level orchestrator validator.  It
        # is intentionally not projected as workflow verification: only the
        # real WorkflowVerifier call/result below may produce those labels.

        for mapped_type, status in mapped:
            records.append(
                _event_record(
                    event_type=mapped_type,
                    task_id=task_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    status=status,
                    metadata={**base, **payload},
                    timestamp=event.timestamp,
                )
            )
    return records


def _last_event_id(db: Database) -> int:
    events = EventStore(db).list_all()
    return int(events[-1].id or 0) if events else 0


def _append_verification_projection(
    records: list[dict[str, Any]],
    *,
    story: str,
    config: str,
    step: PlanStep,
    accepted: bool,
    validation_id: Any,
    reason: str,
) -> None:
    """Record the observed verifier call/result, not a fabricated transition."""
    task_id = str(step.task_id or "")
    validation_identity = validation_id
    validation_id = getattr(validation_identity, "id", validation_identity)
    attempt_id = str(getattr(validation_identity, "attempt_id", "") or "")
    metadata = {
        "story": story,
        "config": config,
        "source": "WorkflowVerifier.verify",
        "validation_id": validation_id,
        "reason": reason[:500],
        "observed_result": "PASSED" if accepted else "FAILED",
    }
    records.append(_event_record(
        event_type="VERIFICATION_STARTED",
        task_id=task_id,
        step_id=step.id,
        attempt_id=attempt_id,
        status="STARTED",
        metadata=metadata,
    ))
    records.append(_event_record(
        event_type="VERIFICATION_PASSED" if accepted else "VERIFICATION_FAILED",
        task_id=task_id,
        step_id=step.id,
        attempt_id=attempt_id,
        status="PASSED" if accepted else "FAILED",
        metadata=metadata,
    ))


def _verify_pending(
    runtime: _DemoRuntime,
    step_id: str,
    *,
    story: str,
    config: str,
    records: list[dict[str, Any]],
) -> Any:
    plan = PlanRepository(runtime.db).get(runtime.plan.id)
    if plan is None:
        raise RuntimeError("DEMO_PLAN_MISSING")
    step = next(item for item in plan.steps if item.id == step_id)
    verifier = WorkflowVerifier(runtime.db)
    result = verifier.verify(step, plan, EventStore(runtime.db))
    _append_verification_projection(
        records,
        story=story,
        config=config,
        step=step,
        accepted=result.accepted,
        validation_id=result.validation,
        reason=result.reason,
    )
    return plan, step, result


async def _collect_false_completion(snapshot: ProtocolSnapshot, output_dir: Path) -> list[dict[str, Any]]:
    db = _new_db("false")
    try:
        step = PlanStep(
            id="false-step",
            title="agent completion claim",
            objective="claim completion without trusted evidence",
            capability="demo.false_completion",
            expected_effects={"verified": True},
        )
        goal, plan = _new_goal_plan(db, "false-completion", [step])
        runtime = _make_runtime(
            db,
            plan=plan,
            goal=goal,
            agent_claim=True,
            handlers={"demo.false_completion": lambda req: ToolResult(status=ToolResultStatus.SUCCESS, output={"verified": False})},
        )
        await _run_through_odys(runtime, snapshot, task_id="P43-FALSE-COMPLETION", run_id="p43-demo-false-1")
        records: list[dict[str, Any]] = []
        last_event_id = 0
        records.extend(_project_events(db, story="false_completion_prevention", config="odys_p3", after_event_id=last_event_id, agent_claim=True))
        last_event_id = _last_event_id(db)
        current = PlanRepository(db).get(plan.id)
        if current is None:
            raise RuntimeError("FALSE_COMPLETION_PLAN_NOT_PERSISTED")
        current_step = current.steps[0]
        if current_step.status != PlanStepStatus.WAITING_FOR_VERIFICATION:
            raise RuntimeError(f"FALSE_COMPLETION_EXPECTED_WAITING:{current_step.status}")
        current, current_step, verification = _verify_pending(
            runtime,
            current_step.id,
            story="false_completion_prevention",
            config="odys_p3",
            records=records,
        )
        if verification.accepted:
            raise RuntimeError("FALSE_COMPLETION_UNEXPECTEDLY_VERIFIED")
        provenance, scope, _ = runtime.service._handle_verification_rejection(
            current_step,
            current,
            verification,
            EventStore(db),
        )
        if provenance is None:
            raise RuntimeError("FALSE_COMPLETION_PROVENANCE_MISSING")
        PlanRepository(db).update(current)
        records.extend(_project_events(db, story="false_completion_prevention", config="odys_p3", after_event_id=last_event_id, agent_claim=True))
        _write_jsonl(output_dir / "false_completion_prevention.jsonl", records)
        return records
    finally:
        db.close()


async def _collect_selective_repair(snapshot: ProtocolSnapshot, output_dir: Path) -> list[dict[str, Any]]:
    db = _new_db("selective")
    try:
        a = PlanStep(
            id="step-a",
            title="Step A",
            objective="produce trusted A evidence",
            capability="demo.selective_a",
            expected_effects={"ok": True},
        )
        b = PlanStep(
            id="step-b",
            title="Step B",
            objective="produce trusted B evidence",
            capability="demo.selective_b",
            expected_effects={"ok": True},
            depends_on=["step-a"],
        )
        goal, plan = _new_goal_plan(db, "selective-repair", [a, b])
        b_calls = {"count": 0}

        def b_handler(req: Any) -> ToolResult:
            b_calls["count"] += 1
            return ToolResult(
                status=ToolResultStatus.SUCCESS,
                output={"ok": b_calls["count"] > 1},
            )

        runtime = _make_runtime(
            db,
            plan=plan,
            goal=goal,
            handlers={
                "demo.selective_a": lambda req: ToolResult(status=ToolResultStatus.SUCCESS, output={"ok": True}),
                "demo.selective_b": b_handler,
            },
        )
        # First pass is deliberately fail-closed: A executes, then waits for
        # external verification, so B cannot be dispatched yet.
        await _run_through_odys(runtime, snapshot, task_id="P43-SELECTIVE-A", run_id="p43-demo-selective-a-1")
        current = PlanRepository(db).get(plan.id)
        if current is None:
            raise RuntimeError("SELECTIVE_PLAN_NOT_PERSISTED")
        a_current = current.steps[0]
        records: list[dict[str, Any]] = []
        last_event_id = 0
        records.extend(_project_events(db, story="selective_repair", config="odys_p3", after_event_id=last_event_id))
        last_event_id = _last_event_id(db)
        a_verification = WorkflowVerifier(db).verify(a_current, current, EventStore(db))
        _append_verification_projection(
            records,
            story="selective_repair",
            config="odys_p3",
            step=a_current,
            accepted=a_verification.accepted,
            validation_id=a_verification.validation,
            reason=a_verification.reason,
        )
        if not a_verification.accepted:
            raise RuntimeError("SELECTIVE_A_NOT_VERIFIED")
        transition_step(a_current, PlanStepStatus.VERIFIED, "demo_external_verification", EventStore(db), plan_id=current.id)
        PlanRepository(db).update(current)
        records.extend(_project_events(db, story="selective_repair", config="odys_p3", after_event_id=last_event_id))
        last_event_id = _last_event_id(db)

        # Resume through the same real executor; B executes once and waits.
        await _run_through_odys(
            runtime,
            snapshot,
            task_id="P43-SELECTIVE-B",
            run_id="p43-demo-selective-b-1",
            resume_plan_id=plan.id,
        )
        records.extend(_project_events(db, story="selective_repair", config="odys_p3", after_event_id=last_event_id))
        last_event_id = _last_event_id(db)
        current, b_current, b_verification = _verify_pending(
            runtime,
            "step-b",
            story="selective_repair",
            config="odys_p3",
            records=records,
        )
        if b_verification.accepted:
            raise RuntimeError("SELECTIVE_B_FAILURE_NOT_OBSERVED")
        provenance, scope, _ = runtime.service._handle_verification_rejection(
            b_current,
            current,
            b_verification,
            EventStore(db),
        )
        if provenance is None or str(scope.value) != "LOCAL":
            raise RuntimeError(f"SELECTIVE_LOCAL_SCOPE_MISSING:{scope}")
        PlanRepository(db).update(current)
        records.extend(_project_events(db, story="selective_repair", config="odys_p3", after_event_id=last_event_id))
        last_event_id = _last_event_id(db)

        # The public repair API owns REPAIR_STARTED, selective invalidation,
        # the new attempt, lineage, and REPAIR_COMPLETED.
        repair_runtime = _make_runtime(
            db,
            plan=current,
            goal=goal,
            handlers={
                "demo.selective_a": lambda req: ToolResult(status=ToolResultStatus.SUCCESS, output={"ok": True}),
                "demo.selective_b": b_handler,
            },
            verifier=WorkflowVerifier(db),
        )
        await repair_runtime.service.repair_after_failure(
            current.id,
            "step-b",
            goal,
        )
        records.extend(_project_events(db, story="selective_repair", config="odys_p3", after_event_id=last_event_id))
        _write_jsonl(output_dir / "selective_repair.jsonl", records)
        return records
    finally:
        db.close()


async def _collect_baseline(snapshot: ProtocolSnapshot, output_dir: Path) -> list[dict[str, Any]]:
    """Collect a real minimal/native failure timeline for the comparison GIF."""
    from lhas.native.models import ProviderResponse
    from lhas.native.provider import ScriptedProviderAdapter
    from evals.reliability.odys_executor import _build_kernel

    kernel, db = _build_kernel(snapshot.configs["minimal"], snapshot)
    try:
        kernel.provider = ScriptedProviderAdapter(
            responses=[ProviderResponse(content="stopped", completion_claim=False)],
            provider_id="p43-demo-baseline",
            model_id="p43-demo-baseline",
            endpoint_identity="local-demo",
        )
        await OdysRuntimeExecutor(
            kernel=kernel,
            config=snapshot.configs["minimal"],
            snapshot=snapshot,
            db=db,
        ).execute(_request(snapshot, run_id="p43-demo-baseline-1", task_id="P43-BASELINE", config_name="minimal"))
        records: list[dict[str, Any]] = []
        for event in EventStore(db).list_all():
            if event.event_type == EventType.NATIVE_MODEL_TURN_STARTED:
                records.append(_event_record(
                    event_type="STEP_DISPATCHED",
                    task_id=str(event.task_id or "P43-BASELINE"),
                    step_id="baseline-step",
                    attempt_id=str(event.attempt_id or ""),
                    status="RUNNING",
                    metadata={"config": "minimal", "source_event_id": event.id, "source_event_type": event.event_type.value},
                    timestamp=event.timestamp,
                ))
            elif event.event_type in {
                EventType.NATIVE_BUDGET_EXHAUSTED,
                EventType.NATIVE_PROVIDER_FAILURE,
                EventType.REPLAN_SIGNAL_CREATED,
            }:
                records.append(_event_record(
                    event_type="FAILURE_DETECTED",
                    task_id=str(event.task_id or "P43-BASELINE"),
                    step_id="baseline-step",
                    attempt_id=str(event.attempt_id or ""),
                    status="FAILED",
                    metadata={"config": "minimal", "source_event_id": event.id, "source_event_type": event.event_type.value, **event.payload},
                    timestamp=event.timestamp,
                ))
        if not records:
            raise RuntimeError("BASELINE_NATIVE_FAILURE_EVENT_MISSING")
        _write_jsonl(output_dir / "baseline.jsonl", records)
        return records
    finally:
        db.close()


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            missing = [field for field in REQUIRED_TRACE_FIELDS if field not in record]
            if missing:
                raise RuntimeError(f"TRACE_SCHEMA_MISSING:{path}:{missing}")
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _combine(output_dir: Path, records: Iterable[dict[str, Any]]) -> Path:
    path = output_dir / "p43-demo-combined.jsonl"
    _write_jsonl(path, records)
    return path


async def collect(output_dir: Path, gif_dir: Path, protocol_root: Path = DEFAULT_PROTOCOL_ROOT) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("*.jsonl"):
        old.unlink()
    snapshot = ProtocolSnapshot.load(protocol_root)
    false_records = await _collect_false_completion(snapshot, output_dir)
    selective_records = await _collect_selective_repair(snapshot, output_dir)
    baseline_records = await _collect_baseline(snapshot, output_dir)
    combined = _combine(output_dir, [*false_records, *selective_records, *baseline_records])
    try:
        gifs = generate_gifs(combined, gif_dir)
    except TraceEventError as exc:
        raise RuntimeError(f"GIF_INPUT_REJECTED:{exc}") from exc
    return {
        "trace_count": 3,
        "trace_files": [
            str(output_dir / "false_completion_prevention.jsonl"),
            str(output_dir / "selective_repair.jsonl"),
            str(output_dir / "baseline.jsonl"),
        ],
        "combined_trace": str(combined),
        "gif_files": [str(path) for path in gifs.values()],
        "protocol_hash": snapshot.protocol_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("results/demo_traces"))
    parser.add_argument("--gif-dir", type=Path, default=Path("evals/reliability/demo_gif"))
    parser.add_argument("--protocol-root", type=Path, default=DEFAULT_PROTOCOL_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(collect(args.output_dir, args.gif_dir, args.protocol_root))
    except Exception as exc:
        print(f"TRACE_COLLECTION_FAILED={type(exc).__name__}:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print("TRACE_COUNT=3")
    print("FALSE_COMPLETION_TRACE=YES")
    print("SELECTIVE_REPAIR_TRACE=YES")
    print("RECOVERY_TRACE=YES")
    print("GIF_READY=YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
