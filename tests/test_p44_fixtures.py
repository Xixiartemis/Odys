"""Tests for executable fixture system (P44).

Validates:
- All 60 task_ids have registered fixtures
- setup/reset lifecycle for each family
- inject_fault for each fault type
- observe returns valid dict
"""
import pytest
from pathlib import Path
from evals.reliability.fixture_packages import FixtureRegistry, BaseFixture


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def registry() -> FixtureRegistry:
    return FixtureRegistry()


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    """Use pytest-managed temporary storage on fresh local and CI checkouts."""
    d = tmp_path / "p44_workspace"
    d.mkdir(parents=True, exist_ok=True)
    return d


EXPECTED_TASK_IDS = sorted([
    f"CI-{i:02d}" for i in range(1, 11)
] + [
    f"ESR-{i:02d}" for i in range(1, 11)
] + [
    f"CWR-{i:02d}" for i in range(1, 11)
] + [
    f"PTF-{i:02d}" for i in range(1, 11)
] + [
    f"RTP-{i:02d}" for i in range(1, 11)
] + [
    f"DL-{i:02d}" for i in range(1, 11)
])


# ---------------------------------------------------------------------------
# 1. All 60 task_ids have fixtures
# ---------------------------------------------------------------------------

def test_fixture_count(registry: FixtureRegistry):
    assert len(registry) == 60, f"Expected 60 fixtures, got {len(registry)}"


def test_all_task_ids_registered(registry: FixtureRegistry):
    actual = registry.all_task_ids()
    assert actual == EXPECTED_TASK_IDS, (
        f"Task ID mismatch.\n"
        f"Missing: {set(EXPECTED_TASK_IDS) - set(actual)}\n"
        f"Extra:   {set(actual) - set(EXPECTED_TASK_IDS)}"
    )


def test_each_fixture_is_basesubclass(registry: FixtureRegistry):
    for tid in registry.all_task_ids():
        fixture = registry.get(tid)
        assert isinstance(fixture, BaseFixture), f"{tid} is not a BaseFixture"
        assert fixture.task_id == tid
        assert fixture.family, f"{tid} has empty family"
        assert fixture.fixture_id, f"{tid} has empty fixture_id"
        assert fixture.fault_ids, f"{tid} has empty fault_ids"


# ---------------------------------------------------------------------------
# 2. Setup/reset cycle (one per family = 6 tests)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sample_task_id", [
    "CI-01", "ESR-01", "CWR-01", "PTF-01", "RTP-01", "DL-01",
], ids=lambda tid: tid.split("-")[0])
def test_setup_reset_cycle(registry: FixtureRegistry, sample_task_id: str, workspace_root: Path):
    fixture = registry.get(sample_task_id)
    ws = workspace_root / f"ws_{sample_task_id}"
    ws.mkdir()

    # setup creates files
    result = fixture.setup(ws)
    assert isinstance(result, dict), f"{sample_task_id}.setup() must return dict"
    files = list(ws.rglob("*"))
    assert len(files) > 0, f"{sample_task_id}.setup() created no files"

    # reset removes all files
    fixture.reset(ws)
    remaining = [f for f in ws.rglob("*") if f.is_file()]
    assert len(remaining) == 0, (
        f"{sample_task_id}.reset() left {len(remaining)} files behind"
    )


# ---------------------------------------------------------------------------
# 3. inject_fault for each fault type
# ---------------------------------------------------------------------------

FAULT_IDS = sorted([
    "FAIL_TOOL_ON_CALL_1",
    "FAIL_TOOL_ON_CALL_2",
    "INTERRUPT_AFTER_EFFECT",
    "PROVIDER_TIMEOUT_ON_CALL_1",
    "PROVIDER_UNAVAILABLE",
    "QUOTA_EXHAUSTED",
    "MALFORMED_RESPONSE",
    "INVALIDATE_ASSUMPTION",
    "STALE_WORKSPACE_BEFORE_DISPATCH",
    "CAPABILITY_UNAVAILABLE",
    "DUPLICATE_DELIVERY_ATTEMPT",
    "PARTIAL_OUTPUT",
])

# Pick one task per fault_id for parametrized testing
FAULT_SAMPLES = {
    "FAIL_TOOL_ON_CALL_1": "ESR-01",
    "FAIL_TOOL_ON_CALL_2": "CI-09",
    "INTERRUPT_AFTER_EFFECT": "ESR-02",
    "PROVIDER_TIMEOUT_ON_CALL_1": "PTF-01",
    "PROVIDER_UNAVAILABLE": "PTF-02",
    "QUOTA_EXHAUSTED": "CI-10",
    "MALFORMED_RESPONSE": "CI-07",
    "INVALIDATE_ASSUMPTION": "CWR-01",
    "STALE_WORKSPACE_BEFORE_DISPATCH": "CI-04",
    "CAPABILITY_UNAVAILABLE": "PTF-09",
    "DUPLICATE_DELIVERY_ATTEMPT": "DL-02",
    "PARTIAL_OUTPUT": "CI-01",
}


@pytest.mark.parametrize(
    "fault_id,task_id",
    list(FAULT_SAMPLES.items()),
    ids=[k for k in FAULT_SAMPLES],
)
def test_inject_fault(registry: FixtureRegistry, fault_id: str, task_id: str, workspace_root: Path):
    fixture = registry.get(task_id)
    assert fault_id in fixture.fault_ids, (
        f"{task_id} does not list {fault_id} in fault_ids"
    )
    ws = workspace_root / f"ws_{fault_id}"
    ws.mkdir(exist_ok=True)
    fixture.setup(ws)

    # inject_fault should not raise
    fixture.inject_fault(ws, fault_id)

    # cleanup
    fixture.reset(ws)


# ---------------------------------------------------------------------------
# 4. observe returns valid dict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sample_task_id", [
    "CI-01", "ESR-01", "CWR-01", "PTF-01", "RTP-01", "DL-01",
], ids=lambda tid: tid.split("-")[0])
def test_observe_returns_valid_dict(registry: FixtureRegistry, sample_task_id: str, workspace_root: Path):
    fixture = registry.get(sample_task_id)
    ws = workspace_root / f"ws_obs_{sample_task_id}"
    ws.mkdir()
    fixture.setup(ws)

    result = fixture.observe(ws)
    assert isinstance(result, dict), f"{sample_task_id}.observe() must return dict"
    assert len(result) > 0, f"{sample_task_id}.observe() returned empty dict"
    assert "expected_effect" in result, (
        f"{sample_task_id}.observe() missing 'expected_effect' key"
    )

    fixture.reset(ws)
