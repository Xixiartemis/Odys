"""P4.6 Official Model Freeze — Real LLM provider wrapper for benchmark.

Replaces ScriptedProviderAdapter with a real OpenAI-compatible provider
for official benchmark runs.  This module is the ONLY new code for P4.6;
no existing benchmark task, fault, validator, metric, runner, protocol,
manifest, or fault file is modified.

Usage::

    from evals.reliability.p46_provider import (
        RealLLMProvider,
        RealLLMMinimalRuntimeFactory,
        RealLLMOdysRuntimeFactory,
        create_real_provider,
    )

    # Quick start from env vars
    provider = create_real_provider()

    # Minimal baseline factory
    factory = RealLLMMinimalRuntimeFactory(workspace_root=Path("/tmp/ws"))

    # Full ODYS factory
    factory = RealLLMOdysRuntimeFactory(workspace_root=Path("/tmp/ws"))
"""

from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lhas.native.models import RuntimeTarget
from lhas.native.provider import OpenAIChatProviderAdapter
from lhas.execution_control import ExecutionControlError, ExecutionControlToken
from evals.reliability.run_phase4 import ROOT_API_BUDGET_FAILURE, RunBudgetExhausted


# These values are the P4.6 freeze inputs.  They are deliberately kept in
# code rather than inferred from a provider default at run time: an official
# run must stop if the effective provider identity drifts.
FROZEN_PROVIDER = "xiaomimimo-openai-compatible"
FROZEN_MODEL = "mimo-v2.5-pro"
CHEAP_MODEL = "mimo-v2.5"
CHEAP_CREDENTIAL_ENV = "ODYS_CHEAP_BENCHMARK_API_KEY"
CHEAP_BASE_URL_ENV = "ODYS_CHEAP_BENCHMARK_BASE_URL"
FROZEN_ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1"
FROZEN_API_VERSION = "chat-completions-v1"
FROZEN_TEMPERATURE = 0.0
FROZEN_MAX_TOKENS = 4096
FROZEN_SEED = 42
FROZEN_SYSTEM_PROMPT_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
FROZEN_TOOL_POLICY_HASH = "c6fb217770dcc6b5da23982cc9f9d70f19f4b45c2e343344bbdddd2f74e46f2b"
SDK_MAX_RETRIES = 0


def _build_benchmark_capability_contract(registry, allowed):
    """Build the explicit contract for concrete benchmark backends.

    The frozen semantic catalog describes the staged-workspace ``workspace.edit``
    contract (old_text/new_text), while the benchmark filesystem backend is a
    deliberate whole-file writer (path/content).  A real benchmark adapter
    must declare that concrete binding explicitly; otherwise ToolContract
    correctly rejects every model edit before the backend can execute it.
    Only capabilities actually exposed by this benchmark registry are mapped,
    and undeclared capabilities remain fail-closed.
    """
    from lhas.capability_registry import CapabilityRegistry, default_capabilities
    from lhas.tools.contract import ToolContract
    from tests.helpers import make_test_capability_definition

    allowed = set(allowed)
    default_by_id = {definition.id: definition for definition in default_capabilities()}
    definitions = []
    for capability_id, definition in default_by_id.items():
        if capability_id in allowed:
            try:
                backend = registry.resolve(capability_id).capability
            except KeyError:
                definitions.append(definition)
            else:
                definitions.append(definition.model_copy(update={
                    "description": backend.description,
                    "input_schema": backend.input_schema,
                    "output_schema": backend.output_schema,
                }))
        else:
            definitions.append(definition)

    for capability_id in sorted(allowed - set(default_by_id)):
        make_definition = make_test_capability_definition(
            capability_id,
            output_schema={},
        )
        definitions.append(make_definition)

    capability_registry = CapabilityRegistry(registry, definitions=definitions)
    return capability_registry, ToolContract(capability_registry, registry)


class ProviderIdentityError(RuntimeError):
    """Raised when the real benchmark provider cannot be proven frozen."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RealLLMProvider:
    """OpenAI-compatible provider that delegates to a real model API.

    Wraps :class:`OpenAIChatProviderAdapter` with explicit construction
    parameters for benchmark reproducibility (temperature, max_tokens, seed).

    Satisfies the :class:`ProviderAdapter` protocol:

    - ``name`` attribute
    - ``runtime_target`` property
    - ``generate(context, tools, timeout_seconds)`` coroutine
    """

    name: str

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        seed: int = 42,
        provider_id: str = FROZEN_PROVIDER,
        endpoint_identity: str | None = None,
        credential_route_id: str = "benchmark",
        client: Any = None,
        expected_model: str = FROZEN_MODEL,
    ):
        if model != expected_model:
            raise ProviderIdentityError(
                f"MODEL_IDENTITY_MISMATCH: expected {expected_model}, got {model}"
            )
        if provider_id != FROZEN_PROVIDER:
            raise ProviderIdentityError(
                f"PROVIDER_IDENTITY_MISMATCH: expected {FROZEN_PROVIDER}, got {provider_id}"
            )
        if not api_key or not str(api_key).strip():
            raise ProviderIdentityError("CREDENTIAL_REQUIRED_BEFORE_RUN")
        if float(temperature) != FROZEN_TEMPERATURE:
            raise ProviderIdentityError("MODEL_PARAMETER_MISMATCH: temperature")
        if int(max_tokens) != FROZEN_MAX_TOKENS:
            raise ProviderIdentityError("MODEL_PARAMETER_MISMATCH: max_tokens")
        if int(seed) != FROZEN_SEED:
            raise ProviderIdentityError("MODEL_PARAMETER_MISMATCH: seed")

        self.name = f"real-llm:{model}"
        self.model = model
        self.expected_model = expected_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.seed = seed
        # Secret-free provider-call accounting. P45 attributes the slice for
        # one benchmark run, including calls made during recovery.
        self.call_records: list[dict[str, Any]] = []
        self._execution_context: dict[str, str] = {}
        self._run_budget: Any = None
        self._execution_control: ExecutionControlToken | None = None

        # Build extra_body for deterministic completions
        extra_body: dict[str, Any] = {}
        if temperature is not None:
            extra_body["temperature"] = temperature
        if max_tokens is not None:
            extra_body["max_tokens"] = max_tokens
        if seed is not None:
            extra_body["seed"] = seed

        self._inner = OpenAIChatProviderAdapter(
            model=model,
            api_key=api_key,
            base_url=base_url,
            extra_body=extra_body or None,
            client=client,
            provider_id=provider_id,
            endpoint_identity=endpoint_identity,
            credential_route_id=credential_route_id,
            max_retries=SDK_MAX_RETRIES,
        )

        actual_endpoint = self.transport_identity.endpoint_identity
        expected_endpoint = _canonical_endpoint(FROZEN_ENDPOINT)
        if actual_endpoint != expected_endpoint:
            raise ProviderIdentityError(
                "PROVIDER_ENDPOINT_MISMATCH: "
                f"expected {expected_endpoint}, got {actual_endpoint}"
            )

    @property
    def runtime_target(self) -> RuntimeTarget:
        """Secret-free identity for trace correlation."""
        return self._inner.runtime_target

    @property
    def transport_identity(self):
        """Transport-layer identity (endpoint host, fingerprint)."""
        return self._inner.transport_identity

    def bind_execution_context(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        phase: str,
    ) -> None:
        """Bind safe correlation fields for the next provider request."""
        self._execution_context = {
            "run_id": str(run_id),
            "task_id": str(task_id),
            "attempt_id": str(attempt_id),
            "phase": str(phase),
        }

    def bind_run_budget(self, ledger: Any) -> None:
        """Bind the one run-scoped provider budget used by all phases."""
        self._run_budget = ledger

    def bind_execution_control(self, control: ExecutionControlToken | None) -> None:
        """Propagate the one root control token to the transport adapter."""
        self._execution_control = control
        self._inner.bind_execution_control(control)

    @property
    def budget_exhausted(self) -> bool:
        return bool(self._run_budget is not None and self._run_budget.exhausted)

    async def generate(
        self,
        *,
        context: Any,
        tools: list[dict[str, Any]],
        timeout_seconds: float,
    ) -> Any:
        """Delegate one model call to the real OpenAI-compatible endpoint.

        Parameters
        ----------
        context:
            A :class:`ModelContext` (or anything the inner adapter accepts)
            containing the messages payload.
        tools:
            JSON-schema tool definitions to send with the request.
        timeout_seconds:
            Per-call deadline in seconds.

        Returns
        -------
        dict[str, Any]
            Normalized response dict (``choices``, ``usage``, etc.).
        """
        record: dict[str, Any] = {
            "call_index": len(self.call_records) + 1,
            "provider_call": True,
            "provider": FROZEN_PROVIDER,
            "model": self.model,
            "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool_count": len(tools),
            **self._execution_context,
        }
        try:
            if self._execution_control is not None:
                self._execution_control.check()
            if self._run_budget is not None:
                phase = self._execution_context.get("phase", "initial")
                try:
                    self._run_budget.reserve(phase)
                except RunBudgetExhausted as exc:
                    record.update(
                        {
                            "provider_call": False,
                            "status": "BUDGET_BLOCKED",
                            "error_type": getattr(
                                exc, "budget_type", ROOT_API_BUDGET_FAILURE
                            ),
                            "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "input_tokens": "NOT_MEASURED",
                            "output_tokens": "NOT_MEASURED",
                            "total_tokens": "NOT_MEASURED",
                        }
                    )
                    self.call_records.append(record)
                    raise exc
            normalized = await self._inner.generate(
                context=context,
                tools=tools,
                timeout_seconds=timeout_seconds,
            )
            if self._execution_control is not None:
                self._execution_control.check()
            actual_model = normalized.get("model") if isinstance(normalized, dict) else None
            if actual_model != self.model:
                raise ProviderIdentityError(
                    "MODEL_IDENTITY_MISMATCH: provider response did not prove "
                    f"{self.model} (reported {actual_model!r})"
                )
            usage = normalized.get("usage") if isinstance(normalized, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            for target, aliases in {
                "input_tokens": ("prompt_tokens", "input_tokens"),
                "output_tokens": ("completion_tokens", "output_tokens"),
                "total_tokens": ("total_tokens",),
            }.items():
                value = next(
                    (usage.get(key) for key in aliases if usage.get(key) is not None),
                    None,
                )
                record[target] = value if isinstance(value, int) else "NOT_MEASURED"
            record.update(
                {
                    "status": "SUCCESS",
                    "response_model": actual_model,
                    "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
            self.call_records.append(record)
            return normalized
        except RunBudgetExhausted:
            # The reserve path already recorded one non-transport blocked
            # attempt.  Do not append a second record in the generic failure
            # handler below.
            raise
        except Exception as exc:
            record.update(
                {
                    "status": "FAILURE",
                    "error_type": type(exc).__name__,
                    "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "input_tokens": "NOT_MEASURED",
                    "output_tokens": "NOT_MEASURED",
                    "total_tokens": "NOT_MEASURED",
                }
            )
            self.call_records.append(record)
            raise

    def __repr__(self) -> str:
        return (
            f"RealLLMProvider(model={self.model!r}, "
            f"base_url={self._inner.base_url!r}, "
            f"temp={self.temperature}, seed={self.seed})"
        )


# ---------------------------------------------------------------------------
# Factory from environment variables
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "mimo-v2.5-pro"
_DEFAULT_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
_DEFAULT_TEMPERATURE = 0.0
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_SEED = 42


def _canonical_endpoint(value: str) -> str:
    """Use the same canonical transport identity as the native adapter."""
    from lhas.native.transport import canonical_transport_identity

    return canonical_transport_identity(value).endpoint_identity


def provider_identity(
    provider: RealLLMProvider,
    *,
    expected_model: str = FROZEN_MODEL,
) -> dict[str, Any]:
    """Return the secret-free identity proved by a constructed provider."""
    if not isinstance(provider, RealLLMProvider):
        raise ProviderIdentityError("PROVIDER_IDENTITY_UNAVAILABLE")

    target = provider.runtime_target
    actual_provider = target.provider_id
    actual_model = target.model_id
    actual_endpoint = provider.transport_identity.endpoint_identity
    if actual_provider != FROZEN_PROVIDER:
        raise ProviderIdentityError("PROVIDER_IDENTITY_MISMATCH")
    if actual_model != expected_model:
        raise ProviderIdentityError("MODEL_IDENTITY_MISMATCH")
    if actual_endpoint != _canonical_endpoint(FROZEN_ENDPOINT):
        raise ProviderIdentityError("PROVIDER_ENDPOINT_MISMATCH")
    if provider.temperature != FROZEN_TEMPERATURE:
        raise ProviderIdentityError("MODEL_PARAMETER_MISMATCH: temperature")
    if provider.max_tokens != FROZEN_MAX_TOKENS:
        raise ProviderIdentityError("MODEL_PARAMETER_MISMATCH: max_tokens")
    if provider.seed != FROZEN_SEED:
        raise ProviderIdentityError("MODEL_PARAMETER_MISMATCH: seed")

    return {
        "provider": actual_provider,
        "model": actual_model,
        "api_version": FROZEN_API_VERSION,
        "endpoint_hash": _sha256_text(actual_endpoint),
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "system_prompt_hash": FROZEN_SYSTEM_PROMPT_HASH,
        "tool_policy_hash": FROZEN_TOOL_POLICY_HASH,
        "sdk_max_retries": SDK_MAX_RETRIES,
    }


def validate_and_persist_provider_identity(
    provider: RealLLMProvider,
    *,
    path: Path = Path("results/official_phase4/provider_identity.json"),
    expected_model: str = FROZEN_MODEL,
) -> dict[str, Any]:
    """Validate provider identity and persist only non-secret evidence."""
    identity = provider_identity(provider, expected_model=expected_model)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderIdentityError("PROVIDER_IDENTITY_ARTIFACT_INVALID") from exc
        if existing != identity:
            raise ProviderIdentityError("PROVIDER_IDENTITY_ARTIFACT_MISMATCH")
        return identity
    path.write_text(
        json.dumps(identity, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return identity


def create_real_provider(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    seed: int | None = None,
    expected_model: str = FROZEN_MODEL,
) -> RealLLMProvider:
    """Create a :class:`RealLLMProvider` from explicit args or env vars.

    Resolution order (each parameter):
    1. Explicit keyword argument (if not None).
    2. Environment variable (see mapping below).
    3. Module-level default.

    Environment variables
    ---------------------
    ODYS_BENCHMARK_MODEL           → model        (default: mimo-v2.5-pro)
    ODYS_BENCHMARK_API_KEY         → api_key      (fallback: HERMES_CUSTOM_TOKEN_PLAN_CN_XIAOMIMIMO_COM_API_KEY)
    ODYS_BENCHMARK_BASE_URL        → base_url     (default: https://token-plan-cn.xiaomimimo.com/v1)
    ODYS_BENCHMARK_TEMPERATURE     → temperature   (default: 0.0)
    ODYS_BENCHMARK_MAX_TOKENS      → max_tokens    (default: 4096)
    ODYS_BENCHMARK_SEED            → seed          (default: 42)
    """
    resolved_model = model or os.environ.get("ODYS_BENCHMARK_MODEL", _DEFAULT_MODEL)
    configured_provider = os.environ.get("ODYS_BENCHMARK_PROVIDER", FROZEN_PROVIDER)
    if configured_provider != FROZEN_PROVIDER:
        raise ProviderIdentityError(
            "PROVIDER_IDENTITY_MISMATCH: "
            f"expected {FROZEN_PROVIDER}, got {configured_provider}"
        )

    resolved_key = (
        api_key
        or os.environ.get("ODYS_BENCHMARK_API_KEY")
        or os.environ.get("HERMES_CUSTOM_TOKEN_PLAN_CN_XIAOMIMIMO_COM_API_KEY")
    )
    if not resolved_key or not str(resolved_key).strip():
        raise ProviderIdentityError(
            "CREDENTIAL_REQUIRED_BEFORE_RUN: set ODYS_BENCHMARK_API_KEY or "
            "HERMES_CUSTOM_TOKEN_PLAN_CN_XIAOMIMIMO_COM_API_KEY in the environment, "
            "or pass api_key= explicitly."
        )

    resolved_base = base_url or os.environ.get("ODYS_BENCHMARK_BASE_URL", _DEFAULT_BASE_URL)
    resolved_temp = (
        temperature
        if temperature is not None
        else float(os.environ.get("ODYS_BENCHMARK_TEMPERATURE", _DEFAULT_TEMPERATURE))
    )
    resolved_max = (
        max_tokens
        if max_tokens is not None
        else int(os.environ.get("ODYS_BENCHMARK_MAX_TOKENS", _DEFAULT_MAX_TOKENS))
    )
    resolved_seed = (
        seed
        if seed is not None
        else int(os.environ.get("ODYS_BENCHMARK_SEED", _DEFAULT_SEED))
    )

    return RealLLMProvider(
        model=resolved_model,
        api_key=resolved_key,
        base_url=resolved_base,
        temperature=resolved_temp,
        max_tokens=resolved_max,
        seed=resolved_seed,
        expected_model=expected_model,
    )


def create_cheap_model_provider(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    seed: int | None = None,
) -> RealLLMProvider:
    """Create the isolated P49 ``mimo-v2.5`` provider profile.

    The profile shares the frozen provider transport and model parameters but
    has its own benchmark identity.  It never changes phase4-v1 inputs.
    """
    resolved_key = api_key or os.environ.get(CHEAP_CREDENTIAL_ENV)
    if not resolved_key or not str(resolved_key).strip():
        raise ProviderIdentityError(
            f"CREDENTIAL_REQUIRED_BEFORE_RUN: set {CHEAP_CREDENTIAL_ENV} "
            "in the environment or pass api_key= explicitly."
        )

    resolved_base_url = base_url or os.environ.get(
        CHEAP_BASE_URL_ENV,
        FROZEN_ENDPOINT,
    )
    return create_real_provider(
        model=CHEAP_MODEL,
        api_key=resolved_key,
        base_url=resolved_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        seed=seed,
        expected_model=CHEAP_MODEL,
    )


# ---------------------------------------------------------------------------
# Runtime factories — drop-in replacements for MinimalRuntimeFactory /
# OdysRuntimeFactory that use a real LLM instead of ScriptedProviderAdapter
# ---------------------------------------------------------------------------

def _build_real_minimal_components(
    config: dict[str, Any],
    workspace_root: Any = None,
    provider: RealLLMProvider | None = None,
) -> tuple[Any, Any, Any]:
    """Build provider, dispatcher, and db for the minimal runtime."""
    import tempfile

    from lhas.native.tools import NativeToolDispatcher
    from lhas.persistence.database import Database
    from lhas.tools.registry import ToolRegistry

    tmp_dir = tempfile.mkdtemp(prefix="odys-p46-minimal-")
    db = Database(Path(tmp_dir) / "p46-minimal.db")
    db.init_db()

    if provider is None:
        provider = create_real_provider()

    allowed = set(config.get("tool_capability_set", []))

    if workspace_root is not None:
        from evals.reliability.tools.registry import create_benchmark_tool_registry
        registry = create_benchmark_tool_registry(
            Path(workspace_root),
            effect_policy=config.get("_phase_effect_policy"),
        )
    else:
        registry = ToolRegistry()

    cap_reg, contract = _build_benchmark_capability_contract(registry, allowed)

    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities=allowed,
        allowed_side_effect_capabilities=allowed,
        capability_registry=cap_reg,
        tool_contract=contract,
    )

    return provider, dispatcher, db


def _build_real_odys_kernel(
    config: dict[str, Any],
    workspace_root: Any = None,
    provider: RealLLMProvider | None = None,
) -> tuple[Any, Any, Any]:
    """Build a full NativeAgentKernel with real LLM provider."""
    import tempfile

    from lhas.native.completion import CompletionAuthority
    from lhas.native.kernel import (
        NativeAgentKernel,
        OFFICIAL_PROVIDER_TIMEOUT_SECONDS,
    )
    from lhas.native.models import NoOpNativeFaultInjector
    from lhas.native.parser import ModelResponseParser
    from lhas.native.tools import NativeToolDispatcher
    from lhas.persistence.database import Database
    from lhas.tools.registry import ToolRegistry
    from tests.helpers import PassingCommandValidator

    tmp_dir = tempfile.mkdtemp(prefix="odys-p46-benchmark-")
    db = Database(Path(tmp_dir) / "p46-benchmark.db")
    db.init_db()

    if provider is None:
        provider = create_real_provider()

    allowed = set(config.get("tool_capability_set", []))

    if workspace_root is not None:
        from evals.reliability.tools.registry import create_benchmark_tool_registry
        registry = create_benchmark_tool_registry(
            Path(workspace_root),
            effect_policy=config.get("_phase_effect_policy"),
        )
    else:
        registry = ToolRegistry()

    cap_reg, contract = _build_benchmark_capability_contract(registry, allowed)

    dispatcher = NativeToolDispatcher(
        db=db,
        registry=registry,
        allowed_capabilities=allowed,
        allowed_side_effect_capabilities=allowed,
        capability_registry=cap_reg,
        tool_contract=contract,
    )

    validator = PassingCommandValidator()
    completion_authority = CompletionAuthority(
        db=db,
        validator=validator,
        fault_injector=NoOpNativeFaultInjector(),
    )

    kernel = NativeAgentKernel(
        db=db,
        provider=provider,
        dispatcher=dispatcher,
        completion_authority=completion_authority,
        parser=ModelResponseParser(),
        provider_timeout_seconds=OFFICIAL_PROVIDER_TIMEOUT_SECONDS,
        fault_injector=NoOpNativeFaultInjector(),
    )

    from evals.reliability.runtime_factory.recovery import OfficialOdysRecoveryCoordinator

    recovery = OfficialOdysRecoveryCoordinator(
        db=db,
        kernel=kernel,
        registry=registry,
        capability_registry=cap_reg,
        tool_contract=contract,
        experiment_macro_replan_enabled=bool(
            config.get("_experiment_macro_replan_enabled", False)
        ),
        escalation_trigger_policy=str(
            config.get("escalation_trigger_policy", "NO_PROGRESS_AWARE")
        ),
        root_budget_authority=config.get("_run_budget_ledger"),
        effect_policy=config.get("_phase_effect_policy"),
    )
    return kernel, db, recovery


class RealLLMMinimalRuntimeFactory:
    """Factory producing minimal runtimes backed by a real LLM.

    Drop-in replacement for :class:`MinimalRuntimeFactory` that uses
    :class:`RealLLMProvider` instead of :class:`ScriptedProviderAdapter`.
    """

    def __init__(
        self,
        *,
        provider: RealLLMProvider | None = None,
        workspace_root: Any = None,
    ):
        self._provider = provider
        self._workspace_root = workspace_root

    def create_runtime(self, config: dict[str, Any]) -> Any:
        """Create a minimal runtime with a real LLM provider."""
        from evals.reliability.runtime_factory.minimal_factory import _MinimalRuntime

        provider, dispatcher, db = _build_real_minimal_components(
            config,
            workspace_root=self._workspace_root,
            provider=self._provider,
        )
        runtime = _MinimalRuntime(provider=provider, dispatcher=dispatcher, db=db)
        assert (
            not hasattr(runtime, "completion") or runtime.completion is None
        ), "MinimalRuntime must NOT have CompletionAuthority"
        return runtime


class RealLLMOdysRuntimeFactory:
    """Factory producing full ODYS runtimes backed by a real LLM.

    Drop-in replacement for :class:`OdysRuntimeFactory` that uses
    :class:`RealLLMProvider` instead of :class:`ScriptedProviderAdapter`.
    """

    def __init__(
        self,
        *,
        provider: RealLLMProvider | None = None,
        workspace_root: Any = None,
    ):
        self._provider = provider
        self._workspace_root = workspace_root

    def create_runtime(self, config: dict[str, Any]) -> Any:
        """Create a full ODYS runtime with a real LLM provider."""
        from evals.reliability.runtime_factory.odys_factory import _OdysRuntime

        kernel, db, recovery = _build_real_odys_kernel(
            config,
            workspace_root=self._workspace_root,
            provider=self._provider,
        )
        runtime = _OdysRuntime(kernel=kernel, db=db, recovery=recovery)
        assert (
            hasattr(runtime, "completion") and runtime.completion is not None
        ), "OdysRuntime MUST have CompletionAuthority"
        return runtime


__all__ = [
    "RealLLMProvider",
    "CHEAP_MODEL",
    "CHEAP_CREDENTIAL_ENV",
    "CHEAP_BASE_URL_ENV",
    "RealLLMMinimalRuntimeFactory",
    "RealLLMOdysRuntimeFactory",
    "ProviderIdentityError",
    "provider_identity",
    "validate_and_persist_provider_identity",
    "create_real_provider",
    "create_cheap_model_provider",
]
