# Phase5 恢复路线与实验归因 — 最终离线验证报告

分支：`fix/phase5-recovery-routing-eval-01`
基线冻结：分支起点 = `973a612a02d984fe8312401b08b3c394e5994649`
（= 任务预期 HEAD ✓；其父 `04ebe293…` = 基线代码 SHA ✓）
日期：2026-10-09
环境：Python 3.11.11（venv）、pydantic 2.13.4、uv 0.11.28、Windows 11

═══════════════════════════════════════════════════════════
1. 执行的命令与真实结果（全部实际运行，退出码/数量/耗时）
═══════════════════════════════════════════════════════════

A. 修改前基线（新分支上，真实重执行）
```
$ uv run pytest tests/test_phase5_execution_control.py tests/test_phase5_mimo_multi_tool.py \
    tests/test_phase5_canonical_path.py tests/test_phase5_runtime_authority.py \
    tests/test_phase5_runtime_validator.py tests/test_phase5_semantic_matrix.py \
    tests/test_phase5_e2e_wiring.py -q
→ 166 passed, 1 skipped (11.71s)
$ uv run pytest tests/test_phase5_falsification.py …（批2 12文件）… -q
→ 292 passed, 1 skipped (141.01s)
基线合计：458 passed / 2 skipped
```

B. 新增定向测试
```
$ uv run pytest tests/test_phase5_recovery_route.py -q
→ 28 passed (3.69s)
$ uv run pytest tests/test_phase5_execution_control.py -q
→ 29 passed (3.56s)   ← 既有 29 项在改造后全部保持通过（断言未改动）
```

C. 最终完整回归（记录跑，修改后）
```
批次1（2 个测试文件 + 既有 6 套）:
→ 194 passed, 1 skipped, 5 warnings (15.67s)
批次2（12 套）:
→ 292 passed, 1 skipped, 1 warning (174.58s)
最终合计：486 passed / 2 skipped / 0 failed
对比基线 458 → +28（全部为新增测试），既有测试零回归
```

D. 既有 flaky 测试（与本次改动无关，单独标记）
```
tests/test_phase5_validity.py::TestT20_NonPolicyIdentityHashMatch::
test_provenance_freeze_idempotent
- 隔离循环 10 次：1 failed（当前代码，第 6 次）
- 基线 worktree（973a612）循环 10 次：1 failed（第 10 次）
- 本次 diff 对该测试文件与 provenance.py 零改动（git diff --stat 为空）
- 机制：ProvenanceFreeze 用 datetime.now() 时间戳计入哈希；Windows 时钟
  粒度下背靠背两次调用可得同一时间戳 → drift 断言偶发不触发
- 处置：不修改（超范围且会破坏历史哈希语义）；不计入本次通过结果；
  上述完整回归中该测试恰通过，历史批次中偶发 1 次失败已如实记录
```

═══════════════════════════════════════════════════════════
2. 变更文件与差异
═══════════════════════════════════════════════════════════

新增：
- src/lhas/phase5/recovery_route.py（路线枚举 + 确定性决策器 + StateChange
  诚实语义 + 冻结映射 + 策略指纹）
- tests/test_phase5_recovery_route.py（28 项）
- docs/phase5/RECOVERY_ROUTING_SOURCE_AUDIT.md（源码审查矩阵）
- docs/phase5/RECOVERY_ROUTING_DESIGN_CONTRACT.md（设计契约）
- docs/phase5/RECOVERY_ROUTING_EVAL_PROTOCOL.md（测评协议 + 预注册）
- AGENT_JOURNAL_PHASE5_RECOVERY_ROUTING.md（实时开发记录）

修改：
- src/lhas/phase5/execution_control.py：事件身份/版本字段、
  state_evidence_available、轮询豁免 + StateChange 记录、路线证据接口
- src/lhas/phase5/agent_core.py：统一恢复入口走路线层+预算门、
  RECOVERY 事件归因字段（decision_id 等）、termination_kind、
  策略 reason 保留（修既有回归）
- src/lhas/phase5/trial_executor.py：execution_provenance、gate 身份绑定
- src/lhas/phase5/runtime_backend.py：非 canonical 排除标记
（canary_runner/real_pilot_runner 的 control_events.json 持久化为上一分支
已有；本分支未再改动）

═══════════════════════════════════════════════════════════
3. 与冻结对象的差异核查
═══════════════════════════════════════════════════════════

- ToolMaze（ef0798aa7f31ac9b33403254b1ef76e8673305fa）：零改动 ✓
  （sandbox/Judge/Metrics 只读使用；diff 无 experiments/phase5/benchmarks）
- Canary-005/006 原始产物：零改动 ✓（本任务全程只读）
- 历史 Manifest：零改动 ✓
- 真实模型 Provider 请求：**0**（见 §6）

═══════════════════════════════════════════════════════════
4. 恢复路线能力补齐情况（对应审查矩阵）
═══════════════════════════════════════════════════════════

| 能力 | 状态 | 验证 |
|------|------|------|
| 统一路线决策入口（六路线） | 已实现 | 28 项单测 |
| REPLAN（可审计、有界、经授权门） | 已实现 | 3 项集成测试 |
| ESCALATE≠STOP（事件/终止类别分离） | 已实现 | 2 项测试 |
| StateChange 诚实语义（UNKNOWN 不伪造） | 已实现 | 2 项测试 |
| 无进展循环识别（相同结果+无状态变化） | 已实现 | 2 项测试 |
| 轮询豁免 | 已实现 | 1 项测试 |
| 路线层与预算层分离（含预算快照） | 已实现 | 3 项测试 |
| 永久失败不重复执行 | 已实现 | 2 项测试 |
| 归因事件（身份/证据/预算/决策关联） | 已实现 | 3 项测试 |
| 版本化 provenance | 已实现 | 1 项测试 |

═══════════════════════════════════════════════════════════
5. 已验证 vs 未验证（诚实边界）
═══════════════════════════════════════════════════════════

已验证（单测 + 冻结环境 E2E + 历史数据只读分析）：
- 上表全部能力；C1_task_036_P4 在冻结 ToolMaze 上的控制行为
  （booking_ready=false → 确认执行 0 次）+ 归因字段完整。
未验证（不得声称已解决）：
- 恢复路线对**真实模型**任务成功率的增益（实验 B 未执行，需授权）。
- REPLAN 质量（是否产生更好计划）——只有"不绕过授权门、有界"被验证。
- B0/B1 离线开关隔离的端到端验证（协议 §2 标注需先补离线验证）。
- 实验 A "安全门关闭"对照（当前无旁路开关，PENDING_HUMAN_APPROVAL）。
- 旧产物的 route/授权字段归因（历史不存在 → UNATTRIBUTABLE）。

═══════════════════════════════════════════════════════════
6. 真实模型请求：0
═══════════════════════════════════════════════════════════

本次全部测试为 Provider-free（ScriptedModelDriver / fake client 单测）
或冻结环境 E2E（fake provider client，无网络）。未启动任何真实模型
Canary、pilot 或在线实验。**未验证内容均如实标注（§5）。**

═══════════════════════════════════════════════════════════
7. 限制与未解决问题
═══════════════════════════════════════════════════════════

1. 既有 flaky 测试（§1.D）待独立修复。
2. 同批无关失败对 commit 类调用的保守拦截仍在（B1-5，进误拦截率指标）。
3. REPLAN_EXHAUSTED 在 canonical 路径实际难以触发（授权门 R2 先于
   决策层终止同签名重复）——作为纵深防御分支保留，仅纯决策层可测。
4. 非 canonical 路径已排除出统计但未补统一事件持久化（协议选项 2）。
5. 实验协议的阈值、样本量终值、真实模型实验授权均待人工批准。

═══════════════════════════════════════════════════════════
8. 最终 Gate 自检（阶段 G 十项）
═══════════════════════════════════════════════════════════

1. Git 可追溯 ✓（分支 + 本报告记录的 SHA，工作区仅含声明的文件）
2. Canary-005/006 产物未修改 ✓（只读；mtime 保持 09-29）
3. ToolMaze/sandbox/Judge 未修改 ✓
4. 无真实模型请求、无在线实验 ✓（§6）
5. 恢复路线规则有对应测试与决策事件 ✓
6. 拦截/执行/恢复/最终结果可区分 ✓（事件 + 归因测试）
7. Manifest/provenance 足以识别关键实验条件 ✓（execution_provenance +
   协议冻结清单；manifest 生成侧补字段列为后续 run 的要求）
8. 测试结果全部真实、失败与跳过未隐藏 ✓（含 flaky 如实标记）
9. 测评协议已冻结 ✓；待人工批准事项已列（协议 §8）
10. Journal 与代码/测试/报告一致 ✓
