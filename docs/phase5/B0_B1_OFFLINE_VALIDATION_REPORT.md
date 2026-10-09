# B0/B1 唯一差异面离线验证报告

日期：2026-10-10｜对应协议 §2｜测试：tests/test_phase5_route_experiment_readiness.py

═══════════════════════════════════════════════════════════
1. 策略定义
═══════════════════════════════════════════════════════════

两臂共享：恢复策略（recovery_strategy_arm = A3_ODYS_FULL，
Phase4 DefaultRecoveryPolicy 桥接）、执行授权门（ExecutionAuthorizationGate，
R0–R3）、结果标准化（ToolResultNormalizer 三轴）、恢复预算门
（RecoveryBudgetGate）、根预算（BudgetedModelDriver）、任务输入、
工具环境、Judge/Validator 口径、事件 Schema。

唯一允许变化的因素 = **route_mode**（路线规则集）：

| 臂 | route_mode | 路线规则 |
|----|-----------|---------|
| B0_BASELINE_ROUTE | `baseline` | Q0/Q1/Q2 保持；**Q3a/Q3b/Q3d 关闭**；策略提议直通（retry→RETRY_SAME/STRATEGY_PASSTHROUGH，retry_with_context→RETRY_CHANGED/STRATEGY_CONTEXT_CHANGE） |
| B1_ROUTE_V1 | `route-v1`（默认，冻结行为） | 全规则集：Q3a 永久失败→REPLAN、Q3b 无进展循环→STOP/REPLAN、Q3d 状态变化→RETRY_CHANGED、Q3e 有界验证 |

实现接缝（最小）：recovery_route.py `RecoveryRouteDecider(mode=…)`、
agent_core.py `set_route_mode()`、trial_executor.py `execute_trial(route_mode=…)`。
默认值 route-v1 —— 不改变任何现有行为（既有 29+28 项测试断言未动、全过）。

═══════════════════════════════════════════════════════════
2. 验证结果（全部真实执行，2026-10-10）
═══════════════════════════════════════════════════════════

命令：`uv run pytest tests/test_phase5_route_experiment_readiness.py -q`
→ **12 passed（8.54s）**

| # | 验证项 | 方法 | 结果 |
|---|--------|------|------|
| V1 | B1 默认行为不变 | 默认 decider 产生 REPLAN（Q3a） | PASS |
| V2 | B0 关闭 Q3a | 永久失败+retry 提议 → RETRY_SAME/STRATEGY_PASSTHROUGH（无 REPLAN） | PASS |
| V3 | B0 关闭 Q3b | 相同结果循环 → 直通（对照 B1 → NO_PROGRESS_LOOP/STOP） | PASS |
| V4 | B0 关闭 Q3d | 观察到状态变化 → 直通（对照 B1 → STATE_CHANGED/RETRY_CHANGED） | PASS |
| V5 | B0 上下文变更提议直通 | retry_with_context → RETRY_CHANGED/STRATEGY_CONTEXT_CHANGE | PASS |
| V6 | B0 保持终止安全 | terminal→STOP、策略 escalate→ESCALATE、stop→STOP | PASS |
| V7 | 非法模式拒绝 | mode="experimental-v2" → ValueError | PASS |
| V8 | 唯一差异面（同场景逐字段对照） | 执行序列/最终答案/预算账本/PROPOSED/AUTHORIZED/EXECUTED/OBSERVED/BLOCKED 事件全部相等；仅 RECOVERY 事件的 route 字段不同（BOUNDED_VERIFICATION vs STRATEGY_PASSTHROUGH） | PASS |
| V9 | 差异面即实验变量 | 永久失败场景：B1=REPLAN→模型重规划 tool_b 执行；B0=直通→**共享门**拒绝相同重放（NO_PROGRESS_RETRY）→停止。无绕过 | PASS |
| V10 | 授权门两模式一致 | booking 场景两模式执行/拦截完全相同（confirm 拦截规则一致） | PASS |
| V11 | REPLAN 产出仍经授权门 | B1 重规划批次的依赖调用照样被拦 | PASS |
| V12 | canonical 执行器开关生效+无泄漏 | execute_trial（fake client，冻结 C1_task_036_P4）双臂对照：execution_provenance.route_mode 分别 route-v1/baseline；其余（derived_view、validity、指纹、护栏、judge 指标、provider 请求次数、控制事件流除身份戳外）逐项相等；两臂 provider 请求均恰 2 次 | PASS |

═══════════════════════════════════════════════════════════
3. 配置泄漏 / 默认值覆盖 / 绕过排查
═══════════════════════════════════════════════════════════

- 默认值：execute_trial(route_mode="route-v1") = 冻结行为；B0 必须显式选择
  → 无静默覆盖。
- 配置泄漏：V8/V12 逐字段对照证明共享组件输出相等；route_mode 只出现在
  decider 与 execution_provenance.route_mode。
- 绕过授权门：V9/V11 证明两模式下门都在工作（B0 的直通重放被门拒绝，
  B1 的 REPLAN 产出被门检查）；无任何路径可跳过 gate.propose。
- 非法输入：V7 拒绝未知模式（fail-closed）。
- 策略混入：两臂 recovery_strategy_arm 同为 A3_ODYS_FULL；A0–A5 的
  默认语义未被 route_mode 触碰（既有 458 项测试断言未动全过）。

═══════════════════════════════════════════════════════════
4. 保存的可复核证据
═══════════════════════════════════════════════════════════

- 测试代码：tests/test_phase5_route_experiment_readiness.py（含全部
  场景脚本与逐字段对照逻辑，可独立重跑）。
- 配置证据：experiments/phase5/manifests/phase5-route-experiment-B0B1-draft-001.json
  （route_arms 声明、unique_difference_surface 字段、manifest_hash 自校验）。
- 事件证据：测试内嵌断言覆盖事件流对照；真实实验运行时每 trial 的
  control_events.json + execution_provenance 将落盘同类证据。

═══════════════════════════════════════════════════════════
5. 限制（不影响本 Gate 结论，登记在案）
═══════════════════════════════════════════════════════════

1. 离线验证用脚本化策略与 fake client；真实模型下的策略切换行为属于
   实验 B 本身（未执行，需批准）。
2. real_pilot_runner 的 run_pilot 尚未接 route_mode 分发（实验执行前
   需要一步 runner 接线 + 复测；不影响当前唯一差异面证明）。
3. B0 的"基线"定义 = 关闭 Q3a/Q3b/Q3d 增强的直通模式（协议 §2 口径）；
   若人工审定认为基线应含 Q3b（无进展停止），需修订协议 §2 后重新验证。

结论：**唯一差异面可证（G2 PASS）**；切换生效、无泄漏、无绕过。
