# Odys Development Instructions

## Architecture gate

Read `docs/ARCHITECTURE_FREEZE.md` before proposing architecture changes.

Read it again before implementing an approved architecture change.

The frozen decisions cover runtime ownership, planning ownership, verified workflow semantics, open-source reuse, adaptive reliability levels, development order, and claim boundaries. A major change requires the evidence and ADR process defined in that document.

Default rule:

> EXTEND, DO NOT REWRITE.

Do not begin a later roadmap phase before its predecessor gate is satisfied. The immediate engineering gate is Phase 1: the real Native Basic Vertical Slice from Model through Tool, Observation, Multiple Turns, CompletionAuthority, Validator, and VERIFIED.

Do not build the full Hermes capability ecosystem before that gate. Do not run live models unless the active task explicitly authorizes it.

Historical experiment and evidence documents must not be rewritten to match current terminology.
