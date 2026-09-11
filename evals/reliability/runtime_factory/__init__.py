"""Runtime factory separation for minimal vs odys benchmark configurations.

This package provides two factories that produce ``BenchmarkRuntime`` objects
with the same interface but different internal capabilities:

- ``MinimalRuntimeFactory``: no CompletionAuthority, no failure provenance,
  no selective repair, no recovery loop.
- ``OdysRuntimeFactory``: full ``NativeAgentKernel`` with CompletionAuthority,
  failure provenance, selective repair, and recovery loop.
"""

from evals.reliability.runtime_factory.base import RuntimeFactory
from evals.reliability.runtime_factory.minimal_factory import MinimalRuntimeFactory
from evals.reliability.runtime_factory.odys_factory import OdysRuntimeFactory
from evals.reliability.runtime_factory.protocol import BenchmarkRuntime

__all__ = [
    "BenchmarkRuntime",
    "RuntimeFactory",
    "MinimalRuntimeFactory",
    "OdysRuntimeFactory",
]
