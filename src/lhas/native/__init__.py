"""Odys-owned provider/tool execution loop and reliability primitives."""

from .models import (
    CandidateStatus,
    CompletionCandidate,
    ExecutionSnapshot,
    ModelContext,
    NativePhase,
    ProviderResponse,
    ProviderToolCall,
    ReconciliationDecision,
    ReplanSignal,
    ToolInvocation,
    ValidationFailure,
    RuntimeTarget,
    ProviderFailureCategory,
    ProviderHealthState,
    TargetSwitch,
    TargetSwitchState,
)
from .completion import AcceptedCompletionValidator, CompletionAuthority
from .executor import NativeAgentExecutor
from .kernel import NativeAgentKernel
from .parser import ModelResponseParser
from .provider import OpenAIChatProviderAdapter, OfflineCompletionProvider, ProviderAdapter, ScriptedProviderAdapter
from .tools import NativeToolDispatcher
from .runtime import ProviderFailureClassifier, ProviderHealthRepository, RuntimeTargetController, RuntimeTargetError, RuntimeTargetResolver
from .persistence import ExecutionSnapshotRepository

__all__ = [
    "CandidateStatus",
    "CompletionCandidate",
    "ExecutionSnapshot",
    "ModelContext",
    "NativePhase",
    "ProviderResponse",
    "ProviderToolCall",
    "ReconciliationDecision",
    "ReplanSignal",
    "ToolInvocation",
    "ValidationFailure",
    "AcceptedCompletionValidator",
    "CompletionAuthority",
    "ModelResponseParser",
    "NativeAgentExecutor",
    "NativeAgentKernel",
    "NativeToolDispatcher",
    "OpenAIChatProviderAdapter",
    "ProviderAdapter",
    "ScriptedProviderAdapter",
    "OfflineCompletionProvider",
    "RuntimeTarget",
    "ProviderFailureCategory",
    "ProviderHealthState",
    "TargetSwitch",
    "TargetSwitchState",
    "ProviderFailureClassifier",
    "ProviderHealthRepository",
    "RuntimeTargetController",
    "RuntimeTargetError",
    "RuntimeTargetResolver",
    "ExecutionSnapshotRepository",
]
