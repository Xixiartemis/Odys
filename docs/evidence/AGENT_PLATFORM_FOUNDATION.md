# Agent Platform Foundation — HV-1.5

Task: `ODYS-HERMES-PLATFORM-FOUNDATION-01`

Base Odys SHA: `882364db75842f7961f4d851153970252e01cec5`

Hermes research baseline:

- repository: `https://github.com/NousResearch/hermes-agent`
- commit: `3f315e46fede84ed4e6c8cfdbd00a13618e68986`
- license: MIT

## Status vocabulary

- **IMPLEMENTED**: production contract/module exists and is connected.
- **OFFLINE_DETERMINISTIC_VALIDATED**: exercised without network or model.
- **LIVE_MODEL_NOT_MEASURED**: no MiMo or other live provider was called.

## Implemented foundation

| Channel | Status | Evidence |
|---|---|---|
| AgentKernel + Worker adapter | IMPLEMENTED | shared run/cancel/status; legacy AgentExecutor adapter |
| Five AgentRole profiles | IMPLEMENTED | ROOT, PLANNER, WORKER, RESEARCHER, REVIEWER |
| Root deterministic routing | OFFLINE_DETERMINISTIC_VALIDATED | simple interaction and long-running goal routes |
| Planner→TaskGraph | OFFLINE_DETERMINISTIC_VALIDATED | schema-valid three-step SIMPLE_DEPENDENCY Plan |
| Durable delegation | OFFLINE_DETERMINISTIC_VALIDATED | Child Task, Run, Attempt, Validation and parent linkage persisted |
| Skills | OFFLINE_DETERMINISTIC_VALIDATED | metadata→SKILL.md→references progressive disclosure |
| Memory | OFFLINE_DETERMINISTIC_VALIDATED | bounds, approval, role denial, replace/remove |
| Session | OFFLINE_DETERMINISTIC_VALIDATED | SQLite lineage, append/read/scroll/list and FTS5 search |
| Knowledge | OFFLINE_DETERMINISTIC_VALIDATED | bounded lexical README/docs search and scoped open |
| Toolsets | OFFLINE_DETERMINISTIC_VALIDATED | six bundles, composition and child subset enforcement |
| MCP | OFFLINE_DETERMINISTIC_VALIDATED | real stdio JSON-RPC initialize/list/call; ToolRegistry bridge |
| ContextAssembler | OFFLINE_DETERMINISTIC_VALIDATED | explicit source selection, priority and hard budget |
| Project Context | OFFLINE_DETERMINISTIC_VALIDATED | AGENTS.md then higher-priority .odys.md |
| ProviderRegistry | IMPLEMENTED | provider/model/api mode/base URL/capabilities; no credentials |
| Validator authority | OFFLINE_DETERMINISTIC_VALIDATED | successful executor claim plus Validator FAIL cannot complete Task |
| Failure hierarchy | OFFLINE_DETERMINISTIC_VALIDATED | five levels, safe routing event, no Dynamic Replan |
| CLI/Agent Tree | OFFLINE_DETERMINISTIC_VALIDATED | chat/agents/skills/memory/mcp and persisted-event tree projection |

## Quantitative facts

| Metric | Value |
|---|---:|
| Repository tests | 367 passed |
| New tests from 321-test base | 46 |
| Agent roles | 5 |
| Toolsets | 6 |
| Bundled Odys skills | 2 |
| Memory scenarios | 3 |
| Session scenarios | 3 |
| Knowledge scenarios | 2 |
| Project Context scenarios | 1 |
| MCP stdio scenarios | 1 (discovery + call + registry bridge) |
| Durable delegation scenarios | 1 full lineage scenario plus 3 policy checks |
| Offline E2E routes | 2 (simple Root chat; full long-goal platform) |
| Hermes source files copied | 0 |
| Hermes source files modified | 0 |
| Hermes concepts reimplemented | 9 subsystems |
| Live model executions | 0 |

## Full offline vertical slice

The acceptance test runs:

1. User request→RootAgent→`LONG_RUNNING_GOAL`;
2. Memory read, AGENTS/.odys project context, and Skill Level-0 discovery;
3. ScriptedPlatformPlanner→three PlanSteps→existing TaskGraphScheduler;
4. each Step→durable Task→Run→Attempt through RecoveringOrchestrator;
5. delegation Step→Delegation row→Child Task→Child Run→Child Attempt;
6. Child fresh context→`coding/code-review` load→Knowledge lexical search→
   fake stdio MCP echo through ToolRegistry;
7. bounded child result→parent Worker;
8. Validator rows pass all child/parent Attempts;
9. Plan and Goal complete; Session, lineage and safe events remain queryable.

The test does not mock database success and does not write Run/Task terminal
state directly. It uses the normal repositories, PlanExecutionService,
RecoveringOrchestrator, Validator and EventStore.

## Safety evidence

- Child persisted context excludes the parent conversation transcript.
- Child default memory is read-only; writes are denied independently of an
  approval flag.
- Child Toolsets are checked as a subset of the parent profile.
- Tool lifecycle events omit arguments and unrestricted context.
- AgentResult and Session models reject hidden reasoning fields/roles.
- MCP is launched with explicit argv, no shell, no network, and a scrubbed
  environment; discovered tools enter the canonical ToolRegistry.
- ProviderProfile has no credential field.
- HV12/HV13 canonical JSON and claim files are not modified.

## Boundaries

This phase is **LIVE_MODEL_NOT_MEASURED**. It makes no claim about model
quality, success rate, token use, or latency. Dynamic Macro Replan is not
implemented: FailureRouter only records the PLAN/GOAL routing boundary. HTTP
MCP, vector retrieval, parallel agents, remote workers, messaging gateways,
cron, browser/dashboard UI and external exactly-once effects remain out of
scope.
