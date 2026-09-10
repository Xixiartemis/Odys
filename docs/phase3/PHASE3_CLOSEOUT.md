# Phase 3 Closeout — Verified Workflow V1

PHASE3_STATUS=CLOSED

## Purpose

Phase 3 delivered Verified Workflow V1: an execution path in which typed
workflow structure, trusted runtime evidence, independent verification, and
bounded recovery are represented as durable system state.

## Milestones and identities

| Milestone | Meaning | Merge SHA |
|---|---|---|
| P3.1 | Typed TaskGraph Authority | `bbd1d352720ccc22dd9454db2331e43a8981b8ce` |
| P3.2 | Production Workflow Integration | `057954e2f4a81875327090423579ca40994f266d` |
| P3.3 | Verified Failure & Selective Repair | `4058edc007ab80ee4103c39c7d3e43c055583f52` |

P3.3 final PR head: `b21b400e843a4b91594f133aaa4a8e9f0adc3811`.
These identities distinguish merge SHAs from execution and documentation
SHAs.

## Canonical production semantics

The verified production chain is:

```text
Goal → Typed TaskGraph → eligibility → dispatch-time precondition check
     → Task / Run / Attempt → runtime execution
     → ToolContract-backed evidence → CLAIMED_COMPLETE
     → external verification → VERIFIED
```

Failure continues through failure provenance, the single repair-scope
authority, bounded selective repair, durable Attempt lineage, and
re-verification. Tool success, Run success, and agent self-assertion do not
make a step VERIFIED. Only the verifier can do so; a missing verifier is
fail-closed.

P3.3 repair scopes are LOCAL, AFFECTED_SUBGRAPH, and MACRO_REPLAN. Retry is
not recovery. Systemic provider/resource failures are MACRO_REPLAN regardless
of DAG shape.

## Identity and scope boundaries

The durable identity chain is Task → Run → Attempt → ValidationResult →
StepFailureProvenance → repair lineage. For verification rejection,
`ValidationResult.attempt_id` is the producing Attempt authority; identity is
not silently replaced with a latest record.

P3.3 authoritative selective-repair semantics apply to
`SIMPLE_DEPENDENCY`. `LINEAR` remains an explicit legacy execution path
outside the P3.3 selective-repair contract.

## Evidence and limitations

Available evidence categories include local regression suites, GitHub PR
merge-ref CI, post-merge main CI, DB-reload repair-lineage tests, and Phase 2
capability conformance. The repository evidence supports 900+ automated tests
across the milestone history; it does not establish long-horizon superiority,
cost efficiency, adaptive reliability, or a benchmark advantage.

Backlog remains: LINEAR modernization; repair-hint and canonical scope
taxonomy harmonization; broader trusted evidence classes; live long-horizon
benchmark execution; adaptive routing; and multi-agent reliability.

## Closure

PHASE3_STATUS=CLOSED

NEXT=P4.1_CONTROLLED_BENCHMARK_FREEZE
