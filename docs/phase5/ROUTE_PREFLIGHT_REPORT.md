# Manifest 与资格 Preflight 报告

日期：2026-10-10｜机器可读证据目录：experiments/phase5/readiness/

═══════════════════════════════════════════════════════════
1. 新 Manifest 生成器
═══════════════════════════════════════════════════════════

脚本：scripts/phase5_route_experiment_manifest.py（只写新文件，拒绝覆盖）
生成物（manifest_hash = sha256(canonical_json(去 hash 字段))，与
canary_runner._manifest_hash 同算法，可独立复算）：

| 产物 | experiment_id | manifest_hash |
|------|---------------|---------------|
| 资格 manifest | phase5-route-readiness-qualification-001 | 473a77018f912f5757b70d718078480804a3bbe66ad2cd62035eb68779d36d19 |
| B0/B1 实验草案 | phase5-route-experiment-B0B1-draft-001 | 2249614d0234c35a6d7ac323dc78d206b2f865cb1323a91596349ac7a00a54d6 |
| 数据隔离报告 | —（schema phase5-route-dataset-isolation-001） | 见文件 |

═══════════════════════════════════════════════════════════
2. Provider-free 资格 preflight（真实执行）
═══════════════════════════════════════════════════════════

命令：
```
$ ODYS_AGENT_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1 \
  uv run python -m lhas.phase5.canary_runner \
  --manifest experiments/phase5/manifests/phase5-route-readiness-qualification-001.json \
  --preflight-only
EXIT=0（stdout 3209 bytes JSON）
```
说明：ODYS_AGENT_BASE_URL 仅用于 endpoint 身份归一化与哈希（preflight
校验输入）；qualification 模式下**零 provider 请求、零凭据**（报告
provider.credential_present=false、api_key_persisted=false）。

机器可读结果（experiments/phase5/readiness/qualification_preflight_report.json）：

| 检查项 | 值 |
|--------|-----|
| preflight_status | **PASS** |
| provider_executed / provider_requests | **NO / 0** |
| manifest_hash 自校验 | 473a7701…（与生成器输出一致） |
| code_sha（preflight 时 HEAD） | 7208d4ac279b949af3de5874b7fa54f6d3ca7eb4 |
| base_exact_head | 973a612a02d984fe8312401b08b3c394e5994649（冻结基线） |
| task 身份 | C1_task_036_P4 / C1 / P4 / file_sha256 5aab3927…（与历史 preflight 一致） |
| engine_preflight | 构造 PASS / 工具插件 PASS / schema PASS / 离线评测器导入 PASS；tool_count=5；provider_tool_schema_hash=c0d99d2e… |
| output 命名空间 | **NOT_TOUCHED**（live 输出目录零接触） |
| 不可变产物 | runs/_qualification_preflights/phase5-route-readiness-qualification-001/qualification_preflight_473a77018f912f57_7208d4ac279b949a_8c94a5fcf5337363.json（create-only） |

历史只读核查：git status 仅显示新增文件；Canary-005/006、ToolMaze、
sandbox、Judge、Metrics、历史 Manifest 零改动（mtime 不变）。

═══════════════════════════════════════════════════════════
3. 归因链可关联性
═══════════════════════════════════════════════════════════

链路：preflight.manifest_hash → manifest（route_policy_version /
route_policy_fingerprint / event_schema_version / implementation_sha /
route_arms.route_mode / 任务指纹）→ 运行时 trial_manifest.json 的
execution_provenance（route_mode、版本、engine_round_guard）→
control_events.json（run_id/trial_id/decision_id）→ recovery_decisions.json
（decision_id）→ native_judgement.json / validity.json。

已验证环节：
- manifest 自校验哈希（生成器 + preflight 双端一致）✓
- trial 级 execution_provenance 落盘（既有测试
  test_execution_provenance_identifies_versions + readiness V12
  execute_trial 双臂对照）✓
- 事件↔恢复决策 decision_id 关联（test_recovery_event_links_call_identity_and_decision）✓
- 任务标识/文件指纹（preflight task.file_sha256 + 隔离报告 24 指纹）✓
- 运行护栏（engine_round_guard 语义入 provenance）✓

已知缺口（登记，不影响本 Gate）：
- preflight 报告体本身不含 route 版本字段（在 manifest 内，经
  manifest_hash 可靠引用；协议允许"记录或可靠引用"）。
- 真实 trial 产物的端到端落盘验证需真实运行（属实验 B，未执行）。

结论：**G3 PASS**——资格 preflight 通过、机器可读、归因链可关联、零请求、
历史零改动。
