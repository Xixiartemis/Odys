# Phase 4 Evidence Boundary

This page records the current Phase 4 evidence without broadening it into a
general benchmark claim.

## Frozen identity

| Field | Value |
| --- | --- |
| Implementation SHA | `ce5eff6c8556a92232c2e8d5b7a9ec442652c1dc` |
| Frozen task projection hash | `40bd38137adaf05343127fb090e4d13b7a7742eb42625ca875375f20f081800a` |
| Model | `mimo-v2.5` |
| Provider | `xiaomimimo-openai-compatible` |
| Experiment | `phase4-live-no-progress-escalation-02h-attempt5` |
| Provider execution during implementation | `NO` |

Attempt5 artifacts are historical immutable evidence. They must not be
overwritten or regenerated in place.

## Proven

Attempt5 contains six valid real-provider runs: V2 verified completion was
3/3 and the legacy baseline was 1/3. The proven V2 causal path is:

```text
fault
  -> repeated no-progress
  -> NO_PROGRESS_AWARE early control
  -> preserved provider budget
  -> durable REPLAN_ACCEPTED
  -> accepted PlanStep reaches the real model
  -> request-scoped capability narrowing
  -> planner-owned workspace mutation
  -> external observable fixture state changes
  -> external validator ACCEPTED
  -> verified_completion=true
```

This is controlled live recovery evidence, not a broad reliability score.

## Qualified offline

Provider-free tests qualify the control-plane semantics for:

- candidate validation boundaries where mutation is not promoted directly to
  `VERIFIED`;
- one root repair authority across local repair and macro replan;
- durable external ACCEPTED/REJECTED finalization;
- side-effect receipts, replay reconciliation, and fail-closed unknown
  commit state;
- bounded plan/version/step/strategy correlation for post-replan events;
- separate ACTION, EFFECT, and EXTERNAL observation semantics.

These tests are qualification evidence, not additional live-model samples.

## Not proven

- Generalization to arbitrary tasks or models.
- Equal-quality recovery cost reduction from Attempt5. The baseline and V2
  outcomes are not equivalent in all pairs.
- A public benchmark improvement.
- Exactly-once effects in arbitrary external systems.
- Phase 5 multi-step benchmark performance.
- Attempt6 live results. Attempt6 has not been executed by this change.

## Limitations

The controlled result uses one model/provider profile, six runs, and a
fault-oriented task projection. External validator authority is required for
`VERIFIED`; a model claim alone is not completion evidence. The side-effect
receipt design is receipt-backed, idempotency-aware, reconciliation-aware,
and fail-closed for unknown state; it does not claim distributed exactly-once
semantics.

## Attempt6 gate

Attempt6 is permitted only after provider-free tests and both Linux and
Windows CI prove one successful post-replan mutation, zero redundant
post-success provider/tool calls, a single root repair authority, and durable
`PlanStep=VERIFIED` / `Plan=COMPLETED`. The frozen task hash above must remain
unchanged. No provider call is part of this implementation gate.

## Causal evidence sketch

```text
same task/fault/validator/root budget
                 |
        +--------+--------+
        |                 |
 legacy bounded       Odys no-progress-aware
        |                 |
 later bounded        typed signal -> macro replan
 recovery             -> one accepted mutation
        |                 |
        +--------+--------+
                 |
        authoritative external validator
                 |
       ACCEPTED -> VERIFIED / otherwise REJECTED
```
