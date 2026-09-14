# Odys Infrastructure Parity Audit 01

Status: **READ-ONLY AUDIT / REQUEST REVIEW**
Audit baseline: `41e019a063dc1c6e1a80df1341dc6ad12fa649ae`
Audit date: 2026-09-14

## Decision first

`INFRA_PARITY_GATE_V1=REQUEST_REVIEW`.

The current Odys baseline is strong in the narrow area it claims: a native
model/tool loop with durable Task/Run/Attempt state, independent completion
acceptance, failure provenance, bounded local recovery, and benchmark identity
controls. The exact-SHA baseline suite was reported as `1201 passed`; this
audit did not rerun provider calls or change code.

It is not yet honest to call the system infrastructure-parity complete with
current Pi, Hermes, and OpenHands-style long-lived runtime substrates. The
highest gaps are:

1. cancellation and absolute-deadline propagation are not one uniformly
   enforced cross-layer contract;
2. generic tool evidence is durable, but a single universal receipt contract
   for every external side effect is not proven;
3. session/checkpoint/sandbox lifecycle is bounded and local, not a general
   provider/session/process restoration substrate;
4. concurrency and subagent execution exist in bounded Odys paths, but do not
   yet have the same general-purpose controls and isolation as the reference
   systems.

Therefore the next benchmark restart should remain gated until the run scope
explicitly excludes these gaps or the P0 items in the matrix are closed.

## Scope and ownership rule

The comparison is against current upstream source snapshots, not marketing
claims:

| Upstream | Repository state inspected | License basis |
|---|---|---|
| Pi successor of `badlogic/pi-mono` | `earendil-works/pi`, `71dca871bc80b6bc97be37f0ca3189399d651fff` | MIT repository license; verify notices before redistribution |
| Hermes | `NousResearch/hermes-agent`, `bf867d3c7451cbc849a56ed5b5f8222ec37909b9` | MIT as declared by the project |
| OpenHands frontend | `OpenHands/OpenHands`, `2c5ce2fa2dca3aa9c7442ff1c46876c60a794eeb` | frontend only; backend recommendations come from SDK |
| OpenHands SDK/server | `OpenHands/software-agent-sdk`, `57f5cc9f4a671fe290783551ba00d57efe2017c` | MIT |
| Sandbox server | `OpenHands/sandbox-server`, `f19f9e0d88272bb393e39e8cbcb78e3e8aa633a3` | inspect exact package/image terms before reuse |

Upstream code is reference material or an adapter source. It must not become
the owner of Odys Task/Run/Attempt identity, `VERIFIED` transitions,
CompletionAuthority, failure provenance, selective repair, or macro replan.
That boundary is already frozen by `docs/adr/0001-runtime-ownership.md`,
`docs/adr/0002-open-source-reuse-policy.md`, and
`docs/adr/0003-workflow-semantics.md`.

## Real Odys production call chain

The benchmark path is:

```text
p46_launcher
  -> Phase4 runner / external validator boundary
  -> P45BenchmarkExecutor
       -> fixture setup + deterministic fault injection
       -> RealLLMMinimalRuntimeFactory OR RealLLMOdysRuntimeFactory
       -> NativeAgentKernel.run
            -> NativeContextAssembler
            -> RealLLMProvider.generate
                 -> OpenAIChatProviderAdapter / OpenAI-compatible transport
            -> ModelResponseParser
            -> NativeToolDispatcher
                 -> CapabilityRegistry availability/binding
                 -> ToolContract validation
                 -> concrete ToolRegistry backend
                 -> ToolResult / ToolEvidence / EventStore
            -> CompletionAuthority for candidate claims
       -> P45 outcome + trace persistence
       -> shared external validator
       -> Odys recovery contract on rejection
            -> bounded repair/revalidation when the runtime proves it can
       -> aggregation / invalid-run accounting
```

The key ownership facts are visible in:

- `evals/reliability/p45_executor.py:232` (`P45BenchmarkExecutor`),
  `:347` (`execute`), `:653` (`recover_after_validation`), and `:746`
  (`_configure_runtime_deadlines`);
- `evals/reliability/p46_provider.py:110` (`RealLLMProvider`), `:425`
  (`create_real_provider`), and `:580` (`_build_real_odys_kernel`);
- `src/lhas/native/kernel.py:42` (`NativeAgentKernel`), `:110` (`run`),
  `:203` (one provider call), `:277` (tool dispatch), and `:307`
  (CompletionAuthority);
- `src/lhas/native/tools.py:126` (`NativeToolDispatcher`) and
  `docs/architecture/TOOL_CONTRACT_V1.md`;
- `src/lhas/orchestrator_v2.py:63` (`RecoveringOrchestrator`) for the outer
  durable lifecycle and `:152` (`resume_run`).

This is not the same design as Pi/Hermes/OpenHands. Their loops are useful
execution primitives; Odys must continue to wrap them, not delegate semantic
authority to them.

## Capability levels

- **L0** — absent or not proven in the current code/evidence.
- **L1** — a local primitive or partial path exists; no complete durable proof.
- **L2** — integrated bounded path with tests and explicit ownership.
- **L3** — authoritative, durable, adversarially tested path suitable for the
  stated Odys scope. L3 is not a claim of universal production maturity.

## Domain findings

### 1. Provider/model boundary — Odys L3 for the frozen benchmark path

`RealLLMProvider` checks credential presence, provider/model/endpoint,
parameters, response model identity, and persists secret-free identity. The
adapter performs one model call and does not own tools, retries, validation, or
workflow. This is a good boundary. The residual gap is generic provider
failover/session restoration outside the frozen benchmark profile.

Pi's `packages/ai/src/` and `packages/agent/src/agent.ts` are good candidates
for transport/model-type ideas; Hermes' provider resolver and OpenHands SDK's
`openhands/sdk/llm/` are adapter references only.

### 2. Native model loop/parser — Odys L3 for native loop semantics

`NativeAgentKernel` owns turn boundaries, parser invocation, bounded context,
tool turns, candidate completion, budget exhaustion, and durable snapshots.
`ModelResponseParser` rejects malformed responses. The current contract is
clearer than a black-box external executor for attribution.

Pi's `packages/agent/src/agent.ts` (`Agent`, `runWithLifecycle`,
`createContextSnapshot`, `prepareNextTurnWithContext`) demonstrates a useful
stateful loop primitive and awaited lifecycle listeners. Reuse only its
event/turn ideas through an adapter; do not replace `NativeAgentKernel`.

### 3. Tools/capability/MCP — Odys L2

The normal path is `CapabilityRegistry -> ToolContract -> ToolRegistry ->
Tool.execute`, with schema, binding, timeout, approval, and safe evidence
checks. `src/lhas/mcp/manager.py:24` and `:52` discover namespaced MCP tools;
`src/lhas/mcp/adapter.py:10` adapts them to the existing ToolRegistry. MCP
transport is real enough for bounded stdio use, but generic receipt, stream,
cancellation, and external side-effect reconciliation are not universal.

Hermes' `model_tools.py:handle_function_call` and registry dispatch are a good
error-wrapping/async-bridge reference. OpenHands SDK's `Tool`/event model and
client-tool acknowledgement boundary are useful adapter references. Neither
should introduce a second capability or completion authority.

### 4. Workspace/sandbox — Odys L2

Odys has staged workspace semantics, path and command policy, baseline/work
separation, safe CLI, and benchmark fixture isolation. `docs/evidence/
E6_WORKSPACE_FOUNDATION.md` explicitly records that baseline integrity is
validated on reopen and that general external side-effect exactly-once is not
claimed.

OpenHands SDK `openhands/sdk/workspace/workspace.py` separates local and remote
workspace implementations. Its `openhands-workspace` remote/cloud workspace
and `sandbox-server` provide a useful process/API boundary. Pi explicitly does
not provide a sandbox, so it is not a sandbox reference. Before adopting a
remote substrate, Odys must retain workspace identity, protected-path policy,
mutation evidence, and cleanup ownership.

### 5. Events/observability — Odys L2

Odys has durable domain events, native snapshots, invocation rows, trace JSONL,
and safe projections. `src/lhas/persistence/event_store.py:23` is a durable
append path, while `src/lhas/native/persistence.py` stores execution and tool
projections. The known limitation is that event history is audit history, not
always the sole state authority; every external side effect is not yet a
universal receipt-backed event contract.

Pi's `agent-session.ts` defines lifecycle events such as `agent_end`,
`agent_settled`, `tool_execution_start/end`, and compaction events. Hermes
uses hooks around `handle_function_call`; OpenHands SDK's `EventLog` persists
typed events with IDs, parent IDs, locking, and branch traversal. These are
strong references for event shape and append safety, not replacements for
Odys state authority.

### 6. Session persistence/resume — Odys L2, scoped

`RecoveringOrchestrator.resume_run()` restores outer state and workspace and
explicitly refuses to claim provider-internal conversation restoration.
`docs/evidence/E6_PROCESS_RESUME.md` records separate-process tests and the
known limitation. This is a truthful bounded contract, but not a general
multi-channel long-lived session service.

Hermes stores full sessions in SQLite with resume/search. OpenHands
`LocalConversation` and `ConversationState` restore typed event history and
agent/tool compatibility. Pi stores JSONL session trees with branch/fork/reload
operations. The best adoption candidate is a session-store interface plus
identity checks, not wholesale replacement of Odys attempts.

### 7. Checkpoint/restore — Odys L2, local isolated fixture scope

`src/lhas/checkpoint.py:78` (`CheckpointRepository`) and the E6 evidence prove
checkpoint/context reconstruction for tested local workspace crash points.
The documented limitation is no provider-internal conversation restoration and
no general side-effect replay.

Hermes' official checkpoint design uses a shared shadow Git store and takes
checkpoints before file/destructive terminal operations. This is a strong
candidate for an optional workspace checkpoint adapter, subject to license,
path policy, atomicity, and no replacement of Odys recovery decisions.

### 8. Cancellation — Odys L1

The public executor protocol exposes `cancel`, and native kernel state can be
marked cancelled (`src/lhas/native/kernel.py:334`), but the current evidence
does not prove that one cancellation signal propagates through provider HTTP,
tool subprocesses, MCP reads, workspace operations, recovery, and all child
work. This is a P0 gap for a general long-running benchmark; it may be scoped
out only for a tightly bounded single-process run with an explicit contract.

Pi passes `AbortSignal` through the model loop and tool execution. OpenHands
SDK exposes `CancellationToken` on `LocalConversation`; its tools can inspect
the token. These are the best primitive references.

### 9. Timeout/deadline — Odys L2 for current benchmark path

P45 carries a root task timeout and configures a 300-second provider ceiling;
the P411/V2 fix made the distinction explicit. Tool Contract clamps per-tool
timeouts, Safe CLI bounds subprocesses, and MCP bounds reads. The remaining
gap is proving one absolute deadline across every nested provider/tool/MCP/
recovery/child operation rather than independent local ceilings. Treat this as
P0 for claims requiring hard wall-clock bounds.

### 10. Context/compaction — Odys L2

`NativeContextAssembler` builds a bounded deterministic context from current
task graph, snapshot, tool outcomes, validation failures, replan signals,
memory/knowledge, and selected conversation context. `docs/05_CONTEXT_POLICY.md`
requires per-attempt snapshots and forbids unbounded history. Odys does not yet
provide a general automatic long-session compaction subsystem equivalent to
Pi/Hermes/OpenHands condensers.

Pi's `AgentSession` has explicit compaction and continuation events. Hermes
documents compression that preserves recent turns and tool pairs. OpenHands
SDK exposes condenser/context-management components. These are P1 references;
they must not rewrite authoritative attempt evidence.

### 11. Budget/cost — Odys L3 for benchmark identity/accounting

`RunBudgetLedger` in `p45_executor.py:47` shares one run budget across initial
and recovery phases; provider call records and invalid-run accounting are
identity-aware. Cost can remain `NOT_MEASURED` when provider usage is absent.
This is strong for frozen experiment accounting, but not a complete
multi-provider pricing/usage ledger.

Hermes exposes detailed token/cost usage files; Pi aggregates usage/cost in
session stats; OpenHands SDK persists conversation metrics. Their accounting
schemas are useful input references, not permission to alter frozen metrics.

### 12. Validation/completion — Odys L3

`CompletionAuthority` is the completion boundary. Candidate text, tool success,
pytest observations, and executor success remain non-authoritative until the
validator accepts. The P410/P411 contract separates validator execution from
acceptance and makes false completion observable. This is a core Odys
differentiator and must not be delegated.

OpenHands hooks/security analyzers, Hermes verification-gated completion
guidance, and Pi stop/settlement events can improve adapters, but none should
be treated as the Odys validator.

### 13. Failure/provenance — Odys L3

Odys has typed failure classification, durable failure reports, native
validation failures, replan signals, provider identity errors, and explicit
attempt IDs. `RecoveringOrchestrator` classifies persisted validation and
attempt failures before recovery. This is authoritative within current scope.

Hermes' error classifier/retry state and OpenHands' typed conversation error
events are useful normalization references. They do not replace Odys
`StepFailureProvenance` or alter its taxonomy.

### 14. Repair/recovery — Odys L3 for bounded P3/P4 scope

Odys retains original and repair attempts, shared root budgets, local repair
selection, external revalidation, and recovery lineage. P45 explicitly calls
the runtime recovery contract after external validation rejection. Macro replan
is separate from local repair. The evidence proves a bounded workflow, not a
general planner that can recover arbitrary external side effects.

OpenHands stop hooks/continuations, Hermes retry/compression, and Pi retry/
compaction are execution-loop mechanisms only. Adopting them wholesale would
create competing retry/recovery authorities and is rejected.

### 15. TaskGraph/planning/replan — Odys L3

The canonical `Plan`/`PlanStep` graph, dependency scheduler, semantic
fingerprints, `ReplanSignal`, and `MacroReplanService` preserve verified work
and invalidate affected descendants. The planner proposes graph content; Odys
acceptance and completion authority remain outside the planner.

Pi/Hermes/OpenHands are not equivalent typed workflow authorities. Their
queues, tasks, todo tools, or conversation branches must not be substituted for
Odys TaskGraph semantics.

### 16. Delegation/subagents — Odys L2

Odys has child Task/Run/Attempt identity, delegation lifecycle, durable delivery
tokens, and parent consumption. The documented delivery guarantee is
at-least-once/idempotent logical consumption, not distributed exactly-once.
The remaining gap is broad subagent runtime isolation and cancellation.

Hermes intercepts `delegate_task` in the agent loop; OpenHands SDK has file/
plugin/programmatic agent definitions and conversation subagents. These are
useful loading and lifecycle references, but parent/child completion remains
Odys-owned.

### 17. Concurrency — Odys L1

There are bounded locks, repository CAS, MCP request serialization, and
delivery idempotency. The current baseline does not prove a uniform scheduler
for parallel model tool calls, child budgets, cancellation, fair queuing, and
workspace isolation across arbitrary concurrent runs. This is P1 for a
parallel benchmark and not a reason to claim general concurrent-agent parity.

Pi exposes sequential/parallel tool-execution modes; Hermes uses worker-thread
bridging for async tools and has loop caps; OpenHands SDK documents parallel
tool execution and state locking. These are the best references for a future
Odys-owned scheduler adapter.

### 18. MCP/benchmark evaluation surface — Odys L2

Phase 4 protocol identity, manifest/fault/fixture/validator hashes, raw/trace/
invalid artifacts, resume identity checks, and aggregation accounting are
strongly frozen. The benchmark runner is not the runtime authority, and it
does not generate claims from missing data. The open gap is broad evaluation
orchestration: multi-process cancellation, remote sandbox provenance, and
complete side-effect receipts must be settled before interpreting long-horizon
results.

OpenHands SDK provides typed MCP config and event-driven conversation/server
surfaces; its agent server exposes REST/WebSocket conversation events and
workspace layout. Hermes exposes batch trajectory generation and usage files;
Pi exposes JSONL/RPC event modes. These are integration references only; no
upstream benchmark number or protocol is imported.

## Priority gaps

### P0 — close before claiming general infrastructure parity

| Gap | Why it matters | Minimum closure |
|---|---|---|
| Absolute deadline propagation | A provider/tool/MCP/recovery child can outlive the root unless every layer consumes one deadline | one cancel/deadline token from root through provider, ToolRequest, Safe CLI, MCP, recovery, and child execution; adversarial timeout tests |
| Cancellation propagation | `cancel()` state alone is not proof that active transport/process work stops | end-to-end cancellation tests with no post-cancel tool/provider mutation and durable terminal event |
| Universal external-side-effect receipt boundary | Current exactly-once claims are intentionally limited to local fixture semantics | every side-effect-capable adapter must return stable operation ID/receipt or be explicitly excluded from official scope |

### P1 — close before long-horizon/parallel benchmark expansion

- durable provider/session continuation identity beyond outer-run resume;
- generic sandbox/process lifecycle and cleanup across remote workers;
- concurrency scheduler with per-run budgets, fairness, cancellation, and
  workspace isolation;
- context compaction with explicit provenance for summarized vs retained
  evidence;
- complete usage/cost normalization when provider usage is available.

### P2 — quality and adoption improvements

- OpenTelemetry exporter behind the existing EventStore boundary;
- optional Pi/Hermes/OpenHands adapters for comparative experiments;
- richer trace viewers and replay tooling;
- license/notice inventory for any redistributed upstream component.

## P0-A implementation evidence

The P0-A implementation is now present on top of the frozen audit-doc commit
`1724b4aca55d81de1abe5254e35f8b90772f682a`. Odys owns one
`ExecutionControlToken` for the root run; it carries the monotonic absolute
deadline, cancellation state/reason/timestamp, and optional attempt/parent
lineage. Component values are local ceilings and are clamped to root
remaining time.

The token is propagated through the audited bounded path:

`Phase4Runner` → `P45BenchmarkExecutor` → runtime factory → native/provider
and tool dispatch → Safe CLI/process and MCP transport → recovery and child
execution. Terminal control checks reject late provider/child results and
prevent post-cancel workflow advancement. Durable cancellation/deadline events
retain run/attempt, reason, source, timestamp, deadline, and parent lineage.

Offline adversarial coverage is `14 passed`, including provider, tool,
Windows Safe CLI process termination, MCP, recovery, child propagation,
local-ceiling-versus-root precedence, idempotent cancellation, and EventStore
reopen evidence. The affected regression set is `162 passed`; the final
project suite is `1215 passed` under the repository pytest launcher. No real
provider was executed.

`IMPLEMENTATION_SHA` is the single implementation commit reported as
`NEW_EXECUTION_SHA` in the closeout. The implementation tree intentionally
does not duplicate a self-referential commit hash inside its own evidence.

P0-A is closed for the local/bounded execution path covered above. Remote
worker cancellation, universal external-side-effect receipts, and a general
parallel scheduler remain outside this gate. The latter receipt boundary is
P0-B and remains open; no P0-B claim is made here.

## Benchmark restart rule

`BENCHMARK_RESTART_ALLOWED=NO` for an unrestricted long-horizon or parallel
benchmark. A narrowly scoped single-process benchmark may be authorized only
if its protocol explicitly declares the P0 exclusions, proves root deadline and
cancellation behavior for the included components, and treats unsupported
external side effects as `NOT_MEASURED` rather than silently successful.

## Count conventions for the gate

The counts below are audit-level gap records, not a count of every sentence in
the domain findings. P0 contains three independent blockers: root deadline
propagation, cancellation propagation, and the universal external-side-effect
receipt boundary. P1 contains session/continuation identity, sandbox/process
lifecycle, context compaction, bounded concurrency, and usage normalization.
P2 contains observability export, optional foreign-runtime adapters, richer
trace tooling, and the license/notice inventory. A domain is counted at its
lowest demonstrated level: the concurrency domain is therefore L1, not L2.

The recommendation counts describe the adoption plan: no direct upstream
dependency is accepted at this gate; five thin adapters are proposed; two
items require Odys-specific construction; and three semantic domains remain
frozen under Odys ownership.

## Source anchors

1. [Odys runtime ownership ADR](../adr/0001-runtime-ownership.md)
2. [Odys reuse policy ADR](../adr/0002-open-source-reuse-policy.md)
3. [Odys workflow semantics ADR](../adr/0003-workflow-semantics.md)
4. [Odys native reliability evidence](../evidence/NATIVE_HARNESS_CORE_RELIABILITY.md)
5. [Odys process-resume evidence](../evidence/E6_PROCESS_RESUME.md)
6. [Odys Tool Contract V1](../architecture/TOOL_CONTRACT_V1.md)
7. [Pi current repository](https://github.com/earendil-works/pi/tree/71dca871bc80b6bc97be37f0ca3189399d651fff)
8. [Pi Agent source](https://github.com/earendil-works/pi/blob/71dca871bc80b6bc97be37f0ca3189399d651fff/packages/agent/src/agent.ts)
9. [Pi AgentSession source](https://github.com/earendil-works/pi/blob/71dca871bc80b6bc97be37f0ca3189399d651fff/packages/coding-agent/src/core/agent-session.ts)
10. [Hermes current repository](https://github.com/NousResearch/hermes-agent/tree/bf867d3c7451cbc849a56ed5b5f8222ec37909b9)
11. [Hermes tools runtime](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/tools-runtime.md)
12. [Hermes sessions](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md)
13. [Hermes checkpoints](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/checkpoints-and-rollback.md)
14. [OpenHands frontend repository boundary](https://github.com/OpenHands/OpenHands/blob/main/AGENTS.md)
15. [OpenHands SDK repository](https://github.com/OpenHands/software-agent-sdk/tree/57f5cc9f4a671fe290783551ba00d57efe2017c)
16. [OpenHands SDK Agent architecture](https://docs.openhands.dev/sdk/arch/agent)
17. [OpenHands SDK LocalConversation source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)
18. [OpenHands SDK EventLog source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/event_store.py)
19. [OpenHands SDK Workspace factory](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/workspace/workspace.py)
20. [OpenHands Agent Server API/runtime layout](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-agent-server/openhands/agent_server/README.md)
21. [OpenHands sandbox-server](https://github.com/OpenHands/sandbox-server/blob/f19f9e0d88272bb393e39e8cbcb78e3e8aa633a3/README.md)

## Final return

```text
AUDIT_BASELINE_SHA=41e019a063dc1c6e1a80df1341dc6ad12fa649ae

BASELINE_SHA=41e019a063dc1c6e1a80df1341dc6ad12fa649ae
PI_UPSTREAM_SHA=71dca871bc80b6bc97be37f0ca3189399d651fff
HERMES_UPSTREAM_SHA=bf867d3c7451cbc849a56ed5b5f8222ec37909b9
OPENHANDS_UPSTREAM_SHA=2c5ce2fa2dca3aa9c7442ff1c46876c60a794eeb
OPENHANDS_SDK_SHA=57f5cc9f4a671fe290783551ba00d57efe2017c

P0_BLOCKER_COUNT=3
P1_COUNT=5
P2_COUNT=4

ODYS_L0_COUNT=0
ODYS_L1_COUNT=2
ODYS_L2_COUNT=9
ODYS_L3_COUNT=7

STRONGEST_ODYS_INFRA=CompletionAuthority plus typed failure/provenance and bounded recovery lineage
WEAKEST_ODYS_INFRA=Cross-layer cancellation propagation

BEST_PROVIDER_REFERENCE=Pi@71dca871/packages/agent/src/agent.ts plus Hermes@bf867d3/agent/conversation_loop.py
BEST_TOOL_REFERENCE=OpenHands SDK@57f5cc9/openhands-sdk/openhands/sdk/conversation/event_store.py plus Hermes@bf867d3/agent/model_tools.py
BEST_WORKSPACE_REFERENCE=OpenHands SDK@57f5cc9/openhands-sdk/openhands/sdk/workspace/workspace.py
BEST_EVENT_REFERENCE=OpenHands SDK@57f5cc9/openhands-sdk/openhands/sdk/conversation/event_store.py
BEST_SESSION_REFERENCE=Hermes@bf867d3 SQLite session state plus OpenHands SDK@57f5cc9 LocalConversation
BEST_CHECKPOINT_PRIMITIVE_REFERENCE=Hermes@bf867d3 shadow-Git checkpoint store
BEST_MCP_REFERENCE=Hermes@bf867d3 model_tools.py registry/async bridge plus official MCP wire semantics
BEST_CANCELLATION_REFERENCE=Pi@71dca871 packages/agent/src/agent.ts AbortSignal
BEST_TIMEOUT_REFERENCE=Hermes@bf867d3 provider/terminal timeout layering
BEST_SANDBOX_REFERENCE=OpenHands sandbox-server@f19f9e0d88272bb393e39e8cbcb78e3e8aa633a3
BEST_COMPACTION_REFERENCE=Pi@71dca871 packages/coding-agent/src/core/agent-session.ts

RECOMMENDED_REUSE_COUNT=0
RECOMMENDED_ADAPTER_COUNT=5
RECOMMENDED_ODYS_BUILD_COUNT=2
FREEZE_COUNT=3

TOP_5_P0_GAPS=1) root absolute deadline propagation; 2) cancellation propagation; 3) universal side-effect receipts; 4) provider/session continuation identity; 5) sandbox/process lifecycle

INFRA_PARITY_GATE_READY=NO
BENCHMARK_RESTART_ALLOWED=NO
LONG_HORIZON_BENCHMARK_ALLOWED=NO

FULL_SUITE_BASELINE=1201 passed
SOURCE_CODE_CHANGED=NO
REAL_PROVIDER_EXECUTED=NO
SOURCE_MODIFIED=NO
IMPLEMENTATION_COMMIT_CREATED=NO
INFRA_PARITY_GATE_V1=REQUEST_REVIEW
BENCHMARK_RESTART_ALLOWED=NO (unrestricted long-horizon/parallel scope)

P0A_IMPLEMENTATION_BASE_SHA=1724b4aca55d81de1abe5254e35f8b90772f682a
P0A_ADVERSARIAL_TESTS=14 passed
P0A_AFFECTED_REGRESSION_TESTS=162 passed
P0A_FINAL_FULL_SUITE=1215 passed
P0A_STATUS=CLOSED_FOR_LOCAL_BOUNDED_SCOPE
P0B_STATUS=OPEN
```
