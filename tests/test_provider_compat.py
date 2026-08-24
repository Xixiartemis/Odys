from types import SimpleNamespace

import pytest

from lhas.inner_agent import AgentsSdkModelConfig
from lhas.inner_agent.provider_compat import (
    DEFAULT_PROFILE,
    MIMO_PROFILE,
    prepare_mimo_chat_request,
    resolve_provider_profile,
    should_replay_mimo_reasoning,
)


def test_default_profile_preserves_existing_selection(monkeypatch):
    monkeypatch.delenv("ODYS_AGENT_PROVIDER_PROFILE", raising=False)
    monkeypatch.delenv("ODYS_AGENT_API_MODE", raising=False)
    config = AgentsSdkModelConfig(model="model", api_key="key")
    assert config.provider_profile is DEFAULT_PROFILE
    assert config.api_mode == "responses"
    assert config.provider_profile.supports_tool_choice is True


def test_mimo_profile_is_explicit_and_prefers_chat_completions(monkeypatch):
    monkeypatch.setenv("ODYS_AGENT_PROVIDER_PROFILE", "mimo")
    monkeypatch.delenv("ODYS_AGENT_API_MODE", raising=False)
    config = AgentsSdkModelConfig(model="mimo-model", api_key="key")
    assert config.provider_profile is MIMO_PROFILE
    assert config.api_mode == "chat_completions"
    assert config.provider_profile.extra_body_dict() == {"thinking": {"type": "enabled"}}


def test_mimo_explicit_api_mode_conflict_is_clear(monkeypatch):
    monkeypatch.delenv("ODYS_AGENT_PROVIDER_PROFILE", raising=False)
    with pytest.raises(ValueError, match="PROVIDER_PROFILE_API_MODE_CONFLICT"):
        AgentsSdkModelConfig(model="mimo-model", api_key="key", provider_profile="mimo", api_mode="responses")


def test_mimo_reasoning_replay_requires_same_origin_model():
    same = SimpleNamespace(model="mimo-model", reasoning=SimpleNamespace(origin_model="mimo-model"))
    unrelated = SimpleNamespace(model="mimo-model", reasoning=SimpleNamespace(origin_model="other-model"))
    unknown = SimpleNamespace(model="mimo-model", reasoning=SimpleNamespace(origin_model=None))
    cross_provider = SimpleNamespace(
        model="mimo-model",
        base_url="https://mimo.example/v1/",
        reasoning=SimpleNamespace(origin_model="mimo-model", provider_data={"base_url": "https://other.example/v1"}),
    )
    assert should_replay_mimo_reasoning(same) is True
    assert should_replay_mimo_reasoning(unrelated) is False
    assert should_replay_mimo_reasoning(unknown) is False
    assert should_replay_mimo_reasoning(cross_provider) is False


def test_mimo_request_omits_tool_choice_and_adds_assistant_content():
    request = prepare_mimo_chat_request(
        {
            "model": "mimo-model",
            "tool_choice": "auto",
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
                {"role": "user", "content": "continue"},
            ],
        }
    )
    assert "tool_choice" not in request
    assert request["messages"][0]["content"] == ""
    assert request["messages"][0]["tool_calls"] == [{"id": "call-1"}]


def test_mimo_model_provider_uses_agents_chat_model():
    from lhas.inner_agent.provider_compat import MimoModelProvider

    class Client:
        base_url = "https://api.example.test/v1/"
        chat = SimpleNamespace(completions=SimpleNamespace())

    model = MimoModelProvider(api_key="key", base_url=Client.base_url, client=Client()).get_model("mimo-model")
    assert model.model == "mimo-model"
    assert model.should_replay_reasoning_content is should_replay_mimo_reasoning


def test_mimo_has_no_server_managed_continuation(monkeypatch):
    monkeypatch.delenv("ODYS_AGENT_PROVIDER_PROFILE", raising=False)
    config = AgentsSdkModelConfig(model="mimo-model", api_key="key", provider_profile="mimo")
    assert config.api_mode == "chat_completions"
    assert not any(hasattr(config, name) for name in ("previous_response_id", "auto_previous_response_id", "conversation_id"))


def test_mimo_backend_routes_chat_completions_without_server_state():
    import asyncio

    from lhas.inner_agent import InnerAgentRequest, InnerAgentStatus, OpenAIAgentsBackend

    captured = {}

    class Provider:
        pass

    class RunConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Runner:
        async def run(self, agent, prompt, **kwargs):
            captured["runner"] = kwargs
            return SimpleNamespace(final_output="ok", context_wrapper=SimpleNamespace(usage={}))

    def provider_factory(**kwargs):
        captured["provider"] = kwargs
        return Provider()

    def run_config_factory(**kwargs):
        return RunConfig(**kwargs)

    request = InnerAgentRequest(task_id="t", run_id="r", attempt_id="a", objective="x")
    backend = OpenAIAgentsBackend(
        registry=SimpleNamespace(resolve=lambda name: None),
        config=AgentsSdkModelConfig(model="mimo-model", api_key="key", provider_profile="mimo"),
        runner=Runner(),
        provider_factory=provider_factory,
        run_config_factory=run_config_factory,
    )
    result = asyncio.run(backend.run(request))
    assert result.status is InnerAgentStatus.SUCCESS
    assert captured["provider"]["use_responses"] is False
    settings = captured["runner"]["run_config"].kwargs["model_settings"]
    assert settings.tool_choice is None and settings.extra_body == {"thinking": {"type": "enabled"}}
    assert not any(key in captured["runner"] for key in ("previous_response_id", "auto_previous_response_id", "conversation_id"))
