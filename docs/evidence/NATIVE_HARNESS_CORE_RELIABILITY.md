# Native Harness Core Reliability

Task: `ODYS-NATIVE-HARNESS-CORE-RELIABILITY-01`

Base milestone: `3aebd369f45e01a2374ce25e81ac621591ff8d8a`

This milestone establishes deterministic infrastructure. It did not run MiMo
or any other live model, and it makes no Hermes superiority claim.

Verification baseline and final result:

```text
BASELINE_TEST_COUNT=396
FINAL_TEST_COUNT=441
FULL_TEST_STATUS=PASS (441 passed in 146.60s)
```

## Architecture forensic audit: before

The pre-milestone `AgentKernel` was a protocol with a
`ScriptedAgentKernel` test/offline implementation and a
`WorkerAgentKernelAdapter`. It was not a production model/tool loop.

The real CLI call chain was:

```text
odys run
  -> ProductRuntime
  -> RecoveringOrchestrator               owns Task/Run/Attempt and outer retry
  -> InnerAgentExecutor
  -> OpenAIAgentsBackend
  -> agents.Runner.run                    owns model turn iteration
       -> SDK Agent / provider
       -> SDK FunctionTool dispatch       invokes Odys ToolRegistry adapters
       -> SDK context continuation
       -> SDK max-turn/termination
  -> RecoveringOrchestrator validator/outcome arbitration
```

Consequently, before this milestone:

```text
CURRENT_NATIVE_LOOP_OWNERSHIP=PROTOCOL_AND_SCRIPTED_FIXTURE_ONLY
EXTERNAL_RUNTIME_OWNS_TURN_LOOP=YES
EXTERNAL_RUNTIME_OWNS_TOOL_DISPATCH=YES
EXTERNAL_RUNTIME_OWNS_TERMINATION=YES
EXTERNAL_RUNTIME_OWNS_CONTEXT=YES
```

Odys already owned the durable outer lifecycle, staged workspace, validator,
failure classification, checkpoint, and cross-attempt recovery. It did not own
the model-to-tool-to-observation loop inside an attempt.

## Architecture after

The native production call chain is:

```text
odys run ... --kernel native
  -> ProductRuntime
  -> RecoveringOrchestrator
       -> durable Task / Run / Attempt / staged workspace
  -> NativeAgentExecutor
  -> NativeAgentKernel
       -> NativeContextAssembler
            -> canonical TaskGraph projection
            -> ExecutionSnapshot
            -> bounded recovery / validation / delivery evidence
            -> selected Session/Memory/Knowledge/Skill inputs
       -> ProviderAdapter.generate             one model API call only
       -> ModelResponseParser
       -> NativeToolDispatcher
            -> capability authorization
            -> durable ToolInvocation identity
            -> existing concrete ToolRegistry tool
            -> E7-A ToolAwareObserver
            -> bounded model observation
            -> EventStore + ExecutionSnapshot projection
       -> next Odys-owned model turn
       -> CompletionCandidate
       -> CompletionAuthority
            -> authoritative validator
            -> ACCEPTED or REJECTED
       -> accepted AgentResult only
  -> AcceptedCompletionValidator projection
  -> RecoveringOrchestrator may transition Task/Run to COMPLETED
```

The provider adapter receives bounded messages and tool schemas and returns one
response. It does not own lifecycle, tools, mutation, validation, retries,
recovery, checkpoints, TaskGraph changes, or termination. The real native
provider uses the OpenAI-compatible HTTP client directly; it does not import or
invoke `agents.Runner`.

Answer to the required architecture question:

> Can Odys execute a real model/task without delegating the agent loop to
> OpenAI Agents SDK or Hermes?

**YES.** Select `--kernel native` with a configured OpenAI-compatible provider.

The compatibility baseline remains available as `--kernel external`.

## Ownership map

| Concern | Canonical owner after this milestone |
| --- | --- |
| Model turn boundary | `NativeAgentKernel` |
| Provider HTTP request | `ProviderAdapter` |
| Model response interpretation | `ModelResponseParser` |
| Tool authorization and dispatch | `NativeToolDispatcher` + existing `ToolRegistry` |
| Tool recovery observations | E7-A `ToolAwareObserver`, restored only within the same Attempt |
| Task/Run/Attempt lifecycle | `RecoveringOrchestrator` and existing repositories |
| Current in-attempt execution state | `ExecutionSnapshotRepository` |
| Side-effect identity/reconciliation | `ToolInvocationRepository` + `NativeToolDispatcher` |
| Completion acceptance | `CompletionAuthority` + authoritative validator |
| Cross-attempt recovery | existing `ResumeDecisionService`, classifier, policy, checkpoint/context reconstruction |
| Plan and dependency graph | existing `Plan`/`PlanStep` repositories and `TaskGraphScheduler` |
| Replan requirement | native `ReplanSignal`; planner still owns replacement plan content |
| Child execution | child Task/Run/Attempt plus child kernel/orchestrator |
| Child delivery | `DelegationLifecycleRepository` + `DurableDeliveryService` |

There is no second native todo graph. `ExecutionSnapshot.taskgraph_position`,
`completed_nodes`, and `pending_nodes` are bounded projections of the existing
canonical `Plan`/`PlanStep` graph. `PlanExecutionService` can inject an
`AgentExecutor` and supplies the active canonical node to the native kernel.

## Persistence ownership

| Data | Canonical store | Duplication boundary |
| --- | --- | --- |
| Conversation history | SQLite `conversation_sessions` / `session_messages` through `SessionRepository` | Execution snapshots contain no full transcript; at most caller-selected bounded context |
| Execution lifecycle | SQLite Task/Run/Attempt, validation, failure, recovery, checkpoint, context-snapshot tables | EventStore is audit history, not the decision authority |
| Native current state | SQLite `native_execution_snapshots` (one latest row per Attempt) | Recent outcomes are capped; full history remains bounded events/invocations |
| Tool side effects | SQLite `native_tool_invocations` plus staged workspace bytes | Raw arguments are not stored; SHA-256 fingerprint and safe result projection only |
| Completion integrity | SQLite `completion_candidates` and `native_validation_failures` | Outer validator reads ACCEPTED authority; it cannot infer acceptance from final text |
| Long-term memory | `.odys/memory/MEMORY.md` and `USER.md` through `BuiltinMemoryProvider` | Not a conversation or execution store |
| Project knowledge | repository README/docs and explicit knowledge directories through `LocalKnowledgeProvider` | Search excerpts are selected into context; knowledge is not copied into Session |
| Delegation/delivery | existing `delegations` plus SQLite `delegation_lifecycle` | Child result and logical delivery token are durable and idempotent |

## Bounded context contract

`NativeContextAssembler` deterministically sorts sources by priority/name and
applies `AgentBudget.max_context_chars`. Inputs are goal, acceptance criteria,
active TaskGraph node, current ExecutionSnapshot, recent tool outcomes,
verification, validation failures, ReplanSignals, delivered child outcomes,
selected memory, selected knowledge, and skill instructions. Conversation input
is a low-priority bounded selection; it is not the execution-state authority.

No hidden chain of thought, credentials, raw API keys, raw tool argument JSON,
unbounded stdout, or unbounded file content is persisted. Tool output included
for the next model turn is redacted and structurally/size bounded.

## State machines

### Native execution termination

```text
CONTINUE
  -> WAITING_TOOL -> CONTINUE
  -> WAITING_CHILD -> CONTINUE
  -> CANDIDATE_COMPLETE
       -> ACCEPTED_COMPLETE
       -> RECOVERING -> CONTINUE
  -> REPLANNING
  -> FAILED
```

No-tools/no-claim is `MODEL_STOPPED_WITHOUT_COMPLETION`, not completion.
Turn, tool-call, attempt, and delegation budgets are harness-owned. Exhaustion
produces `BUDGET_EXHAUSTED` or `DELEGATION_BUDGET_EXHAUSTED`, durable evidence,
and a replan/recovery path rather than a success claim.

### Tool invocation and crash reconciliation

```text
REQUESTED -> STARTED -> FINISHED
     \          \
      \ crash    \ crash
       -> RECONCILED <-

REQUESTED before execution       -> SAFE_TO_RETRY (model must reissue; no auto replay)
STARTED read-only                 -> SAFE_TO_RETRY (no auto replay)
STARTED side effect + mutation    -> DO_NOT_RETRY
STARTED side effect, uncertain    -> RECONCILE_FIRST
```

This is conservative duplicate-side-effect prevention, not a claim of a
general exactly-once transaction protocol.

### Completion integrity

```text
RUNNING
  -> model/harness proposes CompletionCandidate
  -> CANDIDATE_COMPLETION
       -> authoritative validator PASS -> ACCEPTED
       -> authoritative validator FAIL -> REJECTED
            -> ValidationFailure
            -> ReplanSignal
            -> model recovery context

Only ACCEPTED -> Task/Run COMPLETED
```

Model final text, executor success, pytest PASS, and child success are evidence
or candidate sources; none is an independent completion authority.

### Delegation and delivery

```text
Delegation CREATED
  -> child Task + child Run + pending child Attempt durably linked
  -> DISPATCHED / child RUNNING
  -> child outcome:
       COMPLETED | FAILED | TIMEOUT | CRASHED | VALIDATION_REJECTED
  -> DELIVERY_PENDING
  -> DELIVERED
  -> CONSUMED
```

Execution owner, conversation owner, and delivery owner are explicit fields.
Delivery is an at-least-once durable attempt with a stable logical delivery
token and idempotent consume. It is not described as distributed exactly-once.

## Replan contract

`ReplanSignal` records `reason`, `scope`, canonical failed node, and bounded
evidence. Native sources include repeated tool failure, validator rejection,
verification regression, provider/malformed response, budget exhaustion, and
child failure. The harness owns when replanning is required. The existing
planner/TaskGraph layer owns what replacement plan to construct; this milestone
does not add a competing planner.

## Deterministic fault-injection matrix

| Case | Injected boundary | Required durable result |
| --- | --- | --- |
| Native R1 | after tool requested, before execution | Invocation reconciles `SAFE_TO_RETRY`; no automatic execution |
| Native R2 | after side effect executed, before observation | Workspace probe yields `DO_NOT_RETRY`; mutation occurs once |
| Native R3 | after invocation observation, before snapshot update | Finished invocation reconstructs missing snapshot projection |
| Native R4 | after CompletionCandidate persisted | Fresh kernel validates existing candidate once |
| Native R5 | after candidate validation persisted | Fresh kernel returns existing ACCEPTED result without revalidation |
| Native R6 | outer process stops with native Attempt RUNNING | Fresh `RecoveringOrchestrator` selects `RESUME_NATIVE_ATTEMPT` and re-enters the same Attempt |
| D1 | parent process stops while child RUNNING | Delegation, child Task/Run and parent Attempt relation remain queryable |
| D2 | child outcome persisted before delivery | Fresh delivery service advances `DELIVERY_PENDING -> DELIVERED` |
| D3 | delivery performed before local acknowledgement | Stable delivery token prevents duplicate logical delivery |
| D4 | child TIMEOUT after mutation | Partial artifact refs, mutation presence, verification, failure type and retryability persist |
| D5 | reviewer rejects child | `VALIDATION_REJECTED` reaches parent; parent is not completed |
| Lineage | missing/cyclic parent relation | Validation fails closed |

The adversarial suites also cover multiple tool rounds, failure feedback into
the next turn, malformed provider response, provider timeout, turn/tool budget,
false completion, pytest-pass/validator-fail, executor-success/validator-fail,
TaskGraph projection, nested provenance, and duplicate child events/delivery.

## Reliability benchmark foundation

`BenchmarkScenario`, `BenchmarkRunner`, `BenchmarkResult`, and immutable
`FairnessContract` are harness-neutral. Scenario families are:

1. `execution_state_recovery`
2. `completion_integrity`
3. `delegation_lifecycle`

Before a live comparison, one reviewer-approved record must freeze the same
model, provider, objective, fixture digest, initial repository digest, tool
capabilities, timeout, turn/API budget, validator, and fault boundary for both
`runner=hermes` and `runner=odys`. Harness internals may differ; acceptance
criteria may not. Placeholder model/provider/digests in the checked-in scenario
files deliberately prevent these deterministic contracts from being mistaken
for live evidence.

## External runtime compatibility paths

The following path still delegates its in-attempt loop to OpenAI Agents SDK:

```text
odys run ... --kernel external
  -> InnerAgentExecutor
  -> OpenAIAgentsBackend
  -> agents.Runner.run
```

The offline Agent Platform demonstration also retains scripted kernels. These
paths are baseline/compatibility paths and are excluded from native reliability
claims. Hermes is not imported or called anywhere in the implementation.

## Files introduced or modified

Production and contracts:

```text
src/lhas/native/__init__.py
src/lhas/native/models.py
src/lhas/native/provider.py
src/lhas/native/parser.py
src/lhas/native/context.py
src/lhas/native/persistence.py
src/lhas/native/tools.py
src/lhas/native/completion.py
src/lhas/native/kernel.py
src/lhas/native/executor.py
src/lhas/native/delegation.py
src/lhas/reliability/__init__.py
src/lhas/reliability/benchmark.py
src/lhas/agent/__init__.py
src/lhas/agent/models.py
src/lhas/agent/delegation.py
src/lhas/cli.py
src/lhas/cli_runtime.py
src/lhas/domain/enums.py
src/lhas/inner_agent/tool_adapter.py
src/lhas/orchestrator_v2.py
src/lhas/persistence/orm.py
src/lhas/planning/service.py
src/lhas/platform_models.py
src/lhas/resume.py
evals/reliability/README.md
evals/reliability/execution_state_recovery.json
evals/reliability/completion_integrity.json
evals/reliability/delegation_lifecycle.json
```

Adversarial tests:

```text
tests/native_kernel/test_native_loop.py
tests/native_kernel/test_native_cli.py
tests/native_kernel/test_taskgraph_native.py
tests/recovery/test_native_recovery.py
tests/completion_integrity/test_completion_authority.py
tests/delegation_lifecycle/test_durable_delivery.py
tests/test_reliability_benchmark_contract.py
```

## Known limitations

- No live model or Hermes comparison was run. Benchmark fixture/model/provider
  placeholders must be frozen and reviewed before the next stage.
- The native real-provider implementation currently targets OpenAI-compatible
  Chat Completions. The external compatibility path remains available for
  Responses API/provider-specific SDK behavior.
- Side-effect reconciliation is conservative. Uncertain started mutations are
  surfaced as `RECONCILE_FIRST`; they are never silently replayed.
- Durable delivery proves stable logical tokens and idempotent local consume,
  not distributed exactly-once semantics.
- `ReplanSignal` is the foundation contract. This milestone deliberately does
  not implement a large dynamic planner redesign.
- SQLite schema evolution adds new tables through `create_all`; no existing
  column is rewritten in this milestone.

## Required assertions

```text
NATIVE_KERNEL_EXISTS=YES
NATIVE_KERNEL_REAL_PROVIDER_PATH=YES
ODYS_OWNS_MODEL_TURN_BOUNDARY=YES
ODYS_OWNS_TOOL_DISPATCH=YES
ODYS_OWNS_OBSERVATION=YES
ODYS_OWNS_TERMINATION=YES
ODYS_OWNS_COMPLETION_ACCEPTANCE=YES
ODYS_OWNS_RECOVERY=YES
EXTERNAL_AGENT_SDK_REQUIRED_FOR_NATIVE_RUN=NO
E7A_IN_NATIVE_PATH=YES
EXECUTION_STATE_DURABLE=YES
RESUME_RECONCILES_SIDE_EFFECTS=YES
FALSE_COMPLETION_BYPASS_FOUND=NO
DURABLE_CHILD_TASK_RUN_ATTEMPT=YES
DURABLE_DELIVERY=YES
PARENT_CRASH_RECOVERY=PASS
CHILD_CRASH_RECOVERY=PASS
DELIVERY_CRASH_RECOVERY=PASS
BENCHMARK_EXECUTION_RECOVERY_READY=YES
BENCHMARK_COMPLETION_INTEGRITY_READY=YES
BENCHMARK_DELEGATION_READY=YES
HERMES_FILES_COPIED=0
```
