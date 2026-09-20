import asyncio
from pathlib import Path

from evals.reliability.phase4_v1.validate import validate_raw_result
from evals.reliability.real_executor import RealExecutorAdapter
from evals.reliability.run_phase4 import (
    ExecutionRequest,
    FaultInjector,
    FixtureManager,
    ProtocolSnapshot,
    select_runs,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "evals" / "reliability" / "phase4_v1"


class RuntimeResult:
    def __init__(self, *, failure=False):
        self.status = "FAILURE" if failure else "SUCCESS"
        self.completion_claim = not failure
        self.error_type = "TOOL_ERROR" if failure else None
        self.safe_trace = [
            {"event": "TOOL_STARTED", "capability": "workspace.read"},
            {"event": "TOOL_COMPLETED", "capability": "workspace.read", "status": self.status},
        ]
        self.tool_call_count = 2
        self.turn_count = 1
        self.usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


class Runtime:
    def __init__(self, mode, seen, *, failure=False):
        self.mode = mode
        self.seen = seen
        self.failure = failure

    async def execute(self, request):
        self.seen.append((self.mode, request))
        return RuntimeResult(failure=self.failure)


def _request(config_name):
    snapshot = ProtocolSnapshot.load(PROTOCOL_ROOT)
    spec = select_runs(snapshot, task_id="CI-01", config_name=config_name, repeat_index=1)[0]
    fixture = FixtureManager(snapshot).prepare(spec.task)
    fault = FaultInjector(snapshot).plan_for(spec.task)
    return ExecutionRequest(
        run_id=spec.run_id,
        repeat_index=spec.repeat_index,
        task=spec.task,
        config=spec.config,
        fixture=fixture,
        fault=fault,
        fault_context=FaultInjector(snapshot).context_for(spec.task),
    )


def test_real_adapter_contract_is_compatible_with_phase4_outcome():
    seen = []
    adapter = RealExecutorAdapter(
        minimal_runtime_factory=lambda request: Runtime("minimal", seen),
        odys_runtime_factory=lambda request: Runtime("odys_p3", seen),
        state_reader=lambda request, result: {"artifact": "present", "tests": "pass"},
    )
    result = asyncio.run(adapter.execute(_request("minimal")))
    assert set(("run_id", "task_id", "config", "execution_trace", "tool_events", "final_state", "validator_input")) <= set(result)
    assert result["config"] == "minimal"
    assert result["validator_input"] == {
        "final_state": result["final_state"],
        "tool_events": result["tool_events"],
    }
    assert result["observed_state"] == result["final_state"]


def test_minimal_and_odys_use_separate_authority_factories_with_same_inputs():
    seen = []
    adapter = RealExecutorAdapter(
        minimal_runtime_factory=lambda request: Runtime("minimal", seen),
        odys_runtime_factory=lambda request: Runtime("odys_p3", seen),
    )
    minimal_request = _request("minimal")
    odys_request = _request("odys_p3")
    asyncio.run(adapter.execute(minimal_request))
    asyncio.run(adapter.execute(odys_request))

    assert [mode for mode, _ in seen] == ["minimal", "odys_p3"]
    assert minimal_request.task == odys_request.task
    assert minimal_request.fixture == odys_request.fixture
    assert minimal_request.fault == odys_request.fault
    assert not any(minimal_request.config["features"].values())
    assert all(odys_request.config["features"].values())


def test_validator_observes_same_external_shape_for_both_configs():
    seen = []
    adapter = RealExecutorAdapter(
        minimal_runtime_factory=lambda request: Runtime("minimal", seen),
        odys_runtime_factory=lambda request: Runtime("odys_p3", seen),
        state_reader=lambda request, result: {"artifact": "present", "tests": "pass"},
    )
    minimal = asyncio.run(adapter.execute(_request("minimal")))
    odys = asyncio.run(adapter.execute(_request("odys_p3")))
    assert minimal["validator_input"].keys() == odys["validator_input"].keys()
    assert minimal["validator_input"]["final_state"] == odys["validator_input"]["final_state"]
    assert minimal["validator_input"]["tool_events"] == odys["validator_input"]["tool_events"]


def test_runtime_failure_is_returned_as_benchmark_failure():
    seen = []
    adapter = RealExecutorAdapter(
        minimal_runtime_factory=lambda request: Runtime("minimal", seen, failure=True),
        odys_runtime_factory=lambda request: Runtime("odys_p3", seen),
    )
    result = asyncio.run(adapter.execute(_request("minimal")))
    assert result["failure_type"] == "TOOL_ERROR"
    assert result["claimed_complete"] is False
    assert result["execution_trace"]
