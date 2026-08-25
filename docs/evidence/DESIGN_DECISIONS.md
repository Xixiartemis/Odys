# Evidence Design Decisions

## DD-E5-INNER-FAILURE-NOT-TASK-FAILURE

**Context.** In both preserved E5 MiMo calibration runs, the staged calculator fixture reached `FAIL → PASS` and the outer validator accepted the final patch, while the Inner Agent exhausted its 20-turn budget without a completion claim.

**Decision.** The outer validator remains the authority for functional validation. `InnerAgentResult.FAILURE` caused by `TURN_LIMIT` is recorded as an agent-lifecycle failure and is not relabeled as task failure when the validator independently confirms the required patch and tests. Conversely, it is not relabeled as agent success.

**Evidence boundary.** This distinction is an E5 observation, not a runtime behavior change. The two runs are reported with separate functional-validation and agent-completion rates, both scoped to the E5 calculator fixture.

**Future input.** E6 may test reconstruction and validation of useful state from a failed or interrupted Inner Agent: a validated PASS should finish without retry, while a validated FAIL should recover, retry, or resume. No E6 implementation is included here.

## Deferred efficiency hypothesis

`tool_use_behavior` and acceptance-based early stopping remain future efficiency optimizations. They were not enabled or inferred from these runs because both runs still terminated at the 20-turn limit.
