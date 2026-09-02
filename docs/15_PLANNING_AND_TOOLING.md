# Planner and Tool Foundation (Phase D)

Status: **Historical / Superseded as product direction**

Canonical architecture: `docs/ARCHITECTURE_FREEZE.md`

Superseded by: `docs/01_ARCHITECTURE.md` and `docs/PRODUCT_DIRECTION.md`

This file remains the implementation record for its milestone. Current ownership distinguishes Odys-owned macro planning from agent-owned micro-planning.

Phase D adds provider-neutral `Goal -> Plan -> PlanStep -> Capability -> Tool` contracts. `DeterministicPlanner` only constructs an allow-listed linear plan; it never executes tools or decides completion. `ToolRegistry` resolves explicit capabilities and rejects unknown names. `PlanExecutionService` persists Goal/Plan/PlanStep, bridges each step to the existing Task/Run/Attempt runtime, and records replayable tool request/result/error/usage payloads in events.

Each completed step persists its output and passes it forward as `Step Output -> Execution Context -> Next Step`; declared `PlanStep.inputs` remain unchanged. Canonical runtime context is `steps[step_id]` with capability, output, artifacts, and usage. Runtime metadata reads `lhas.HARNESS_VERSION` (currently HV-0.4). A gated step can resume only after an explicit step-scoped approval and keeps the same Plan id.

Phase D2 adds explicit-live, provider-neutral `document.resume.read`, `web.search`, and bounded `web.fetch` adapters plus job parsing/ranking/artifact adapters. No network call occurs without `--live`; SSRF, content-type, size, timeout, and HTTP failure guards are enforced. This changes Tool Policy, so the harness is HV-0.5.

Phase E1 adds deterministic serial `SIMPLE_DEPENDENCY` TaskGraph scheduling with READY/BLOCKED states and dependency-closure context isolation. No dynamic replan or parallel execution is included. This orchestration policy is HV-0.6.

Capabilities may require human approval. Such steps become `WAITING_FOR_HUMAN_APPROVAL` and no tool call is made. Fake tools are offline test doubles; no web, browser, shell, MCP, real LLM planner, DAG concurrency, or multi-agent behavior is part of this phase.
