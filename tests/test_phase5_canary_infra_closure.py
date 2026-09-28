"""Provider-free adversarial tests for the live-canary infrastructure gate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import lhas.phase5.canary_runner as canary_runner
from lhas.phase5.canary_runner import (
    CanaryPreflightError,
    _manifest_hash,
    preflight_canary,
    run_canary,
)
from lhas.phase5.provider_lock import ProviderLockError, validate_resolved_config


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "experiments" / "phase5" / "manifests"


@pytest.fixture(autouse=True)
def isolated_qualification_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        canary_runner,
        "_QUALIFICATION_ROOT",
        tmp_path / "qualification-preflights",
    )


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
    artifact = Path(report["preflight_artifact"]["path"])
    assert report["preflight_role"] == "QUALIFICATION_PREFLIGHT"
    assert report["output"]["status"] == "NOT_TOUCHED"
    assert not (Path(report["output"]["path"]) / "canary_preflight.json").exists()
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
        preflight_canary(path, mode="live")


def test_qualification_then_live_different_endpoint_uses_separate_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    monkeypatch.setenv("ODYS_AGENT_API_KEY", "never-persist-this-key")
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://qualification.invalid/v1")
    qualification = preflight_canary(path, mode="qualification")

    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://live.invalid/v1")
    live = preflight_canary(path, mode="live")

    assert qualification["preflight_role"] == "QUALIFICATION_PREFLIGHT"
    assert live["preflight_role"] == "LIVE_EXECUTION_PREFLIGHT"
    assert qualification["provider"]["endpoint_identity"] != live["provider"]["endpoint_identity"]
    assert Path(qualification["preflight_artifact"]["path"]).is_file()
    assert Path(live["preflight_artifact"]["path"]) == (
        Path(live["output"]["path"]) / "live_preflight.json"
    )
    assert not (Path(live["output"]["path"]) / "canary_preflight.json").exists()


def test_live_preflight_same_identity_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://live.invalid/v1")
    first = preflight_canary(path, mode="live")
    artifact = Path(first["preflight_artifact"]["path"])
    before = artifact.read_bytes()

    second = preflight_canary(path, mode="live")

    assert second == first
    assert artifact.read_bytes() == before


def test_live_preflight_different_endpoint_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://live-a.invalid/v1")
    preflight_canary(path, mode="live")
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://live-b.invalid/v1")

    with pytest.raises(CanaryPreflightError, match="immutable and differs"):
        preflight_canary(path, mode="live")


def test_historical_preflight_is_preserved_when_live_preflight_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    output_dir = Path(manifest["output_dir"])
    output_dir.mkdir(parents=True)
    historical = output_dir / "canary_preflight.json"
    historical_bytes = b'{"historical":true,"provider_requests":0}\n'
    historical.write_bytes(historical_bytes)
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://live.invalid/v1")

    report = preflight_canary(path, mode="live")

    assert report["output"]["historical_preflight_preserved"] is True
    assert historical.read_bytes() == historical_bytes
    assert (output_dir / "live_preflight.json").is_file()


def test_both_preflight_modes_make_zero_provider_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://preflight.invalid/v1")
    with patch("lhas.phase5.canary_runner.LiveModelDriver") as live_driver:
        qualification = preflight_canary(path, mode="qualification")
        live = preflight_canary(path, mode="live")

    live_driver.assert_not_called()
    assert qualification["provider_requests"] == 0
    assert live["provider_requests"] == 0
    assert qualification["provider_executed"] == "NO"
    assert live["provider_executed"] == "NO"


def test_live_execution_cannot_start_when_live_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    with (
        patch(
            "lhas.phase5.canary_runner.preflight_canary",
            side_effect=CanaryPreflightError("invalid live preflight"),
        ),
        patch("lhas.phase5.canary_runner.LiveModelDriver") as live_driver,
        patch("lhas.phase5.canary_runner.execute_trial") as execute_trial,
    ):
        with pytest.raises(CanaryPreflightError, match="invalid live preflight"):
            run_canary(path)

    live_driver.assert_not_called()
    execute_trial.assert_not_called()


def test_api_key_is_never_persisted_in_qualification_or_live_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "super-secret-api-key-that-must-not-be-written"
    path = _temporary_manifest("phase5-real-canary-004.json", tmp_path)
    monkeypatch.setenv("ODYS_AGENT_API_KEY", secret)
    monkeypatch.setenv("ODYS_AGENT_BASE_URL", "https://preflight.invalid/v1")
    preflight_canary(path, mode="qualification")
    preflight_canary(path, mode="live")

    for artifact in tmp_path.rglob("*.json"):
        assert secret not in artifact.read_text(encoding="utf-8")


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
