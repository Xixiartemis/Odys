from collections.abc import Sequence
from .models import InnerAgentRequest, InnerAgentResult

class ScriptedInnerAgentBackend:
    name="scripted-inner-agent"
    def __init__(self, result: InnerAgentResult|Sequence[InnerAgentResult]): self._results=list(result) if isinstance(result,Sequence) and not isinstance(result,InnerAgentResult) else [result]; self.invocations=0
    async def run(self, request: InnerAgentRequest) -> InnerAgentResult:
        result=self._results[min(self.invocations,len(self._results)-1)]; self.invocations+=1; return result
