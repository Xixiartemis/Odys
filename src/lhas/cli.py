"""LHAS Typer CLI — minimal entry point (docs/03 tech stack, docs/12)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from lhas import HARNESS_VERSION, DEFAULT_CONTEXT_POLICY_VERSION, DEFAULT_DATASET_VERSION
from lhas.config import db_path, log_dir
from lhas.domain.models import Project, json_loads
from lhas.executors.mock import MockConfig, MockExecutor, MockScenario
from lhas.logging_setup import setup_logging
from lhas.orchestrator import Orchestrator
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, ProjectRepository, RunRepository, TaskRepository
from lhas.stage0 import print_stage0, run_stage0
from lhas.stageb import print_stageb, run_stageb
from lhas.task_service import create_task
from lhas.live_tools import build_live_registry
from lhas.planning.models import Goal, CapabilitySpec
from lhas.planning.planner import DeterministicPlanner
from lhas.planning.service import PlanExecutionService
from lhas.persistence.planning_repositories import GoalRepository, PlanRepository
from lhas.persistence.phaseb_repos import FailureReportRepository, RecoveryActionRepository
from lhas.cli_runtime import (
    CliConfigurationError,
    ProductRuntime,
    inspect_run,
    list_recent_runs,
    resolve_provider_settings,
)
from lhas.cli_ui import execute_with_progress, project_agent_tree
from lhas.command_validation import parse_verification_command
from lhas.agent.platform import OfflineAgentPlatform
from lhas.agent.profile import AgentProfileRegistry
from lhas.memory import BuiltinMemoryProvider
from lhas.skills import SkillRegistry

app = typer.Typer(
    help="Odys - Plan. Act. Recover. Finish.",
    no_args_is_help=True,
)
goal_app = typer.Typer(help="Run and inspect constrained goals")
skills_app = typer.Typer(help="Discover progressively disclosed skills")
memory_app = typer.Typer(help="Inspect bounded persistent memory")
mcp_app = typer.Typer(help="Inspect Model Context Protocol servers")
app.add_typer(goal_app, name="goal")
app.add_typer(skills_app, name="skills")
app.add_typer(memory_app, name="memory")
app.add_typer(mcp_app, name="mcp")
console = Console()


def _open_db() -> Database:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)
    db.init_db()
    return db


def _platform_db(override: Optional[Path]) -> Database:
    path=(override or Path(".odys/platform.db")).expanduser().resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    db=Database(path); db.init_db(); return db


@app.command("chat")
def chat(
    message: str = typer.Argument(..., help="Message or long-running goal"),
    repo: Path = typer.Option(Path("."), "--repo", help="Project context root"),
    offline: bool = typer.Option(False, "--offline", help="Use the deterministic no-network platform"),
    db_override: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
) -> None:
    """Talk to RootAgent; long goals route through the Odys control plane."""
    if not offline:
        raise typer.BadParameter("Agent Platform Foundation chat currently requires --offline")
    root=repo.expanduser().resolve()
    if not root.is_dir(): raise typer.BadParameter(f"repository path does not exist: {root}")
    db=_platform_db(db_override)
    async def execute():
        platform=await OfflineAgentPlatform.create(db,root,memory_root=(Path(db_override).resolve().parent/"memory" if db_override else root/".odys"/"memory"))
        try:
            return await platform.root.handle(message,project_id=platform.project.id)
        finally:
            await platform.close()
    try:
        response=asyncio.run(execute())
        console.print(f"Route: {response.route.value}")
        console.print(response.output)
        console.print(f"Session: {response.session_id}")
        if response.goal_id: console.print(f"Goal: {response.goal_id}")
        if response.plan_id: console.print(f"Plan: {response.plan_id}")
        for run_id in response.run_refs: console.print(f"Run: {run_id}")
        tree=project_agent_tree(EventStore(db).list_all())
        if tree:
            console.print("Agent tree:")
            for node in tree: console.print(f"  {node['role']} {node['agent_id']} {node['status']}")
    finally:
        db.close()


@app.command("agents")
def agents() -> None:
    """List installed role profiles sharing the AgentKernel contract."""
    for profile in AgentProfileRegistry().list():
        console.print(f"{profile.role.value}\t{profile.name}\t{profile.provider}/{profile.model}\ttoolsets={','.join(sorted(profile.toolsets))}")


def _cli_skills() -> SkillRegistry:
    return SkillRegistry([Path.cwd()/".odys"/"skills",Path.home()/".odys"/"skills"])


@skills_app.command("list")
def skills_list() -> None:
    for item in _cli_skills().list(): console.print(f"{item.name}\t{item.description}")


@skills_app.command("show")
def skills_show(name: str, reference: Optional[str] = typer.Option(None,"--reference")) -> None:
    document=_cli_skills().view(name,reference); console.print(document.content)


@memory_app.command("show")
def memory_show() -> None:
    provider=BuiltinMemoryProvider(Path.home()/".odys"/"memory")
    for item in provider.list(): console.print(f"{item.scope}\t{item.id}\t{item.content}")


@memory_app.command("search")
def memory_search(query: str) -> None:
    provider=BuiltinMemoryProvider(Path.home()/".odys"/"memory")
    for item in provider.search(query): console.print(f"{item.scope}\t{item.id}\t{item.content}")


@mcp_app.command("list")
def mcp_list() -> None:
    console.print("No persistent MCP servers configured. Offline acceptance uses stdio server 'offline'.")

@goal_app.command("run")
def goal_run(goal: str = typer.Option(...,"--goal"), file: Path = typer.Option(...,"--file"), live: bool = typer.Option(False,"--live"), output_dir: Path = typer.Option(Path("artifacts"),"--output-dir")):
    """Run the D2 smoke pipeline; real network requires --live."""
    if not live: raise typer.BadParameter("real web capabilities require explicit --live")
    db=_open_db(); projects=ProjectRepository(db); project=projects.get_by_name("D2-LIVE") or projects.create(Project(name="D2-LIVE"))
    registry=build_live_registry(); names=["document.resume.read","web.search","web.fetch","job.parse","job.match","job.rank","artifact.write"]
    g=Goal(project_id=project.id,objective=goal,allowed_capabilities=names,metadata={"plan_steps":names,"resume_path":str(file),"query":goal,"output_dir":str(output_dir)})
    print(f"GOAL {g.id}: {goal}")
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),registry).execute_goal(g,experiment_id=None,context={"live":True}))
    print(f"PLAN {plan.id} {plan.status.value}")
    for s in plan.steps:
        print(f"STEP {s.capability} {s.status.value} task={s.task_id}")
        if s.capability == "artifact.write" and s.status.value == "COMPLETED" and isinstance(s.output,dict) and s.output.get("artifact_path"):
            print(f"ARTIFACT {s.output['artifact_path']}")
    db.close()

@goal_app.command("inspect")
def goal_inspect(goal_id: str):
    db=_open_db(); g=GoalRepository(db).get(goal_id)
    if not g: raise typer.BadParameter(f"goal {goal_id} not found")
    print(f"Goal {g.id}: {g.objective}")
    plan_id=None
    for ev in EventStore(db).list_all():
        if ev.event_type.value == "PLAN_CREATED" and ev.payload.get("plan",{}).get("goal_id")==goal_id: plan_id=ev.payload["plan"].get("id")
    if plan_id:
        plan=PlanRepository(db).get(plan_id); print(f"Plan {plan.id} status={plan.status.value}")
        rr, ar, fr, rec = RunRepository(db), AttemptRepository(db), FailureReportRepository(db), RecoveryActionRepository(db)
        for step in plan.steps:
            print(f"STEP {step.capability} status={step.status.value} task={step.task_id or '-'}")
            if step.task_id:
                for run in rr.list_for_task(step.task_id):
                    for attempt in ar.list_for_run(run.id):
                        reports=fr.list_for_attempt(attempt.id); actions=rec.list_for_attempt(attempt.id)
                        print(f"  RUN {run.id} ATTEMPT {attempt.attempt_number} status={attempt.status.value} error_type={attempt.error_type or '-'} error_message={attempt.error_message or '-'}")
                        for report in reports: print(f"    failure_type={report.failure_type.value}")
                        for action in actions: print(f"    recovery={action.action_type.value}")
    print(f"project_id={g.project_id} capabilities={g.allowed_capabilities}"); db.close()


@app.command("init-db")
def init_db() -> None:
    """Create the SQLite schema (data/lhas.db)."""
    db = _open_db()
    print(f"database ready: {db_path()}")
    db.close()


@app.command("project-create")
def project_create(
    name: str = typer.Argument(..., help="Project name, e.g. RUNTIME-V0.1"),
    type: str = typer.Option("generic", help="Project type"),
    root_path: Optional[str] = typer.Option(None, help="Project root path"),
) -> None:
    """Create a Project."""
    db = _open_db()
    repo = ProjectRepository(db)
    existing = repo.get_by_name(name)
    if existing:
        print(f"project already exists: {existing.id} ({existing.name})")
    else:
        project = repo.create(Project(name=name, type=type, root_path=root_path))
        print(f"created project: {project.id} ({project.name})")
    db.close()


@app.command("task-create")
def task_create(
    project: str = typer.Option(..., help="Project name"),
    title: str = typer.Argument(..., help="Task title"),
    objective: str = typer.Argument(..., help="Task objective"),
    constraints: Optional[str] = typer.Option(None, help="Comma-separated constraints"),
    acceptance: Optional[str] = typer.Option(None, help="Comma-separated acceptance criteria"),
    max_attempts: int = typer.Option(3, min=1),
    timeout: float = typer.Option(60.0, min=0.1),
) -> None:
    """Create a Task (emits TASK_CREATED)."""
    db = _open_db()
    project_row = ProjectRepository(db).get_by_name(project)
    if project_row is None:
        raise typer.BadParameter(f"project '{project}' not found; run project-create first")
    task = create_task(
        db,
        project_id=project_row.id,
        title=title,
        objective=objective,
        constraints=[c.strip() for c in constraints.split(",")] if constraints else [],
        acceptance_criteria=[c.strip() for c in acceptance.split(",")] if acceptance else [],
        max_attempts=max_attempts,
        timeout_seconds=timeout,
    )
    print(f"created task: {task.id} ({task.title}) status={task.status.value}")
    db.close()


@app.command("task-list")
def task_list(project: Optional[str] = typer.Option(None, help="Filter by project name")) -> None:
    """List tasks."""
    db = _open_db()
    project_repo = ProjectRepository(db)
    project_id = project_repo.get_by_name(project).id if project else None
    for task in TaskRepository(db).list(project_id=project_id):
        print(f"{task.status.value:<10} {task.id}  {task.title}  (attempts<= {task.max_attempts}, timeout={task.timeout_seconds}s)")
    db.close()


@app.command("task-run")
def run_task(
    task_id: str = typer.Argument(..., help="Task id"),
    scenario: MockScenario = typer.Option(MockScenario.SUCCESS, help="MockExecutor scenario"),
    harness_version: str = typer.Option(HARNESS_VERSION),
) -> None:
    """Run a Task with MockExecutor and print the event chain."""
    db = _open_db()
    task = TaskRepository(db).get(task_id)
    if task is None:
        raise typer.BadParameter(f"task {task_id} not found")
    orchestrator = Orchestrator(
        db,
        executor_factory=lambda: MockExecutor(MockConfig(scenario=scenario)),
        harness_version=harness_version,
    )
    run = asyncio.run(orchestrator.execute_task(task.id))
    attempts = AttemptRepository(db).list_for_run(run.id)
    events = EventStore(db).list_for_task(task.id)
    print(f"task  : {task.title} ({task.id})")
    print(f"run   : {run.id} status={run.status.value}")
    print(f"attempts: {len(attempts)}")
    print("events:")
    for ev in events:
        print(f"  #{ev.id:03d} {ev.event_type.value:<20} attempt={ev.attempt_id or '-'}")
    db.close()


def _product_db_path(override: Optional[Path]) -> Path:
    return (override or db_path()).expanduser().resolve()


def _print_interrupt(run_id: Optional[str]) -> None:
    console.print("\nRESULT: INTERRUPTED", style="bold yellow")
    if run_id:
        console.print(f"Run ID: {run_id}")
        console.print(f"odys resume {run_id}")


def _print_product_summary(data: dict) -> None:
    run = data["run"]
    tools = data["tools"]
    workspace = data["workspace"]
    validation = data.get("validation")
    result = "PASS" if run["status"] == "COMPLETED" else "FAIL"
    console.print(f"\nRESULT: {result}", style="bold green" if result == "PASS" else "bold red")
    console.print(f"Run ID: {run['id']}")
    console.print(f"Task status: {'COMPLETED' if run['status'] == 'COMPLETED' else run['status']}")
    console.print(f"Run status: {run['status']}")
    console.print(f"Attempts: {len(data['attempts'])}")
    console.print(f"Total turns: {tools['total_turns']}")
    console.print(f"Total tool calls: {tools['total_calls']}")
    console.print(f"Total tool failures: {tools['total_failures']}")
    console.print(f"Tool failure rate: {tools['failure_rate'] * 100:.2f}%")
    console.print(f"Changed files: {workspace['changed_file_count']}")
    console.print(f"Validation status: {validation['status'] if validation else 'UNKNOWN'}")
    console.print(f"Validation command: {json.dumps(data['verification_command'], ensure_ascii=False)}")
    exit_code = (validation or {}).get("evidence", {}).get("exit_code")
    console.print(f"Validation exit code: {exit_code if exit_code is not None else 'N/A'}")
    console.print(f"Duration: {run['duration_ms']} ms")
    _print_runtime_truth(run)
    attempt_failures = [item for item in data["attempts"] if item.get("error_type")]
    if attempt_failures:
        console.print("Attempt failure types: " + ", ".join(item["error_type"] for item in attempt_failures[-5:]))
        last_failure = attempt_failures[-1]
        if last_failure.get("error_message"):
            console.print(f"Last attempt error: {last_failure['error_message']}")
    if result == "FAIL":
        top = data["tools"]["failures_by_type"]
        if top:
            console.print("Top failure types: " + ", ".join(f"{name}={count}" for name, count in list(top.items())[:5]))
        actions = [item.get("recovery_action") for item in data["recovery"] if item.get("recovery_action")]
        if actions:
            console.print(f"Last recovery action: {actions[-1]['action']}")
    console.print(f"odys inspect {run['id']}")
    if run["status"] == "RUNNING":
        console.print(f"odys resume {run['id']}")


def _print_runtime_truth(run: dict) -> None:
    truth = run.get("runtime_truth") or {}
    console.print("RUNTIME TARGET")
    console.print(f"configured: {json.dumps(truth.get('configured'), ensure_ascii=False, sort_keys=True)}")
    console.print(f"effective: {json.dumps(truth.get('effective'), ensure_ascii=False, sort_keys=True)}")
    console.print(f"transport: {truth.get('transport') or 'NOT_RECORDED'}")
    console.print(f"fingerprint: {truth.get('fingerprint') or 'NOT_RECORDED'}")


@app.command("run")
def product_run(
    goal: str = typer.Argument(..., help="Natural-language engineering goal"),
    repo: Path = typer.Option(..., "--repo", help="Local source repository"),
    verify: str = typer.Option(..., "--verify", help="Authoritative verification command"),
    max_attempts: int = typer.Option(3, "--max-attempts", min=1),
    max_turns: int = typer.Option(20, "--max-turns", min=1),
    provider: Optional[str] = typer.Option(None, "--provider", help="Provider profile (default: configured profile or mimo; offline for deterministic demo)"),
    kernel: str = typer.Option("external", "--kernel", help="Agent loop owner: native or external"),
    no_ui: bool = typer.Option(False, "--no-ui", help="Use plain deterministic console output"),
    yes: bool = typer.Option(False, "--yes", help="Skip launch confirmation"),
    db_override: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
) -> None:
    """Run a goal against an immutable local source repository."""
    source = repo.expanduser().resolve()
    if not source.is_dir():
        raise typer.BadParameter(f"repository path does not exist or is not a directory: {source}", param_hint="--repo")
    try:
        verify_argv = parse_verification_command(verify)
        settings = resolve_provider_settings(provider)
    except (ValueError, CliConfigurationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not yes:
        confirmed = typer.confirm(
            f"Run Odys against {source} using {settings.profile}/{settings.model}? "
            "The source stays immutable; work occurs in a durable staged workspace."
        )
        if not confirmed:
            raise typer.Abort()
    runtime = ProductRuntime(_product_db_path(db_override))
    try:
        prepared = runtime.prepare_new(
            goal=goal,
            repo=source,
            verify_argv=verify_argv,
            max_attempts=max_attempts,
            max_turns=max_turns,
            provider=settings.profile,
            kernel=kernel,
        )
        console.print("Odys", style="bold cyan")
        console.print("Plan. Act. Recover. Finish.")
        try:
            run, data, _ = asyncio.run(execute_with_progress(prepared, no_ui=no_ui, console=console))
        except KeyboardInterrupt:
            latest = runtime.latest_run_for_task(prepared.task.id)
            _print_interrupt(latest.id if latest else None)
            raise typer.Exit(code=130)
        _print_product_summary(data)
        if run.status.value != "COMPLETED":
            raise typer.Exit(code=1)
    except CliConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        runtime.close()


@app.command("resume")
def product_resume(
    run_id: str = typer.Argument(..., help="Durable Run ID"),
    migrate_provider: Optional[str] = typer.Option(None, "--migrate-provider", help="Explicit provider id for a blocked native run"),
    migrate_model: Optional[str] = typer.Option(None, "--migrate-model", help="Explicit model id for provider migration"),
    credential_route: Optional[str] = typer.Option(None, "--credential-route", help="Route id resolved from ODYS_AGENT_*_<ROUTE>"),
    no_ui: bool = typer.Option(False, "--no-ui", help="Use plain deterministic console output"),
    db_override: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
) -> None:
    """Resume an interrupted durable run in its existing workspace."""
    runtime = ProductRuntime(_product_db_path(db_override))
    try:
        prepared = runtime.prepare_resume(
            run_id,
            migrate_provider=migrate_provider,
            migrate_model=migrate_model,
            credential_route=credential_route,
        )
        try:
            run, data, _ = asyncio.run(execute_with_progress(prepared, no_ui=no_ui, console=console))
        except KeyboardInterrupt:
            _print_interrupt(run_id)
            raise typer.Exit(code=130)
        _print_product_summary(data)
        if run.status.value != "COMPLETED":
            raise typer.Exit(code=1)
    except CliConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        runtime.close()


def _human_inspect(data: dict, *, show_events: bool) -> None:
    run = data["run"]
    console.print(Panel.fit(
        f"[bold]Run ID[/bold] {run['id']}\n"
        f"[bold]Status[/bold] {run['status']}\n"
        f"[bold]Harness[/bold] {run['harness']}\n"
        f"[bold]Task[/bold] {run['task_title']}\n"
        f"[bold]Source[/bold] {run['source_root']}\n"
        f"[bold]Workspace[/bold] {run['workspace_session']}\n"
        f"[bold]Provider/model[/bold] {run['provider']}/{run['model']}\n"
        f"[bold]Duration[/bold] {run['duration_ms']} ms",
        title="Odys Inspect",
    ))
    _print_runtime_truth(run)
    attempts = Table(title="Attempts")
    for column in ("#", "Status", "Error", "Context", "Turns", "Calls", "Failures", "Validation"):
        attempts.add_column(column)
    for item in data["attempts"]:
        attempts.add_row(
            str(item["attempt_number"]), item["status"], item["error_type"] or "-",
            item["context_policy"] or "-", str(item["turn_count"]), str(item["tool_calls"]),
            str(item["tool_failures"]), item["validation"] or "UNKNOWN",
        )
    console.print(attempts)
    workspace = data["workspace"]
    console.print(f"Workspace: changed_files={workspace['changed_file_count']} source_unchanged={workspace['source_unchanged']}")
    for path in workspace["changed_files"]:
        console.print(f"  {path}")
    console.print("Diff summary: " + json.dumps(workspace["diff_summary"], ensure_ascii=False, sort_keys=True))
    tools = data["tools"]
    console.print("Tools calls: " + json.dumps(tools["calls_by_capability"], sort_keys=True))
    console.print("Tool failures: " + json.dumps(tools["failures_by_capability"], sort_keys=True))
    console.print("Failure types: " + json.dumps(tools["failures_by_type"], sort_keys=True))
    console.print(f"Validation: {(data.get('validation') or {}).get('status', 'UNKNOWN')}")
    for item in data["recovery"]:
        console.print("Recovery: " + json.dumps(item, ensure_ascii=False, sort_keys=True))
    if show_events:
        console.print("Events:")
        for event in data.get("events", []):
            console.print("  " + json.dumps(event, ensure_ascii=False, sort_keys=True))


@app.command("inspect")
def product_inspect(
    run_id: str = typer.Argument(..., help="Run ID"),
    events: bool = typer.Option(False, "--events", help="Include bounded safe lifecycle events"),
    as_json: bool = typer.Option(False, "--json", help="Emit bounded safe JSON"),
    db_override: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
) -> None:
    """Inspect a persisted run without exposing secrets or raw model data."""
    runtime = ProductRuntime(_product_db_path(db_override))
    try:
        data = inspect_run(runtime.db, run_id, include_events=events)
        if as_json:
            console.print_json(json.dumps(data, ensure_ascii=False, sort_keys=True))
        else:
            _human_inspect(data, show_events=events)
    except CliConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        runtime.close()


@app.command("runs")
def product_runs(
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by Run status"),
    db_override: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
) -> None:
    """List recent durable runs."""
    runtime = ProductRuntime(_product_db_path(db_override))
    try:
        rows = list_recent_runs(runtime.db, limit=limit, status=status)
        if not console.is_terminal:
            console.print("STATUS\tRUN_ID\tTASK\tATTEMPTS\tPROVIDER/MODEL\tUPDATED")
            for item in rows:
                console.print(
                    f"{item['status']}\t{item['run_id']}\t{item['task_title']}\t"
                    f"{item['attempts']}\t{item['provider_model']}\t{item['updated_at']}"
                )
            return
        table = Table("Status", "Run ID", "Task", "Attempts", "Provider/model", "Updated")
        for item in rows:
            table.add_row(item["status"], item["run_id"], item["task_title"], str(item["attempts"]), item["provider_model"], item["updated_at"])
        console.print(table)
    finally:
        runtime.close()


@app.command("events")
def events(
    task_id: str = typer.Argument(..., help="Task id"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSONL"),
) -> None:
    """Show the event timeline for a Task."""
    db = _open_db()
    for ev in EventStore(db).list_for_task(task_id):
        if as_json:
            print(json.dumps({
                "sequence": ev.id, "task_id": ev.task_id, "run_id": ev.run_id,
                "attempt_id": ev.attempt_id, "type": ev.event_type.value,
                "timestamp": ev.timestamp.isoformat(), "payload": ev.payload,
            }, ensure_ascii=False))
        else:
            print(f"#{ev.id:03d} {ev.event_type.value:<22} {ev.timestamp.isoformat()} {json.dumps(ev.payload, ensure_ascii=False)}")
    db.close()


@app.command("stage0")
def stage0() -> None:
    """Run the Phase A Stage 0 acceptance suite and write the experiment record."""
    setup_logging(log_dir())
    db = _open_db()
    project_repo = ProjectRepository(db)
    project = project_repo.get_by_name("RUNTIME-V0.1")
    if project is None:
        project = project_repo.create(Project(name="RUNTIME-V0.1", type="benchmark"))
    results, exp_id = run_stage0(db, project_id=project.id, experiment_id="EXP-20260818-RUNTIME-001")
    exit_code = print_stage0(results, db, exp_id)
    db.close()
    if exit_code != 0:
        raise typer.Exit(code=1)


@app.command("stageb")
def stageb() -> None:
    """Run the Phase B validation/recovery suite and write the experiment record."""
    setup_logging(log_dir())
    db = _open_db()
    project_repo = ProjectRepository(db)
    project = project_repo.get_by_name("RUNTIME-V0.1")
    if project is None:
        project = project_repo.create(Project(name="RUNTIME-V0.1", type="benchmark"))
    results, exp_id = run_stageb(db, project_id=project.id, experiment_id="EXP-20260818-RUNTIME-002")
    exit_code = print_stageb(results, exp_id)
    db.close()
    if exit_code != 0:
        raise typer.Exit(code=1)


@app.command("jobbench")
def jobbench(
    dataset: str = typer.Option("benchmarks/job-v0.1", help="Dataset directory"),
    predictor: str = typer.Option("rule", help="rule (deterministic) | llm (requires LHAS_JOB_LLM_API_KEY)"),
    experiment_id: Optional[str] = typer.Option(None, help="Explicit EXP id (default auto)"),
    as_of: Optional[str] = typer.Option(None, help="Reference date YYYY-MM-DD for expiration"),
    no_record: bool = typer.Option(False, "--no-record", help="Do not write an experiment record"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Allow recording a development experiment from a dirty workspace"),
    recovery: bool = typer.Option(False, "--recovery", help="Run the LLM predictor through Task/Run/Attempt/Validation/Recovery"),
) -> None:
    """Run the Job Benchmark on a locked dataset and write an EXP-JOB record."""
    from datetime import date

    from lhas.job.bench import run_job_bench
    from lhas.job.models import labels_status, load_job_dataset
    from lhas.job.recorder import JobExperimentRecorder

    ds = load_job_dataset(dataset)
    print(f"dataset : {ds.manifest.get('dataset_id')} ({len(ds.jobs)} jobs)")
    status = labels_status(ds)
    print(f"labels  : {status}")
    if status.get("DRAFT", 0):
        print("warning : ground truth is DRAFT — metrics are provisional until human review")

    recorder = JobExperimentRecorder()
    exp_id = experiment_id or recorder.next_id("JOB")
    as_of_date = date.fromisoformat(as_of) if as_of else None
    runtime_db = _open_db() if recovery else None
    if recovery and predictor != "llm":
        raise typer.BadParameter("--recovery currently requires --predictor llm")
    result = run_job_bench(
        ds, predictor=predictor, as_of=as_of_date, runtime=recovery,
        db=runtime_db, experiment_id=exp_id if recovery else None,
    )

    m = result.metrics
    print("-" * 56)
    print(f"predictor: {result.predictor}   model: {result.model or '-'}")
    print(f"hard_constraint_accuracy    {m.hard_constraint_accuracy:.3f}")
    print(f"fit_classification_accuracy {m.fit_classification_accuracy:.3f}")
    print(f"precision@5                 {m.precision_at_5:.3f}")
    print(f"recall@10                   {m.recall_at_10:.3f}")
    print(f"ranking_quality (ndcg@10)   {m.ranking_quality_ndcg10:.3f}")
    print(f"evidence_accuracy           {m.evidence_accuracy:.3f}")
    print(f"hallucination_rate          {m.hallucination_rate:.3f}")
    print(f"duplicate_detection_rate    {m.duplicate_detection_rate:.3f}")
    print(f"expired_job_detection_rate  {m.expired_job_detection_rate:.3f}")
    print("-" * 56)
    for e in result.evaluations:
        marks = ("H" if e.hard_correct else "h") + ("F" if e.fit_correct else "f") + ("A" if e.apply_correct else "a")
        print(f"  {e.job_id}  {marks}  hit={e.evidence_hit:.2f} cov={e.evidence_coverage:.2f}{'  HALLUC' if e.hallucination else ''}")

    if no_record:
        if runtime_db is not None:
            runtime_db.close()
        return
    recorder.record(
        experiment_id=exp_id,
        dataset_id=ds.manifest.get("dataset_id", "unknown"),
        ground_truth_status=str(status),
        predictor=result.predictor,
        model=result.model,
        provider=result.provider or ("mock" if result.predictor == "rule" else "llm"),
        model_config=result.model_config,
        harness_version=HARNESS_VERSION,
        context_policy_version="CP-2" if recovery else "CP-1",
        recovery="ON" if recovery else "OFF",
        metrics=m,
        predictions=[p.model_dump() for p in result.predictions],
        evaluations=[e.model_dump() for e in result.evaluations],
        allow_dirty=allow_dirty,
        db=runtime_db,
        runs=result.runs,
    )
    print(f"experiment record: experiments/{exp_id}/")
    if runtime_db is not None:
        runtime_db.close()


@app.command()
def version() -> None:
    """Print version info."""
    console.print("Odys", style="bold cyan")
    console.print("Plan. Act. Recover. Finish.")
    console.print(f"Harness: {HARNESS_VERSION}")
    console.print(f"Package: {__import__('lhas').__version__}")


if __name__ == "__main__":
    app()
