# ADR 0002 — Open-Source Reuse Policy

Status: **ACCEPTED / FROZEN**

## Context

Odys should reuse mature transport, protocol, telemetry, and isolation components without outsourcing the semantics under research. Several projects overlap with workflow, checkpoint, or agent-runtime ownership.

## Decision

Reuse directly:

- official provider SDKs for HTTP, authentication transport, streaming, and provider protocols;
- the official MCP Python SDK for MCP wire semantics;
- OpenTelemetry for trace/metric export;
- established container/sandbox primitives for OS isolation.

Wrap each through an Odys adapter and policy boundary. EventStore remains durable execution truth. Odys retains workspace/capability policy, protected paths, side-effect classification, ToolRegistry integration, and runtime ownership.

Treat LangGraph, Temporal, LongHorizon-Harness, Pi, and Hermes as references, baselines, foreign executors, or optional future deployment adapters according to `docs/ARCHITECTURE_FREEZE.md`. None becomes the canonical Odys core owner.

## Consequences

- Odys avoids custom MCP, tracing, provider transport, and container protocols.
- Research variables remain attributable to Odys.
- Adapter maintenance is required.
- External projects can still be compared or integrated at explicit boundaries.

## Alternatives Rejected

- Hand-implementing the MCP wire protocol.
- Reconstructing execution truth from telemetry spans.
- Embedding LangGraph as the canonical Workflow Runtime.
- Implementing current research recovery/checkpoint semantics in Temporal.
- Importing Pi AgentHarness as NativeAgentKernel.
- Making Hermes AIAgent the execution-lifecycle owner.
- Embedding LongHorizon-Harness as the reliability core.

## Conditions for Reconsideration

Core ownership may change only after stable benchmark claims, a demonstrated limitation, evidence-based alternatives analysis, migration cost, a superseding ADR, and explicit impact on research attribution.
