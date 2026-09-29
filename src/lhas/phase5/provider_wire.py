"""Explicit, secret-free OpenAI-compatible wire boundary for Phase5.

The Phase5 core stores Odys-owned conversation records.  This module is the
only translation from those records to the provider request contract.  It
also canonicalizes the official ToolMaze function-call specifications so the
model request, runtime provenance, and pairing hash share one representation.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping, Sequence


class ProviderProtocolError(RuntimeError):
    """A provider request/response violates the frozen wire contract."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _function_spec(tool: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        spec = tool["paradigms"]["function_call"]["spec"]
    except (KeyError, TypeError) as exc:
        raise ProviderProtocolError(
            f"ToolMaze function-call spec missing for tool {tool.get('name', '<unknown>')}"
        ) from exc
    if not isinstance(spec, Mapping):
        raise ProviderProtocolError("ToolMaze function-call spec must be an object")
    return spec


def canonical_tool_schemas(
    tool_definitions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert official ToolMaze definitions to deterministic OpenAI tools."""
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_tool in tool_definitions:
        spec = _function_spec(raw_tool)
        name = spec.get("name")
        description = spec.get("description")
        parameters = spec.get("parameters")
        if not isinstance(name, str) or not name.strip():
            raise ProviderProtocolError("provider tool function name is empty")
        if name in seen:
            raise ProviderProtocolError(f"duplicate provider tool function: {name}")
        if not isinstance(description, str) or not description.strip():
            raise ProviderProtocolError(f"provider tool description is empty: {name}")
        if not isinstance(parameters, Mapping):
            raise ProviderProtocolError(f"provider tool parameters missing: {name}")
        parameters_copy = deepcopy(dict(parameters))
        # A few frozen official specs omit the top-level type for an
        # intentionally unconstrained property.  The OpenAI function schema
        # still requires the parameters object itself to declare object.
        parameters_copy.setdefault("type", "object")
        if parameters_copy.get("type") != "object":
            raise ProviderProtocolError(
                f"provider tool parameters.type must be object: {name}"
            )
        if not isinstance(parameters_copy.get("properties", {}), Mapping):
            raise ProviderProtocolError(f"provider tool properties must be object: {name}")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters_copy,
                },
            }
        )
        seen.add(name)
    tools.sort(key=lambda item: item["function"]["name"])
    return tools


def tool_schema_hash(tool_schemas: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical_json(list(tool_schemas))).hexdigest()


def _serialize_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ProviderProtocolError("provider tool arguments are not valid JSON") from exc
        return arguments
    return json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))


def serialize_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Serialize Odys conversation records into provider-only messages."""
    serialized: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role in {"user", "system"}:
            serialized.append({"role": role, "content": str(message.get("content", ""))})
            continue
        if role == "assistant":
            output: dict[str, Any] = {
                "role": "assistant",
                "content": str(message.get("content") or ""),
            }
            transport = message.get("_provider_transport", {})
            reasoning = transport.get("reasoning_content") if isinstance(transport, Mapping) else None
            if reasoning is not None:
                output["reasoning_content"] = reasoning
            call = message.get("tool_call")
            if call is not None:
                if not isinstance(call, Mapping):
                    raise ProviderProtocolError("assistant tool_call record is malformed")
                provider_id = call.get("provider_tool_call_id") or call.get("id")
                if not isinstance(provider_id, str) or not provider_id:
                    raise ProviderProtocolError("assistant provider tool_call_id is missing")
                name = call.get("name")
                if not isinstance(name, str) or not name:
                    raise ProviderProtocolError("assistant tool function name is missing")
                output["tool_calls"] = [
                    {
                        "id": provider_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": _serialize_arguments(call.get("arguments", {})),
                        },
                    }
                ]
            serialized.append(output)
            continue
        if role == "tool":
            provider_id = message.get("provider_tool_call_id") or message.get("tool_call_id")
            if not isinstance(provider_id, str) or not provider_id:
                raise ProviderProtocolError("tool provider tool_call_id is missing")
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            serialized.append(
                {"role": "tool", "tool_call_id": provider_id, "content": content}
            )
            continue
        raise ProviderProtocolError(f"unsupported internal message role: {role!r}")
    return serialized
