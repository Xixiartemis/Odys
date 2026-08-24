# LHAS Roadmap

## Phase A — Core Runtime
实现：
- Task
- Run
- Attempt
- Event
- SQLite
- MockExecutor

Gate：
- Stage 0 全部通过
- Fail → Recover → Pass 能由 Mock 跑通
- 连续失败可 Escalate

## Phase B — Validation / Recovery
实现：
- Validator
- FailureClassifier
- RecoveryPolicy
- Context Snapshot

Gate：
- 真实 FailureReport
- RecoveryAction 有完整日志
- 第二 Attempt 自动执行

## Phase C — Job Benchmark Offline
实现：
- Resume / Candidate Profile
- 固定 JD Dataset
- Hard Rule Validator
- Semantic Matcher
- Ranker
- Ground Truth Eval

Gate：
- 30 JD 可完整跑完
- 自动生成第一份 Evaluation Report

## Phase D — Real Search
实现：
- SearchProvider
- Job Source Adapter
- JD Parser
- Duplicate / Expiration Detection

Gate：
- 能搜索真实公开岗位
- 结果可追溯到 Source
- 失效 / 重复可判断

## Phase E — Long-Horizon Golden Task
实现：

`Search → Parse → Filter → Match → Rank → Top N → Application Draft`

Gate：
- 全流程有 Task / Run / Attempt
- 失败可恢复
- 每一步有 Validation
- 自动生成实验报告

## Phase F — Application Dry Run
实现：
- ApplicationProvider
- 表单映射
- 字段验证
- READY_TO_SUBMIT

Gate：
- 自动执行到 Submit 前
- Human Approval Gate 生效
- 不允许自动越权提交

## Phase G — Public V0
完成：
- README
- Architecture Diagram
- Quick Start
- Demo
- Benchmark Results
- Failure Analysis
- ADR

Gate：
- 别人 clone 后可以复现实验

## Phase H — SWE Benchmark
增加：
- CodexExecutor
- Git Workspace
- deterministic test validator
- coding recovery experiments

## V1+
按证据决定是否加入：
- MCP
- Planner
- TaskGraph
- Project Memory
- Multi-Executor
- React Control Plane
- Durable Execution
- Temporal
- Multi-Agent

原则：
只有当实验数据证明当前瓶颈需要它时，才引入新模块。
## Phase D Foundation Hardening

Only LINEAR plans are executable. SIMPLE_DEPENDENCY is explicitly unsupported until a later phase. Recovery reuses RecoveringOrchestrator, and human approval resumes the persisted plan without re-running completed steps.
## Phase E1 — deterministic TaskGraph runtime

`SIMPLE_DEPENDENCY` plans now execute serially through a pure scheduler with READY/BLOCKED semantics, dependency-closure context, independent-branch continuation, and same-Plan approval resume. Parallel execution and dynamic replanning remain out of scope. Harness version: HV-0.6.
## Phase E2 — Inner Agent Runtime

E1 is complete. E2 adds the provider-neutral inner-agent backend boundary and the first optional OpenAI Agents SDK backend without rewiring the macro TaskGraph.
