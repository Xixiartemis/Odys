<p align="center">
  <img src="./docs/assets/odys-logo.png" width="170" alt="Odys logo">
</p>

<h1 align="center">Odys</h1>

<p align="center">
  <strong>面向长时任务 AI Agent 的可靠执行运行时</strong>
</p>

<p align="center">
  <em>Reliable execution for long-running AI agents.</em>
</p>

<p align="center">
  不只让 Agent “做事”，更让它能够证明任务真的完成了。
</p>

<p align="center">
  <a href="https://github.com/Xixiartemis/Odys/actions">
    <img src="https://img.shields.io/github/actions/workflow/status/Xixiartemis/Odys/test.yml?branch=main&style=flat-square" alt="CI">
  </a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue?style=flat-square" alt="Python >= 3.11">
  <img src="https://img.shields.io/badge/status-experimental-orange?style=flat-square" alt="Experimental status">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#为什么是-odys">为什么是 Odys</a> ·
  <a href="#-工作原理">工作原理</a> ·
  <a href="#-架构与归属">架构</a> ·
  <a href="#-路线图">路线图</a>
</p>

---

## Odys 是什么？

很多 Agent Runtime 关注模型下一步应该做什么。Odys 关注的是另一件事：

> 一个长时间运行的 Agent，如何证明自己真的完成了任务？失败之后，如何保留根因并可靠恢复？

Odys 是一个面向 **long-horizon tasks** 的实验性 Agent Runtime，研究重点包括：

- Verified Completion
- Durable Task / Run / Attempt 状态
- Failure Provenance & Recovery
- Checkpoint / Resume
- Runtime Truth
- Adaptive Reliability

核心原则很简单：

```text
Agent 说 “done”
        ≠
任务已经被验证
```

Odys 将执行和完成判定分开：

```text
Agent 执行 → Completion Claim → 独立 Validator → Verified Outcome
```

North Star：用尽可能少的可靠性机制，换取一个真正被验证过的结果。

## 为什么是 Odys？

长时 Agent 的可靠性问题不只是 Prompt 问题。进程中断、重复副作用、过期计划、失败根因丢失、Runtime Truth 漂移，以及“进程还活着但没有进展”，都属于 Runtime Semantics。

| 问题 | Odys 机制 |
| --- | --- |
| Agent 过早声称完成 | CompletionAuthority + Validator |
| 长任务中断后丢失状态 | Durable Task / Run / Attempt + Checkpoint / Resume |
| 失败后继续执行却丢失根因 | Failure Classification + Failure Provenance + Recovery |
| 计划与现实不一致 | Selective Repair + Macro Replan |
| Provider / Model 身份漂移 | Runtime Truth |
| 进程存在但没有有效进展 | Execution Liveness |
| 简单任务被重型 Harness 拖慢 | Adaptive Reliability |

Odys 决定下一步必须达成什么“可验证结果”；Agent 自己决定怎么做到。

## 快速开始

### 环境要求

- Python 3.11+
- Git
- [`uv`](https://docs.astral.sh/uv/)

安装完整开发依赖：

```bash
uv sync --extra dev --extra live --extra agent
```

运行确定性测试：

```bash
uv run pytest
```

初始化数据库：

```bash
uv run odys init-db
```

### 运行 Native Agent 任务

```bash
uv run odys run \
  --repo /path/to/project \
  --kernel native \
  --verify "pytest -q" \
  "Fix the failing tests and verify the implementation."
```

Windows PowerShell：

```powershell
uv run odys run `
  --repo D:\path\to\project `
  --kernel native `
  --verify "pytest -q" `
  "Fix the failing tests and verify the implementation."
```

检查或恢复持久化 Run：

```bash
uv run odys inspect <RUN_ID>
uv run odys resume <RUN_ID>
```

`--verify` 指定的是权威验证命令。Agent 自己执行测试产生的是 execution evidence，不等同于 Validator 接受的 acceptance evidence。

## 工作原理

最小执行闭环如下：

```text
Goal
 │
 ▼
Native Agent Runtime
   Model → Tool → Observation → State → Next Turn
 │
 └── Completion Claim
          │
          ▼
   CompletionAuthority
          │
          ▼
   Authoritative Validator
      ├── PASS → VERIFIED
      └── FAIL → CLASSIFY → RECOVER
```

Agent 负责微观执行：读文件、搜索符号、编辑代码、执行命令、阅读结果、决定下一步 Tool Call。Odys 负责 Task、Run、Attempt、Validation、Failure、Recovery、Checkpoint 和 Runtime Truth 等运行时语义。

## 架构与归属

Odys 遵循冻结的五层架构：

```text
Product Surfaces
        ↓
Capability Runtime
        ↓
Native Minimal Agent Runtime
        ↓
Verified Workflow Runtime
        ↓
Adaptive Reliability Control Plane
```

架构归属边界：

- **Product Surfaces**：CLI、API、TUI 及未来产品入口。
- **Capability Runtime**：Tools、MCP、Skills、Retrieval、Memory、Browser、Shell、Sandbox 等能力接入。
- **Native Minimal Agent Runtime**：`Context → Model → Tool → Observation → State`。
- **Verified Workflow Runtime**：TaskGraph、依赖、前置条件、验收、证据、Repair 和 Replan。
- **Adaptive Reliability Control Plane**：Task / Run / Attempt、CompletionAuthority、Validation、Failure、Recovery、Checkpoint、Runtime Truth 和 Liveness。

Odys owns execution semantics。成熟开源项目提供 primitives、protocols、infrastructure 和 capability implementations，不拥有竞争性的 Odys lifecycle。

更多冻结决策见 [`docs/ARCHITECTURE_FREEZE.md`](docs/ARCHITECTURE_FREEZE.md) 和 [`docs/01_ARCHITECTURE.md`](docs/01_ARCHITECTURE.md)。

## Adaptive Reliability

可靠性机制按任务需要逐步启用：

| Level | 模式 | Runtime Contract |
| --- | --- | --- |
| 0 | **FAST** | Native Model/Tool Loop + Runtime Truth |
| 1 | **GUARDED** | FAST + CompletionAuthority + Validator |
| 2 | **DURABLE** | GUARDED + Workflow + Checkpoint + Recovery + Replan |
| 3 | **MULTI_AGENT** | DURABLE + Durable Delegation + Dependency Scheduling |

目标不是机制越多越可靠，而是 **Minimum Sufficient Reliability**。

## 当前状态与能力边界

当前代码库已经具备这些基础能力：

- Native Agent Kernel 与 real model/tool loop foundation
- Durable Task / Run / Attempt
- Tool execution 与 durable observations
- CompletionAuthority 与 deterministic validation
- Failure Classification / Recovery
- Checkpoint / Resume foundations
- Runtime Truth
- Execution Health / Liveness

Odys 仍处于实验性开发阶段，正在研究 long-horizon reliable execution。当前不宣称：

- Production Ready
- 能可靠完成任意 long-horizon task
- 优于 Pi、Hermes 或 LangGraph
- Token Optimal
- MCP、Memory、RAG、Browser 或 Multi-Agent workflow 已全部完成

当前重点是：**先证明 Runtime Invariants，再扩展 Capability Surface。**

## Failure & Recovery

Failure 是一等 Runtime State。理想的因果链是：

```text
Executor Terminal Reason
        ↓
Attempt.error_type
        ↓
FailureReport
        ↓
RecoveryPolicy
        ↓
Recovery Action
```

具体根因不能在没有明确映射的情况下静默折叠成无关的通用类型。例如 `BUDGET_EXHAUSTED` 不应被误报为 `EMPTY_RESULT`。否则恢复决策、统计和 benchmark 结论都会失真。

## Capability Strategy

Odys 采用 **Reuse First**：底层协议、基础设施和能力实现优先复用成熟生态，Odys 自己聚焦 Execution Lifecycle、Verification、Recovery 和 Runtime Truth。

以下是规划方向，不代表当前全部已实现：

- Official MCP SDK
- SKILL.md-style progressive Skills
- mature Retrieval / RAG infrastructure
- pluggable Memory Provider
- existing Browser automation runtime
- external Sandbox backend
- OpenTelemetry-compatible telemetry

## Engineering Philosophy

Odys 的工程闭环是：

```text
Reproducible Failure
        ↓
Baseline Evidence
        ↓
Mechanism Hypothesis
        ↓
Minimal Implementation
        ↓
Deterministic Regression
        ↓
Live Experiment
        ↓
Measured Result
```

两条重要边界：

> Green tests != invariant proven.

> Live task finished != verified success.

没有真实数据的指标保持 `NOT_MEASURED`，而不是估算。

## 路线图

```text
Phase 0  Architecture Freeze
   ↓
Phase 1  Basic Native Vertical Slice
   ↓
Phase 2  Minimum Capability Parity
   ↓
Phase 3  Verified Workflow V1
   ↓
Phase 4  Controlled Ablation
   ↓
Phase 5  Adaptive Reliability
   ↓
Phase 6  Capability Expansion
   ↓
Phase 7  Productionization
```

详细路线见 [`docs/14_ROADMAP.md`](docs/14_ROADMAP.md)。产品与研究方向见 [`docs/PRODUCT_DIRECTION.md`](docs/PRODUCT_DIRECTION.md)。

## 开发与文档

```bash
uv sync --extra dev --extra live --extra agent
uv run pytest
uv run python -m pip check
```

核心文档：

- [`docs/ARCHITECTURE_FREEZE.md`](docs/ARCHITECTURE_FREEZE.md) — 冻结架构决策
- [`docs/01_ARCHITECTURE.md`](docs/01_ARCHITECTURE.md) — Runtime 架构
- [`docs/09_EVAL_PROTOCOL.md`](docs/09_EVAL_PROTOCOL.md) — 实验与验证协议
- [`docs/14_ROADMAP.md`](docs/14_ROADMAP.md) — Roadmap
- [`AGENTS.md`](AGENTS.md) — Coding Agent 开发约束

项目结构：

```text
Odys/
├── src/lhas/              # Runtime implementation
├── tests/                 # Deterministic regression suite
├── docs/                  # Architecture / specifications / evidence
├── experiments/           # Experimental records
├── evals/                 # Evaluation records and fixtures
├── tasks/                 # Task specifications
├── scripts/               # Validation / experiment tooling
└── AGENTS.md              # Coding-agent engineering policy
```

历史 evidence 尽量保留原始事实，不为了适配新术语而重写。

---

<p align="center">
  <img src="./docs/assets/odys-logo.png" width="72" alt="Odys">
</p>

<p align="center">
  <strong>让 Agent 不只是完成任务，而是能够证明它完成了。</strong>
</p>

<p align="center">
  <em>Build agents that can prove they finished.</em>
</p>
