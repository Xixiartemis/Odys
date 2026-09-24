"""Offline Grader Firewall.

Separates runtime execution from evaluation.  The runtime-accessible
layer must not read/import:
  - ToolMaze oracle solution DAG
  - hidden perturbation ground truth
  - benchmark judge results
  - ToolSandbox target milestones
  - Terminal-Bench hidden/final test result before termination

Offline evaluator runs only after runtime termination.
Automated firewall audit verifies these invariants.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from .types import AuditReport


class FirewallViolation(RuntimeError):
    """Raised when the firewall detects a violation."""


class OfflineGraderFirewall:
    """Enforces separation between runtime and evaluation.

    The firewall operates at two levels:
    1. Module-level: checks that runtime code does not import hidden-state modules
    2. API-level: verifies that offline evaluation runs only after runtime termination
    """

    # Symbols that runtime must never access
    _PROHIBITED_IMPORTS = frozenset({
        "oracle_solution",
        "oracle_path",
        "hidden_perturbation",
        "ground_truth_labels",
        "judge_results",
        "target_milestones",
        "hidden_test_result",
    })

    # Attributes on adapters that are offline-only
    _OFFLINE_ONLY_METHODS = frozenset({
        "_get_oracle_solution",
        "_get_perturbation_label",
        "offline_native_evaluate",
        "get_target_milestones",
        "derive_progress_metrics",
    })

    def __init__(self):
        self._runtime_active = False
        self._runtime_terminated = False
        self._audit_log: list[dict[str, Any]] = []

    def begin_runtime(self) -> None:
        """Mark the start of a runtime phase."""
        if self._runtime_active:
            raise FirewallViolation("runtime already active — cannot begin again")
        self._runtime_active = True
        self._runtime_terminated = False
        self._audit_log.append({"event": "runtime_started"})

    def end_runtime(self) -> None:
        """Mark the end of the runtime phase.  Evaluation may now proceed."""
        if not self._runtime_active:
            raise FirewallViolation("runtime not active — cannot end")
        self._runtime_active = False
        self._runtime_terminated = True
        self._audit_log.append({"event": "runtime_ended"})

    def check_runtime_access(self, method_name: str) -> None:
        """Verify that a method is callable during runtime phase.

        Raises FirewallViolation if the method is offline-only.
        """
        if method_name in self._OFFLINE_ONLY_METHODS:
            raise FirewallViolation(
                f"method '{method_name}' is offline-only — "
                f"not callable during runtime phase"
            )

    def check_offline_access(self, method_name: str) -> None:
        """Verify that an offline-only method is callable after runtime.

        Raises FirewallViolation if runtime is still active.
        """
        if self._runtime_active and method_name in self._OFFLINE_ONLY_METHODS:
            raise FirewallViolation(
                f"method '{method_name}' requires runtime termination — "
                f"runtime is still active"
            )

    def audit_module_access(self) -> dict[str, str]:
        """Audit that no prohibited symbols are accessible from runtime context.

        Returns a dict of audit results.
        """
        results: dict[str, str] = {}

        # Check that the runtime-visible modules don't export prohibited symbols
        runtime_modules = [
            "lhas.phase5.types",
            "lhas.phase5.shadow_observer",
            "lhas.phase5.control_arms",
        ]

        for mod_name in runtime_modules:
            try:
                mod = importlib.import_module(mod_name)
                for prohibited in self._PROHIBITED_IMPORTS:
                    if hasattr(mod, prohibited):
                        results[f"{mod_name}.{prohibited}"] = "VIOLATION"
                    else:
                        results[f"{mod_name}.{prohibited}"] = "OK"
            except ImportError:
                results[mod_name] = "MODULE_NOT_FOUND"

        return results

    def audit_label_leakage(
        self,
        runtime_artifact: dict[str, Any],
    ) -> dict[str, str]:
        """Audit that runtime artifacts contain no hidden labels.

        Returns dict of leakage findings.
        """
        findings: dict[str, str] = {}
        prohibited_keys = {
            "oracle_solution", "oracle_path", "hidden_perturbation",
            "ground_truth", "judge_result", "target_milestones",
            "hidden_test_result", "perturbation_label",
        }

        def _check_dict(d: dict[str, Any], path: str = "") -> None:
            for key, value in d.items():
                full_key = f"{path}.{key}" if path else key
                if key.lower() in prohibited_keys or key in prohibited_keys:
                    findings[full_key] = "LEAKAGE_DETECTED"
                elif isinstance(value, dict):
                    _check_dict(value, full_key)

        _check_dict(runtime_artifact)
        if not findings:
            findings["overall"] = "NO_LEAKAGE"
        return findings

    def generate_audit_report(self) -> AuditReport:
        """Generate a comprehensive firewall audit report."""
        module_audit = self.audit_module_access()

        violations = [k for k, v in module_audit.items() if v == "VIOLATION"]
        runtime_hidden = "NO" if not violations else "YES"

        return AuditReport(
            firewall={
                "RUNTIME_HIDDEN_GROUND_TRUTH_ACCESS": runtime_hidden,
                "RUNTIME_ORACLE_ACCESS": runtime_hidden,
                "RUNTIME_FINAL_JUDGE_ACCESS": runtime_hidden,
                "runtime_active": str(self._runtime_active),
                "runtime_terminated": str(self._runtime_terminated),
                "violation_count": str(len(violations)),
            },
            pairing={},
            accounting={
                "audit_events": len(self._audit_log),
            },
            exclusions=[],
        )

    def fail_closed(self, report: AuditReport) -> None:
        """Fail closed if any firewall violation is detected.

        Raises FirewallViolation if any access check failed.
        """
        violations = [
            k for k, v in report.firewall.items()
            if v == "YES"
        ]
        if violations:
            raise FirewallViolation(
                f"firewall violation detected: {violations}"
            )
