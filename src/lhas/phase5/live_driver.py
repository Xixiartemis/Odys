"""Live MiMo/OpenAI-compatible driver with a frozen wire contract."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI

from .model_driver import DriverTokenUsage, ModelAction, ModelToolCall
from .provider_wire import ProviderProtocolError, canonical_tool_schemas, serialize_messages

logger = logging.getLogger(__name__)


class ProviderExecutionError(RuntimeError):
    """Provider transport/protocol failure, classified as INVALID_INFRA."""

    def __init__(self, message: str, *, failure_class: str = "UNKNOWN_TRANSPORT"):
        super().__init__(message)
        self.failure_class = failure_class


def _classify_provider_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "connect" in msg or "connection" in msg:
        return "CONNECTION_RESET"
    if "timeout" in msg or "timed out" in msg:
        return "READ_TIMEOUT" if "read" in msg else "WRITE_TIMEOUT" if "write" in msg else "READ_TIMEOUT"
    if "tls" in msg or "ssl" in msg or "handshake" in msg:
        return "TLS_FAILURE"
    if "dns" in msg or "resolve" in msg or "name resolution" in msg:
        return "DNS_FAILURE"
    if "429" in msg or "rate" in msg or "too many" in msg:
        return "HTTP_429"
    if any(code in msg for code in ("500", "502", "503", "504")):
        return "HTTP_5XX"
    if any(code in msg for code in ("400", "401", "403", "404")):
        return "HTTP_4XX_PROVIDER_ERROR"
    return "UNKNOWN_TRANSPORT"


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class LiveModelDriver:
    """MiMo thinking-mode driver with zero SDK retries."""

    def __init__(
        self,
        *,
        provider: str = "mimo",
        model_id: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        thinking_enabled: bool = True,
        request_timeout: int = 120,
        max_completion_tokens: int = 4096,
        max_output_tokens: Optional[int] = None,
        sdk_max_retries: int = 0,
        max_retries: Optional[int] = None,
        effective_temperature: float = 1.0,
        effective_top_p: float = 0.95,
        sampling_parameters_sent: bool = False,
        supports_tool_choice: bool = False,
        supports_parallel_tool_calls: bool = False,
        request_parallel_tool_calls_hint: bool = False,
        provider_may_return_multiple_tool_calls: bool = True,
        client_factory: Optional[Callable[..., Any]] = None,
    ):
        self._provider = provider
        self._model_id = model_id or os.getenv("ODYS_AGENT_MODEL", "mimo-v2.5")
        self._base_url = base_url or os.getenv("ODYS_AGENT_BASE_URL")
        self._api_key = api_key or os.getenv("ODYS_AGENT_API_KEY")
        self._thinking_enabled = thinking_enabled
        self._request_timeout = request_timeout
        self._max_completion_tokens = max_completion_tokens if max_output_tokens is None else max_output_tokens
        self._sdk_max_retries = sdk_max_retries if max_retries is None else max_retries
        self._effective_temperature = effective_temperature
        self._effective_top_p = effective_top_p
        self._sampling_parameters_sent = sampling_parameters_sent
        self._supports_tool_choice = supports_tool_choice
        self._supports_parallel_tool_calls = supports_parallel_tool_calls
        self._request_parallel_tool_calls_hint = request_parallel_tool_calls_hint
        self._provider_may_return_multiple_tool_calls = provider_may_return_multiple_tool_calls
        self._client_factory = client_factory or OpenAI
        if not self._api_key:
            raise ValueError("ODYS_AGENT_API_KEY not set")
        if not self._base_url:
            raise ValueError("ODYS_AGENT_BASE_URL not set")
        if self._sdk_max_retries != 0:
            raise ValueError("SDK retries must be frozen at zero for Phase5 live execution")
        if self._supports_parallel_tool_calls:
            raise ValueError("parallel tool calls are disabled by the frozen provider lock")
        if self._sampling_parameters_sent and self._thinking_enabled:
            raise ValueError("custom sampling parameters cannot be sent in MiMo thinking mode")

        self._input_tokens = 0
        self._output_tokens = 0
        self._request_count = 0
        self._http_attempt_count = 0
        self._client = None
        self._last_reasoning_content: Optional[str] = None

    def construct_client(self) -> Any:
        """Construct the SDK client only; this does not perform network I/O."""
        if self._client is None:
            self._client = self._client_factory(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._request_timeout,
                max_retries=0,
            )
        return self._client

    def _get_client(self) -> Any:
        return self.construct_client()

    def _request_kwargs(self, messages: List[Dict[str, Any]], tool_definitions: List[Dict[str, Any]]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self._model_id,
            "messages": serialize_messages(messages),
            "max_completion_tokens": self._max_completion_tokens,
            "parallel_tool_calls": self._request_parallel_tool_calls_hint,
        }
        if tool_definitions:
            kwargs["tools"] = canonical_tool_schemas(tool_definitions)
        if self._thinking_enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        return kwargs

    def next_action(
        self,
        messages: List[Dict[str, Any]],
        tool_definitions: List[Dict[str, Any]],
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> ModelAction:
        """Make exactly one HTTP attempt and validate the response contract."""
        try:
            kwargs = self._request_kwargs(messages, tool_definitions)
        except ProviderProtocolError as exc:
            raise ProviderExecutionError(str(exc), failure_class="PROVIDER_PROTOCOL_FAILURE") from exc
        self._request_count += 1
        self._http_attempt_count += 1
        try:
            response = self._get_client().chat.completions.create(**kwargs)
        except Exception as exc:
            failure_class = _classify_provider_error(exc)
            logger.error("Provider %s", failure_class)
            raise ProviderExecutionError(
                f"Provider {failure_class}: {type(exc).__name__}",
                failure_class=failure_class,
            ) from exc

        choices = _value(response, "choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderExecutionError("provider response choices is missing or empty", failure_class="PROVIDER_PROTOCOL_FAILURE")
        choice = choices[0]
        message = _value(choice, "message")
        if message is None or _value(message, "role", "assistant") != "assistant":
            raise ProviderExecutionError("provider assistant message is malformed", failure_class="PROVIDER_PROTOCOL_FAILURE")
        response_model = _value(response, "model")
        if response_model is not None and response_model != self._model_id:
            raise ProviderExecutionError("provider response model identity mismatch", failure_class="PROVIDER_PROTOCOL_FAILURE")

        usage = _value(response, "usage")
        input_tokens = _value(usage, "prompt_tokens") if usage is not None else None
        output_tokens = _value(usage, "completion_tokens") if usage is not None else None
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise ProviderExecutionError("provider response usage is missing", failure_class="PROVIDER_PROTOCOL_FAILURE")
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens

        tool_calls = _value(message, "tool_calls") or []
        if not isinstance(tool_calls, list):
            raise ProviderExecutionError("provider tool_calls envelope is malformed", failure_class="PROVIDER_PROTOCOL_FAILURE")
        reasoning = _value(message, "reasoning_content")
        self._last_reasoning_content = reasoning
        if tool_calls:
            if not isinstance(reasoning, str):
                raise ProviderExecutionError("reasoning_content missing from MiMo tool response", failure_class="PROVIDER_PROTOCOL_FAILURE")
            parsed_calls: list[ModelToolCall] = []
            provider_ids: set[str] = set()
            for tool_call in tool_calls:
                provider_id = _value(tool_call, "id")
                function = _value(tool_call, "function")
                name = _value(function, "name")
                arguments = _value(function, "arguments")
                if not isinstance(provider_id, str) or not provider_id:
                    raise ProviderExecutionError("provider tool_call_id is missing", failure_class="PROVIDER_PROTOCOL_FAILURE")
                if provider_id in provider_ids:
                    raise ProviderExecutionError("duplicate provider tool_call_id", failure_class="PROVIDER_PROTOCOL_FAILURE")
                provider_ids.add(provider_id)
                if not isinstance(name, str) or not name or not isinstance(arguments, str):
                    raise ProviderExecutionError("provider function tool call is malformed", failure_class="PROVIDER_PROTOCOL_FAILURE")
                try:
                    parsed_arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ProviderExecutionError("provider tool arguments are not valid JSON", failure_class="PROVIDER_PROTOCOL_FAILURE") from exc
                if not isinstance(parsed_arguments, dict):
                    raise ProviderExecutionError("provider tool arguments must be a JSON object", failure_class="PROVIDER_PROTOCOL_FAILURE")
                parsed_calls.append(
                    ModelToolCall(
                        tool_name=name,
                        arguments=parsed_arguments,
                        provider_tool_call_id=provider_id,
                    )
                )
            first_call = parsed_calls[0]
            return ModelAction(
                type="tool_call",
                tool_name=first_call.tool_name,
                arguments=first_call.arguments,
                tool_calls=parsed_calls,
                provider_tool_call_id=first_call.provider_tool_call_id,
                provider_reasoning_content=reasoning,
            )
        return ModelAction(
            type="final_answer",
            content=_value(message, "content") or "",
            provider_reasoning_content=reasoning,
        )

    def get_token_usage(self) -> DriverTokenUsage:
        return DriverTokenUsage(input_tokens=self._input_tokens, output_tokens=self._output_tokens)

    def get_total_tokens(self) -> int:
        return self._input_tokens + self._output_tokens

    def record_tool_result(self, tool_name: str, result: Dict[str, Any]) -> None:
        return None

    def reset(self) -> None:
        self._input_tokens = 0
        self._output_tokens = 0
        self._request_count = 0
        self._http_attempt_count = 0
        self._last_reasoning_content = None

    @property
    def provider_request_count(self) -> int:
        return self._http_attempt_count

    @property
    def logical_provider_call_count(self) -> int:
        return self._request_count

    @property
    def reasoning_content_present(self) -> bool:
        return self._last_reasoning_content is not None

    @property
    def resolved_config(self) -> Dict[str, Any]:
        return {
            "provider": self._provider,
            "model_id": self._model_id,
            "thinking_enabled": self._thinking_enabled,
            "effective_temperature": self._effective_temperature,
            "effective_top_p": self._effective_top_p,
            "sampling_parameters_sent": self._sampling_parameters_sent,
            "max_completion_tokens": self._max_completion_tokens,
            "request_timeout": self._request_timeout,
            "sdk_max_retries": self._sdk_max_retries,
            "supports_tool_choice": self._supports_tool_choice,
            "supports_parallel_tool_calls": self._supports_parallel_tool_calls,
            "request_parallel_tool_calls_hint": self._request_parallel_tool_calls_hint,
            "provider_may_return_multiple_tool_calls": self._provider_may_return_multiple_tool_calls,
        }
