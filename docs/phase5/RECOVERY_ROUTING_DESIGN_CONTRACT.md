# Runtime 恢复路线设计契约（Recovery Route Design Contract）

版本：`recovery-route-v1`（ROUTE_POLICY_VERSION）
基线：`fix/phase5-execution-control-01` @ `973a612`；本契约实现在
`fix/phase5-recovery-routing-eval-01`。

## 1. 目标与边界

恢复路线决策回答一个问题：**基于真实执行证据，下一步应该走哪条路线，为什么。**

边界：
- 路线决策 ≠ 预算判断。两层分离、分别记录（§5）。
- REPLAN 只是可审计的决策结果：落地为"带失败上下文要求模型重新规划"，
  新产生的每个工具调用仍必须通过现有执行授权门（ExecutionAuthorizationGate）。
  不自动调用任何新工具。
- ESCALATE 与 STOP 是两个不同状态（§3）。
- 不新增 LLM 决策调用；全部规则确定性、有限、可解释。
- 不改变 BudgetedModelDriver 根预算权威；不改变 Token/Provider 调用/轮次口径。

## 2. 路线集合（冻结）

| 路线 | 含义 | 落地映射（现有机制） |
|------|------|---------------------|
| `CONTINUE` | 有可信进展，继续执行 | 无挂起恢复，模型继续（RecoveryActionKind.NONE）|
| `RETRY_SAME` | 有明确瞬态依据或其他充分证据，重试原动作 | RecoveryActionKind.RETRY（原调用重放）|
| `RETRY_CHANGED` | 参数/前置条件/相关状态已发生足以改变决策的变化，尝试修改后的动作 | RecoveryActionKind.RETRY_WITH_CONTEXT（失败上下文 + 模型调整动作）|
| `REPLAN` | 当前路线被证据否定或无法推进，需要重新规划 | RecoveryActionKind.RETRY_WITH_CONTEXT + 路线标签 REPLAN（模型重规划，新调用经授权门）|
| `ESCALATE` | 当前层无法安全决策，带证据交回上层控制机制 | RecoveryActionKind.ESCALATE → termination_kind=ESCALATION |
| `STOP` | 达到终止条件，不再继续 | RecoveryActionKind.STOP → termination_kind=STOP |

与既有词汇映射：RETRY↔RETRY_SAME；RETRY_WITH_CONTEXT↔RETRY_CHANGED/REPLAN
（按路线标签区分）；RecoveryActionKind 保持不变（不破坏 Phase4 桥接与既有测试）。

## 3. ESCALATE ≠ STOP

| 维度 | ESCALATE | STOP |
|------|----------|------|
| 语义 | 未解决问题带证据上交，等待上层/人工 | 终止条件已满足，正常/明确结束 |
| reason_code | `*_ESCALATED` | `*_STOP` |
| termination_kind | `ESCALATION` | `STOP` |
| 预算 | 不再消耗 | 不再消耗 |

## 4. 决策输入（全部来自真实证据，禁止伪造）

| 输入 | 来源 | 证据等级 |
|------|------|---------|
| 调用稳定签名（tool_name + arguments_digest） | execution_control 尝试历史 | DERIVED |
| 上次结果规范化三轴（exec/ready/retryable） | ToolResultNormalizer | DIRECT（字段）/ UNKNOWN（缺证据）|
| 前置条件及证据等级 | gate.conditions（*_ready 值） | DIRECT / ABSENT |
| 状态变化 StateChange | gate.evaluate_state_change() | 见 §6 |
| 瞬态依据 transient_basis | 显式 retryable/transient 标记或瞬态错误类型 | DIRECT |
| 恢复预算剩余 | RecoveryBudgetGate.remaining | DIRECT |
| 执行预算（model calls/turns used） | BudgetedModelDriver 计数 | DIRECT |
| 是否已终止/安全拦截 | gate.is_terminal / core 标志 | DIRECT |

## 5. 两层判断（路线 vs 预算）

1. **决策层**（RecoveryRouteDecider）：选择路线 + 原因码 + 证据 + 状态变化。
2. **预算门**（RecoveryBudgetGate / PassThrough）：该路线是否获准执行。

规则：
- 预算不足记录为 `budget_decision=ESCALATE/DENY`，**不得**伪装成"重试不合理"。
- 路线合理**不得**绕过预算门。
- 被路线层拒绝的重试不消耗恢复预算（budget_decision=NOT_CONSUMED）。

## 6. StateChange 语义（关键）

| 值 | 含义 | 判定 |
|----|------|------|
| `OBSERVED_CHANGE` | 有可信证据表明状态变化 | 两次观察间就绪条件/显式状态字段发生可观察差异 |
| `NO_OBSERVED_CHANGE` | 有状态证据且未观察到变化 | 工具结果暴露状态证据（readiness 键或显式 status）且摘要一致 |
| `UNKNOWN` | 状态变化不可观测 | 工具结果不暴露任何状态证据 |

铁律：**"没有观察到变化" ≠ "状态没有变化"。** 相同返回字符串不构成环境状态
不变的证据。无状态证据时一律 `UNKNOWN`，禁止伪造无进展证据。
无进展判定的合法依据是"无新证据"（结果摘要相同 + 无瞬态依据 + 无可信进展），
不是"环境确认未变"。事件中两者分别记录。

## 7. 路线规则（优先级从高到低，确定性）

- **Q0 终止/安全**：已终止或安全条件不满足 → `STOP`（TERMINAL_STATE）。
  禁止继续相关动作。
- **Q1 策略终态**：策略 ESCALATE → `ESCALATE`（STRATEGY_ESCALATE）；
  策略 STOP → `STOP`（STRATEGY_STOP）。
- **Q2 无恢复提议**：→ `CONTINUE`（STRATEGY_NONE）。
- **Q3 重试族提议**（RETRY / RETRY_WITH_CONTEXT）：
  - a. 上次失败为 `NOT_RETRYABLE`（永久失败）：
     首次 → `REPLAN`（PERMANENT_FAILURE_REPLAN）——不重复同动作，交模型重规划；
     该签名已 REPLAN 过 → `ESCALATE`（REPLAN_EXHAUSTED）。
  - b. 无进展循环（≥2 次相同非成功结果、无新证据、无瞬态依据）：
     → `STOP`（NO_PROGRESS_LOOP）——禁止无限重复。
     （重规划/升级/停止三者中取停止：与既有冻结语义兼容，
     见审计矩阵 B1-3 与执行控制交付测试。）
  - c. 明确瞬态依据（transient_basis）→ `RETRY_SAME`（TRANSIENT_BASIS）。
  - d. 可信变化证据（StateChange=OBSERVED_CHANGE）→ `RETRY_CHANGED`（STATE_CHANGED）。
  - e. 其余（单次非永久失败、依据不确定）→ `RETRY_SAME`
     （BOUNDED_VERIFICATION，有界验证重试，仅一次）。
- **Q4 预算不足或状态无法安全判定**：由预算门输出 ESCALATE/DENY →
  `ESCALATE`（BUDGET_EXHAUSTED）/ `STOP`。不得绕过。

合法性说明（与既有语义冲突处理）：Q3-b 取 STOP 而非 REPLAN，因为既有
执行控制交付已冻结"无进展重试 → 明确停止/升级"语义且有测试锚定；本契约
选择两者交集（STOP ∈ {重规划, 升级, 停止}），最小兼容。

## 8. 预算语义（不变）

- 根预算权威：BudgetedModelDriver（max_turns / max_model_calls / token_budget）。
- 恢复预算：RecoveryBudgetGate（A3/A4 active，A5 pass-through）。
- 引擎轮次护栏：max_turns + max_model_calls（逐调用交付语义，见 provenance）。
- 新增：路线决策记录预算前后快照（recovery_budget_before/after、
  model_calls_used），只读快照，不改变任何口径。

## 9. 事件与归因

每个恢复决策在 RECOVERY 控制事件中至少记录：
`decision_id, run_id, trial_id, step, tool_name, tool_call_id,
provider_tool_call_id, action_signature(arguments_digest), route, reason_code,
rationale, evidence, evidence_grade, state_change, state_summary,
action(mapped RecoveryActionKind), retry_justified, budget_decision,
recovery_budget_before, recovery_budget_after, model_calls_used, authorized`。

语义铁律：
- BLOCKED ≠ 已执行；SYNTHETIC ≠ 环境回执；AUTHORIZED ≠ 最终成功。
- `status=confirmed` ≠ 任务完成（任务完成以外部 Judge/Validator 为准）。
- 模型请求数、工具执行数、恢复次数、Token、费用分别计数。

事件 Schema 版本：`phase5-control-events-v2`（新增字段全部向后兼容可选）。
旧产物缺失字段在分析侧输出 `UNATTRIBUTABLE`。

## 10. 边界条件

- 轮询豁免：成功（affects_followups=False）只读重复**不**触发无进展循环规则；
  成功副作用重复由幂等规则（R3）约束。
- 批内无关失败对 commit 类调用的保守拦截保持（已知误拦截来源，进指标）。
- REPLAN/RETRY_CHANGED 产生的动作全部经执行授权门，与普通调用同权。
- 路线决策器无 LLM、无随机、无网络。
