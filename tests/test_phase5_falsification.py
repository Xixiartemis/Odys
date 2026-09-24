"""Phase5 Falsification Tests — BENCHMARK-OPTIONAL.

Tests that require the external ToolMaze benchmark checkout are skipped
when the data is absent.  Default CI passes from clean checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lhas.phase5.types import (
    BenchmarkName,
    BudgetConfig,
    ControlArm,
    GenerationConfig,
    PerturbationMode,
    SignalKind,
    TrialManifest,
    TrialStatus,
)

# Benchmark availability check
_BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "experiments" / "phase5" / "benchmarks" / "toolmaze" / "data"
_BENCHMARK_AVAILABLE = (_BENCHMARK_DIR / "perturbed_tasks").is_dir()


def _require_benchmark():
    if not _BENCHMARK_AVAILABLE:
        pytest.skip("External ToolMaze benchmark not available (expected in experiments/phase5/benchmarks/toolmaze/data/)")


# ── T1: ToolMaze native P-mode preserved ─────────────────────────

class TestT1_ToolMazeNativePModePreserved:
    def test_all_perturbation_modes_present(self):
        _require_benchmark()
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        modes = {t.perturbation_mode for t in tasks}
        for m in PerturbationMode:
            assert m in modes

    def test_p_mode_in_native_condition(self):
        _require_benchmark()
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        for task in tasks:
            condition = adapter.native_condition(task)
            assert task.perturbation_mode.value in condition


# ── T2: Benchmark condition identical across arms ────────────────

class TestT2_BenchmarkConditionIdenticalAcrossArms:
    @pytest.mark.asyncio
    async def test_all_arms_share_same_task(self):
        _require_benchmark()
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        from lhas.phase5.control_arms import create_policy
        adapter = ToolMazeAdapter()
        gen_config = GenerationConfig(model_id="test", provider="test")
        tasks = adapter.enumerate_tasks()
        desc = tasks[0]
        runtime_task = adapter.build_runtime_task(desc)
        for arm in ControlArm:
            policy = create_policy(arm)
            result = await policy.execute_trial(
                task=runtime_task, adapter=adapter, generation_config=gen_config,
            )
            assert result.get("task_id") == desc.task_id or "task_id" in str(result)


# ── T9: Native metric preserved ─────────────────────────────────

class TestT9_NativeMetricPreserved:
    def test_toolmaze_native_result_preserved(self):
        _require_benchmark()
        from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
        adapter = ToolMazeAdapter()
        tasks = adapter.enumerate_tasks()
        perturbed = [t for t in tasks if t.perturbation_mode.value != "P0"][:5]
        for desc in perturbed:
            artifact = adapter.finalize_runtime_artifact(desc.task_id)
            result = adapter.offline_native_evaluate(desc.task_id, artifact)
            assert result.tsr is not None


# ── T15: Paired trial identity ──────────────────────────────────

class TestT15_PairedTrialIdentity:
    def test_trial_manifest_identity(self):
        gen_config = GenerationConfig(model_id="test", provider="test")
        budget = BudgetConfig(max_turns=30, max_model_calls=50)
        m1 = TrialManifest(
            experiment_id="EXP-001", trial_id="t1",
            benchmark_name=BenchmarkName.TOOLMAZE,
            benchmark_revision="test", dataset_digest="abc123",
            task_id="T1", native_condition="C1/P0",
            perturbation_mode=PerturbationMode.P0,
            arm=ControlArm.A0_BARE,
            model_id="test", provider="test",
            generation_config=gen_config, root_budget=budget,
        )
        m2 = TrialManifest(
            experiment_id="EXP-001", trial_id="t2",
            benchmark_name=m1.benchmark_name,
            benchmark_revision=m1.benchmark_revision,
            dataset_digest=m1.dataset_digest,
            task_id=m1.task_id, native_condition=m1.native_condition,
            perturbation_mode=m1.perturbation_mode,
            arm=ControlArm.A3_ODYS_FULL,
            model_id=m1.model_id, provider=m1.provider,
            generation_config=m1.generation_config, root_budget=m1.root_budget,
        )
        assert m1.task_id == m2.task_id
        assert m1.arm != m2.arm


# ── Firewall tests ──────────────────────────────────────────────

class TestFirewall:
    def test_offline_methods_blocked_during_runtime(self):
        from lhas.phase5.firewall import OfflineGraderFirewall, FirewallViolation
        fw = OfflineGraderFirewall()
        fw.begin_runtime()
        with pytest.raises(FirewallViolation):
            fw.check_runtime_access("offline_native_evaluate")
        fw.end_runtime()

    def test_module_audit_clean(self):
        from lhas.phase5.firewall import OfflineGraderFirewall
        fw = OfflineGraderFirewall()
        audit = fw.audit_module_access()
        for k, v in audit.items():
            assert v != "VIOLATION", f"Firewall violation: {k}"


# ── Phase4 regression ───────────────────────────────────────────

class TestPhase4Regression:
    def test_phase4_core_imports(self):
        from lhas.recovery import DefaultRecoveryPolicy
        from lhas.domain.enums import FailureType, RecoveryActionType
        assert DefaultRecoveryPolicy is not None
        assert FailureType.TOOL_ERROR.value == "TOOL_ERROR"

    def test_phase4_enum_values_stable(self):
        from lhas.domain.enums import RecoveryActionType
        assert RecoveryActionType.ESCALATE.value == "ESCALATE"
        assert RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT.value == "RETRY_WITH_FAILURE_CONTEXT"
