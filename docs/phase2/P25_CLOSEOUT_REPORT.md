# Phase 2 Closeout Report

**Schema:** `p25-closeout-v2`
**Base SHA:** `df00459`
**Tested HEAD:** `bea1bbb`
**Python:** `3.11.11`
**Platform:** `Windows-10-10.0.26200-SP0`
**Execution-derived:** `True`
**Manually synthesized:** `False`
**Reproducible:** `True`
**Real builtin backend path:** `YES`
**Real platform backend path:** `YES`
**Decision:** PASS

## Summary

| Metric | Count |
|---|---:|
| Scenario IDs | 7 |
| Execution records | 19 |
| PASS records | 19 |
| FAIL records | 0 |
| Contract accepted | 14 |
| Contract rejected | 5 |
| Backend executions | 13 |
| Typed evidence records | 13 |

## Execution Evidence

| Scenario | Seq | Capability | Definition source | Backend | Contract | Executed | Status | Error | Evidence capability | Evidence type | Evidence source | Result |
|---|---:|---|---|---|---|---|---|---|---|---|---|---|
| S1_BUILTIN_SUCCESS | 1 | workspace.list | odys-runtime | WorkspaceListTool | True | True | SUCCESS |  | workspace.list | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S1_BUILTIN_SUCCESS | 2 | workspace.read | odys-runtime | WorkspaceReadTool | True | True | SUCCESS |  | workspace.read | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S1_BUILTIN_SUCCESS | 3 | workspace.edit | odys-runtime | WorkspaceEditTool | True | True | SUCCESS |  | workspace.edit | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S1_BUILTIN_SUCCESS | 4 | workspace.diff | odys-runtime | WorkspaceDiffTool | True | True | SUCCESS |  | workspace.diff | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S1_BUILTIN_SUCCESS | 5 | test.run | odys-runtime | _RoutingCliBackend | True | True | SUCCESS |  | test.run | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S1_BUILTIN_SUCCESS | 6 | git.status | odys-runtime | _RoutingCliBackend | True | True | SUCCESS |  | git.status | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S1_BUILTIN_SUCCESS | 7 | git.diff | odys-runtime | _RoutingCliBackend | True | True | SUCCESS |  | git.diff | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S1_BUILTIN_SUCCESS | 8 | environment.inspect | odys-runtime | _RoutingCliBackend | True | True | SUCCESS |  | environment.inspect | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S2_INVALID_REQUEST_FAIL_CLOSED | 1 | workspace.read | odys-runtime | WorkspaceReadTool | False | False | FAILURE | INVALID_ARGUMENT |  |  |  | PASS |
| S2_INVALID_REQUEST_FAIL_CLOSED | 2 | test.run | odys-runtime | _RoutingCliBackend | False | False | FAILURE | INVALID_ARGUMENT |  |  |  | PASS |
| S2_INVALID_REQUEST_FAIL_CLOSED | 3 |  | unknown | none | False | False | FAILURE | INVALID_ARGUMENT |  |  |  | PASS |
| S2_INVALID_REQUEST_FAIL_CLOSED | 4 | git.status | odys-runtime | _RoutingCliBackend | False | False | FAILURE | INVALID_ARGUMENT |  |  |  | PASS |
| S3_MISSING_BACKEND | 1 | workspace.read | odys-runtime | none | False | False | FAILURE | CAPABILITY_UNAVAILABLE |  |  |  | PASS |
| S4_MCP_LOCAL | 1 | mcp.odys-fake.echo | mcp:odys-fake | MCPToolAdapter | True | True | SUCCESS |  | mcp.odys-fake.echo | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S5_SKILL_READINESS | 1 | workspace.read,mcp.odys-fake.echo | odys-runtime;mcp:odys-fake | none (readiness-only) | True | False | NOT_EXECUTED |  |  |  |  | PASS |
| S6_PLATFORM | 1 | platform.prepare | odys-runtime | _KernelTool | True | True | SUCCESS |  | platform.prepare | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S6_PLATFORM | 2 | platform.delegate | odys-runtime | _DelegationTool | True | True | SUCCESS |  | platform.delegate | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S6_PLATFORM | 3 | platform.finalize | odys-runtime | _KernelTool | True | True | SUCCESS |  | platform.finalize | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |
| S7_TOOL_SUCCESS_NOT_COMPLETION | 1 | workspace.read | odys-runtime | WorkspaceReadTool | True | True | SUCCESS |  | workspace.read | TOOL_EXECUTION | odys-tool-contract-v1 | PASS |

Artifact: `artifacts/phase2/p25-closeout.json`
