import asyncio
import json
from pathlib import Path

from lhas.reliability import BenchmarkResult, BenchmarkScenario, ContractBenchmarkRunner


ROOT = Path(__file__).resolve().parents[1]


def _scenarios():
    return [BenchmarkScenario.model_validate(json.loads(path.read_text(encoding="utf-8"))) for path in sorted((ROOT / "evals" / "reliability").glob("*.json"))]


def test_three_reliability_families_are_declared():
    scenarios = _scenarios()
    assert {item.family.value for item in scenarios} == {"execution_state_recovery", "completion_integrity", "delegation_lifecycle", "runtime_target_truth", "provider_quota_exhaustion", "complex_workflow_replan"}
    assert all("odys" not in item.fairness.acceptance_validator.casefold() for item in scenarios)


def test_hermes_and_odys_runner_receive_same_frozen_contract():
    scenario = _scenarios()[0]
    seen = []

    def make(name):
        def run(item):
            seen.append((name, item.fairness.model_dump(mode="json")))
            return BenchmarkResult(scenario_id=item.scenario_id, runner=name, completed=True, metrics={metric: False for metric in item.metric_names}, invariant_results={})
        return ContractBenchmarkRunner(name, run)

    asyncio.run(make("hermes").run(scenario))
    asyncio.run(make("odys").run(scenario))
    assert seen[0][1] == seen[1][1]


def test_runner_fails_closed_when_metric_is_missing():
    scenario = _scenarios()[0]
    runner = ContractBenchmarkRunner("odys", lambda item: BenchmarkResult(scenario_id=item.scenario_id, runner="odys", completed=False, metrics={}, invariant_results={}))
    import pytest
    with pytest.raises(ValueError, match="missing metrics"):
        asyncio.run(runner.run(scenario))
