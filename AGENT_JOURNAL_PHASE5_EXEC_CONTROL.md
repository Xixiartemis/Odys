# Agent Journal — Odys Phase5 执行控制闭环修复

> 实时记录：只记录真实发生的检查、代码变更、命令与测试结果。
> 开始时间：2026-10-09

## 0. 任务

修复 Canary-005 / Canary-006 暴露的执行控制缺陷：
- P0-1 逐调用授权（Runtime 逐个交付工具调用，可拦截剩余调用）
- P0-2 标准化结果语义（exec/ready/retry 三轴）
- P1 无进展恢复（重复调用拒绝 + 幂等检查 + 两独立判断）
- 审计事件 + lineage P2 + 实验兼容

## 1. 核查阶段

### 1.1 git 状态核查（真实命令输出）

```
$ cd /d/projects/odys-phase5-live && git status && git branch --show-current && git rev-parse HEAD
HEAD detached at 04ebe29
nothing to commit, working tree clean
---BRANCH---（空，detached）
---HEAD---
04ebe293d25ae7bf0fa8a82df5fec007b71ee6db
```

- HEAD = 04ebe293d25ae7bf0fa8a82df5fec007b71ee6db
- 与任务要求核实的实验运行 SHA `04ebe293d25ae7bf0fa8a82df5fec007b71ee6db` **一致** ✓
- 工作区干净，无未提交变更 ✓
- 当前 detached HEAD，后续修复将在新分支上进行
- remote: https://github.com/Xixiartemis/Odys.git

### 1.2 仓库结构确认

运行产物仓库 = D:\projects\odys-phase5-live（本次开发仓库）
- src/lhas/phase5/ 含 agent_core.py (903行)、agent_adapter.py (152行)、
  shadow_observer.py (143行)、trial_executor.py (446行)、recovery_budget.py (105行)、
  runtime_validator.py (280行)、control_arms.py (978行) 等
- 恢复相关：src/lhas/recovery.py、src/lhas/recovery_control.py

（后续记录持续追加）

## 2. 缺陷定位（基于 Canary-005/006 真实产物）

Canary-005 (A3_ODYS_FULL) 与 Canary-006 (A0_BARE) 的 public_tool_observations 显示：
- step=2 模型一批提 2 个调用：`build_flight_booking_request`(idx 0) 返回
  `booking_ready:false`，同批 `confirm_held_recommendation_order`(idx 1) 仍然执行
  （status=confirmed）——逐调用授权缺失。
- ODYS_FULL 的 step=3 用相同参数摘要(222756ea4a96f25f) 重复调
  `build_flight_booking_request`，结果完全一致——无进展重试未被拒绝。

冻结引擎行为（sandbox.py，只读确认）：`engine.run()` 循环里
`if action.tool_calls and len(action.tool_calls) > 1` → 整批顺序执行、逐个
`receive_tool_result`。引擎不改，边界改：adapter 每次 step() 只放行 1 个调用。

## 3. 基线测试（修改前，真实输出）

- 批次1（mimo_multi_tool/canonical_path/runtime_authority/runtime_validator/
  semantic_matrix/e2e_wiring）：137 passed, 1 skipped（8.17s）
- 批次2（falsification/canary_infra_closure/final_adversarial_closure/
  live_provider_protocol/substrate/toolmaze_integration/validity/convergence/
  recovery_control_plane×2/recovery_policy/p4_recovery_semantics）：
  292 passed, 1 skipped（162.33s）
- 基线合计：429 passed, 2 skipped

## 4. 代码变更（分支 fix/phase5-execution-control-01，基线 04ebe293）

### 4.1 新增 src/lhas/phase5/execution_control.py
- 三轴标准化：ToolExecStatus(SUCCEEDED/FAILED/UNKNOWN)、
  ToolReadyStatus(READY/NOT_READY/UNKNOWN/NO_EVIDENCE)、
  Retryability(RETRYABLE/NOT_RETRYABLE/UNKNOWN)
- ToolResultNormalizer：通用结果适配器；booking_ready=false→NOT_READY；
  status=confirmed→exec SUCCEEDED 但 confirmation_receipt=True、不提供就绪证据；
  缺证据→UNKNOWN（不默认成功）；retryable/transient 显式标记→RETRYABLE
- ToolSemantics/ToolSemanticsRegistry：显式工具元数据（odys_semantics、
  annotations）+ 通用动词启发（COMMIT_VERBS/WRITE_VERBS）；无 Canar y 专用
  故障标签、无 Judge 规则
- ExecutionAuthorizationGate：R1 依赖前置未决拦截（跨轮次持久）；
  R2 无进展重试拒绝（相同参数+相同结果+无状态变化→停止/升级；
  NOT_RETRYABLE 失败拒绝首次相同重复；允许一次有界验证重试）；
  R3 副作用成功重复需幂等证据；is_retry_justified() 与恢复预算两个独立判断
- 控制事件：PROPOSED/AUTHORIZED/EXECUTED/OBSERVED/BLOCKED/RECOVERY/ESCALATION

### 4.2 agent_core.py
- set_execution_control / set_execution_lineage / get_last_tool_call_record
- record_blocked_tool_call()：合成响应（明确标注 SYNTHETIC、_synthetic_response
  标记），只写对话历史补齐协议配对，不进观察/证据/影子/轨迹
- has_terminal_recovery_pending + mark_control_escalation
- _authorize_recovery / 校验路径：判断1 重试合理性（无进展→转 STOP），
  判断2 恢复预算（仅在合理时消耗）；RECOVERY/ESCALATION 控制事件
- RETRY 重放前清理 _active_tool_call_batch（避免结果错挂批次）
- P2 lineage：evidence ledger / shadow observer 使用真实 task_id/trial_id

### 4.3 agent_adapter.py（P0-1 核心）
- 模型原始批次完整保留（调用+原始 tool_call_id/provider id）
- step() 每次只向引擎交付 1 个授权调用；先 gate.propose 再交付
- 拦截调用：合成响应 + BLOCKED 事件 + 继续；终态（无进展）→ 全部剩余拦截
  + 显式停止状态
- receive_tool_result：映射回原批次索引；记录→授权状态更新→恢复判断
- ShadowProgressObserver 未改动（非干预职责保持）

### 4.4 trial_executor.py
- 所有臂共享 ExecutionAuthorizationGate（基础执行安全门一致）
- lineage 真实标识注入
- 引擎轮次护栏 max_turns+max_model_calls（逐调用交付消耗轮次；
  根预算权威仍是 BudgetedModelDriver）
- TrialResult.control_events + to_dict 统计

### 4.5 canary_runner.py / real_pilot_runner.py
- 持久化 control_events.json（新产物文件）

## 5. 新增测试 tests/test_phase5_execution_control.py（29 项）

真实执行结果：`uv run pytest tests/test_phase5_execution_control.py -q`
→ 29 passed（3.86s），一次全过，含 e2e（真实冻结引擎 + Scripted provider，
C1_task_036_P4：confirm_held_recommendation_order 执行次数 = 0）。

覆盖（对照任务第六节）：
1. booking_ready=false 后确认工具执行次数为 0 ✓（单测 + e2e）
2. 前置条件满足时正常序列继续 ✓
3. UNKNOWN/FAILURE 时依赖副作用被拦截 ✓（3 参数化）
4. 未就绪状态跨模型轮次有效 ✓
5. 无进展重试被拒绝/升级 ✓（模型重复、NOT_RETRYABLE、恢复 RETRY 转 STOP、
   瞬态依据保留、参数变化=进展）
6. 单调用/正常多工具/预算逻辑无回归 ✓（含副作用幂等检查）
7. 原始调用 ID 与真实/合成响应配对 ✓（serialize_messages 往返）
8. 拦截调用只进控制事件 ✓
9. ShadowObserver 非干预 ✓
10. A0/A3 基础安全门一致、恢复能力不混合 ✓

## 6. 回归测试（修改后）

- 批次1+新测试：166 passed, 1 skipped（11.87s）← 137+29，零回归
- 批次2：292 passed, 1 skipped（146.36s）← 与基线 292/1 完全一致，零回归
- 合计：458 passed, 2 skipped（基线 429 + 新增 29，无任何失败/回归）
- 覆盖完整性：grep 确认全部 14 个 import lhas.phase5 的测试文件均在两批内

## 7. 原始产物完整性（第五节要求）

- runs/phase5-real-canary-005-r2 与 -006-r2 下所有原始 JSON mtime 均为
  2026-09-29（运行当日），未被修改、覆盖或删除。
- 目录内另有 .zip 备份文件 mtime 2026-10-09 15:04——早于本次代码修改
  （15:4x 起），为此前已存在的备份，本次工作未创建也未修改。
- 未重跑任何真实模型 Canary 实验。

## 8. 完成状态

- 离线回归全部通过，按指令停在此处，未发起真实模型实验。
