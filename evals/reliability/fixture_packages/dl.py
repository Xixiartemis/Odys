"""DELEGATION_LIFECYCLE fixtures (DL-01 through DL-10)."""
from pathlib import Path
from .base import BaseFixture


class _DLBase(BaseFixture):
    family = "DELEGATION_LIFECYCLE"
    fixture_id = "fixture-delegation-v1"


class DL01(_DLBase):
    task_id = "DL-01"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "parent_child_ledger.json", {
            "parent_id": "P1",
            "children": [],
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        ledger = self._read_json(workspace_dir / "parent_child_ledger.json")
        ledger["children"].append({
            "child_id": "C1",
            "status": "failed",
            "error": "FAIL_TOOL_ON_CALL_1",
            "created_at": "2026-01-01T00:00:00Z",
        })
        self._write_json(workspace_dir / "parent_child_ledger.json", ledger)

    def observe(self, workspace_dir: Path) -> dict:
        ledger = self._read_json(workspace_dir / "parent_child_ledger.json")
        children = ledger.get("children", [])
        return {
            "child_count": len(children),
            "child_statuses": [c.get("status") for c in children],
            "lifecycle_durable": all(bool(c.get("child_id")) for c in children),
            "expected_effect": "child lifecycle (create/execute/complete) is durable",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "parent_child_ledger.json").unlink(missing_ok=True)


class DL02(_DLBase):
    task_id = "DL-02"
    fault_ids = ["DUPLICATE_DELIVERY_ATTEMPT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "delivery_ledger.json", {
            "deliveries": [],
            "parent_waiting": True,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        ledger = self._read_json(workspace_dir / "delivery_ledger.json")
        ledger["deliveries"].append({"id": "d1", "result": "data"})
        ledger["deliveries"].append({"id": "d2", "result": "data", "duplicate": True})
        self._write_json(workspace_dir / "delivery_ledger.json", ledger)

    def observe(self, workspace_dir: Path) -> dict:
        ledger = self._read_json(workspace_dir / "delivery_ledger.json")
        deliveries = ledger.get("deliveries", [])
        duplicates = [d for d in deliveries if d.get("duplicate")]
        return {
            "delivery_count": len(deliveries),
            "duplicate_count": len(duplicates),
            "expected_effect": "result delivered once, duplicate rejected",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "delivery_ledger.json").unlink(missing_ok=True)


class DL03(_DLBase):
    task_id = "DL-03"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "delegation_state.json", {
            "parent_id": "P1",
            "child_id": "C1",
            "child_status": "running",
            "parent_observed_failure": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        state = self._read_json(workspace_dir / "delegation_state.json")
        state["child_status"] = "failed"
        state["error"] = "FAIL_TOOL_ON_CALL_1"
        state["parent_observed_failure"] = True
        self._write_json(workspace_dir / "delegation_state.json", state)

    def observe(self, workspace_dir: Path) -> dict:
        state = self._read_json(workspace_dir / "delegation_state.json")
        return {
            "child_status": state.get("child_status"),
            "parent_observed_failure": state.get("parent_observed_failure", False),
            "expected_effect": "child failure visible to parent workflow",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "delegation_state.json").unlink(missing_ok=True)


class DL04(_DLBase):
    task_id = "DL-04"
    fault_ids = ["DUPLICATE_DELIVERY_ATTEMPT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "delivery_ledger.json", {
            "entries": [],
            "parent_effects": 0,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        ledger = self._read_json(workspace_dir / "delivery_ledger.json")
        ledger["entries"].append({"delivery_id": "d1", "processed": True})
        ledger["entries"].append({"delivery_id": "d2", "processed": False, "duplicate": True})
        self._write_json(workspace_dir / "delivery_ledger.json", ledger)

    def observe(self, workspace_dir: Path) -> dict:
        ledger = self._read_json(workspace_dir / "delivery_ledger.json")
        processed = [e for e in ledger.get("entries", []) if e.get("processed")]
        return {
            "processed_count": len(processed),
            "duplicate_rejected": any(
                e.get("duplicate") for e in ledger.get("entries", [])
            ),
            "expected_effect": "duplicate delivery creates no second parent side effect",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "delivery_ledger.json").unlink(missing_ok=True)


class DL05(_DLBase):
    task_id = "DL-05"
    fault_ids = ["INTERRUPT_AFTER_EFFECT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "handoff_record.json", {
            "parent_id": "P1",
            "child_id": "C1",
            "handoff_evidence": None,
            "persisted": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        record = self._read_json(workspace_dir / "handoff_record.json")
        record["handoff_evidence"] = {
            "delegated_at": "2026-01-01T00:00:00Z",
            "capability_set": ["workspace.read"],
        }
        record["persisted"] = True
        record["interrupted"] = True
        self._write_json(workspace_dir / "handoff_record.json", record)

    def observe(self, workspace_dir: Path) -> dict:
        record = self._read_json(workspace_dir / "handoff_record.json")
        return {
            "handoff_evidence_exists": record.get("handoff_evidence") is not None,
            "persisted": record.get("persisted", False),
            "expected_effect": "handoff evidence durable and auditable",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "handoff_record.json").unlink(missing_ok=True)


class DL06(_DLBase):
    task_id = "DL-06"
    fault_ids = ["INTERRUPT_AFTER_EFFECT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "child_state.json", {
            "child_id": "C1",
            "progress": {"step": 3, "total": 5},
            "interrupted": False,
            "partial_work": {"data": "partial"},
        })
        self._write_json(workspace_dir / "delivery_ledger.json", {
            "deliveries": [],
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        state = self._read_json(workspace_dir / "child_state.json")
        state["interrupted"] = True
        self._write_json(workspace_dir / "child_state.json", state)

    def observe(self, workspace_dir: Path) -> dict:
        state = self._read_json(workspace_dir / "child_state.json")
        ledger = self._read_json(workspace_dir / "delivery_ledger.json")
        return {
            "child_interrupted": state.get("interrupted", False),
            "partial_work_exists": bool(state.get("partial_work")),
            "delivery_count": len(ledger.get("deliveries", [])),
            "expected_effect": "child resumes, parent receives one result",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["child_state.json", "delivery_ledger.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class DL07(_DLBase):
    task_id = "DL-07"
    fault_ids = ["PARTIAL_OUTPUT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "parent_state.json", {
            "parent_id": "P1",
            "pending_children": ["C1"],
            "completion_claimed": False,
        })
        self._write_json(workspace_dir / "child_evidence.json", {
            "child_id": "C1",
            "result_durable": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        parent = self._read_json(workspace_dir / "parent_state.json")
        parent["completion_claimed"] = True  # premature
        self._write_json(workspace_dir / "parent_state.json", parent)

    def observe(self, workspace_dir: Path) -> dict:
        parent = self._read_json(workspace_dir / "parent_state.json")
        child = self._read_json(workspace_dir / "child_evidence.json")
        return {
            "parent_completion_claimed": parent.get("completion_claimed", False),
            "child_result_durable": child.get("result_durable", False),
            "premature": parent.get("completion_claimed") and not child.get("result_durable"),
            "expected_effect": "parent waits for durable child result",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["parent_state.json", "child_evidence.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class DL08(_DLBase):
    task_id = "DL-08"
    fault_ids = ["PROVIDER_TIMEOUT_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "child_provider.json", {
            "child_id": "C1",
            "provider_status": "available",
            "failure_visible_to_parent": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        cp = self._read_json(workspace_dir / "child_provider.json")
        cp["provider_status"] = "timeout"
        cp["error"] = "PROVIDER_TIMEOUT_ON_CALL_1"
        cp["failure_visible_to_parent"] = True
        self._write_json(workspace_dir / "child_provider.json", cp)

    def observe(self, workspace_dir: Path) -> dict:
        cp = self._read_json(workspace_dir / "child_provider.json")
        return {
            "provider_status": cp.get("provider_status"),
            "failure_visible_to_parent": cp.get("failure_visible_to_parent", False),
            "expected_effect": "child provider failure visible to parent, no hidden retry",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "child_provider.json").unlink(missing_ok=True)


class DL09(_DLBase):
    task_id = "DL-09"
    fault_ids = ["CAPABILITY_UNAVAILABLE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "child_capability.json", {
            "child_id": "C1",
            "required": ["cli.exec"],
            "available": ["workspace.read"],
            "denial_recorded": False,
            "substitute_calls": 0,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        cap = self._read_json(workspace_dir / "child_capability.json")
        cap["denial_recorded"] = True
        cap["error"] = "CAPABILITY_UNAVAILABLE"
        self._write_json(workspace_dir / "child_capability.json", cap)

    def observe(self, workspace_dir: Path) -> dict:
        cap = self._read_json(workspace_dir / "child_capability.json")
        return {
            "denial_recorded": cap.get("denial_recorded", False),
            "substitute_calls": cap.get("substitute_calls", 0),
            "expected_effect": "capability denial durable, no undeclared substitute",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "child_capability.json").unlink(missing_ok=True)


class DL10(_DLBase):
    task_id = "DL-10"
    fault_ids = ["MALFORMED_RESPONSE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "lifecycle_evidence.json", {
            "delegation_id": "D1",
            "events": ["created", "dispatched", "completed"],
            "claim_scope": "delegation_lifecycle_only",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        evidence = self._read_json(workspace_dir / "lifecycle_evidence.json")
        evidence["malformed_result"] = True
        evidence["error"] = "MALFORMED_RESPONSE"
        self._write_json(workspace_dir / "lifecycle_evidence.json", evidence)

    def observe(self, workspace_dir: Path) -> dict:
        evidence = self._read_json(workspace_dir / "lifecycle_evidence.json")
        return {
            "lifecycle_events": evidence.get("events", []),
            "claim_scope": evidence.get("claim_scope"),
            "malformed": evidence.get("malformed_result", False),
            "expected_effect": "lifecycle evidence present, claim scope bounded",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "lifecycle_evidence.json").unlink(missing_ok=True)


DL_FIXTURES = [DL01, DL02, DL03, DL04, DL05, DL06, DL07, DL08, DL09, DL10]
