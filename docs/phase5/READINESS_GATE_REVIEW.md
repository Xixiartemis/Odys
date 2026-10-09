# Recovery Route Experiment Readiness — Gate 审查表

日期：2026-10-10｜分支：fix/phase5-recovery-routing-eval-01
证据基线：实现提交 4956e91f…、协议冻结 7208d4ac…、本就绪工作见最终提交
（Journal：AGENT_JOURNAL_PHASE5_READINESS_GATE.md）

═══════════════════════════════════════════════════════════
第一阶段：逐项剩余 Gate 审查表（启动前）
═══════════════════════════════════════════════════════════

| # | Gate | 当前状态 | 证据 | 未满足条件 | 通过标准 | 人工决策 |
|---|------|---------|------|-----------|---------|---------|
| 1 | 实现与协议冻结身份一致 | PASS | git log：4956e91f(实现)/7208d4a(冻结) 在分支；协议 §7 清单已填 SHA/指纹/哈希；git diff 973a612 对 benchmarks/runs 为空 | 无 | 分支/HEAD/清单三方一致 | 否 |
| 2 | B0/B1 唯一差异面离线验证 | PASS | tests/test_phase5_route_experiment_readiness.py 12 项（差异面逐字段对照、开关生效、门一致性、无绕过）；报告 B0_B1_OFFLINE_VALIDATION_REPORT.md | 无（详见报告限制节） | 唯一差异可证 + 开关生效 + 无泄漏 | 否 |
| 3 | Manifest/归因链 provider-free preflight | PASS | canary_runner --preflight-only EXIT=0；qualification_preflight_report.json（PASS/0 请求/NOT_TOUCHED）；不可变产物落盘 _qualification_preflights/ | preflight 报告本身不含 route 版本字段（在 manifest 与 trial provenance 中，链路可关联） | preflight PASS + 机器可读产物 + 历史零改动 | 否 |
| 4 | 开发集/终评集隔离 | PASS（新抽取）/ BLOCKED（历史 pilot-003） | dataset_isolation_report.json：新抽取 20 任务 dev_overlap=[]；pilot-003 与开发集重叠 3 任务 → 不可用作终评集 | 历史 pilot-003 不可用于终评（须用新草案清单） | 抽取清单零重叠 + 指纹完整 | 是（终评清单需批准） |
| 5 | 统计方案与阈值 | NEEDS_HUMAN_APPROVAL | 协议 §3-§5：样本量 30/50–100、配对 bootstrap、指标清单齐备；阈值候选已原样列出待选 | 无统计效力证据（零真实运行）；阈值未批准 | 人工批准阈值与样本量终值 | **是** |
| 6 | Flaky 风险 | FIXED（待确认） | 根因明确（frozen_at 计入哈希 × Windows 时钟粒度）；断言行未变、输入确定性化 + 根因回归测试；修复后 20/20 稳定 | 无 | 修复有根因+回归 或 明确豁免 | 否（修复）/是（如选豁免） |
| 7 | 零真实模型请求 | PASS | 本任务全部命令为 pytest/生成器/preflight（provider_executed:NO, provider_requests:0） | 无 | 可证零请求 | 否 |
| 8 | 历史产物与冻结环境未改 | PASS | git status：仅 3 个 src（route_mode 接缝）+ 新增文件；benchmarks/runs 零 diff | 无 | 零改动 | 否 |

═══════════════════════════════════════════════════════════
第六阶段：最终 Gate（G1–G8）
═══════════════════════════════════════════════════════════

| Gate | 结论 | 依据摘要 |
|------|------|---------|
| G1 实现与协议冻结身份一致 | **PASS** | 分支/HEAD/冻结清单三方一致；冻结对象零 diff |
| G2 B0/B1 唯一差异面离线验证 | **PASS** | 12 项确定性测试；差异=route_mode（Q3a/Q3b/Q3d）；共享门/预算/Judge 口径逐项对照相等 |
| G3 Manifest 与归因链 provider-free preflight | **PASS** | EXIT=0；PASS/0 请求/NOT_TOUCHED；manifest_hash 自校验；不可变产物落盘；归因链 manifest_hash→manifest(版本)→trial provenance |
| G4 开发集与最终评估集隔离 | **PASS（条件）** | 新草案抽取零重叠+24 指纹；条件：终评必须使用草案清单，历史 pilot-003 因 3 任务重叠被阻断 |
| G5 统计方案和阈值 | **NEEDS_HUMAN_APPROVAL** | 阈值候选与样本量终值等待人工选择（协议 §4/§8）；不得视为已批准 |
| G6 Flaky 风险 | **PASS（已修复）** | 根因+最小差异+回归测试；断言未放宽、测试未屏蔽；修复前证据保留（FLAKY_TEST_REVIEW_REPORT.md） |
| G7 零真实模型请求 | **PASS** | 全程离线；preflight 机器证据 provider_requests=0 |
| G8 历史产物与冻结环境未修改 | **PASS** | git 只读核查 + 时间戳核查 |

结论：**技术 Gate 全部通过（G4 附条件）；G5 待人工批准。**
按约束停在此处：真实模型实验启动申请须待 G5 批准（及 G4 终评清单确认）后提交，
本任务完成本身不代表获得实验启动授权。

═══════════════════════════════════════════════════════════
启动真实模型实验前仍须人工完成的事项（汇总）
═══════════════════════════════════════════════════════════

1. 协议 §4 采用线/否定线阈值选择（PENDING_HUMAN_APPROVAL）。
2. 样本量终值（30 资格 vs 50–100 发表）与重复运行次数。
3. 终评任务清单确认（草案：experiments/phase5/manifests/
   phase5-route-experiment-B0B1-draft-001.json，manifest_hash
   2249614d0234c35a6d7ac323dc78d206b2f865cb1323a91596349ac7a00a54d6）。
4. 真实模型实验启动授权（含 ODYS_AGENT_API_KEY/ODYS_AGENT_BASE_URL
   凭据环境的提供方式）。
5. （可选）实验 A "安全门关闭"对照开关是否引入（PENDING_HUMAN_APPROVAL）。
