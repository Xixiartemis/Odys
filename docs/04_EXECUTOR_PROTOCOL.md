# AgentExecutor Protocol

Status: **Compatibility contract.** Canonical architecture is `docs/01_ARCHITECTURE.md`. An executor/capability does not own a competing long-horizon runtime or completion authority.

## 定位
Executor 是“干活的人”，不是“决定任务是否成功的人”。

LHAS Core 只依赖抽象协议，不依赖 Codex。

## 输入
Executor 接收：
- Task
- ExecutionContext
- ToolPermissions
- Budget
- Run / Attempt metadata

## 输出
Executor 返回：
- ExecutionResult
- Events
- Artifacts
- Usage
- Raw provider metadata（可选）

## V0 接口
```python
class AgentExecutor(Protocol):
    async def execute(self, request): ...
    async def resume(self, request): ...
    async def cancel(self, run_id: str): ...
    async def status(self, run_id: str): ...
```

## Executor 不负责
- 判定 Task Complete
- 修改 Acceptance Criteria
- 决定是否 Retry
- 管理长期 Memory
- 修改 Benchmark
- 绕过 Human Approval Gate

## V0 实现

### MockExecutor
用于：
- 状态机
- Event
- Timeout
- Crash
- Fail Once → Pass
- 连续失败 → Escalate

不消耗模型额度。

### GeneralAgentExecutor
用于 Job Agent / 通用任务，允许接低成本第三方模型。

### CodexExecutor
用于 SWE Coding Benchmark 或需要真实 Coding Agent Runtime 的实验。

## 配置必须外置
至少支持：
- provider
- model
- base_url
- reasoning_effort
- timeout
- max_attempts
- budget

更换模型或 Provider 不应修改 LHAS Core。
Planner tools are invoked through ToolRequest/ToolResult and then enter the existing Task/Run/Attempt validator and recovery runtime. Tool implementations remain offline and provider-neutral in this phase.
