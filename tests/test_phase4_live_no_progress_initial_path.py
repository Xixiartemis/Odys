"""Deterministic proof that the V2 controller reaches the initial native loop."""

from __future__ import annotations

import asyncio
import json

from scripts.phase4_live_no_progress_final import (
    _build_runner,
    _build_specs,
    _task,
)
from scripts.phase4_live_no_progress_parity import _DeterministicProvider, _tool_call
from evals.reliability.run_phase4 import ProtocolSnapshot


class _RepeatedDeniedEditProvider(_DeterministicProvider):
    """Provider double that would spend all 20 initial calls on one no-op."""

    async def generate(self, *, context, tools, timeout_seconds):
        response = await super().generate(
            context=context,
            tools=tools,
            timeout_seconds=timeout_seconds,
        )
        if self.policy.phase != "initial":
            return response
        ordinal = self._phase_calls["initial"]
        return {
            "id": response["id"],
            "model": response["model"],
            "choices": [
                {
                    "message": {
                        "content": "Retrying the same denied alternate effect.",
                        "tool_calls": [
                            _tool_call(f"initial-denied-edit-{ordinal}")
                        ],
                    }
                }
            ],
            "usage": response["usage"],
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
    provider = _RepeatedDeniedEditProvider(policies[spec.run_id])

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


def test_legacy_initial_loop_does_not_use_no_progress_for_control(tmp_path):
    snapshot = ProtocolSnapshot.load()
    task = _task(snapshot)
    specs, policies = _build_specs(snapshot, task=task)
    spec = next(
        item for item in specs if item.arm_id == "baseline" and item.repeat_index == 1
    )
    output = tmp_path / "initial-baseline"
    provider = _RepeatedDeniedEditProvider(policies[spec.run_id])

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
