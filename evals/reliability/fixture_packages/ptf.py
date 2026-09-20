"""PROVIDER_TOOL_FAILURE fixtures (PTF-01 through PTF-10)."""
from pathlib import Path
from .base import BaseFixture


class _PTFBase(BaseFixture):
    family = "PROVIDER_TOOL_FAILURE"
    fixture_id = "fixture-provider-v1"


class PTF01(_PTFBase):
    task_id = "PTF-01"
    fault_ids = ["PROVIDER_TIMEOUT_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "provider_stub.json", {
            "endpoint": "https://api.example.com/v1",
            "status": "available",
            "call_count": 0,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        stub = self._read_json(workspace_dir / "provider_stub.json")
        stub["status"] = "timeout"
        stub["call_count"] = 1
        stub["error"] = "PROVIDER_TIMEOUT_ON_CALL_1"
        self._write_json(workspace_dir / "provider_stub.json", stub)

    def observe(self, workspace_dir: Path) -> dict:
        stub = self._read_json(workspace_dir / "provider_stub.json")
        return {
            "failure_type": stub.get("status"),
            "call_count": stub.get("call_count", 0),
            "expected_effect": "timeout classified, no false completion",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "provider_stub.json").unlink(missing_ok=True)


class PTF02(_PTFBase):
    task_id = "PTF-02"
    fault_ids = ["PROVIDER_UNAVAILABLE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "provider_stub.json", {
            "endpoint": "https://api.example.com/v1",
            "status": "available",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "provider_stub.json", {
            "endpoint": "https://api.example.com/v1",
            "status": "unavailable",
            "error": "PROVIDER_UNAVAILABLE",
            "systemic": True,
        })

    def observe(self, workspace_dir: Path) -> dict:
        stub = self._read_json(workspace_dir / "provider_stub.json")
        return {
            "failure_type": stub.get("status"),
            "systemic": stub.get("systemic", False),
            "expected_effect": "provider unavailability classified as systemic",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "provider_stub.json").unlink(missing_ok=True)


class PTF03(_PTFBase):
    task_id = "PTF-03"
    fault_ids = ["QUOTA_EXHAUSTED"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "provider_budget.json", {
            "quota_remaining": 100,
            "status": "available",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "provider_budget.json", {
            "quota_remaining": 0,
            "status": "quota_exhausted",
            "error": "QUOTA_EXHAUSTED",
        })

    def observe(self, workspace_dir: Path) -> dict:
        budget = self._read_json(workspace_dir / "provider_budget.json")
        return {
            "quota_remaining": budget.get("quota_remaining"),
            "failure_type": budget.get("status"),
            "expected_effect": "quota classified as macro scope, no local repair",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "provider_budget.json").unlink(missing_ok=True)


class PTF04(_PTFBase):
    task_id = "PTF-04"
    fault_ids = ["MALFORMED_RESPONSE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "provider_response.json", {
            "status": "ok",
            "data": {"result": "valid_output"},
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "provider_response.json", {
            "status": "malformed",
            "data": None,
            "error": "MALFORMED_RESPONSE",
        })

    def observe(self, workspace_dir: Path) -> dict:
        resp = self._read_json(workspace_dir / "provider_response.json")
        return {
            "response_status": resp.get("status"),
            "data_valid": resp.get("data") is not None,
            "expected_effect": "malformed output rejected, not treated as success",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "provider_response.json").unlink(missing_ok=True)


class PTF05(_PTFBase):
    task_id = "PTF-05"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "tool_state.json", {
            "tool_name": "data_processor",
            "call_count": 0,
            "status": "ready",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "tool_state.json", {
            "tool_name": "data_processor",
            "call_count": 1,
            "status": "failed",
            "error_type": "tool_failure",
            "error": "FAIL_TOOL_ON_CALL_1",
            "attempt_id": "A1",
        })

    def observe(self, workspace_dir: Path) -> dict:
        state = self._read_json(workspace_dir / "tool_state.json")
        return {
            "failure_type": state.get("error_type"),
            "attempt_present": bool(state.get("attempt_id")),
            "expected_effect": "typed error visible, attempt identity present",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "tool_state.json").unlink(missing_ok=True)


class PTF06(_PTFBase):
    task_id = "PTF-06"
    fault_ids = ["FAIL_TOOL_ON_CALL_2"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "stubs.json", {
            "tool": {"status": "ready", "call_count": 0},
            "provider": {"status": "available"},
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        stubs = self._read_json(workspace_dir / "stubs.json")
        stubs["tool"]["status"] = "failed"
        stubs["tool"]["call_count"] = 2
        stubs["tool"]["error"] = "FAIL_TOOL_ON_CALL_2"
        self._write_json(workspace_dir / "stubs.json", stubs)

    def observe(self, workspace_dir: Path) -> dict:
        stubs = self._read_json(workspace_dir / "stubs.json")
        return {
            "tool_status": stubs["tool"]["status"],
            "provider_status": stubs["provider"]["status"],
            "classification": "retryable" if stubs["provider"]["status"] == "available" else "systemic",
            "expected_effect": "classification determines repair scope",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "stubs.json").unlink(missing_ok=True)


class PTF07(_PTFBase):
    task_id = "PTF-07"
    fault_ids = ["PROVIDER_UNAVAILABLE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "provider_stub.json", {
            "status": "available",
            "retry_count": 0,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "provider_stub.json", {
            "status": "unavailable",
            "error": "PROVIDER_UNAVAILABLE",
            "systemic": True,
        })

    def observe(self, workspace_dir: Path) -> dict:
        stub = self._read_json(workspace_dir / "provider_stub.json")
        return {
            "failure_type": stub.get("status"),
            "systemic": stub.get("systemic", False),
            "expected_effect": "provider unavailability visible, no repeated local work",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "provider_stub.json").unlink(missing_ok=True)


class PTF08(_PTFBase):
    task_id = "PTF-08"
    fault_ids = ["INTERRUPT_AFTER_EFFECT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "effect_state.json", {
            "partial_effect": {"data": "partial_value"},
            "committed": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        state = self._read_json(workspace_dir / "effect_state.json")
        state["committed"] = True
        state["crashed"] = True
        state["error"] = "INTERRUPT_AFTER_EFFECT"
        self._write_json(workspace_dir / "effect_state.json", state)

    def observe(self, workspace_dir: Path) -> dict:
        state = self._read_json(workspace_dir / "effect_state.json")
        return {
            "partial_effect_exists": bool(state.get("partial_effect")),
            "committed": state.get("committed", False),
            "expected_effect": "partial effect reconciled, no duplicate",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "effect_state.json").unlink(missing_ok=True)


class PTF09(_PTFBase):
    task_id = "PTF-09"
    fault_ids = ["CAPABILITY_UNAVAILABLE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "capability_registry.json", {
            "available": ["workspace.read"],
            "unavailable": ["cli.exec"],
            "requested": "cli.exec",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        reg = self._read_json(workspace_dir / "capability_registry.json")
        reg["resolution"] = "failed"
        reg["error"] = "CAPABILITY_UNAVAILABLE"
        self._write_json(workspace_dir / "capability_registry.json", reg)

    def observe(self, workspace_dir: Path) -> dict:
        reg = self._read_json(workspace_dir / "capability_registry.json")
        return {
            "requested_capability": reg.get("requested"),
            "available": reg.get("available", []),
            "resolution": reg.get("resolution"),
            "expected_effect": "capability failure visible, no silent substitute",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "capability_registry.json").unlink(missing_ok=True)


class PTF10(_PTFBase):
    task_id = "PTF-10"
    fault_ids = ["PROVIDER_TIMEOUT_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "retry_budget.json", {
            "max_retries": 3,
            "current_retry": 0,
            "provider_status": "available",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        budget = self._read_json(workspace_dir / "retry_budget.json")
        budget["current_retry"] = budget["max_retries"]
        budget["provider_status"] = "timeout"
        budget["error"] = "PROVIDER_TIMEOUT_ON_CALL_1"
        self._write_json(workspace_dir / "retry_budget.json", budget)

    def observe(self, workspace_dir: Path) -> dict:
        budget = self._read_json(workspace_dir / "retry_budget.json")
        return {
            "current_retry": budget.get("current_retry", 0),
            "max_retries": budget.get("max_retries", 3),
            "budget_respected": budget.get("current_retry", 0) <= budget.get("max_retries", 3),
            "expected_effect": "retry budget respected after repeated failures",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "retry_budget.json").unlink(missing_ok=True)


PTF_FIXTURES = [PTF01, PTF02, PTF03, PTF04, PTF05, PTF06, PTF07, PTF08, PTF09, PTF10]
