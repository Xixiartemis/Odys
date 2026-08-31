"""Deterministic E7-A comparison over the frozen HV13 task fixture."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

_SCRIPT_REPO_ROOT=Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0,str(_SCRIPT_REPO_ROOT))

from lhas import HARNESS_VERSION
from lhas.domain.models import Project
from lhas.executors.protocol import ExecutionResult
from lhas.inner_agent.observability import project_tool_metrics
from lhas.inner_agent.tool_adapter import ToolAwareObserver, _args_signature, safe_tool_summary
from lhas.orchestrator_v2 import RecoveringOrchestrator
from lhas.persistence.database import Database
from lhas.persistence.repositories import ProjectRepository, TaskRepository
from lhas.task_service import create_task
from lhas.tools.protocol import ToolRequest
from lhas.workspace import CommandPolicy, CommandRule, RunWorkspaceManager, StagedWorkspace
from lhas.workspace.tools import SafeCliTool, WorkspaceDiffTool, WorkspaceEditLinesTool, WorkspaceEditTool, WorkspaceReadTool
from scripts.hv12_longtask_recovery import FixturePytestValidator, _pytest
from scripts.hv13_longtask_recovery import _task_objective


REPO_ROOT=Path(__file__).resolve().parents[1]
FIXTURE_ROOT=REPO_ROOT / "evals" / "fixtures" / "hv12_session_lifecycle"
DEFAULT_OUTPUT=REPO_ROOT / "evals" / "runs" / "HV15-E7A-DRY-001.json"
EVALUATION_ID="HV15-E7A-DRY-001"
FIXTURE_VERSION="HV12-SESSION-LIFECYCLE-1"
MAX_ATTEMPTS=3
INNER_TURN_BUDGET=20
CANONICAL_HASHES={
    "HV12-LIVE-001.json":"144985bc68dbc2d3e8ecbde669c7f14d28adb4d8c3a693668b4b5acaa1603af9",
    "HV13-LIVE-001.json":"3e6ecf1b718d2e8f0a292fa4cf8f1d2d71c455128f3d0a23fabad81079840bb5",
    "HV13-LIVE-001.claim.json":"67b8977da846e78cc933c3893ecbc4851b870c8620de00ac222205b26a21cc03",
}


def _tree_hash(root: Path) -> str:
    digest=hashlib.sha256()
    excluded={"__pycache__",".pytest_cache",".git"}
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not any(part in excluded for part in item.relative_to(root).parts)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def _tool_sequence(workspace, *, safe_normalization: bool) -> dict[str,Any]:
    observer=ToolAwareObserver(); observations=[]; call_number=0
    policy=CommandPolicy([CommandRule(["pytest"],allow_extra_args=True)])
    tools={
        "workspace.read":WorkspaceReadTool(workspace),
        "workspace.edit":WorkspaceEditTool(workspace,allow_safe_normalization=safe_normalization),
        "workspace.edit_lines":WorkspaceEditLinesTool(workspace),
        "workspace.diff":WorkspaceDiffTool(workspace),
        "cli.exec":SafeCliTool(workspace,policy),
    }

    async def call(capability: str, arguments: dict[str,Any]):
        nonlocal call_number
        call_number += 1
        if call_number > INNER_TURN_BUDGET:
            raise RuntimeError("INNER_TURN_BUDGET_EXCEEDED")
        result=await tools[capability].execute(ToolRequest(
            tool_call_id=f"dry-{call_number}",task_id="e7a",run_id="dry",
            attempt_id="attempt-1",capability=capability,arguments=arguments,
        ))
        summary=observer.decorate(capability,arguments,result,safe_tool_summary(capability,arguments,result),_args_signature(arguments))
        observations.append({"event":"TOOL_OBSERVATION_SUMMARY",**summary})
        return result

    session_path="src/session_store.py"
    initial=await call("workspace.read",{"path":session_path})
    brittle={
        "path":session_path,
        "old_text":"        self._cache: dict[str, Session] = {}  \n",
        "new_text":"        self._cache: dict[tuple[str, str], Session] = {}\n",
        "expected_sha256":initial.output["sha256"],
    }
    first_edit=await call("workspace.edit",brittle)
    if not safe_normalization:
        second_edit=await call("workspace.edit",brittle)
        assert first_edit.status.value == second_edit.status.value == "FAILURE"
    else:
        assert first_edit.status.value == "SUCCESS"

    current=await call("workspace.read",{"path":session_path})
    content=current.output["content"]
    replacements={
        "self._cache: dict[str, Session] = {}":"self._cache: dict[tuple[str, str], Session] = {}",
        "self._cache.get(session_id)":"self._cache.get(key)",
        "self._cache[session_id] = session":"self._cache[key] = session",
        "self._cache.pop(session_id, None)":"self._cache.pop((tenant_id, session_id), None)",
        "session_id in self._cache":"(tenant_id, session_id) in self._cache",
    }
    updated=content
    for old,new in replacements.items(): updated=updated.replace(old,new)
    if updated != content:
        result=await call("workspace.edit_lines",{
            "path":session_path,"start_line":1,"end_line":current.output["total_lines"],
            "new_lines":updated.splitlines(),"expected_sha256":current.output["sha256"],
        })
        assert result.status.value == "SUCCESS"

    router_path="src/message_router.py"
    router=await call("workspace.read",{"path":router_path})
    router_updated=router.output["content"].replace(
        "return service.create_session(tenant_id, session_id, message)","return None"
    )
    result=await call("workspace.edit_lines",{
        "path":router_path,"start_line":1,"end_line":router.output["total_lines"],
        "new_lines":router_updated.splitlines(),"expected_sha256":router.output["sha256"],
    })
    assert result.status.value == "SUCCESS"
    diff=await call("workspace.diff",{})
    verification=await call("cli.exec",{"argv":["pytest","-q"],"cwd":"."})
    metrics=project_tool_metrics(observations)
    return {
        "policy":"safe_unique_normalization" if safe_normalization else "legacy_exact_only",
        "metrics":metrics,
        "first_verification_turn":metrics["first_pytest_tool_call"],
        "turn_mapping":"one scripted tool call equals one deterministic turn",
        "final_functional_validation":"PASS" if verification.status.value == "SUCCESS" and verification.output["exit_code"] == 0 else "FAIL",
        "changed_files":diff.output["changed_files"],
        "workspace_tree_sha256":_tree_hash(workspace.root),
    }


class ComparisonExecutor:
    name="E7ADeterministicComparisonExecutor"

    def __init__(self,workspace,*,safe_normalization):
        self.workspace=workspace
        self.safe_normalization=safe_normalization
        self.observation: dict[str,Any] | None=None

    async def execute(self,request):
        self.observation=await _tool_sequence(self.workspace,safe_normalization=self.safe_normalization)
        passed=self.observation["final_functional_validation"] == "PASS"
        return ExecutionResult(
            status="SUCCESS" if passed else "FAILURE",
            output="deterministic E7-A candidate",
            error_type=None if passed else "VERIFICATION_FAILED",
        )


async def _scenario(root: Path, *, safe_normalization: bool) -> dict[str,Any]:
    root.mkdir(parents=True,exist_ok=True)
    db=Database(root / "runtime.db"); db.init_db()
    sessions_root=root / "sessions"
    try:
        project=Project(name="e7a-dry",root_path=str(FIXTURE_ROOT))
        ProjectRepository(db).create(project)
        objective,constraints,acceptance=_task_objective()
        contract_sha256=hashlib.sha256(json.dumps(
            {"objective":objective,"constraints":constraints,"acceptance":acceptance},
            ensure_ascii=False,sort_keys=True,separators=(",",":")
        ).encode("utf-8")).hexdigest()
        task=create_task(
            db,project_id=project.id,title="E7-A deterministic comparison",
            objective=objective,constraints=constraints,
            acceptance_criteria=acceptance,max_attempts=MAX_ATTEMPTS,
            timeout_seconds=float(INNER_TURN_BUDGET * 90),
        )
        holder: dict[str,ComparisonExecutor]={}
        def factory(workspace):
            executor=ComparisonExecutor(workspace,safe_normalization=safe_normalization)
            holder["executor"]=executor
            return executor
        validator=FixturePytestValidator(sessions_root)
        orchestrator=RecoveringOrchestrator(
            db,workspace_executor_factory=factory,
            workspace_manager=RunWorkspaceManager(db,sessions_root,source_root=FIXTURE_ROOT),
            validator=validator,harness_version=HARNESS_VERSION,
            context_policy_version="CP-3",executor_type=ComparisonExecutor.name,
            provider="deterministic",model="scripted-e7a",
            dataset_version=FIXTURE_VERSION,experiment_id=EVALUATION_ID,
        )
        run=await orchestrator.execute_task(task.id)
        observation=dict(holder["executor"].observation or {})
        observation.update({
            "outer_run_result":run.status.value,
            "outer_task_result":TaskRepository(db).get(task.id).status.value,
            "outer_validator_calls":validator.calls,
            "attempt_budget":{"max_attempts":MAX_ATTEMPTS,"inner_turn_budget":INNER_TURN_BUDGET},
            "task_contract_sha256":contract_sha256,
            "orchestrator":"RecoveringOrchestrator",
            "validator":"FixturePytestValidator",
            "verification_argv":["pytest","-q"],
            "production_tool_invocations":True,
        })
        return observation
    finally:
        db.close()


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator,6) if denominator else 0.0


def run_evaluation(output_path: Path | None = None) -> dict[str,Any]:
    fixture_before=_tree_hash(FIXTURE_ROOT)
    with tempfile.TemporaryDirectory(prefix="odys-e7a-dry-") as temporary:
        base=Path(temporary)
        initial_workspace=StagedWorkspace.create(FIXTURE_ROOT,base / "initial")
        initial=_pytest(initial_workspace.root)
        baseline=asyncio.run(_scenario(base / "baseline",safe_normalization=False))
        e7a=asyncio.run(_scenario(base / "e7a",safe_normalization=True))
    fixture_after=_tree_hash(FIXTURE_ROOT)
    baseline_metrics=baseline["metrics"]; e7a_metrics=e7a["metrics"]
    canonical={name:hashlib.sha256((REPO_ROOT / "evals" / "runs" / name).read_bytes()).hexdigest() for name in CANONICAL_HASHES}
    canonical_unchanged=canonical == CANONICAL_HASHES
    checks={
        "initial_fixture_tests_fail":initial["status"] == "FAIL",
        "baseline_final_validation_passes":baseline["final_functional_validation"] == "PASS",
        "e7a_final_validation_passes":e7a["final_functional_validation"] == "PASS",
        "baseline_outer_validator_completes":baseline["outer_task_result"] == "COMPLETED" and baseline["outer_validator_calls"] == 1,
        "e7a_outer_validator_completes":e7a["outer_task_result"] == "COMPLETED" and e7a["outer_validator_calls"] == 1,
        "e7a_edit_failures_reduced":e7a_metrics["workspace_edit_failures"] < baseline_metrics["workspace_edit_failures"],
        "e7a_verifies_earlier":e7a["first_verification_turn"] < baseline["first_verification_turn"],
        "same_task_contract":baseline["task_contract_sha256"] == e7a["task_contract_sha256"],
        "same_outer_components":baseline["orchestrator"] == e7a["orchestrator"] and baseline["validator"] == e7a["validator"],
        "same_attempt_and_turn_budget":baseline["attempt_budget"] == e7a["attempt_budget"],
        "same_verification_policy":baseline["verification_argv"] == e7a["verification_argv"],
        "production_tool_invocations":baseline["production_tool_invocations"] and e7a["production_tool_invocations"],
        "fixture_unchanged":fixture_before == fixture_after,
        "canonical_artifacts_unchanged":canonical_unchanged,
    }
    result={
        "evaluation_id":EVALUATION_ID,
        "phase":"E7A_TOOL_AWARE_RECOVERY_AND_VERIFICATION",
        "mode":"deterministic_dry_comparison",
        "real_model_executed":False,
        "harness_version":HARNESS_VERSION,
        "fixture_version":FIXTURE_VERSION,
        "comparison_scope":"same fixture; legacy exact-only edit policy versus E7-A safe unique normalization",
        "controlled_variables":{
            "fixture_version":FIXTURE_VERSION,
            "task_contract_sha256":baseline["task_contract_sha256"],
            "orchestrator":baseline["orchestrator"],
            "validator":baseline["validator"],
            "attempt_budget":baseline["attempt_budget"],
            "verification_argv":baseline["verification_argv"],
            "only_mechanism_difference":"workspace.edit safe unique normalization enabled in E7-A arm",
        },
        "stochastic_success_rate_claimed":False,
        "status":"PASS" if all(checks.values()) else "FAIL",
        "checks":checks,
        "baseline":baseline,
        "e7a":e7a,
        "comparison":{
            "edit_failure_rate_baseline":_rate(baseline_metrics["workspace_edit_failures"],baseline_metrics["workspace_edit_calls"]),
            "edit_failure_rate_e7a":_rate(e7a_metrics["workspace_edit_failures"],e7a_metrics["workspace_edit_calls"]),
            "tool_failure_rate_baseline":_rate(baseline_metrics["total_tool_failures"],baseline_metrics["total_tool_calls"]),
            "tool_failure_rate_e7a":_rate(e7a_metrics["total_tool_failures"],e7a_metrics["total_tool_calls"]),
            "repeated_edit_target_not_found_baseline":baseline_metrics["repeated_edit_failures"],
            "repeated_edit_target_not_found_e7a":e7a_metrics["repeated_edit_failures"],
            "first_verification_turn_baseline":baseline["first_verification_turn"],
            "first_verification_turn_e7a":e7a["first_verification_turn"],
            "final_functional_validation_baseline":baseline["final_functional_validation"],
            "final_functional_validation_e7a":e7a["final_functional_validation"],
            "outer_task_result_baseline":baseline["outer_task_result"],
            "outer_task_result_e7a":e7a["outer_task_result"],
        },
        "outer_validator_authority":"PRESERVED; both arms complete through RecoveringOrchestrator and FixturePytestValidator",
        "fixture_tree_sha256_before":fixture_before,
        "fixture_tree_sha256_after":fixture_after,
        "canonical_artifact_sha256":canonical,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True,exist_ok=True)
        output_path.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args(argv)
    result=run_evaluation(args.output)
    print(f"EVALUATION_ID={result['evaluation_id']}")
    print(f"STATUS={result['status']}")
    print(f"OUTPUT={args.output}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
