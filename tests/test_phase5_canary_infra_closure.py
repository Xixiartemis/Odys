"""Provider-free adversarial tests for the live-canary infrastructure gate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from lhas.phase5.canary_runner import (
    CanaryPreflightError,
    _manifest_hash,
    preflight_canary,
    run_canary,
)
from lhas.phase5.provider_lock import ProviderLockError, validate_resolved_config


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "experiments" / "phase5" / "manifests"


def _temporary_manifest(source_name: str, tmp_path: Path) -> Path:
    source = json.loads((MANIFEST_DIR / source_name).read_text(encoding="utf-8"))
    source["output_dir"] = str(tmp_path / source["experiment_id"])
    source.pop("manifest_hash", None)
    source["manifest_hash"] = _manifest_hash(source)
    path = tmp_path / source_name
    path.write_text(json.dumps(source, indent=2), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "manifest_name,task_id,arm",
    [
        ("phase5-real-canary-004.json", "C1_task_082_P0", "A0_BARE"),
        ("phase5-real-canary-005.json", "C1_task_036_P4", "A3_ODYS_FULL"),
        ("phase5-real-canary-006.json", "C1_task_036_P4", "A0_BARE"),
    ],
)
def test_three_canary_manifests_preflight_provider_free(
    manifest_name: str,
    task_id: str,
    arm: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://preflight.invalid/v1")
    secret = "must-never-enter-an-artifact"
    monkeypatch.setenv("ODYS_AGENT_API_KEY", secret)
    path = _temporary_manifest(manifest_name, tmp_path)

    with patch("lhas.phase5.canary_runner.LiveModelDriver") as live_driver:
        report = preflight_canary(path)
        live_driver.assert_not_called()

    assert report["preflight_status"] == "PASS"
    assert report["provider_executed"] == "NO"
    assert report["provider_requests"] == 0
    assert report["task"]["task_id"] == task_id
    assert report["arms"] == [arm]
    assert report["trials_expected"] == 1
    artifact = Path(report["output"]["path"]) / "canary_preflight.json"
    artifact_text = artifact.read_text(encoding="utf-8")
    assert secret not in artifact_text
    assert report["provider"]["api_key_persisted"] is False


def test_cli_manifest_id_mismatch_fails_closed_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://preflight.invalid/v1")
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    with pytest.raises(CanaryPreflightError, match="CLI experiment id mismatch"):
        run_canary(path, experiment_id="phase5-real-canary-005", preflight_only=True)


def test_output_collision_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://preflight.invalid/v1")
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    output_dir = Path(manifest["output_dir"])
    output_dir.mkdir(parents=True)
    (output_dir / "canary_report.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CanaryPreflightError, match="occupied"):
        preflight_canary(path)


def test_provider_lock_mismatch_is_rejected_before_request():
    actual = {
        "provider": "mimo",
        "model_id": "wrong-model",
        "temperature": 0.0,
        "top_p": 1.0,
        "max_output_tokens": 4096,
        "request_timeout": 120,
        "max_retries": 3,
        "thinking_enabled": True,
        "supports_tool_choice": False,
        "supports_parallel_tool_calls": False,
    }
    lock = {
        "provider": "mimo",
        "exact_model_id": "mimo-v2.5",
        "temperature": 0.0,
        "top_p": 1.0,
        "max_output_tokens": 4096,
        "request_timeout_seconds": 120,
        "thinking_enabled": True,
        "supports_tool_choice": False,
    }
    with pytest.raises(ProviderLockError, match="provider-lock mismatch"):
        validate_resolved_config(actual, lock)


def test_manifest_hash_is_immutable_and_not_a_runtime_override():
    manifest = json.loads(
        (MANIFEST_DIR / "phase5-real-canary-004.json").read_text(encoding="utf-8")
    )
    assert manifest["task"]["task_id"] == "C1_task_082_P0"
    assert manifest["manifest_hash"] == _manifest_hash(manifest)
