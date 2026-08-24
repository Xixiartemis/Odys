import os
from .models import InnerAgentRequest, InnerAgentResult, InnerAgentStatus, OdysAgentRunContext
from .provider_compat import MimoModelProvider, ProviderCompatProfile, resolve_provider_profile
from .tool_adapter import allowed_tools
from .trace import InnerAgentTrace, OdysAgentsRunHooks

class AgentsSdkModelConfig:
    def __init__(self, model=None, api_key=None, base_url=None, api_mode=None, provider_profile=None):
        self.model=model or os.getenv("ODYS_AGENT_MODEL"); self.api_key=api_key or os.getenv("ODYS_AGENT_API_KEY"); self.base_url=base_url or os.getenv("ODYS_AGENT_BASE_URL")
        self.provider_profile = provider_profile if isinstance(provider_profile, ProviderCompatProfile) else resolve_provider_profile(provider_profile)
        configured_api_mode = api_mode if api_mode is not None else os.getenv("ODYS_AGENT_API_MODE")
        if configured_api_mode and self.provider_profile.preferred_api_mode and configured_api_mode != self.provider_profile.preferred_api_mode:
            raise ValueError("PROVIDER_PROFILE_API_MODE_CONFLICT")
        self.api_mode = configured_api_mode or self.provider_profile.preferred_api_mode or "responses"
        self.tracing_enabled=os.getenv("ODYS_AGENT_SDK_TRACING","false").lower()=="true"
    def validate(self):
        if not self.model: raise ValueError("ODYS_AGENT_MODEL must be explicitly configured")
        if self.api_mode not in {"responses","chat_completions"}: raise ValueError("PROVIDER_API_UNSUPPORTED")
        if self.provider_profile.preferred_api_mode and self.api_mode != self.provider_profile.preferred_api_mode:
            raise ValueError("PROVIDER_PROFILE_API_MODE_CONFLICT")

class OpenAIAgentsBackend:
    name="openai-agents"
    def __init__(self, registry, config=None, runner=None, provider_factory=None, run_config_factory=None): self.registry=registry; self.config=config or AgentsSdkModelConfig(); self.runner=runner; self.provider_factory=provider_factory; self.run_config_factory=run_config_factory
    def _instructions(self, r): return "Complete the current subgoal. Tool failures are observations; adjust strategy. Do not claim uncalled work or expand permissions. Final output is a candidate completion claim; an outer validator independently verifies it.\nObjective: "+r.objective+"\nConstraints: "+str(r.constraints)+"\nAcceptance: "+str(r.acceptance_criteria)+"\nContext: "+str(r.context)
    async def run(self, request):
        trace = InnerAgentTrace()
        hooks = OdysAgentsRunHooks(trace)
        filtered = []
        def usage_data(value):
            if value is None: return {}
            return value.model_dump() if hasattr(value, "model_dump") else (dict(value) if isinstance(value, dict) else {})
        metadata = lambda: {"model": self.config.model or "", "api_mode": self.config.api_mode, "provider_profile": self.config.provider_profile.name, "tracing_enabled": self.config.tracing_enabled, "filtered_capabilities": filtered}
        try:
            self.config.validate()
            from agents import Agent, Runner, OpenAIProvider, RunConfig
            runner=self.runner or Runner
            tools,filtered=allowed_tools(self.registry,request,trace=trace)
            if self.provider_factory:
                provider = self.provider_factory(api_key=self.config.api_key, base_url=self.config.base_url, use_responses=self.config.api_mode == "responses")
            elif self.config.provider_profile.name == "mimo":
                provider = MimoModelProvider(api_key=self.config.api_key, base_url=self.config.base_url)
            else:
                provider = OpenAIProvider(api_key=self.config.api_key, base_url=self.config.base_url, use_responses=self.config.api_mode == "responses")
            run_config_kwargs = {"model":self.config.model, "model_provider":provider, "tracing_disabled":not self.config.tracing_enabled, "trace_include_sensitive_data":False}
            if self.config.provider_profile.name == "mimo":
                from agents import ModelSettings
                run_config_kwargs["model_settings"] = ModelSettings(tool_choice=None, extra_body=self.config.provider_profile.extra_body_dict())
            run_config = (self.run_config_factory or RunConfig)(**run_config_kwargs)
            context = OdysAgentRunContext(task_id=request.task_id, run_id=request.run_id, attempt_id=request.attempt_id, execution_context=request.context, metadata=request.metadata)
            agent=Agent(name="OdysInnerAgent",instructions=self._instructions(request),tools=tools,model=self.config.model)
            result=await runner.run(agent,self._instructions(request),context=context,max_turns=request.max_turns,hooks=hooks,run_config=run_config)
            output=getattr(result,"final_output",None); usage=getattr(getattr(result,"context_wrapper",None),"usage",None)
            changes=[item for item in trace.items if item.get("event") == "WORKSPACE_CHANGE"]
            patch=next((item for item in trace.items if item.get("event") == "WORKSPACE_PATCH"), None)
            artifacts={"workspace_changes": changes} if changes else {}
            if patch: artifacts["workspace_patch"] = patch.get("patch", {})
            return InnerAgentResult(status=InnerAgentStatus.SUCCESS,final_output=str(output) if output is not None else None,completion_claim=output is not None,turn_count=hooks.turn_count,tool_call_count=hooks.tool_call_count,usage=usage_data(usage),artifacts=artifacts,trace=trace.items,provider_metadata={**metadata(),"base_url_configured":bool(self.config.base_url),"api_key_configured":bool(self.config.api_key)})
        except Exception as exc:
            kind="AGENT_TURN_LIMIT" if exc.__class__.__name__=="MaxTurnsExceeded" else type(exc).__name__
            run_data = getattr(exc, "run_data", None)
            failure_usage = usage_data(getattr(getattr(run_data, "context_wrapper", None), "usage", None))
            return InnerAgentResult(status=InnerAgentStatus.FAILURE,error_type=kind,error_message=str(exc),turn_count=hooks.turn_count,tool_call_count=hooks.tool_call_count,usage=failure_usage,trace=trace.items,provider_metadata=metadata())
