"""Generic fault layer interface.

Interface for DERIVED_WRAPPER fault injection.  NOT used in primary
ToolMaze results (FaultSource.BENCHMARK_NATIVE only).

DERIVED_WRAPPER is reserved for Terminal-Bench/TUA-Bench derived
perturbation studies.  Any result using it must be labeled:
    BENCHMARK_DERIVED_PERTURBATION
and must not be reported as an official benchmark score.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .types import FaultSource, PerturbationMode


@runtime_checkable
class FaultLayer(Protocol):
    """Interface for fault injection layers."""

    @property
    def fault_source(self) -> FaultSource: ...

    def inject_fault(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        perturbation_mode: PerturbationMode,
    ) -> dict[str, Any]:
        """Inject a fault into a tool call.

        Returns the (possibly modified) tool result.
        """
        ...

    def is_enabled_for_benchmark(self, benchmark_name: str) -> bool:
        """Check if this fault layer is enabled for the given benchmark."""
        ...


class NoOpFaultLayer:
    """No-op fault layer — returns inputs unchanged.

    Used when FaultSource is BENCHMARK_NATIVE (the benchmark handles
    its own perturbation injection).
    """

    @property
    def fault_source(self) -> FaultSource:
        return FaultSource.NONE

    def inject_fault(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        perturbation_mode: PerturbationMode,
    ) -> dict[str, Any]:
        return {"tool_name": tool_name, "input": tool_input, "faulted": False}

    def is_enabled_for_benchmark(self, benchmark_name: str) -> bool:
        return False


class DerivedWrapperFaultLayer:
    """Derived wrapper fault layer.

    MUST NOT be used in primary ToolMaze results.
    Reserved for Terminal-Bench/TUA-Bench derived perturbation studies.
    """

    def __init__(self, wrapper_configs: dict[str, Any] | None = None):
        self._configs = wrapper_configs or {}

    @property
    def fault_source(self) -> FaultSource:
        return FaultSource.DERIVED_WRAPPER

    def inject_fault(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        perturbation_mode: PerturbationMode,
    ) -> dict[str, Any]:
        """Inject a derived perturbation.

        Results using this MUST be labeled BENCHMARK_DERIVED_PERTURBATION.
        """
        config = self._configs.get(tool_name, {})
        if not config:
            return {"tool_name": tool_name, "input": tool_input, "faulted": False}
        return {
            "tool_name": tool_name,
            "input": tool_input,
            "faulted": True,
            "fault_type": "DERIVED_WRAPPER",
            "label": "BENCHMARK_DERIVED_PERTURBATION",
            "config": config,
        }

    def is_enabled_for_benchmark(self, benchmark_name: str) -> bool:
        # Only enabled for non-ToolMaze benchmarks in derived mode
        return benchmark_name not in {"toolmaze"}


def create_fault_layer(
    fault_source: FaultSource,
    configs: dict[str, Any] | None = None,
) -> FaultLayer:
    """Factory for fault layers."""
    if fault_source == FaultSource.BENCHMARK_NATIVE:
        return NoOpFaultLayer()
    elif fault_source == FaultSource.DERIVED_WRAPPER:
        return DerivedWrapperFaultLayer(configs)
    else:
        return NoOpFaultLayer()
