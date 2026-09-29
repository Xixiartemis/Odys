"""Pure provider-lock validation for provider-free preflight and live entrypoints."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


class ProviderLockError(ValueError):
    """Raised when a resolved provider configuration violates the frozen lock."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def load_provider_lock(path: str | Path) -> dict[str, Any]:
    lock = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(lock, dict):
        raise ProviderLockError("provider lock must be a JSON object")
    return lock


def provider_config_hash(lock: Mapping[str, Any]) -> str:
    """Hash only non-secret, request-relevant frozen provider settings."""
    fields = {
        key: lock.get(key)
        for key in (
            "provider",
            "exact_model_id",
            "endpoint",
            "temperature",
            "top_p",
            "effective_temperature",
            "effective_top_p",
            "sampling_parameters_sent",
            "max_output_tokens",
            "max_completion_tokens",
            "request_timeout_seconds",
            "sdk_max_retries",
            "provider_retry_policy",
            "thinking_enabled",
            "supports_tool_choice",
            "supports_parallel_tool_calls",
        )
    }
    return hashlib.sha256(canonical_json(fields)).hexdigest()


def normalize_endpoint(value: str) -> str:
    """Return a secret-free, stable endpoint identity string."""
    raw = value.strip()
    if not raw:
        raise ProviderLockError("provider endpoint is empty")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderLockError("provider endpoint must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ProviderLockError("provider endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ProviderLockError("provider endpoint must not contain query or fragment")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


def resolve_endpoint_identity(
    lock: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    require_frozen_endpoint: bool = False,
) -> dict[str, str]:
    """Resolve endpoint provenance without reading or returning an API key."""
    source = str(lock.get("endpoint", ""))
    env = os.environ if environ is None else environ
    if source.startswith("from_environment_"):
        env_name = source.removeprefix("from_environment_")
        resolved = env.get(env_name, "")
        if not resolved:
            raise ProviderLockError(f"required endpoint environment variable is unset: {env_name}")
        source_kind = f"environment:{env_name}"
    else:
        resolved = source
        source_kind = "provider-lock"
    normalized = normalize_endpoint(resolved)
    frozen = lock.get("frozen_endpoint")
    if require_frozen_endpoint and frozen is not None and normalized != normalize_endpoint(str(frozen)):
        raise ProviderLockError(
            f"resolved provider endpoint differs from frozen endpoint: {normalized} != {frozen}"
        )
    return {
        "source": source_kind,
        "normalized_endpoint": normalized,
        "endpoint_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def expected_resolved_config(lock: Mapping[str, Any]) -> dict[str, Any]:
    if "max_completion_tokens" in lock:
        return {
            "provider": lock.get("provider"),
            "model_id": lock.get("exact_model_id"),
            "thinking_enabled": lock.get("thinking_enabled"),
            "effective_temperature": lock.get("effective_temperature"),
            "effective_top_p": lock.get("effective_top_p"),
            "sampling_parameters_sent": lock.get("sampling_parameters_sent", False),
            "max_completion_tokens": lock.get("max_completion_tokens"),
            "request_timeout": lock.get("request_timeout_seconds"),
            "sdk_max_retries": lock.get("sdk_max_retries", 0),
            "supports_tool_choice": lock.get("supports_tool_choice"),
            "supports_parallel_tool_calls": lock.get("supports_parallel_tool_calls", False),
        }
    return {
        "provider": lock.get("provider"),
        "model_id": lock.get("exact_model_id"),
        "temperature": lock.get("temperature"),
        "top_p": lock.get("top_p"),
        "max_output_tokens": lock.get("max_output_tokens"),
        "request_timeout": lock.get("request_timeout_seconds"),
        "max_retries": 3,
        "thinking_enabled": lock.get("thinking_enabled"),
        "supports_tool_choice": lock.get("supports_tool_choice"),
        "supports_parallel_tool_calls": lock.get(
            "supports_parallel_tool_calls", False
        ),
    }


def validate_resolved_config(
    actual: Mapping[str, Any],
    lock: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a redacted parity report or raise before any provider request."""
    expected = expected_resolved_config(lock)
    mismatches = {
        key: {"actual": actual.get(key), "expected": value}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise ProviderLockError(f"provider-lock mismatch: {mismatches}")
    return {
        "valid": True,
        **{key: expected[key] for key in expected if key != "valid"},
    }


def validate_manifest_generation_config(
    generation_config: Mapping[str, Any],
    lock: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate manifest settings without constructing a live driver."""
    if "max_completion_tokens" in generation_config or "sampling_parameters_sent" in generation_config:
        actual = {
            "provider": generation_config.get("provider"),
            "model_id": generation_config.get("model_id"),
            "thinking_enabled": generation_config.get("thinking_enabled"),
            "effective_temperature": generation_config.get("effective_temperature"),
            "effective_top_p": generation_config.get("effective_top_p"),
            "sampling_parameters_sent": generation_config.get("sampling_parameters_sent", False),
            "max_completion_tokens": generation_config.get("max_completion_tokens"),
            "request_timeout": generation_config.get("request_timeout"),
            "sdk_max_retries": generation_config.get("sdk_max_retries", 0),
            "supports_tool_choice": generation_config.get("supports_tool_choice"),
            "supports_parallel_tool_calls": generation_config.get("supports_parallel_tool_calls", False),
        }
    else:
        # Historical manifests remain readable and qualification-only.  They
        # are never accepted as the corrected live-execution identity.
        actual = {
            "provider": generation_config.get("provider"),
            "model_id": generation_config.get("model_id"),
            "temperature": generation_config.get("temperature"),
            "top_p": generation_config.get("top_p"),
            "max_output_tokens": generation_config.get("max_output_tokens"),
            "request_timeout": generation_config.get("request_timeout"),
            "max_retries": generation_config.get("max_retries"),
            "thinking_enabled": generation_config.get("thinking_enabled"),
            "supports_tool_choice": generation_config.get("supports_tool_choice"),
            "supports_parallel_tool_calls": generation_config.get(
                "supports_parallel_tool_calls", False
            ),
        }
        # The lock now contains the corrected live-only MiMo settings.  Keep
        # the legacy comparison self-contained so historical qualification
        # manifests remain readable without inheriting live settings.
        expected = {
            "provider": lock.get("provider"),
            "model_id": lock.get("exact_model_id"),
            "temperature": lock.get("temperature"),
            "top_p": lock.get("top_p"),
            "max_output_tokens": lock.get("max_output_tokens"),
            "request_timeout": lock.get("request_timeout_seconds"),
            "max_retries": 3,
            "thinking_enabled": lock.get("thinking_enabled"),
            "supports_tool_choice": lock.get("supports_tool_choice"),
            "supports_parallel_tool_calls": lock.get(
                "supports_parallel_tool_calls", False
            ),
        }
    if "max_completion_tokens" in generation_config or "sampling_parameters_sent" in generation_config:
        expected = expected_resolved_config(lock)
    mismatches = {
        key: {"actual": actual.get(key), "expected": value}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise ProviderLockError(f"manifest/provider-lock mismatch: {mismatches}")
    return {
        "valid": True,
        "provider_config_hash": provider_config_hash(lock),
        "resolved_config": actual,
    }
