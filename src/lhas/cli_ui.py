"""Rich/TTY progress rendering driven only by persisted Odys state."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lhas import HARNESS_VERSION
from lhas.cli_runtime import inspect_run_async

_UI_EVENT_FIELDS = {
    "event_id", "event_type", "capability", "status", "error_type", "path",
    "files_changed", "turn_number", "attempt_number", "action",
    "failure_repeat_count", "strategy_change_required",
    "agent_id", "session_id", "route", "delegation_id", "child_agent_id",
    "child_task_id", "parent_agent_id", "parent_run_id", "spawn_depth", "role", "validation",
    "plan_id", "step_id", "task_id", "run_id",
}


def _safe_activity(events) -> list[dict[str, Any]]:
    return [
        {key: event[key] for key in _UI_EVENT_FIELDS if key in event}
        for event in list(events)[-8:]
        if isinstance(event, dict)
    ]


def should_use_rich(*, no_ui: bool, stream=None) -> bool:
    if no_ui:
        return False
    target = stream or sys.stdout
    try:
        return bool(target.isatty())
    except Exception:
        return False


def project_agent_tree(events) -> list[dict[str, Any]]:
    """Build a bounded hierarchy using persisted events only."""
    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    steps: dict[str, str] = {}
    root_id: str | None = None
    for event in list(events)[-200:]:
        if hasattr(event, "event_type"):
            event_type=event.event_type.value; payload=event.payload or {}
        else:
            event_type=str(event.get("event_type","")); payload=event
        if event_type=="ROOT_AGENT_STARTED":
            agent_id=str(payload.get("agent_id","root"))[:128]
            nodes[agent_id]={"agent_id":agent_id,"role":"ROOT","status":"RUNNING","parent_agent_id":None}; order.append(agent_id)
            root_id=agent_id
        elif event_type=="ROOT_AGENT_COMPLETED":
            agent_id=str(payload.get("agent_id","root"))[:128]
            nodes.setdefault(agent_id,{"agent_id":agent_id,"role":"ROOT","parent_agent_id":None})["status"]="COMPLETED"
        elif event_type=="PLAN_STEP_STARTED":
            task_id=str(payload.get("task_id",""))[:64]; step_id=str(payload.get("step_id",""))[:64]
            worker=f"worker-{task_id}"
            nodes[worker]={"agent_id":worker,"role":"WORKER","status":"RUNNING","parent_agent_id":root_id,"task_id":task_id}; order.append(worker); steps[step_id]=worker
        elif event_type in {"PLAN_STEP_COMPLETED","PLAN_STEP_FAILED"}:
            worker=steps.get(str(payload.get("step_id","")))
            if worker in nodes: nodes[worker]["status"]="COMPLETED" if event_type=="PLAN_STEP_COMPLETED" else "FAILED"
        elif event_type=="DELEGATION_CREATED":
            child=str(payload.get("child_agent_id","child"))[:128]
            nodes[child]={"agent_id":child,"role":str(payload.get("role","WORKER"))[:32],"status":"CREATED","parent_agent_id":str(payload.get("parent_agent_id","worker"))[:128],"child_task_id":str(payload.get("child_task_id",""))[:64],"delegation_id":str(payload.get("delegation_id",""))[:64]}; order.append(child)
        elif event_type in {"DELEGATION_STARTED","DELEGATION_COMPLETED","DELEGATION_FAILED"}:
            delegation=str(payload.get("delegation_id",""))
            for node in nodes.values():
                if node.get("delegation_id")==delegation:
                    node["status"]={"DELEGATION_STARTED":"RUNNING","DELEGATION_COMPLETED":"COMPLETED","DELEGATION_FAILED":"FAILED"}[event_type]
    return [nodes[key] for key in order if key in nodes][-20:]


def project_view_state(
    inspection: dict[str, Any] | None,
    *,
    goal: str,
    provider: str,
    model: str,
    max_attempts: int,
    max_turns: int,
) -> dict[str, Any]:
    if inspection is None:
        return {
            "header": {"product": "Odys", "tagline": "Plan. Act. Recover. Finish.", "harness": HARNESS_VERSION, "provider_model": f"{provider}/{model}"},
            "goal": " ".join(goal.split())[:512],
            "run": {"id": None, "run_status": "STARTING", "task_status": "RUNNING", "duration_ms": 0, "attempt": f"0 / {max_attempts}"},
            "agent": {"current_attempt": 0, "turns": f"0 / {max_turns}", "tool_calls": 0, "tool_failures": 0},
            "workspace": {"session_id": None, "changed_files": 0, "source_unchanged": None},
            "validation": "UNKNOWN",
            "recovery": {"failure": None, "action": None, "checkpoint": None, "cp3": False},
            "recent_activity": [],
            "agent_tree": [],
        }
    run = inspection["run"]
    attempts = inspection["attempts"]
    current = attempts[-1] if attempts else {}
    recovery = inspection["recovery"][-1] if inspection["recovery"] else {}
    validation = inspection.get("validation")
    return {
        "header": {"product": "Odys", "tagline": "Plan. Act. Recover. Finish.", "harness": run["harness"], "provider_model": f"{run['provider']}/{run['model']}"},
        "goal": " ".join(goal.split())[:512],
        "run": {
            "id": run["id"],
            "run_status": run["status"],
            "task_status": "COMPLETED" if run["status"] == "COMPLETED" else ("RUNNING" if run["status"] == "RUNNING" else run["status"]),
            "duration_ms": run["duration_ms"],
            "attempt": f"{current.get('attempt_number', 0)} / {max_attempts}",
        },
        "agent": {
            "current_attempt": current.get("attempt_number", 0),
            "turns": f"{current.get('turn_count', 0)} / {max_turns}",
            "tool_calls": inspection["tools"]["total_calls"],
            "tool_failures": inspection["tools"]["total_failures"],
        },
        "workspace": {
            "session_id": inspection["workspace"]["session_id"],
            "changed_files": inspection["workspace"]["changed_file_count"],
            "source_unchanged": inspection["workspace"]["source_unchanged"],
        },
        "validation": validation["status"] if validation else "UNKNOWN",
        "recovery": {
            "failure": (recovery.get("failure_report") or {}).get("failure_type"),
            "action": (recovery.get("recovery_action") or {}).get("action"),
            "checkpoint": (recovery.get("checkpoint") or {}).get("id"),
            "cp3": bool(recovery.get("cp3_used")),
        },
        "recent_activity": _safe_activity(inspection.get("events", [])),
        "agent_tree": project_agent_tree(inspection.get("events", [])),
    }


def render_dashboard(state: dict[str, Any]):
    header = state["header"]
    heading = Text()
    heading.append("Odys\n", style="bold cyan")
    heading.append("Plan. Act. Recover. Finish.\n", style="italic")
    heading.append(f"Harness {header['harness']}  {header['provider_model']}")
    run_table = Table(show_header=False, box=None, expand=True)
    for key, value in state["run"].items():
        run_table.add_row(key.replace("_", " ").title(), str(value or "-"))
    agent_table = Table(show_header=False, box=None, expand=True)
    for key, value in state["agent"].items():
        agent_table.add_row(key.replace("_", " ").title(), str(value))
    workspace_table = Table(show_header=False, box=None, expand=True)
    for key, value in state["workspace"].items():
        workspace_table.add_row(key.replace("_", " ").title(), str(value if value is not None else "UNKNOWN"))
    recovery_table = Table(show_header=False, box=None, expand=True)
    for key, value in state["recovery"].items():
        recovery_table.add_row(key.replace("_", " ").title(), str(value or "-"))
    activity = "\n".join(
        f"#{event.get('event_id', '-')} {event.get('event_type', 'UNKNOWN')}"
        + (f" [{event.get('capability')}]" if event.get("capability") else "")
        for event in state["recent_activity"]
    ) or "(no persisted activity yet)"
    tree = "\n".join(
        f"{'  ' if node.get('parent_agent_id') else ''}{node.get('role', 'AGENT')} {node.get('agent_id', '-')}: {node.get('status', 'UNKNOWN')}"
        for node in state.get("agent_tree", [])
    ) or "(single agent)"
    return Group(
        Panel(heading),
        Panel(state["goal"], title="GOAL"),
        Panel(run_table, title="RUN"),
        Panel(agent_table, title="AGENT"),
        Panel(workspace_table, title="WORKSPACE"),
        Panel(state["validation"], title="VALIDATION"),
        Panel(recovery_table, title="RECOVERY"),
        Panel(tree, title="AGENT TREE"),
        Panel(activity, title="RECENT ACTIVITY"),
    )


class PlainProgress:
    def __init__(self, console: Console):
        self.console = console
        self.last_event_id = 0

    def update(self, state: dict[str, Any]) -> None:
        run_id = state["run"]["id"]
        if run_id and self.last_event_id == 0:
            self.console.print(f"Run ID: {run_id}")
        for event in state["recent_activity"]:
            sequence = int(event.get("event_id", 0) or 0)
            if sequence <= self.last_event_id:
                continue
            self.console.print(f"[{sequence}] {event.get('event_type', 'UNKNOWN')}")
            self.last_event_id = sequence


async def execute_with_progress(prepared, *, no_ui: bool, console: Console | None = None, refresh_seconds: float = 0.25):
    console = console or Console()
    use_rich = should_use_rich(no_ui=no_ui, stream=getattr(console, "file", None))
    operation = asyncio.create_task(prepared.execute())
    live = None
    plain = PlainProgress(console)
    last_inspection = None
    try:
        if use_rich:
            try:
                initial = project_view_state(None, goal=prepared.task.objective, provider=prepared.provider, model=prepared.model, max_attempts=prepared.task.max_attempts, max_turns=prepared.max_turns)
                live = Live(render_dashboard(initial), console=console, refresh_per_second=4, transient=False)
                live.start(refresh=True)
            except Exception:
                live = None
                use_rich = False
        while not operation.done():
            run = prepared.runtime.latest_run_for_task(prepared.task.id)
            if run is not None:
                try:
                    last_inspection = await inspect_run_async(prepared.runtime.db, run.id, include_events=True, recent_event_limit=8)
                    state = project_view_state(last_inspection, goal=prepared.task.objective, provider=prepared.provider, model=prepared.model, max_attempts=prepared.task.max_attempts, max_turns=prepared.max_turns)
                    if live is not None:
                        live.update(render_dashboard(state), refresh=True)
                    else:
                        plain.update(state)
                except Exception:
                    # Rendering/inspection cannot become part of task correctness.
                    if live is not None:
                        try:
                            live.stop()
                        except Exception:
                            pass
                        live = None
                    use_rich = False
            try:
                await asyncio.wait_for(asyncio.shield(operation), timeout=max(0.1, refresh_seconds))
            except asyncio.TimeoutError:
                pass
        run = await operation
        last_inspection = await inspect_run_async(prepared.runtime.db, run.id, include_events=True, recent_event_limit=8)
        state = project_view_state(last_inspection, goal=prepared.task.objective, provider=prepared.provider, model=prepared.model, max_attempts=prepared.task.max_attempts, max_turns=prepared.max_turns)
        if live is not None:
            live.update(render_dashboard(state), refresh=True)
        else:
            plain.update(state)
        return run, last_inspection, use_rich
    finally:
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass
