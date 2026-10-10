# Agent Journal — Odys Phase5 恢复路线决策与实验归因闭环

> 实时记录：只记录真实执行的检查、命令、代码变更与测试结果。
> 开始时间：2026-10-09（执行控制修复交付之后）

## 0. 任务

补齐两项能力（不堆叠 Runtime 模块）：
1. 恢复路线决策：基于真实执行结果、前置条件、状态变化与预算选择合理下一步
2. 实验归因：从原始轨迹可靠区分 授权拦截/真实执行/恢复决策/任务完成/成本变化，
   支持公平对照实验

顺序：冻结 → 源码审查 → 最小必要改造 → 离线验证 → 测评协议冻结。

## 阶段 A：冻结现有交付与验证基线

### A.1 Git 状态（真实命令输出 2026-10-09）

```
$ git status && git branch --show-current && git rev-parse HEAD
On branch fix/phase5-execution-control-01
nothing to commit, working tree clean
---BRANCH---
fix/phase5-execution-control-01
---HEAD---
973a612a02d984fe8312401b08b3c394e5994649
---LOG---
973a612 Fix Phase5 execution-control loop: ...
04ebe29 fix phase5 mimo multi-tool canary transport
7532780 Canonicalize frozen evaluator provenance hash
```

- HEAD = 973a612a02d984fe8312401b08b3c394e5994649 —— 与任务预期 HEAD **一致** ✓
- 基线代码 SHA 04ebe293… = 973a612 的父提交 ✓
- 工作区干净，无未知修改 ✓
- ToolMaze 冻结版本 ef0798aa7f31ac9b33403254b1ef76e8673305fa（沿用，未动）

### A.2 环境（真实命令输出）

```
$ uv --version
uv 0.11.28 (ebf0f43d7 2026-07-07 x86_64-pc-windows-msvc)
```
（python 版本与依赖在下方 A.4 补记）

### A.3 本次基线测试（真实重新执行，不冒充历史结果）

- 命令批次1：`uv run pytest tests/test_phase5_execution_control.py
  tests/test_phase5_mimo_multi_tool.py tests/test_phase5_canonical_path.py
  tests/test_phase5_runtime_authority.py tests/test_phase5_runtime_validator.py
  tests/test_phase5_semantic_matrix.py tests/test_phase5_e2e_wiring.py -q`
- 命令批次2：`uv run pytest tests/test_phase5_falsification.py
  tests/test_phase5_canary_infra_closure.py tests/test_phase5_final_adversarial_closure.py
  tests/test_phase5_live_provider_protocol.py tests/test_phase5_substrate.py
  tests/test_phase5_toolmaze_integration.py tests/test_phase5_validity.py
  tests/test_phase5_convergence.py tests/test_recovery_control_plane.py
  tests/test_recovery_control_plane_evidence_gate.py tests/test_recovery_policy.py
  tests/test_p4_recovery_semantics.py -q`
- 状态：后台执行中（proc_3ac6ebab660e），完成后补记真实结果。
- 历史报告的 458 passed / 2 skipped 为**历史证据**，本次以重新执行结果为准。

### A.4 既有文档资产（阶段 F 复用对象）

- docs/phase5-benchmark-plan.md（Phase5 测评计划）
- docs/evidence/phase5-plan.md
- docs/09_EVAL_PROTOCOL.md / docs/12_EXPERIMENT_PROTOCOL.md
- docs/08_RECOVERY_POLICY.md（恢复策略规范）
- Manifests: experiments/phase5/manifests/*.json（含 schema_version、manifest_hash、
  base_exact_head、benchmark/provider provenance）

（后续记录持续追加）

### A.5 本次基线真实结果（proc_3ac6ebab660e，修改前）

```
批次1: 166 passed, 1 skipped (11.71s)
批次2: 292 passed, 1 skipped (141.01s)
合计: 458 passed / 2 skipped  ← 与历史报告一致，本次为重新执行结果
```

## 阶段 B：源码审查（产出 docs/phase5/RECOVERY_ROUTING_SOURCE_AUDIT.md）

真实取证摘要：
- B1-2/B1-8：RecoveryActionKind 仅 none/retry/retry_with_context/escalate/
  stop（control_arms.py:114）；`grep REPLAN src/lhas/phase5/*.py` = 0 命中 →
  无统一路线入口、无 REPLAN → 缺失。
- B1-3：无进展判定用摘要比较，把"摘要未变"当"状态未变" → 需诚实化。
- B1-7：R2 对成功只读轮询也会误杀 → 需豁免。
- B2-6：runtime_backend.finalize() 返回裸 trace，无实验身份 → 选"排除出
  测评范围"方案。
- B2-7/B2-8：缺策略版本/事件 Schema/轮次护栏 provenance。
- 历史 Canary-005/006 只读归因：成本/结果/恢复次数可重建；逐调用授权事件
  历史不存在 → 标 UNATTRIBUTABLE，不补造。
- 已完整实现（不重复做）：两判断分离(B1-1)、副作用幂等(B1-6)、被拒重试
  不耗预算(B1-10)、跨轮状态(B1-11)、合成/拦截区分(B2-2)、SYNTHETIC 不计
  真实调用(B2-9)、有效性分类(B2-10)。

## 阶段 C/D：实现（分支 fix/phase5-recovery-routing-eval-01）

### 新增 src/lhas/phase5/recovery_route.py
- RecoveryRoute 六路线 + map_route_to_recovery_action 冻结映射
  （RETRY↔RETRY_SAME；RETRY_WITH_CONTEXT↔RETRY_CHANGED/REPLAN）
- StateChange（OBSERVED_CHANGE/NO_OBSERVED_CHANGE/UNKNOWN）+
  EvidenceGrade（DIRECT/DERIVED/ABSENT/UNKNOWN）
- RecoveryRouteDecider：Q0 终止 → Q1 策略终态 → Q2 无提议 CONTINUE →
  Q3a 永久失败 REPLAN/ESCALATE → Q3b 无进展循环 STOP（策略为
  retry_with_context 时 REPLAN/ESCALATE）→ Q3c 策略上下文变更
  RETRY_CHANGED → Q3d 瞬态 RETRY_SAME → Q3e 有界验证 RETRY_SAME
- evaluate_state_change：无状态证据一律 UNKNOWN（不伪造无进展证据）
- REPLAN 有界（每签名一次，再失败→ESCALATE/REPLAN_EXHAUSTED）
- route_policy_fingerprint() 确定性指纹

### execution_control.py
- 事件自动携带 run_id/trial_id/event_schema_version
  (phase5-control-events-v2)/execution_control_version
- NormalizedToolResult.state_evidence_available
- R2 循环规则：仅非成功(affects_followups)重复 + 记录真实 StateChange；
  成功只读轮询豁免
- route_attempts()/state_change_for()/last_normalized() 路线证据接口
- bind_identity(run_id, trial_id)

### agent_core.py
- 两个恢复入口（工具结果路径 + 校验路径）统一走 _authorize_recovery：
  路线层（RouteDecision）→ 预算门（独立）
- RECOVERY 事件补齐归因字段：decision_id/route/reason_code/rationale/
  route_evidence/evidence_grade/state_change/state_summary/action_signature/
  tool_call_id/provider_tool_call_id/retry_justified/budget_decision/
  recovery_budget_before/after/model_calls_used/authorized
- recovery_decisions 条目与事件用 decision_id 关联
- termination_kind（STOP/ESCALATION/BUDGET_EXHAUSTION）区分终止状态
- 策略原始 reason 保留（不覆盖证据）——修 2 个既有测试暴露的回归

### trial_executor.py / runtime_backend.py
- execution_provenance 块（版本、fingerprint、引擎轮次护栏语义、
  execution_path=CANONICAL_TRIAL_EXECUTOR）
- 非 canonical 路径标注 NON_CANONICAL_RUNTIME_BACKEND +
  included_in_canonical_analysis=False（选"排除"方案）

## 阶段 E：测试

新增 tests/test_phase5_recovery_route.py（28 项）：
`uv run pytest tests/test_phase5_recovery_route.py -q` → 28 passed (3.69s)

覆盖：CONTINUE/瞬态重试/不可重试→REPLAN 不重复/循环识别/UNKNOWN 不伪造/
状态变化再评估/REPLAN 不绕授权门/ESCALATE≠STOP/预算不足不重试且不伪装/
被拒重试不耗预算（预算 5→4→4、账本 1 条）/轮询不误杀/副作用幂等/
调用身份与 decision_id 关联/事件顺序/SYNTHETIC 不计执行/JSON 往返+旧
Schema 兼容/版本 provenance/非 canonical 排除/工具失败 vs 基础设施失败/
冻结 C1_task_036_P4 行为+归因/根预算权威/无未授权 provider 调用。

开发中真实修正（非放宽断言）：
1. 路线层覆盖策略 reason → 既有 2 测试失败 → 改为保留原样 reason
   （e2e_wiring TestT6/T8 恢复通过，断言未动）。
2. _no_progress_loop 需含"状态无变化"条件（状态变化=进展）。
3. 新测试自身3处 fixture 错误（失败检测漏 timeout、callable payload、
   未接预算门断言 ALLOW）修正。

## 阶段 F：测评协议（docs/phase5/RECOVERY_ROUTING_EVAL_PROTOCOL.md）

- 实验 A（授权门安全不变量，provider-free）与实验 B（恢复路线增益）
  严格分开，禁止合并归因
- 预注册：任务集（开发集边界：此前测试过的任务不进确认性统计）、
  故障类别+触发证据、双臂唯一差异面、lock 配置、有效性定义、
  样本量（30 资格/50–100 发表，复用 benchmark-plan）、指标与统计
- 建议阈值全部标注 PENDING_HUMAN_APPROVAL
- 冻结清单：协议版本已定，代码 SHA 待最终报告锁定

## 既有失败标记（与本次改动无关，单独记录）

`tests/test_phase5_validity.py::TestT20_NonPolicyIdentityHashMatch::
test_provenance_freeze_idempotent` 为**既有 flaky 测试**，证据：

1. 本次 diff 对 tests/test_phase5_validity.py 与 src/lhas/phase5/provenance.py
   零改动：`git diff --stat 973a612 -- <两文件>` 输出为空。
2. 隔离循环复现（当前代码）：同一单测连续跑 10 次 → 第 6 次失败（1/10）。
   `for i in 1..10: uv run pytest <该测试> -q` → "1 failed" ×1。
3. 基线复现：git worktree 检出 973a612（与两文件在本次工作中未变一致）
   同样循环 10 次 → 第 10 次失败（1/10）。
4. 机制：ProvenanceFreeze.freeze() 以 `datetime.now(timezone.utc).isoformat()`
   写入 frozen_at 并计入 manifest_hash；测试断言"第二次调用因时间戳不同而
   drift→ValueError"。Windows 时钟粒度下两次背靠背调用可返回同一时间戳 →
   哈希相同 → 不抛 drift → DID NOT RAISE 偶发失败。
   （provenance.py:68 frozen_at；测试注释自述该依赖。）

处置：不修改该测试、不修改 provenance.py（超出本次任务范围，且改动哈希
语义会破坏历史 provenance 校验）。最终回归中该测试的偶发失败单独标记，
不计入本次通过结果。建议后续独立任务修复（例如 drift 断言改为显式内容
差异输入，保持断言不变）。

（最终回归记录跑完成后补记）

## 最终回归记录跑（真实输出）

```
$ uv run pytest tests/test_phase5_execution_control.py tests/test_phase5_recovery_route.py
  tests/test_phase5_mimo_multi_tool.py tests/test_phase5_canonical_path.py
  tests/test_phase5_runtime_authority.py tests/test_phase5_runtime_validator.py
  tests/test_phase5_semantic_matrix.py tests/test_phase5_e2e_wiring.py -q
→ 194 passed, 1 skipped, 5 warnings (15.67s)

$ uv run pytest （批2 12 套）-q
→ 292 passed, 1 skipped, 1 warning (174.58s)

合计：486 passed / 2 skipped / 0 failed（基线 458 → +28 新增，零回归）
```

## 交付物清单

1. AGENT_JOURNAL_PHASE5_RECOVERY_ROUTING.md（本文件）
2. docs/phase5/RECOVERY_ROUTING_DESIGN_CONTRACT.md（设计契约）
3. docs/phase5/RECOVERY_ROUTING_SOURCE_AUDIT.md（审查矩阵）
4. src/lhas/phase5/recovery_route.py + tests/test_phase5_recovery_route.py
5. 事件 Schema 更新：execution_control.py（phase5-control-events-v2）
   + agent_core.py 归因字段 + trial_executor.py execution_provenance
   （既有实现充分处给出测试证据，见审查矩阵"已实现"行）
6. docs/phase5/RECOVERY_ROUTING_EVAL_PROTOCOL.md（协议 + 预注册）
7. docs/phase5/RECOVERY_ROUTING_OFFLINE_VALIDATION_REPORT.md（最终报告）

## 阶段 G：Gate 审计

十项 Gate 全部通过，详见最终报告 §8。真实模型请求 = 0。
待人工批准：协议阈值、样本量终值、真实模型实验授权、安全门对照开关。

## 阶段 H：PR #37 合并前修复 — Windows CRLF 误报（2026-10-10）

### H.1 问题与根因（真实证据）

PR #37 推送前的 Windows 全量 pytest 出现 1 个失败：
`tests/test_phase4_live_no_progress_escalation.py::test_preflight_is_provider_free_and_does_not_create_output`
（断言 `report["experiment_01_unchanged"] is True`）。

根因：`_frozen_experiment_01_unchanged`（scripts/phase4_live_no_progress_escalation.py）
用**字节级比较** `git show HEAD:<path>`（LF blob）与工作区文件。本机
core.autocrlf=true，工作区为 CRLF，逐字节必然不等 → 干净工作区被误报为
已修改。实证（2026-10-10）：

```
$ git show HEAD:scripts/phase4_live_controlled_experiment.py > blob; wc -c blob 工作区文件
47361 blob / 48435 工作区（差 1074 = 行数，纯 CRLF 增量）
$ 去 \r 后 cmp → CONTENT-IDENTICAL（两文件均内容逐字节相同）
```

同一文件内 `_committed_file_unchanged` 的注释早已记录该坑（"Git's
comparison honors the repository's line-ending attributes; a
byte-for-byte comparison would falsely flag a clean CRLF worktree"），
但 `_frozen_experiment_01_unchanged` 未复用该口径。ubuntu CI 为 LF 检出
不受影响；windows CI 专项集原本不含该测试文件。

### H.2 修复（最小差异，BASE=94dde69a51490317acc9e43dd39422e186113744）

提交 **bb3c20faed9982a143bafebbb66e21525b02533e**（3 files, +126/-4）：

1. `scripts/phase4_live_no_progress_escalation.py`（+8/-4）：
   `_frozen_experiment_01_unchanged` 保留 `git show` 的"必须已提交"校验，
   比较部分改为复用 `_committed_file_unchanged` 的 git diff 口径。
   fail-closed 语义不变：未提交路径 / 文件缺失 / git 错误均返回 False。
   不引入新的文件哈希规则。
2. `tests/test_phase4_live_no_progress_escalation.py`（+118）：8 个回归
   测试 — LF 一致、干净 CRLF（本修复的目标场景）、真实内容修改、暂存
   修改、文件缺失、未提交路径、diff 错误、show 错误（后两者 fail-closed）。
   全部在 tmp_path 一次性 fixture git 仓库执行（monkeypatch
   REPO_ROOT/EXPERIMENT_01_PATHS），真实冻结实验文件零接触。
3. `.github/workflows/test.yml`（+1）：将
   tests/test_phase4_live_no_progress_escalation.py 加入 windows CI 专项集合。

未触碰：recovery_route.py 决策语义、安全门、预算门、Judge、历史 Canary、
ToolMaze（benchmark-lock.json 冻结身份 ef0798a 未动）、冻结实验内容。
零真实模型请求。

### H.3 测试证据（真实输出 2026-10-10）

```
$ uv run pytest tests/test_phase4_live_no_progress_escalation.py -q
→ 14 passed in 9.72s（6 既有 + 8 新增）

$ uv run pytest -q   （Windows 全量）
→ 1799 passed, 2 skipped, 6 warnings in 550.53s (0:09:10)
  （基线 1790 passed / 2 skipped / 1 failed → 修复后 0 failed，+8 新增测试）
```

ubuntu 全量 CI 与 windows CI 于推送后在 PR #37 验证（结果见 PR checks）。
