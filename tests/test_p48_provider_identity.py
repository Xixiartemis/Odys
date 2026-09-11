import json

import pytest

from evals.reliability.p46_provider import (
    FROZEN_ENDPOINT,
    FROZEN_MODEL,
    FROZEN_PROVIDER,
    ProviderIdentityError,
    RealLLMProvider,
    create_real_provider,
    provider_identity,
    validate_and_persist_provider_identity,
)


class _Client:
    base_url = FROZEN_ENDPOINT


def test_benchmark_credential_is_required_before_real_provider_creation(monkeypatch):
    monkeypatch.delenv("ODYS_BENCHMARK_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_CUSTOM_TOKEN_PLAN_CN_XIAOMIMIMO_COM_API_KEY", raising=False)
    with pytest.raises(ProviderIdentityError, match="CREDENTIAL_REQUIRED_BEFORE_RUN"):
        create_real_provider()


def test_provider_identity_is_secret_free_and_persisted(tmp_path):
    provider = RealLLMProvider(
        model=FROZEN_MODEL,
        api_key="test-secret-never-persisted",
        base_url=FROZEN_ENDPOINT,
        client=_Client(),
    )
    identity = provider_identity(provider)
    path = tmp_path / "provider_identity.json"
    validate_and_persist_provider_identity(provider, path=path)

    assert identity["provider"] == FROZEN_PROVIDER
    assert identity["model"] == FROZEN_MODEL
    assert identity["endpoint_hash"]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted == identity
    assert "test-secret-never-persisted" not in path.read_text(encoding="utf-8")


def test_executor_persists_identity_into_the_current_output_bundle(tmp_path):
    from evals.reliability.p45_executor import P45BenchmarkExecutor

    provider = RealLLMProvider(
        model=FROZEN_MODEL,
        api_key="test-secret-never-persisted",
        base_url=FROZEN_ENDPOINT,
        client=_Client(),
    )
    executor = P45BenchmarkExecutor(
        factory_type="real",
        provider=provider,
        provider_identity=provider_identity(provider),
    )
    bundle_path = tmp_path / "warmup_v2" / "provider_identity.json"
    executor.persist_provider_identity(bundle_path)

    assert bundle_path.exists()
    assert json.loads(bundle_path.read_text(encoding="utf-8"))["model"] == FROZEN_MODEL
    assert not (tmp_path / "provider_identity.json").exists()


def test_model_and_endpoint_drift_fail_closed():
    with pytest.raises(ProviderIdentityError, match="MODEL_IDENTITY_MISMATCH"):
        RealLLMProvider(
            model="different-model",
            api_key="test-key",
            base_url=FROZEN_ENDPOINT,
            client=_Client(),
        )

    with pytest.raises(ProviderIdentityError, match="PROVIDER_ENDPOINT_MISMATCH"):
        RealLLMProvider(
            model=FROZEN_MODEL,
            api_key="test-key",
            base_url="https://different.example/v1",
            client=type("OtherClient", (), {"base_url": "https://different.example/v1"})(),
        )
