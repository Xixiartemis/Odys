"""Deterministic proof that the V2 controller reaches the initial native loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from scripts.phase4_live_no_progress_final import (
    _build_runner,
    _build_specs,
    _task,
)
from scripts.phase4_live_no_progress_parity import _DeterministicProvider
from evals.reliability.run_phase4 import ProtocolSnapshot


def _visible_active_contract(context):
    sections = getattr(context, "sections", {})
    contract = sections.get("active_step_contract") if isinstance(sections, Mapping) else None
    return dict(contract) if isinstance(contract, Mapping) else None


def _tool_call_for_contract(call_id, capability, inputs):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": capability,
            "arguments": json.dumps(dict(inputs)),
        },
    }


class _ContractAwareProvider(_DeterministicProvider):
    """Provider double whose action selection is driven only by model context.

    It deliberately does not use ``PhaseEffectPolicy.phase`` to choose the
    correct post-replan action.  Without an active durable PlanStep contract
    it emits a denied ``workspace.edit`` probe; with the exact contract it
    emits the contract's capability and inputs through the normal tool path.
    """

    def __init__(self, policy):
        super().__init__(policy)
        self.visible_contracts = []
        self.visible_tool_sets = []

    async def generate(self, *, context, tools, timeout_seconds):
        del timeout_seconds
        if self._execution_control is not None:
            self._execution_control.check()
        phase = self.policy.phase
        self._phase_calls[phase] = self._phase_calls.get(phase, 0) + 1
        call_index = len(self.call_records) + 1
        self.call_records.append(
            {
                "call_index": call_index,
                "provider_call": True,
                "provider": self.name,
                "model": self.model,
                "phase": phase,
                "run_id": self._context.get("run_id"),
                "attempt_id": self._context.get("attempt_id"),
                "status": "SUCCESS",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            }
        )
        contract = _visible_active_contract(context)
        self.visible_contracts.append(contract)
        self.visible_tool_sets.append(
            {item.get("function", {}).get("name") for item in tools}
        )
        if contract is not None:
            capability = contract.get("capability")
            inputs = contract.get("inputs")
            if isinstance(capability, str) and isinstance(inputs, Mapping):
                return {
                    "id": f"contract-aware-{call_index}",
                    "model": self.model,
                    "choices": [
                        {
                            "message": {
                                "content": "Executing the accepted plan step.",
                                "tool_calls": [
                                    _tool_call_for_contract(
                                        f"contract-step-{len(self.call_records)}",
                                        capability,
                                        inputs,
                                    )
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }

        # No accepted active-step contract: intentionally use a different
        # capability so a missing projection cannot silently perform the
        # post-replan edit.
        return {
            "id": f"contract-aware-{call_index}",
            "model": self.model,
            "choices": [
                {
                    "message": {
                        "content": "Retrying the same denied alternate effect.",
                        "tool_calls": [
                            _tool_call_for_contract(
                                f"uncontracted-edit-{len(self.call_records)}",
                                "workspace.edit",
                                {
                                    "path": "state.json",
                                    "content": '{"route":"alternate","state_status":"verified"}\n',
                                },
                            )
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


def _records(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_v2_initial_no_progress_stops_before_root_budget_and_emits_signal(tmp_path):
    snapshot = ProtocolSnapshot.load()
    task = _task(snapshot)
    specs, policies = _build_specs(snapshot, task=task)
    spec = next(item for item in specs if item.arm_id == "v2" and item.repeat_index == 1)
    output = tmp_path / "initial-v2"
    provider = _ContractAwareProvider(policies[spec.run_id])

    counts = asyncio.run(
        _build_runner(snapshot, output, provider=provider).run((spec,))
    )

    assert counts["invalid"] == 0
    raw = _records(output / "raw.jsonl")
    assert len(raw) == 1
    record = raw[0]
    accounting = record["runtime_environment"]["execution_accounting"]
    assert accounting["provider_calls"] < task["max_model_calls"]
    assert accounting["provider_calls"] > 0
    assert record["runtime_environment"]["runtime_source"] == "odys_factory"
    assert record["runtime_environment"]["recovery"]["repair_scope"] == "MACRO_REPLAN"
    assert record["runtime_environment"]["recovery"]["recovery_success"] is True
    assert record["verified_completion"] is True
    assert (
        record["runtime_environment"]["validation"]["final_acceptance_status"]
        == "ACCEPTED"
    )
    assert (
        record["runtime_environment"]["state_evidence"][
            "state_changed_after_repair"
        ]
        is True
    )
    assert any(item.get("accepted") is True for item in policies[spec.run_id].replan_results)
    assert any(
        item.get("phase") == "post_replan" and item.get("allowed") is True
        for item in policies[spec.run_id].allowed
    )
    assert any(
        item.get("reason") == "REPAIR_NO_PROGRESS"
        for item in policies[spec.run_id].recovery_detections
    )
    assert any(
        item.get("reason") == "REPAIR_NO_PROGRESS"
        for item in policies[spec.run_id].replan_signal_reasons
    )
    task_inputs = task["experiment_step_inputs"]["workspace.edit_lines"]
    contracts = [item for item in provider.visible_contracts if item is not None]
    assert contracts
    assert any(
        item.get("capability") == "workspace.edit_lines"
        and item.get("inputs") == task_inputs
        for item in contracts
    )
    assert any(
        tools == {"workspace.edit_lines"}
        for contract, tools in zip(provider.visible_contracts, provider.visible_tool_sets)
        if isinstance(contract, Mapping)
        and contract.get("capability") == "workspace.edit_lines"
    ), provider.visible_tool_sets


def test_legacy_initial_loop_does_not_use_no_progress_for_control(tmp_path):
    snapshot = ProtocolSnapshot.load()
    task = _task(snapshot)
    specs, policies = _build_specs(snapshot, task=task)
    spec = next(
        item for item in specs if item.arm_id == "baseline" and item.repeat_index == 1
    )
    output = tmp_path / "initial-baseline"
    provider = _ContractAwareProvider(policies[spec.run_id])

    asyncio.run(_build_runner(snapshot, output, provider=provider).run((spec,)))

    # LEGACY may retain bounded detection evidence once recovery begins; the
    # contract under test is that it does not turn that evidence into an
    # initial-path control signal or early stop.
    assert any(
        item.get("reason") == "REPAIR_NO_PROGRESS"
        for item in policies[spec.run_id].replan_signal_reasons
    ) is False
    accounting = _records(output / "raw.jsonl")[0]["runtime_environment"][
        "execution_accounting"
    ]
    initial_calls = [
        item
        for item in accounting["provider_call_records"]
        if item.get("phase") == "initial"
    ]
    assert len(initial_calls) == task["max_model_calls"]
