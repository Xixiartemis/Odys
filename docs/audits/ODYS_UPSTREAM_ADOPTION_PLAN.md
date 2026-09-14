# Odys Upstream Adoption Plan

This is an adapter plan, not a proposal to replace Odys semantics. The order
is deliberately infrastructure-first and evidence-first.

## Non-transferable ownership

No adopted component may own:

- Task/Run/Attempt IDs or lifecycle truth;
- `CLAIMED_COMPLETE -> VERIFIED` acceptance;
- failure provenance or repair scope;
- recovery attempt lineage or macro replan;
- frozen benchmark protocol, validator, metrics, or result classification.

The integration shape is always:

```text
upstream primitive
  -> Odys adapter / policy boundary
  -> Odys IDs + EventStore + workspace identity + deadline/cancel token
  -> Odys validator/recovery authority
```

## Ordered candidates

### P0-A — Root cancellation/deadline token

Reference: Pi `packages/agent/src/agent.ts` (`AbortSignal` passed through the
loop and tool execution), OpenHands SDK
`openhands-sdk/openhands/sdk/conversation/cancellation.py` and
`LocalConversation`, plus local P45 `:_configure_runtime_deadlines`.

Reuse mode: **pattern only**, no code copy initially. Define an Odys-owned
root execution token carrying absolute deadline, cancellation state, run ID,
and attempt ID. Adapt it into provider calls, `ToolRequest`, Safe CLI, MCP,
recovery, and child execution.

Why: independent timeouts currently do not prove a single root bound. This is
the highest-risk integrity gap.

Required tests: provider blocked, subprocess blocked, MCP blocked, recovery
blocked, and child blocked; cancellation must stop active work and append one
durable terminal event without a post-cancel mutation.

License: no third-party code required.

### P0-B — Side-effect receipt interface

Reference: local `ToolInvocationRepository`/`NativeToolDispatcher`; OpenHands
typed event/tool result surfaces; Hermes post-tool hook path.

Reuse mode: **Odys-owned interface**, upstream event fields may inform shape.
Every external side-effect adapter must declare whether it returns a stable
operation ID/receipt, is idempotent by key, or is outside the official scope.

Why: local staged workspace evidence is not a universal exactly-once protocol.

Required tests: crash before observation, crash after side effect, replay,
reconciliation, and explicit `NOT_MEASURED` for unsupported side effects.

### P1-A — Session store and cold reload

Reference: Pi `packages/coding-agent/src/core/session-manager.ts` and
`agent-session.ts`; Hermes `hermes_state.py`/SQLite sessions; OpenHands SDK
`conversation/state.py`, `conversation/event_store.py`, and
`conversation/impl/local_conversation.py`.

Reuse mode: **new Odys persistence adapter** behind current repositories. Use
OpenHands EventLog ideas (typed IDs, parent links, append locking) and Hermes
SQLite indexing ideas only where they preserve Odys attempt/validation
identity. Do not make a conversation transcript the execution authority.

Why: current `resume_run()` truthfully restores outer state and workspace but
does not restore provider-internal conversation state.

Required tests: second process reload, stale identity rejection, branch/fork
without attempt-ID collision, provider swap rejection, and full state/event
reconciliation.

License: Pi MIT; Hermes MIT; OpenHands SDK MIT. Preserve notices if code is
ever copied rather than reimplemented.

### P1-B — Workspace checkpoint adapter

Reference: Hermes official checkpoint design (shadow Git store, checkpoint
before destructive file/terminal operations); OpenHands SDK local/remote
workspace factory and sandbox-server control plane.

Reuse mode: **optional backend**, never the policy owner. Checkpoints must be
created and addressed by Odys run/attempt identity, and reopen must validate
baseline/workspace identity before exposure.

Why: current checkpoint proof is strong for isolated local fixtures, not a
general remote workspace lifecycle.

Required tests: baseline immutability, remote cleanup, crash during checkpoint,
path-policy preservation, and no silent rebase.

License: inspect exact upstream package/image terms and produce a NOTICE file
before redistribution. Do not vendor sandbox-server source as a shortcut.

### P1-C — Context compaction with provenance

Reference: Pi `AgentSession` compaction/continuation events; Hermes
`conversation_compression.py` and session lineage; OpenHands SDK condenser
architecture in `openhands/sdk/context/condenser/`.

Reuse mode: **algorithmic reference only**. Compaction must record what was
summarized, what remains authoritative, the context-policy version, and the
input attempt ID. Failure evidence and validator evidence cannot be summarized
away without durable references.

Why: Odys already has bounded context, but not a general long-session
compaction subsystem.

Required tests: summary omission, tool-pair boundaries, compaction crash,
reopen, token accounting, and proof that validator/recovery evidence survives.

### P1-D — Bounded concurrency/subagent scheduler

Reference: Pi `toolExecution` sequential/parallel modes; Hermes worker-thread
async bridge and loop caps; OpenHands SDK parallel tool execution and state
locking; local `DurableDeliveryService`.

Reuse mode: **Odys scheduler design**, not foreign agent loop. Scheduler inputs
must carry run/attempt/child IDs, budgets, workspace/session binding, and the
root deadline/cancel token.

Why: current bounded locks and CAS do not prove fair parallel execution with
shared budgets and cancellation.

Required tests: parallel read-only tools, serialized mutations, child budget
exhaustion, cancellation, duplicate delivery, workspace isolation, and no
cross-run evidence mixing.

### P1-E — MCP transport hardening

Reference: OpenHands SDK typed MCP configuration/provider surfaces and Hermes
registry dispatch; local `src/lhas/mcp/manager.py` remains the current
namespaced stdio implementation.

Reuse mode: **official MCP SDK/wire semantics through an Odys adapter**. Keep
CapabilityRegistry non-executing and route concrete execution through
ToolContract/ToolRegistry. Add request IDs, root deadline, cancellation,
bounded output, and receipt classification.

Why: current MCP manager serializes requests and bounds messages, but does not
provide the complete general side-effect/cancellation contract.

Required tests: malformed JSON-RPC, server death, timeout/cancel, duplicate
request, oversized response, server-name collision, and safe secret filtering.

### P2-A — Observability export

Reference: Pi lifecycle events/JSONL modes, Hermes post-tool hooks and usage
file, OpenHands SDK callbacks/EventLog and Agent Server REST/WebSocket event
stream.

Reuse mode: **export adapter only**. EventStore remains the local durable
source for decisions; OpenTelemetry or JSONL/RPC export may be added after
append, with bounded and sanitized payloads.

Required tests: export failure does not change state, no secret leakage,
stable IDs, ordering, backpressure, and replay equivalence.

### P2-B — Comparative foreign-runtime adapters

Reference snapshots:

- Pi: `earendil-works/pi@71dca871bc80b6bc97be37f0ca3189399d651fff`,
  `packages/agent/src/agent.ts` and
  `packages/coding-agent/src/core/agent-session.ts`;
- Hermes: `NousResearch/hermes-agent@bf867d3c7451cbc849a56ed5b5f8222ec37909b9`,
  `agent/conversation_loop.py`, `model_tools.py`, `hermes_state.py`;
- OpenHands SDK:
  `OpenHands/software-agent-sdk@57f5cc9f4a671fe290783551ba00d57efe2017c`,
  `LocalConversation`, `ConversationState`, `EventLog`, `Workspace`;
- sandbox-server:
  `OpenHands/sandbox-server@f19f9e0d88272bb393e39e8cbcb78e3e8aa633a3`,
  `README.md` and app-server conversation modules.

Reuse mode: **foreign executor adapters for controlled comparison only**.
They must emit Odys-compatible execution observations, never `VERIFIED` on
their own, and never be mixed into the native benchmark without a separately
frozen configuration identity.

## Adoption acceptance checklist

An upstream primitive is admissible only when all are true:

1. exact source SHA and file/symbol are recorded;
2. license and redistribution obligations are known;
3. adapter preserves Odys IDs, policy, validator, and recovery ownership;
4. timeout/cancellation behavior is explicit;
5. persistence/reopen behavior is tested;
6. unsupported behavior is surfaced as a typed failure or `NOT_MEASURED`;
7. no frozen Phase 4 input or historical evidence changes;
8. full suite and targeted boundary tests pass at the final exact SHA.

## Recommended next gate

Do not start another official benchmark version solely to exercise an upstream
component. First close P0-A and P0-B with a small offline integration suite,
then run a non-official substrate pilot. Only after the pilot has durable
evidence should any new benchmark protocol version be proposed.

## Sources

- [Odys runtime ownership](../adr/0001-runtime-ownership.md)
- [Odys reuse policy](../adr/0002-open-source-reuse-policy.md)
- [Pi Agent](https://github.com/earendil-works/pi/blob/71dca871bc80b6bc97be37f0ca3189399d651fff/packages/agent/src/agent.ts)
- [Pi AgentSession](https://github.com/earendil-works/pi/blob/71dca871bc80b6bc97be37f0ca3189399d651fff/packages/coding-agent/src/core/agent-session.ts)
- [Hermes tools runtime](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/tools-runtime.md)
- [Hermes sessions](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md)
- [Hermes checkpoints](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/checkpoints-and-rollback.md)
- [OpenHands SDK LocalConversation](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)
- [OpenHands SDK EventLog](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/event_store.py)
- [OpenHands SDK workspace](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/workspace/workspace.py)
- [OpenHands Agent Server](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-agent-server/openhands/agent_server/README.md)
- [OpenHands sandbox-server](https://github.com/OpenHands/sandbox-server/blob/f19f9e0d88272bb393e39e8cbcb78e3e8aa633a3/README.md)
