"""RuntimeFactory abstract base class.

A ``RuntimeFactory`` produces a :class:`BenchmarkRuntime` for a given
benchmark config.  Subclasses decide which kernel components to include.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from evals.reliability.runtime_factory.protocol import BenchmarkRuntime


class RuntimeFactory(ABC):
    """Abstract factory that creates a :class:`BenchmarkRuntime`.

    Each subclass represents one benchmark configuration (minimal or odys).
    The factory is responsible for constructing the kernel with the correct
    set of capabilities and wrapping it in a runtime that satisfies the
    :class:`BenchmarkRuntime` protocol.
    """

    @abstractmethod
    def create_runtime(self, config: dict[str, Any]) -> BenchmarkRuntime:
        """Create and return a runtime for the given benchmark config.

        Parameters
        ----------
        config:
            The benchmark config dict.  Must contain at least ``config_id``
            and ``features``.

        Returns
        -------
        BenchmarkRuntime
            A runtime implementing the shared benchmark interface.
        """
        ...
