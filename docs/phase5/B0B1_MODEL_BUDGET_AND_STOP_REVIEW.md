# B0/B1 模型配置、调用预算与停止规则审查

日期：2026-10-10
范围：实验 B 正式运行前的模型配置核查、分层预算、异常停止规则与
授权隔离。**零真实请求；费用/时间缺数据处标 UNKNOWN，不编造。**

═══════════════════════════════════════════════════════════
1. Provider 配置核查（三层区分）
═══════════════════════════════════════════════════════════

| 项 | 配置文件声明（provider-lock.json） | 实际请求字段（live_driver._request_kwargs） | 服务端推定行为 |
|----|-----------------------------------|--------------------------------------------|----------------|
| model_id | mimo-v2.5 | `model: "mimo-v2.5"` 发送 | 响应 model 字段与请求不一致即 PROVIDER_PROTOCOL_FAILURE（live_driver.py:169） |
| endpoint | frozen_endpoint=token-plan-cn.xiaomimimo.com/v1（from ODYS_AGENT_BASE_URL） | base_url 传给 SDK client | 不适用 |
| temperature | 声明 0.0 | **未发送**（字段不存在于请求） | effective_temperature=1.0（推定） |
| top_p | 声明 1.0 | **未发送** | effective_top_p=0.95（推定） |
| sampling_parameters_sent | **false（如实）** | 一致 | — |
| seed | seed_support=false | **未发送** | 无 seed 语义 |
| max_completion_tokens | 4096 | `max_completion_tokens: 4096` 发送 | 服务端截断行为未验证 |
| request_timeout | 120s | SDK client timeout=120 | — |
| SDK 自动重试 | sdk_max_retries=0 | client 构造 max_retries=0 | — |
| 退避策略 | provider_retry_policy="exponential_backoff_max_3" | **客户端无任何重试/退避实现**（next_action 单次 HTTP 尝试） | 服务端/基础设施行为，**不可从客户端代码验证** |
| 多工具调用 | supports_tool_choice=false、supports_parallel_tool_calls=false、request_parallel_tool_calls_hint=false、provider_may_return_multiple_tool_calls=true、requires_assistant_content_for_tool_calls=true | `parallel_tool_calls` hint 发送；响应多 tool_call 逐个排队执行（per-call delivery） | thinking 模式 + 自定义采样参数被构造函数拒绝（live_driver.py:101-102） |
| 配置指纹 | provider_config_hash=`4bffac88a0ab58a5e677a0d30f301fa61dd4cf48aca1aa1d567139efa140047b` | run_pilot 逐 trial 校验 driver.resolved_config 与 lock 一致（validate_resolved_config） | — |

**结论（P0-6 落实）**：模型输出**不是**确定性的；temperature=0/seed
不得作为可复现性证据。可复现性依赖：冻结任务/工具/Judge/预算/
策略指纹 + 重复运行统计，不依赖单 trial 复现。E2E T14 已断言请求
载荷中确实不含 temperature/top_p/seed。

═══════════════════════════════════════════════════════════
2. 分层预算
═══════════════════════════════════════════════════════════

## 2.1 单 Trial 预算（root_budget，双臂共享，BudgetedModelDriver 硬执行）

- max_turns = 15
- max_model_calls = 50
- token_budget = 100,000（max_tokens 同值）
- deadline_seconds = 未设置（manifest 中为 null）

执行权威：BudgetedModelDriver（trial_executor 内包装，拒绝预包装
驱动）；engine_round_guard = max_turns + max_model_calls = 65（护栏
语义记录于 execution_provenance）。预算耗尽 = VALID 科学结果
（T7 实证），非基建失败。

## 2.2 整轮实验预算（理论上限；按推荐 50 对 × 2 臂 × 1 重复）

| 项 | 30 对 | **50 对** | 100 对 |
|----|-------|-----------|--------|
| Trial 总数 | 60 | **100** | 200 |
| 理论最大模型调用 | 3,000 | **5,000** | 10,000 |
| 理论最大 Token（in+out） | 6×10⁶ | **10⁷** | 2×10⁷ |
| 理论最大工具执行 | 依轨迹，无硬顶（root budget 间接约束） | 同左 | 同左 |
| 预计完成时间区间 | **UNKNOWN**（历史 pilot 运行目录为空，无可靠耗时分布；canary 单 trial 参考值不足以外推） | UNKNOWN | UNKNOWN |
| 费用估算 | **UNKNOWN**（无当前 Provider 价格表与历史用量分布；按任务约束不编造） | UNKNOWN | UNKNOWN |

实验级预算执行机制（已实现，离线可验证）：
- manifest `experiment_budget.max_total_trials`：计划 trial 数超限
  → 启动前 ValueError（EXPERIMENT_BUDGET_EXCEEDED）；
- manifest `experiment_budget.max_total_model_calls`：累计调用达限
  → 停止后续 trial，summary.stopped_reason=EXPERIMENT_BUDGET_EXHAUSTED，
  已完成数据保留。

## 2.3 异常停止规则（触发证据 / 停止范围 / 数据保留 / 恢复前置）

| # | 停止条件 | 触发证据 | 停止范围 | 已完成数据 | 恢复前置 |
|---|----------|----------|----------|------------|----------|
| 1 | API 凭据缺失 | 启动前 env 检查 ODYS_AGENT_API_KEY 缺失/空 | 全局，不开跑 | 不适用 | 提供凭据后重启 |
| 2 | Provider/模型身份不匹配 | validate_resolved_config 失败或响应 model 不一致（PROVIDER_PROTOCOL_FAILURE） | 当前 trial 终止 INVALID_INFRA；连续 3 个 trial 触发则全局停 | 保留 | 修复配置并重新校验 lock |
| 3 | 任务/Judge 哈希不匹配 | preflight：任务指纹/evaluator 身份与 manifest 不符 | 全局，不开跑 | 不适用 | 修复清单或环境 |
| 4 | 双臂公共配置不一致 | ExperimentPairValidator violations > 0 | 该 pair 标记无效并全局停（配对设计破坏） | 保留 pair 记录 | 修复后重跑该 pair |
| 5 | 未授权执行 | ExecutionAuthorizationGate 拦截计数异常或授权门绕过证据 | 全局停 + 安全事件上报 | 保留 | 人工安全审查 |
| 6 | 根预算/实验总预算耗尽 | BudgetedModelDriver 耗尽 / experiment_budget 守卫 | 单 trial 终止（VALID）或全局停 | 保留 | 批准追加预算 |
| 7 | 基础设施持续无效 | 连续 ≥3 trial INVALID_INFRA 或 INVALID_INFRA 率 >20% | 全局停（判定 E） | 保留 | 根因修复 + 离线验证 |
| 8 | 数据集污染 | 运行中发现任务与开发集/历史重叠证据 | 全局停 | 保留并标记该批不入确认性统计 | 换清单重批 |
| 9 | 输出身份冲突 | trial_id 碰撞 / create-only 写入失败（FileExistsError） | 全局停 | 保留 | 身份方案修复 |
| 10 | 归因文件缺失 | trial 产物缺 control_events/validity/provider_usage 任一 | 该 trial 标 UNATTRIBUTABLE；比例 >5% 全局停 | 保留 | 修复产物管线 |
| 11 | 运行状态不明 | 进程中断后无法判定已完成 trial | 停止重跑；按 COMPLETE 标记恢复（T10 语义） | 保留 | 按 attempt 语义续跑 |

═══════════════════════════════════════════════════════════
3. 授权隔离（三级，互不混用）
═══════════════════════════════════════════════════════════

| 级别 | 状态 | 入口 | 机制 |
|------|------|------|------|
| 1. Provider-free E2E / preflight | **允许** | `run_pilot(offline_validation=True)` | 无需审批；产物永久标记 OFFLINE_VALIDATION / included_in_confirmatory_analysis=false / real_provider_executed=NO |
| 2. 真实模型成本诊断 | **未授权** | 无独立入口 | 须 G5 批准 + 凭据 + 预算确认后新增 |
| 3. 实验 B 正式确认性评估 | **未授权** | `run_pilot(authorization={status:HUMAN_APPROVED, approved_by, manifest_hash})` | fail-closed：缺审批记录/审批哈希与 manifest_hash 不符均 PermissionError（E2E TestAuthorization 实证） |

**无任何泛化 --force 开关**：offline_validation 与 human approval 是
两个语义独立、不可互换的入口；offline 模式的结果永久排除出确认性
统计。若正式入口保护不足的阻断问题：已补齐（授权 fail-closed、
身份唯一性前置校验、实验预算守卫、create-only 产物），均离线可验证
（T7/T10/T11），未触发任何真实请求。

═══════════════════════════════════════════════════════════
4. 遗留不确定性（如实登记）
═══════════════════════════════════════════════════════════

1. 费用与耗时 UNKNOWN（见 2.2）——G5 批准前须由用户提供价格口径
   或接受"先小规模实测再定预算"。
2. 服务端 effective sampling 行为不可验证（1 表）；重复运行统计是
   唯一可信的稳定性口径。
3. provider_retry_policy 声明与客户端零重试不一致（记录于 P0-6），
   如需可靠重试须协议修订后实现。
