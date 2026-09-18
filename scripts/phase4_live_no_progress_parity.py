"""Provider-free qualification of the real Phase 4 no-progress path.

Experiment 02B proved the recovery control-plane primitives in an isolated
simulation.  This module deliberately uses the production path instead:

    Phase4Runner -> P45BenchmarkExecutor -> RealLLM*RuntimeFactory
    -> NativeAgentKernel/NativeToolDispatcher -> validator/recovery

Only the provider transport is replaced with a deterministic in-process
provider.  The provider emits the same OpenAI-compatible response shape as
the real adapter, but never performs network I/O.

The effect policy is an experiment-local authorization boundary.  It proves
that an alternate/verified mutation is denied during initial and local repair
phases and is only permitted after the canonical MacroReplanService accepts
the changed strategy.  It does not change frozen Phase 4 inputs.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT, REPO_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from evals.reliability.effect_policy import PhaseEffectPolicy
from evals.reliability.fixture_packages.registry import FixtureRegistry
from evals.reliability.p45_executor import P45BenchmarkExecutor
from evals.reliability.p46_provider import CHEAP_MODEL, FROZEN_PROVIDER
from evals.reliability.run_phase4 import (
    ConfigLoader,
    FixtureManager,
    Phase4Runner,
    ProtocolSnapshot,
    RunSpec,
)
from lhas.native.models import RuntimeTarget
from lhas.native.transport import canonical_transport_identity


EXPERIMENT_ID = "phase4-live-no-progress-parity-02d"
TASK_ID = "P4E02D-CWR-NP-01"
FAULT_ID = "FAIL_TOOL_ON_CALL_1"
REPEATS = 1
ARMS = (("baseline", "LEGACY_BOUNDED"), ("v2", "NO_PROGRESS_AWARE"))
EXPECTED_PROTOCOL_HASH = "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
DEFAULT_OUTPUT = REPO_ROOT / "results" / EXPERIMENT_ID


class QualificationInvalidRunError(RuntimeError):
    """Fail closed with the real invalid-run evidence from the qualification."""

    def __init__(self, counts: dict[str, Any], invalid_records: list[dict[str, Any]]) -> None:
        self.counts = dict(counts)
        self.invalid_records = list(invalid_records)
        details = "; ".join(
            f"{item.get('run_id', '<missing-run-id>')}"
            f":{item.get('failure_type') or item.get('invalid_reason') or item.get('error_type') or '<unknown>'}"
            for item in self.invalid_records
        )
        if not details:
            details = "<invalid.jsonl empty or unavailable>"
        super().__init__(
            "PHASE4_QUALIFICATION_INVALID_RUNS: "
            f"invalid={self.counts.get('invalid', 0)}; {details}"
        )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _ParityFixture:
    task_id = TASK_ID

    @staticmethod
    def _path(workspace: Path) -> Path:
        return workspace / "state.json"

    def setup(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        self._path(workspace).write_text(
            '{"route":"local","state_status":"blocked"}\n',
            encoding="utf-8",
        )

    def inject_fault(self, workspace: Path, fault_id: str) -> None:
        state = json.loads(self._path(workspace).read_text(encoding="utf-8"))
        state["fault_armed"] = fault_id
        self._path(workspace).write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def observe(self, workspace: Path) -> dict[str, Any]:
        return json.loads(self._path(workspace).read_text(encoding="utf-8"))

    def reset(self, workspace: Path) -> None:
        self._path(workspace).unlink(missing_ok=True)


def _fixture_registry() -> FixtureRegistry:
    registry = FixtureRegistry()
    registry._registry[TASK_ID] = _ParityFixture  # type: ignore[attr-defined]
    return registry


def _task(snapshot: ProtocolSnapshot, *, experiment_id: str = EXPERIMENT_ID) -> dict[str, Any]:
    fixture_id = "fixture-replan-v1"
    fixture = snapshot.fixtures["fixtures"][fixture_id]
    return {
        "benchmark_version": experiment_id,
        "task_id": TASK_ID,
        "family": "COMPLEX_WORKFLOW_REPLAN",
        "title": "Provider-free live-path no-progress parity",
        "objective": "Change the authoritative route through a bounded recovery path.",
        "fixture_id": fixture_id,
        "fixture_version": str(fixture["version"]),
        "fixture_hash_source": f"fixtures/catalog.json#{fixture_id}",
        "initial_state": "local blocked route",
        "required_capabilities": ["workspace.edit", "workspace.edit_lines"],
        "acceptance_criteria": [
            "observable state has route=alternate",
            "observable state has state_status=verified",
        ],
        "validator_id": "external-observable-v1",
        "fault_injection": FAULT_ID,
        "fault_timing": "first native tool dispatch",
        "max_turns": 20,
        "max_model_calls": 20,
        "timeout_seconds": 120,
        "repair_max_no_progress": 3,
        "repair_max_repeated_action": 16,
        "repair_max_repeated_state": 2,
        "side_effect_policy": "phase-gated alternate effect",
        "expected_observable_effects": {
            "route": "alternate",
            "state_status": "verified",
        },
        "measurement_tags": ["no_progress", "macro_replan", "parity"],
        "experiment_initial_plan_steps": ["workspace.edit"],
        "experiment_replan_plan_steps": ["workspace.edit_lines"],
        "experiment_step_inputs": {
            "workspace.edit": {
                "path": "state.json",
                "content": '{"route":"local","state_status":"blocked"}\n',
            },
            "workspace.edit_lines": {
                "path": "state.json",
                "old_string": '"route":"local","state_status":"blocked"',
                "new_string": '"route":"alternate","state_status":"verified"',
            },
        },
    }


def _tool_call(call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "workspace.edit_lines",
            "arguments": json.dumps(
                {
                    "path": "state.json",
                    "old_string": '"route":"local","state_status":"blocked"',
                    "new_string": '"route":"alternate","state_status":"verified"',
                },
            ),
        },
    }


def _tool_call_with_arguments(
    call_id: str, capability: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": capability,
            "arguments": json.dumps(arguments),
        },
    }


class _DeterministicProvider:
    """Offline provider double with real-adapter response semantics."""

    name = "scripted-provider-02d"

    def __init__(self, policy: PhaseEffectPolicy):
        self.policy = policy
        self.model = CHEAP_MODEL
        self._transport = canonical_transport_identity("https://phase4-02d.invalid/v1")
        self._runtime_target = RuntimeTarget(
            provider_id="scripted-provider-02d",
            model_id=CHEAP_MODEL,
            endpoint_identity=self._transport.endpoint_identity,
            endpoint_host=self._transport.endpoint_host,
            endpoint_fingerprint=self._transport.endpoint_fingerprint,
            credential_route_id="none",
            route_type="scripted",
        )
        self.call_records: list[dict[str, Any]] = []
        self._context: dict[str, str] = {}
        self._phase_calls: dict[str, int] = {}
        self._execution_control = None
        self._run_budget = None
        self._post_replan_edit_sent = False

    @property
    def runtime_target(self) -> RuntimeTarget:
        return self._runtime_target

    @property
    def transport_identity(self):
        return self._transport

    def bind_execution_context(self, *, run_id: str, task_id: str, attempt_id: str, phase: str) -> None:
        self._context = {
            "run_id": str(run_id),
            "task_id": str(task_id),
            "attempt_id": str(attempt_id),
            "phase": str(phase),
        }
        if phase == "initial":
            self._phase_calls = {}
            self._post_replan_edit_sent = False
        self.policy.bind_provider_phase(phase)

    def bind_execution_control(self, control: Any) -> None:
        self._execution_control = control

    def bind_run_budget(self, ledger: Any) -> None:
        self._run_budget = ledger

    async def generate(self, *, context: Any, tools: list[dict[str, Any]], timeout_seconds: float) -> dict[str, Any]:
        del tools, timeout_seconds
        if self._execution_control is not None:
            self._execution_control.check()
        phase = self.policy.phase
        ordinal = self._phase_calls.get(phase, 0) + 1
        self._phase_calls[phase] = ordinal
        record = {
            "call_index": len(self.call_records) + 1,
            "provider_call": True,
            "provider": "scripted-provider-02d",
            "model": CHEAP_MODEL,
            "phase": phase,
            "run_id": self._context.get("run_id"),
            "attempt_id": self._context.get("attempt_id"),
            "status": "SUCCESS",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }
        self.call_records.append(record)

        # Keep the qualification provider compatible with the generic
        # active-step contract.  Local repair is still an alternate no-op,
        # but it must use the capability selected by the current PlanStep;
        # post-replan uses the exact durable inputs projected to the model.
        sections = getattr(context, "sections", {})
        active_contract = sections.get("active_step_contract") if isinstance(sections, dict) else None
        if isinstance(active_contract, dict) and active_contract.get("capability"):
            capability = str(active_contract["capability"])
            if capability == "workspace.edit":
                arguments = {
                    "path": "state.json",
                    "content": '{"route":"alternate","state_status":"verified"}\n',
                }
            else:
                arguments = dict(active_contract.get("inputs", {}))
            return {
                "id": f"phase4-02d-{len(self.call_records)}",
                "model": CHEAP_MODEL,
                "choices": [{"message": {"content": "Executing active plan step.", "tool_calls": [_tool_call_with_arguments(f"active-step-{len(self.call_records)}", capability, arguments)]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

        if phase == "post_replan" and not self._post_replan_edit_sent:
            self._post_replan_edit_sent = True
            return {
                "id": f"phase4-02d-{len(self.call_records)}",
                "model": CHEAP_MODEL,
                "choices": [{"message": {"content": "Applying alternate strategy.", "tool_calls": [_tool_call("post-replan-edit")]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        if phase == "post_replan":
            return {
                "id": f"phase4-02d-{len(self.call_records)}",
                "model": CHEAP_MODEL,
                "choices": [{"message": {"content": "Completed.", "tool_calls": []}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        if phase == "initial" and ordinal == 1:
            return {
                "id": f"phase4-02d-{len(self.call_records)}",
                "model": CHEAP_MODEL,
                "choices": [{"message": {"content": "Trying the alternate effect.", "tool_calls": [_tool_call("initial-alternate-effect")]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        if phase == "initial":
            return {
                "id": f"phase4-02d-{len(self.call_records)}",
                "model": CHEAP_MODEL,
                "choices": [{"message": {"content": "Completed.", "tool_calls": []}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        return {
            "id": f"phase4-02d-{len(self.call_records)}",
            "model": CHEAP_MODEL,
            "choices": [{"message": {"content": "Retrying the same alternate effect.", "tool_calls": [_tool_call(f"local-alternate-effect-{ordinal}")]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


def _build_runner(
    snapshot: ProtocolSnapshot,
    output: Path,
    policy: PhaseEffectPolicy,
    provider: _DeterministicProvider,
    *,
    experiment_id: str = EXPERIMENT_ID,
) -> tuple[Phase4Runner, tuple[RunSpec, ...]]:
    task = _task(snapshot, experiment_id=experiment_id)
    specs: list[RunSpec] = []
    loader = ConfigLoader(snapshot)
    for arm, escalation_policy in ARMS:
        config = dict(loader.load("odys_p3"))
        config["_experiment_macro_replan_enabled"] = True
        config["escalation_trigger_policy"] = escalation_policy
        config["experiment_arm"] = arm
        config["_phase_effect_policy"] = policy
        specs.append(RunSpec(task=task, config=config, repeat_index=1, arm_id=arm))
    executor = P45BenchmarkExecutor(
        fixture_registry=_fixture_registry(),
        factory_type="real",
        provider=provider,
        expected_model=CHEAP_MODEL,
        experiment_macro_replan_enabled=True,
    )
    runner = Phase4Runner(
        snapshot,
        output_dir=output,
        executor=executor,
        fixture_manager=FixtureManager(snapshot),
        model=CHEAP_MODEL,
        provider=FROZEN_PROVIDER,
        benchmark_version=experiment_id,
        repo_root=REPO_ROOT,
        trace_path=output / "traces.jsonl",
        require_trace=True,
    )
    return runner, tuple(specs)


def _event_types(record: dict[str, Any]) -> list[str]:
    return [str(item.get("event_type")) for item in record.get("execution_trace", [])]


def _script_uses_local_monkeypatch() -> bool:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_live_path_instrumentation":
                return True
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            owner = target.value
            if (
                isinstance(owner, ast.Name)
                and (owner.id, target.attr)
                in {
                    ("benchmark_tools_registry", "create_benchmark_tool_registry"),
                    ("MacroReplanService", "consume"),
                    ("RecoveryController", "emit_signal"),
                }
            ):
                return True
    return False


async def qualify_async(
    output: Path,
    *,
    experiment_id: str = EXPERIMENT_ID,
) -> dict[str, Any]:
    snapshot = ProtocolSnapshot.load()
    if snapshot.protocol_hash != EXPECTED_PROTOCOL_HASH:
        raise RuntimeError("FROZEN_PROTOCOL_HASH_CHANGED")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"OUTPUT_COLLISION:{output}")
    output.mkdir(parents=True, exist_ok=True)
    policy = PhaseEffectPolicy()
    provider = _DeterministicProvider(policy)
    runner, specs = _build_runner(
        snapshot,
        output,
        policy,
        provider,
        experiment_id=experiment_id,
    )
    counts = await runner.run(specs)

    if counts.get("invalid", 0):
        raise QualificationInvalidRunError(
            counts,
            _load_jsonl(output / "invalid.jsonl"),
        )
    raw_records = _load_jsonl(output / "raw.jsonl")
    trace_records = _load_jsonl(output / "traces.jsonl")
    by_run = {item["benchmark_run_id"]: item for item in raw_records}
    trace_by_run = {item["run_id"]: item for item in trace_records}
    arms: dict[str, Any] = {}
    for arm, _policy in ARMS:
        record = next(item for item in raw_records if item.get("benchmark_run_id", "").endswith(f"::{arm}::repeat-1"))
        trace = trace_by_run[record["benchmark_run_id"]]
        events = _event_types(trace)
        validation = record["runtime_environment"].get("validation", {})
        recovery = record["runtime_environment"].get("recovery", {})
        run_id = record["benchmark_run_id"]
        controller_signals = [
            item
            for item in policy.replan_signal_reasons
            if item["run_id"] == run_id
        ]
        controller_detections = [
            item
            for item in policy.recovery_detections
            if item["run_id"] == run_id
        ]
        replan_results = [
            item
            for item in policy.replan_results
            if run_id in item.get("plan_id", "")
        ]
        arms[arm] = {
            "run_id": run_id,
            "initial_validator": validation.get("acceptance_status"),
            "final_validator": validation.get("final_acceptance_status"),
            "recovery_attempted": recovery.get("recovery_attempted"),
            "recovery_success": recovery.get("recovery_success"),
            "repair_attempts": recovery.get("repair_attempts"),
            "recovery_detections": controller_detections,
            "controller_signals": controller_signals,
            "replan_results": replan_results,
            "no_progress_observed": any(
                item["reason"] == "REPAIR_NO_PROGRESS"
                for item in controller_detections
            ),
            "no_progress_used_for_control": any(
                item["reason"] == "REPAIR_NO_PROGRESS"
                and item["escalation_policy"] == "NO_PROGRESS_AWARE"
                for item in controller_signals
            ),
            "macro_replan_executed": any(
                item["accepted"] for item in replan_results
            ),
            "event_types": events,
            "fault_trigger_index": next(
                (event.get("metadata", {}).get("trigger_index") for event in trace["execution_trace"] if event.get("event_type") == "FAULT_TRIGGERED"),
                None,
            ),
        }
    baseline = arms["baseline"]
    v2 = arms["v2"]
    report = {
        "experiment_id": experiment_id,
        "provider_executed": False,
        "scripted_provider": True,
        "same_live_execution_path": True,
        "shared_effect_policy_implementation": policy.policy_id == "phase4-effect-policy-v1",
        "script_local_monkeypatch": _script_uses_local_monkeypatch(),
        "real_runner_uses_same_policy": policy.registry_install_count == len(ARMS),
        "runtime_tool_policy_id": policy.policy_id,
        "registry_policy_install_count": policy.registry_install_count,
        "protocol_hash": snapshot.protocol_hash,
        "planned_runs": len(specs),
        "valid_runs": counts["valid"],
        "invalid_runs": counts["invalid"],
        "initial_alternate_effect_blocked": any(item["alternate_effect"] and item["phase"] == "initial" for item in policy.denied),
        "local_repair_alternate_effect_blocked": any(item["alternate_effect"] and item["phase"] == "local_repair" for item in policy.denied),
        "post_replan_alternate_effect_allowed": any(item["alternate_effect"] and item["phase"] == "post_replan" for item in policy.allowed),
        "both_arms_enter_recovery": baseline["recovery_attempted"] and v2["recovery_attempted"],
        "fault_trigger_index": baseline["fault_trigger_index"] == 1 and v2["fault_trigger_index"] == 1,
        "baseline": baseline,
        "v2": v2,
        "phase_effect_denials": policy.denied,
        "phase_effect_allows": policy.allowed,
        "replan_reservations": policy.replan_reservations,
        "replan_results": policy.replan_results,
        "replan_signal_reasons": policy.replan_signal_reasons,
        "recovery_detections": policy.recovery_detections,
        "acceptance": {
            "initial_validator_rejected": baseline["initial_validator"] == "REJECTED" and v2["initial_validator"] == "REJECTED",
            "baseline_final_validator": baseline["final_validator"],
            "v2_final_validator": v2["final_validator"],
            "replan_executed": any(event == "REPLAN_ACCEPTED" for arm in arms.values() for event in arm["event_types"]),
        },
    }
    (output / "qualification.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def qualify(
    output: Path | None = None,
    *,
    experiment_id: str = EXPERIMENT_ID,
) -> dict[str, Any]:
    if output is None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="phase4-02d-") as directory:
            return asyncio.run(
                qualify_async(Path(directory), experiment_id=experiment_id)
            )
    return asyncio.run(qualify_async(output, experiment_id=experiment_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualify", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if not args.qualify:
        parser.error("only --qualify is supported; this qualification never calls a real provider")
    report = qualify(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print("PHASE4_02D_QUALIFICATION_COMPLETE")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
