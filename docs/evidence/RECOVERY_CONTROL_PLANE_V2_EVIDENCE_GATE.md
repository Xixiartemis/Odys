# Recovery Control Plane V2 Evidence Gate

- Base SHA: `db15e50c6e4a1df0d8c2e0706e048a1225510423`
- Final SHA: `462383805542854661a23892c0236be208405335`
- Provider execution: `NO`

## Historical replay

Persisted `tool_invocations` are replayed through the current `RepairProgressTracker` and `EffectProgressEvaluator`; no model output is regenerated.

- R1: stop turn `3`, reason `NO_PROGRESS`, typed signal `REPAIR_NO_PROGRESS`, avoided `15` of `18` turns.
- R2: stop turn `3`, reason `NO_PROGRESS`, typed signal `REPAIR_NO_PROGRESS`, avoided `16` of `19` turns.

## Context projection

The runtime exposes `chars_used` and provider-returned usage, but no offline tokenizer is installed; provider token fields therefore remain `NOT_MEASURED`.

- Context chars by turn: `{1: 4098, 2: 5064, 4: 6086, 8: 6355, 16: 6911, 32: 6929}`
- Growth class: `BOUNDED_WINDOW_CHAR_PROJECTION`
- Linear growth eliminated at projection level: `True`
- Durable history is not modified by this measurement.

## Budget and control-flow proof

- Local calls before escalation: `3`
- Reservations remaining at escalation: `{'local_repair': 1, 'macro_replan': 2, 'post_replan': 2, 'validation': 1}`
- Local lease isolated from escalation reserve: `True`
- Macro replan / post-replan / validation executed offline: `True` / `True` / `True`
- Positive and negative authoritative-validation paths are covered by the targeted control-plane tests; only the authoritative validator can grant VERIFIED.

## Decision

The evidence gate is ready for a live controlled experiment. The remaining limitation is deliberate: provider token counts require the live provider's returned usage fields.
