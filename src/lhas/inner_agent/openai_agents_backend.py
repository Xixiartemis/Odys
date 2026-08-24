import os
from .models import InnerAgentRequest, InnerAgentResult, InnerAgentStatus
from .tool_adapter import allowed_tools

class AgentsSdkModelConfig:
    def __init__(self, model=None, api_key=None, base_url=None, api_mode=None):
        self.model=model or os.getenv("ODYS_AGENT_MODEL"); self.api_key=api_key or os.getenv("ODYS_AGENT_API_KEY"); self.base_url=base_url or os.getenv("ODYS_AGENT_BASE_URL"); self.api_mode=api_mode or os.getenv("ODYS_AGENT_API_MODE","responses"); self.tracing_enabled=os.getenv("ODYS_AGENT_SDK_TRACING","false").lower()=="true"
    def validate(self):
        if not self.model: raise ValueError("ODYS_AGENT_MODEL must be explicitly configured")
        if self.api_mode not in {"responses","chat_completions"}: raise ValueError("PROVIDER_API_UNSUPPORTED")

class OpenAIAgentsBackend:
    name="openai-agents"
    def __init__(self, registry, config=None, runner=None): self.registry=registry; self.config=config or AgentsSdkModelConfig(); self.runner=runner
    def _instructions(self, r): return "Complete the current subgoal. Tool failures are observations; adjust strategy. Do not claim uncalled work or expand permissions. Final output is a candidate completion claim; an outer validator independently verifies it.\nObjective: "+r.objective+"\nConstraints: "+str(r.constraints)+"\nAcceptance: "+str(r.acceptance_criteria)+"\nContext: "+str(r.context)
    async def run(self, request):
        try:
            self.config.validate()
            from agents import Agent, Runner
            runner=self.runner or Runner
            tools,filtered=allowed_tools(self.registry,request)
            agent=Agent(name="OdysInnerAgent",instructions=self._instructions(request),tools=tools,model=self.config.model)
            result=await runner.run(agent,self._instructions(request),max_turns=request.max_turns)
            output=getattr(result,"final_output",None); usage=getattr(result,"usage",None)
            return InnerAgentResult(status=InnerAgentStatus.SUCCESS,final_output=str(output) if output is not None else None,completion_claim=output is not None,usage=usage.model_dump() if hasattr(usage,"model_dump") else (usage or {}),provider_metadata={"model":self.config.model,"api_mode":self.config.api_mode,"tracing_enabled":self.config.tracing_enabled,"filtered_capabilities":filtered})
        except Exception as exc:
            kind="AGENT_TURN_LIMIT" if exc.__class__.__name__=="MaxTurnsExceeded" else type(exc).__name__
            return InnerAgentResult(status=InnerAgentStatus.FAILURE,error_type=kind,error_message=str(exc),provider_metadata={"model":self.config.model or ""})
