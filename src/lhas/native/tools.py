"""Odys-owned authorization, dispatch, observation, and reconciliation.

After P2.3 integration the normal invocation path is:

    NativeToolDispatcher → CapabilityRegistry → ToolContract → ToolRegistry → Tool

NativeToolDispatcher owns:
    - invocation identity and lifecycle events
    - policy enforcement (allowed_capabilities, side_effect, delegation budget)
    - duplicate invocation reconciliation
    - mutation observation
    - observer decoration

ToolContract owns:
    - capability resolution (CapabilityRegistry)
    - input/output JSON Schema validation
    - semantic argv prefix guards
    - tool resolution (ToolRegistry)
    - evidence generation
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from lhas.agent.models import AgentRequest
from lhas.capability_registry import (
    CapabilityAvailability,
    CapabilityRegistry,
    CapabilityRuntimeContext,
    default_capabilities,
)
from lhas.domain.enums import EventType
from lhas.domain.models import utcnow
from lhas.execution_control import ExecutionControlError, ExecutionControlToken, ExecutionLayerTimeout, await_with_control
from lhas.inner_agent.tool_adapter import ToolAwareObserver, _args_signature, safe_tool_summary
from lhas.side_effects import EffectClass, ReceiptStatus, SideEffectReceiptManager
from lhas.native.models import (
    ExecutionSnapshot,
    InvocationState,
    NativeFaultPoint,
    NoOpNativeFaultInjector,
    ProviderToolCall,
    ReconciliationDecision,
    SideEffectClass,
    ToolInvocation,
)
from lhas.native.persistence import ToolInvocationRepository
from lhas.persistence.event_store import EventStore
from lhas.tools.contract import ToolContract, ToolErrorCode
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus


_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)\s*[:=]\s*[^\s,;]+")
_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password|credential)")


def _safe_value(value: Any, limit: int = 12_000) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _SECRET.sub(r"\1=[REDACTED]", value)[:limit]
    if isinstance(value, list):
        return [_safe_value(item, max(256, limit // 20)) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: (
                "[REDACTED]"
                if _SECRET_KEY.search(str(key))
                else _safe_value(item, max(256, limit // 20))
            )
            for key, item in list(value.items())[:100]
        }
    return _safe_value(str(value), limit)


def _safe_tool_arguments(capability: str, arguments: Any) -> dict[str, Any]:
    """Return bounded forensic arguments without persisting arbitrary input.

    The invocation fingerprint remains the identity for the complete request.
    For diagnostics, only workspace-edit routing fields are projected; file
    contents are represented by a digest and length so a secret in a proposed
    edit cannot become durable event data.  Other tools expose their argument
    names only, which is enough to diagnose schema/shape drift without copying
    arbitrary model input into the event store.
    """
    if not isinstance(arguments, dict):
        return {"argument_type": type(arguments).__name__}

    safe: dict[str, Any] = {
        "argument_keys": sorted(str(key)[:128] for key in arguments),
    }
    if capability in {"workspace.edit", "workspace.edit_lines"}:
        if "path" in arguments:
            safe["path"] = _safe_value(arguments["path"], 512)
        for key in ("start_line", "end_line"):
            if key in arguments:
                safe[key] = _safe_value(arguments[key], 64)
        if "content" in arguments:
            content = str(arguments["content"])
            safe["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            safe["content_length"] = len(content)
        for key in ("replacement", "line", "lines"):
            if key not in arguments:
                continue
            value = arguments[key]
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            safe[f"{key}_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            safe[f"{key}_count"] = len(value) if isinstance(value, list) else 1
    return safe


def _build_runtime_capability_registry(registry) -> CapabilityRegistry:
    """Build the runtime view from the explicit core catalog only.

    Backend ``CapabilitySpec`` values are never promoted into semantic
    definitions here.  Adapter-specific definitions must be supplied by the
    adapter when constructing its own ``CapabilityRegistry``.
    """
    return CapabilityRegistry(registry, definitions=default_capabilities())


class NativeToolDispatcher:
    """Dispatch tool calls through CapabilityRegistry → ToolContract boundary.

    All normal tool invocations MUST route through ``tool_contract.invoke()``.
    Direct ``tool.execute()`` calls are forbidden — use the contract boundary.
    """

    def __init__(
        self,
        *,
        db,
        registry,
        allowed_capabilities: set[str],
        allowed_side_effect_capabilities: set[str],
        capability_registry: CapabilityRegistry | None = None,
        tool_contract: ToolContract | None = None,
        fault_injector: Any = None,
        mutation_probe: Callable[[], Awaitable[bool]] | None = None,
        receipt_manager: SideEffectReceiptManager | None = None,
    ):
        self.db = db
        self.registry = registry
        self.allowed_capabilities = set(allowed_capabilities)
        self.allowed_side_effect_capabilities = set(allowed_side_effect_capabilities)
        self.fault_injector = fault_injector or NoOpNativeFaultInjector()
        self.mutation_probe = mutation_probe
        self.observer = ToolAwareObserver()
        self.invocations = ToolInvocationRepository(db)
        self.events = EventStore(db)
        # Runtime-level receipt authority.  It is not owned by benchmark
        # adapters; callers may inject the same service into other runtimes.
        self.receipts = receipt_manager or SideEffectReceiptManager(db, event_store=self.events)

        # P2.3: CapabilityRegistry + ToolContract integration.
        # When no explicit CapabilityRegistry is provided, use the explicit
        # core catalog. Undeclared backend tools fail closed at the contract.
        if capability_registry is None:
            capability_registry = _build_runtime_capability_registry(registry)
        self.capability_registry = capability_registry
        if tool_contract is None:
            tool_contract = ToolContract(capability_registry, registry)
        self.tool_contract = tool_contract

        # Build a default runtime context; the real platform is injected at
        # dispatch time via the ExecutionSnapshot / AgentRequest.
        self._default_runtime_context = CapabilityRuntimeContext(
            platform="windows",
            available_tools=set(registry.list_capabilities()),
        )

    def restore_observer(self, state: dict[str, Any]) -> None:
        self.observer.restore(state)

    def observer_state(self) -> dict[str, Any]:
        return self.observer.snapshot()

    def _runtime_context(self, snapshot: ExecutionSnapshot | None = None) -> CapabilityRuntimeContext:
        """Build a CapabilityRuntimeContext from current state."""
        available = set(self.registry.list_capabilities())
        return CapabilityRuntimeContext(
            platform="windows",
            available_tools=available,
        )

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas from CapabilityDefinitions (semantic contract).

        Only explicitly declared capabilities are exposed to the model.
        Runtime-fallback capabilities (source="runtime") are excluded —
        they exist for internal routing only and must not appear in
        model-facing schemas.
        """
        schemas = []
        for name in sorted(self.allowed_capabilities):
            try:
                definition = self.capability_registry.get(name)
            except KeyError:
                continue
            # Exclude runtime-fallback capabilities from model-facing schemas
            if getattr(definition, "source", None) == "runtime":
                continue
            # Check capability availability via discovery
            context = self._runtime_context()
            records = {
                r.id: r
                for r in self.capability_registry.discover(context)
                if r.id == name
            }
            record = records.get(name)
            if record is None or record.availability is not CapabilityAvailability.AVAILABLE:
                continue
            # Policy: skip tools requiring human approval or unauthorized side-effects
            try:
                concrete = self.registry.resolve(definition.preferred_tool)
                spec = concrete.capability
            except (KeyError, AttributeError):
                spec = None
            if spec is not None:
                if getattr(spec, "requires_human_approval", False):
                    continue
                if getattr(spec, "side_effect", False) and name not in self.allowed_side_effect_capabilities:
                    continue
            schemas.append({
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.input_schema or {"type": "object", "additionalProperties": False},
                },
            })
        return schemas

    @staticmethod
    def _identity(attempt_id: str, provider_call_id: str) -> str:
        return hashlib.sha256(f"{attempt_id}:{provider_call_id}".encode("utf-8")).hexdigest()

    @staticmethod
    def _side_effect_class(name: str, definition: Any | None, concrete_spec: Any | None = None) -> SideEffectClass:
        if name == "platform.delegate":
            return SideEffectClass.DELEGATION
        # Check the concrete tool's CapabilitySpec for side_effect
        if concrete_spec is not None and getattr(concrete_spec, "side_effect", False):
            return SideEffectClass.WORKSPACE_MUTATION if name.startswith("workspace.") else SideEffectClass.EXTERNAL
        return SideEffectClass.READ_ONLY

    @staticmethod
    def _effect_class(name: str, definition: Any | None, concrete_spec: Any | None = None) -> EffectClass:
        declared = getattr(definition, "effect_class", EffectClass.NONE)
        if declared is not EffectClass.NONE:
            return declared
        # Legacy concrete tools may still expose side_effect=True.  Treat
        # those as locally durable until a semantic capability opts into
        # stronger external idempotency/receipt facts.  This preserves the
        # existing mutation probe contract while remaining conservative for
        # genuinely external adapters, which must declare their class.
        if concrete_spec is not None and getattr(concrete_spec, "side_effect", False):
            return EffectClass.LOCAL_REVERSIBLE if name.startswith("workspace.") else EffectClass.LOCAL_DURABLE
        return EffectClass.NONE

    async def dispatch(
        self,
        call: ProviderToolCall,
        request: AgentRequest,
        snapshot: ExecutionSnapshot,
        *,
        execution_control: ExecutionControlToken | None = None,
    ) -> dict[str, Any]:
        if execution_control is not None:
            execution_control.check()
        invocation_id = self._identity(snapshot.attempt_id, call.id)
        existing = self.invocations.get(invocation_id)
        if existing is not None:
            return {
                "tool_call_id": call.id[:128],
                "capability": existing.capability,
                "status": existing.result_status or "RECONCILIATION_REQUIRED",
                "error_type": existing.error_type,
                "reconciliation": (existing.reconciliation or ReconciliationDecision.DO_NOT_RETRY).value,
                "safe_summary": existing.result_summary,
                "duplicate_logical_invocation": True,
            }

        # Resolve capability definition from CapabilityRegistry
        try:
            definition = self.capability_registry.get(call.name)
        except KeyError:
            definition = None

        # Resolve concrete tool for side-effect classification
        concrete_tool = None
        concrete_spec = None
        if definition is not None:
            for tool_name in (definition.preferred_tool, *definition.fallback_tools):
                try:
                    concrete_tool = self.registry.resolve(tool_name)
                    concrete_spec = concrete_tool.capability
                    break
                except KeyError:
                    continue

        invocation = ToolInvocation(
            id=invocation_id,
            task_id=snapshot.task_id,
            run_id=snapshot.run_id,
            attempt_id=snapshot.attempt_id,
            ordinal=snapshot.tool_call_count + 1,
            capability=call.name[:128],
            args_fingerprint=_args_signature(call.arguments),
            side_effect_class=self._side_effect_class(call.name, definition, concrete_spec),
        )
        self.invocations.create(invocation)
        self.events.append(
            EventType.NATIVE_TOOL_REQUESTED,
            task_id=snapshot.task_id,
            run_id=snapshot.run_id,
            attempt_id=snapshot.attempt_id,
            payload={
                "invocation_id": invocation.id,
                "capability": invocation.capability,
                "ordinal": invocation.ordinal,
                "args_sha256": invocation.args_fingerprint,
                # This is a bounded forensic projection.  Arbitrary model
                # arguments remain out of the durable event and invocation
                # models; workspace edit contents are represented by hashes.
                "arguments_sanitized": _safe_tool_arguments(
                    call.name, call.arguments
                ),
            },
        )
        self.fault_injector.hit(NativeFaultPoint.AFTER_TOOL_REQUESTED, invocation=invocation)
        if execution_control is not None:
            execution_control.check()

        # --- Policy boundary (NativeToolDispatcher owns these) ---
        if definition is None:
            return self._finish_denied(invocation, "UNKNOWN_CAPABILITY")
        if call.name not in self.allowed_capabilities:
            return self._finish_denied(invocation, "CAPABILITY_NOT_ALLOWED")
        if call.name == "platform.delegate" and len(snapshot.delegation_dependencies) >= request.budget.max_delegations:
            return self._finish_denied(invocation, "DELEGATION_BUDGET_EXHAUSTED")
        if concrete_spec is not None and concrete_spec.side_effect and call.name not in self.allowed_side_effect_capabilities:
            return self._finish_denied(invocation, "SIDE_EFFECT_NOT_ALLOWED")
        if concrete_spec is not None and concrete_spec.requires_human_approval:
            return self._finish_denied(invocation, "HUMAN_APPROVAL_REQUIRED")

        effect_class = self._effect_class(call.name, definition, concrete_spec)
        receipt = None
        if effect_class is not EffectClass.NONE:
            step_id = (
                snapshot.taskgraph_position
                or request.metadata.get("step_id")
                or f"attempt:{snapshot.attempt_id}"
            )
            target = {
                "workspace_ref": request.metadata.get("workspace_ref"),
                "path": call.arguments.get("path") if isinstance(call.arguments, dict) else None,
                "capability": call.name,
            }
            receipt = self.receipts.begin(
                operation_id=invocation.id,
                task_id=snapshot.task_id,
                run_id=snapshot.run_id,
                attempt_id=snapshot.attempt_id,
                step_id=str(step_id),
                tool_call_id=call.id[:128],
                tool_name=concrete_spec.name if concrete_spec is not None else call.name,
                effect_class=effect_class,
                target=target,
                request={"capability": call.name, "arguments": call.arguments},
                idempotency_key=request.metadata.get("idempotency_key"),
                workspace_before_digest=request.metadata.get("workspace_before_digest"),
                sanitized_metadata={"task_id": snapshot.task_id, "workspace_ref": request.metadata.get("workspace_ref")},
            )
            self.receipts.mark_dispatch_started(receipt.receipt_id)

        invocation.state = InvocationState.STARTED
        invocation.started_at = utcnow()
        self.invocations.update(invocation)
        self.events.append(EventType.NATIVE_TOOL_STARTED, task_id=snapshot.task_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id, payload={"invocation_id": invocation.id, "capability": invocation.capability})
        self.fault_injector.hit(NativeFaultPoint.AFTER_TOOL_STARTED, invocation=invocation)
        if execution_control is not None:
            execution_control.check()
        started = time.monotonic()

        # --- ToolContract boundary: ALL execution goes through contract ---
        tool_name = definition.preferred_tool
        if concrete_tool is not None:
            # Use the actual resolved tool name (may be a fallback)
            tool_name = concrete_spec.name if concrete_spec else definition.preferred_tool

        contract_request = ToolRequest(
            tool_call_id=call.id[:128],
            task_id=snapshot.task_id,
            run_id=snapshot.run_id,
            attempt_id=snapshot.attempt_id,
            capability_id=call.name,
            tool_name=tool_name,
            arguments=call.arguments,
            context=request.context,
            timeout_seconds=(
                execution_control.effective_timeout(
                    getattr(definition, "timeout_seconds", None)
                )
                if execution_control is not None
                else None
            ),
            execution_control=execution_control,
            side_effect_receipt_manager=self.receipts if receipt is not None else None,
            side_effect_receipt_id=receipt.receipt_id if receipt is not None else None,
            step_id=str(snapshot.taskgraph_position or request.metadata.get("step_id") or f"attempt:{snapshot.attempt_id}"),
            metadata=request.metadata,
        )
        runtime_context = self._runtime_context(snapshot)

        try:
            result = await await_with_control(
                self.tool_contract.invoke(contract_request, runtime_context),
                control=execution_control,
                local_ceiling=getattr(definition, "timeout_seconds", None),
                timeout_failure_type="TOOL_TIMEOUT",
                source="tool",
            )
        except ExecutionLayerTimeout as exc:
            result = ToolResult(
                status=ToolResultStatus.FAILURE,
                error_type=exc.failure_type,
                error_message=exc.reason,
            )
        except ExecutionControlError:
            # Leave a STARTED invocation durable for reconciliation; no
            # post-cancel observation may advance runtime truth.
            raise
        except Exception as exc:
            result = ToolResult(status=ToolResultStatus.FAILURE, error_type="TOOL_EXECUTION_ERROR", error_message=str(exc)[:512])

        if execution_control is not None:
            execution_control.check()

        self.fault_injector.hit(NativeFaultPoint.AFTER_TOOL_EXECUTED, invocation=invocation, result=result)
        if receipt is not None:
            if result.status is ToolResultStatus.SUCCESS:
                result_output = result.output if isinstance(result.output, dict) else None
                self.receipts.mark_committed(
                    receipt.receipt_id,
                    result=result.output,
                    workspace_before_digest=(result_output or {}).get("before_sha256"),
                    workspace_after_digest=(result_output or {}).get("after_sha256"),
                )
            else:
                # A failed response after dispatch does not prove that no
                # external effect happened. Preserve uncertainty instead of
                # allowing a blind replay.
                self.receipts.mark_unknown(receipt.receipt_id, error_class=result.error_type or "TOOL_FAILURE")
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = self.observer.decorate(
            call.name,
            call.arguments,
            result,
            safe_tool_summary(call.name, call.arguments, result),
            invocation.args_fingerprint,
        )
        output = result.output if isinstance(result.output, dict) else {"value": result.output}
        model_observation = {
            "tool_call_id": call.id[:128],
            "capability": call.name,
            "status": result.status.value,
            "error_type": result.error_type,
            "error_message": _safe_value(result.error_message, 512),
            "safe_summary": _safe_value(summary, 8_000),
            "bounded_output": _safe_value(output, 12_000),
            "duration_ms": duration_ms,
        }
        before = summary.get("before_sha256") or output.get("before_sha256")
        after = summary.get("after_sha256") or output.get("after_sha256")
        invocation.observed_mutation = bool(
            summary.get("meaningful_mutation")
            or (before and after and before != after)
            or (
                call.name in {"workspace.edit", "workspace.edit_lines"}
                and result.status is ToolResultStatus.SUCCESS
                and isinstance(output, dict)
                and any(
                    key in output
                    for key in ("checksum", "bytes_written", "lines_written")
                )
            )
            or (call.name == "platform.delegate" and result.status is ToolResultStatus.SUCCESS)
        )
        invocation.state = InvocationState.FINISHED
        invocation.result_status = result.status.value
        invocation.error_type = result.error_type
        invocation.result_summary = _safe_value(summary, 8_000)
        invocation.finished_at = utcnow()
        invocation.duration_ms = duration_ms
        self.invocations.update(invocation)
        self.events.append(
            EventType.NATIVE_TOOL_OBSERVED,
            task_id=snapshot.task_id,
            run_id=snapshot.run_id,
            attempt_id=snapshot.attempt_id,
            payload={
                "invocation_id": invocation.id,
                "capability": invocation.capability,
                "ordinal": invocation.ordinal,
                "status": invocation.result_status,
                "error_type": invocation.error_type,
                "duration_ms": duration_ms,
                "observed_mutation": invocation.observed_mutation,
                "summary": invocation.result_summary,
                "bounded_output": _safe_value(output, 12_000),
            },
        )
        if receipt is not None:
            current_receipt = self.receipts.receipts.get(receipt.receipt_id)
            if current_receipt is not None and current_receipt.status is ReceiptStatus.COMMITTED:
                current_receipt = self.receipts.mark_observed(receipt.receipt_id, result=result.output)
            if current_receipt is not None:
                model_observation["side_effect_receipt"] = {
                    "receipt_id": current_receipt.receipt_id,
                    "effect_class": current_receipt.effect_class.value,
                    "status": current_receipt.status.value,
                }
        self.fault_injector.hit(NativeFaultPoint.AFTER_TOOL_OBSERVED, invocation=invocation)
        return model_observation

    def _finish_denied(self, invocation: ToolInvocation, error_type: str) -> dict[str, Any]:
        invocation.state = InvocationState.FINISHED
        invocation.result_status = ToolResultStatus.FAILURE.value
        invocation.error_type = error_type
        invocation.result_summary = {"capability": invocation.capability, "status": "FAILURE", "error_type": error_type}
        invocation.finished_at = utcnow()
        invocation.duration_ms = 0
        self.invocations.update(invocation)
        self.events.append(EventType.NATIVE_TOOL_OBSERVED, task_id=invocation.task_id, run_id=invocation.run_id, attempt_id=invocation.attempt_id, payload={"invocation_id": invocation.id, **invocation.result_summary})
        return {"tool_call_id": invocation.id, "safe_summary": invocation.result_summary, **invocation.result_summary}

    async def reconcile_unfinished(self, attempt_id: str) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        mutation_present: bool | None = None
        for invocation in self.invocations.unfinished_for_attempt(attempt_id):
            receipt = self.receipts.receipts.get_by_operation(invocation.id)
            if invocation.state is InvocationState.REQUESTED:
                decision = ReconciliationDecision.SAFE_TO_RETRY
            elif invocation.side_effect_class is SideEffectClass.READ_ONLY:
                decision = ReconciliationDecision.SAFE_TO_RETRY
            elif receipt is not None and receipt.status in {ReceiptStatus.COMMITTED, ReceiptStatus.OBSERVED}:
                decision = ReconciliationDecision.DO_NOT_RETRY
            elif receipt is not None and receipt.status is ReceiptStatus.COMMIT_STATE_UNKNOWN:
                decision = ReconciliationDecision.UNKNOWN
            else:
                if mutation_present is None and self.mutation_probe is not None:
                    try:
                        mutation_present = bool(await self.mutation_probe())
                    except Exception:
                        mutation_present = None
                decision = (
                    ReconciliationDecision.DO_NOT_RETRY
                    if mutation_present is True or invocation.observed_mutation
                    else ReconciliationDecision.RECONCILE_FIRST
                )
                if receipt is not None and invocation.side_effect_class is not SideEffectClass.READ_ONLY:
                    receipt = self.receipts.reconcile(receipt.receipt_id, effect_present=mutation_present)
                    if receipt.status in {ReceiptStatus.COMMITTED, ReceiptStatus.OBSERVED}:
                        decision = ReconciliationDecision.DO_NOT_RETRY
                    elif receipt.status is ReceiptStatus.COMMIT_STATE_UNKNOWN:
                        decision = ReconciliationDecision.UNKNOWN
            invocation.state = InvocationState.RECONCILED
            invocation.reconciliation = decision
            invocation.finished_at = utcnow()
            invocation.result_status = "INTERRUPTED"
            invocation.error_type = "PROCESS_INTERRUPTED"
            invocation.result_summary = {
                "capability": invocation.capability,
                "status": "INTERRUPTED",
                "reconciliation": decision.value,
                "args_sha256": invocation.args_fingerprint,
            }
            self.invocations.update(invocation)
            observation = {
                "capability": invocation.capability,
                "status": "INTERRUPTED",
                "reconciliation": decision.value,
                "retry_was_automatic": False,
                "safe_summary": invocation.result_summary,
            }
            observations.append(observation)
            self.events.append(EventType.NATIVE_TOOL_RECONCILED, task_id=invocation.task_id, run_id=invocation.run_id, attempt_id=invocation.attempt_id, payload={"invocation_id": invocation.id, **invocation.result_summary})
        return observations
