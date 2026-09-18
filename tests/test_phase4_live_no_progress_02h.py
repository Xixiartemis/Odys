from __future__ import annotations

import asyncio
import json

from scripts.phase4_live_no_progress_02h import (
    EXPECTED_RUNS,
    EXPERIMENT_ID,
    PROVIDER_TIMEOUT_CEILING_SECONDS,
    ROOT_TIMEOUT_SECONDS,
    _preflight,
    _timeout_regression,
)


def test_02h_preflight_isolates_live_deadlines_without_provider(tmp_path):
    output = tmp_path / "02h-preflight"
    report = _preflight(output)

    assert report["experiment_id"] == EXPERIMENT_ID
    assert report["expected_runs"] == EXPECTED_RUNS == 6
    assert report["shared_effect_policy"] is True
    assert report["policy_instance_count"] == 6
    assert report["unique_effect_policies"] == 6
    assert report["root_timeout_seconds"] == ROOT_TIMEOUT_SECONDS == 900.0
    assert report["provider_timeout_ceiling_seconds"] == PROVIDER_TIMEOUT_CEILING_SECONDS == 300.0
    assert report["root_timeout_gt_provider_timeout"] is True
    assert report["other_effective_config_diff"] == []
    assert report["provider_executed"] is False
    assert report["output_created"] is False
    assert not output.exists()


def test_02h_sdk_timeout_is_consumed_and_typed():
    report = asyncio.run(_timeout_regression())

    assert report["simulated_provider_timeout"] is True
    assert report["unretrieved_task_exception"] is False
    assert report["failure_classification"] == "PROVIDER_TIMEOUT"
    assert report["root_control_remains_consistent"] is True
    json.dumps(report)
