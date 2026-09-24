"""Live ModelDriver — OpenAI-compatible API client for Phase5 canary/pilot.

This driver calls a real model API endpoint. It is used for live
canary and pilot experiments (not provider-free tests).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .model_driver import ModelAction, DriverTokenUsage, ModelToolCall

logger = logging.getLogger(__name__)


class LiveModelDriver:
    """OpenAI-compatible API client that satisfies ModelDriver protocol.

    Reads configuration from environment variables:
    - ODYS_AGENT_API_KEY
    - ODYS_AGENT_BASE_URL
    - ODYS_AGENT_MODEL (or model_id parameter)
    - ODYS_AGENT_PROVIDER_PROFILE (default: "mimo")
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
    ):
        self._model_id = model_id or os.getenv("ODYS_AGENT_MODEL", "mimo-v2.5")
        self._base_url = base_url or os.getenv("ODYS_AGENT_BASE_URL")
        self._api_key = api_key or os.getenv("ODYS_AGENT_API_KEY")
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._thinking_enabled = thinking_enabled
        self._request_timeout = request_timeout

        if not self._api_key:
            raise ValueError("ODYS_AGENT_API_KEY not set")
        if not self._base_url:
            raise ValueError("ODYS_AGENT_BASE_URL not set")

        # Token accounting
        self._input_tokens = 0
        self._output_tokens = 0
        self._request_count = 0

        # Lazy client
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._request_timeout,
            )
        return self._client

    def next_action(
        self,
        messages: List[Dict[str, Any]],
        tool_definitions: List[Dict[str, Any]],
        generation_config: Optional[Dict[str, Any]] = None,
    ) -> ModelAction:
        """Call the model API and return the next action."""
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

        # Build request
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

        # Call API
        self._request_count += 1
        start = time.time()
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            logger.error("API call failed: %s", exc)
            return ModelAction(
                type="final_answer",
                content=f"[API_ERROR] {exc}",
            )

        elapsed = time.time() - start
        logger.info("API call %d completed in %.1fs", self._request_count, elapsed)

        # Parse response
        choice = response.choices[0]
        message = choice.message

        # Update token accounting
        if response.usage:
            self._input_tokens += response.usage.prompt_tokens or 0
            self._output_tokens += response.usage.completion_tokens or 0

        # Check for tool calls
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

        # Final answer
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
        """No-op for live driver — tool results are handled by the adapter."""
        pass

    def reset(self) -> None:
        """Reset token accounting for a new trial."""
        self._input_tokens = 0
        self._output_tokens = 0
        self._request_count = 0
