"""Manual real-model E5 smoke; never required by deterministic CI."""
from __future__ import annotations
import asyncio, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from collections import Counter
from pathlib import Path

from lhas import HARNESS_VERSION
from lhas.domain.models import new_id
from lhas.executors.protocol import ExecutionRequest
from lhas.inner_agent.executor import InnerAgentExecutor
from lhas.inner_agent.openai_agents_backend import AgentsSdkModelConfig, OpenAIAgentsBackend
from lhas.persistence.database import Database
from lhas.planning.models import CapabilitySpec
from lhas.tools.registry import ToolRegistry
from lhas.workspace import CommandPolicy, CommandRule, StagedWorkspace, register_staged_workspace_tools

def _sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def _tree_sha(root):
    digest=hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()
def _fixture(root):
    (root / "src").mkdir(); (root / "tests").mkdir()
    (root / "src/calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "tests/test_calculator.py").write_text("from src.calculator import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
def _test_after(stage):
    proc=subprocess.run([sys.executable,"-m","pytest","-q"],cwd=stage.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=120)
    return proc.returncode == 0, proc.returncode

_RUN_FILE_RE = re.compile(r"^E5-LIVE-(\d+)\.json$")
MAX_TURNS = 20
_PROVIDER_CONNECTION_ERRORS = {"APIConnectionError"}
_PROVIDER_AUTH_ERRORS = {"AuthenticationError"}


def _next_run_file(directory=Path("evals/runs")):
    """Allocate the next run filename without ever selecting an existing file."""
    directory.mkdir(parents=True, exist_ok=True)
    numbers = []
    for path in directory.iterdir():
        match = _RUN_FILE_RE.match(path.name)
        if match:
            numbers.append(int(match.group(1)))
    number = max(numbers, default=0) + 1
    candidate = directory / f"E5-LIVE-{number:03d}.json"
    while candidate.exists():
        number += 1
        candidate = directory / f"E5-LIVE-{number:03d}.json"
    return candidate


def _write_run_file(payload, directory=Path("evals/runs")):
    """Write once using exclusive creation so concurrent runs cannot overwrite."""
    while True:
        output_path = _next_run_file(directory)
        try:
            with output_path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            return output_path
        except FileExistsError:
            continue


def _tool_sequence(trace):
    """Return only ordered capability names; never include arguments or content."""
    return [item.get("capability") for item in trace
            if item.get("event") == "TOOL_INVOCATION_SIGNATURE" and item.get("capability")]


def _provider_failure_stage(error_type, trace):
    if not error_type:
        return None
    if error_type in _PROVIDER_CONNECTION_ERRORS or error_type in _PROVIDER_AUTH_ERRORS:
        return "MODEL_REQUEST"
    if any(item.get("event") == "TOOL_OBSERVATION_SUMMARY" and item.get("status") == "FAILURE"
           for item in trace):
        return "TOOL_EXECUTION"
    return "UNKNOWN"


def _failure_layer(error_type, trace, status, validator_passed):
    if error_type in _PROVIDER_CONNECTION_ERRORS:
        return "PROVIDER_CONNECTION"
    if error_type in _PROVIDER_AUTH_ERRORS:
        return "PROVIDER_AUTH"
    if error_type == "AGENT_TURN_LIMIT":
        return "AGENT_TURN_LIMIT"
    if any(item.get("event") == "TOOL_OBSERVATION_SUMMARY" and item.get("status") == "FAILURE"
           for item in trace) or (error_type or "").startswith("TOOL_"):
        return "TOOL"
    if status == "SUCCESS" and not validator_passed:
        return "VALIDATION"
    return "UNKNOWN"


def _termination_status(status, error_type, completion_claim_present):
    if status == "SUCCESS" and completion_claim_present:
        return "COMPLETED"
    if error_type == "AGENT_TURN_LIMIT":
        return "TURN_LIMIT"
    if error_type in _PROVIDER_CONNECTION_ERRORS or error_type in _PROVIDER_AUTH_ERRORS:
        return "PROVIDER_FAILURE"
    return "UNKNOWN"


def _strict_validator(*, test_before_passed, test_after_passed, source_repo_unchanged, validator_final_patch_files, target_file="src/calculator.py"):
    return bool(
        not test_before_passed
        and test_after_passed
        and source_repo_unchanged
        and validator_final_patch_files
        and target_file in validator_final_patch_files
    )


def _tool_failure_aggregation(summaries):
    failures=[item for item in summaries if item.get("status") == "FAILURE"]
    by_type=Counter(item.get("error_type") or "UNKNOWN" for item in failures)
    by_capability=Counter(item.get("capability") or "UNKNOWN" for item in failures)
    return failures, dict(by_type), dict(by_capability)


def _live_task():
    return {"objective":"The repository contains a failing test. Find the cause, fix it in the staged workspace, run the relevant tests, inspect the final diff, and return a concise completion claim. Do not modify the source repository.","constraints":["Use only the exposed workspace tools","cli.exec is restricted to pytest commands for this fixture. Use workspace.list/read/search/diff for repository inspection."],"acceptance_criteria":["staged test passes","source remains unchanged"],"allowed_capabilities":["workspace.list","workspace.read","workspace.search","workspace.edit","workspace.diff","cli.exec"],"allowed_side_effect_capabilities":["workspace.edit"]}


def _live_command_policy():
    return CommandPolicy([CommandRule(["pytest"], allow_extra_args=True)])


async def main():
    required=("ODYS_AGENT_MODEL","ODYS_AGENT_API_KEY")
    if not all(os.getenv(k) for k in required): print("STATUS=SKIPPED_CONFIG"); return 0
    try:
        config=AgentsSdkModelConfig()
    except ValueError as exc:
        print(f"ERROR={exc}")
        return 1
    started=time.monotonic(); source=Path(tempfile.mkdtemp(prefix="odys-e5-source-")); stage=None; before_sha=None
    try:
        _fixture(source); before_sha=_tree_sha(source); stage=StagedWorkspace.create(source)
        registry=ToolRegistry(); policy=_live_command_policy()
        register_staged_workspace_tools(registry, stage, policy)
        task=_live_task()
        request=ExecutionRequest(task_id=new_id(),run_id=new_id(),attempt_id=new_id(),attempt_number=1,task=task,metadata={"max_turns":MAX_TURNS})
        test_before,before_exit=_test_after(stage)
        backend=OpenAIAgentsBackend(registry, config); result=await InnerAgentExecutor(backend,allowed_side_effect_capabilities=["workspace.edit"]).execute(request)
        validator_patch=await stage.diff(); test_after, exit_code=_test_after(stage); patch=result.artifacts.get("workspace_patch",{}); trace=(result.raw or {}).get("trace",[])
        signatures=[(x.get("capability"),x.get("args_sha256")) for x in trace if x.get("event")=="TOOL_INVOCATION_SIGNATURE"]; counts={k:signatures.count(k) for k in set(signatures)}
        sequence=_tool_sequence(trace)
        summaries=[x for x in trace if x.get("event")=="TOOL_OBSERVATION_SUMMARY"]; inspected=[]
        for item in summaries:
            inspected.extend(item.get("matched_paths",[]));
            if item.get("capability")=="workspace.read" and item.get("path"): inspected.append(item["path"])
        agent_candidate_patch_files=patch.get("changed_files",[])
        validator_final_patch_files=validator_patch.get("changed_files",[])
        source_repo_unchanged=_tree_sha(source)==before_sha
        functional_validation_passed=_strict_validator(test_before_passed=test_before,test_after_passed=test_after,source_repo_unchanged=source_repo_unchanged,validator_final_patch_files=validator_final_patch_files)
        completion_claim_present=bool((result.raw or {}).get("completion_claim", False) and result.output)
        failures,failures_by_type,failures_by_capability=_tool_failure_aggregation(summaries)
        payload={"run_id":request.run_id,"timestamp":time.time(),"git_sha":subprocess.run(["git","rev-parse","HEAD"],capture_output=True,text=True).stdout.strip(),"harness_version":HARNESS_VERSION,"model":config.model,"provider_profile":config.provider_profile.name,"status":result.status.value,"agent_status":result.status.value,"functional_validation_passed":functional_validation_passed,"validator_passed":functional_validation_passed,"completion_claim_present":completion_claim_present,"termination_status":_termination_status(result.status.value,result.error_type,completion_claim_present),"duration_ms":int((time.monotonic()-started)*1000),"turn_count":(result.raw or {}).get("turn_count",0),"tool_call_count":(result.raw or {}).get("tool_call_count",0),"tool_calls_by_capability":{cap:sum(1 for x in signatures if x[0]==cap) for cap,_ in signatures},"tool_sequence":sequence,"tool_failures":[x.get("error_type") for x in summaries if x.get("status")=="FAILURE"],"tool_failure_count":len(failures),"tool_failures_by_type":failures_by_type,"tool_failures_by_capability":failures_by_capability,"repeated_tool_calls":sum(v>1 for v in counts.values()),"repeated_tool_call_count":sum(max(0,v-1) for v in counts.values()),"files_inspected":list(dict.fromkeys(inspected))[:100],"files_modified":agent_candidate_patch_files,"agent_candidate_patch_files":agent_candidate_patch_files,"validator_final_patch_files":validator_final_patch_files,"validator_final_files_changed":validator_patch.get("files_changed",0),"validator_final_lines_added":validator_patch.get("lines_added",0),"validator_final_lines_removed":validator_patch.get("lines_removed",0),"validator_final_patch_sha256":hashlib.sha256(validator_patch.get("diff","").encode("utf-8")).hexdigest(),"test_before":"FAIL" if not test_before else f"PASS({before_exit})","test_after":"PASS" if test_after else f"FAIL({exit_code})","input_tokens":result.usage.get("input_tokens"),"output_tokens":result.usage.get("output_tokens"),"total_tokens":result.usage.get("total_tokens"),"candidate_patch_files":agent_candidate_patch_files,"source_repo_unchanged":source_repo_unchanged,"final_output":result.output,"error_type":result.error_type,"error_message":result.error_message,"provider_api_mode":config.api_mode,"base_url_configured":bool(config.base_url),"api_key_configured":bool(config.api_key),"provider_failure_stage":_provider_failure_stage(result.error_type, trace),"failure_layer":_failure_layer(result.error_type, trace, result.status.value, functional_validation_passed)}
        output_path=_write_run_file(payload); print("STATUS="+payload["status"]); print("RESULT="+output_path.as_posix()); return 0
    finally:
        shutil.rmtree(source,ignore_errors=True)
if __name__ == "__main__": raise SystemExit(asyncio.run(main()))
