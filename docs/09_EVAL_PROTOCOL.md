# Evaluation Protocol

## Primary objectives

Primary success metric: **Verified Completion Rate**.

Primary efficiency metric: **Cost per Verified Completion**.

Repeated reliability metric: **Repeated Reliability / `pass^k`**.

```text
total execution cost / validator-accepted tasks
```

Minimum raw token usage is not the product objective. A lightweight failure can be less efficient than a more reliable run when measured per accepted outcome.

## Required metric families

### Effectiveness and completion integrity

- task success and validator acceptance;
- first-pass and final verified completion;
- recovery success;
- false-completion rate;
- stale-plan execution;
- duplicate side effects.

Recovery success means an initial Attempt failed, no human modified the core result, and automated recovery ultimately produced validator-accepted completion.

### Reliability

- executor crash and timeout rate;
- validation failure and unknown failure rate;
- provider failure classification;
- regression rate;
- liveness and stalled-run evidence.

### Efficiency

Track when available:

- fresh input tokens and cached input tokens;
- output tokens;
- model calls and tool calls;
- dead-end turns and redundant tool calls;
- recovery turns and attempts;
- executor and validation time;
- wall time;
- provider cost;
- cost per verified completion.

A missing measurement is `NOT_MEASURED`; it must never be estimated or fabricated without a documented method.

### Verified workflow

- `dependency_violation_rate`;
- `stale_plan_execution_rate`;
- `verified_transition_rate`;
- `false_completion_rate`;
- `repair_scope`;
- `lost_work_after_failure`;
- `duplicate_side_effect_rate`;
- `checkpoint_recovery_rate`;
- `workflow_completion_ratio`;
- `control_overhead_ratio`.

### Autonomy

Track human intervention count and type, including clarification, environment repair, context supply, manual recovery, manual code repair, and explicit approval.

### Context efficiency

Track selected context size/tokens, sources supplied, failure-context size, recovery-context delta, cache reuse, and whether progressive disclosure was used. Durable state size is not prompt size.

### Quality

Benchmark-specific deterministic criteria remain authoritative. SWE tasks may include tests, regression, lint, typecheck, and acceptance checks. Domain benchmarks must preserve frozen ground truth and validation criteria.

## Paired benchmark philosophy

Future comparison targets are Pi, Hermes, and Odys. Where technically possible, paired runs use the same model, task, repository, capabilities, validation criteria, and similar budget. The research question is whether adaptive reliability improves long-horizon verified completion while keeping cost per verified completion close to lightweight runtimes.

Do not claim that Odys, Pi, or Hermes categorically lacks a mechanism or that Odys is superior without paired evidence.

## Formal conclusion discipline

Allowed example:

> Under the frozen task/model/validator contract, the DURABLE policy increased validator-accepted completion from X to Y while cost per verified completion changed from A to B.

Not allowed without evidence:

> Odys is smarter, production-ready, token-optimal, or categorically better than Pi or Hermes.
