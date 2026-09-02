# LHAS 领域模型

Status: **Compatibility reference.** Canonical product ownership and layer boundaries are defined in `docs/01_ARCHITECTURE.md`; the durable Task/Run/Attempt model below remains authoritative.

## 三层执行模型
```text
Project
  └─ Task
      └─ Run
          ├─ Attempt 1
          ├─ Attempt 2
          └─ Attempt 3
```

- **Task**：业务目标本身。
- **Run**：在固定实验配置下执行一次完整 Task。
- **Attempt**：Run 中一次具体 Agent 尝试；Recovery 后再次执行即产生新 Attempt。

只要模型、Harness Version、Context Policy、Dataset Version 等实验条件发生变化，就创建新 Run。

## 核心对象

### Project
- id
- name
- type
- root_path
- created_at

### Task
- id
- project_id
- title
- objective
- constraints
- acceptance_criteria
- status
- created_at
- updated_at

建议状态：
`CREATED / READY / RUNNING / VALIDATING / RECOVERING / COMPLETED / FAILED / BLOCKED / ESCALATED / CANCELLED`

### Run
- id
- task_id
- experiment_id
- executor_type
- provider
- model
- harness_version
- context_policy_version
- dataset_version
- status
- started_at
- finished_at

### Attempt
- id
- run_id
- attempt_number
- status
- context_snapshot_id
- started_at
- finished_at
- executor_result
- usage
- failure_type

### Event
- id
- task_id
- run_id
- attempt_id
- sequence
- event_type
- timestamp
- payload

### ValidationResult
- id
- attempt_id
- passed
- level
- checks
- evidence
- stdout
- stderr
- duration

### FailureReport
- id
- attempt_id
- failure_type
- evidence
- summary
- confidence
- suggested_recovery

### RecoveryAction
- id
- attempt_id
- action_type
- reason
- context_policy
- created_at

## Job Benchmark 专用版本对象
- resume_version
- candidate_profile_version
- career_goal_version
- jd_dataset_version
- ground_truth_version

任何一个发生改变，正式实验必须生成新的 Dataset / Experiment 标识。
Phase D adds provider-neutral Goal, Plan, PlanStep and Capability entities. Plan persistence keeps declared inputs separate from runtime execution context and records step output/artifacts/usage.
