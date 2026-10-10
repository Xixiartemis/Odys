# B0/B1 正式实验 Provider-free 端到端验证报告（T1–T14）

日期：2026-10-10
分支：`feat/phase5-b0b1-experiment-readiness`
测试文件：`tests/test_phase5_b0b1_e2e.py`
真实运行：`uv run pytest tests/test_phase5_b0b1_e2e.py -q`
→ **28 passed in 130.55s**（0 failed / 0 skipped，退出码 0）

═══════════════════════════════════════════════════════════
0. 验证边界（先声明，防止误读）
═══════════════════════════════════════════════════════════

- 全部测试使用 fake client（LiveModelDriver + client_factory 注入）或
  脚本化模型驱动（SequenceDriver）或确定性桩（core.receive_tool_result
  边界注入故障证据）。**真实模型请求数 = 0**（T14 显式网络阻断证明）。
- Fake Provider 的结果**不是**真实模型增益，不进入任何效果统计。
- 测试只使用**开发集任务** C1_task_036_P4（协议 §3.1 允许）；
  最终候选测试集一个都没有运行。
- 冻结 ToolMaze 数据、Judge、Metrics 零改动；历史 Canary/Pilot 产物
  零改动。

═══════════════════════════════════════════════════════════
1. 场景矩阵与真实结果
═══════════════════════════════════════════════════════════

| # | 场景 | 测试 | 方法 | 结果 |
|---|------|------|------|------|
| T1 | 普通成功 | TestPairedRun::test_t1_t8_t12_t13_paired_run | run_pilot 双臂，fake client，真实工具 get_employee_id（status=success 正向公开证据）→ 终答被冻结校验器 ACCEPT | PASS：双臂 VALID/completed、恢复决策 0、Judge 结果逐项相等、各 2 个 provider 请求 |
| T2 | 瞬态失败 | TestRecoveryRouteScenarios::test_t2_transient_failure_bounded_legal_retry + test_t2_transient_basis_retries_same_action | 桩注入 {"transient": true} 故障证据；E2E + 决策层 | PASS（见 §2 范围说明）：两臂路线均在合法词表内、有界重试族、预算账本逐项相等；RETRY_SAME/TRANSIENT_BASIS 语义在冻结决策层验证（Q3c） |
| T3 | 永久失败 | test_t3_permanent_failure_replan_only_in_b1 | 桩注入 {"retryable": false} | PASS：B1 产生 **REPLAN**（Q3a PERMANENT_FAILURE_REPLAN）；B0 直通无 REPLAN；两臂全部决策可回链 control_events |
| T4 | 无进展重复 | test_t4_no_progress_loop_bounded_in_both_arms | 同参同结果重复执行 ×3 | PASS：B1 触发 Q3b（NO_PROGRESS_REPLAN/NO_PROGRESS_LOOP 族）；B0 无 Q3b 增强但被**共享门**以 NO_PROGRESS_RETRY 拦截盲重放；两臂决策数 ≤4（有界） |
| T5 | 状态确实变化 | test_t5_observed_state_change_recorded_and_honest + test_t5_q3d_route_semantics_at_decision_layer | 桩两次注入 booking_ready False→True | PASS：state_change=OBSERVED_CHANGE **如实记录**（真实证据，不凭空制造）；Q3d 路线语义在冻结决策层验证（见 §2） |
| T6 | 前置条件未满足 | TestSharedGateAndBudget::test_t6_unmet_precondition_blocks_confirm | booking_ready=false 时模型试图 confirm | PASS：两臂均不执行 confirm（public_tool_observations 无该调用），拦截规则同为 DEPENDENCY_PRECONDITION_UNRESOLVED，拦截清单逐项相等 |
| T7 | 预算耗尽 | test_t7_root_budget_hard_limit_shared | max_model_calls=1 硬顶 | PASS：两臂均 MODEL_CALL_BUDGET_EXHAUSTED 且 VALID（科学结果非基建失败）；model_calls_used ≤ 1、provider_request_count ≤ 1（根预算未被突破） |
| T8 | 身份隔离 | T1 测试内断言 | 同任务 B0/B1 | PASS：trial_id/run_id 全异、产物目录互不覆盖、trial_manifest 身份各自正确 |
| T9 | 重复运行 | TestRepetitionAndResume::test_t9_repetitions_get_distinct_identities | repetitions=2 | PASS：4 个 trial_id 全异（_r01/_r02），paired_manifests 按 repetition 分文件，无覆盖 |
| T10 | 中断与恢复 | test_t10_interrupt_and_resume_no_double_count | 删 B1 COMPLETE + 遗留不完整 attempt 目录后重跑 | PASS：B0 跳过（ALREADY_COMPLETE_SKIPPED）、B1 恰重执行一次且落**新 attempt 目录**（create-only）；遗留 attempt 零改动；重跑产物携带 B1 身份不误归臂 |
| T11 | 非法 Manifest | TestInvalidManifests（8 例）+ TestAuthorization（3 例） | 错 schema/未知 route_mode/错误臂/缺预算/哈希错/重复身份/错误指纹/缺 route_arms/未审批/审批哈希不匹配 | PASS：全部 fail-closed（ValueError/PermissionError），含审批哈希与 manifest_hash 绑定校验 |
| T12 | 配置唯一差异 | T1 测试内断言 + 配对校验器 | ExperimentPairValidator.INVARIANT_FIELDS 全量比对 | PASS：两臂 30 项不变量逐项相等；差异仅 route_mode + comparison 身份；配对校验 B0/B1 语义（同臂+不同 comparison+route_mode 唯一差异）生效 |
| T13 | 归因链 | T1 测试内断言 | manifest_hash→pair→trial→decision→event→judge→usage | PASS：manifest_hash 一致；每个 recovery decision_id 可在 control_events 中找到（或标 suppressed）；trial_manifest/native_judgement/provider_usage 身份一致 |
| T14 | 无 Provider 请求 | TestZeroRealRequests::test_t14_no_network_escape | 非环回 connect/create_connection 全阻断 + fake 请求计数 | PASS：阻断计数 0；唯一流量为计数在案的 fake client 请求；断言请求中**不含** temperature/top_p/seed 字段（与 P0-6 声明一致） |

═══════════════════════════════════════════════════════════
2. 重要发现（如实登记，不修饰）
═══════════════════════════════════════════════════════════

## F1（范围说明）：Q3c/Q3d 在 A3 策略下被规则 c 抢占

冻结规则顺序（recovery_route.py:323-353）：规则 c「策略明确要求
changed context → RETRY_CHANGED」排在 Q3c（transient→RETRY_SAME）
与 Q3d（state change→RETRY_CHANGED/STATE_CHANGED）**之前**。而冻结
A3 策略（OdysFullStrategy → Phase4 DefaultRecoveryPolicy）对故障的
提议恒为 `retry_with_context`（failure_report.suggested_recovery 固定
"retry with failure context"）。因此在真实执行管线中：
- Q3c/Q3d 的**路线级**差异（RETRY_SAME/STATE_CHANGED vs 直通）只在
  `proposed_action="retry"` 时可达；
- A3 双臂 E2E 中可观测的 B0/B1 路线差异恰好是 **Q3a（永久失败→REPLAN）
  与 Q3b（无进展→REPLAN/STOP）**，T3/T4 已实证；
- T2/T5 的 RETRY_SAME 与 STATE_CHANGED 语义在冻结决策层验证
  （与 readiness V1–V11 同层），E2E 断言降为"合法路线+证据如实记录+
  预算相等"。**未放宽任何断言语义，只把断言放在可达层。**

对实验设计的含义：B0/B1 的预注册差异声明为"Q3a/Q3b/Q3d 规则集"，
其中 Q3d 的路线级效果在 A3 策略提议分布下不可达——G5 材料中应
作为已知边界登记（不影响差异面成立：唯一差异仍是 route_mode）。

## F2（缺陷修复）：_write_trial_artifacts 的 tsr 类型崩溃

冻结 Judge 的 metrics_report["tsr"] 为按扰动模式的字典
（{'P0':…,'P4':…}），既有代码将其直接传入 NativeResult(tsr=…)（要求
数值）→ 每当 Judge 实际运行（如预算耗尽路径）产物写入崩溃。
修复（real_pilot_runner.py，最小差异）：tsr 非数值时置 None，
完整字典保留在 native_metrics.json（原有行为不受影响）。

## F3（测试认知）：attempt 目录语义

基 trial 目录存在但缺 COMPLETE 时，重跑落 `{trial_id}__{attempt-xxx}`
新目录（create-only），绝不回写基目录——T10 断言据此修正为检查新
attempt 目录的 COMPLETE 与身份字段。

## F4（测试认知）：Windows asyncio 自管道

ProactorEventLoop 的 socketpair fallback 会本地 connect——网络阻断
必须放行环回地址、只拦非环回 connect/create_connection（T14 采用）。

## F5（测试认知）：A3 校验器拒绝无正向证据的终答

PublicEvidenceCompletionValidator 要求正向公开证据
（status∈{success,ok,completed,done} 或 SIDE_EFFECT_CONFIRMED）才
ACCEPT；INDETERMINATE/REJECT 会触发恢复。T1 的"普通成功"场景因此
选用真实返回 status=success 的 get_employee_id 工具，而非人为放行。

═══════════════════════════════════════════════════════════
3. 测试边界合规
═══════════════════════════════════════════════════════════

- 未运行任何最终候选任务（候选集在 B1 阶段才生成，且仅为 DRAFT）。
- 未修改冻结 ToolMaze/Judge/Metrics/sandbox；桩只注入于
  core.receive_tool_result 边界（测试运行时内存态，冻结文件零改动）。
- 未放宽既有断言、未删除测试、未屏蔽错误（F2 是修复崩溃不是放宽）。
- 真实模型请求 = 0；未使用真实 API Key（api_key="dummy-never-sent"）。
