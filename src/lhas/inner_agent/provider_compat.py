"""Small, provider-neutral compatibility profiles for OpenAI-compatible APIs.

Profiles describe transport quirks only.  They never contain credentials, URLs,
or persisted model reasoning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ProviderCompatProfile:
    name: str
    preferred_api_mode: str | None
    replay_reasoning_content: bool
    supports_tool_choice: bool
    requires_assistant_content_for_tool_calls: bool
    thinking_enabled: bool
    extra_body: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra_body", _freeze(self.extra_body))

    def extra_body_dict(self) -> dict[str, Any]:
        def thaw(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {key: thaw(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [thaw(item) for item in value]
            return value

        return thaw(self.extra_body)


DEFAULT_PROFILE = ProviderCompatProfile(
    name="default",
    preferred_api_mode=None,
    replay_reasoning_content=False,
    supports_tool_choice=True,
    requires_assistant_content_for_tool_calls=False,
    thinking_enabled=False,
)

MIMO_PROFILE = ProviderCompatProfile(
    name="mimo",
    preferred_api_mode="chat_completions",
    replay_reasoning_content=True,
    supports_tool_choice=False,
    requires_assistant_content_for_tool_calls=True,
    thinking_enabled=True,
    extra_body={"thinking": {"type": "enabled"}},
)

_PROFILES = {"default": DEFAULT_PROFILE, "mimo": MIMO_PROFILE}


def resolve_provider_profile(name: str | None = None) -> ProviderCompatProfile:
    """Resolve explicit configuration, defaulting conservatively to DEFAULT."""
    configured = name if name is not None else os.getenv("ODYS_AGENT_PROVIDER_PROFILE")
    normalized = (configured or "default").strip().lower()
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        raise ValueError("PROVIDER_PROFILE_UNSUPPORTED") from exc


def should_replay_mimo_reasoning(context: Any) -> bool:
    """Replay only reasoning from the exact configured model.

    The Agents SDK supplies origin model/provider metadata in ``context``.  Exact
    model equality is the conservative same-provider condition; unknown origins
    are never replayed across providers.
    """
    origin_model = getattr(getattr(context, "reasoning", None), "origin_model", None)
    destination_model = getattr(context, "model", None)
    if not (origin_model and destination_model and origin_model == destination_model):
        return False
    provider_data = getattr(getattr(context, "reasoning", None), "provider_data", {}) or {}
    origin_base_url = provider_data.get("base_url") or provider_data.get("provider_base_url")
    destination_base_url = getattr(context, "base_url", None)
    if origin_base_url is not None and destination_base_url is not None:
        return str(origin_base_url).rstrip("/") == str(destination_base_url).rstrip("/")
    if origin_base_url is not None and destination_base_url is None:
        return False
    return True


def prepare_mimo_chat_request(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Apply only MiMo wire compatibility to a Chat Completions request.

    This function is deliberately pure and does not retain request content.
    """
    request = dict(kwargs)
    request.pop("tool_choice", None)
    messages = []
    for message in request.get("messages", []) or []:
        normalized = dict(message)
        if (
            normalized.get("role") == "assistant"
            and normalized.get("tool_calls")
            and normalized.get("content") is None
        ):
            normalized["content"] = ""
        messages.append(normalized)
    if "messages" in request:
        request["messages"] = messages
    return request


class _MimoCompletions:
    def __init__(self, completions: Any):
        self._completions = completions

    async def create(self, **kwargs: Any) -> Any:
        return await self._completions.create(**prepare_mimo_chat_request(kwargs))


class _MimoChat:
    def __init__(self, chat: Any):
        self.completions = _MimoCompletions(chat.completions)


class _MimoClient:
    """Delegating client shim; the underlying OpenAI SDK still owns HTTP."""

    def __init__(self, client: Any):
        self._client = client
        self.base_url = client.base_url
        self.chat = _MimoChat(client.chat)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def with_options(self, **kwargs: Any) -> "_MimoClient":
        return _MimoClient(self._client.with_options(**kwargs))


class MimoModelProvider:
    """Agents SDK ModelProvider for the verified MiMo Chat Completions contract."""

    def __init__(self, *, api_key: str | None, base_url: str | None, client: Any = None):
        from openai import AsyncOpenAI

        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._wrapped_client = _MimoClient(self._client)

    def get_model(self, model_name: str | None):
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

        return OpenAIChatCompletionsModel(
            model=model_name or "",
            openai_client=self._wrapped_client,
            should_replay_reasoning_content=should_replay_mimo_reasoning,
            strict_feature_validation=True,
        )

    async def aclose(self) -> None:
        # The Agents SDK provider contract does not require closing shared clients.
        return None


__all__ = [
    "DEFAULT_PROFILE",
    "MIMO_PROFILE",
    "MimoModelProvider",
    "ProviderCompatProfile",
    "prepare_mimo_chat_request",
    "resolve_provider_profile",
    "should_replay_mimo_reasoning",
]
