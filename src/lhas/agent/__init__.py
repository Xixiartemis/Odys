from lhas.agent.context import AssembledContext, ContextAssembler, ContextPriority, ContextSource
from lhas.agent.kernel import AgentKernel, ScriptedAgentKernel, WorkerAgentKernelAdapter
from lhas.agent.failure import FailureRouter, FailureSignal
from lhas.agent.models import AgentBudget, AgentRequest, AgentResult, AgentRole, AgentStatus
from lhas.agent.profile import AgentProfile, AgentProfileRegistry, default_profiles
from lhas.agent.provider import ProviderProfile, ProviderRegistry
from lhas.agent.toolsets import Toolset, ToolsetRegistry, default_toolsets

__all__ = [
    "AgentBudget", "AgentKernel", "AgentProfile", "AgentProfileRegistry", "DelegationService", "FailureRouter", "FailureSignal",
    "AgentRequest", "AgentResult", "AgentRole", "AgentStatus",
    "AssembledContext", "ContextAssembler", "ContextPriority", "ContextSource",
    "ProviderProfile", "ProviderRegistry", "RootAgentResponse", "RootAgentService", "RootRoute", "ScriptedAgentKernel", "Toolset",
    "ToolsetRegistry", "WorkerAgentKernelAdapter", "default_profiles",
    "default_toolsets",
]


def __getattr__(name):
    # Delegation and Root depend on platform persistence models, which in turn
    # import the provider-neutral agent models. Keep these facades lazy so the
    # package has no import-order-dependent cycle.
    if name == "DelegationService":
        from lhas.agent.delegation import DelegationService
        return DelegationService
    if name in {"RootAgentResponse", "RootAgentService", "RootRoute"}:
        from lhas.agent.root import RootAgentResponse, RootAgentService, RootRoute
        return {"RootAgentResponse": RootAgentResponse, "RootAgentService": RootAgentService, "RootRoute": RootRoute}[name]
    raise AttributeError(name)
