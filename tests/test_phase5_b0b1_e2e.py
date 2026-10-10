"""Provider-free B0/B1 experiment E2E tests (T1–T14).

These tests prove that the FORMAL experiment entry
(``RealPilotRunner.run_pilot`` with a ``phase5-b0b1-experiment-001``
manifest) correctly dispatches, executes, records and attributes both
comparison arms.  Everything runs on fake clients / scripted model
drivers / deterministic stubs; real network access is blocked and the
real provider request count is asserted to be zero (T14).

Dev-set task only (C1_task_036_P4).  The frozen ToolMaze dataset, Judge
and Metrics are never modified.  Fake-provider outcomes are NEVER
reported as real model gains.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from lhas.phase5.agent_core import Phase5AgentCore
from lhas.phase5.control_arms import ControlArm, ExperimentPairValidator
from lhas.phase5.live_driver import LiveModelDriver
from lhas.phase5.model_driver import ModelAction, ModelToolCall
from lhas.phase5.recovery_route import (
    ROUTE_MODE_BASELINE,
    ROUTE_MODE_V1,
    RecoveryRoute,
    RecoveryRouteDecider,
    RouteDecisionInputs,
    StateChange,
)
from lhas.phase5.real_pilot_runner import (
    B0B1_MANIFEST_SCHEMA_VERSION,
    RealPilotRunner,
)
from lhas.phase5.trial_executor import execute_trial
from lhas.phase5.types import BudgetConfig, ComparisonArm, TrialIdentity

from scripts.phase5_route_experiment_manifest import (
    build_formal_b0b1_manifest,
    manifest_hash,
)

ROOT = Path(__file__).resolve().parents[1]
TOOLMAZE_ROOT = ROOT / "experiments" / "phase5" / "benchmarks" / "toolmaze"
TASK_PATH = TOOLMAZE_ROOT / "data" / "perturbed_tasks" / "c1" / "C1_task_036_P4.json"
DEV_TASK_ID = "C1_task_036_P4"


@pytest.fixture(autouse=True)
def _require_frozen_task():
    """The frozen ToolMaze data must be hydrated (CI runs the hydrator)."""
    if not TASK_PATH.exists():
        pytest.skip("frozen ToolMaze task data not available")


# ══════════════════════════════════════════════════════════════════════
# Fake provider client (counts every request; never touches the network)
# ══════════════════════════════════════════════════════════════════════

class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(responses)
        self.base_url = "https://token-plan-cn.xiaomimimo.com/v1"


def _response(tool_calls=None, *, content="done"):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["reasoning_content"] = "batch reasoning"
        message["tool_calls"] = tool_calls
    return {
        "model": "mimo-v2.5",
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


LIVE_DRIVER_KWARGS = dict(
    provider="mimo",
    model_id="mimo-v2.5",
    base_url="https://token-plan-cn.xiaomimimo.com/v1",
    api_key="dummy-never-sent",
    thinking_enabled=True,
    effective_temperature=1.0,
    effective_top_p=0.95,
    sampling_parameters_sent=False,
    max_completion_tokens=4096,
    sdk_max_retries=0,
    supports_tool_choice=False,
    supports_parallel_tool_calls=False,
    request_parallel_tool_calls_hint=False,
    provider_may_return_multiple_tool_calls=True,
)

# Scripted per-trial provider responses shared by run_pilot scenarios:
# one real success tool call (get_employee_id returns an explicit
# "status": "success" — public positive evidence the frozen completion
# validator can ACCEPT), then a final answer.  2 provider requests per
# trial, zero faults, zero recovery triggers.
def _success_script():
    return [
        _response([
            _call("provider-1", "get_employee_id", {"name": "alice"}),
        ]),
        _response(None, content="done"),
    ]


ALL_FAKE_CLIENTS: list[_FakeClient] = []


def _driver_factory(script_fn):
    def factory():
        fake = _FakeClient(script_fn())
        ALL_FAKE_CLIENTS.append(fake)
        return LiveModelDriver(client_factory=lambda **_: fake, **LIVE_DRIVER_KWARGS)
    return factory


# ══════════════════════════════════════════════════════════════════════
# Formal manifest fixtures (create-only, tmp dir)
# ══════════════════════════════════════════════════════════════════════

def _dev_pair():
    from lhas.phase5.toolmaze_adapter import ToolMazeAdapter

    adapter = ToolMazeAdapter()
    raw = adapter._find_task(DEV_TASK_ID)
    assert raw is not None
    return {
        "task_id": DEV_TASK_ID,
        "base_task_id": "C1_task_036",
        "complexity": "C1",
        "perturbation_mode": "P4",
        "file_path": str(raw.get("_source_file", "")),
    }


def _make_manifest(**kwargs) -> dict:
    kwargs.setdefault("experiment_id", "phase5-b0b1-e2e")
    kwargs.setdefault("pairs", [_dev_pair()])
    kwargs.setdefault("repetitions", 1)
    kwargs.setdefault("seed", 42)
    return build_formal_b0b1_manifest(**kwargs)


def _persist(tmp_path: Path, manifest: dict, *, rehash: bool = True) -> Path:
    if rehash:
        manifest.pop("manifest_hash", None)
        manifest["manifest_hash"] = manifest_hash(manifest)
    path = tmp_path / f"{manifest['experiment_id']}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return path


def _runner(tmp_path: Path, manifest_path: Path, **kwargs) -> RealPilotRunner:
    return RealPilotRunner(
        model_driver_factory=kwargs.pop("factory", _driver_factory(_success_script)),
        manifest_path=manifest_path,
        output_dir=kwargs.pop("output_dir", tmp_path / "out"),
        **kwargs,
    )


# ══════════════════════════════════════════════════════════════════════
# Identity unit rules (P0-2 fix)
# ══════════════════════════════════════════════════════════════════════

class TestTrialIdentity:
    def test_legacy_identity_formula_unchanged(self):
        ident = TrialIdentity(
            experiment_id="exp", task_id="T1",
            recovery_strategy_arm=ControlArm.A3_ODYS_FULL,
        )
        assert ident.trial_id == "exp_T1_A3_ODYS_FULL"
        assert ident.run_id == "trial-exp_T1_A3_ODYS_FULL"

    def test_b0_b1_identities_never_collide(self):
        ids = {
            TrialIdentity(
                experiment_id="exp", task_id="T1",
                recovery_strategy_arm=ControlArm.A3_ODYS_FULL,
                comparison_arm_id=arm.value, route_mode=arm.route_mode,
                repetition_index=rep,
            ).trial_id
            for arm in ComparisonArm for rep in (1, 2)
        }
        assert len(ids) == 4

    def test_comparison_arm_route_mode_consistency_enforced(self):
        with pytest.raises(ValueError):
            TrialIdentity(
                experiment_id="exp", task_id="T1",
                recovery_strategy_arm=ControlArm.A3_ODYS_FULL,
                comparison_arm_id="B0_BASELINE_ROUTE",
                route_mode="route-v1",  # wrong for B0
            )

    def test_unknown_route_mode_rejected(self):
        with pytest.raises(ValueError):
            TrialIdentity(
                experiment_id="exp", task_id="T1",
                recovery_strategy_arm=ControlArm.A3_ODYS_FULL,
                route_mode="experimental-v2",
            )

    def test_executor_cross_checks_identity(self):
        with pytest.raises(ValueError):
            execute_trial(
                arm=ControlArm.A0_BARE,
                task_json={"task_description": "x"},
                model_driver=_SequenceDriver([]),
                budget=BudgetConfig(max_turns=1, max_model_calls=1),
                experiment_id="exp", task_id="T1",
                identity=TrialIdentity(
                    experiment_id="exp", task_id="T1",
                    recovery_strategy_arm=ControlArm.A3_ODYS_FULL,
                    comparison_arm_id="B1_ROUTE_V1",
                    route_mode="route-v1",
                ),
            )


class _SequenceDriver:
    def __init__(self, actions):
        self.actions = list(actions)

    def next_action(self, messages, tool_definitions, generation_config=None):
        if not self.actions:
            return ModelAction(type="final_answer", content="script exhausted")
        return self.actions.pop(0)

    def record_tool_result(self, tool_name, result):
        pass

    def get_token_usage(self):
        return type("Usage", (), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})()

    def get_total_tokens(self):
        return 0

    def reset(self):
        pass


# ══════════════════════════════════════════════════════════════════════
# Manifest schema fail-closed rules (T11)
# ══════════════════════════════════════════════════════════════════════

class TestInvalidManifests:
    def test_t11a_route_arms_on_wrong_schema_rejected(self, tmp_path):
        manifest = _make_manifest()
        manifest["schema_version"] = "phase5-real-pilot-002"
        manifest.pop("manifest_hash", None)
        manifest["manifest_hash"] = manifest_hash(manifest)
        path = _persist(tmp_path, manifest, rehash=False)
        with pytest.raises(ValueError, match="refusing to guess"):
            _runner(tmp_path, path)

    def test_t11b_unknown_route_mode_rejected(self, tmp_path):
        manifest = _make_manifest()
        manifest["route_arms"][0]["route_mode"] = "experimental-v2"
        path = _persist(tmp_path, manifest)
        with pytest.raises(ValueError, match="requires route_mode"):
            _runner(tmp_path, path)

    def test_t11c_wrong_recovery_arm_rejected(self, tmp_path):
        manifest = _make_manifest()
        manifest["route_arms"][0]["recovery_strategy_arm"] = "A0_BARE"
        manifest["route_arms"][1]["recovery_strategy_arm"] = "A0_BARE"
        path = _persist(tmp_path, manifest)
        with pytest.raises(ValueError, match="A3_ODYS_FULL"):
            _runner(tmp_path, path)

    def test_t11d_missing_budget_rejected(self, tmp_path):
        manifest = _make_manifest()
        manifest.pop("budgets")
        manifest.pop("root_budget")
        path = _persist(tmp_path, manifest)
        with pytest.raises(ValueError, match="BUDGET_MISSING"):
            _runner(tmp_path, path)

    def test_t11e_manifest_hash_mismatch_rejected(self, tmp_path):
        manifest = _make_manifest()
        manifest["task_count"] = 99  # tamper AFTER hashing
        path = _persist(tmp_path, manifest, rehash=False)
        with pytest.raises(ValueError, match="hash mismatch"):
            _runner(tmp_path, path)

    def test_t11f_duplicate_pair_identity_rejected(self):
        with pytest.raises(ValueError, match="duplicate task-fault pair"):
            _make_manifest(pairs=[_dev_pair(), _dev_pair()])

    def test_t11g_wrong_task_fingerprint_rejected(self, tmp_path):
        manifest = _make_manifest()
        manifest["tasks"][0]["file_path"] = "perturbed_tasks/c1/C1_task_999_P0.json"
        path = _persist(tmp_path, manifest)
        with pytest.raises(ValueError, match="preflight failed"):
            _runner(tmp_path, path)

    def test_t11h_missing_route_arms_rejected(self, tmp_path):
        manifest = _make_manifest()
        manifest.pop("route_arms")
        path = _persist(tmp_path, manifest)
        with pytest.raises(ValueError, match="route_arms"):
            _runner(tmp_path, path)


# ══════════════════════════════════════════════════════════════════════
# Authorization fail-closed (run-time gate)
# ══════════════════════════════════════════════════════════════════════

class TestAuthorization:
    def test_unapproved_real_run_fails_closed(self, tmp_path):
        path = _persist(tmp_path, _make_manifest())
        runner = _runner(tmp_path, path)
        with pytest.raises(PermissionError, match="B0B1_APPROVAL_REQUIRED"):
            runner.run_pilot()

    def test_approval_bound_to_other_manifest_rejected(self, tmp_path):
        path = _persist(tmp_path, _make_manifest())
        runner = _runner(tmp_path, path)
        with pytest.raises(PermissionError, match="MANIFEST_MISMATCH"):
            runner.run_pilot(authorization={
                "status": "HUMAN_APPROVED", "approved_by": "tester",
                "manifest_hash": "0" * 64,
            })

    def test_offline_validation_entry_needs_no_approval(self, tmp_path):
        path = _persist(tmp_path, _make_manifest())
        runner = _runner(tmp_path, path)
        result = runner.run_pilot(offline_validation=True)
        assert result["execution_mode"]["execution_mode"] == "OFFLINE_VALIDATION"
        assert result["execution_mode"]["included_in_confirmatory_analysis"] is False
        assert result["execution_mode"]["real_provider_executed"] == "NO"

    def test_offline_validation_refuses_real_credentials(self, tmp_path, monkeypatch):
        """Adversarial: real traffic must never be recorded as fake.

        Offline validation is fail-closed when a real API credential is
        present in the environment.
        """
        monkeypatch.setenv("ODYS_AGENT_API_KEY", "sk-real-looking-key")
        path = _persist(tmp_path, _make_manifest())
        runner = _runner(tmp_path, path)
        with pytest.raises(PermissionError, match="OFFLINE_WITH_REAL_CREDENTIALS"):
            runner.run_pilot(offline_validation=True)


# ══════════════════════════════════════════════════════════════════════
# T1 / T8 / T12 / T13 — happy-path paired run through the formal entry
# ══════════════════════════════════════════════════════════════════════

class TestPairedRun:
    def test_t1_t8_t12_t13_paired_run(self, tmp_path):
        ALL_FAKE_CLIENTS.clear()
        path = _persist(tmp_path, _make_manifest())
        runner = _runner(tmp_path, path)
        result = runner.run_pilot(offline_validation=True)

        # T1: both arms executed without errors
        assert result["errors"] == 0
        assert result["executed_trials"] == 2
        assert result["all_pairings_valid"] is True

        # T8: distinct identities, distinct artifact directories
        pairs = result["pair_records"]
        assert len(pairs) == 1
        trial_ids = [t["trial_id"] for t in pairs[0]["trials"]]
        assert len(set(trial_ids)) == 2
        for tid in trial_ids:
            assert (tmp_path / "out" / tid / "COMPLETE").exists()
            assert (tmp_path / "out" / tid / "trial_manifest.json").exists()

        # T12: unique difference surface — pairing valid means every
        # invariant matched and only route_mode/identity differ
        by_arm = {t["comparison_arm_id"]: t for t in pairs[0]["trials"]}
        assert set(by_arm) == {"B0_BASELINE_ROUTE", "B1_ROUTE_V1"}
        assert by_arm["B0_BASELINE_ROUTE"]["route_mode"] == "baseline"
        assert by_arm["B1_ROUTE_V1"]["route_mode"] == "route-v1"
        inv0 = json.loads((tmp_path / "out" / trial_ids[0] / "trial_manifest.json").read_text(encoding="utf-8"))
        inv1 = json.loads((tmp_path / "out" / trial_ids[1] / "trial_manifest.json").read_text(encoding="utf-8"))
        for field in ExperimentPairValidator.INVARIANT_FIELDS:
            assert inv0.get(field) == inv1.get(field), field
        assert inv0["comparison_arm_id"] != inv1["comparison_arm_id"]

        # T1: identical judge results across arms (same scripted run)
        assert by_arm["B0_BASELINE_ROUTE"]["grader_pass"] == by_arm["B1_ROUTE_V1"]["grader_pass"]
        assert by_arm["B0_BASELINE_ROUTE"]["tsr"] == by_arm["B1_ROUTE_V1"]["tsr"]

        # T1: plain success — no fault, no recovery trigger in either arm
        for t in pairs[0]["trials"]:
            assert t["recovery_routes"] == [], t
            assert t["validity"] == "VALID", t
            assert t["termination_reason"] == "completed", t

        # T13: attribution chain — manifest_hash → pair → trial →
        # decision → authorization/execution → judge → usage
        experiment_manifest = json.loads(
            (tmp_path / "out" / "experiment_manifest.json").read_text(encoding="utf-8")
        )
        assert experiment_manifest["manifest_hash"] == json.loads(
            path.read_text(encoding="utf-8")
        )["manifest_hash"]
        for tid in trial_ids:
            tdir = tmp_path / "out" / tid
            tmanifest = json.loads((tdir / "trial_manifest.json").read_text(encoding="utf-8"))
            decisions = json.loads((tdir / "recovery_decisions.json").read_text(encoding="utf-8"))
            events = json.loads((tdir / "control_events.json").read_text(encoding="utf-8"))
            judgement = json.loads((tdir / "native_judgement.json").read_text(encoding="utf-8"))
            usage = json.loads((tdir / "provider_usage.json").read_text(encoding="utf-8"))
            assert tmanifest["trial_id"] == tid
            assert isinstance(judgement, dict)
            assert "provider_request_count" in usage
            # every recovery decision id is traceable into the event stream
            event_decision_ids = {
                e.get("decision_id") for e in events if e.get("decision_id")
            }
            for d in decisions:
                assert d.get("decision_id") in event_decision_ids or d.get("suppressed")

        # T14 part 1: provider request count is exactly the scripted count
        total_requests = sum(len(f.chat.completions.requests) for f in ALL_FAKE_CLIENTS)
        assert total_requests == 4  # 2 per trial × 2 trials
        for fake in ALL_FAKE_CLIENTS:
            for request in fake.chat.completions.requests:
                # no sampling parameters are sent (P0-6 honesty)
                assert "temperature" not in request
                assert "top_p" not in request
                assert "seed" not in request


# ══════════════════════════════════════════════════════════════════════
# T9 / T10 — repetitions and interrupt/resume identity safety
# ══════════════════════════════════════════════════════════════════════

class TestRepetitionAndResume:
    def test_t9_repetitions_get_distinct_identities(self, tmp_path):
        path = _persist(tmp_path, _make_manifest(repetitions=2))
        runner = _runner(tmp_path, path)
        result = runner.run_pilot(offline_validation=True)
        assert result["executed_trials"] == 4
        trial_ids = [
            t["trial_id"] for pair in result["pair_records"] for t in pair["trials"]
        ]
        assert len(set(trial_ids)) == 4
        for tid in trial_ids:
            assert (tmp_path / "out" / tid / "COMPLETE").exists()
        paired_files = sorted(
            p.name for p in (tmp_path / "out" / "paired_manifests").iterdir()
        )
        assert paired_files == [
            f"{DEV_TASK_ID}_r01_paired.json",
            f"{DEV_TASK_ID}_r02_paired.json",
        ]

    def test_t10_interrupt_and_resume_no_double_count(self, tmp_path):
        path = _persist(tmp_path, _make_manifest())
        runner = _runner(tmp_path, path)
        first = runner.run_pilot(offline_validation=True)
        assert first["executed_trials"] == 2

        # Simulate an interrupted rerun: one arm finished (COMPLETE),
        # the other left a partial artifact dir WITHOUT COMPLETE.
        b1_id = f"phase5-b0b1-e2e_{DEV_TASK_ID}_B1_ROUTE_V1_r01"
        partial = tmp_path / "out" / f"{b1_id}__attempt-deadbeef"
        partial.mkdir(parents=True, exist_ok=True)
        (partial / "official_trace.json").write_text("{}", encoding="utf-8")
        # remove B1's COMPLETE to simulate "never finished"
        (tmp_path / "out" / b1_id / "COMPLETE").unlink()

        runner2 = _runner(tmp_path, path)
        second = runner2.run_pilot(offline_validation=True)
        # B0 (still COMPLETE) is skipped, B1 re-executed exactly once
        assert second["skipped_complete"] == 1
        assert second["executed_trials"] == 1
        # no double counting: old artifacts preserved; the re-execution
        # lands in a NEW attempt directory (create-only semantics), the
        # pre-existing partial attempt is untouched
        b0_id = f"phase5-b0b1-e2e_{DEV_TASK_ID}_B0_BASELINE_ROUTE_r01"
        assert (tmp_path / "out" / b0_id / "COMPLETE").exists()
        assert (partial / "official_trace.json").read_text(encoding="utf-8") == "{}"
        completed_attempts = [
            p for p in (tmp_path / "out").iterdir()
            if p.name.startswith(b1_id + "__attempt-") and (p / "COMPLETE").exists()
        ]
        assert len(completed_attempts) == 1, [
            p.name for p in (tmp_path / "out").iterdir() if p.name.startswith(b1_id)
        ]
        # the re-executed attempt carries B1's identity — never mis-attributed
        attempt_manifest = json.loads(
            (completed_attempts[0] / "trial_manifest.json").read_text(encoding="utf-8")
        )
        assert attempt_manifest["comparison_arm_id"] == "B1_ROUTE_V1"
        assert attempt_manifest["trial_id"] == b1_id
        b0_manifest = json.loads(
            (tmp_path / "out" / b0_id / "trial_manifest.json").read_text(encoding="utf-8")
        )
        assert b0_manifest["comparison_arm_id"] == "B0_BASELINE_ROUTE"


# ══════════════════════════════════════════════════════════════════════
# T6 / T7 — shared safety gate and shared root budget
# ══════════════════════════════════════════════════════════════════════

class TestSharedGateAndBudget:
    def test_t6_unmet_precondition_blocks_confirm(self, monkeypatch):
        """booking_ready=false: the dependent confirm is blocked in BOTH arms."""
        if not TASK_PATH.exists():
            pytest.skip("frozen ToolMaze task data not available")
        actions = [
            _tool_action("build_flight_booking_request",
                         {"flight_number": "CA1234"}, "call-1"),
            _tool_action("confirm_held_recommendation_order",
                         {"reservation_id": "RESV-1"}, "call-2"),
            ModelAction(type="final_answer", content="done"),
        ]
        stubs = [{"booking_ready": False}]
        results = {}
        for arm in ("B1_ROUTE_V1", "B0_BASELINE_ROUTE"):
            results[arm] = _run_stub_trial(
                monkeypatch,
                route_mode=ComparisonArm(arm).route_mode,
                stubs=stubs,
                comparison_arm=arm,
                actions=actions,
            )
        blocked = {}
        for arm, res in results.items():
            executed_tools = [o["tool_name"] for o in res.public_tool_observations]
            assert "confirm_held_recommendation_order" not in executed_tools, executed_tools
            blocked[arm] = [
                (e.get("tool_name"), e.get("rule"))
                for e in res.control_events if e.get("kind") == "BLOCKED"
            ]
            assert ("confirm_held_recommendation_order",
                    "DEPENDENCY_PRECONDITION_UNRESOLVED") in blocked[arm], blocked[arm]
        # identical interception in both arms
        assert blocked["B1_ROUTE_V1"] == blocked["B0_BASELINE_ROUTE"]

    def test_t7_root_budget_hard_limit_shared(self, tmp_path):
        manifest = _make_manifest()
        manifest["root_budget"] = {
            "max_turns": 5, "max_model_calls": 1,
            "max_tokens": 100000, "token_budget": 100000,
        }
        manifest["budgets"] = dict(manifest["root_budget"])
        path = _persist(tmp_path, manifest)
        script = [
            _response([
                _call("provider-1", "get_flight_info_aviationstack",
                      {"flight_number": "CA1234"}),
            ]),
        ]
        runner = _runner(tmp_path, path, factory=_driver_factory(lambda: script))
        result = runner.run_pilot(offline_validation=True)
        assert result["errors"] == 0
        for pair in result["pair_records"]:
            for t in pair["trials"]:
                # budget exhaustion is a VALID scientific outcome
                assert t["validity"] == "VALID"
                assert t["termination_reason"] in (
                    "MODEL_CALL_BUDGET_EXHAUSTED",
                    "TURN_BUDGET_EXHAUSTED",
                    "TOKEN_BUDGET_EXHAUSTED",
                )
                # the root budget was never exceeded
                assert t["provider_usage"]["model_calls_used"] <= 1
                assert t["provider_usage"]["provider_request_count"] <= 1


# ══════════════════════════════════════════════════════════════════════
# T2–T5 — recovery route semantics through the canonical executor
# (deterministic fault stub at the tool-result boundary; everything
# downstream — gate, normalizer, route layer, budget gate, judge — is
# the real frozen pipeline)
# ══════════════════════════════════════════════════════════════════════

def _scripted_actions():
    return [
        ModelAction(
            type="tool_call",
            tool_calls=[ModelToolCall(
                tool_name="get_flight_info_aviationstack",
                arguments={"flight_number": "CA1234"},
                provider_tool_call_id="prov-1",
            )],
        ),
        ModelAction(type="final_answer", content="done"),
    ]


def _tool_action(name, arguments, provider_id):
    return ModelAction(
        type="tool_call",
        tool_calls=[ModelToolCall(
            tool_name=name, arguments=dict(arguments),
            provider_tool_call_id=provider_id,
        )],
    )


def _run_stub_trial(monkeypatch, *, route_mode, stubs, comparison_arm,
                    actions=None, budget=None):
    original = Phase5AgentCore.receive_tool_result
    counter = {"n": 0}

    def stubbed(self, tool_name, result, tool_call_index=0):
        idx = counter["n"]
        counter["n"] += 1
        if idx < len(stubs):
            merged = dict(result) if isinstance(result, dict) else {"raw": str(result)}
            merged.update(stubs[idx])
            result = merged
        return original(self, tool_name, result, tool_call_index=tool_call_index)

    monkeypatch.setattr(Phase5AgentCore, "receive_tool_result", stubbed)
    identity = TrialIdentity(
        experiment_id="b0b1-stub",
        task_id=DEV_TASK_ID,
        recovery_strategy_arm=ControlArm.A3_ODYS_FULL,
        comparison_arm_id=comparison_arm,
        route_mode=route_mode,
    )
    result = execute_trial(
        arm=ControlArm.A3_ODYS_FULL,
        task_json=json.loads(TASK_PATH.read_text(encoding="utf-8")),
        model_driver=_SequenceDriver(list(actions) if actions else _scripted_actions()),
        budget=budget or BudgetConfig(max_turns=6, max_model_calls=6, token_budget=100000),
        experiment_id="b0b1-stub",
        task_id=DEV_TASK_ID,
        route_mode=route_mode,
        identity=identity,
    )
    monkeypatch.setattr(Phase5AgentCore, "receive_tool_result", original)
    return result


def _routes(result):
    return [d.get("route") for d in result.recovery_decisions]


def _reason_codes(result):
    return [d.get("reason_code") for d in result.recovery_decisions]


class TestRecoveryRouteScenarios:
    def test_t2_transient_failure_bounded_legal_retry(self, monkeypatch):
        if not TASK_PATH.exists():
            pytest.skip("frozen ToolMaze task data not available")
        stubs = [{"error": "transient glitch", "transient": True}]
        b1 = _run_stub_trial(
            monkeypatch, route_mode="route-v1", stubs=stubs,
            comparison_arm="B1_ROUTE_V1",
        )
        b0 = _run_stub_trial(
            monkeypatch, route_mode="baseline", stubs=stubs,
            comparison_arm="B0_BASELINE_ROUTE",
        )
        legal = {"CONTINUE", "RETRY_SAME", "RETRY_CHANGED", "REPLAN", "ESCALATE", "STOP"}
        assert set(_routes(b1)) <= legal
        assert set(_routes(b0)) <= legal
        # transient failure produces a bounded retry-family decision
        assert set(_routes(b1)) & {"RETRY_SAME", "RETRY_CHANGED"}, b1.recovery_decisions
        assert set(_routes(b0)) & {"RETRY_SAME", "RETRY_CHANGED"}, b0.recovery_decisions
        # identical shared budget accounting across arms
        assert (b1.provider_usage["model_calls_used"]
                == b0.provider_usage["model_calls_used"])

    def test_t2_transient_basis_retries_same_action(self):
        """Q3c semantics: explicit transient basis + plain-retry proposal.

        NOTE (documented scope): the frozen A3 policy always proposes
        retry_with_context, and rule c (honour the context-retry proposal)
        preempts the Q3c/Q3d branches.  The RETRY_SAME transient-basis
        semantic is therefore verified at the frozen decision layer —
        the same layer the readiness V-tests exercised it in.
        """
        b1 = RecoveryRouteDecider(mode=ROUTE_MODE_V1)
        decision = b1.decide(RouteDecisionInputs(
            step=1, proposed_action="retry", tool_name="tool_a",
            last_exec_status="FAILED", last_retryable="RETRYABLE",
            transient_basis=True,
        ))
        assert decision.route is RecoveryRoute.RETRY_SAME
        assert decision.reason_code == "TRANSIENT_BASIS"
        b0 = RecoveryRouteDecider(mode=ROUTE_MODE_BASELINE)
        passthrough = b0.decide(RouteDecisionInputs(
            step=1, proposed_action="retry", tool_name="tool_a",
            last_exec_status="FAILED", last_retryable="RETRYABLE",
            transient_basis=True,
        ))
        assert passthrough.route is RecoveryRoute.RETRY_SAME
        assert passthrough.reason_code == "STRATEGY_PASSTHROUGH"

    def test_t3_permanent_failure_replan_only_in_b1(self, monkeypatch):
        if not TASK_PATH.exists():
            pytest.skip("frozen ToolMaze task data not available")
        stubs = [{"error": "bad input", "retryable": False}]
        b1 = _run_stub_trial(
            monkeypatch, route_mode="route-v1", stubs=stubs,
            comparison_arm="B1_ROUTE_V1",
        )
        b0 = _run_stub_trial(
            monkeypatch, route_mode="baseline", stubs=stubs,
            comparison_arm="B0_BASELINE_ROUTE",
        )
        # B1 (route-v1) re-plans on a permanent failure (Q3a)
        assert "REPLAN" in _routes(b1), (b1.recovery_decisions, _reason_codes(b1))
        # B0 passes the strategy proposal through — no Q3a enhancement
        assert "REPLAN" not in _routes(b0), (b0.recovery_decisions, _reason_codes(b0))
        # every route decision is traceable in the control event stream
        for result in (b1, b0):
            decision_ids = {d.get("decision_id") for d in result.recovery_decisions}
            event_ids = {e.get("decision_id") for e in result.control_events}
            assert decision_ids <= event_ids

    def test_t4_no_progress_loop_bounded_in_both_arms(self, monkeypatch):
        if not TASK_PATH.exists():
            pytest.skip("frozen ToolMaze task data not available")
        # identical non-success results on identical replayed calls
        stubs = [{"error": "boom"}, {"error": "boom"}]
        identical = {"tool_name": "get_flight_info_aviationstack",
                     "arguments": {"flight_number": "CA1234"}}
        actions = [
            _tool_action(identical["tool_name"], identical["arguments"], "call-1"),
            _tool_action(identical["tool_name"], identical["arguments"], "call-2"),
            _tool_action(identical["tool_name"], identical["arguments"], "call-3"),
            ModelAction(type="final_answer", content="done"),
        ]
        b1 = _run_stub_trial(
            monkeypatch, route_mode="route-v1", stubs=stubs,
            comparison_arm="B1_ROUTE_V1", actions=actions,
        )
        b0 = _run_stub_trial(
            monkeypatch, route_mode="baseline", stubs=stubs,
            comparison_arm="B0_BASELINE_ROUTE", actions=actions,
        )
        # B1 bounds the loop with its Q3b enhancement rule
        b1_codes = set(_reason_codes(b1))
        assert b1_codes & {"NO_PROGRESS_REPLAN", "NO_PROGRESS_LOOP", "REPLAN_EXHAUSTED"}, (
            b1.recovery_decisions,
        )
        # B0 passes the proposal through — no Q3b enhancement
        b0_codes = set(_reason_codes(b0))
        assert not b0_codes & {"NO_PROGRESS_REPLAN", "NO_PROGRESS_LOOP"}, b0.recovery_decisions
        # the SHARED gate bounds B0's blind passthrough replay
        b0_blocked = [
            e.get("rule") for e in b0.control_events if e.get("kind") == "BLOCKED"
        ]
        assert "NO_PROGRESS_RETRY" in b0_blocked, (b0.control_events, _routes(b0))
        # and the gate is active in BOTH arms (proposals recorded)
        for res in (b1, b0):
            kinds = [e.get("kind") for e in res.control_events]
            assert "PROPOSED" in kinds or "BLOCKED" in kinds, kinds
            # no unbounded replay in either arm
            assert len(res.recovery_decisions) <= 4, res.recovery_decisions

    def test_t5_observed_state_change_recorded_and_honest(self, monkeypatch):
        if not TASK_PATH.exists():
            pytest.skip("frozen ToolMaze task data not available")
        stubs = [
            {"error": "x", "status": "failed", "booking_ready": False},
            {"error": "x", "status": "failed", "booking_ready": True},
        ]
        actions = [
            _tool_action("get_flight_info_aviationstack",
                         {"flight_number": "CA1234"}, "call-1"),
            _tool_action("get_flight_info_aviationstack",
                         {"flight_number": "CA1234"}, "call-2"),
            _tool_action("get_flight_info_aviationstack",
                         {"flight_number": "CA1234"}, "call-3"),
            ModelAction(type="final_answer", content="done"),
        ]
        b1 = _run_stub_trial(
            monkeypatch, route_mode="route-v1", stubs=stubs,
            comparison_arm="B1_ROUTE_V1", actions=actions,
        )
        b0 = _run_stub_trial(
            monkeypatch, route_mode="baseline", stubs=stubs,
            comparison_arm="B0_BASELINE_ROUTE", actions=actions,
        )
        # the state change is grounded in real observed evidence — the
        # recorded state_change field reflects it, never fabricated
        for res in (b1, b0):
            state_changes = [d.get("state_change") for d in res.recovery_decisions]
            assert "OBSERVED_CHANGE" in state_changes, res.recovery_decisions
        # both arms stay in the legal route vocabulary and are bounded
        legal = {"CONTINUE", "RETRY_SAME", "RETRY_CHANGED", "REPLAN", "ESCALATE", "STOP"}
        assert set(_routes(b1)) <= legal
        assert set(_routes(b0)) <= legal

    def test_t5_q3d_route_semantics_at_decision_layer(self):
        """Q3d semantics: observed state change + plain-retry proposal.

        Same documented scope as the Q3c test: with retry_with_context
        proposals (all the frozen A3 policy emits) rule c preempts Q3d,
        so the route-level B0/B1 difference is verified at the frozen
        decision layer (mirrors readiness V4).
        """
        b1 = RecoveryRouteDecider(mode=ROUTE_MODE_V1)
        decision = b1.decide(RouteDecisionInputs(
            step=1, proposed_action="retry", tool_name="tool_a",
            last_exec_status="FAILED", last_retryable="UNKNOWN",
            state_change=StateChange.OBSERVED_CHANGE,
        ))
        assert decision.route is RecoveryRoute.RETRY_CHANGED
        assert decision.reason_code == "STATE_CHANGED"
        b0 = RecoveryRouteDecider(mode=ROUTE_MODE_BASELINE)
        passthrough = b0.decide(RouteDecisionInputs(
            step=1, proposed_action="retry", tool_name="tool_a",
            last_exec_status="FAILED", last_retryable="UNKNOWN",
            state_change=StateChange.OBSERVED_CHANGE,
        ))
        assert passthrough.route is RecoveryRoute.RETRY_SAME
        assert passthrough.reason_code == "STRATEGY_PASSTHROUGH"


# ══════════════════════════════════════════════════════════════════════
# T14 — zero real provider requests (explicit network block)
# ══════════════════════════════════════════════════════════════════════

class TestZeroRealRequests:
    def test_t14_no_network_escape(self, monkeypatch, tmp_path):
        blocked = []

        def deny(*args, **kwargs):
            blocked.append(args)
            raise AssertionError("real network access attempted")

        # Block OUTBOUND connections to non-loopback addresses: asyncio's
        # ProactorEventLoop legitimately creates local socketpair sockets
        # for its self-pipe on Windows (loopback connect in the fallback) —
        # those are not network access.  Real provider traffic can only
        # leave via a non-loopback connect / create_connection.
        def guarded_connect(sock, address, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in ("127.0.0.1", "::1", "localhost"):
                blocked.append(address)
                raise AssertionError("real network access attempted")
            return original_connect(sock, address, *args, **kwargs)

        original_connect = socket.socket.connect
        monkeypatch.setattr(socket, "create_connection", deny)
        monkeypatch.setattr(socket.socket, "connect", guarded_connect)

        path = _persist(tmp_path, _make_manifest())
        runner = _runner(tmp_path, path)
        result = runner.run_pilot(offline_validation=True)
        assert result["errors"] == 0
        assert blocked == []
        # the only provider traffic is the counted fake-client traffic
        total = sum(len(f.chat.completions.requests) for f in ALL_FAKE_CLIENTS)
        assert total > 0
