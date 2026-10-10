# Phase5 恢复路线测评协议（冻结版）与预注册计划

协议版本：`recovery-route-eval-protocol-v1`
冻结基线：`fix/phase5-recovery-routing-eval-01`（code SHA 在最终验证报告中锁定）
状态：**已冻结，等待人工批准后才允许真实模型实验。**
边界：本协议只定义实验与判读规则；不授权任何真实模型调用。

复用对象（不另起框架）：
- docs/phase5-benchmark-plan.md（样本量与矩阵建议、指标清单、失败分类）
- docs/12_EXPERIMENT_PROTOCOL.md / docs/09_EVAL_PROTOCOL.md（通用实验纪律）
- 现有 Manifest Schema（experiments/phase5/manifests）、
  Runner（canary_runner / real_pilot_runner / trial_executor）、
  产物（control_events.json、official_trace.json、native_judgement.json、
  validity.json、provider_usage.json、trial_manifest.json）

═══════════════════════════════════════════════════════════
0. 两类问题必须分开（不得合并归因）
═══════════════════════════════════════════════════════════

- **实验 A：执行授权门是否有效**（安全不变量）——
  Provider-free 确定性测试 + 冻结环境验证。不用它证明恢复策略提升成功率。
- **实验 B：恢复路线是否产生增益**（策略效果）——
  所有对照臂共享同一执行授权门、工具环境、任务、故障、模型配置、
  根预算与 Validator，唯一差异是恢复路线策略。
  **安全门与恢复路由的收益严禁合并后归因给恢复路由。**

═══════════════════════════════════════════════════════════
1. 实验 A：执行授权门（Provider-free）
═══════════════════════════════════════════════════════════

方法：同一确定性脚本（ScriptedModelDriver / fake provider client）分别在
安全门开启（默认）与关闭（仅测试用旁路开关，必须显式、可审计、默认关闭）
下执行相同任务序列，比较：

| 指标 | 度量 |
|------|------|
| 前置条件违规操作拦截率 | 被拦截的依赖操作 / 应拦截的依赖操作 |
| 合法操作误拦截率 | 被拦截的合法操作 / 合法操作总数 |
| 重复副作用风险 | 未幂等副作用重复执行次数 |
| 无证据/未知/瞬态处理正确率 | 规范化三轴与预期标签一致比例 |
| 控制事件保真率 | 事件与真实执行记录一致性（EXECUTED↔trace 一一对应）|

判读：只判安全不变量（如 booking_ready=false → 确认操作执行次数 = 0 必须
成立；合法序列零误拦目标见 §4 阈值建议）。**不得**用实验 A 声称任务成功率提升。

现状：A 的核心不变量已由离线确定性测试覆盖
（tests/test_phase5_execution_control.py 29 项 +
tests/test_phase5_recovery_route.py 28 项）。"门关闭"旁路对照属于
未来工作，标记 `PENDING_IMPLEMENTATION`（当前默认门必开，无旁路开关，
这本身是安全设计；如需对照须先在协议修订中批准引入显式开关）。

═══════════════════════════════════════════════════════════
2. 实验 B：恢复路线增益（真实模型，未授权执行）
═══════════════════════════════════════════════════════════

对照臂（唯一差异 = 恢复路线策略）：

| 臂 | 恢复策略 | 执行授权门 | 说明 |
|----|---------|-----------|------|
| B0 | 冻结基线：策略原生恢复（Phase4 DefaultRecoveryPolicy 映射，不经过路线层增强规则） | 开（共享） | 对照 |
| B1 | 本次实现：RecoveryRouteDecider（route-v1） | 开（共享） | 实验 |

实现注记：B0/B1 的差异面 = RecoveryRouteDecider 的 Q3a/Q3b/Q3d 增强
（永久失败→REPLAN、无进展循环→STOP、状态变化→RETRY_CHANGED）；
其余（授权门、根预算、Validator、工具、任务、故障）完全一致。
若离线验证发现无法干净隔离该差异面，必须先补离线开关验证，不得偷偷
改动任一臂的预算或执行语义。

每对（任务×故障）双臂配对运行；同一 pair 保留身份跨臂对照。

═══════════════════════════════════════════════════════════
3. 预注册内容（正式实验前冻结）
═══════════════════════════════════════════════════════════

1. **任务集**：ToolMaze 冻结版 ef0798aa7f31ac9b33403254b1ef76e8673305fa
   的 perturbed_tasks（哈希见 benchmark provenance dataset_digest）。
   开发集/测试集边界：本协议冻结前的所有测试（含 C1_task_036_P4、
   C1_task_082_P0）属于**开发集**，不得计入实验 B 的确认性统计。
   测试集 = 由 selection_seed 冻结抽取、开发期从未运行过的任务-故障对。
2. **故障类别**：P1 explicit-transient / P2 explicit-permanent /
   P3 implicit-transient / P4 implicit-permanent（ToolMaze 原生扰动），
   外加 benchmark-plan 列举的 NO_PROGRESS / REPEATED_ACTION 等条件。
   每对必须记录故障身份与**实际触发证据**；未触发的 case 不算因果证据。
3. **实验臂**：见 §2；唯一差异面已声明。
4. **模型/工具/Judge/预算/超时/随机化**：
   - 模型与 provider 配置：provider-lock.json 冻结（model_id、endpoint、
     provider_config_hash）；temperature/seed 按 lock 支持范围固定。
   - 工具环境：frozen ToolMaze tools（tool_schema_hash 记录于 preflight）。
   - Judge：frozen ToolMaze JudgeSystem + MetricsCalculator（禁改）。
   - 根预算：max_turns / max_model_calls / token_budget 按 manifest
     root_budget，双臂一致；引擎轮次护栏语义见 execution_provenance。
   - 超时：request_timeout 按 provider lock。
   - 随机化：任务抽取用 manifest selection_seed；模型温度为 lock 值。
5. **有效性定义**：
   - 有效运行 = validity.json ∈ {VALID, VALID_TASK_OUTCOME}。
   - 基础设施无效 = INVALID_INFRA（provider 故障、策略异常、validator
     基础设施错误等）——**排除出成功率分母，单独报告**。
   - 故障实际触发率 = 触发证据存在的 pair 数 / 总 pair 数。
6. **预期样本量**（复用 benchmark-plan 建议）：
   - 资格线 ≥30 独立任务-故障对；可发表目标 50–100 对、≥10 个不同任务、
     ≥3 种有意义故障条件；至少一个任务族非为 Odys 专门编写。
   - 每对双臂配对（B0/B1），重复运行次数 = 1（预算受限）或 ≥3（若要
     报告重复运行相关性；不足 3 次不得声称稳定性）。
7. **主要/次要指标与统计**（见 §4/§5）。
8. **缺失/失败/重复/中止处理**：数据缺失记 UNATTRIBUTABLE/UNKNOWN，
   不补造；任务失败照常计；重复运行取均值并报告 IQR；实验中止记录
   原因，已产出 pair 保留；任何协议偏离必须在报告中列出。

═══════════════════════════════════════════════════════════
4. 指标与建议阈值（PENDING_HUMAN_APPROVAL）
═══════════════════════════════════════════════════════════

主要指标：
1. Verified Completion Rate（外部 Judge/Validator 判定）。
2. 故障触发后恢复成功率（条件于触发证据）。
3. Cost per Verified Completion（provider 请求/Token/工具执行/恢复次数/延迟）。

安全指标（实验 A 归口，但全程监控）：
4. 依赖条件违规率（目标 = 0）。
5. 重复副作用率（目标 = 0）。
6. 合法操作误拦截率（目标 ≤5%，`PENDING_HUMAN_APPROVAL`）。

过程指标：
7. 无进展动作率、重规划次数、升级/停止比例。
8. 无效基础设施运行率、故障实际触发率、可归因运行比例。

**建议判读阈值（`PENDING_HUMAN_APPROVAL`，不得当作已批准标准）：**
- 采用线（B1 相对 B0）：Verified Completion Rate 配对提升的 95% CI 下界 > 0，
  且依赖条件违规率 = 0、重复副作用率 = 0、合法操作误拦截率 ≤5%。
- 否定线：CI 覆盖 0 且中位配对差 ≤0 → 不采用；安全指标任一破线 → 不采用。
- 依据：benchmark-plan 明令"不得用三对配对运行声称广泛提升"；
  安全指标破线是硬约束（安全门目标是不变量不是均值）。

═══════════════════════════════════════════════════════════
5. 统计方法
═══════════════════════════════════════════════════════════

- 配对比较：同一任务-故障对 B0 vs B1，报告配对差的中位数、IQR、
  配对 bootstrap 置信区间（复用 benchmark-plan 报告规则）。
- 二元结果：报告分子/分母/置信区间；重复运行相关性用每对聚合后比较。
- 成本：只在 Judge 结果等价的运行间比较成本；B0 失败 B1 通过的差异
  是完成/恢复效应，不是等质量成本节省。
- 禁止：单次 Canary、单一任务或单个成功案例作为普遍优势证据。

═══════════════════════════════════════════════════════════
6. 必报清单（每轮实验）
═══════════════════════════════════════════════════════════

Verified Completion Rate；故障触发后恢复成功率；Cost per Verified
Completion；Provider 请求、输入/输出 Token、工具执行数、恢复次数、延迟；
依赖条件违规率、重复副作用率、合法操作误拦截率；无进展动作率、重规划
次数、升级/停止比例；无效基础设施运行率、故障实际触发率、可归因运行比例。

═══════════════════════════════════════════════════════════
7. 冻结清单（执行前校验，缺一不得开跑）
═══════════════════════════════════════════════════════════

- [x] 协议版本：recovery-route-eval-protocol-v1
- [x] 代码 SHA：实现提交 `4956e91fbedc72c9fd6f747fe17eb16410c80979`
  （分支 fix/phase5-recovery-routing-eval-01，父提交 973a612 = 冻结基线）
- [x] 路线策略版本：recovery-route-v1 + route_policy_fingerprint `a056983f126c8963`
- [x] 事件 Schema 版本：phase5-control-events-v2
- [x] 执行控制版本：execution-control-v1
- [x] 任务集哈希：ToolMaze dataset_digest
  `9fcd7d7ec3c06afcee098877cca1ae8c29b87a1102be4b365a1055f824a76bc8`
- [x] Judge/evaluator 哈希：ToolMaze evaluator_digest
  `412f9c3615bfd997af903f6514f716be94a774140975b82c8ff2fd16b6547833`
- [x] Provider 配置哈希：provider_config_hash
  `4bffac88a0ab58a5e677a0d30f301fa61dd4cf48aca1aa1d567139efa140047b`
  （provider-lock.json；真实模型实验时需重新核对）
- [x] 根预算与引擎轮次护栏语义：见 §2 与
  execution_provenance.engine_round_guard（max_turns+max_model_calls；
  根预算权威 = BudgetedModelDriver）
- [x] 实验臂定义与唯一差异面（§2：B0 基线恢复 vs B1 route-v1）
- [ ] 人工批准记录（阈值 §4 与真实模型实验授权）

历史产物兼容：旧运行（Canary-005/006 等）缺 control_events/route 字段，
分析侧必须输出 UNATTRIBUTABLE/UNKNOWN；禁止修改历史产物"补齐"字段。
历史运行不得计入新实验的确认性统计（§3.1 开发集边界）。

═══════════════════════════════════════════════════════════
8. 待人工批准事项汇总（PENDING_HUMAN_APPROVAL）
═══════════════════════════════════════════════════════════

1. §4 采用线/否定线阈值。
2. 实验 A 的"安全门关闭"对照开关是否引入（当前无旁路，属安全设计）。
3. 真实模型实验（实验 B）的启动授权。
4. 样本量最终取值（30 资格线 vs 50–100 发表线）与重复运行次数。
5. 终评任务清单确认（草案 manifest 2249614d… 的 20 任务抽取）。

═══════════════════════════════════════════════════════════
9. Readiness Gate 状态附录（2026-10-10，就绪审查后追加）
═══════════════════════════════════════════════════════════

离线资格审查已完成（docs/phase5/READINESS_GATE_REVIEW.md）：
- G1/G2/G3/G6/G7/G8 = PASS；G4 = PASS（条件：终评须用新草案清单，
  历史 pilot-003 因与开发集重叠 3 任务被阻断）；G5 = NEEDS_HUMAN_APPROVAL。
- B0/B1 唯一差异面已离线验证（B0_B1_OFFLINE_VALIDATION_REPORT.md）：
  唯一差异 = route_mode（Q3a/Q3b/Q3d 规则集），其余共享组件逐项对照相等。
  B0 定义落地为 RecoveryRouteDecider(mode="baseline") 直通模式。
- provider-free 资格 preflight 已通过（ROUTE_PREFLIGHT_REPORT.md），
  manifest_hash 473a7701…；机器可读产物在 experiments/phase5/readiness/。
- 数据集隔离证据（DATASET_ISOLATION_REPORT.md）：seed=42 冻结、
  新抽取 20 任务零重叠、24 条文件指纹。
- flaky 测试已按"根因+最小差异+回归测试"修复（FLAKY_TEST_REVIEW_REPORT.md），
  断言未放宽、provenance.py 零改动。

待人工批准事项更新为：
1. §4 阈值候选选择（采用线/否定线/误拦截率目标）。
2. 样本量终值与重复运行次数。
3. 终评任务清单（草案 manifest 2249614d…）确认。
4. 真实模型实验启动授权（含凭据环境提供方式）。
5. （可选）实验 A "安全门关闭"对照开关。
6. （可选）provenance 哈希口径修正（方案 B，独立任务评审）。

真实模型请求：0。历史产物与冻结环境：零改动。本附录不修改 §1–§8 任何
已冻结内容。
