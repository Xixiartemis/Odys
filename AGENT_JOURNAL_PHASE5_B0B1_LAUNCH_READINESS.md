# Agent Journal — Phase5 B0/B1 正式实验接线与 G5 审批准备

> 实时记录：只记录真实执行的检查、命令、代码变更与测试结果。
> 开始时间：2026-10-10（PR #37 合并后）

## 0. 任务

目标 A：正式 B0/B1 双臂实验工程化（run_pilot 可用正式 Manifest 执行
B0/B1 对照，共享 A3/授权门/预算门/Judge，唯一差异 route_mode，
Provider-free 端到端验证）。
目标 B：G5 人工审批材料（20 任务草案审查、30/50/100 可行性、50 对
候选清单、统计协议、预算与停止规则）。
约束：零真实请求、不改冻结资产、不自动推送、G5 不得自行批准。

## 阶段 0：冻结基线与源码审查（2026-10-10）

### 0.1 基线核对（真实命令输出）

```
$ git status --short && git branch --show-current
（干净）
fix/phase5-recovery-routing-eval-01
$ git fetch origin && git rev-parse origin/main
eb6a53c92c0ec082e5f9f6662f5f18b82e49fe28
$ gh pr view 37 --json state,mergeCommit,headRefOid
state=MERGED, mergeCommit=eb6a53c92c0ec082e5f9f6662f5f18b82e49fe28,
head=0e7d74320cfa1a963ff995c4de5130e8115b4e43
（PR #37 已于 2026-10-10T02:56:56Z 合并，merge commit 即新 origin/main）
$ git checkout -b feat/phase5-b0b1-experiment-readiness origin/main
→ 分支创建，HEAD = eb6a53c92c0ec082e5f9f6662f5f18b82e49fe28
```

### 0.2 冻结身份记录

| 项 | 值 | 出处 |
|----|----|------|
| 分支 / HEAD | feat/phase5-b0b1-experiment-readiness @ eb6a53c | git |
| origin/main | eb6a53c92c0ec082e5f9f6662f5f18b82e49fe28 | git fetch |
| PR #37 合并 SHA | eb6a53c（merge commit） | gh pr view 37 |
| route-v1 策略版本 | recovery-route-v1 | recovery_route.py:38 |
| route-v1 指纹 | route_policy_fingerprint()（sha256，规则集 Q0–Q3e + 映射） | recovery_route.py:413-425 |
| 实验协议版本 | recovery-route-eval-protocol-v1（冻结，待人工批准） | RECOVERY_ROUTING_EVAL_PROTOCOL.md |
| ToolMaze commit | ef0798aa7f31ac9b33403254b1ef76e8673305fa | benchmark-lock.json |
| dataset_revision | 08b0239a98b0b07839d673f5bc0ef7d836db9296 | benchmark-lock.json |
| dataset_hash | 9fcd7d7e…6bc8（scope: perturbed_tasks/**/*.json 按 POSIX 路径排序拼接） | benchmark-lock.json |
| evaluator 三口径 | E1=412f9c36…7833 / E2=74958635…e740 / E3=412f9c36…741f | 见审计 §P0-5 |
| provider 配置身份 | provider_config_hash(provider-lock.json)；mimo-v2.5 | provider-lock.json |
| 事件 Schema | phase5-control-events-v2 / execution-control-v1 | execution_control.py:69-70 |
| 运行环境 | Windows 10.0.26200-SP0 / Python 3.11.11 / uv | 本地 |

### 0.3 源码审查

实际阅读：real_pilot_runner.py（921 行全）、trial_executor.py（497 行全）、
recovery_route.py（425 行全）、control_arms.py（705–978 关键段）、
agent_core.py（160–220 route_mode/lineage 段）、live_driver.py（100–220
请求构造段）、toolmaze_adapter.py（75–180 身份段）、canary_runner.py
（40–160 指纹段）、scripts/phase5_route_experiment_manifest.py（全）、
B0_B1_OFFLINE_VALIDATION_REPORT.md、DATASET_ISOLATION_REPORT.md、
benchmark-lock.json、provider-lock.json、phase5-code-freeze.json。

产出：docs/phase5/B0B1_RUNNER_SOURCE_AUDIT.md（调用链 + 问题矩阵）。

### 0.4 问题矩阵结论

- P0-1 route_mode 未进 run_pilot 调用侧：**成立**（接缝在 executor，调用缺失）
- P0-2 trial 身份冲突：**成立**（+3 个放大器：pairing "arm must differ"、
  paired manifest 重名静默丢失、attempt 机制只防同名重跑）
- P0-3 Manifest schema 不一致：**成立**（草案无 tasks/arm_definitions）
- P0-4 20 硬编码：**部分成立**（元数据误标，不阻断 30/50/100）
- P0-5 evaluator 哈希三值并存：**成立**（E1/E2/E3 口径不同；只报告不替换）
- P0-6 非确定性：**成立**（temperature/top_p/seed 未发送，声明≠可复现）
- 另记 P1-A..P1-D（ControlArm 维度不够表达 B0/B1、paired 重名、
  provider=="none" 旁路、max_rounds==max_turns 约束）

### 0.5 风险/未解决

- E1/E3 历史口径来源脚本不在库内，差异无法完全归因（列入 G5 待批准项）。

## 阶段 A1：正式 B0/B1 双臂接线（2026-10-10）

### A1.1 修改文件（最小改造）

1. `src/lhas/phase5/types.py`：新增 `ComparisonArm`（B0_BASELINE_ROUTE/
   B1_ROUTE_V1，route_mode 映射冻结）与 `TrialIdentity`（显式身份参数：
   experiment_id/task_id/comparison_arm_id/recovery_strategy_arm/
   route_mode/repetition_index → 派生 trial_id/attempt 由产物层/ run_id；
   comparison 缺省时 trial_id 保持历史公式，A0–A5 零变化）。
   校验 fail-closed：未知 route_mode、comparison 与 route_mode 不匹配
   均 raise。
2. `src/lhas/phase5/trial_executor.py`：`execute_trial(..., identity=None)`
   可选显式身份（与 arm/route_mode 交叉校验）；identity 提供时
   execution_provenance 增记 comparison_arm_id/repetition_index。
   不传 identity 时逐字节保持旧行为。
3. `src/lhas/phase5/real_pilot_runner.py`：
   - Manifest 显式 schema 判别（phase5-b0b1-experiment-001 → 实验模式；
     route_arms 出现在其他 schema → 拒绝猜测；缺预算 → 拒绝）；
   - `_resolve_route_arms()`：仅允许 B0/B1 × A3_ODYS_FULL，route_mode
     必须与枚举映射一致，缺臂/重臂拒绝；
   - `_check_b0b1_authorization()`：真实执行 fail-closed（需
     status=HUMAN_APPROVED + approved_by + manifest_hash 绑定）；
     `offline_validation=True` 为独立 provider-free 入口，结果永久标记
     OFFLINE_VALIDATION、included_in_confirmatory_analysis=False，
     **不伪造审批状态**；
   - `_run_b0b1_experiment()`：配对调度（seed 轮转）→ 身份唯一性前置
     证明 → 每 trial 新 ModelDriver → canonical execute_trial（显式
     route_mode+identity）→ 共享配对校验 → create-only 产物 →
     {task}_r{rep}_paired.json → experiment_budget 累计守卫
     （max_total_trials 前置、max_total_model_calls 中止）；
   - COMPLETE 预检跳过（防重复计数）；P0-4 报告字段改相对判定；
   - 修复 F2：tsr 字典崩溃（见 A2）。
4. `src/lhas/phase5/control_arms.py`：ExperimentPairValidator 最小兼容
   ——comparison_arm_id 在场时校验"同恢复臂+comparison 必须不同+
   route_mode 唯一差异"；缺省时旧规则（arm 必须不同）逐字不变。
5. `scripts/phase5_route_experiment_manifest.py`：新增
   `build_formal_b0b1_manifest()`（create-only 契约，同时满足 Runner
   执行字段与实验身份字段；evaluator 三口径并存记录 E1/E2/E3 + scope
   注记；cluster_id=base_task_id）。旧模式与旧产物零改动。

### A1.2 受影响既有测试（真实输出）

```
$ uv run pytest tests/test_phase5_route_experiment_readiness.py \
    tests/test_phase5_canonical_path.py tests/test_phase5_e2e_wiring.py -q
→ 69 passed, 1 skipped (10.56s)   ← 零回归
```

## 阶段 A2：Provider-free E2E T1–T14（2026-10-10）

### A2.1 测试文件与真实结果

`tests/test_phase5_b0b1_e2e.py`（28 个测试）：

```
$ uv run pytest tests/test_phase5_b0b1_e2e.py -q
→ 28 passed in 130.55s (0 failed / 0 skipped, exit 0)
```

T1–T14 全部 PASS，逐场景证据见
docs/phase5/B0B1_PROVIDER_FREE_E2E_REPORT.md 与机器可读
experiments/phase5/readiness/b0b1_e2e_evidence.json。
真实模型请求 = 0（T14 非环回 connect 全阻断 + fake 计数）。

### A2.2 过程记录（失败→根因→修复，不省略）

1. 首跑 16 过 1 挂（-x 提前停止）→ 全量 8 挂 18 过。逐项根因：
   - T1：单工具脚本的终答无正向公开证据 → 校验器 INDETERMINATE →
     A3 恢复 → 脚本耗尽（F5）。改用真实返回 status=success 的
     get_employee_id 后干净 2 请求（F5 记录）。
   - T7/T10：`_write_trial_artifacts` 将 Judge 的字典型 tsr 传给
     NativeResult 崩溃（F2）——真 bug，已修（数值槽位非数值置 None）。
   - T6：confirm 无 booking 时门不拦的原因是缺少 booking_ready=False
     证据（门依据结果中的就绪键）；改为桩注入 booking_ready=False 后
     两臂同规则拦截（DEPENDENCY_PRECONDITION_UNRESOLVED）。
   - T2/T5：实测发现 F1（规则 c 抢占 Q3c/Q3d，A3 提议恒为
     retry_with_context）；按可达层修正断言，语义验证放决策层，
     E2E 断言保留"合法+有界+预算相等+证据如实"。
   - T10：断言与 attempt 语义不符（重跑落新 attempt 目录，F3）；
     修正断言为新 attempt 目录 + 身份字段。
   - T14：阻断 socket 构造器误伤 asyncio 自管道（F4）；改为放行环回、
     拦非环回 connect/create_connection。
2. 全部为测试侧修正或真缺陷修复；**未放宽任何冻结语义断言**，
   未改冻结文件。

### A2.3 未解决风险

- F1 的 Q3d 不可达边界进入 G5 待批准项（实验设计语义边界）。

## 阶段 B1：任务集可行性与 50 对候选（2026-10-10）

### B1.1 真实扫描（只读）

```
$ uv run python scripts/phase5_route_experiment_manifest.py --mode candidates
→ feasibility: 30/50/100 全部 feasible，16/16 格覆盖，各 N 个独立 base
  manifest_hash=5c1c5a45fd1a0bf621f0b97b0b262f86636e08cd584c1c8f5fb45013c83bd51b
```

原始池 2000 对（400 base × P0-P4）；P3/P4 带 corruption_type（9 类）；
P0 无故障。排除规则：P0 全排除 + 开发 base 级（3 base）+ 历史
manifest base 级（pilot-001/002/003 + canary 引用的全部 base）+
一 base 一变体 → 池 1520 对 / 380 base（每格 95）。

### B1.2 污染审计发现（重要）

- 历史运行只有 canary-004/005/006（开发集单元）；**pilot-002 运行目录
  为空（0 文件）、pilot-003 无运行目录**——历史 pilot 从未执行任务。
- 既有 20 对草案与 pilot-002/003 manifest **共享 17/20 单元**
  （同 seed-42 抽样流 + 剔除 3 个开发单元补抽 3 个）。按"历史
  manifest 即污染"最严格口径该草案不干净；50 对候选按此口径
  与开发集/历史交集全为 []（_b1_crosscheck.py 实证）。
- 发现后处置：生成器升级排除规则并重新生成候选（旧生成物为本
  会话未提交文件，已删除重建）；draft 数字经机器证据复核后修正
  （1520/380/每格 95；分层 C1×13/C2×12/C3×13/C4×12、
  P1×12/P2×13/P3×13/P4×12）。

产出：docs/phase5/B0B1_FINAL_DATASET_PROPOSAL.md、
experiments/phase5/readiness/b0b1_candidates_feasibility.json、
experiments/phase5/manifests/phase5-b0b1-candidates-50-draft-001.json
（DRAFT_CANDIDATE_PENDING_APPROVAL，未执行）。

## 阶段 B2：统计协议与 G5 材料（2026-10-10）

产出：docs/phase5/B0B1_G5_APPROVAL_PACKET.md（A–I 九项批准项，
逐项建议/依据/替代/风险）。要点：
- 唯一主要指标 VCR（Judge 判定有效 trial 口径），配对差为分析单位；
- 五分类判定 A-E；对冻结否定线的对抗性审查结论：跨 0 应判
  "证据不足"而非否定（修订建议不覆盖冻结原文）；
- cluster bootstrap 仅在允许同 base 多变体时启用（防虚假收窄）；
- 不得宣称"50 对一定充足"（基线 VCR 未知 → 效力 UNKNOWN）；
- 预注册扩展规则（50→100）防 optional stopping；
- 误拦截率 ≤5% 的分母/标注/CI 口径标记待批准，不假定通过；
- F1（Q3d 边界）登记为批准项附注。

## 阶段 B3：模型配置/预算/停止规则（2026-10-10）

产出：docs/phase5/B0B1_MODEL_BUDGET_AND_STOP_REVIEW.md。
- 三层配置核查表（声明/实际发送/服务端推定）：temperature/top_p/seed
  未发送（P0-6 落实）；sdk_max_retries=0 且客户端零退避，
  provider_retry_policy=exponential_backoff_max_3 为不可验证声明；
- 分层预算：单 Trial 硬顶 15/50/100k；30/50/100 对理论上限
  （60/100/200 trial，≤3k/5k/10k 调用）；时间与费用 UNKNOWN 不编造；
- 11 条异常停止规则（触发证据/范围/数据保留/恢复前置）；
- 三级授权隔离（E2E 允许 / 成本诊断未授权 / 正式实验未授权），
  无泛化 --force；授权 fail-closed + 身份唯一性 + 实验预算守卫
  已实现并离线验证。

## 阶段 C：对抗性复审（2026-10-10）

逐项构造反例（任务 §9.3）：

| 反例 | 结论 | 证据/处置 |
|------|------|-----------|
| B0/B1 实调同一 route_mode | 不存在 | T3 双臂路线差异实证；provenance.route_mode 逐 trial 记录；identity 交叉校验 route_mode↔comparison |
| 同臂伪装不同身份 | 不存在 | TrialIdentity 校验 comparison↔route_mode 映射；配对校验要求同臂+不同 comparison |
| 两臂预算不同 | 不存在 | INVARIANT_FIELDS 含 root_model_call_budget/root_token_budget/wall_deadline_budget，T12 逐项相等 |
| 重复运行覆盖历史产物 | 不存在 | T9/T10（attempt 目录 create-only、COMPLETE 跳过） |
| 失败 Trial 被错误排除 | 不存在 | errors.json + pair_trials.error 记录；INVALID_INFRA 单独报告不入分母（协议口径） |
| 真实调用被错标 Fake | **存在缺口→已修** | offline_validation 原可配真实驱动错标 real_provider_executed=NO；新增 fail-closed：环境存在 ODYS_AGENT_API_KEY 即拒绝（B0B1_OFFLINE_WITH_REAL_CREDENTIALS）+ 回归测试 test_offline_validation_refuses_real_credentials |
| 未发生故障算恢复成功 | 不存在（口径层） | 协议 §3.2 要求逐 trial 触发证据；提案 §1 明确 P0 不入分母 |
| 同 base 重复计数 | 不存在 | 50 候选 cluster_id 唯一；统计协议要求 cluster bootstrap（若放宽） |
| 非法 Manifest 绕过审批 | 不存在 | T11 8 例 + TestAuthorization 3 例 fail-closed |
| 异常后静默重试 | 不存在 | errors 全记录；重跑走显式 attempt 目录；停止规则 #11 |
| 测试模式意外真实请求 | 不存在 | T14 非环回 connect 全阻断 + fake 计数 |

发现 P1×1（真实调用错标）已修复并通过回归（17 passed 含新测试）。

## 阶段 C：完整回归、CI 同口径检查与提交（2026-10-10）

### C.1 完整回归（真实输出）

```
$ uv run pytest -q --junitxml=...\b0b1_full_regression.xml
→ 1828 passed, 2 skipped, 6 warnings in 701.34s (0:11:14)
  退出码 0（FULL_GREEN；基线 1799 passed/2 skipped → +29 新测试，零回归）
```

新测试 29 = tests/test_phase5_b0b1_e2e.py 28 项（T1–T14）+
对抗性回归 test_offline_validation_refuses_real_credentials。
既有断言零改动（F2 为崩溃修复非放宽）。

### C.2 CI 同口径 canary preflight（真实输出，3/3 PASS）

```
$ ODYS_AGENT_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1 \
  uv run python -m lhas.phase5.canary_runner --manifest ...004-r3/005-r2/006-r2.json --preflight-only
→ preflight_status=PASS ×3；provider_requests=0；provider_executed=NO；
  credential_present=false；output=NOT_TOUCHED（LIVE_EXECUTION_OUTPUT）
```

### C.3 冻结资产核验（git diff 实证）

- experiments/phase5/benchmarks、runs/、历史 canary/pilot manifest、
  benchmark-lock/provider-lock、20 对草案：**零 diff**（相对
  PR #37 冻结基线 94dde69a）。
- recovery_route.py / execution_control.py / recovery_budget.py /
  firewall.py / runtime_validator.py / model_driver.py / canary_runner.py
  / provenance.py / evals/：**零 diff**（route-v1 决策语义、安全门、
  预算门、Judge、Metrics 未触碰）。
- 工作区干净；p25 执行派生文件按既定口径还原未入库。

### C.4 提交

- BASE_SHA = eb6a53c92c0ec082e5f9f6662f5f18b82e49fe28（origin/main = PR #37 merge）
- CODE_SHA = 8777d3934d5a3f03485edf9733dcfd7536fb4dfb（15 files）
- FINAL_SHA = 本 Journal 补记提交（见最终汇报）
- 未推送远程、未建 PR、未合并、未启动真实模型实验。

### C.5 Gate 结论（详见最终汇报 §13）

ENGINEERING_READY=YES；PROVIDER_FREE_E2E_PASS=YES；
DATASET_CANDIDATE_READY=YES（DRAFT）；STATS_APPROVAL_READY=YES；
BUDGET_APPROVAL_READY=YES；HUMAN_G5_APPROVED=**NO**；
REAL_MODEL_EXECUTION_AUTHORIZED=**NO**。
