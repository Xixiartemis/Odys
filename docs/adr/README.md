# ADR 登记

ADR(Architecture Decision Records)记录影响项目长期结构的设计决策。
规则(docs/13_CODING_AI_GUIDE.md):

- 关键设计决策必须通过 ADR 保留。
- ADR 只新增、不覆盖;新决策写新编号。
- 状态:`Proposed` → `Accepted` / `Superseded by ADR-XXX`。
- 格式:背景(为什么)→ 现状 → 决策 → 影响 → 后续动作。

## 目录

- [ADR-001 Orchestrator 组合化演进(技术债登记)](ADR-001-orchestrator-policy-composition.md)
- [ADR 0001 Runtime Ownership](0001-runtime-ownership.md) — ACCEPTED / FROZEN
- [ADR 0002 Open-Source Reuse Policy](0002-open-source-reuse-policy.md) — ACCEPTED / FROZEN
- [ADR 0003 Verified Workflow Semantics](0003-workflow-semantics.md) — ACCEPTED / FROZEN
- [ADR 0004 Adaptive Reliability Levels](0004-adaptive-reliability-levels.md) — ACCEPTED / FROZEN

Canonical freeze: [`docs/ARCHITECTURE_FREEZE.md`](../ARCHITECTURE_FREEZE.md).
