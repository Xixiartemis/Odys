import asyncio
import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from lhas.agent.platform import OfflineAgentPlatform
from lhas.cli import app
from lhas.cli_ui import project_agent_tree
from lhas.domain.enums import EventType, RunStatus, TaskStatus
from lhas.domain.models import Project, Task
from lhas.executors.mock import MockConfig, MockExecutor, MockScenario
from lhas.mcp import MCPManager, MCPServerConfig, register_mcp_tools
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import ValidationResultRepository
from lhas.persistence.planning_repositories import PlanRepository
from lhas.persistence.platform_repositories import DelegationRepository, SessionRepository
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository, TaskRepository
from lhas.tools.protocol import ToolRequest, ToolResultStatus
from lhas.tools.registry import ToolRegistry
from lhas.validation import NeverPassValidator


def test_mcp_stdio_discovery_and_toolregistry_bridge():
    async def scenario():
        manager=MCPManager(); registry=ToolRegistry()
        try:
            tools=await manager.connect(MCPServerConfig(name="offline",command=[sys.executable,"-m","lhas.mcp.fake_server"]))
            assert [tool.name for tool in tools]==["mcp.offline.echo"]
            registered=register_mcp_tools(registry,manager,tools)
            result=await registry.resolve(registered[0]).execute(ToolRequest(tool_call_id="c",task_id="t",run_id="r",attempt_id="a",capability=registered[0],arguments={"text":"hello"}))
            assert result.status is ToolResultStatus.SUCCESS
            assert result.output["content"][0]["text"]=="hello"
            assert registry.resolve(registered[0]).capability.origin=="mcp"
            assert registry.resolve(registered[0]).capability.server_name=="offline"
        finally:
            await manager.close_all()
    asyncio.run(scenario())


def test_validator_remains_authoritative_when_agent_claims_success():
    db=Database(":memory:"); db.init_db(); project=ProjectRepository(db).create(Project(name="validator-authority")); task=TaskRepository(db).create(Task(project_id=project.id,title="claim",objective="claim",max_attempts=1))
    orchestrator=RecoveringOrchestrator(db,executor_factory=lambda:MockExecutor(MockConfig(scenario=MockScenario.SUCCESS)),validator=NeverPassValidator())
    run=asyncio.run(orchestrator.execute_task(task.id)); persisted=TaskRepository(db).get(task.id)
    assert run.status is not RunStatus.COMPLETED
    assert persisted.status is not TaskStatus.COMPLETED
    assert ValidationResultRepository(db).get_for_attempt(AttemptRepository(db).list_for_run(run.id)[0].id).passed is False
    db.close()


def test_full_offline_agent_platform_vertical_slice(tmp_path):
    (tmp_path/"docs").mkdir(); (tmp_path/"README.md").write_text("Odys agent runtime architecture and durable delegation",encoding="utf-8"); (tmp_path/"docs"/"platform.md").write_text("Agent platform knowledge evidence",encoding="utf-8"); (tmp_path/"AGENTS.md").write_text("Use deterministic validation",encoding="utf-8"); (tmp_path/".odys.md").write_text("Keep the control plane authoritative",encoding="utf-8")
    db=Database(tmp_path/"platform.db")
    async def scenario():
        platform=await OfflineAgentPlatform.create(db,tmp_path,memory_root=tmp_path/"memory")
        try:
            return await platform.root.handle("Implement the complete durable Agent Platform offline vertical slice",project_id=platform.project.id)
        finally:
            await platform.close()
    response=asyncio.run(scenario())
    assert response.route.value=="LONG_RUNNING_GOAL"
    plan=PlanRepository(db).get(response.plan_id)
    assert plan.status.value=="COMPLETED" and len(plan.steps)==3
    assert all(step.task_id for step in plan.steps)
    parent_runs=[RunRepository(db).list_for_task(step.task_id)[0] for step in plan.steps]
    assert all(run.status is RunStatus.COMPLETED for run in parent_runs)
    assert all(len(AttemptRepository(db).list_for_run(run.id))==1 for run in parent_runs)
    delegations=DelegationRepository(db).list_for_parent_run(parent_runs[1].id)
    assert len(delegations)==1
    delegation=delegations[0]
    assert delegation.status.value=="COMPLETED"
    assert delegation.child_task_id and delegation.child_run_id
    child_task=TaskRepository(db).get(delegation.child_task_id); child_run=RunRepository(db).get(delegation.child_run_id); child_attempts=AttemptRepository(db).list_for_run(child_run.id)
    assert child_task.status is TaskStatus.COMPLETED
    assert child_run.status is RunStatus.COMPLETED
    assert len(child_attempts)==1
    assert ValidationResultRepository(db).get_for_attempt(child_attempts[0].id).passed is True
    assert "complete durable Agent Platform" not in json.dumps(delegation.context)
    child_raw=json.loads(child_attempts[0].executor_result)
    assert child_raw["raw"]["tool_call_count"]==3
    assert {item["capability"] for item in child_raw["raw"]["safe_trace"]}=={"skills.view","knowledge.search","mcp.offline.echo"}
    assert "messages" not in child_raw["raw"] and "transcript" not in child_raw["raw"]
    sessions=SessionRepository(db).list(); assert len(sessions)==1
    assert [item.role for item in SessionRepository(db).read(sessions[0].id)]==["user","assistant"]
    assert SessionRepository(db).search("durable Agent Platform")
    events=EventStore(db).list_all(); event_types={item.event_type for item in events}
    required={EventType.ROOT_AGENT_STARTED,EventType.PLAN_REQUESTED,EventType.PLAN_CREATED,EventType.DELEGATION_CREATED,EventType.DELEGATION_STARTED,EventType.CHILD_RUN_LINKED,EventType.DELEGATION_COMPLETED,EventType.SKILL_LOADED,EventType.KNOWLEDGE_SEARCHED,EventType.MCP_SERVER_CONNECTED,EventType.MCP_TOOL_DISCOVERED,EventType.GOAL_COMPLETED,EventType.ROOT_AGENT_COMPLETED}
    assert required<=event_types
    tool_events=[event for event in events if event.event_type in {EventType.TOOL_CALL_STARTED,EventType.TOOL_CALL_COMPLETED,EventType.TOOL_CALL_FAILED}]
    assert all("arguments" not in json.dumps(event.payload).casefold() for event in tool_events)
    tree=project_agent_tree(events)
    assert any(node["role"]=="ROOT" and node["status"]=="COMPLETED" for node in tree)
    assert any(node["role"]=="RESEARCHER" and node["status"]=="COMPLETED" for node in tree)
    db.close()


runner=CliRunner()


def test_cli_agents():
    result=runner.invoke(app,["agents"]); assert result.exit_code==0; assert "ROOT" in result.output and "RESEARCHER" in result.output


def test_cli_skills_list_and_show():
    listed=runner.invoke(app,["skills","list"]); shown=runner.invoke(app,["skills","show","coding/bug-fix"])
    assert listed.exit_code==0 and "coding/bug-fix" in listed.output
    assert shown.exit_code==0 and "Reproduce the failure" in shown.output


def test_cli_memory_show_and_search(monkeypatch,tmp_path):
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:tmp_path))
    shown=runner.invoke(app,["memory","show"]); searched=runner.invoke(app,["memory","search","missing"])
    assert shown.exit_code==0 and searched.exit_code==0


def test_cli_mcp_list():
    result=runner.invoke(app,["mcp","list"]); assert result.exit_code==0 and "stdio" in result.output


def test_cli_chat_requires_explicit_offline(tmp_path):
    result=runner.invoke(
        app,
        ["chat","hello","--repo",str(tmp_path),"--db",str(tmp_path/"db.sqlite")],
        terminal_width=240,
    )
    assert result.exit_code!=0 and "requires --offline" in result.output


def test_cli_offline_simple_chat(tmp_path):
    (tmp_path/"README.md").write_text("project",encoding="utf-8")
    result=runner.invoke(app,["chat","hello Odys","--offline","--repo",str(tmp_path),"--db",str(tmp_path/"db.sqlite")])
    assert result.exit_code==0
    assert "SIMPLE_INTERACTION" in result.output and "Session:" in result.output
