# Task: LHAS-PHASE-A-CORE-01

Status note: **Historical completed task.** Current product direction is `docs/PRODUCT_DIRECTION.md`; terminology below is preserved as implementation history.

- **Task ID:** LHAS-PHASE-A-CORE-01
- **Phase:** A — Core Runtime
- **Status:** Done (2026-08-18, Stage 0 PASS, EXP-20260818-RUNTIME-001)
- **Created:** 2026-08-18

## 前置阅读

开工前完整阅读:
- `docs/00_PROJECT_CHARTER.md`
- `docs/01_ARCHITECTURE.md`
- `docs/02_DOMAIN_MODEL.md`
- `docs/03_ENGINEERING_CONSTRAINTS.md`
- `docs/04_EXECUTOR_PROTOCOL.md`
- `docs/10_LOGGING_SPEC.md`
- `docs/13_CODING_AI_GUIDE.md`
- `docs/14_ROADMAP.md`

## 目标

实现 LHAS Phase A Core Runtime,不接入任何真实 LLM、搜索服务或 Codex。

## 必须实现

1. Project / Task / Run / Attempt / Event 领域模型。
2. 对应状态 Enum。
3. SQLAlchemy + SQLite 持久化。
4. AgentExecutor Protocol。
5. MockExecutor。
6. EventStore。
7. 最小 Orchestrator。
8. pytest 测试。
9. Typer CLI 最小入口。

## 本阶段禁止

LangGraph、Temporal、Redis、Celery、React、FastAPI 业务接口、Planner、Memory、
MCP、Codex、第三方 LLM、Job Search。

## 测试场景

- A. MockExecutor 一次成功。
- B. 第一次失败、第二次成功。
- C. Executor timeout。
- D. Executor crash。
- E. 连续三次失败后 ESCALATED。

要求每个状态转换产生 Event,并持久化 Task / Run / Attempt / Event。

## Stage 0 验收链

```
TASK_CREATED
RUN_STARTED
ATTEMPT_STARTED
EXECUTOR_FAILED
FAILURE / RETRY
ATTEMPT_STARTED #2
EXECUTOR_COMPLETED
TASK_COMPLETED
```

## 完成定义

- 场景 A–E 测试全部通过,不得降低验收标准。
- Stage 0 全部 PASS,生成第一份实验记录 `EXP-20260818-RUNTIME-001`。
- 每个里程碑有 Git commit。
