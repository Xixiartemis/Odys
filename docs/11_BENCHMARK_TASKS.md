# Benchmark Tasks

Status: **Historical benchmark catalogue.** Canonical comparison philosophy and current development gates are defined in `docs/PRODUCT_DIRECTION.md` and `docs/14_ROADMAP.md`.

## Benchmark 目标
测试集必须固定、可重复、可对比。

不能每次临时找不同任务然后比较成功率。

## Benchmark Suite

### A. Job Application Benchmark
第一 Golden Task：

`Search → Parse → Filter → Match → Rank → Prepare → Validate → Approval → Submit`

### B. SWE Benchmark
第二 Benchmark：

`Task → Code Change → Test → Failure → Recovery → Regression`

## Job Dataset V0.1
建议固定：
- Resume V1
- Candidate Profile V1
- Career Goal V1
- 30 个 JD 快照
- Ground Truth V1

JD 组成：
- 10 个高匹配
- 10 个中匹配
- 10 个明显不匹配

故意包含：
- 重复岗位
- 过期岗位
- AI 全栈
- Agent 应用
- AI Coding
- AI 前端
- 算法岗
- 学历不满足
- 城市不匹配
- 技能部分匹配

## Ground Truth
每个 JD 至少标注：
- hard_constraints_pass
- expected_fit: HIGH / MEDIUM / LOW
- expected_positive_evidence
- expected_risks
- expiration_status
- duplicate_group

## Benchmark 难度
- **L0**：Runtime 自测
- **L1**：单步骤 / 单来源
- **L2**：多来源、多字段
- **L3**：第一次较容易失败，需要 Recovery
- **L4**：搜索、解析、匹配、排序、准备申请的长链路
- **L5**：跨系统、包含真实申请动作；V1 后正式使用

## Stage Tasks
### Stage 0
MockExecutor：
- Fail Once → Pass
- Timeout
- Crash
- 连续失败 → Escalated

### Stage 1
离线 30 JD：
- hard constraints
- fit classification
- ranking
- evidence extraction

### Stage 2
实时搜索：
- 搜索公开岗位
- 去重
- 失效判断
- 结构化 JD

### Stage 3
长任务：
- Search → Match → Rank → Top N → Application Draft

### Stage 4
Dry Run：
- 自动填写到 Submit 前
- 验证字段
- Human Approval Gate

### Stage 5
真实 Submit：
仅在明确人工批准后执行。

## Holdout
建议：
- Development Dataset：5 tasks
- Evaluation Dataset：10–15 tasks
- Holdout Dataset：5 tasks
