# Coding AI Guide

## 开工前必须阅读
每次 Coding AI 开工前至少读取：
1. `ARCHITECTURE_FREEZE.md`
2. `01_ARCHITECTURE.md`
3. `PRODUCT_DIRECTION.md`
4. `02_DOMAIN_MODEL.md`
5. `03_ENGINEERING_CONSTRAINTS.md`
6. 当前 Task Spec
7. 当前阶段相关专项文档

提出或实现任何架构改动前，必须完整读取 `docs/ARCHITECTURE_FREEZE.md`。若改动触及冻结的 runtime ownership、planning ownership、workflow semantics、reuse policy、adaptive levels 或开发顺序，必须满足其中的 benchmark evidence 与 ADR 要求。

> Read `docs/ARCHITECTURE_FREEZE.md` before proposing architecture changes.

不要默认读取全部历史 Experiment。

## 每个开发任务开始前输出
- **Understanding**：理解的目标
- **Scope**：准备修改哪些模块 / 文件
- **Constraints**：不能违反哪些约束
- **Plan**：准备如何实现
- **Risks**：可能影响哪些已有行为

## 完成后必须输出
- Changed Files
- Implementation Summary
- Tests Run
- Test Results
- Known Limitations
- Architecture Impact
- New Dependencies
- Contract Changes
- Follow-up Suggestions

## 完成定义
Coding AI 不能只说 Done。

必须至少满足：
- 代码已实现
- 相关测试已运行
- 测试结果已报告
- 没有降低 Validator
- 没有无关大规模重构
- 已知限制明确说明

## 禁止事项
- 擅自改变项目方向
- 擅自修改 Domain Model
- 擅自引入大型框架
- 删除测试
- 修改 Benchmark 让实现通过
- 隐藏失败
- 把 Exception 转成 success
- 自动绕过 Human Approval
- 在未要求时实现未来模块
- 为“代码漂亮”重构无关代码

## AI Coding 的定位
AI Coding 负责：
- implementation
- boilerplate
- tests
- 局部 debugging

人负责：
- architecture
- invariants
- acceptance
- experiment design
- failure analysis
- trade-offs

关键设计决策需要通过 ADR 保留。

默认原则：**EXTEND, DO NOT REWRITE.**
