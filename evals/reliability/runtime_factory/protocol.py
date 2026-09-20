"""BenchmarkRuntime protocol — the shared interface for all runtime configs.

Both ``MinimalRuntimeFactory`` and ``OdysRuntimeFactory`` produce objects
that satisfy this protocol.  The benchmark runner interacts only through
this interface, so the two configurations are interchangeable at the
harness level while differing radically in internal capability.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from evals.reliability.run_phase4 import ExecutionOutcome


@runtime_checkable
class BenchmarkRuntime(Protocol):
    """A runtime that can execute a single benchmark task.

    Every runtime returned by a :class:`RuntimeFactory` must satisfy this
    protocol.  The runner calls :meth:`execute` with the same ``task`` and
    ``config`` dicts regardless of which factory produced the runtime.
    """

    async def execute(self, task: dict[str, Any], config: dict[str, Any]) -> ExecutionOutcome:
        """Run one benchmark task and return the observed outcome.

        Parameters
        ----------
        task:
            The frozen benchmark task definition (from the protocol snapshot).
        config:
            The benchmark config dict (features, tool_capability_set, etc.).

        Returns
        -------
        ExecutionOutcome
            Harness-neutral observations about what happened during execution.
        """
        ...


@runtime_checkable
class RecoverableBenchmarkRuntime(BenchmarkRuntime, Protocol):
    """Explicit runtime contract for validator-rejection recovery.

    The official runner may invoke this contract only for a configuration
    whose frozen features enable recovery.  A runtime that does not implement
    it is not silently treated as a recoverable runtime.
    """

    async def recover_after_validation(
        self,
        request: Any,
        outcome: ExecutionOutcome,
        validation: Any,
    ) -> ExecutionOutcome | dict[str, Any] | None:
        ...


@runtime_checkable
class ExternallyFinalizableBenchmarkRuntime(BenchmarkRuntime, Protocol):
    """Runtime contract for the external-validator-to-durable-state bridge.

    Recovery may deliberately return a candidate that is still waiting for
    external validation. Only a runtime implementing this contract may
    consume that validator verdict and project it into durable plan state.
    """

    async def finalize_after_external_validation(
        self,
        request: Any,
        outcome: ExecutionOutcome,
        validation: Any,
    ) -> dict[str, Any] | None:
        ...
