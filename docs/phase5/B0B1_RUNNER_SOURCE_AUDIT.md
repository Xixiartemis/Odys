# B0/B1 正式实验接线 · Runner 源码审查（问题矩阵）

日期：2026-10-10
分支：`feat/phase5-b0b1-experiment-readiness`（基线 = origin/main = eb6a53c，PR #37 merge commit）
范围：正式 B0/B1 双臂实验接线前的源码审查。**只审查与记录，本文件不含运行结果。**

═══════════════════════════════════════════════════════════
1. 调用链（实际代码，非设计文档）
═══════════════════════════════════════════════════════════

```
RealPilotRunner.run_pilot()                        real_pilot_runner.py:468
 ├─ _load_manifest()            manifest_hash 校验  :383
 ├─ _resolve_manifest_units()   任务身份解析        :419
 ├─ 调度表 arm_schedule.json（seed 轮转）           :503-510
 └─ for unit × arm:
      trial_id = f"{experiment_id}_{task_id}_{arm}"  :526   ← 身份公式（P0-2）
      model_driver = model_driver_factory()          :537   （每 trial 新驱动）
      execute_trial(arm, task_json, ...,             :542   ← canonical 入口
                    max_rounds=max_rounds)                  ← 未传 route_mode（P0-1）
         trial_executor.execute_trial()             trial_executor.py:158
          ├─ BudgetedModelDriver 包装（根预算唯一权威）  :192
          ├─ strategy = _STRATEGY_MAP[arm]()          :200   （A0–A5 策略实例）
          ├─ RecoveryBudgetGate / PassThrough         :210-217
          ├─ ExecutionAuthorizationGate（共享安全门）  :262-264
          ├─ core.set_route_mode(route_mode)          :278   （Experiment B 接缝）
          ├─ ExecutionEngine.run()（唯一执行引擎）     :282-314
          ├─ core.get_recovery_decisions()            :367
          ├─ execution_provenance（含 route_mode）     :136-155, 385-388
          └─ _run_offline_grader()（冻结 Judge/Metrics）:457
      ExperimentPairValidator.compute_trial_invariants :596
      ExperimentPairValidator.validate()              :636   （配对校验）
      _write_trial_artifacts()  create-only + COMPLETE :726
 └─ experiment_manifest.json / paired_manifests/     :662-701
```

canonical 约束（现状满足）：
- execute_trial 是唯一 live 执行入口（canary_runner 同样调用它，canary_runner.py:44）。
- run_pilot 不自建 Core/Engine/Strategy/Observer（docstring + 代码一致）。
- 根预算唯一权威 = BudgetedModelDriver（execute_trial 内包装，拒绝预包装驱动 :186）。

═══════════════════════════════════════════════════════════
2. 问题矩阵
═══════════════════════════════════════════════════════════

## P0-1 恢复路线模式没有进入正式执行器 —— 确认成立（调用侧缺口）

- 现状：`execute_trial(..., route_mode="route-v1")` 接缝**已存在**
  （trial_executor.py:168，传入 core.set_route_mode :278，记录于
  execution_provenance :385-388）。但 `run_pilot()` 调用处
  （real_pilot_runner.py:542-551）**不传 route_mode** → 两臂都会落到
  默认 route-v1。
- 严重程度：P0（若直接运行 B0/B1，两组实际同策略，实验无效）。
- 证据位置：real_pilot_runner.py:542-551；trial_executor.py:158-169。
- 最小修改：run_pilot 增加显式实验执行单元（comparison_arm_id +
  route_mode），调用 execute_trial 时传入；默认不传时行为不变。

## P0-2 B0/B1 Trial 身份冲突 —— 确认成立

- 现状：trial_id 公式在两处完全相同且只含 experiment_id/task_id/arm：
  real_pilot_runner.py:526 与 trial_executor.py:181。
  B0/B1 的 recovery_strategy_arm 同为 A3_ODYS_FULL（ControlArm 相同）
  → 同任务两臂 trial_id 完全相同 → 产物目录互撞 / COMPLETE 标记互吞。
  run_id = f"trial-{trial_id}"（trial_executor.py:275）同源冲突。
- 放大器 1：`ExperimentPairValidator.validate()` 要求 "arm MUST differ"
  （control_arms.py:840）→ B0/B1 同臂会被误判 pairing violation。
- 放大器 2：paired_manifests 落盘路径 = f"{task_id}_paired.json"，
  "if not path.exists()" 跳过（real_pilot_runner.py:699-701）→
  同任务重复运行时第二个重复**静默丢失**，不是覆盖而是无记录。
- 放大器 3：`_write_trial_artifacts` 的 attempt 机制（:737-745）能防
  同 trial 重跑覆盖，但解决不了两臂同名。
- 严重程度：P0。
- 最小修改：引入显式 `TrialIdentity`（experiment_id / task_id /
  comparison_arm_id / recovery_strategy_arm / route_mode /
  repetition_index → trial_id / attempt_id / run_id）；
  trial_id 追加 comparison_arm 与重复索引段（A0–A5 未启用新模式时
  生成公式不变）；ExperimentPairValidator 增加 B0/B1 兼容判据
  （comparison_arm_id 必须不同且 route_mode 是唯一差异）；
  paired manifest 文件名纳入 comparison/repetition 维度。

## P0-3 Manifest Schema 不一致 —— 确认成立

- 现状：草案 `phase5-route-experiment-001`
  （scripts/phase5_route_experiment_manifest.py:244-276）字段为
  `route_arms` / `strategy_arm` / `selected_task_ids` / `task_fingerprints`
  / `selection_seed`；Runner 权威入口要求 `tasks`（执行单元，缺则 raise，
  real_pilot_runner.py:323-327）、`arm_definitions` 或 `arms`（:335-345）、
  `budgets` 或 `root_budget`（:355-363）、`seed`/`selection_seed`（:368）。
  → 草案直接喂给 run_pilot 会在 `_load_manifest`/`__init__` 就失败。
- 严重程度：P0（正式运行入口无法消费正式实验 Manifest）。
- 证据位置：对比 phase5_route_experiment_manifest.py:244-276 与
  real_pilot_runner.py:318-370。
- 最小修改：新增 create-only 的正式 B0/B1 Manifest（schema
  `phase5-b0b1-experiment-001`），同时携带 Runner 所需执行字段
  （tasks/arm_definitions/budgets/seed）与实验身份字段（route_arms/
  comparison 语义/预注册身份）；**原草案文件保持只读不动**。
  Runner 端加一个显式 schema 判别（experiment_role/schema_version），
  不做隐式字段猜测。

## P0-4 20 任务硬编码 —— 部分成立（元数据误标，不阻断）

- 现状：`_resolve_manifest_units()` 报告字段
  `"PILOT_TASK_IDENTITY": "PROVEN" if len(resolved) == 20 ...`
  （real_pilot_runner.py:450）。30/50/100 任务**不会被拒绝**
  （raise 条件是 mismatch 或 resolved != manifest 任务数，:453-454），
  但报告会把非 20 的合法解析标成 "FAILED"。全库检索确认
  PILOT_TASK_IDENTITY 无下游消费者（仅生成处 1 处）。
  `select_20_tasks()`（:101）是 20-cell pilot 专用抽样，保留不动。
- 严重程度：P1（误导性元数据；有被未来消费者误信的风险）。
- 最小修改：报告字段改为相对 manifest 任务数的判定
  （resolved == expected && no mismatches → PROVEN），20-cell 语义
  只保留在 select_20_tasks 与 pilot 生成器。

## P0-5 冻结哈希口径不一致 —— 确认成立（三值并存，只报告不替换）

同一"evaluator 身份"存在**三个不同记录值**：

| # | 值 | 出处 | 计算口径（按代码/注释推定） |
|---|----|------|------|
| E1 | `412f9c3615bfd997af903f6514f716be94a774140975b82c8ff2fd16b6547833` | benchmark-lock.json `evaluator_hash`；toolmaze_adapter.py:86 默认常量；real_pilot_runner.py:608 offline_grader_identity 字符串 | 历史记录值，口径未在库内重算（常量直传） |
| E2 | `74958635c3794bdd1202ad2a35f2d31f9504221905f98b94173d8c87474be740` | canary_runner.py:68 `_FROZEN_EVALUATOR_DIGEST`（preflight 实算比对 :166-170）；route 生成器 BENCHMARK_IDENTITY（phase5_route_experiment_manifest.py:43） | SHA256(judge.py+metrics.py+sandbox.py 的 **LF 归一字节**拼接)，见 canary_runner.py:114-124 注释 |
| E3 | `412f9c3615bfd997af903f6514f716be94a7741f`（40 hex） | phase5-code-freeze.json `evaluator_digest` | 历史记录值；与 E1 前 38 字符相同但结尾不同（…7741**4** vs …7741**f**），非简单截断 |

- 分析：E2 是唯一可现场复算的口径（LF 归一化后 Windows/Linux 一致）。
  E1/E3 是历史冻结记录，来源脚本不在库内；E3 与 E1 的尾字符差异
  无法用截断解释。作用位置：E1 进入配对不变量的
  `offline_grader_identity` 与 `evaluator_digest`（两臂一致，配对不受
  影响）；E2 用于 canary preflight 的 fail-closed 校验；E3 只在
  code-freeze 快照中。
- 严重程度：P1（身份矛盾存在，但各口径在各自使用点内部自洽；
  不影响 B0/B1 配对有效性——两臂引用同一值）。
- 处置（按任务约束）：**不替换、不重算历史冻结值**。本报告即差异
  报告。建议 G5 材料中把 "evaluator 身份以哪个口径为准" 列为人工
  待批准项；正式 B0/B1 Manifest 同时记录 E1/E2 及其 scope 字符串，
  归因链引用时标注口径。

## P0-6 模型配置并非天然确定性 —— 确认成立

- 现状：`live_driver._request_kwargs()`（live_driver.py:125-136）实际
  发送字段只有：model / messages / max_completion_tokens /
  parallel_tool_calls / tools / extra_body.thinking。
  **不发送 temperature、top_p、seed**。provider-lock.json 如实记录
  `sampling_parameters_sent: false`、`seed_support: false`、
  `effective_temperature: 1.0`、`effective_top_p: 0.95`（服务端推定）。
  构造函数并禁止 "thinking 模式 + 自定义采样参数"（:101-102）。
  SDK max_retries=0（:118），重试口径与 provider_retry_policy
  （exponential_backoff_max_3）的关系在 B3 报告中继续核对。
- 严重程度：P1（不是缺陷，是**声明边界**：不得把温度 0/seed 写成
  可复现性证据）。
- 处置：G5 材料与 B3 报告明确 "声明参数 / 实际发送参数 / 服务端
  推定参数" 三层区分；实验重复设计不依赖单 trial 复现。

## 其他 P1（顺带发现）

- P1-A：`run_pilot()` 的 `arms: list[ControlArm]` 维度无法表达 B0/B1
  （同为 A3）——需要独立的 comparison 维度，不能复用 ControlArm 列表
  冒充（任务硬性要求禁止伪装 A0/A1/A2）。→ A1 设计输入。
- P1-B：`generate_paired_manifest` 落盘名不含 repetition
  （real_pilot_runner.py:699）→ 重复运行静默丢记录（见 P0-2 放大器 2）。
- P1-C：`_assert_provider_config`（:457-464）只在 provider != "none" 时
  校验；fake/离线驱动走 provider=="none" 旁路——E2E 测试须证明该旁路
  不会被真实运行误用（T14 覆盖）。
- P1-D：`run_pilot` 的 `max_rounds` 必须等于 max_turns（:499-500），
  否则 raise——正式 Manifest 的 root_budget.max_turns=15 与
  budgets.max_turns 一致性校验要在 Manifest 适配层做。

═══════════════════════════════════════════════════════════
3. 最小修改方案汇总（A1 执行清单）
═══════════════════════════════════════════════════════════

1. `types.py`：新增 `ComparisonArm`（B0_BASELINE_ROUTE / B1_ROUTE_V1）与
   `TrialIdentity`（含 comparison_arm_id、route_mode、repetition_index；
   派生 trial_id/attempt_id/run_id）。A0–A5 旧公式保留为默认路径。
2. `trial_executor.execute_trial`：新增可选 `identity`/`route_mode` 已有；
   trial_id 派生改为由 identity 提供（缺省回退旧公式）。
3. `real_pilot_runner.run_pilot`：新增实验模式（manifest schema 判别 →
   B0/B1 双臂调度），execute_trial 显式传 route_mode；产物路径纳入
   comparison/repetition；paired manifest 文件名消歧。
4. `control_arms.ExperimentPairValidator`：最小兼容——B0/B1 对
   （comparison_arm_id 不同、recovery arm 相同）改为校验
   "route_mode 是唯一差异"；A0–A5 旧行为不变。
5. `phase5_route_experiment_manifest.py`：不动（create-only 草案保留）；
   新增独立生成器输出正式 schema（或扩展同一脚本新增 mode，不改旧产物）。
6. 测试：T1–T14 provider-free E2E（fake client / 脚本驱动 / 显式网络
   阻断 + 请求计数 0）。

═══════════════════════════════════════════════════════════
4. 已确认不成立/不需修改的传闻风险
═══════════════════════════════════════════════════════════

- "20 任务会被硬性拒绝"：不成立（P0-4 实为元数据误标）。
- "execute_trial 无 route_mode 接缝"：不成立（接缝在，缺的是调用侧）。
- "存在第二条执行路径"：未发现（canary 与 pilot 均经 execute_trial；
  run_pilot 不自建执行组件）。
