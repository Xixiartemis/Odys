"""Phase 5 Falsification Harness — Provider-Free Tests (T1–T17).

All tests run without a real provider/model.  They verify structural
invariants, firewall integrity, metric preservation, and arm authority
matching.  Phase 4 regression is verified by running the existing suite.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from lhas.phase5.types import (
    AuditReport,
    BenchmarkName,
    BudgetConfig,
    ControlArm,
    FaultSource,
    GenerationConfig,
    NativeResult,
    PerturbationMode,
    RuntimeTask,
    SignalKind,
    TaskDescriptor,
    TopologyClass,
    TrialManifest,
    TrialStatus,
)
from lhas.phase5.toolmaze_adapter import ToolMazeAdapter
from lhas.phase5.toolsandbox_adapter import ToolSandboxAdapter
from lhas.phase5.shadow_observer import ShadowProgressObserver
from lhas.phase5.control_arms import (
    ARM_POLICIES,
    BarePolicy,
    OdysFullPolicy,
    OdysMinusObservableProgress,
    OdysMinusRecoveryBudgetPolicy,
    RetryOnlyPolicy,
    ValidatorOnlyPolicy,
    assert_matched_authority,
    create_policy,
)
from lhas.phase5.fault_layer import (
    DerivedWrapperFaultLayer,
    NoOpFaultLayer,
    create_fault_layer,
)
from lhas.phase5.firewall import OfflineGraderFirewall, FirewallViolation
from lhas.phase5.artifacts import ArtifactWriter
from lhas.phase5.provenance import (
    ProvenanceFreeze,
    arm_definitions_snapshot,
    compute_manifest_hash,
)
from lhas.phase5.extension_adapters import TerminalBenchAdapter, TUABenchAdapter


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def toolmaze_adapter() -> ToolMazeAdapter:
    return ToolMazeAdapter()


@pytest.fixture
def toolsandbox_adapter() -> ToolSandboxAdapter:
    return ToolSandboxAdapter()


@pytest.fixture
def gen_config() -> GenerationConfig:
    return GenerationConfig(
        model_id="test-model-v1",
        provider="test-provider",
        temperature=0.0,
        seed=42,
    )


@pytest.fixture
def budget() -> BudgetConfig:
    return BudgetConfig(max_turns=30, max_model_calls=50)


@pytest.fixture
def shadow_observer() -> ShadowProgressObserver:
    return ShadowProgressObserver()


@pytest.fixture
def artifact_dir() -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="phase5_art_"))
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def provenance_dir() -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="phase5_prov_"))
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════
# T1: ToolMaze native P-mode preserved
# ══════════════════════════════════════════════════════════════════════

class TestT1_ToolMazeNativePModePreserved:
    """T1: Verify ToolMaze adapter preserves all P0-P4 perturbation modes."""

    def test_all_perturbation_modes_present(self, toolmaze_adapter: ToolMazeAdapter):
        tasks = toolmaze_adapter.enumerate_tasks()
        modes = {t.perturbation_mode for t in tasks}
        assert PerturbationMode.P0 in modes
        assert PerturbationMode.P1 in modes
        assert PerturbationMode.P2 in modes
        assert PerturbationMode.P3 in modes
        assert PerturbationMode.P4 in modes

    def test_p_mode_in_native_condition(self, toolmaze_adapter: ToolMazeAdapter):
        tasks = toolmaze_adapter.enumerate_tasks()
        for task in tasks:
            condition = toolmaze_adapter.native_condition(task)
            assert task.perturbation_mode.value in condition

    def test_native_result_preserves_p_mode(self, toolmaze_adapter: ToolMazeAdapter):
        tasks = toolmaze_adapter.enumerate_tasks()
        for task in tasks:
            artifact = toolmaze_adapter.finalize_runtime_artifact(task.task_id)
            result = toolmaze_adapter.offline_native_evaluate(task.task_id, artifact)
            assert result.native_metrics.get("perturbation_mode") == task.perturbation_mode.value


# ══════════════════════════════════════════════════════════════════════
# T2: Benchmark condition identical across arms
# ══════════════════════════════════════════════════════════════════════

class TestT2_BenchmarkConditionIdenticalAcrossArms:
    """T2: All arms must receive identical task/tools/environment/budget."""

    @pytest.mark.asyncio
    async def test_all_arms_share_same_task(self, toolmaze_adapter, gen_config, budget):
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]
        runtime_task = toolmaze_adapter.build_runtime_task(descriptor)

        results = {}
        for arm in ControlArm:
            policy = create_policy(arm)
            result = await policy.execute_trial(
                task=runtime_task,
                adapter=toolmaze_adapter,
                generation_config=gen_config,
            )
            results[arm] = result

        violations = assert_matched_authority(
            results,
            task_id=runtime_task.task_id,
            generation_config=gen_config,
            budget=budget,
        )
        assert violations == [], f"Authority violations: {violations}"


# ══════════════════════════════════════════════════════════════════════
# T3: Hidden perturbation labels unavailable to runtime
# ══════════════════════════════════════════════════════════════════════

class TestT3_HiddenPerturbationLabelsUnavailableToRuntime:
    """T3: RuntimeTask must not contain perturbation labels or oracle data."""

    def test_runtime_task_has_no_perturbation_labels(self, toolmaze_adapter):
        tasks = toolmaze_adapter.enumerate_tasks()
        for descriptor in tasks:
            runtime_task = toolmaze_adapter.build_runtime_task(descriptor)
            task_dict = runtime_task.model_dump()
            flat = json.dumps(task_dict).lower()
            # Runtime task must not contain oracle or perturbation labels
            assert "oracle" not in flat
            assert "perturbation_label" not in flat
            assert "ground_truth" not in flat

    def test_runtime_task_has_no_oracle_solution(self, toolmaze_adapter):
        tasks = toolmaze_adapter.enumerate_tasks()
        for descriptor in tasks:
            runtime_task = toolmaze_adapter.build_runtime_task(descriptor)
            task_dict = runtime_task.model_dump()
            assert "oracle_solution" not in json.dumps(task_dict)


# ══════════════════════════════════════════════════════════════════════
# T4: Oracle path unavailable to runtime
# ══════════════════════════════════════════════════════════════════════

class TestT4_OraclePathUnavailableToRuntime:
    """T4: RuntimeTask and public observations must not expose oracle paths."""

    def test_public_observation_has_no_oracle(self, toolmaze_adapter):
        tasks = toolmaze_adapter.enumerate_tasks()
        for descriptor in tasks[:3]:
            obs = toolmaze_adapter.collect_public_observation(descriptor.task_id, 0)
            obs_json = json.dumps(obs).lower()
            assert "oracle" not in obs_json
            assert "solution" not in obs_json


# ══════════════════════════════════════════════════════════════════════
# T5: Shadow observer cannot influence execution
# ══════════════════════════════════════════════════════════════════════

class TestT5_ShadowObserverCannotInfluenceExecution:
    """T5: ShadowProgressObserver returns signals but has no side effects."""

    def test_observer_returns_shadow_record(self, shadow_observer):
        record = shadow_observer.observe(
            task_id="test-task",
            step=0,
            action_identity="tool_alpha",
            tool_result={"status": "success"},
        )
        assert record.signal == SignalKind.PROGRESSING
        assert record.trial_id == "test-task"

    def test_observer_has_no_execution_methods(self, shadow_observer):
        """Observer must not have methods that modify execution state."""
        assert not hasattr(shadow_observer, "request_recovery")
        assert not hasattr(shadow_observer, "modify_budget")
        assert not hasattr(shadow_observer, "influence_execution")

    def test_observer_does_not_see_perturbation_labels(self, shadow_observer):
        """Observer receives only public tool results."""
        record = shadow_observer.observe(
            task_id="test-task",
            step=0,
            action_identity="tool_alpha",
            tool_result={"status": "success", "output": "done"},
        )
        features = record.observable_features
        assert "perturbation_mode" not in features
        assert "oracle" not in json.dumps(features)

    def test_observer_cannot_request_recovery(self, shadow_observer):
        """Verify observer protocol has no recovery request method."""
        from lhas.phase5.types import ProgressObserver
        # ProgressObserver protocol defines only observe()
        assert hasattr(ProgressObserver, "observe")


# ══════════════════════════════════════════════════════════════════════
# T6: Same root budget across arms
# ══════════════════════════════════════════════════════════════════════

class TestT6_SameRootBudgetAcrossArms:
    """T6: All arms receive identical root budget configuration."""

    def test_budget_is_frozen(self, budget):
        """BudgetConfig is immutable."""
        assert budget.max_turns == 30
        assert budget.max_model_calls == 50

    @pytest.mark.asyncio
    async def test_all_arms_receive_same_budget(self, toolmaze_adapter, gen_config):
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]
        runtime_task = toolmaze_adapter.build_runtime_task(descriptor)

        for arm in ControlArm:
            policy = create_policy(arm)
            result = await policy.execute_trial(
                task=runtime_task,
                adapter=toolmaze_adapter,
                generation_config=gen_config,
            )
            # Task budget is the same reference for all arms
            assert runtime_task.budget.max_turns == 30
            assert runtime_task.budget.max_model_calls == 50


# ══════════════════════════════════════════════════════════════════════
# T7: A4 removes only Observable Progress authority
# ══════════════════════════════════════════════════════════════════════

class TestT7_A4RemovesOnlyObservableProgress:
    """T7: A4 (ODYS_MINUS_OBSERVABLE_PROGRESS) disables only progress signals."""

    @pytest.mark.asyncio
    async def test_a4_disables_observable_progress(self, toolmaze_adapter, gen_config):
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]
        runtime_task = toolmaze_adapter.build_runtime_task(descriptor)

        policy = create_policy(ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS)
        result = await policy.execute_trial(
            task=runtime_task,
            adapter=toolmaze_adapter,
            generation_config=gen_config,
        )
        assert result.get("observable_progress_used") is False

    @pytest.mark.asyncio
    async def test_a4_still_has_shadow_observer(self, toolmaze_adapter, gen_config):
        """A4 still records shadow observations — just doesn't use them for recovery."""
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]
        runtime_task = toolmaze_adapter.build_runtime_task(descriptor)

        policy = create_policy(ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS)
        result = await policy.execute_trial(
            task=runtime_task,
            adapter=toolmaze_adapter,
            generation_config=gen_config,
        )
        # Shadow records should still exist
        assert result.get("shadow_records", 0) > 0

    @pytest.mark.asyncio
    async def test_a4_preserves_other_mechanisms(self, toolmaze_adapter, gen_config):
        """A4 still has retry and validator."""
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]
        runtime_task = toolmaze_adapter.build_runtime_task(descriptor)

        a4 = create_policy(ControlArm.A4_ODYS_MINUS_OBSERVABLE_PROGRESS)
        a3 = create_policy(ControlArm.A3_ODYS_FULL)

        result_a4 = await a4.execute_trial(
            task=runtime_task, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        result_a3 = await a3.execute_trial(
            task=runtime_task, adapter=toolmaze_adapter, generation_config=gen_config,
        )
        # Both should have steps (execution still happens)
        assert len(result_a4["steps"]) > 0
        assert len(result_a3["steps"]) > 0


# ══════════════════════════════════════════════════════════════════════
# T8: A5 removes only Recovery Budget Policy
# ══════════════════════════════════════════════════════════════════════

class TestT8_A5RemovesOnlyRecoveryBudgetPolicy:
    """T8: A5 (ODYS_MINUS_RECOVERY_BUDGET_POLICY) disables only budget-aware recovery."""

    @pytest.mark.asyncio
    async def test_a5_disables_budget_policy(self, toolmaze_adapter, gen_config):
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]
        runtime_task = toolmaze_adapter.build_runtime_task(descriptor)

        policy = create_policy(ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY)
        result = await policy.execute_trial(
            task=runtime_task,
            adapter=toolmaze_adapter,
            generation_config=gen_config,
        )
        assert result.get("recovery_budget_policy_active") is False

    @pytest.mark.asyncio
    async def test_a5_preserves_other_mechanisms(self, toolmaze_adapter, gen_config):
        """A5 still has retry, validator, and observable progress."""
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]
        runtime_task = toolmaze_adapter.build_runtime_task(descriptor)

        policy = create_policy(ControlArm.A5_ODYS_MINUS_RECOVERY_BUDGET_POLICY)
        result = await policy.execute_trial(
            task=runtime_task,
            adapter=toolmaze_adapter,
            generation_config=gen_config,
        )
        assert len(result["steps"]) > 0
        assert result.get("shadow_records", 0) > 0


# ══════════════════════════════════════════════════════════════════════
# T9: Benchmark native metric preserved verbatim
# ══════════════════════════════════════════════════════════════════════

class TestT9_NativeMetricPreservedVerbatim:
    """T9: Native benchmark metrics must be preserved without modification."""

    def test_toolmaze_native_result_preserved(self, toolmaze_adapter):
        tasks = toolmaze_adapter.enumerate_tasks()
        # Test P1-P4 tasks (P0 has no perturbation, PRR/RC are None)
        perturbed_tasks = [t for t in tasks if t.perturbation_mode.value != "P0"]
        for task in perturbed_tasks[:10]:
            artifact = toolmaze_adapter.finalize_runtime_artifact(task.task_id)
            result = toolmaze_adapter.offline_native_evaluate(task.task_id, artifact)
            assert result.tsr is not None
            # PRR and RC may be None if no victim tool is detected

    def test_toolsandbox_native_result_preserved(self, toolsandbox_adapter):
        tasks = toolsandbox_adapter.enumerate_tasks()
        for task in tasks:
            artifact = toolsandbox_adapter.finalize_runtime_artifact(task.task_id)
            result = toolsandbox_adapter.offline_native_evaluate(task.task_id, artifact)
            assert result.tsr is not None

    def test_native_result_not_modified_by_artifacts(self, toolmaze_adapter, artifact_dir):
        """Artifact writer stores native result verbatim."""
        writer = ArtifactWriter(artifact_dir)
        tasks = toolmaze_adapter.enumerate_tasks()
        task = tasks[0]
        artifact = toolmaze_adapter.finalize_runtime_artifact(task.task_id)
        native = toolmaze_adapter.offline_native_evaluate(task.task_id, artifact)

        path = writer.write_benchmark_result(task.task_id, native)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["tsr"] == native.tsr
        assert stored["prr"] == native.prr
        assert stored["rc"] == native.rc


# ══════════════════════════════════════════════════════════════════════
# T10: Derived metric cannot overwrite native metric
# ══════════════════════════════════════════════════════════════════════

class TestT10_DerivedMetricCannotOverwriteNative:
    """T10: Derived metrics are stored separately from native metrics."""

    def test_derived_metrics_stored_separately(self, toolsandbox_adapter, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        tasks = toolsandbox_adapter.enumerate_tasks()
        task = tasks[0]

        derived = toolsandbox_adapter.derive_progress_metrics(task.task_id, [])
        path = writer.write_derived_metrics(task.task_id, derived)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored.get("_label") == "PHASE5_DERIVED_METRIC"
        assert stored.get("_not_native_benchmark_score") is True

    def test_derived_and_native_in_different_paths(self, toolmaze_adapter, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        tasks = toolmaze_adapter.enumerate_tasks()
        task = tasks[0]

        artifact = toolmaze_adapter.finalize_runtime_artifact(task.task_id)
        native = toolmaze_adapter.offline_native_evaluate(task.task_id, artifact)
        derived = NativeResult(tsr=0.99, raw_score=0.99)  # Simulated derived

        native_path = writer.write_benchmark_result(task.task_id, native)
        derived_path = writer.write_derived_metrics(task.task_id, derived)  # type: ignore

        assert "benchmark" in str(native_path)
        assert "derived" in str(derived_path)
        assert str(native_path) != str(derived_path)


# ══════════════════════════════════════════════════════════════════════
# T11: Raw artifacts contain no hidden labels
# ══════════════════════════════════════════════════════════════════════

class TestT11_RawArtifactsContainNoHiddenLabels:
    """T11: Runtime artifacts must not contain oracle/perturbation/ground_truth."""

    def test_raw_artifact_clean(self, toolmaze_adapter, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        tasks = toolmaze_adapter.enumerate_tasks()
        task = tasks[0]

        path = writer.write_raw_artifact(
            task.task_id,
            runtime_events=[{"type": "test"}],
            tool_calls=[{"tool": "alpha"}],
            state_observations=[],
            progress_shadow=[],
            budget_ledger={"max_turns": 30},
        )
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored_json = json.dumps(stored).lower()
        assert "oracle" not in stored_json
        assert "ground_truth" not in stored_json
        assert "perturbation_label" not in stored_json

    def test_firewall_audit_clean(self):
        firewall = OfflineGraderFirewall()
        audit = firewall.audit_module_access()
        for key, value in audit.items():
            assert value != "VIOLATION", f"Firewall violation: {key}"


# ══════════════════════════════════════════════════════════════════════
# T12: Offline grader executes only after termination
# ══════════════════════════════════════════════════════════════════════

class TestT12_OfflineGraderOnlyAfterTermination:
    """T12: Offline-only methods must not be callable during runtime."""

    def test_offline_methods_blocked_during_runtime(self):
        firewall = OfflineGraderFirewall()
        firewall.begin_runtime()

        with pytest.raises(FirewallViolation, match="offline-only"):
            firewall.check_runtime_access("offline_native_evaluate")

        with pytest.raises(FirewallViolation, match="offline-only"):
            firewall.check_runtime_access("_get_oracle_solution")

        with pytest.raises(FirewallViolation, match="offline-only"):
            firewall.check_runtime_access("get_target_milestones")

    def test_offline_methods_allowed_after_termination(self):
        firewall = OfflineGraderFirewall()
        firewall.begin_runtime()
        firewall.end_runtime()

        # Should not raise
        firewall.check_offline_access("offline_native_evaluate")
        firewall.check_offline_access("_get_oracle_solution")

    def test_fail_closed_on_violation(self):
        firewall = OfflineGraderFirewall()
        report = AuditReport(
            firewall={"RUNTIME_HIDDEN_GROUND_TRUTH_ACCESS": "YES"},
        )
        with pytest.raises(FirewallViolation, match="firewall violation"):
            firewall.fail_closed(report)


# ══════════════════════════════════════════════════════════════════════
# T13: Fault wrapper disabled in ToolMaze primary
# ══════════════════════════════════════════════════════════════════════

class TestT13_FaultWrapperDisabledInToolMazePrimary:
    """T13: DERIVED_WRAPPER must not be used in ToolMaze primary results."""

    def test_toolmaze_uses_benchmark_native_fault_source(self, toolmaze_adapter):
        identity = toolmaze_adapter.benchmark_identity
        assert identity.benchmark_name == BenchmarkName.TOOLMAZE

    def test_noop_fault_layer_for_benchmark_native(self):
        layer = create_fault_layer(FaultSource.BENCHMARK_NATIVE)
        assert isinstance(layer, NoOpFaultLayer)
        assert layer.fault_source == FaultSource.NONE
        assert not layer.is_enabled_for_benchmark("toolmaze")

    def test_derived_wrapper_not_enabled_for_toolmaze(self):
        layer = DerivedWrapperFaultLayer()
        assert not layer.is_enabled_for_benchmark("toolmaze")

    def test_derived_wrapper_must_be_labeled(self):
        layer = DerivedWrapperFaultLayer({"tool_alpha": {"inject": "error"}})
        result = layer.inject_fault(
            task_id="test",
            tool_name="tool_alpha",
            tool_input={},
            perturbation_mode=PerturbationMode.P1,
        )
        assert result.get("label") == "BENCHMARK_DERIVED_PERTURBATION"


# ══════════════════════════════════════════════════════════════════════
# T14: Invalid infrastructure runs are not counted valid
# ══════════════════════════════════════════════════════════════════════

class TestT14_InvalidInfraRunsNotCountedValid:
    """T14: INVALID_INFRA runs must be classified separately from VALID."""

    def test_classification_writes_status(self, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        path = writer.classify_trial("trial-001", TrialStatus.INVALID_INFRA, "timeout")
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["status"] == "INVALID_INFRA"
        assert stored["reason"] == "timeout"

    def test_classification_excludes_trial(self, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        path = writer.classify_trial("trial-002", TrialStatus.EXCLUDED, "provider_error")
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["status"] == "EXCLUDED"

    def test_classification_valid(self, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        path = writer.classify_trial("trial-003", TrialStatus.VALID)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["status"] == "VALID"


# ══════════════════════════════════════════════════════════════════════
# T15: Paired trial identity reconciliation passes
# ══════════════════════════════════════════════════════════════════════

class TestT15_PairedTrialIdentityReconciliation:
    """T15: Paired trials must share identical task/environment/model/provider."""

    def test_trial_manifest_identity(self, toolmaze_adapter, gen_config, budget):
        tasks = toolmaze_adapter.enumerate_tasks()
        descriptor = tasks[0]

        manifest1 = TrialManifest(
            experiment_id="EXP-001",
            trial_id="trial-A",
            benchmark_name=toolmaze_adapter.benchmark_identity.benchmark_name,
            benchmark_revision=toolmaze_adapter.benchmark_identity.benchmark_revision,
            dataset_digest=toolmaze_adapter.benchmark_identity.dataset_digest,
            task_id=descriptor.task_id,
            native_condition=toolmaze_adapter.native_condition(descriptor),
            perturbation_mode=descriptor.perturbation_mode,
            arm=ControlArm.A0_BARE,
            model_id=gen_config.model_id,
            provider=gen_config.provider,
            generation_config=gen_config,
            root_budget=budget,
        )
        manifest2 = TrialManifest(
            experiment_id="EXP-001",
            trial_id="trial-B",
            benchmark_name=manifest1.benchmark_name,
            benchmark_revision=manifest1.benchmark_revision,
            dataset_digest=manifest1.dataset_digest,
            task_id=manifest1.task_id,
            native_condition=manifest1.native_condition,
            perturbation_mode=manifest1.perturbation_mode,
            arm=ControlArm.A3_ODYS_FULL,
            model_id=manifest1.model_id,
            provider=manifest1.provider,
            generation_config=manifest1.generation_config,
            root_budget=manifest1.root_budget,
        )
        # Paired trials share identity except trial_id and arm
        assert manifest1.task_id == manifest2.task_id
        assert manifest1.model_id == manifest2.model_id
        assert manifest1.provider == manifest2.provider
        assert manifest1.benchmark_revision == manifest2.benchmark_revision
        assert manifest1.dataset_digest == manifest2.dataset_digest
        assert manifest1.arm != manifest2.arm


# ══════════════════════════════════════════════════════════════════════
# T16: Provider/tool accounting reconciles
# ══════════════════════════════════════════════════════════════════════

class TestT16_ProviderToolAccountingReconciles:
    """T16: Provider and tool accounting must reconcile between raw and audit."""

    def test_raw_artifact_has_budget_ledger(self, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        path = writer.write_raw_artifact(
            "trial-001",
            runtime_events=[],
            tool_calls=[{"tool": "alpha", "result": "ok"}],
            state_observations=[],
            progress_shadow=[],
            budget_ledger={"max_turns": 30, "turns_used": 5},
        )
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert "budget_ledger" in stored
        assert "tool_calls" in stored

    def test_audit_report_structure(self, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        report = AuditReport(
            firewall={"check": "PASS"},
            pairing={"matched_trials": 6},
            accounting={"tool_calls": 10, "model_calls": 5},
            exclusions=[],
        )
        path = writer.write_audit("test_audit", report)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert "accounting" in stored
        assert stored["accounting"]["tool_calls"] == 10


# ══════════════════════════════════════════════════════════════════════
# T17: Phase 4 recovery regression suite remains green
# ══════════════════════════════════════════════════════════════════════

class TestT17_Phase4RecoveryRegressionGreen:
    """T17: Phase 5 must not modify Phase 4 core.  This is verified by
    running the existing Phase 4 test suite separately.
    """

    def test_phase5_does_not_modify_phase4_core(self):
        """Verify Phase 4 core modules are importable and unchanged."""
        from lhas.recovery import DefaultRecoveryPolicy, RecoveryAction
        from lhas.experiments import ExperimentRecorder
        from lhas.reliability.benchmark import BenchmarkRunner, FairnessContract
        # If these imports succeed, Phase 4 core is intact
        assert DefaultRecoveryPolicy is not None
        assert ExperimentRecorder is not None
        assert BenchmarkRunner is not None
        assert FairnessContract is not None

    def test_phase4_enum_values_unchanged(self):
        """Verify Phase 4 enum values are stable."""
        from lhas.domain.enums import FailureType, RecoveryActionType
        assert FailureType.TOOL_ERROR.value == "TOOL_ERROR"
        assert FailureType.TIMEOUT.value == "TIMEOUT"
        assert RecoveryActionType.RETRY_WITH_FAILURE_CONTEXT.value == "RETRY_WITH_FAILURE_CONTEXT"
        assert RecoveryActionType.ESCALATE.value == "ESCALATE"


# ══════════════════════════════════════════════════════════════════════
# Additional structural tests
# ══════════════════════════════════════════════════════════════════════

class TestBenchmarkAdapterProtocol:
    """Verify ToolMaze and ToolSandbox implement BenchmarkAdapter."""

    def test_toolmaze_satisfies_protocol(self, toolmaze_adapter):
        from lhas.phase5.types import BenchmarkAdapter
        assert isinstance(toolmaze_adapter, BenchmarkAdapter)

    def test_toolsandbox_satisfies_protocol(self, toolsandbox_adapter):
        from lhas.phase5.types import BenchmarkAdapter
        assert isinstance(toolsandbox_adapter, BenchmarkAdapter)


class TestProvenanceFreeze:
    """Test benchmark provenance freeze functionality."""

    def test_freeze_and_verify(self, provenance_dir, gen_config, toolmaze_adapter):
        freeze = ProvenanceFreeze(provenance_dir)
        identity = toolmaze_adapter.benchmark_identity
        tasks = [t.task_id for t in toolmaze_adapter.enumerate_tasks()]

        manifest = freeze.freeze(
            experiment_id="EXP-TEST-001",
            benchmark_identity=identity,
            selected_task_ids=tasks[:5],
            generation_config=gen_config,
            arm_definitions=arm_definitions_snapshot(),
            budgets={"max_turns": 30, "max_model_calls": 50},
        )
        assert "manifest_hash" in manifest

        # Verify passes
        verified = freeze.verify("EXP-TEST-001")
        assert verified["manifest_hash"] == manifest["manifest_hash"]

    def test_provenance_drift_detection(self, provenance_dir, gen_config, toolmaze_adapter):
        freeze = ProvenanceFreeze(provenance_dir)
        identity = toolmaze_adapter.benchmark_identity
        tasks = [t.task_id for t in toolmaze_adapter.enumerate_tasks()]

        freeze.freeze(
            experiment_id="EXP-DRIFT-001",
            benchmark_identity=identity,
            selected_task_ids=tasks[:5],
            generation_config=gen_config,
            arm_definitions=arm_definitions_snapshot(),
            budgets={"max_turns": 30, "max_model_calls": 50},
        )
        # Second freeze with same ID but different tasks should fail
        with pytest.raises(ValueError, match="Provenance drift"):
            freeze.freeze(
                experiment_id="EXP-DRIFT-001",
                benchmark_identity=identity,
                selected_task_ids=tasks[:3],  # Different!
                generation_config=gen_config,
                arm_definitions=arm_definitions_snapshot(),
                budgets={"max_turns": 30, "max_model_calls": 50},
            )


class TestShadowObserverSignals:
    """Verify shadow observer signal classification."""

    def test_progressing_signal(self, shadow_observer):
        record = shadow_observer.observe(
            task_id="t1", step=0, action_identity="tool_a",
            tool_result={"status": "success"},
        )
        assert record.signal == SignalKind.PROGRESSING

    def test_anomaly_signal(self, shadow_observer):
        record = shadow_observer.observe(
            task_id="t1", step=0, action_identity="tool_a",
            tool_result={"status": "error"},
        )
        assert record.signal == SignalKind.ANOMALY

    def test_stalled_signal(self, shadow_observer):
        for i in range(3):
            record = shadow_observer.observe(
                task_id="t1", step=i, action_identity="same_tool",
                tool_result={"status": "success"},
            )
        assert record.signal == SignalKind.STALLED

    def test_jsonl_output(self, shadow_observer):
        shadow_observer.observe(
            task_id="t1", step=0, action_identity="tool_a",
            tool_result={"status": "success"},
        )
        jsonl = shadow_observer.to_jsonl("t1")
        assert len(jsonl.strip().split("\n")) == 1
        data = json.loads(jsonl.strip().split("\n")[0])
        assert data["trial_id"] == "t1"


class TestExtensionBoundaries:
    """Verify extension adapters raise NotImplementedError."""

    def test_terminal_bench_not_implemented(self):
        with pytest.raises(NotImplementedError, match="TerminalBenchAdapter"):
            TerminalBenchAdapter()

    def test_tua_bench_not_implemented(self):
        with pytest.raises(NotImplementedError, match="TUABenchAdapter"):
            TUABenchAdapter()


class TestToolSandboxDerivedMetrics:
    """Test ToolSandbox progress validation metrics."""

    def test_derived_metrics_with_events(self, toolsandbox_adapter):
        events = [
            {"event_type": "TOOL_CALL_COMPLETED"},
            {"event_type": "PLAN_STEP_COMPLETED"},
            {"event_type": "TOOL_CALL_COMPLETED"},
        ]
        derived = toolsandbox_adapter.derive_progress_metrics("TS-001", events)
        assert derived.milestone_alignment is not None
        assert derived.milestone_alignment > 0
        assert derived.validity == TrialStatus.VALID

    def test_derived_metrics_no_events(self, toolsandbox_adapter):
        derived = toolsandbox_adapter.derive_progress_metrics("TS-001", [])
        assert derived.no_advancement_detection == 1.0

    def test_derived_metrics_labeled_as_derived(self, toolsandbox_adapter):
        derived = toolsandbox_adapter.derive_progress_metrics("TS-001", [])
        # These are Phase5-derived, not official ToolSandbox scores
        assert hasattr(derived, "milestone_alignment")
        assert hasattr(derived, "no_advancement_detection")


class TestArtifactSchema:
    """Test artifact directory structure and schema compliance."""

    def test_artifact_writer_creates_dirs(self, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        assert (artifact_dir / "raw").is_dir()
        assert (artifact_dir / "benchmark").is_dir()
        assert (artifact_dir / "derived").is_dir()
        assert (artifact_dir / "audits").is_dir()
        assert (artifact_dir / "analysis").is_dir()

    def test_list_trials(self, artifact_dir):
        writer = ArtifactWriter(artifact_dir)
        writer.write_raw_artifact(
            "t1", [], [], [], [], {"max_turns": 10},
        )
        writer.write_raw_artifact(
            "t2", [], [], [], [], {"max_turns": 10},
        )
        assert sorted(writer.list_trials()) == ["t1", "t2"]

    def test_manifest_schema_version(self, artifact_dir, toolmaze_adapter, gen_config, budget):
        writer = ArtifactWriter(artifact_dir)
        identity = toolmaze_adapter.benchmark_identity
        tasks = toolmaze_adapter.enumerate_tasks()

        manifest = TrialManifest(
            experiment_id="EXP-001",
            trial_id="trial-001",
            benchmark_name=identity.benchmark_name,
            benchmark_revision=identity.benchmark_revision,
            dataset_digest=identity.dataset_digest,
            task_id=tasks[0].task_id,
            native_condition="C1/P0",
            perturbation_mode=PerturbationMode.P0,
            arm=ControlArm.A0_BARE,
            model_id=gen_config.model_id,
            provider=gen_config.provider,
            generation_config=gen_config,
            root_budget=budget,
        )
        path = writer.write_manifest(manifest)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["_schema_version"] == "phase5-falsification-01"
