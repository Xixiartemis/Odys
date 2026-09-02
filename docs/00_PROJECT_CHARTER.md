# LHAS 项目总纲（Project Charter）

Status: **Historical / Superseded**

Canonical architecture: `docs/ARCHITECTURE_FREEZE.md`

Superseded by: `docs/PRODUCT_DIRECTION.md` and `docs/01_ARCHITECTURE.md`

This file preserves the original V0 hypothesis and must not be read as the current Odys product definition.

## 项目定位
LHAS（Long-Horizon Agent System）是一个面向长任务的可验证 Agent Runtime / Harness。

它不是新的聊天机器人，也不是重新实现一个 Coding Agent。核心目标是：

> 通过外部状态管理、Context 构建、验证、失败分类、Recovery 和 Eval，让现有 Agent 在长任务中更可靠地完成目标。

## 第一阶段 Golden Task
第一阶段真实业务场景：

> 根据候选人简历与职业目标，搜索公开校招岗位，解析 JD，完成匹配、筛选、排序、申请材料准备，并在人工确认后执行投递。

主链路：

`Goal → Search → Job Pool → Parse → Match → Rank → Prepare → Validate → Human Approval → Submit`

第二类 Benchmark 保留为 SWE Coding Task，用于提供更强的确定性验证环境。

## V0 核心假设
1. 外部 Harness 能否比“单次 Agent 调用”获得更高的最终任务完成率。
2. Validation + Failure Classification + Recovery 是否能把首次失败任务救回来。
3. 更合理的 Context Policy 是否能提升成功率，而不是只增加 Token。
4. 是否能在减少人工介入的同时保持结果质量与可追溯性。

## V0 必做
- Task / Run / Attempt 三层状态模型
- AgentExecutor Protocol
- MockExecutor
- 一个真实 AgentExecutor
- ContextBuilder
- Structural / Rule / Semantic / Action Validation
- Failure Taxonomy
- Recovery Policy
- Event / Trace
- Experiment / Eval
- Job Search & Match Benchmark
- Human Approval Gate

## V0 不做
- Multi-Agent
- 复杂 Planner
- DAG 调度
- 长期向量 Memory
- 大型 React 控制台
- Temporal
- Kubernetes
- 自研云 Sandbox
- 多个正式 Executor 同时接入
- 自动绕过人工确认进行不可逆申请提交

## 成功标准
V0 至少完成：
- 一个真实长任务端到端执行
- 首次 Attempt 失败
- 自动 Validation
- 自动 Failure Classification
- 自动 Recovery
- 第二次 Attempt
- 再次 Validation
- 最终 PASS / FAIL / ESCALATED
- 完整实验日志
- 自动生成测评报告
- 至少一次 Baseline vs Harness 对照实验

## 项目原则
- Executor 不决定任务是否完成。
- Agent 自称 Done 不等于 Task Complete。
- Validator 不修改任务结果，只判断。
- RecoveryPolicy 只做决策，不直接执行外部动作。
- Context 必须由 ContextBuilder 统一构建。
- 每个正式实验都必须可复现。
- 历史实验不可覆盖。
- 一个实验尽量只改变一个核心变量。
- 项目首先是实验平台，其次才是产品。
