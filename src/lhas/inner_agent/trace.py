from typing import Any
class InnerAgentTrace:
    def __init__(self): self.items:list[dict[str,Any]]=[]
    def add(self,event:str,**fields): self.items.append({"event":event,**fields})
