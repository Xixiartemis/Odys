"""The Odys-owned model/tool/validation execution loop."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from lhas.agent.models import AgentRequest, AgentResult, AgentStatus
from lhas.domain.enums import EventType
from lhas.native.completion import CompletionAuthority
from lhas.native.context import NativeContextAssembler
from lhas.native.delegation import DurableDeliveryService
from lhas.native.models import (
    CandidateStatus,
    ExecutionSnapshot,
    NativeFaultPoint,
    NativePhase,
    NoOpNativeFaultInjector,
    ReplanSignal,
)
from lhas.native.parser import ModelResponseError, ModelResponseParser
from lhas.native.persistence import (
    ExecutionSnapshotRepository,
    ReplanSignalRepository,
    ValidationFailureRepository,
)
from lhas.native.models import ProviderFailureCategory, ProviderHealthState, RuntimeTarget
from lhas.native.runtime import ProviderFailureClassifier, ProviderHealthRepository
from lhas.persistence.event_store import EventStore


class NativeAgentKernel:
    """Own every turn boundary and accept completion only after validation."""

    name = "NativeAgentKernel"

    def __init__(
        self,
        *,
        db,
        provider,
        dispatcher,
        completion_authority: CompletionAuthority,
        parser: ModelResponseParser | None = None,
        context_assembler: NativeContextAssembler | None = None,
        provider_timeout_seconds: float = 120.0,
        fault_injector: Any = None,
        runtime_target_controller: Any = None,
        provider_health: ProviderHealthRepository | None = None,
        provider_factory: Any = None,
    ):
        self.db = db
        self.provider = provider
        self.dispatcher = dispatcher
        self.completion = completion_authority
        self.parser = parser or ModelResponseParser()
        self.context_assembler = context_assembler or NativeContextAssembler()
        self.provider_timeout_seconds = min(max(float(provider_timeout_seconds), 0.1), 300.0)
        self.fault_injector = fault_injector or NoOpNativeFaultInjector()
        self.snapshots = ExecutionSnapshotRepository(db)
        self.validation_failures = ValidationFailureRepository(db)
        self.replans = ReplanSignalRepository(db)
        self.events = EventStore(db)
        self.delivery = DurableDeliveryService(db)
        self._states: dict[str, AgentStatus] = {}
        self.runtime_target_controller = runtime_target_controller
        self.provider_health = provider_health or ProviderHealthRepository(db)
        self.provider_factory = provider_factory

    def switch_runtime_target(self, new_target: RuntimeTarget, *, expected_current: RuntimeTarget,
                              runtime_id: str | None = None, reason: str = "explicit provider migration"):
        if self.runtime_target_controller is None:
            raise RuntimeError("RUNTIME_TARGET_CONTROLLER_REQUIRED")
        if runtime_id is None:
            raise RuntimeError("RUNTIME_ID_REQUIRED")
        switch = self.runtime_target_controller.request_switch(runtime_id, new_target,
            expected_current=expected_current, runtime_id=runtime_id, reason=reason)
        if switch.state.value == "COMMITTED" and self.provider_factory is not None:
            self.provider = self.provider_factory(new_target)
        active = getattr(self, "_active_snapshot", None)
        if active is not None and active.run_id == runtime_id and switch.state.value == "COMMITTED":
            active.effective_target = new_target
            active.fallback_reason = reason
            active.target_event_id = switch.id
            self._save(active)
        return switch

    async def run(self, request: AgentRequest) -> AgentResult:
        task_id, run_id, attempt_id = self._ids(request)
        self._states[request.agent_id] = AgentStatus.RUNNING
        snapshot = self.snapshots.get_for_attempt(attempt_id)
        if snapshot is None:
            snapshot = self._new_snapshot(request, task_id, run_id, attempt_id)
            self._save(snapshot)
        self._ensure_runtime_target(request, snapshot)
        self._active_snapshot = snapshot
        self.dispatcher.restore_observer(snapshot.repeated_failure_state)

        pending = await self.completion.resume_pending(attempt_id)
        if pending and pending.status is CandidateStatus.ACCEPTED:
            snapshot.completion_candidate_id = pending.id
            snapshot.phase = NativePhase.ACCEPTED_COMPLETE
            self._save(snapshot)
            return self._accepted(request, snapshot, pending.summary)
        if pending and pending.status is CandidateStatus.REJECTED:
            snapshot.completion_candidate_id = pending.id
            snapshot.phase = NativePhase.RECOVERING
            snapshot.current_failure = {"type": "VALIDATOR_REJECTION", "candidate_id": pending.id}
            self._save(snapshot)

        durable_count_before = snapshot.tool_call_count
        reconciled = await self.dispatcher.reconcile_unfinished(attempt_id)
        invocations = self.dispatcher.invocations.list_for_attempt(attempt_id)
        recovered_finished = [
            {
                "capability": item.capability,
                "status": item.result_status,
                "error_type": item.error_type,
                "safe_summary": item.result_summary,
                "recovered_from_durable_invocation": True,
            }
            for item in invocations
            if item.ordinal > durable_count_before and item.state.value == "FINISHED"
        ]
        if reconciled or recovered_finished:
            snapshot.recent_tool_outcomes.extend(recovered_finished)
            snapshot.recent_tool_outcomes.extend(reconciled)
            snapshot.tool_call_count = max([snapshot.tool_call_count, *[item.ordinal for item in invocations]])
            snapshot.workspace_mutation_version += sum(
                1 for item in invocations
                if item.ordinal > durable_count_before and item.observed_mutation
            )
            snapshot.phase = NativePhase.RECOVERING
            snapshot.current_failure = {
                "type": "PROCESS_INTERRUPTED_TOOL",
                "reconciled_count": len(reconciled),
                "recovered_finished_count": len(recovered_finished),
            }
            self._save(snapshot)

        while snapshot.model_turn_count < request.budget.max_turns:
            self._consume_deliveries(snapshot)
            if snapshot.tool_call_count >= request.budget.max_tool_calls:
                return self._budget_exhausted(request, snapshot, "TOOL_CALL_BUDGET")
            failures = self.validation_failures.list_for_attempt(attempt_id)
            signals = self.replans.list_for_attempt(attempt_id)
            context = self.context_assembler.build(
                request,
                snapshot,
                validation_failures=failures,
                replan_signals=signals,
            )
            next_turn = snapshot.model_turn_count + 1
            if snapshot.effective_target is not None:
                health = self.provider_health.get(snapshot.effective_target)
                if health and health["state"] in {ProviderHealthState.QUOTA_BLOCKED.value, ProviderHealthState.AUTH_BLOCKED.value}:
                    return self._provider_failure(request, snapshot, ProviderFailureCategory(health["failure_category"] or ProviderFailureCategory.UNKNOWN_PROVIDER_FAILURE.value), next_turn, health.get("reason"))
            target_payload = {
                "configured_target": snapshot.configured_target.safe_projection() if snapshot.configured_target else None,
                "effective_target": snapshot.effective_target.safe_projection() if snapshot.effective_target else None,
            }
            self.events.append(EventType.NATIVE_MODEL_TURN_STARTED, task_id=task_id, run_id=run_id, attempt_id=attempt_id, payload={"turn": next_turn, "model_turn_ordinal": next_turn, "provider": getattr(self.provider, "name", type(self.provider).__name__), **target_payload, "context_chars": context.chars_used, "context_budget": context.budget_chars})
            started = time.monotonic()
            try:
                raw = await self.provider.generate(
                    context=context,
                    tools=self.dispatcher.tool_schemas(),
                    timeout_seconds=self.provider_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                return self._provider_failure(request, snapshot, ProviderFailureCategory.PROVIDER_TIMEOUT, next_turn, str(exc))
            except Exception as exc:
                return self._provider_failure(request, snapshot, ProviderFailureClassifier.classify(exc), next_turn, str(exc))
            duration_ms = int((time.monotonic() - started) * 1000)
            try:
                response = self.parser.parse(raw)
            except ModelResponseError as exc:
                snapshot.model_turn_count = next_turn
                snapshot.phase = NativePhase.FAILED
                snapshot.current_failure = {"type": "PROVIDER_MALFORMED_RESPONSE", "failure_category": "MALFORMED_PROVIDER_RESPONSE", "category": str(exc)[:128]}
                self._save(snapshot)
                self.events.append(EventType.NATIVE_MODEL_RESPONSE_REJECTED, task_id=task_id, run_id=run_id, attempt_id=attempt_id, payload={"turn": next_turn, "error_type": "PROVIDER_MALFORMED_RESPONSE", "category": str(exc)[:128]})
                self._create_replan(snapshot, "PROVIDER_MALFORMED_RESPONSE", {"category": str(exc)[:128]})
                return self._failed(request, snapshot, "PROVIDER_MALFORMED_RESPONSE")

            snapshot.model_turn_count = next_turn
            snapshot.model_turn_ordinal = next_turn
            snapshot.phase = NativePhase.CONTINUE
            self._save(snapshot)
            self.events.append(
                EventType.NATIVE_MODEL_TURN_COMPLETED,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
                payload={
                    "turn": next_turn,
                    "duration_ms": duration_ms,
                    "tool_call_count": len(response.tool_calls),
                    "completion_claim": response.completion_claim,
                    "content_sha256": hashlib.sha256(response.content.encode("utf-8")).hexdigest(),
                    "usage": self._safe_usage(response.usage),
                },
            )
            self.fault_injector.hit(NativeFaultPoint.AFTER_MODEL_TURN_PERSISTED, snapshot=snapshot)

            if response.tool_calls:
                if snapshot.tool_call_count + len(response.tool_calls) > request.budget.max_tool_calls:
                    return self._budget_exhausted(request, snapshot, "TOOL_CALL_BUDGET")
                snapshot.phase = NativePhase.WAITING_TOOL
                self._save(snapshot)
                for call in response.tool_calls:
                    previous_verification = dict(snapshot.verification_state)
                    observation = await self.dispatcher.dispatch(call, request, snapshot)
                    snapshot.tool_call_count += 1
                    snapshot.recent_tool_outcomes.append(observation)
                    snapshot.repeated_failure_state = self.dispatcher.observer_state()
                    summary = observation.get("safe_summary") if isinstance(observation, dict) else {}
                    if isinstance(summary, dict) and summary.get("meaningful_mutation"):
                        snapshot.workspace_mutation_version += 1
                    if isinstance(summary, dict) and summary.get("verification_kind"):
                        snapshot.verification_state = {
                            "kind": summary.get("verification_kind"),
                            "outcome": summary.get("pytest_observation"),
                            "tool_call_index": summary.get("tool_call_index"),
                        }
                        if previous_verification.get("outcome") == "PASS" and snapshot.verification_state.get("outcome") == "FAIL":
                            self._create_replan(snapshot, "VERIFICATION_REGRESSION", {"previous": "PASS", "current": "FAIL"})
                    if isinstance(summary, dict) and summary.get("strategy_change_required"):
                        self._create_replan(snapshot, "REPEATED_TOOL_FAILURE", {"capability": observation.get("capability"), "error_type": observation.get("error_type")})
                    if observation.get("status") == "FAILURE":
                        snapshot.current_failure = {
                            "type": observation.get("error_type") or "TOOL_FAILURE",
                            "capability": observation.get("capability"),
                        }
                    self._save(snapshot)
                snapshot.phase = NativePhase.CONTINUE
                self._save(snapshot)
                continue

            if response.completion_claim:
                snapshot.phase = NativePhase.CANDIDATE_COMPLETE
                self._save(snapshot)
                candidate = await self.completion.evaluate_claim(snapshot, response.content, source="MODEL_CLAIM")
                snapshot.completion_candidate_id = candidate.id
                if candidate.status is CandidateStatus.ACCEPTED:
                    snapshot.phase = NativePhase.ACCEPTED_COMPLETE
                    snapshot.current_failure = {}
                    self._save(snapshot)
                    return self._accepted(request, snapshot, candidate.summary)
                snapshot.phase = NativePhase.RECOVERING
                snapshot.current_failure = {"type": "VALIDATOR_REJECTION", "candidate_id": candidate.id}
                snapshot.recent_tool_outcomes.append({
                    "capability": "completion.validate",
                    "status": "FAILURE",
                    "error_type": "VALIDATOR_REJECTION",
                    "candidate_id": candidate.id,
                    "safe_summary": candidate.validation,
                })
                self._save(snapshot)
                continue

            snapshot.phase = NativePhase.FAILED
            snapshot.current_failure = {"type": "MODEL_STOPPED_WITHOUT_COMPLETION"}
            self._save(snapshot)
            self._create_replan(snapshot, "MODEL_STOPPED_WITHOUT_COMPLETION", {})
            return self._failed(request, snapshot, "MODEL_STOPPED_WITHOUT_COMPLETION")

        return self._budget_exhausted(request, snapshot, "TURN_BUDGET")

    async def cancel(self, agent_id: str) -> None:
        self._states[agent_id] = AgentStatus.CANCELLED

    async def status(self, agent_id: str) -> dict[str, Any]:
        return {"agent_id": agent_id, "status": self._states.get(agent_id, AgentStatus.PENDING).value, "kernel": self.name}

    @staticmethod
    def _ids(request: AgentRequest) -> tuple[str, str, str]:
        try:
            return (str(request.metadata["task_id"]), str(request.metadata["run_id"]), str(request.metadata["attempt_id"]))
        except KeyError as exc:
            raise ValueError("native kernel requires durable task_id/run_id/attempt_id") from exc

    @staticmethod
    def _new_snapshot(request: AgentRequest, task_id: str, run_id: str, attempt_id: str) -> ExecutionSnapshot:
        runtime = request.context if isinstance(request.context, dict) else {}
        taskgraph = runtime.get("taskgraph") if isinstance(runtime.get("taskgraph"), dict) else {}
        return ExecutionSnapshot(
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            goal=request.objective,
            taskgraph_position=taskgraph.get("active_node") or runtime.get("active_node"),
            completed_nodes=list(taskgraph.get("completed_nodes") or runtime.get("completed_nodes") or []),
            pending_nodes=list(taskgraph.get("pending_nodes") or runtime.get("pending_nodes") or []),
            workspace_identity=dict(runtime.get("workspace_identity") or {}),
        )

    def _save(self, snapshot: ExecutionSnapshot) -> None:
        self.snapshots.save(snapshot)
        self.events.append(EventType.NATIVE_EXECUTION_SNAPSHOT, task_id=snapshot.task_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id, payload={"snapshot_id": snapshot.id, "version": snapshot.version, "phase": snapshot.phase.value, "model_turn_count": snapshot.model_turn_count, "tool_call_count": snapshot.tool_call_count, "workspace_mutation_version": snapshot.workspace_mutation_version, "completion_candidate_id": snapshot.completion_candidate_id})

    def _provider_failure(self, request: AgentRequest, snapshot: ExecutionSnapshot, error_type: str | ProviderFailureCategory, turn: int, detail: str | None = None) -> AgentResult:
        category = ProviderFailureCategory(error_type)
        snapshot.model_turn_count = turn
        snapshot.model_turn_ordinal = turn
        snapshot.phase = NativePhase.RECOVERING
        snapshot.current_failure = {"type": category.value, "category": category.value, "detail": (detail or "")[:512],
                                    "same_target_retryable": category in {ProviderFailureCategory.TRANSIENT_RATE_LIMIT, ProviderFailureCategory.PROVIDER_UNAVAILABLE, ProviderFailureCategory.PROVIDER_TIMEOUT}}
        self._save(snapshot)
        if snapshot.effective_target is not None:
            health_state = ProviderHealthState.QUOTA_BLOCKED if category is ProviderFailureCategory.QUOTA_EXHAUSTED else ProviderHealthState.AUTH_BLOCKED if category is ProviderFailureCategory.AUTH_INVALID else ProviderHealthState.TRANSIENTLY_UNAVAILABLE if category in {ProviderFailureCategory.PROVIDER_UNAVAILABLE, ProviderFailureCategory.PROVIDER_TIMEOUT, ProviderFailureCategory.TRANSIENT_RATE_LIMIT} else ProviderHealthState.UNKNOWN
            self.provider_health.record(snapshot.effective_target, health_state, category=category, reason=(detail or category.value))
            self.events.append(EventType.PROVIDER_HEALTH_CHANGED, task_id=snapshot.task_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id, payload={"target": snapshot.effective_target.safe_projection(), "state": health_state.value, "failure_category": category.value})
        self.events.append(EventType.NATIVE_PROVIDER_FAILURE, task_id=snapshot.task_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id, payload={"turn": turn, "failure_category": category.value, "configured_target": snapshot.configured_target.safe_projection() if snapshot.configured_target else None, "effective_target": snapshot.effective_target.safe_projection() if snapshot.effective_target else None})
        return self._failed(request, snapshot, category.value)

    def _ensure_runtime_target(self, request: AgentRequest, snapshot: ExecutionSnapshot) -> None:
        metadata = request.metadata or {}
        configured = metadata.get("configured_target")
        effective = metadata.get("effective_target")
        if configured is not None:
            configured = configured if isinstance(configured, RuntimeTarget) else RuntimeTarget.model_validate(configured)
        elif getattr(self.provider, "runtime_target", None) is not None:
            configured = self.provider.runtime_target
        if effective is not None:
            effective = effective if isinstance(effective, RuntimeTarget) else RuntimeTarget.model_validate(effective)
        elif configured is not None:
            effective = configured
        if self.runtime_target_controller is not None and configured is not None:
            try:
                binding = self.runtime_target_controller.current(run_id := snapshot.run_id)
            except Exception:
                self.runtime_target_controller.bind(snapshot.run_id, configured, run_id=snapshot.run_id, session_id=metadata.get("session_id"))
                binding = self.runtime_target_controller.current(snapshot.run_id)
            configured = binding["configured_target"]
            effective = binding["effective_target"]
            snapshot.fallback_reason = binding.get("fallback_reason")
            snapshot.target_event_id = binding.get("switch_id")
        changed = snapshot.configured_target != configured or snapshot.effective_target != effective
        snapshot.configured_target = configured
        snapshot.effective_target = effective
        if changed:
            self._save(snapshot)

    def _budget_exhausted(self, request: AgentRequest, snapshot: ExecutionSnapshot, budget: str) -> AgentResult:
        snapshot.phase = NativePhase.REPLANNING
        snapshot.current_failure = {"type": "BUDGET_EXHAUSTED", "budget": budget}
        self._save(snapshot)
        self.events.append(EventType.NATIVE_BUDGET_EXHAUSTED, task_id=snapshot.task_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id, payload={"budget": budget, "model_turn_count": snapshot.model_turn_count, "tool_call_count": snapshot.tool_call_count})
        self._create_replan(snapshot, "BUDGET_EXHAUSTED", {"budget": budget})
        return self._failed(request, snapshot, "BUDGET_EXHAUSTED")

    def _create_replan(self, snapshot: ExecutionSnapshot, reason: str, evidence: dict[str, Any]) -> None:
        signal = ReplanSignal(task_id=snapshot.task_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id, reason=reason, scope="TASKGRAPH_NODE", failed_node_id=snapshot.taskgraph_position, evidence=evidence)
        self.replans.create(signal)
        self.events.append(EventType.REPLAN_SIGNAL_CREATED, task_id=snapshot.task_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id, payload={"signal_id": signal.id, "reason": signal.reason, "scope": signal.scope, "failed_node_id": signal.failed_node_id})

    def _consume_deliveries(self, snapshot: ExecutionSnapshot) -> None:
        outcomes = self.delivery.consume_for_parent_attempt(
            snapshot.attempt_id,
            set(snapshot.consumed_delivery_tokens),
        )
        if not outcomes:
            return
        for item in outcomes:
            token = item["delivery_token"]
            snapshot.delegation_dependencies[token] = item
            snapshot.consumed_delivery_tokens.append(token)
            status = str(item.get("outcome", {}).get("status", ""))
            if status and status != "COMPLETED":
                self._create_replan(snapshot, "CHILD_FAILURE", {"delivery_token": token, "child_status": status})
        self._save(snapshot)

    def _accepted(self, request: AgentRequest, snapshot: ExecutionSnapshot, output: str) -> AgentResult:
        self._states[request.agent_id] = AgentStatus.COMPLETED
        return AgentResult(status=AgentStatus.COMPLETED, final_output=output, completion_claim=True, turn_count=snapshot.model_turn_count, tool_call_count=snapshot.tool_call_count, safe_trace=snapshot.recent_tool_outcomes[-100:], artifacts={"completion_candidate_id": snapshot.completion_candidate_id, "execution_snapshot_id": snapshot.id})

    def _failed(self, request: AgentRequest, snapshot: ExecutionSnapshot, error_type: str) -> AgentResult:
        self._states[request.agent_id] = AgentStatus.FAILED
        if snapshot.phase not in {NativePhase.REPLANNING, NativePhase.RECOVERING}:
            snapshot.phase = NativePhase.FAILED
            self._save(snapshot)
        return AgentResult(status=AgentStatus.FAILED, completion_claim=False, turn_count=snapshot.model_turn_count, tool_call_count=snapshot.tool_call_count, safe_trace=snapshot.recent_tool_outcomes[-100:], artifacts={"execution_snapshot_id": snapshot.id, "workspace_mutation_version": snapshot.workspace_mutation_version}, error_type=error_type)

    @staticmethod
    def _safe_usage(usage: dict[str, Any]) -> dict[str, Any]:
        allowed = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
        return {key: value for key, value in usage.items() if key in allowed and isinstance(value, (int, float))}
