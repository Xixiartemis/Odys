# Odys Infrastructure Capability Matrix

Baseline: `41e019a063dc1c6e1a80df1341dc6ad12fa649ae`
Levels: L0 absent/unproven; L1 primitive/partial; L2 integrated bounded path;
L3 authoritative durable path for the stated scope.

| DOMAIN | ODYS_LEVEL | ODYS_EVIDENCE | PI_LEVEL | HERMES_LEVEL | OPENHANDS_LEVEL | BEST_REFERENCE | DECISION | GAP_PRIORITY | SEMANTIC_LOCK | UPSTREAM_SHA | NOTES |
|---|---:|---|---:|---:|---:|---|---|---|---|---|---|
| Provider/model boundary | L3 | `p46_provider.py:110`, identity/credential/endpoint checks | L3 | L2 | L3 | Pi `packages/ai`; OH SDK `llm/` | adapter only | P1 | NO | Pi `71dca871`; OH SDK `57f5cc9` | preserve Odys identity proof |
| Native loop/parser | L3 | `native/kernel.py:42`, parser, turn snapshots | L3 | L3 | L3 | Pi `packages/agent/src/agent.ts` | retain Odys owner | NONE | YES | Pi `71dca871` | do not import foreign termination authority |
| Tools/capability/MCP | L2 | `native/tools.py:126`; `ToolContract`; `mcp/manager.py:24` | L2 | L3 | L3 | Hermes `model_tools.py`; OH SDK `tool/` | thin adapters | P1 | NO | Hermes `bf867d3`; OH SDK `57f5cc9` | MCP transport is not completion authority |
| Workspace/sandbox | L2 | staged workspace, Safe CLI, E6 foundation | L0 | L2 | L3 | OH SDK workspace + sandbox-server | isolate behind interface | P1 | NO | OH sandbox `f19f9e0` | Pi intentionally has no sandbox |
| Events/observability | L2 | `EventStore`, native snapshots/invocations, JSONL traces | L3 | L2 | L3 | OH SDK `EventLog`; Pi session events | adopt shapes/exporters | P1 | NO | OH SDK `57f5cc9`; Pi `71dca871` | EventStore remains state-adjacent audit truth |
| Session persistence/resume | L2 | `RecoveringOrchestrator.resume_run`; E6 tests | L3 | L3 | L3 | OH `LocalConversation`; Hermes SQLite | add identity-aware session adapter | P1 | NO | OH SDK `57f5cc9`; Hermes `bf867d3` | no provider-internal restore claim |
| Checkpoint/restore | L2 | `checkpoint.py:78`; CP-3 reconstruction | L2 | L3 | L2 | Hermes shadow Git checkpoints | optional workspace adapter | P1 | NO | Hermes `bf867d3` | local fixture scope only |
| Cancellation | L1 | executor `cancel`; native status marker | L3 | L2 | L3 | Pi AbortSignal; OH CancellationToken | implement root propagation | P0 | NO | Pi `71dca871`; OH SDK `57f5cc9` | active provider/tool/process stop unproven globally |
| Timeout/deadline | L2 | P45 root timeout + 300s provider ceiling; tool/MCP limits | L2 | L2 | L3 | OH conversation/run limits; Pi AbortSignal | unify absolute deadline | P0 | NO | OH SDK `57f5cc9`; Pi `71dca871` | local ceilings are not one root deadline |
| Context/compaction | L2 | `NativeContextAssembler`; CP policies; bounded context | L3 | L3 | L3 | Pi AgentSession; Hermes compression; OH condensers | add provenance-preserving compaction | P1 | NO | Pi `71dca871`; Hermes `bf867d3`; OH SDK `57f5cc9` | no unbounded transcript persistence |
| Budget/cost | L3 | `RunBudgetLedger`; provider accounting; NOT_MEASURED | L3 | L3 | L3 | Hermes usage file; Pi stats; OH metrics | retain, normalize optional usage | NONE | YES | all three | frozen benchmark accounting is strong |
| Validation/completion | L3 | `CompletionAuthority`; external validator; P410 semantics | L2 | L2 | L2 | no direct substitute | Odys-owned | NONE | YES | local only | upstream verification is not Odys acceptance |
| Failure/provenance | L3 | failure reports, validation failures, attempt IDs, signals | L2 | L2 | L3 | OH typed errors; Hermes classifier | retain taxonomy | NONE | YES | Hermes `bf867d3`; OH SDK `57f5cc9` | provenance must survive adapters |
| Repair/recovery | L3 | P45 recovery contract, shared budget, lineage, revalidation | L2 | L2 | L2 | no direct substitute | Odys-owned | NONE | YES | local only | a future side-effect receipt gap is tracked as P0-B |
| TaskGraph/planning/replan | L3 | Plan/PlanStep, scheduler, MacroReplanService | L1 | L1 | L1 | no direct substitute | Odys-owned | NONE | YES | local only | foreign task queues are not canonical graph |
| Delegation/subagents | L2 | child Task/Run/Attempt, delivery token/service | L2 | L3 | L3 | Hermes delegation; OH subagent registry | adapter/reference | P1 | NO | Hermes `bf867d3`; OH SDK `57f5cc9` | at-least-once delivery, not distributed exactly-once |
| Concurrency | L1 | locks, CAS, MCP serialization, delivery idempotency | L2 | L3 | L3 | Pi parallel tools; Hermes loop caps; OH parallel tools | add Odys scheduler | P1 | NO | Pi `71dca871`; Hermes `bf867d3`; OH SDK `57f5cc9` | contains bounded L2 primitives, but general concurrency scheduler contract remains L1 |
| MCP/benchmark evaluation | L2 | frozen identity, runner, raw/trace/invalid, resume accounting | L2 | L3 | L3 | OH MCP/event server; Hermes trajectory/usage; Pi JSONL/RPC | preserve protocol; add adapters | P1 | NO | all three; OH sandbox `f19f9e0` | no upstream numbers imported |

## Ownership boundary

The following remain Odys-only: Task/Run/Attempt identity, verified workflow
transitions, CompletionAuthority, validator truth, failure provenance, repair
scope, recovery lineage, macro replan, and benchmark protocol identity.

The following may be reused behind adapters: provider transports, typed event
serialization, session storage primitives, checkpoint storage, sandbox/process
isolation, MCP wire behavior, OpenTelemetry export, and bounded concurrency
primitives. Every adapter must preserve Odys IDs, deadlines, cancellation,
tool policy, workspace identity, and durable evidence.

## Gate interpretation

The matrix is not a claim that upstream systems are “better overall.” It
identifies where they provide mature substrate primitives and where their
semantic ownership conflicts with Odys. `GAP_PRIORITY` classifies missing
infrastructure contracts; `SEMANTIC_LOCK=YES` records an Odys-owned authority
boundary and is not itself a blocker. The independent P0 count is exactly
three: absolute deadline propagation, cancellation propagation, and the
universal external-side-effect receipt boundary.
