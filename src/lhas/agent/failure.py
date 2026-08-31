"""Failure-level routing boundary without implementing dynamic replan."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lhas.domain.enums import EventType, FailureLevel
from lhas.persistence.event_store import EventStore


class FailureSignal(BaseModel):
    model_config=ConfigDict(extra="forbid")
    level: FailureLevel
    source: str=Field(max_length=128)
    error_type: str=Field(max_length=128)
    summary: str=Field(default="",max_length=1_000)


class FailureRouter:
    ROUTES={
        FailureLevel.TOOL:"LOCAL_TOOL_RECOVERY",
        FailureLevel.ATTEMPT:"CHECKPOINT_RETRY",
        FailureLevel.TASK:"STRATEGY_REPLACEMENT_BOUNDARY",
        FailureLevel.PLAN:"DYNAMIC_REPLAN_BOUNDARY",
        FailureLevel.GOAL:"HUMAN_CLARIFICATION",
    }

    def __init__(self,db): self.events=EventStore(db)

    def route(self,signal:FailureSignal,*,task_id:str|None=None,run_id:str|None=None,attempt_id:str|None=None)->str:
        route=self.ROUTES[signal.level]
        self.events.append(EventType.FAILURE_ROUTED,task_id=task_id,run_id=run_id,attempt_id=attempt_id,payload={"level":signal.level.value,"source":signal.source,"error_type":signal.error_type,"route":route})
        return route
