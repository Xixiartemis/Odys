"""EXECUTION_STATE_RECOVERY fixtures (ESR-01 through ESR-10)."""
from pathlib import Path
from .base import BaseFixture


class _ESRBase(BaseFixture):
    family = "EXECUTION_STATE_RECOVERY"
    fixture_id = "fixture-execution-v1"


class ESR01(_ESRBase):
    task_id = "ESR-01"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "partial_work.json", {
            "step": "generate_data",
            "status": "partial",
            "data_rows": 5,
            "expected_rows": 10,
        })
        self._write_json(workspace_dir / "checkpoint.json", {
            "last_completed_step": "init",
            "in_progress_step": "generate_data",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "tool_crash.json", {
            "error": "FAIL_TOOL_ON_CALL_1",
            "crashed_at": "generate_data",
            "partial_state": True,
        })

    def observe(self, workspace_dir: Path) -> dict:
        work = self._read_json(workspace_dir / "partial_work.json")
        crash = self._read_json(workspace_dir / "tool_crash.json")
        return {
            "partial_work_exists": work.get("status") == "partial",
            "crash_recorded": bool(crash.get("error")),
            "expected_effect": "partial work reconciled, final test passes",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["partial_work.json", "checkpoint.json", "tool_crash.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class ESR02(_ESRBase):
    task_id = "ESR-02"
    fault_ids = ["INTERRUPT_AFTER_EFFECT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "artifact.json", {
            "created": True,
            "content": {"data": "important_value"},
        })
        self._write_json(workspace_dir / "workflow_state.json", {
            "current_step": 3,
            "total_steps": 5,
            "interrupted": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        state = self._read_json(workspace_dir / "workflow_state.json")
        state["interrupted"] = True
        self._write_json(workspace_dir / "workflow_state.json", state)

    def observe(self, workspace_dir: Path) -> dict:
        artifact = self._read_json(workspace_dir / "artifact.json")
        state = self._read_json(workspace_dir / "workflow_state.json")
        return {
            "artifact_retained": artifact.get("created", False),
            "interrupted": state.get("interrupted", False),
            "current_step": state.get("current_step"),
            "expected_effect": "artifact retained, workflow resumes to valid end state",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["artifact.json", "workflow_state.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class ESR03(_ESRBase):
    task_id = "ESR-03"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "task_state.json", {
            "attempts": 0,
            "max_retries": 3,
            "transient_failure": True,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        state = self._read_json(workspace_dir / "task_state.json")
        state["attempts"] = 1
        state["last_error"] = "FAIL_TOOL_ON_CALL_1"
        self._write_json(workspace_dir / "task_state.json", state)

    def observe(self, workspace_dir: Path) -> dict:
        state = self._read_json(workspace_dir / "task_state.json")
        return {
            "attempts": state.get("attempts", 0),
            "max_retries": state.get("max_retries", 3),
            "final_effect_present": False,
            "expected_effect": "retry succeeds, final effect present, retry bounded",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "task_state.json").unlink(missing_ok=True)


class ESR04(_ESRBase):
    task_id = "ESR-04"
    fault_ids = ["INTERRUPT_AFTER_EFFECT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "side_effect_ledger.json", {
            "effects": [],
            "committed": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        ledger = self._read_json(workspace_dir / "side_effect_ledger.json")
        ledger["effects"] = [{"id": "effect_1", "status": "committed"}]
        ledger["committed"] = True
        self._write_json(workspace_dir / "side_effect_ledger.json", ledger)

    def observe(self, workspace_dir: Path) -> dict:
        ledger = self._read_json(workspace_dir / "side_effect_ledger.json")
        return {
            "side_effect_count": len(ledger.get("effects", [])),
            "committed": ledger.get("committed", False),
            "expected_effect": "exactly one effect, no duplicates after recovery",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "side_effect_ledger.json").unlink(missing_ok=True)


class ESR05(_ESRBase):
    task_id = "ESR-05"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "mutation_log.json", {
            "mutations": [],
            "tool_calls": 0,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        log = self._read_json(workspace_dir / "mutation_log.json")
        log["tool_calls"] = 1
        log["last_error"] = "FAIL_TOOL_ON_CALL_1"
        self._write_json(workspace_dir / "mutation_log.json", log)

    def observe(self, workspace_dir: Path) -> dict:
        log = self._read_json(workspace_dir / "mutation_log.json")
        return {
            "mutation_count": len(log.get("mutations", [])),
            "tool_calls": log.get("tool_calls", 0),
            "expected_effect": "no durable effect before recovery",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "mutation_log.json").unlink(missing_ok=True)


class ESR06(_ESRBase):
    task_id = "ESR-06"
    fault_ids = ["FAIL_TOOL_ON_CALL_2"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "ancestor.json", {
            "status": "verified",
            "hash": "ancestor_hash_abc",
            "content": "verified content",
        })
        self._write_json(workspace_dir / "descendant.json", {
            "status": "pending",
            "depends_on": "ancestor.json",
            "hash": "",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "descendant.json", {
            "status": "failed",
            "depends_on": "ancestor.json",
            "error": "FAIL_TOOL_ON_CALL_2",
            "hash": "",
        })

    def observe(self, workspace_dir: Path) -> dict:
        ancestor = self._read_json(workspace_dir / "ancestor.json")
        descendant = self._read_json(workspace_dir / "descendant.json")
        return {
            "ancestor_status": ancestor.get("status"),
            "ancestor_hash": ancestor.get("hash"),
            "descendant_status": descendant.get("status"),
            "expected_effect": "ancestor preserved, descendant eventually passes",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["ancestor.json", "descendant.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class ESR07(_ESRBase):
    task_id = "ESR-07"
    fault_ids = ["INTERRUPT_AFTER_EFFECT"]

    def setup(self, workspace_dir: Path) -> dict:
        content = "line1\nline2\nline3\nline4\nline5\n"
        self._write_text(workspace_dir / "baseline.txt", content)
        self._write_json(workspace_dir / "edit_state.json", {
            "target_file": "baseline.txt",
            "applied_edits": 0,
            "total_edits": 3,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        # Simulate partial edit: only 1 of 3 edits applied
        self._write_text(workspace_dir / "baseline.txt",
                         "line1_edited\nline2\nline3\nline4\nline5\n")
        state = self._read_json(workspace_dir / "edit_state.json")
        state["applied_edits"] = 1
        state["interrupted"] = True
        self._write_json(workspace_dir / "edit_state.json", state)

    def observe(self, workspace_dir: Path) -> dict:
        content = self._read_text(workspace_dir / "baseline.txt")
        state = self._read_json(workspace_dir / "edit_state.json")
        return {
            "file_hash": self._file_hash(workspace_dir / "baseline.txt"),
            "applied_edits": state.get("applied_edits", 0),
            "total_edits": state.get("total_edits", 0),
            "expected_effect": "file matches expected final hash after reconciliation",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["baseline.txt", "edit_state.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class ESR08(_ESRBase):
    task_id = "ESR-08"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "recovery_budget.json", {
            "max_attempts": 3,
            "current_attempt": 0,
            "status": "active",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        budget = self._read_json(workspace_dir / "recovery_budget.json")
        budget["current_attempt"] = budget["max_attempts"]
        budget["status"] = "exhausted"
        budget["last_error"] = "FAIL_TOOL_ON_CALL_1"
        self._write_json(workspace_dir / "recovery_budget.json", budget)

    def observe(self, workspace_dir: Path) -> dict:
        budget = self._read_json(workspace_dir / "recovery_budget.json")
        return {
            "current_attempt": budget.get("current_attempt", 0),
            "max_attempts": budget.get("max_attempts", 3),
            "status": budget.get("status"),
            "budget_respected": budget.get("current_attempt", 0) <= budget.get("max_attempts", 3),
            "expected_effect": "failure visible, attempt budget respected",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "recovery_budget.json").unlink(missing_ok=True)


class ESR09(_ESRBase):
    task_id = "ESR-09"
    fault_ids = ["MALFORMED_RESPONSE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "attempt_record.json", {
            "attempt_id": "A1",
            "status": "failed",
            "error_type": "tool_failure",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "malformed_report.json", {
            "attempt_id": None,
            "error_type": "MALFORMED_RESPONSE",
            "corrupted": True,
        })

    def observe(self, workspace_dir: Path) -> dict:
        record = self._read_json(workspace_dir / "attempt_record.json")
        report = self._read_json(workspace_dir / "malformed_report.json")
        return {
            "attempt_id": record.get("attempt_id"),
            "failure_identity_durable": bool(record.get("attempt_id")),
            "malformed_report": report.get("corrupted", False),
            "expected_effect": "failure identity is durable across recovery",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["attempt_record.json", "malformed_report.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class ESR10(_ESRBase):
    task_id = "ESR-10"
    fault_ids = ["PROVIDER_TIMEOUT_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "progress.json", {
            "step": 4,
            "total_steps": 10,
            "persisted_data": {"rows": 400},
        })
        self._write_json(workspace_dir / "provider_status.json", {
            "status": "available",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "provider_status.json", {
            "status": "timeout",
            "error": "PROVIDER_TIMEOUT_ON_CALL_1",
        })

    def observe(self, workspace_dir: Path) -> dict:
        progress = self._read_json(workspace_dir / "progress.json")
        provider = self._read_json(workspace_dir / "provider_status.json")
        return {
            "progress_preserved": bool(progress.get("persisted_data")),
            "provider_status": provider.get("status"),
            "expected_effect": "progress preserved, final validator passes after recovery",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["progress.json", "provider_status.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


ESR_FIXTURES = [ESR01, ESR02, ESR03, ESR04, ESR05, ESR06, ESR07, ESR08, ESR09, ESR10]
