from typing import Protocol, runtime_checkable
from .models import InnerAgentRequest, InnerAgentResult

@runtime_checkable
class InnerAgentBackend(Protocol):
    name: str
    async def run(self, request: InnerAgentRequest) -> InnerAgentResult: ...
