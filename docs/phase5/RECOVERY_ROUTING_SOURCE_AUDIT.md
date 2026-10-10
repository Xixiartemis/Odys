# Phase5 恢复路线与实验归因 — 源码审查矩阵

- 基线分支：`fix/phase5-execution-control-01` @ `973a612a02d984fe8312401b08b3c394e5994649`
- 基线代码 SHA：`04ebe293d25ae7bf0fa8a82df5fec007b71ee6db`
- 审查日期：2026-10-09
- 审查方式：逐条跟踪真实执行路径（execute_trial → ExecutionEngine.run →
  agent_adapter.step/receive_tool_result → agent_core → execution_control /
  recovery_budget），不按模块名推断能力。

## B1. 恢复路线决策

| # | 问题 | 当前实现位置 | 代码与测试证据 | 状态 | 风险 | 必要修改 |
|---|------|-------------|---------------|------|------|---------|
| B1-1 | `is_retry_justified()` 与 `RecoveryBudgetGate` 是否独立决策、独立记账 | `execution_control.py:693`（is_retry_justified）；`agent_core.py:841`（_authorize_recovery 两判断）；`recovery_budget.py`（独立账本） | 被拒重试 `budget_decision="NOT_CONSUMED"` 且 `recovery_budget.ledger` 不增长；`tests/test_phase5_execution_control.py::test_recovery_retry_no_progress_converted_to_explicit_stop` 断言 `len(budget_gate.ledger)==1` | **已实现** | 路线合理性与预算充足性未被命名/建模为显式两层，新需求下需可独立报告 | 引入显式 RouteDecision 记录（路线层）与 BudgetDecision（预算层）分层字段 |
| B1-2 | 是否有统一的恢复路线决策入口 | 无。策略 `on_step_result` 直接返回 `RecoveryDecision`（`control_arms.py:114`：none/retry/retry_with_context/escalate/stop）；gate 只回答重试合理性 | `grep REPLAN src/lhas/phase5/*.py` → 0 命中；无 CONTINUE/RETRY_CHANGED 枚举 | **缺失** | 路线选择散落在策略+两处 core 代码（验证路径与工具结果路径），不可统一审计 | 新增 `recovery_route.py`：RecoveryRoute 枚举 + RecoveryRouteDecider，唯一决策入口 |
| B1-3 | 相同参数/相同结果/无状态变化判断是否使用可信状态证据 | `execution_control.py:636`（_evaluate_repeat）比较 `result_digest` 与 `_state_digest()`（条件状态摘要） | 规则用摘要比较；**把"摘要未变"当作"状态未变"**，对不暴露状态字段的工具不成立 | **部分实现** | 违反"不能仅凭两次返回字符串相同就断言环境状态没有变化" | 增加 StateChange（OBSERVED_CHANGE / NO_OBSERVED_CHANGE / UNKNOWN）与证据等级；无状态证据时记 UNKNOWN |
| B1-4 | UNKNOWN / NOT_READY / NO_EVIDENCE / 瞬态失败语义一致 | `execution_control.py:299`（ToolResultNormalizer 三轴） | `test_phase5_execution_control.py::TestNormalizedResultSemantics` 6 项 | **已实现** | 语义已一致但证据等级未显式记录 | 归因字段补 evidence_grade（DIRECT/DERIVED/UNKNOWN） |
| B1-5 | 是否可能因无关工具失败错误拦截当前调用 | `execution_control.py:616`（R1 批内 `_batch_unsafe`）：同批任何非成功观察都会拦截后续 commit 类调用 | 规则文本即如此；跨轮次只持久化就绪条件（`_conditions`），批内信号随 `begin_batch()` 清除 | **部分实现（已知偏差）** | 同批无关工具失败会拦截同批后续 commit 调用（保守偏严） | 保持（保守安全边界），在设计契约与协议中标注为已知误拦截来源，纳入"合法操作误拦截率"指标 |
| B1-6 | 已成功副作用调用的重复执行是否受约束 | `execution_control.py:636` R3 | `test_side_effect_success_repeat_requires_idempotency` | **已实现** | 无 | 无 |
| B1-7 | 合法只读轮询是否被误判为空转 | `execution_control.py:636` R2 规则 2 对**任何**相同重复生效，包括成功只读轮询（第 3 次相同即拒绝） | 无测试覆盖成功轮询；现行规则会误杀 | **缺失（误判风险）** | 违反"合法轮询不会因单纯重复返回内容就被误杀" | R2 循环规则限定为非成功（affects_followups）结果；成功只读轮询豁免 |
| B1-8 | REPLAN 是否只是可审计决策，还是有可靠再规划/再授权链路 | 不存在 | 见 B1-2 | **缺失** | — | REPLAN 作为路线枚举 + 决策事件；落地映射到 RETRY_WITH_CONTEXT（模型重规划），新调用仍经执行授权门 |
| B1-9 | 预算耗尽 / 无法继续 / 需升级 / 正常停止能否明确区分 | 预算耗尽：`BudgetExhausted` → `TOKEN/TURN/MODEL_CALL_BUDGET_EXHAUSTED`（trial_executor.py）；升级/停止：均置 `_escalation_flag`，事件为 ESCALATION | `core._escalation_flag` 单布尔；STOP 与 ESCALATE 下游消息同为 "TERMINATED:" | **部分实现** | STOP 与 ESCALATE 事件可区分（reason），但终止类别未结构化 | 增加 `termination_kind`（STOP / ESCALATION / BUDGET_EXHAUSTION）与不同 reason_code |
| B1-10 | 被拒绝的重试是否错误消耗恢复预算 | `agent_core.py:841`（retry_justified is False 时跳过预算门） | 同 B1-1 测试 | **已实现** | 无 | 无 |
| B1-11 | 跨模型轮次状态是否持续并在新证据后正确更新 | `execution_control.py:809`（_update_conditions）：`*_ready` true 清除 / false 置位 | `test_not_ready_state_persists_across_model_turns` | **已实现** | commit 回执不提供就绪证据（设计如此）| 无 |

## B2. 实验归因

| # | 问题 | 当前实现位置 | 证据 | 状态 | 风险 | 必要修改 |
|---|------|-------------|------|------|------|---------|
| B2-1 | 调用能否关联 tool_call_id / Provider ID / Run / Trial / Step / 工具名 | 控制事件含 step/tool_name/tool_call_id/provider_tool_call_id | `control_events.json`（新产物） | **部分实现** | 缺 run_id / trial_id / 批次序号 | gate 增加 bind_identity(run_id, trial_id)；事件补 run_id/trial_id/batch_index |
| B2-2 | 拦截 / 合成 / 真实执行 / 真实结果可区分 | `_synthetic_response` 标记（agent_core.record_blocked_tool_call）；BLOCKED 事件；观察/证据/轨迹均不含被拦截调用 | `test_blocked_calls_only_in_control_events_not_execution_records` | **已实现** | 无 | 无 |
| B2-3 | 控制事件能否关联到具体恢复决策 | RECOVERY 事件含 step/tool_name/action/reason | 无 decision_id | **部分实现** | 无法与 recovery_decisions.json 条目一一对应 | 增加 decision_id 并同步写入 recovery_decisions 条目 |
| B2-4 | 是否记录决策依据 / 状态变化 / 预算变化 / 最终结果 | RECOVERY 事件有 retry_justified/budget_decision；无状态摘要、无预算前后、无 judge 结果 | — | **部分实现** | — | 事件补 state_change/evidence_grade/state_summary/budget_before/after/model_calls_used；trial_manifest 补 judge 摘要已有（native_judgement.json 分文件） |
| B2-5 | control_events.json 是否覆盖受测执行路径 | canonical：execute_trial（canary_runner/real_pilot_runner 持久化） | `canary_runner.py`、`real_pilot_runner.py` `_write_json_once(... "control_events.json")` | **已实现（canonical）** | 非 canonical 路径缺持久化 | 见 B2-6 |
| B2-6 | 非 canonical 路径是否缺事件持久化或实验身份 | `runtime_backend.py:373`（finalize 返回裸 TraceLogger dict） | 无 run/trial 身份、无控制事件、无实验标识 | **缺失** | 混入 canonical 统计将污染归因 | 最小改造：sealed artifact 标注 `execution_path_kind=NON_CANONICAL_RUNTIME_BACKEND` + `included_in_canonical_analysis=False`，并明确排除出本轮测评范围 |
| B2-7 | Manifest 能否区分授权策略/恢复策略/运行器版本/任务数据/Provider/Judge 版本 | manifests：schema_version、manifest_hash、base_exact_head、benchmark provenance（toolmaze commit/digest）、provider_config_hash、root_budget；preflight：code_sha | `experiments/phase5/manifests/*.json`；`live_preflight.json` | **部分实现** | 缺 execution_control/route 策略版本、事件 Schema 版本、引擎轮次护栏语义 | trial_manifest 增加 `execution_provenance` 块；preflight 增加 `execution_control_provenance`；协议预注册清单要求未来 manifest 引用之 |
| B2-8 | 逐调用交付导致的引擎轮次语义变化是否有 provenance | `trial_executor.py`（engine_round_guard = max_turns + max_model_calls）仅代码注释 | — | **缺失** | 后续对轮次/成本口径误读 | execution_provenance 记录 `engine_round_guard` 与语义说明 |
| B2-9 | 合成响应是否可能被统计成真实调用/成功 | 真实执行统计来自官方 trace（engine 记录）、public_tool_observations（仅真实）；合成只入对话历史 | `test_blocked_calls_only_in_control_events_not_execution_records` | **已实现** | 无 | 补一项事件级测试：SYNTHETIC 不进 EXECUTED/OBSERVED 事件 |
| B2-10 | 分析工具能否分开任务失败与基础设施无效 | `TrialResult.validity`（VALID / INVALID_INFRA）+ termination_reason 分类（trial_executor.py:380-401）；artifacts.classify_trial | 测试 `test_phase5_validity.py` 等 | **已实现** | 无 | 协议中固定有效性定义 |

## 历史产物只读归因验证（Canary-005/006）

对 `experiments/phase5/runs/phase5-real-canary-005-r2/-006-r2` 只读检查：

- 可重建：工具调用顺序与参数摘要（public_tool_observations.json）、
  恢复决策（recovery_decisions.json：005 有 3 条 / 006 空）、
  验证器事件（validator_events.json）、预算账本（recovery_budget_ledger.json）、
  成本（provider_usage.json / budget_ledger.json）、最终判定
  （native_judgement.json + validity.json）。
- **不可重建（UNATTRIBUTABLE，禁止补造）**：逐调用授权事件（PROPOSED/
  AUTHORIZED/BLOCKED）——该机制在 04ebe293/973a612 才引入，历史运行无此账本；
  被拦截调用在历史数据中与正常未提议调用不可区分；路线决策（route）字段不存在。
- 结论：历史产物可用于成本/结果/恢复次数归因；不可用于授权拦截归因。
  分析逻辑对缺失字段必须输出 `UNATTRIBUTABLE`，不得推断。

## 结论 → 最小改造清单（阶段 C/D）

1. 新增 `recovery_route.py`（路线枚举 + 确定性决策器）——B1-2/B1-8 缺独立决策边界。
2. `execution_control.py`：StateChange 证据分级（B1-3）、轮询豁免（B1-7）、
   事件身份/版本字段（B2-1/B2-7）。
3. `agent_core.py`：路线决策接入两个恢复入口，decision_id 关联（B2-3），
   termination_kind（B1-9），事件补证据/预算字段（B2-4）。
4. `trial_executor.py`：execution_provenance（B2-7/B2-8）。
5. `runtime_backend.py`：非 canonical 标记与排除（B2-6，选"排除"方案）。
6. 测试补齐（阶段 E 清单）。
7. 测评协议 + 预注册（阶段 F）。

已完整实现的机制（B1-1/6/10/11、B2-2/9/10）不重复实现，仅补测试证据。
