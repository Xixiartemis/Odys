# Phase5 Research Substrate Design

## Purpose

Minimum Task State / Evidence / Evaluation substrate required to test
the Odys Recovery Control Policy rigorously.

**NOT** a general agent framework.  **NOT** a multi-agent system.
**NOT** a replacement for Phase4 runtime semantics.

## Source-of-Truth Hierarchy

```
1. Environment observation
2. Append-only execution evidence
3. Validator feedback
4. Verified state commit
5. Materialized verified task state
```

Each level can only be established by the level below it being verified.

## Key Invariants

### UNVERIFIED_AGENT_CLAIM_CANNOT_MUTATE_VERIFIED_STATE

Model claims (text output, reasoning, assertions) are NOT verified facts.
Only validator-accepted evidence backed by a StateCommit can advance
VerifiedTaskState.

### CONTROL_STATE_CANNOT_BE_USED_AS_VERIFIED_ENVIRONMENT_STATE

ControlState (turn index, remaining budget, execution status) is
transient runtime state.  It is NOT environment truth and cannot be
treated as a verified fact.

### Append-Only Evidence

EvidenceEvent records are immutable once created (Pydantic frozen=True).
Monotonic sequence per run.  No update/delete path.

### StateCommitter Is Sole Promotion Authority

Only `TaskStateReducer.commit()` with a SUCCESS + ACCEPT
ValidatorFeedback can advance VerifiedTaskState.

### Verified Work Preservation

Accepted work survives later failures.  If step A is committed and
step B fails, A remains in VerifiedTaskState.

## Source-of-Truth Statements

- Agent claim ≠ evidence
- Evidence ≠ verified progress
- Tool success ≠ task progress
- Validator feedback ≠ state mutation
- Benchmark score ≠ runtime authority

## Architecture

```
EvidenceLedger (append-only)
    ↓
ValidatorFeedback (ACCEPT/REJECT/INDETERMINATE)
    ↓
TaskStateReducer.commit() — sole promotion authority
    ↓
StateCommit (immutable record)
    ↓
VerifiedTaskState (materialized view)
```

## Runtime Validator vs Benchmark Grader

| Aspect | Runtime Validator | Benchmark Grader |
|--------|------------------|-----------------|
| Runs when | During runtime | After termination |
| Produces | ValidatorFeedback | BenchmarkOutcome |
| Can trigger StateCommit | Yes | No |
| Can mutate TaskState | Indirectly via reducer | Never |
| Can trigger recovery | Yes | No |

## Type/API Firewall

`BenchmarkOutcome` is a frozen Pydantic model with no reference to
`VerifiedTaskState`, `ControlState`, or `TaskStateReducer`.

`OfflineGrader` has no reference to any runtime state object.

## Related Design Inspirations

These systems informed the architectural invariants:

1. **LongHorizon-Harness** — task state outside model context; only
   independently verified facts become trusted progress; rejected work
   remains evidence, not progress.

2. **Temporal Durable Execution** — append-only event history; current
   state is a projection/materialized view; actions and resulting events
   are distinct.

3. **LangGraph Checkpointing** — durable checkpoints; preserve
   successful/pending writes across recovery; do not redo confirmed work
   unnecessarily.

4. **ToolSandbox** — environment/world-state snapshots; evaluation
   against state/milestones rather than self-reported completion.

5. **OpenAI Agents SDK Tracing** — stable run/turn/tool correlation
   identifiers; explicit event hierarchy.

6. **Anthropic Long-Running Harness** — structured artifacts across
   execution boundaries; separate execution and evaluator responsibility.

**No claim is made that these systems endorse Odys.**

## Non-Goals

- `NO_MULTI_AGENT` — no orchestration, handoffs, manager/coder agents
- `NO_GENERAL_MEMORY` — no RAG, conversation memory, knowledge base
- `NO_MCP` — no Model Context Protocol integration
- `NO_SKILL_SYSTEM` — no skill discovery/loading
- `NO_CHAT_MEMORY` — no conversation history management
- `NO_WORKFLOW_BUILDER` — no general workflow DAG engine
- `NO_NEW_RECOVERY_POLICY` — reuses frozen Phase4 recovery
- `NO_PAPER_CLAIM_CHANGE` — no modification to research hypotheses

## Module Structure

```
src/lhas/phase5/substrate/
    __init__.py          — public API
    state.py             — VerifiedTaskState, ControlState, StateCommit
    evidence.py          — EvidenceEvent, EvidenceLedger
    artifacts.py         — ArtifactRef, ArtifactStore, EffectReceipt
    validation.py        — ValidatorFeedback
    reducer.py           — TaskStateReducer (sole commit authority)
    store.py             — BenchmarkOutcome, RuntimeValidator, OfflineGrader
```
