"""Strict normalization of provider output into the native loop contract."""

from __future__ import annotations

import json
from typing import Any

from lhas.native.models import ProviderResponse, ProviderToolCall


class ModelResponseError(ValueError):
    pass


class ModelResponseParser:
    def parse(self, value: Any) -> ProviderResponse:
        if isinstance(value, ProviderResponse):
            return value
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="python")
        if not isinstance(value, dict):
            raise ModelResponseError("PROVIDER_RESPONSE_NOT_OBJECT")

        if "choices" in value:
            try:
                message = value["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ModelResponseError("PROVIDER_RESPONSE_MISSING_MESSAGE") from exc
            usage = value.get("usage") or {}
            metadata = {"provider_response_id": str(value.get("id") or "")[:128]}
        else:
            message = value.get("message", value)
            usage = value.get("usage") or {}
            metadata = value.get("metadata") or {}
        if hasattr(message, "model_dump"):
            message = message.model_dump(mode="python")
        if not isinstance(message, dict):
            raise ModelResponseError("PROVIDER_MESSAGE_NOT_OBJECT")

        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        if not isinstance(content, str):
            raise ModelResponseError("PROVIDER_CONTENT_INVALID")

        calls: list[ProviderToolCall] = []
        raw_calls = message.get("tool_calls") or value.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelResponseError("PROVIDER_TOOL_CALLS_INVALID")
        for index, raw in enumerate(raw_calls):
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump(mode="python")
            if not isinstance(raw, dict):
                raise ModelResponseError("PROVIDER_TOOL_CALL_INVALID")
            function = raw.get("function") or raw
            if hasattr(function, "model_dump"):
                function = function.model_dump(mode="python")
            if not isinstance(function, dict) or not function.get("name"):
                raise ModelResponseError("PROVIDER_TOOL_FUNCTION_INVALID")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ModelResponseError("PROVIDER_TOOL_ARGUMENTS_INVALID") from exc
            if not isinstance(arguments, dict):
                raise ModelResponseError("PROVIDER_TOOL_ARGUMENTS_INVALID")
            calls.append(ProviderToolCall(
                id=str(raw.get("id") or f"provider-call-{index + 1}"),
                name=str(function["name"]),
                arguments=arguments,
            ))
        explicit_claim = bool(value.get("completion_claim", message.get("completion_claim", False)))
        return ProviderResponse(
            content=content[:40_000],
            tool_calls=calls,
            completion_claim=explicit_claim or (bool(content.strip()) and not calls),
            usage=usage if isinstance(usage, dict) else {},
            metadata=metadata if isinstance(metadata, dict) else {},
        )
