"""Phase C hardening: a Job validation failure must traverse the Runtime loop."""

import asyncio
import json
from pathlib import Path

from lhas.domain.enums import EventType, RunStatus
from lhas.executors.general import GeneralAgentExecutor
from lhas.failure import RuleFailureClassifier
from lhas.job.models import load_job_dataset
from lhas.job.validation import JobMatchValidator
from lhas.persistence.event_store import EventStore
from lhas.persistence.phaseb_repos import (
    ContextSnapshotRepository,
    FailureReportRepository,
    ValidationResultRepository,
)
from lhas.recovery import DefaultRecoveryPolicy
from lhas.context_builder import ContextBuilder
from lhas.job.bench import _JobRuntimeOrchestrator
from lhas.job.recorder import JobExperimentRecorder
from lhas.job.metrics import MetricsReport
from lhas import HARNESS_VERSION


DATASET = Path(__file__).resolve().parents[1] / "benchmarks" / "job-v0.1"


class PromptDrivenFakeLLM:
    """Fake provider whose recovery behavior depends only on actual Prompt text."""

    model = "fake-model"
    provider = "fake-provider"

    def __init__(self):
        self.prompts: list[str] = []

    def chat_json(self, messages):
        prompt = messages[1]["content"]
        self.prompts.append(prompt)
        is_recovered = all(
            marker in prompt
            for marker in ("WRONG_MATCH", "Recovery guidance", "previous attempts")
        )
        fit = "LOW" if is_recovered else "HIGH"
        return {
            "fit": fit,
            "score": 30 if is_recovered else 90,
            "hard_constraints_pass": True,
            "evidence": ["Python"],
            "risks": [],
            "should_apply": True,
        }


def test_job_failure_recovery_runs_through_runtime(make_task, db, tmp_path):
    dataset = load_job_dataset(DATASET)
    task = make_task(title="job-runtime", objective="match JD-013", max_attempts=3)
    fake_llm = PromptDrivenFakeLLM()
    orchestrator = _JobRuntimeOrchestrator(
        db,
        jobs_by_task={task.id: dataset.jobs["JD-013"].model_dump(mode="json")},
        executor_factory=lambda: GeneralAgentExecutor(client=fake_llm),
        validator=JobMatchValidator(dataset),
        classifier=RuleFailureClassifier(),
        recovery_policy=DefaultRecoveryPolicy(context_policy="CP-2"),
        context_builder=ContextBuilder(
            policy="CP-2",
            profile={
                "candidate_profile": dataset.profile.model_dump(mode="json"),
                "career_goal": dataset.goal.model_dump(mode="json"),
            },
        ),
        context_policy_version="CP-2",
        harness_version=HARNESS_VERSION,
        provider="fake-provider",
        model="fake-model",
        dataset_version="JOB-V0.1",
        experiment_id="EXP-TEST-JOB-RUNTIME-001",
    )

    run = asyncio.run(orchestrator.execute_task(task.id))

    assert run.status is RunStatus.COMPLETED
    assert len(fake_llm.prompts) == 2
    assert '"job_id": "JD-013"' in fake_llm.prompts[0]
    assert '"candidate_id": "CAND-001"' in fake_llm.prompts[0]
    assert "target_roles" in fake_llm.prompts[0]
    assert "WRONG_MATCH" in fake_llm.prompts[1]
    assert "Recovery guidance" in fake_llm.prompts[1]
    assert "previous attempts" in fake_llm.prompts[1]
    attempts = orchestrator.attempt_repo.list_for_run(run.id)
    assert len(attempts) == 2
    assert attempts[0].context_snapshot_id != attempts[1].context_snapshot_id
    assert FailureReportRepository(db).list_for_attempt(attempts[0].id)[0].failure_type.value == "WRONG_MATCH"
    assert ValidationResultRepository(db).list_for_attempt(attempts[0].id)[0].attempt_id == attempts[0].id
    assert ValidationResultRepository(db).list_for_attempt(attempts[1].id)[0].passed is True
    assert ContextSnapshotRepository(db).list_for_attempt(attempts[0].id)[0].policy == "CP-2"
    assert ContextSnapshotRepository(db).list_for_attempt(attempts[1].id)[0].policy == "CP-3"
    event_types = [e.event_type for e in EventStore(db).list_for_task(task.id)]
    assert EventType.FAILURE_CLASSIFIED in event_types
    assert EventType.RECOVERY_STARTED in event_types
    assert EventType.TASK_COMPLETED in event_types

    recorder = JobExperimentRecorder(tmp_path / "experiments")
    exp_dir = recorder.record(
        experiment_id="EXP-TEST-JOB-RUNTIME-001",
        dataset_id="JOB-V0.1",
        ground_truth_status="DRAFT",
        predictor="llm-runtime",
        model="fake-model",
        provider="fake-provider",
        model_config={"base_url": "http://fake", "timeout": 5},
        harness_version=HARNESS_VERSION,
        context_policy_version="CP-2",
        recovery="ON",
        metrics=MetricsReport(
            n_jobs=1, hard_constraint_accuracy=1, fit_classification_accuracy=1,
            precision_at_5=1, recall_at_10=1, ranking_quality_ndcg10=1,
            evidence_accuracy=1, evidence_coverage=1, hallucination_rate=0,
            duplicate_detection_rate=1, expired_job_detection_rate=1,
        ),
        predictions=[], evaluations=[],
        git={"commit": "abc123", "branch": "test", "dirty_workspace": False},
        db=db, runs=[run],
    )
    metadata = json.loads((exp_dir / "experiment.json").read_text(encoding="utf-8"))
    assert metadata["provider"] == "fake-provider"
    assert metadata["model"] == "fake-model"
    assert metadata["model_config"]["timeout"] == 5
    task_dir = exp_dir / "tasks" / "job-runtime"
    assert (task_dir / "task.json").exists()
    assert (task_dir / "result.json").exists()
    assert (task_dir / "run.json").exists()
    attempt_dir = task_dir / "attempts" / "attempt-02"
    assert (attempt_dir / "context.json").exists()
    assert (attempt_dir / "validation.json").exists()
    assert (attempt_dir / "recovery.json").exists()
    run_metadata = json.loads((task_dir / "run.json").read_text(encoding="utf-8"))
    for key in ("experiment_id", "harness_version", "provider", "model", "dataset_version"):
        assert run_metadata[key] == metadata[key], key
    assert "baseline B0" not in (exp_dir / "summary.md").read_text(encoding="utf-8")
