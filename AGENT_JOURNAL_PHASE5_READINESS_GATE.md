# Agent Journal — Phase5 恢复路线实验就绪 Gate（Readiness Gate）

> 实时记录。开始：2026-10-09（继 route-v1 交付之后）
> 边界：只补实验准备与证据缺口；不重设计冻结机制、不改历史产物、
> 零真实模型请求、不推送远程、需人工批准事项停在对应 Gate。

## 阶段一：冻结状态与协议审查

### 1.1 Git 状态（真实输出）

```
$ git status && git branch --show-current && git log --oneline -4
On branch fix/phase5-recovery-routing-eval-01
nothing to commit, working tree clean
7208d4a Lock protocol freeze checklist: ...
4956e91 Add recovery-route decision layer and experiment attribution (route-v1)
973a612 Fix Phase5 execution-control loop: ...（冻结基线）
04ebe29 fix phase5 mimo multi-tool canary transport
$ git rev-parse HEAD
7208d4ac279b949af3de5874b7fa54f6d3ca7eb4
$ git diff --stat 973a612 -- experiments/phase5/benchmarks experiments/phase5/runs
（空）→ 冻结 ToolMaze 与历史 runs 零改动 ✓
```

- 实现提交 4956e91f…、协议冻结提交 7208d4ac… 均在分支上 ✓
- 工作区干净 ✓

### 1.2 Manifest 生成现状（只读取证）

- 无独立 phase5 manifest 生成脚本；既有生成路径：
  real_pilot_runner.build_pilot_manifest()（PIPELINE_PILOT schema）
  与手写 canary manifest（schema phase5-live-canary-002，自校验 manifest_hash
  = sha256(canonical_json(去 manifest_hash 字段))，canary_runner._manifest_hash:82）。
- canary_runner 校验契约（_verify_manifest/_validate_task/_validate_arms/
  _validate_budget/validate_manifest_generation_config）已逐条读取，
  生成器必须精确满足。
- provider-free 资格 preflight 入口已确认：
  `python -m lhas.phase5.canary_runner --manifest <m> --preflight-only`
  （输出 JSON 报告 + 不可变 qualification 产物，标注 NOT_TOUCHED 不碰 live 命名空间）。

（后续阶段记录持续追加）

## 阶段二：B0/B1 唯一差异面验证

- 定义：唯一差异 = route_mode（Q3a/Q3b/Q3d 规则集）。B0=baseline 直通
  （RecoveryRouteDecider(mode="baseline")），B1=route-v1（默认，冻结行为）。
- 最小接缝：recovery_route.py mode 参数、agent_core.set_route_mode、
  trial_executor.execute_trial(route_mode=…) + provenance 记录。
- 新增 tests/test_phase5_route_experiment_readiness.py：
  `uv run pytest tests/test_phase5_route_experiment_readiness.py -q`
  → **12 passed (8.54s)**（V1–V12 见 B0_B1_OFFLINE_VALIDATION_REPORT.md）。
- 关键证据：同场景逐字段对照（共享事件流相等、仅 route 字段不同）；
  永久失败场景 B1=REPLAN vs B0=直通被共享门拒绝（无绕过）；
  execute_trial 双臂 fake-client 对照除 route_mode 外逐项相等、
  provider 请求各 2 次。
- 结论：唯一差异面可证 → G2 PASS。

## 阶段三：Manifest 与资格 Preflight

- 新生成器 scripts/phase5_route_experiment_manifest.py（拒绝覆盖既有文件）。
- 生成 3 产物：资格 manifest（hash 473a7701…）、B0/B1 实验草案
  （hash 2249614d…）、数据隔离报告。
- preflight 命令（真实执行）：
  `ODYS_AGENT_BASE_URL=… uv run python -m lhas.phase5.canary_runner
   --manifest …qualification-001.json --preflight-only` → **EXIT=0**。
  首跑无 env 报 "required endpoint environment variable is unset"
  （endpoint 身份校验输入，非凭据）；设冻结 endpoint 后通过。
- 机器可读结果：qualification_preflight_report.json ——
  preflight_status=PASS、provider_executed=NO、provider_requests=0、
  credential_present=false、output=NOT_TOUCHED、
  不可变产物落盘 runs/_qualification_preflights/…473a7701…7208d4ac….json。
- git status：仅新增文件；历史 Canary/ToolMaze/Judge/Manifest 零改动。
- 结论：G3 PASS。

## 阶段四：数据集隔离与统计方案审查

- 开发集（grep 测试引用确定）：C1_task_036_P4、C1_task_082_P0、
  C1_task_015_P1、C1_task_082_P1。
- 抽取：seed=42 冻结（pilot 口径），select_20_tasks 语义 + 开发集
  choice 前剔除 → 20 任务、dev_overlap=[]（PASS）、24 条文件指纹
  （缺失 0）、逐 cell 剔除审计。
- 历史 pilot-003 只读检查：与开发集重叠 3 任务
  （C1_task_015_P1/C1_task_036_P4/C1_task_082_P0）→ 不可用作终评集（阻断）。
- 统计方案对照协议 §3–§5：齐备；阈值候选原样列出待人工选择；
  明确登记"零真实运行，无统计效力证据"。
- 结论：G4 PASS（条件：终评用草案清单）；G5 NEEDS_HUMAN_APPROVAL。

## 阶段五：Flaky 独立核查

- 修复前隔离复测：10 次 1 失败（约 10%，随负载波动）；完整回归表现
  间歇（292→291+1→292）——分开记录。
- 基线 worktree(973a612) 复现 1/10；两相关文件本次 diff 零改动
  → 既有环境敏感性。
- 根因：frozen_at 计入 manifest_hash × Windows 时钟粒度（机制演示：
  背靠背 datetime.now() 碰撞 996/1000；测试间隔大故罕见）。
- 修复（方案 A，测试侧最小差异）：第二次 freeze 用显式内容差异输入
  （budgets 30→31），**断言行字节不变**；新增根因回归测试
  test_manifest_hash_covers_frozen_at；provenance.py 零改动。
- 修复后：TestT20 全类 20 轮 **0/20 失败**。
- 备选方案 B（哈希口径修正）/C（豁免）记录在 FLAKY_TEST_REVIEW_REPORT.md。
- 结论：G6 PASS（已修复）。

## 最终完整回归（真实输出）

```
批次1（readiness 12 + recovery_route 28 + execution_control 29 + 既有 6 套）:
→ 206 passed, 1 skipped, 5 warnings (23.53s)
批次2（12 套，含修复后 validity）:
→ 293 passed, 1 skipped, 1 warning (175.75s)
合计：499 passed / 2 skipped / 0 failed
（= 前次 486 + 12 readiness + 1 根因回归测试；既有断言零改动）
```

## 交付物

1. AGENT_JOURNAL_PHASE5_READINESS_GATE.md（本文件）
2. docs/phase5/READINESS_GATE_REVIEW.md（Gate 审查表 G1–G8）
3. docs/phase5/B0_B1_OFFLINE_VALIDATION_REPORT.md
4. docs/phase5/ROUTE_PREFLIGHT_REPORT.md
5. docs/phase5/DATASET_ISOLATION_REPORT.md
6. docs/phase5/FLAKY_TEST_REVIEW_REPORT.md
7. scripts/phase5_route_experiment_manifest.py + 3 生成产物
8. tests/test_phase5_route_experiment_readiness.py（12 项）
9. 协议状态附录（RECOVERY_ROUTING_EVAL_PROTOCOL.md §9）
10. experiments/phase5/readiness/ 机器可读证据（隔离报告、preflight 报告）

## 最终 Gate

G1 PASS｜G2 PASS｜G3 PASS｜G4 PASS(条件)｜G5 NEEDS_HUMAN_APPROVAL｜
G6 PASS(已修复)｜G7 PASS（零真实模型请求）｜G8 PASS（历史零改动）。
按约束停在 G5：等待人工批准后方可提交真实模型实验启动申请。
