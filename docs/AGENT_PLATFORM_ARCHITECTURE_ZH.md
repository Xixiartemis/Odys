# Odys Agent Platform / Agent OS Foundation 架构

本文描述 HV-1.5 已实现的平台骨架。核心原则是：Hermes 提供成熟设计启发，Odys 保留可恢复、可验证、可审计的长期任务控制平面。

## 1. 总体架构

用户从 CLI 进入 RootAgent。简单交互由统一 AgentKernel 完成；长期 Goal 经 Planner 生成已有 Plan/PlanStep，再进入 TaskGraph Scheduler。每个 Step 都成为 Odys Task，由 RecoveringOrchestrator 创建 Run/Attempt，最终由 Validator 裁决。Memory、Session、Knowledge、Skill、Toolset 与 MCP 是支持层，不拥有任务完成权。

## 2. Root / Planner / Worker / Subagent

Root 负责会话、检索和确定性路由。Planner 只产出 schema-valid Plan，不写 workspace、不完成 Task。Worker 执行 PlanStep。Subagent 通过 DelegationService 产生持久化 Child Task→Child Run→Child Attempt；它不是内存中的 asyncio task。

## 3. AgentKernel

`AgentKernel.run/cancel/status` 是所有角色共享的窄接口。输入 `AgentRequest` 包含角色、目标、选定上下文、消息、能力、Toolset、Skill、Memory/Knowledge scope、预算和父级 lineage；输出 `AgentResult` 只包含最终输出、completion claim、计数、usage、artifact、安全 trace 和 child run 引用，不含 hidden chain-of-thought。

## 4. Control Plane

Goal、Plan、TaskGraph、Task、Run、Attempt、Checkpoint、CP-3、Process Resume、Validator、FailureClassifier、RecoveryPolicy 与 EventStore 都继续以原 Odys 实现为权威。平台层是 Adapter/Service，不另造 HermesRun 或第二恢复引擎。

## 5. Memory

`BuiltinMemoryProvider` 将长期用户/工作记忆分别保存到 `MEMORY.md` 与 `USER.md`。单条与总文件均有边界。Root 可在审批后写；Worker/子代理默认只读。Memory 与会话、项目知识严格分离，并由 ContextAssembler 选择后进入 Agent。

## 6. Session

Conversation Session 与 message 存在现有 SQLite。Repository 支持 create、append、list、read/scroll 和 FTS5 search。只保存 user message、assistant final、可选 safe tool summary、时间与 lineage；禁止 provider 原始 transcript 和 reasoning 角色。

## 7. Knowledge

`LocalKnowledgeProvider` 对项目 README、docs、显式 knowledge directory 做 bounded lexical search/open。当前不使用 Vector DB；接口可在未来增加 Vector/Hybrid Provider。

## 8. Skill

SkillRegistry 扫描项目 `.odys/skills/` 与用户 `~/.odys/skills/`。Level 0 只暴露 name/description/metadata；Level 1 显式加载 SKILL.md；Level 2 只从该 Skill 的 references 目录加载指定文件。内置示例为 `coding/bug-fix` 和 `coding/code-review`。

## 9. Tool

原生 `ToolRegistry` 是唯一 capability 注册与解析入口。平台工具、Skill/Memory/Knowledge 工具和 MCP Adapter 都遵守同一 ToolRequest/ToolResult contract。持久事件只记录 capability、状态、错误类型和 artifact key 等摘要，不记录原始参数。

## 10. MCP

第一版真实支持 stdio：启动显式 argv 子进程，执行 initialize、initialized notification、tools/list 和 tools/call。发现的工具以 `mcp.<server>.<tool>` 注册到 ToolRegistry。元数据包含 origin、server_name、side_effect、approval 和 risk。离线 fake server 不访问网络。

## 11. Toolset

Toolset 是命名 capability 集，当前有 workspace、terminal、skills、memory、knowledge、mcp 六组。AgentProfile 引用 Toolset，Registry 再展开为当前 ToolRegistry 中实际存在的 capability。Child Toolset 必须是 Parent 的子集。

## 12. ContextAssembler

ContextAssembler 接受显式选中的 Goal/Task、Delegation Context、WorkingState/Checkpoint、Recent Evidence、Session、Memory、Project Context、Skill metadata、Knowledge 和 Tool summary。每节有优先级和字符预算，按 required/high/normal/low 截断。它不全量加载 Skill、Session 或 Knowledge；历史 Attempt 仍由 ContextBuilder/CP-3 统一重建。

## 13. Delegation

Delegation 持久化 parent/child agent、Task、Run、spawn depth、状态、bounded context/result。默认 max_children=3、max_spawn_depth=1。Child 只收到明确 goal、压缩 context、角色、Toolset、Skill 与预算，不继承 parent transcript；返回 summary、artifact、evidence、changed files、validation 和 child_run_id。

## 14. Validator

Root、Worker、Subagent 的 `completion_claim=true` 都不是成功判决。只有 Outer Validator 能完成 Task；`Agent SUCCESS + Validator FAIL` 不能完成，已有 `Agent FAILURE + Validator PASS` Outcome Arbitration 语义保持不变。

## 15. Failure Hierarchy

FailureLevel 定义 TOOL、ATTEMPT、TASK、PLAN、GOAL。FailureRouter 将它们路由到 local recovery、checkpoint retry、strategy replacement boundary、dynamic replan boundary 或 human clarification，并写安全事件。本阶段只实现 contract/event/boundary，没有实现 Dynamic Replan。

## 16. 数据流

离线完整流为：User→Root classification→Memory read→Project Context→Skill metadata→Scripted Planner→三步 TaskGraph→Worker Run。第二步 Worker 调用 DelegationService→Child Task/Run/Attempt→Skill load→Knowledge search→fake MCP→bounded child result→Parent。所有父 Step 经 Validator 后完成，Plan 与 Goal 完成，全部状态进入 SQLite/EventStore。

## 17. 当前已实现部分

已实现五种 AgentRole、统一 Kernel 与旧 InnerAgent Adapter、Profile/Provider/Toolset Registry、Skills、Memory、Session FTS5、Knowledge、Project Context、MCP stdio、持久委派、Failure boundary、Root 路由、三步 Planner/TaskGraph、Agent Tree 投影、CLI chat/agents/skills/memory/mcp，以及无网络全平台 E2E。

## 18. Hermes-derived vs Odys-native

Agent loop 统一、Skill 渐进披露、Provider/Profile、Toolset、Memory Provider、Session search、MCP 生命周期和 fresh child context 来自 Hermes 架构研究并由 Odys 原生重实现。Durable Task/Run/Attempt、恢复、CP-3、Workspace、Validator、Outcome Arbitration 与 Evidence 完全是 Odys-native。没有复制 Hermes Python 文件或 branding。

## 19. 后续 Roadmap

下一阶段可在保持边界的前提下增加真实 ModelPlanner、更多 AgentKernel provider adapter、持久 MCP 配置、HTTP transport、审批 UI、Project Knowledge 索引增量更新和受控的 Task/Plan strategy replacement。Dynamic Macro Replan、并行分布式 Agent、Vector DB 与消息 Gateway 仍需独立设计和证据。
