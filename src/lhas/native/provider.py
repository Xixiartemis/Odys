"""Provider adapters perform one model API call and never own the agent loop."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from lhas.execution_control import ExecutionControlToken, await_with_control
from lhas.native.models import ModelContext, ProviderResponse, RuntimeTarget
from lhas.native.transport import canonical_transport_identity, opaque_transport_identity


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


class ProviderResponseNormalizationError(ValueError):
    """The SDK returned a response shape the native parser cannot consume."""


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
        provider_id: str | None = None,
        endpoint_identity: str | None = None,
        credential_route_id: str = "default",
        route_type: str = "chat_completions",
        max_retries: int = 0,
    ):
        if not model or not api_key:
            raise ValueError("model and api_key are required")
        self.model = model
        self.provider_id = provider_id or self.name
        # Accepted for source compatibility only. Runtime identity is always
        # recomputed from the transport used by the actual client.
        self.requested_endpoint_identity = endpoint_identity
        self.credential_route_id = credential_route_id
        self.route_type = route_type
        self.max_retries = int(max_retries)
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.api_key = api_key
        self.base_url = base_url
        self.extra_body = dict(extra_body or {})
        self._execution_control: ExecutionControlToken | None = None
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - installation contract
                raise RuntimeError("agent extra is required for the real native provider") from exc
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=self.max_retries,
            )
        self.client = client

    def bind_execution_control(self, control: ExecutionControlToken | None) -> None:
        """Bind the current Odys root control without creating a new authority."""
        self._execution_control = control

    @property
    def runtime_target(self) -> RuntimeTarget:
        transport = self.transport_identity
        return RuntimeTarget(provider_id=self.provider_id, model_id=self.model,
            endpoint_identity=transport.endpoint_identity,
            endpoint_host=transport.endpoint_host,
            endpoint_fingerprint=transport.endpoint_fingerprint,
            credential_route_id=self.credential_route_id,
            route_type=self.route_type)

    @property
    def transport_identity(self):
        # client.base_url is authoritative when present. This also detects a
        # transport replacement between preparation and a later model call.
        return canonical_transport_identity(getattr(self.client, "base_url", None) or self.base_url)

    async def generate(self, *, context: ModelContext, tools: list[dict[str, Any]], timeout_seconds: float) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "messages": context.messages}
        if tools:
            kwargs.update({"tools": tools, "tool_choice": "auto"})
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        control = self._execution_control
        if control is not None:
            control.check()
            effective_timeout = control.effective_timeout(float(timeout_seconds))
            if effective_timeout is None:
                effective_timeout = float(timeout_seconds)
            if effective_timeout <= 0:
                control.check()
        else:
            effective_timeout = max(0.1, float(timeout_seconds))
        # Keep the SDK transport timeout aligned with the caller's canonical
        # per-provider deadline.  Test doubles do not implement with_options,
        # so the existing injected-client path remains unchanged.
        request_client = self.client
        with_options = getattr(self.client, "with_options", None)
        if callable(with_options):
            request_client = with_options(timeout=effective_timeout)
        raw = await await_with_control(
            request_client.chat.completions.create(**kwargs),
            control=control,
            local_ceiling=effective_timeout,
            timeout_failure_type="PROVIDER_TIMEOUT",
            source="provider",
        )
        return self._normalize_response(raw)

    @staticmethod
    def _normalize_response(raw: Any) -> dict[str, Any]:
        """Convert SDK response models to the parser's stable dict contract."""
        if isinstance(raw, dict):
            return raw
        dump = getattr(raw, "model_dump", None)
        if callable(dump):
            normalized = dump(mode="python")
        else:
            as_dict = getattr(raw, "dict", None)
            normalized = as_dict() if callable(as_dict) else None
        if not isinstance(normalized, dict):
            raise ProviderResponseNormalizationError("PROVIDER_RESPONSE_NOT_NORMALIZABLE")
        return normalized


class ScriptedProviderAdapter:
    """Deterministic provider boundary used by adversarial native-loop tests."""

    name = "scripted-native-provider"

    def __init__(self, responses: Iterable[Any], *, runtime_target: RuntimeTarget | None = None,
                 provider_id: str = "scripted", model_id: str = "scripted-model",
                 endpoint_identity: str = "scripted", credential_route_id: str = "default"):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self._execution_control: ExecutionControlToken | None = None
        self._runtime_target = runtime_target or RuntimeTarget(provider_id=provider_id, model_id=model_id,
            endpoint_identity=endpoint_identity, credential_route_id=credential_route_id, route_type="scripted")

    @property
    def runtime_target(self) -> RuntimeTarget:
        return self._runtime_target

    @property
    def transport_identity(self):
        return opaque_transport_identity(self._runtime_target.endpoint_identity)

    def bind_execution_control(self, control: ExecutionControlToken | None) -> None:
        self._execution_control = control

    async def generate(self, *, context: ModelContext, tools: list[dict[str, Any]], timeout_seconds: float) -> Any:
        if self._execution_control is not None:
            self._execution_control.check()
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
        if inspect.isawaitable(value):
            value = await await_with_control(
                value,
                control=self._execution_control,
                local_ceiling=timeout_seconds,
                timeout_failure_type="PROVIDER_TIMEOUT",
                source="provider",
            )
        if self._execution_control is not None:
            self._execution_control.check()
        return value


class OfflineCompletionProvider:
    """Network-free provider for an already-valid native CLI smoke run."""

    name = "offline-native-provider"

    def bind_execution_control(self, control: ExecutionControlToken | None) -> None:
        self._execution_control = control

    @property
    def runtime_target(self) -> RuntimeTarget:
        return RuntimeTarget(provider_id=self.name, model_id="offline", endpoint_identity="local", credential_route_id="none", route_type="offline")

    @property
    def transport_identity(self):
        return opaque_transport_identity("local")

    async def generate(self, *, context: ModelContext, tools: list[dict[str, Any]], timeout_seconds: float) -> ProviderResponse:
        control = getattr(self, "_execution_control", None)
        if control is not None:
            control.check()
        return ProviderResponse(
            content="Request independent completion validation.",
            completion_claim=True,
            metadata={"offline": True},
        )
