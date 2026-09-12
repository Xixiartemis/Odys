import json
from pathlib import Path

import pytest

from evals.reliability.p46_launcher import (
    CHEAP_CONFIG_HASH,
    PROFILE_CHEAP,
    PROFILE_PHASE4,
    _load_executor_from_flag,
    load_benchmark_profile,
)
from evals.reliability.p46_provider import (
    CHEAP_CREDENTIAL_ENV,
    CHEAP_MODEL,
    FROZEN_MODEL,
    ProviderIdentityError,
    create_cheap_model_provider,
)
from evals.reliability.run_phase4 import ProtocolSnapshot


def test_cheap_profile_loads_its_frozen_identity():
    profile = load_benchmark_profile(PROFILE_CHEAP)

    assert profile.name == PROFILE_CHEAP
    assert profile.benchmark_version == "phase4-v1-cheap-model"
    assert profile.model == CHEAP_MODEL
    assert profile.provider == "xiaomimimo-openai-compatible"
    assert profile.credential_env == CHEAP_CREDENTIAL_ENV
    assert profile.config_hash == CHEAP_CONFIG_HASH


def test_default_profile_remains_the_original_pro_profile():
    profile = load_benchmark_profile(PROFILE_PHASE4)

    assert profile.benchmark_version == "phase4-v1"
    assert profile.model == FROZEN_MODEL
    assert profile.provider == "xiaomimimo-openai-compatible"
    assert profile.credential_env == "ODYS_BENCHMARK_API_KEY"


def test_cheap_provider_does_not_reuse_generic_pro_credential(monkeypatch):
    monkeypatch.delenv(CHEAP_CREDENTIAL_ENV, raising=False)
    monkeypatch.setenv("ODYS_BENCHMARK_API_KEY", "pro-only-test-secret")

    with pytest.raises(ProviderIdentityError, match="CREDENTIAL_REQUIRED_BEFORE_RUN"):
        create_cheap_model_provider()


def test_launcher_selects_cheap_executor_and_identity(monkeypatch):
    monkeypatch.setenv(CHEAP_CREDENTIAL_ENV, "cheap-test-secret")
    monkeypatch.delenv("ODYS_BENCHMARK_PROVIDER", raising=False)
    monkeypatch.delenv("ODYS_CHEAP_BENCHMARK_BASE_URL", raising=False)

    executor = _load_executor_from_flag(None, load_benchmark_profile(PROFILE_CHEAP))

    assert executor.provider_identity["model"] == CHEAP_MODEL
    assert executor.provider_identity["provider"] == "xiaomimimo-openai-compatible"


def test_launcher_default_executor_remains_pro(monkeypatch):
    monkeypatch.setenv("ODYS_BENCHMARK_API_KEY", "pro-test-secret")
    monkeypatch.delenv(CHEAP_CREDENTIAL_ENV, raising=False)
    monkeypatch.delenv("ODYS_BENCHMARK_PROVIDER", raising=False)

    executor = _load_executor_from_flag(None, load_benchmark_profile(PROFILE_PHASE4))

    assert executor.provider_identity["model"] == FROZEN_MODEL


def test_frozen_protocol_hash_is_unchanged():
    snapshot = ProtocolSnapshot.load()

    assert snapshot.protocol_hash == (
        "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
    )


def test_cheap_provider_identity_artifact_contains_no_secret():
    path = Path("results/official_phase4/cheap_model/provider_identity.json")
    identity = json.loads(path.read_text(encoding="utf-8"))

    assert identity["model"] == CHEAP_MODEL
    assert identity["provider"] == "xiaomimimo-openai-compatible"
    assert "api_key" not in identity


def test_warmup_propagates_cheap_profile_and_derived_output(monkeypatch, tmp_path):
    import evals.reliability.p46_launcher as launcher

    class Snapshot:
        protocol_hash = (
            "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
        )

    class Executor:
        provider_identity = {
            "model": CHEAP_MODEL,
            "provider": "xiaomimimo-openai-compatible",
        }

    captured = {}
    monkeypatch.delenv("ODYS_BENCHMARK_MODEL", raising=False)
    monkeypatch.delenv("ODYS_BENCHMARK_PROVIDER", raising=False)
    monkeypatch.setattr(launcher.ProtocolSnapshot, "load", lambda *args: Snapshot())
    monkeypatch.setattr(launcher, "_build_warmup_runs", lambda snapshot: ())
    monkeypatch.setattr(
        launcher,
        "_load_executor_from_flag",
        lambda executor_spec, profile: (captured.setdefault("profile", profile), Executor())[1],
    )

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {"total_runs": 0}

    monkeypatch.setattr(launcher, "_run_benchmark", fake_run)

    assert launcher.main(
        [
            "warmup",
            "--config-profile",
            "cheap_model",
            "--output",
            str(tmp_path / "warmup"),
        ]
    ) == 0
    assert captured["profile"].name == PROFILE_CHEAP
    assert captured["profile"].model == CHEAP_MODEL
    assert captured["output_dir"] == tmp_path / "warmup"


def test_warmup_default_keeps_pro_profile_and_default_output(monkeypatch):
    import evals.reliability.p46_launcher as launcher

    class Snapshot:
        protocol_hash = (
            "993eae04290fe683d40fcf845b9e7325b9572b227e78e7cb3090dd7ee37637c3"
        )

    class Executor:
        provider_identity = {
            "model": FROZEN_MODEL,
            "provider": "xiaomimimo-openai-compatible",
        }

    captured = {}
    monkeypatch.delenv("ODYS_BENCHMARK_MODEL", raising=False)
    monkeypatch.delenv("ODYS_BENCHMARK_PROVIDER", raising=False)
    monkeypatch.setattr(launcher.ProtocolSnapshot, "load", lambda *args: Snapshot())
    monkeypatch.setattr(launcher, "_build_warmup_runs", lambda snapshot: ())
    monkeypatch.setattr(
        launcher,
        "_load_executor_from_flag",
        lambda executor_spec, profile: (captured.setdefault("profile", profile), Executor())[1],
    )

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {"total_runs": 0}

    monkeypatch.setattr(launcher, "_run_benchmark", fake_run)

    assert launcher.main(["warmup"]) == 0
    assert captured["profile"].name == PROFILE_PHASE4
    assert captured["profile"].model == FROZEN_MODEL
    assert captured["output_dir"] == launcher.DEFAULT_OUTPUT_DIR
