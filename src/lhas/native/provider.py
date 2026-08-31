"""Provider adapters perform one model API call and never own the agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from lhas.native.models import ModelContext, ProviderResponse


@runtime_checkable
class ProviderAdapter(Protocol):
    name: str

    async def generate(
        self,
        *,
        context: ModelContext,
        tools: list[dict[str, Any]],
        timeout_seconds: float,
    ) -> Any: ...


class OpenAIChatProviderAdapter:
    """Single-turn OpenAI-compatible Chat Completions adapter.

    The OpenAI client owns HTTP only. NativeAgentKernel owns iteration,
    dispatch, observations, validation, recovery, budgets, and termination.
    """

    name = "openai-compatible-chat"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        extra_body: dict[str, Any] | None = None,
        client: Any = None,
    ):
        if not model or not api_key:
            raise ValueError("model and api_key are required")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.extra_body = dict(extra_body or {})
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - installation contract
                raise RuntimeError("agent extra is required for the real native provider") from exc
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.client = client

    async def generate(self, *, context: ModelContext, tools: list[dict[str, Any]], timeout_seconds: float) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "messages": context.messages}
        if tools:
            kwargs.update({"tools": tools, "tool_choice": "auto"})
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        return await asyncio.wait_for(
            self.client.chat.completions.create(**kwargs),
            timeout=max(0.1, float(timeout_seconds)),
        )


class ScriptedProviderAdapter:
    """Deterministic provider boundary used by adversarial native-loop tests."""

    name = "scripted-native-provider"

    def __init__(self, responses: Iterable[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate(self, *, context: ModelContext, tools: list[dict[str, Any]], timeout_seconds: float) -> Any:
        self.calls.append({
            "context": context.model_dump(mode="json"),
            "tool_names": [item.get("function", {}).get("name") for item in tools],
            "timeout_seconds": timeout_seconds,
        })
        if not self._responses:
            raise RuntimeError("SCRIPTED_PROVIDER_EXHAUSTED")
        value = self._responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value(context)
        return value


class OfflineCompletionProvider:
    """Network-free provider for an already-valid native CLI smoke run."""

    name = "offline-native-provider"

    async def generate(self, *, context: ModelContext, tools: list[dict[str, Any]], timeout_seconds: float) -> ProviderResponse:
        return ProviderResponse(
            content="Request independent completion validation.",
            completion_claim=True,
            metadata={"offline": True},
        )
