"""COMPLETION_INTEGRITY fixtures (CI-01 through CI-10)."""
from pathlib import Path
from .base import BaseFixture


class _CIBase(BaseFixture):
    family = "COMPLETION_INTEGRITY"
    fixture_id = "fixture-completion-v1"


class CI01(_CIBase):
    task_id = "CI-01"
    fault_ids = ["PARTIAL_OUTPUT"]

    def setup(self, workspace_dir: Path) -> dict:
        # Artifact file that must exist before claiming completion
        self._write_json(workspace_dir / "artifact.json", {
            "status": "pending",
            "content": "",
            "required_fields": ["title", "body", "author", "timestamp"],
        })
        self._write_text(workspace_dir / "test_runner.sh",
                         "#!/bin/bash\ntest -f artifact.json && echo PASS || echo FAIL\n")
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        # Simulate claiming completion before artifact is created
        self._write_json(workspace_dir / "completion_claim.json", {
            "claimed": True,
            "artifact_verified": False,  # artifact not yet produced
        })

    def observe(self, workspace_dir: Path) -> dict:
        artifact = self._read_json(workspace_dir / "artifact.json")
        claim = self._read_json(workspace_dir / "completion_claim.json")
        return {
            "artifact_present": bool(artifact.get("content")),
            "completion_claimed": claim.get("claimed", False),
            "expected_effect": "artifact must exist before claim",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["artifact.json", "test_runner.sh", "completion_claim.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CI02(_CIBase):
    task_id = "CI-02"
    fault_ids = ["PARTIAL_OUTPUT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "output_schema.json", {
            "required_fields": ["name", "email", "address", "phone"],
            "type": "object",
        })
        self._write_json(workspace_dir / "partial_output.json", {
            "name": "Alice",
            "email": "alice@example.com",
            # address and phone missing
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        # partial_output already present from setup
        pass

    def observe(self, workspace_dir: Path) -> dict:
        schema = self._read_json(workspace_dir / "output_schema.json")
        output = self._read_json(workspace_dir / "partial_output.json")
        required = set(schema.get("required_fields", []))
        present = set(output.keys())
        missing = required - present
        return {
            "required_fields": sorted(required),
            "present_fields": sorted(present),
            "missing_fields": sorted(missing),
            "expected_effect": "all required fields must be present",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["output_schema.json", "partial_output.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CI03(_CIBase):
    task_id = "CI-03"
    fault_ids = ["FAIL_TOOL_ON_CALL_1"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_text(workspace_dir / "test_command.sh",
                         "#!/bin/bash\necho 'running tests...'\nexit 0\n")
        self._write_json(workspace_dir / "test_result.json", {
            "executed": False,
            "result": None,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "test_result.json", {
            "executed": False,
            "result": "TOOL_FAILURE",
            "error": "FAIL_TOOL_ON_CALL_1",
        })

    def observe(self, workspace_dir: Path) -> dict:
        result = self._read_json(workspace_dir / "test_result.json")
        return {
            "test_executed": result.get("executed", False),
            "test_result": result.get("result"),
            "expected_effect": "test must be executed and pass",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["test_command.sh", "test_result.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CI04(_CIBase):
    task_id = "CI-04"
    fault_ids = ["STALE_WORKSPACE_BEFORE_DISPATCH"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "workspace_state.json", {
            "version": 1,
            "content_hash": "abc123",
            "last_modified": "2026-01-01T00:00:00Z",
        })
        self._write_json(workspace_dir / "evidence.json", {
            "based_on_version": 1,
            "content_hash": "abc123",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        # Change workspace state, making evidence stale
        self._write_json(workspace_dir / "workspace_state.json", {
            "version": 2,
            "content_hash": "xyz789",
            "last_modified": "2026-01-02T00:00:00Z",
        })

    def observe(self, workspace_dir: Path) -> dict:
        state = self._read_json(workspace_dir / "workspace_state.json")
        evidence = self._read_json(workspace_dir / "evidence.json")
        stale = state.get("content_hash") != evidence.get("content_hash")
        return {
            "current_version": state.get("version"),
            "evidence_version": evidence.get("based_on_version"),
            "evidence_stale": stale,
            "expected_effect": "evidence must match current state",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["workspace_state.json", "evidence.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CI05(_CIBase):
    task_id = "CI-05"
    fault_ids = ["PARTIAL_OUTPUT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "task_spec.json", {
            "semantic_effect": "file_created_with_content",
            "success_message": "Task completed successfully",
        })
        self._write_json(workspace_dir / "result.json", {
            "success_message": "Task completed successfully",
            "effect_produced": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        # result already has success string but no semantic effect
        pass

    def observe(self, workspace_dir: Path) -> dict:
        spec = self._read_json(workspace_dir / "task_spec.json")
        result = self._read_json(workspace_dir / "result.json")
        return {
            "semantic_effect": spec.get("semantic_effect"),
            "effect_produced": result.get("effect_produced", False),
            "has_success_string": bool(result.get("success_message")),
            "expected_effect": "semantic effect must be observable, not just a string",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["task_spec.json", "result.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CI06(_CIBase):
    task_id = "CI-06"
    fault_ids = ["PARTIAL_OUTPUT"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "criteria.json", {
            "acceptance_criteria": [
                {"id": "c1", "description": "File exists", "met": True},
                {"id": "c2", "description": "Tests pass", "met": True},
                {"id": "c3", "description": "No warnings", "met": False},
            ]
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        criteria = self._read_json(workspace_dir / "criteria.json")
        criteria["acceptance_criteria"][2]["met"] = False  # ensure c3 stays false
        self._write_json(workspace_dir / "criteria.json", criteria)

    def observe(self, workspace_dir: Path) -> dict:
        criteria = self._read_json(workspace_dir / "criteria.json")
        items = criteria.get("acceptance_criteria", [])
        all_met = all(c.get("met", False) for c in items)
        missing = [c["id"] for c in items if not c.get("met", False)]
        return {
            "criteria_total": len(items),
            "criteria_met": len(items) - len(missing),
            "criteria_missing": missing,
            "all_criteria_met": all_met,
            "expected_effect": "all criteria must pass",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "criteria.json").unlink(missing_ok=True)


class CI07(_CIBase):
    task_id = "CI-07"
    fault_ids = ["MALFORMED_RESPONSE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "evidence_sources.json", {
            "trusted": ["tool_output", "file_read"],
            "untrusted": ["agent_claims"],
        })
        self._write_json(workspace_dir / "evidence.json", {
            "source": "agent_claims",
            "content": "All tests passed",
            "trusted": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "evidence.json", {
            "source": "agent_claims",
            "content": "All tests passed",
            "trusted": False,
            "malformed": True,
        })

    def observe(self, workspace_dir: Path) -> dict:
        evidence = self._read_json(workspace_dir / "evidence.json")
        return {
            "evidence_source": evidence.get("source"),
            "is_trusted": evidence.get("trusted", False),
            "expected_effect": "only trusted evidence should satisfy validator",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["evidence_sources.json", "evidence.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CI08(_CIBase):
    task_id = "CI-08"
    fault_ids = ["MALFORMED_RESPONSE"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "validator_input.json", {
            "status": "valid",
            "evidence": {"type": "tool_output", "content": "tests passed"},
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "validator_input.json", {
            "status": "malformed",
            "evidence": {"type": "tool_output", "content": None, "error": "MALFORMED_RESPONSE"},
        })

    def observe(self, workspace_dir: Path) -> dict:
        inp = self._read_json(workspace_dir / "validator_input.json")
        return {
            "input_status": inp.get("status"),
            "is_malformed": inp.get("status") == "malformed",
            "expected_effect": "malformed evidence must be rejected (fail closed)",
        }

    def reset(self, workspace_dir: Path) -> None:
        (workspace_dir / "validator_input.json").unlink(missing_ok=True)


class CI09(_CIBase):
    task_id = "CI-09"
    fault_ids = ["FAIL_TOOL_ON_CALL_2"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "valid_artifact.json", {
            "status": "verified",
            "content": {"key": "value"},
            "hash": "valid_hash_123",
        })
        self._write_json(workspace_dir / "incomplete_artifact.json", {
            "status": "incomplete",
            "content": {},
            "hash": "",
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        # Simulate tool failure during repair that could overwrite valid artifact
        self._write_json(workspace_dir / "repair_error.json", {
            "error": "FAIL_TOOL_ON_CALL_2",
            "attempted_overwrite": "valid_artifact.json",
        })

    def observe(self, workspace_dir: Path) -> dict:
        valid = self._read_json(workspace_dir / "valid_artifact.json")
        incomplete = self._read_json(workspace_dir / "incomplete_artifact.json")
        error = self._read_json(workspace_dir / "repair_error.json")
        return {
            "valid_artifact_status": valid.get("status"),
            "incomplete_artifact_status": incomplete.get("status"),
            "repair_error": error.get("error"),
            "expected_effect": "valid artifact must be preserved, incomplete must be corrected",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["valid_artifact.json", "incomplete_artifact.json", "repair_error.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


class CI10(_CIBase):
    task_id = "CI-10"
    fault_ids = ["QUOTA_EXHAUSTED"]

    def setup(self, workspace_dir: Path) -> dict:
        self._write_json(workspace_dir / "provider_state.json", {
            "quota_remaining": 100,
            "status": "available",
        })
        self._write_json(workspace_dir / "required_effect.json", {
            "effect": "generate_report",
            "produced": False,
        })
        return {"files_created": self._list_files(workspace_dir)}

    def inject_fault(self, workspace_dir: Path, fault_id: str) -> None:
        self._write_json(workspace_dir / "provider_state.json", {
            "quota_remaining": 0,
            "status": "quota_exhausted",
        })

    def observe(self, workspace_dir: Path) -> dict:
        provider = self._read_json(workspace_dir / "provider_state.json")
        effect = self._read_json(workspace_dir / "required_effect.json")
        return {
            "quota_remaining": provider.get("quota_remaining"),
            "effect_produced": effect.get("produced", False),
            "expected_effect": "completion must not be claimed when quota prevents effect",
        }

    def reset(self, workspace_dir: Path) -> None:
        for f in ["provider_state.json", "required_effect.json"]:
            (workspace_dir / f).unlink(missing_ok=True)


CI_FIXTURES = [CI01, CI02, CI03, CI04, CI05, CI06, CI07, CI08, CI09, CI10]
