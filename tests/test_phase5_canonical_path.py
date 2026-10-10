"""Canonical execution path identity tests.

Proves that CanaryRunner and RealPilotRunner both dispatch to the
SAME canonical ``lhas.phase5.trial_executor.execute_trial`` function.

Also runs a provider-free smoke test using ScriptedModelDriver.

CANARY_EXECUTOR_SYMBOL=lhas.phase5.trial_executor.execute_trial
PILOT_EXECUTOR_SYMBOL=lhas.phase5.trial_executor.execute_trial
CANONICAL_EXPERIMENT_EXECUTOR=lhas.phase5.trial_executor.execute_trial
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════
#  Conditional imports
# ══════════════════════════════════════════════════════════════════════

def _try_import(module_name: str):
    try:
        return importlib.import_module(module_name)
    except (ModuleNotFoundError, ImportError):
        return None


_trial_executor_mod = _try_import("lhas.phase5.trial_executor")
_real_pilot_runner_mod = _try_import("lhas.phase5.real_pilot_runner")
_canary_runner_mod = _try_import("lhas.phase5.canary_runner")
_model_driver_mod = _try_import("lhas.phase5.model_driver")
_control_arms_mod = _try_import("lhas.phase5.control_arms")
_types_mod = _try_import("lhas.phase5.types")

HAS_TRIAL_EXECUTOR = _trial_executor_mod is not None
HAS_PILOT_RUNNER = _real_pilot_runner_mod is not None
HAS_CANARY_RUNNER = _canary_runner_mod is not None
HAS_MODEL_DRIVER = _model_driver_mod is not None
HAS_CONTROL_ARMS = _control_arms_mod is not None
HAS_TYPES = _types_mod is not None

requires_trial_executor = pytest.mark.skipif(
    not HAS_TRIAL_EXECUTOR, reason="trial_executor not available"
)
requires_pilot_runner = pytest.mark.skipif(
    not HAS_PILOT_RUNNER, reason="real_pilot_runner not available"
)
requires_canary_runner = pytest.mark.skipif(
    not HAS_CANARY_RUNNER, reason="canary_runner not available"
)
requires_model_driver = pytest.mark.skipif(
    not HAS_MODEL_DRIVER, reason="model_driver not available"
)
requires_all = pytest.mark.skipif(
    not all([HAS_TRIAL_EXECUTOR, HAS_MODEL_DRIVER, HAS_CONTROL_ARMS, HAS_TYPES]),
    reason="Core modules not all available"
)


# ══════════════════════════════════════════════════════════════════════
#  T1: Import identity — both runners import the same execute_trial
# ══════════════════════════════════════════════════════════════════════

class TestCanonicalPathIdentity:
    """Prove that canary and pilot use the exact same execute_trial."""

    @requires_trial_executor
    @requires_canary_runner
    def test_canary_imports_canonical_execute_trial(self):
        """CanaryRunner imports execute_trial from trial_executor.

        We verify via AST to avoid importing canary_runner (which modifies
        sys.path at module level, polluting other tests).
        """
        import ast
        canary_src = Path(__file__).resolve().parent.parent / "src" / "lhas" / "phase5" / "canary_runner.py"
        tree = ast.parse(canary_src.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "trial_executor" in node.module:
                    for alias in node.names:
                        if alias.name == "execute_trial":
                            imports.append(node.module)
        assert len(imports) >= 1, (
            "canary_runner.py does not import execute_trial from trial_executor"
        )

    @requires_trial_executor
    @requires_pilot_runner
    def test_pilot_imports_canonical_execute_trial(self):
        """RealPilotRunner's execute_trial comes from trial_executor module."""
        from lhas.phase5.trial_executor import execute_trial as canonical_fn
        import lhas.phase5.real_pilot_runner as pilot_mod
        pilot_fn = pilot_mod.execute_trial
        assert pilot_fn is canonical_fn, (
            f"PILOT_EXECUTOR_SYMBOL mismatch: "
            f"real_pilot_runner.execute_trial={id(pilot_fn)} != "
            f"trial_executor.execute_trial={id(canonical_fn)}"
        )

    @requires_trial_executor
    @requires_pilot_runner
    def test_pilot_canonical_function_identity(self):
        """RealPilotRunner dispatches to the SAME function object as TrialExecutor."""
        import lhas.phase5.real_pilot_runner as pilot_mod
        import lhas.phase5.trial_executor as executor_mod

        pilot_fn = pilot_mod.execute_trial
        canonical_fn = executor_mod.execute_trial

        assert pilot_fn is canonical_fn, (
            f"OBJECT_IDENTITY_OR_IMPORT_IDENTITY=FAIL: "
            f"pilot={id(pilot_fn)}, canonical={id(canonical_fn)}"
        )

    @requires_canary_runner
    def test_canary_also_imports_from_trial_executor(self):
        """CanaryRunner source code imports from trial_executor (AST check)."""
        import ast
        canary_src = Path(__file__).resolve().parent.parent / "src" / "lhas" / "phase5" / "canary_runner.py"
        tree = ast.parse(canary_src.read_text(encoding="utf-8"))
        has_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "trial_executor" in node.module:
                    for alias in node.names:
                        if alias.name == "execute_trial":
                            has_import = True
        assert has_import, (
            "CANARY_EXECUTOR_SYMBOL: canary_runner.py must import execute_trial from trial_executor"
        )

    @requires_pilot_runner
    def test_pilot_has_no_ToolMazeRuntimeBackend_import(self):
        """RealPilotRunner must NOT import ToolMazeRuntimeBackend."""
        import lhas.phase5.real_pilot_runner as pilot_mod
        has_backend = hasattr(pilot_mod, "ToolMazeRuntimeBackend")
        assert not has_backend, (
            "RealPilotRunner still has ToolMazeRuntimeBackend in namespace"
        )

    @requires_pilot_runner
    def test_pilot_has_no_BudgetExhausted_import(self):
        """RealPilotRunner must NOT import BudgetExhausted directly."""
        import lhas.phase5.real_pilot_runner as pilot_mod
        has_budget_exhausted = hasattr(pilot_mod, "BudgetExhausted")
        assert not has_budget_exhausted, (
            "RealPilotRunner still has BudgetExhausted in namespace"
        )

    @requires_pilot_runner
    def test_pilot_has_no__STRATEGY_MAP_import(self):
        """RealPilotRunner must NOT import _STRATEGY_MAP directly."""
        import lhas.phase5.real_pilot_runner as pilot_mod
        has_strategy_map = hasattr(pilot_mod, "_STRATEGY_MAP")
        assert not has_strategy_map, (
            "RealPilotRunner still has _STRATEGY_MAP in namespace"
        )

    @requires_pilot_runner
    @requires_trial_executor
    def test_neither_runner_creates_execution_engine_directly(self):
        """Neither runner imports ExecutionEngine directly (AST check for canary)."""
        import ast
        import lhas.phase5.real_pilot_runner as pilot_mod

        # Pilot: check namespace
        assert not hasattr(pilot_mod, "ExecutionEngine"), (
            "RealPilotRunner should not import ExecutionEngine"
        )

        # Canary: check source AST (avoid importing due to sys.path side effects)
        canary_src = Path(__file__).resolve().parent.parent / "src" / "lhas" / "phase5" / "canary_runner.py"
        tree = ast.parse(canary_src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "sandbox" in node.module:
                    for alias in node.names:
                        if alias.name == "ExecutionEngine":
                            assert False, "CanaryRunner imports ExecutionEngine directly"


# ══════════════════════════════════════════════════════════════════════
#  T2: Provider-free smoke — ScriptedModelDriver through RealPilotRunner
# ══════════════════════════════════════════════════════════════════════

def _make_frozen_task_json() -> dict:
    """Create a minimal frozen task JSON matching ToolMaze schema."""
    return {
        "task_id": "smoke_C1_task_001",
        "template_id": "C1_template_001",
        "complexity": "C1",
        "task_description": "Check the status of flight CA1234",
        "user_input": {"query": "Check the status of flight CA1234"},
        "execution_trace": [
            {
                "step_id": 1,
                "tool_name": "search_flight",
                "tool_arguments": {"flight_number": "CA1234"},
                "expected_output": {"status": "on_time"},
            }
        ],
        "evaluation": {
            "grader_type": "toolmaze_judge",
            "expected_tool_sequence": ["search_flight"],
            "success_criteria": {"tool_calls_completed": True},
        },
    }


@pytest.fixture(autouse=True)
def _isolate_sys_path_and_modules():
    """Save/restore sys.path and sys.modules to prevent test pollution.

    RealPilotRunner.__init__ modifies sys.path and caches 'tools.loader'
    when it finds the real ToolMaze repo. This would break tests that
    rely on the fixture ToolLoader.
    """
    saved_path = sys.path.copy()
    saved_tools_modules = {
        k: v for k, v in sys.modules.items()
        if k == "tools" or k.startswith("tools.")
    }
    yield
    # Restore sys.path
    sys.path[:] = saved_path
    # Remove any tools.* modules that were added during the test
    to_remove = [k for k in sys.modules if k == "tools" or k.startswith("tools.")]
    for k in to_remove:
        if k not in saved_tools_modules:
            del sys.modules[k]
        else:
            sys.modules[k] = saved_tools_modules[k]


def _make_scripted_driver(actions: list):
    """Create a ScriptedModelDriver with the given scripted actions."""
    ScriptedAction = _model_driver_mod.ScriptedAction
    ScriptedModelDriver = _model_driver_mod.ScriptedModelDriver
    script = [ScriptedAction(**a) if isinstance(a, dict) else a for a in actions]
    return ScriptedModelDriver(script)


def _make_tool_definitions_from_task(task_json: dict) -> list:
    """Extract tool definitions from task's execution_trace."""
    tool_names = set()
    for step in task_json.get("execution_trace", []):
        if "tool_name" in step:
            tool_names.add(step["tool_name"])
    return [{"name": tn, "description": f"Tool: {tn}"} for tn in sorted(tool_names)]


class TestProviderFreeSmoke:
    """Provider-free smoke tests using ScriptedModelDriver.

    Runs one frozen task through RealPilotRunner for at least A0 and A3.
    Requires:
    - official ExecutionEngine used
    - TrialExecutor called
    - native grader produced
    - artifacts produced
    """

    @requires_all
    @requires_pilot_runner
    @pytest.mark.parametrize("arm_name", ["A0_BARE", "A3_ODYS_FULL"])
    def test_smoke_real_pilot_runner_a0_a3(self, arm_name: str):
        """Run one frozen task through RealPilotRunner for A0 and A3.

        Uses ScriptedModelDriver (no real provider).
        Verifies canonical execution path, grader, and artifacts.
        """
        from lhas.phase5.control_arms import ControlArm
        from lhas.phase5.types import BudgetConfig
        from lhas.phase5.real_pilot_runner import RealPilotRunner

        arm = ControlArm(arm_name)
        task_json = _make_frozen_task_json()
        tool_defs = _make_tool_definitions_from_task(task_json)

        # Create a scripted driver that does one tool call then final answer
        driver = _make_scripted_driver([
            {"type": "tool_call", "tool_name": "search_flight",
             "arguments": {"flight_number": "CA1234"}},
            {"type": "final_answer", "content": "Flight CA1234 is on time."},
        ])

        budget = BudgetConfig(max_turns=5, max_model_calls=10)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            manifest = {
                "schema_version": "phase5-real-pilot-001",
                "experiment_role": "PIPELINE_PILOT",
                "experiment_id": "smoke-test-001",
                "selected_task_ids": ["smoke_C1_task_001"],
                "tasks": ["smoke_C1_task_001"],
                "arm_definitions": {arm_name: {}},
                "arms": [arm_name],
                "generation_config": {
                    "model_id": "scripted-test",
                    "provider": "none",
                    "temperature": 0.0,
                },
                "budgets": {"max_turns": 5, "max_model_calls": 10},
                "root_budget": {"max_turns": 5, "max_model_calls": 10},
                "seed": 42,
            }
            manifest_path = tmpdir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            output_dir = tmpdir / "output"

            # Mock the adapter to return our task
            mock_adapter = MagicMock()
            mock_desc = MagicMock()
            mock_desc.task_id = "smoke_C1_task_001"
            mock_desc.topology.value = "C1"
            mock_desc.perturbation_mode.value = "P0"
            mock_adapter.enumerate_tasks.return_value = [mock_desc]
            mock_adapter._find_task.return_value = task_json
            mock_adapter.build_runtime_task.return_value = MagicMock(
                environment_snapshot={}
            )

            # Track whether execute_trial was called
            execution_called = []

            from lhas.phase5.trial_executor import execute_trial as canonical_fn

            def _capture_execute_trial(**kwargs):
                execution_called.append(kwargs)
                return canonical_fn(**kwargs)

            with patch(
                "lhas.phase5.real_pilot_runner.execute_trial",
                side_effect=_capture_execute_trial,
            ), patch(
                "lhas.phase5.trial_executor._run_offline_grader",
                return_value={
                    "judgement": {"pass": True},
                    "metrics_report": {"tsr": 1.0, "prr": 1.0, "rc": 1.0},
                    "metrics_summary": {"tsr": 1.0, "prr": 1.0, "rc": 1.0},
                    "grader_source": "FROZEN_TOOLMAZE",
                    "error": None,
                },
            ):
                runner = RealPilotRunner(
                    model_driver=driver,
                    manifest_path=manifest_path,
                    output_dir=output_dir,
                    adapter=mock_adapter,
                )

                result = runner.run_pilot(arms=[arm], max_rounds=5)

                # --- Assertions (inside tempfile context) ---

                # 1. TrialExecutor was called
                assert len(execution_called) == 1, (
                    f"Expected execute_trial to be called once, got {len(execution_called)}"
                )

                # 2. Canonical arguments were passed
                call_kwargs = execution_called[0]
                assert call_kwargs["arm"] == arm
                assert call_kwargs["task_json"] == task_json
                assert call_kwargs["experiment_id"] == "smoke-test-001"
                assert call_kwargs["task_id"] == "smoke_C1_task_001"

                # 3. Result is well-formed
                assert result["experiment_id"] == "smoke-test-001"
                assert result["task_count"] == 1
                assert result["arm_count"] == 1
                assert result["errors"] == 0

                # 4. Artifacts were produced
                assert output_dir.exists()

                trial_dirs = [
                    d for d in output_dir.glob("*_*")
                    if d.is_dir() and (d / "official_trace.json").exists()
                ]
                assert len(trial_dirs) >= 1, (
                    f"No trial artifact directories found in {output_dir}"
                )

                trial_dir = trial_dirs[0]
                assert (trial_dir / "official_trace.json").exists()
                assert (trial_dir / "derived_runtime_view.json").exists()
                assert (trial_dir / "native_judgement.json").exists()
                assert (trial_dir / "validity.json").exists()
                assert (trial_dir / "trial_manifest.json").exists()

                # 5. Validity classification
                validity_data = json.loads(
                    (trial_dir / "validity.json").read_text()
                )
                assert validity_data["validity"] in ("VALID", "INVALID_INFRA")


# ══════════════════════════════════════════════════════════════════════
#  T3: Documentation constants
# ══════════════════════════════════════════════════════════════════════

class TestDocumentationConstants:
    """Verify the canonical executor is documented."""

    @requires_trial_executor
    def test_trial_executor_is_single_canonical_path(self):
        """execute_trial docstring states it is the ONLY function."""
        from lhas.phase5.trial_executor import execute_trial
        doc = execute_trial.__doc__ or ""
        assert "canonical" in doc.lower() or "only" in doc.lower(), (
            "execute_trial should document itself as the canonical/only path"
        )