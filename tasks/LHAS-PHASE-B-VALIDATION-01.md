# Task: LHAS-PHASE-B-VALIDATION-01

Status note: **Historical implementation task.** Current product direction is `docs/PRODUCT_DIRECTION.md`; terminology below is preserved as implementation history.

- **Task ID:** LHAS-PHASE-B-VALIDATION-01
- **Phase:** B — Validation / Failure / Recovery
- **Status:** In progress
- **Created:** 2026-08-18
- **Depends on:** LHAS-PHASE-A-CORE-01 (done, Stage 0 PASS, EXP-20260818-RUNTIME-001)

## 前置阅读

- `docs/01_ARCHITECTURE.md`(Validation/Failure/Recovery 主链路)
- `docs/05_CONTEXT_POLICY.md`(ContextBuilder,CP-0/CP-1/CP-2)
- `docs/06_VALIDATION_SPEC.md`(V1–V4 Validator)
- `docs/07_FAILURE_TAXONOMY.md`(FailureClassifier)
- `docs/08_RECOVERY_POLICY.md`(RecoveryPolicy)
- `docs/09_EVAL_PROTOCOL.md`、`docs/12_EXPERIMENT_PROTOCOL.md`、`docs/14_ROADMAP.md`

## 目标

实现核心闭环:

```
Executor
   ↓
Validator
   ↓
FAIL
   ↓
FailureClassifier
   ↓
RecoveryPolicy
   ↓
ContextBuilder
   ↓
Attempt #2
```

做到 **FAIL → CLASSIFY → RECOVER → PASS**。

## 必须实现

1. Validator(V1 Structural / V2 Rule,deterministic 优先;只判断、不修改结果)。
2. FailureClassifier(映射到 docs/07 Failure Taxonomy,产出真实 FailureReport)。
3. RecoveryPolicy(docs/08 V0 默认策略:Attempt1→RETRY_WITH_FAILURE_CONTEXT,
   Attempt2→RETRY_WITH_EXPANDED_CONTEXT,Attempt3→ESCALATE;特殊 Failure 处理)。
4. ContextBuilder(CP-0/CP-1/CP-2;每个 Attempt 保存 Context Snapshot)。
5. RecoveringOrchestrator(复用 Phase A executor 处理,替换决策层)。
6. 持久化:validation_results / failure_reports / recovery_actions /
   context_snapshots 四张新表。
7. pytest 测试 + Stage B 套件 + 实验记录 EXP-20260818-RUNTIME-002。

## 本阶段禁止

真实 LLM、真实搜索、Codex、Planner、Memory、MCP、Human Approval Gate(Phase F)。

## 验收

- recoverable-context:FAIL→CLASSIFY(MISSING_CONTEXT)→RECOVER→PASS。
- validation-feedback:VALIDATION_FAILED→CLASSIFY→RECOVER→VALIDATION_PASSED。
- unrecoverable:3 次失败 → 3 份 FailureReport → RETRY/RETRY/ESCALATE。
- harness_version 升级 HV-0.2(Recovery/Context/Validation 策略变化,docs/12)。
