"""Extension boundaries for future benchmark adapters.

TerminalBenchAdapter and TUABenchAdapter are provided as extension points.
They must NOT be used in primary ToolMaze results.
Any integration must preserve native benchmark semantics.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from .types import (
    BenchmarkAdapter,
    BenchmarkIdentity,
    BenchmarkName,
    NativeResult,
    PerturbationMode,
    RuntimeTask,
    TaskDescriptor,
)


class TerminalBenchAdapter:
    """Extension boundary for Terminal-Bench integration.

    NOT IMPLEMENTED — reserved for future use.
    Must not fabricate unsupported integrations.
    """

    def __init__(self):
        raise NotImplementedError(
            "TerminalBenchAdapter is an extension boundary. "
            "Implement only when Terminal-Bench integration is available."
        )


class TUABenchAdapter:
    """Extension boundary for TUA-Bench integration.

    NOT IMPLEMENTED — reserved for future use.
    Must not fabricate unsupported integrations.
    """

    def __init__(self):
        raise NotImplementedError(
            "TUABenchAdapter is an extension boundary. "
            "Implement only when TUA-Bench integration is available."
        )
