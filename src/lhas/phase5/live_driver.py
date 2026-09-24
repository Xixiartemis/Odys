"""Live ModelDriver — OpenAI-compatible API client for Phase5 canary/pilot.

This driver calls a real model API endpoint. Provider transport errors
raise ProviderExecutionError (INVALID_INFRA) — they never become
model final answers.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .model_driver import ModelAction, DriverTokenUsage, ModelToolCall

logger = logging.getLogger(__name__)


class ProviderExecutionError(RuntimeError):
    """Provider transport/infrastructure failure — classified as INVALID_INFRA."""
    def __init__(self, message: str, *, failure_class: str = "UNKNOWN_TRANSPORT"):
        super().__init__(message)
        self.failure_class = failure_class


def _classify_provider_error(exc: Exception) -> str:
    """Classify provider exception into transport failure category."""
    msg = str(exc).lower()
    cls = type(exc).__name__.lower()

    if "connect" in msg or "connection" in msg:
        return "CONNECTION_RESET"
    if "timeout" in msg or "timed out" in msg:
        if "read" in msg:
            return "READ_TIMEOUT"
        if "write" in msg:
            return "WRITE_TIMEOUT"
        return "READ_TIMEOUT"
    if "tls" in msg or "ssl" in msg or "handshake" in msg:
        return "TLS_FAILURE"
    if "dns" in msg or "resolve" in msg or "name resolution" in msg:
        return "DNS_FAILURE"
    if "429" in msg or "rate" in msg or "too many" in msg:
        return "HTTP_429"
    if "500" in msg or "502" in msg or "503" in msg or "504" in msg:
        return "HTTP_5XX"
    if "400" in msg or "401" in msg or "403" in msg or "404" in msg:
        return "HTTP_4XX_PROVIDER_ERROR"
    if "schema" in msg or "parse" in msg or "format" in msg:
        return "PROVIDER_SCHEMA_FAILURE"
    return "UNKNOWN_TRANSPORT"


class LiveModelDriver:
    """OpenAI-compatible API client that satisfies ModelDriver protocol.

    Provider transport errors raise ProviderExecutionError — they never
    become model final answers.
    """

    def __init__(
        self,
        *,
        model_id: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
        thinking_enabled: bool = True,
        request_timeout: int = 120,
        max_retries: int = 3,
    ):
        self._model_id = model_id or os.getenv("ODYS_AGENT_MODEL", "mimo-v2.5")
        self._base_url = base_url or os.getenv("ODYS_AGENT_BASE_URL")
        self._api_key = api_key or os.getenv("ODYS_AGENT_API_KEY")
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._thinking_enabled = thinking_enabled
        self._request_timeout = request_timeout
        self._max_retries = max_retries

        if not self._api_key:
            raise ValueError("ODYS_AGENT_API_KEY not set")
        if not self._base_url:
            raise ValueError("ODYS_AGENT_BASE_URL not set")

        # Token accounting
        self._input_tokens = 0
        self._output_tokens = 0
        self._request_count = 0
        self._http_attempt_count = 0

        # Lazy client
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._request_timeout,
                max_retries=self._max_retries,
            )
        return self._client

    def next_action(
        self,
        messages: List[Dict[str, Any]],
        tool_definitions: List[Dict[str, Any]],
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> ModelAction:
        """Call the model API. Raises ProviderExecutionError on transport failure."""
        client = self._get_client()

        # Convert tool definitions to OpenAI format
        tools = None
        if tool_definitions:
            tools = []
            for td in tool_definitions:
                if isinstance(td, dict) and "function" in td:
                    tools.append(td)
                elif isinstance(td, dict):
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": td.get("name", td.get("function", {}).get("name", "")),
                            "description": td.get("description", ""),
                            "parameters": td.get("parameters", td.get("function", {}).get("parameters", {})),
                        }
                    })

        kwargs: Dict[str, Any] = {
            "model": self._model_id,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._thinking_enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        # Call API — transport errors raise ProviderExecutionError
        self._request_count += 1
        start = time.time()
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            self._http_attempt_count += 1
            failure_class = _classify_provider_error(exc)
            logger.error("Provider %s: %s", failure_class, exc)
            raise ProviderExecutionError(
                f"Provider {failure_class}: {exc}",
                failure_class=failure_class,
            ) from exc

        elapsed = time.time() - start
        self._http_attempt_count += 1
        logger.info("API call %d completed in %.1fs", self._request_count, elapsed)

        choice = response.choices[0]
        message = choice.message

        if response.usage:
            self._input_tokens += response.usage.prompt_tokens or 0
            self._output_tokens += response.usage.completion_tokens or 0

        if message.tool_calls:
            tc = message.tool_calls[0]
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {"raw": tc.function.arguments}

            return ModelAction(
                type="tool_call",
                tool_name=tc.function.name,
                arguments=args,
                thought=getattr(message, "reasoning_content", None) or getattr(message, "content", None),
                tool_call_id=tc.id,
            )

        content = message.content or ""
        return ModelAction(
            type="final_answer",
            content=content,
            thought=getattr(message, "reasoning_content", None),
        )

    def get_token_usage(self) -> DriverTokenUsage:
        return DriverTokenUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )

    def get_total_tokens(self) -> int:
        return self._input_tokens + self._output_tokens

    def record_tool_result(self, tool_name: str, result: Dict[str, Any]) -> None:
        pass

    def reset(self) -> None:
        self._input_tokens = 0
        self._output_tokens = 0
        self._request_count = 0
        self._http_attempt_count = 0

    @property
    def provider_request_count(self) -> int:
        return self._http_attempt_count
