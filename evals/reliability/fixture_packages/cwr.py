"""COMPLEX_WORKFLOW_REPLAN fixtures (CWR-01 through CWR-10)."""
from pathlib import Path
from .base import BaseFixture


class _CWRBase(BaseFixture):
    family = "COMPLEX_WORKFLOW_REPLAN"
    fixture_id = "fixture-replan-v1"


class CWR01(_CWRBase):
    task_id = "CWR-01"
    fault_ids = ["INVALIDATE_ASSUMPTION"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "dependency_graph.json", {
            "nodes": [
                {"id": "A", "status": "verified", "assumption": "data_format_v1"},
                {"id": "B", "status": "pending", "depends_on": ["A"]},
                {"id": "C", "status": "pending", "depends_on": ["A"]},
            ],
            "edges": [["A", "B"], ["A", "C"]],
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        graph = self._read_json(workspace_dir / "dependency_graph.json")
        for node in graph["nodes"]:
            if node["id"] == "A":
                node["assumption"] = "data_format_v2"
                node["assumption_valid"] = False
        self._write_json(workspace_dir / "dependency_graph.json", graph)

    def observe(self, workspace_dir: Path) -> dict:
        graph = self._read_json(workspace_dir / "dependency_graph.json")
        affected = [
            n["id"] for n in graph["nodes"]
            if n.get("depends_on") and "A" in n["depends_on"]
        ]
        return {
            "assumption_invalidated": any(
                not n.get("assumption_valid", True) for n in graph["nodes"]
            ),
            "affected_descendants": affected,
            "expected_effect": "affected descendants corrected, scope limited to subgraph",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "dependency_graph.json").unlink(missing_ok=True)


class CWR02(_CWRBase):
    task_id = "CWR-02"
    fault_ids = ["STALE_WORKSPACE_BEFORE_DISPATCH"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "context.json", {
            "version": 1,
            "data_source": "api_v1",
            "downstream_steps": ["transform", "validate", "output"],
        })
        self._write_json(workspace_dir / "workflow_state.json", {
            "current_step": "transform",
            "context_version": 1,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "context.json", {
            "version": 2,
            "data_source": "api_v2",
            "downstream_steps": ["transform", "validate", "output"],
            "breaking_change": True,
        })

    def observe(self, workspace_dir: Path) -> dict:
        ctx = self._read_json(workspace_dir / "context.json")
        state = self._read_json(workspace_dir / "workflow_state.json")
        return {
            "context_version": ctx.get("version"),
            "workflow_context_version": state.get("context_version"),
            "stale": ctx.get("version") != state.get("context_version"),
            "expected_effect": "downstream context refreshed before dispatch",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["context.json", "workflow_state.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CWR03(_CWRBase):
    task_id = "CWR-03"
    fault_ids = ["PROVIDER_UNAVAILABLE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "workflow.json", {
            "steps": [
                {"id": "s1", "type": "local", "status": "complete"},
                {"id": "s2", "type": "provider", "status": "pending"},
                {"id": "s3", "type": "provider", "status": "pending"},
                {"id": "s4", "type": "local", "status": "pending"},
            ],
            "provider_status": "available",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        wf = self._read_json(workspace_dir / "workflow.json")
        wf["provider_status"] = "unavailable"
        self._write_json(workspace_dir / "workflow.json", wf)

    def observe(self, workspace_dir: Path) -> dict:
        wf = self._read_json(workspace_dir / "workflow.json")
        return {
            "provider_status": wf.get("provider_status"),
            "provider_steps": [
                s["id"] for s in wf.get("steps", []) if s.get("type") == "provider"
            ],
            "expected_effect": "systemic failure triggers macro replan, not local retry",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "workflow.json").unlink(missing_ok=True)


class CWR04(_CWRBase):
    task_id = "CWR-04"
    fault_ids = ["INVALIDATE_ASSUMPTION"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "branches.json", {
            "branch_a": {
                "status": "verified",
                "nodes": ["A1", "A2", "A3"],
                "independent": True,
            },
            "branch_b": {
                "status": "pending",
                "nodes": ["B1", "B2"],
                "failed_at": None,
            },
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        branches = self._read_json(workspace_dir / "branches.json")
        branches["branch_b"]["status"] = "failed"
        branches["branch_b"]["failed_at"] = "B1"
        self._write_json(workspace_dir / "branches.json", branches)

    def observe(self, workspace_dir: Path) -> dict:
        branches = self._read_json(workspace_dir / "branches.json")
        return {
            "branch_a_status": branches["branch_a"]["status"],
            "branch_b_status": branches["branch_b"]["status"],
            "expected_effect": "branch_a preserved (not reexecuted), branch_b repaired",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "branches.json").unlink(missing_ok=True)


class CWR05(_CWRBase):
    task_id = "CWR-05"
    fault_ids = ["INVALIDATE_ASSUMPTION"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "dependencies.json", {
            "prerequisite": {"id": "P", "status": "failed", "output": None},
            "dependent": {"id": "D", "status": "blocked", "requires": "P"},
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        deps = self._read_json(workspace_dir / "dependencies.json")
        deps["prerequisite"]["status"] = "repaired_pending_verification"
        self._write_json(workspace_dir / "dependencies.json", deps)

    def observe(self, workspace_dir: Path) -> dict:
        deps = self._read_json(workspace_dir / "dependencies.json")
        return {
            "prerequisite_status": deps["prerequisite"]["status"],
            "dependent_status": deps["dependent"]["status"],
            "expected_effect": "dependent runs only after prerequisite verification",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "dependencies.json").unlink(missing_ok=True)


class CWR06(_CWRBase):
    task_id = "CWR-06"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "workflow.json", {
            "steps": [
                {"id": "s1", "status": "verified"},
                {"id": "s2", "status": "failed"},
                {"id": "s3", "status": "pending", "depends_on": "s2"},
            ],
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        wf = self._read_json(workspace_dir / "workflow.json")
        wf["steps"][1]["error"] = "FAIL_TOOL_ON_CALL_1"
        self._write_json(workspace_dir / "workflow.json", wf)

    def observe(self, workspace_dir: Path) -> dict:
        wf = self._read_json(workspace_dir / "workflow.json")
        return {
            "step_statuses": {s["id"]: s["status"] for s in wf["steps"]},
            "verified_ancestors_preserved": wf["steps"][0]["status"] == "verified",
            "expected_effect": "local repair only, verified ancestors preserved",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "workflow.json").unlink(missing_ok=True)


class CWR07(_CWRBase):
    task_id = "CWR-07"
    fault_ids = ["FAIL_TOOL_ON_CALL_2"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "lineage.json", {
            "original_attempt": "A1",
            "repair_attempt": None,
            "workflow_type": "typed",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        lineage = self._read_json(workspace_dir / "lineage.json")
        lineage["repair_attempt"] = "A2"
        lineage["repair_error"] = "FAIL_TOOL_ON_CALL_2"
        self._write_json(workspace_dir / "lineage.json", lineage)

    def observe(self, workspace_dir: Path) -> dict:
        lineage = self._read_json(workspace_dir / "lineage.json")
        return {
            "original_attempt": lineage.get("original_attempt"),
            "repair_attempt": lineage.get("repair_attempt"),
            "lineage_durable": bool(
                lineage.get("original_attempt") and lineage.get("repair_attempt")
            ),
            "expected_effect": "A1 and A2 are durable",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "lineage.json").unlink(missing_ok=True)


class CWR08(_CWRBase):
    task_id = "CWR-08"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "repair_budget.json", {
            "max_local_attempts": 3,
            "current_attempt": 0,
            "step_id": "failing_step",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        budget = self._read_json(workspace_dir / "repair_budget.json")
        budget["current_attempt"] = budget["max_local_attempts"]
        budget["status"] = "exhausted"
        budget["last_error"] = "FAIL_TOOL_ON_CALL_1"
        self._write_json(workspace_dir / "repair_budget.json", budget)

    def observe(self, workspace_dir: Path) -> dict:
        budget = self._read_json(workspace_dir / "repair_budget.json")
        return {
            "current_attempt": budget.get("current_attempt", 0),
            "max_attempts": budget.get("max_local_attempts", 3),
            "budget_respected": budget.get("current_attempt", 0) <= budget.get("max_local_attempts", 3),
            "expected_effect": "budget respected, failure visible",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "repair_budget.json").unlink(missing_ok=True)


class CWR09(_CWRBase):
    task_id = "CWR-09"
    fault_ids = ["INVALIDATE_ASSUMPTION"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "plan.json", {
            "mode": "LINEAR",
            "steps": ["s1", "s2", "s3"],
            "legacy": True,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        plan = self._read_json(workspace_dir / "plan.json")
        plan["assumption_invalidated"] = True
        self._write_json(workspace_dir / "plan.json", plan)

    def observe(self, workspace_dir: Path) -> dict:
        plan = self._read_json(workspace_dir / "plan.json")
        return {
            "mode": plan.get("mode"),
            "is_legacy": plan.get("legacy", False),
            "expected_effect": "report legacy LINEAR behavior, no selective rewrite",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "plan.json").unlink(missing_ok=True)


class CWR10(_CWRBase):
    task_id = "CWR-10"
    fault_ids = ["QUOTA_EXHAUSTED"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "workflow.json", {
            "nodes": [{"id": "n1", "type": "provider", "status": "pending"}],
            "shape": "single_node",
        })
        self._write_json(workspace_dir / "provider_budget.json", {
            "quota_remaining": 100,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "provider_budget.json", {
            "quota_remaining": 0,
            "error": "QUOTA_EXHAUSTED",
        })

    def observe(self, workspace_dir: Path) -> dict:
        budget = self._read_json(workspace_dir / "provider_budget.json")
        wf = self._read_json(workspace_dir / "workflow.json")
        return {
            "quota_remaining": budget.get("quota_remaining"),
            "workflow_shape": wf.get("shape"),
            "expected_effect": "quota escalates to macro replan even in single-node workflow",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["workflow.json", "provider_budget.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


CWR_FIXTURES = [CWR01, CWR02, CWR03, CWR04, CWR05, CWR06, CWR07, CWR08, CWR09, CWR10]
