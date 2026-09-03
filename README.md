<div>
<p align="center">
  <img src="./docs/assets/odys-logo.png" width="170" alt="Odys logo">
</p>
</div>

<div>
<h1 align="center">Odys</h1>
</div>

<div>
<p align="center">
  <strong>面向长时任务 AI Agent 的可靠执行运行时</strong>
</p>
</div>

<div>
<p align="center">
  <em>Reliable execution for long-running AI agents.</em>
</p>
</div>

<div>
<p align="center">
  不只让 Agent “做事”，更让它能够证明任务真的完成了。
</p>
</div>

<div>
<p align="center">
  <a href="https://github.com/Xixiartemis/Odys/actions">
    <img src="https://img.shields.io/github/actions/workflow/status/Xixiartemis/Odys/test.yml?branch=main&style=flat-square" alt="CI">
  </a>
  <a href="./LICENSE">
    <img src="https://img.shields.io/github/license/Xixiartemis/Odys?style=flat-square" alt="License">
  </a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/status-experimental-orange?style=flat-square" alt="Status">
</p>
</div>

<div>
<p align="center">
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-为什么是-odys">为什么是 Odys</a> ·
  <a href="#-它是怎么工作的">工作原理</a> ·
  <a href="#-架构">架构</a> ·
  <a href="#-路线图">路线图</a>
</p>
</div>

---

## Odys 是什么？

很多 Agent Runtime 擅长回答一个问题：

> **模型下一步应该做什么？**

Odys 更关注另一个问题：

> **一个长时间运行的 Agent，怎么证明自己真的完成了任务？如果失败了，又如何可靠恢复？**

Odys 是一个面向 **长时任务（long-horizon tasks）**  的实验性 Agent Runtime，重点研究：

* **可验证完成（Verified Completion）**
* **持久化执行状态（Durable Execution State）**
* **失败归因与恢复（Failure Provenance &amp; Recovery）**
* **Checkpoint / Resume**
* **Runtime Truth**
* **按任务难度动态提升可靠性（Adaptive Reliability）**

核心原则：

> **Agent 说 “done”，不等于任务真的完成。**

Odys 将执行和完成判定拆开：

```text
Agent 执行
   ↓
Completion Claim
   ↓
独立验证
   ↓
Verified Outcome
```

对于简单任务，运行时应该尽量轻量。

对于复杂、长时、容易失败的任务，再逐步启用更强的验证、恢复、Checkpoint、Workflow 和 Replan 机制。

> **North Star：用尽可能少的可靠性机制，换取一个真正被验证过的结果。**

---

## ✨ 为什么是 Odys？

长时 Agent 的问题，往往已经不只是 Prompt 问题。

它们可能会：

* 在任务仍然有错误时提前宣称完成；
* 进程中断后丢掉大量已经完成的工作；
* 重试时重复执行副作用；
* 环境变化后继续执行已经过期的计划；
* 失败后丢失真正的根因；
* Provider / Model 已经切换，但 Runtime 状态仍记录旧目标；
* 进程看似还活着，却长时间没有有效进展；
* 在一个很简单的任务上消耗大量模型轮次和工具调用。

Odys 把这些问题视为 **Runtime Semantics**，而不是单纯依赖更强的模型或更长的提示词。

### 核心问题与机制

| 问题                      | Odys 机制 |
| --------------------------- | ----------- |
| Agent 过早说“完成”      | **CompletionAuthority + Validator**          |
| 长任务过程中进程中断      | **Durable Task / Run / Attempt**          |
| 失败后继续执行            | **Failure Classification + Recovery**          |
| 恢复后丢工作              | **Checkpoint / Resume**          |
| 失败根因被泛化            | **Failure Provenance**          |
| 计划与现实不一致          | **Selective Repair + Macro Replan**          |
| Provider / Model 身份漂移 | **Runtime Truth**          |
| “活着”但没有进展        | **Execution Liveness**          |
| 简单任务也套重型 Harness  | **Adaptive Reliability Levels**          |

Odys 不试图替代模型自己的微观推理。

> **Odys 决定下一步必须达成什么“可验证结果”；Agent 自己决定怎么做到。**

---

## 🚀 快速开始

### 环境要求

* Python 3.11+
* Git
* [`uv`](https://docs.astral.sh/uv/)

Clone：

```bash
git clone https://github.com/Xixiartemis/Odys.git
cd Odys
```

安装完整开发依赖：

```bash
uv sync --extra dev --extra live --extra agent
```

运行测试：

```bash
uv run pytest
```

初始化：

```bash
uv run odys init-db
```

### 运行一个 Native Agent 任务

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

检查持久化 Run：

```bash
uv run odys inspect <RUN_ID>
```

Odys 特别强调：

```text
Agent 自己执行 pytest
        ≠
任务已经被验证

模型说 “done”
        ≠
任务已经被验证

Odys 的权威 Validator 接受证据
        =
Verified Completion
```

---

## 🧭 它是怎么工作的？

一个最小 Odys 执行闭环：

```text
Goal
 │
 ▼
┌──────────────────────┐
│ Native Agent Runtime │
│                      │
│ Model → Tool         │
│   ↑       ↓          │
│ State ← Observation  │
└──────────┬───────────┘
           │
           │ Completion Claim
           ▼
┌──────────────────────┐
│ CompletionAuthority  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Authoritative        │
│ Validator            │
└──────────┬───────────┘
           │
       ┌───┴────┐
       │        │
      PASS     FAIL
       │        │
       ▼        ▼
   VERIFIED   CLASSIFY
                │
                ▼
             RECOVER
```

Agent 负责微观执行：

```text
读文件
→ 搜索符号
→ 修改代码
→ 执行命令
→ 阅读结果
→ 决定下一步 Tool Call
```

Odys 负责运行时语义：

```text
Task
Run
Attempt
Completion
Validation
Failure
Recovery
Checkpoint
Runtime Truth
```

---

## 🛡️ Adaptive Reliability

Odys 不希望所有任务都使用一个重型 Harness。

可靠性机制应该按需启用。

| Level | 模式 | Runtime Contract                                     |
| ------- | ------ | ------------------------------------------------------ |
| 0     | **FAST**     | Native Model/Tool Loop + Runtime Truth               |
| 1     | **GUARDED**     | FAST + CompletionAuthority + Validator               |
| 2     | **DURABLE**     | GUARDED + Workflow + Checkpoint + Recovery + Replan  |
| 3     | **MULTI_AGENT**     | DURABLE + Durable Delegation + Dependency Scheduling |

概念上：

```text
简单任务
   │
   ▼
 FAST
   │
需要独立验证？
   ▼
 GUARDED
   │
需要恢复 / 长时状态？
   ▼
 DURABLE
   │
需要委派多个 Agent？
   ▼
 MULTI_AGENT
```

目标不是：

> “机制越多越可靠。”

而是：

> **Minimum Sufficient Reliability —— 最小充分可靠性。**

---

## 🏗️ 架构

```text
┌─────────────────────────────────────────────────────┐
│ Product Surfaces                                    │
│ CLI · API · TUI · future Web                       │
├─────────────────────────────────────────────────────┤
│ Capability Runtime                                  │
│ Tools · MCP · Skills · Retrieval · Memory          │
│ Browser · Search · Code · Shell · Sandbox          │
├─────────────────────────────────────────────────────┤
│ Native Minimal Agent Runtime                        │
│ Context → Model → Tool → Observation → State       │
├─────────────────────────────────────────────────────┤
│ Verified Workflow Runtime                           │
│ TaskGraph · Dependencies · Preconditions            │
│ Acceptance · Evidence · Repair · Replan            │
├─────────────────────────────────────────────────────┤
│ Adaptive Reliability Control Plane                  │
│ Task · Run · Attempt · CompletionAuthority          │
│ Validation · Failure · Recovery · Checkpoint        │
│ Runtime Truth · Liveness · Budget · Cost           │
└─────────────────────────────────────────────────────┘
```

---

## 1. Native Agent Runtime

最小执行内核：

```text
Context
  ↓
Model
  ↓
Tool
  ↓
Observation
  ↓
State
  └──────→ Next Turn
```

模型仍然保留 Tool-level 的自主性。

Odys 不会在中心 Planner 中预先规划模型每一次 read / edit / shell 调用。

---

## 2. Capability Runtime

Capability 回答：

> **Agent 能做什么？**

Odys 的策略不是把所有生态重新造一遍，而是尽量复用成熟实现：

```text
MCP
Skills
Filesystem
Shell / Code
Retrieval / RAG
Memory
Browser / Search
Sandbox
Delegation
```

Odys 自己重点拥有：

```text
Capability Policy
Risk
Side Effect
Evidence
Verification
Recovery Semantics
```

而不是重新实现底层协议、向量索引、浏览器引擎或 OAuth。

---

## 3. Verified Workflow Runtime

长时任务最终需要的不只是“更多 Model Turns”。

Odys 计划把更大的任务建模为带验证语义的 Step：

```text
Step
├── goal
├── dependencies
├── preconditions
├── expected_effects
├── acceptance_criteria
├── evidence
├── capabilities
├── risk
├── budget
└── recovery_policy
```

状态转换：

```text
PLANNED
   ↓
READY
   ↓
RUNNING
   ↓
CLAIMED_COMPLETE
   ↓
Validator
   ├── VERIFIED
   │
   └── CLASSIFIED_FAILURE
            ↓
       Local Repair
            ↓
 Affected-Subgraph Repair
            ↓
       Macro Replan
```

关键点：

> **Agent 声称一个 Step 完成，不会自动让 Workflow 进入 VERIFIED。**

---

## 4. Reliability Control Plane

Odys 的可靠性层负责持久化执行事实。

```text
Task
└── Run
    ├── Attempt 1
    ├── Attempt 2
    └── Attempt N
```

核心对象：

* **CompletionAuthority**
  将模型的完成声明和真正的完成授权分开。
* **Validator**
  提供独立、可重复的验收证据。
* **Failure Provenance**
  保留真实根因，避免错误地把具体失败折叠成泛化标签。
* **Checkpoint / Resume**
  在长时任务中保留有价值的执行状态。
* **Runtime Truth**
  区分 configured / effective / actual transport。
* **Execution Liveness**
  区分“最近有实际业务进展”和“进程还存在”。
* **Budget**
  控制模型轮次、工具调用、Attempt 和未来可靠性成本。

---

## 🔍 Runtime Truth

一个 Agent Runtime 必须知道：

> **到底是谁执行了这次请求？**

Odys 区分：

```text
Configured Target
       ↓
Effective Target
       ↓
Actual Transport
```

例如：

```text
Configured
mimo / mimo-v2.5

Effective
mimo / mimo-v2.5

Actual Transport
https://provider.example/v1
```

Runtime Truth 对这些场景非常重要：

* Provider fallback
* Model migration
* Resume
* Experiment reproducibility
* Cost attribution
* Failure attribution

如果 Runtime 无法证明 actual transport 与 durable target 一致，应该 fail closed，而不是猜测。

---

## ✅ Verified Completion

Odys 把“完成”当成一个协议：

```text
Model
  │
  │ "我认为已经完成"
  ▼
Completion Candidate
  │
  ▼
CompletionAuthority
  │
  ▼
Authoritative Validator
  │
  ├── PASS → 可以进入完成状态
  │
  └── FAIL → Classification + Recovery
```

这里最重要的区别：

```text
Agent 自己执行测试
       ↓
Execution Evidence

Odys 外部 Validator 执行测试
       ↓
Acceptance Evidence
```

这样可以避免一个非常常见的 Agent False Green：

> 模型因为自己看到的输出“看起来正确”，就认为整个任务已经成功。

---

## ♻️ Failure & Recovery

Failure 是一等 Runtime State。

理想的失败因果链：

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

具体失败不应该静默折叠成无关的通用类型。

例如：

```text
BUDGET_EXHAUSTED
```

不应该在没有明确映射关系的情况下变成：

```text
EMPTY_RESULT
```

因为一旦根因丢失，后续恢复策略、统计分析和 Benchmark 都会失真。

---

## 💾 Durable State ≠ Prompt State

Odys 不把模型上下文窗口当数据库。

Durable State 可以包含：

```text
Task / Run / Attempt
Workflow State
Events
Validation Evidence
Failure Reports
Checkpoints
Memory
Artifacts
```
