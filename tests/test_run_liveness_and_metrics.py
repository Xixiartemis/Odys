from datetime import datetime, timedelta, timezone

from lhas.cli_runtime import encode_cli_config, inspect_run
from lhas.cli_ui import project_view_state
from lhas.domain.enums import EventType, RunStatus
from lhas.domain.models import Attempt, Project, Run
from lhas.liveness import project_liveness
from lhas.persistence.database import Database
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import (
    AttemptRepository,
    ProjectRepository,
    RunRepository,
)
from lhas.task_service import create_task


def _time():
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _case(db, make_task):
    task = make_task(
        constraints=[
            encode_cli_config(
                verify_argv=["pytest", "-q"],
                max_turns=20,
                provider="mimo",
                model="mimo-v2.5",
                kernel="native",
            )
        ]
    )
    run = RunRepository(db).create(
        Run(task_id=task.id, status=RunStatus.RUNNING, executor_type="NativeAgentExecutor")
    )
    attempt = AttemptRepository(db).create(
        Attempt(run_id=run.id, attempt_number=1, status="RUNNING")
    )
    return task, run, attempt


def _append(db, event_type, *, run_id, attempt_id, timestamp, **payload):
    return EventStore(db).append(
        event_type,
        run_id=run_id,
        attempt_id=attempt_id,
        timestamp=timestamp,
        payload=payload,
    )


def test_native_metrics_are_event_derived_and_tui_consistent(db, make_task):
    _, run, attempt = _case(db, make_task)
    start = _time()
    for index in range(3):
        _append(
            db,
            EventType.NATIVE_MODEL_TURN_STARTED,
            run_id=run.id,
            attempt_id=attempt.id,
            timestamp=start + timedelta(seconds=index),
            turn=index + 1,
        )
    for index in range(2):
        _append(
            db,
            EventType.NATIVE_TOOL_STARTED,
            run_id=run.id,
            attempt_id=attempt.id,
            timestamp=start + timedelta(seconds=3 + index),
            invocation_id=f"inv-{index}",
            capability="workspace.read",
        )
        _append(
            db,
            EventType.NATIVE_TOOL_OBSERVED,
            run_id=run.id,
            attempt_id=attempt.id,
            timestamp=start + timedelta(seconds=4 + index),
            invocation_id=f"inv-{index}",
            capability="workspace.read",
            status="FAILURE" if index == 1 else "SUCCESS",
            error_type="TOOL_ERROR" if index == 1 else None,
        )

    inspection = inspect_run(
        db,
        run.id,
        now=start + timedelta(seconds=5),
        stall_threshold_seconds=600,
    )
    row = inspection["attempts"][0]
    state = project_view_state(
        inspection,
        goal="goal",
        provider="mimo",
        model="mimo-v2.5",
        max_attempts=3,
        max_turns=20,
    )

    assert row["turn_count"] == 3
    assert row["tool_calls"] == 2
    assert row["tool_failures"] == 1
    assert inspection["tools"]["total_turns"] == 3
    assert inspection["tools"]["total_calls"] == 2
    assert inspection["tools"]["total_failures"] == 1
    assert inspection["tools"]["calls_by_capability"] == {"workspace.read": 2}
    assert inspection["tools"]["failures_by_capability"] == {"workspace.read": 1}
    assert state["agent"]["turns"] == "3 / 20"
    assert state["agent"]["tool_calls"] == 2
    assert state["agent"]["tool_failures"] == 1


def test_running_attempt_metrics_and_last_durable_event_are_visible(db, make_task):
    _, run, attempt = _case(db, make_task)
    start = _time()
    _append(
        db,
        EventType.NATIVE_MODEL_TURN_STARTED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=start,
        turn=1,
    )
    last = _append(
        db,
        EventType.NATIVE_TOOL_STARTED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=start + timedelta(seconds=2),
        invocation_id="inv-1",
        capability="workspace.edit",
    )

    inspection = inspect_run(db, run.id, now=start + timedelta(seconds=3))

    assert inspection["attempts"][0]["status"] == "RUNNING"
    assert inspection["attempts"][0]["turn_count"] == 1
    assert inspection["attempts"][0]["tool_calls"] == 1
    assert inspection["attempts"][0]["last_event_cursor"] == last.id
    assert inspection["attempts"][0]["last_event_type"] == "NATIVE_TOOL_STARTED"


def test_fresh_process_preserves_event_derived_metrics(tmp_path):
    path = tmp_path / "runtime.db"
    first = Database(path)
    first.init_db()
    project = ProjectRepository(first).create(Project(name="fresh", type="test"))
    task = create_task(
        first,
        project_id=project.id,
        title="fresh",
        objective="inspect",
        constraints=[
            encode_cli_config(
                verify_argv=["pytest", "-q"],
                max_turns=20,
                provider="mimo",
                model="mimo-v2.5",
            )
        ],
    )
    run = RunRepository(first).create(Run(task_id=task.id, status="RUNNING"))
    attempt = AttemptRepository(first).create(
        Attempt(run_id=run.id, attempt_number=1, status="RUNNING")
    )
    _append(
        first,
        EventType.NATIVE_MODEL_TURN_STARTED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=_time(),
        turn=1,
    )
    first.close()

    second = Database(path)
    inspection = inspect_run(second, run.id, now=_time() + timedelta(seconds=1))
    second.close()

    assert inspection["attempts"][0]["turn_count"] == 1
    assert inspection["tools"]["total_turns"] == 1


def test_recent_real_progress_is_active_and_exposes_model_operation(db, make_task):
    _, run, attempt = _case(db, make_task)
    start = _time()
    _append(
        db,
        EventType.MODEL_CALL_STARTED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=start,
    )
    _append(
        db,
        EventType.NATIVE_MODEL_TURN_STARTED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=start + timedelta(seconds=1),
    )

    inspection = inspect_run(
        db,
        run.id,
        now=start + timedelta(seconds=5),
        stall_threshold_seconds=10,
    )

    live = inspection["run"]["liveness"]
    assert live["execution_health"] == "ACTIVE"
    assert live["current_operation"] == "MODEL_CALL"
    assert live["operation_age_ms"] == 5000
    assert live["last_progress_event_type"] == "NATIVE_MODEL_TURN_STARTED"


def test_old_unfinished_operation_is_stalled(db, make_task):
    _, run, attempt = _case(db, make_task)
    start = _time()
    call = _append(
        db,
        EventType.MODEL_CALL_STARTED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=start,
    )

    inspection = inspect_run(
        db,
        run.id,
        now=start + timedelta(seconds=11),
        stall_threshold_seconds=10,
    )
    live = inspection["run"]["liveness"]

    assert live["execution_health"] == "STALLED"
    assert live["current_operation"] == "MODEL_CALL"
    assert live["last_progress_event_cursor"] == call.id
    assert live["no_progress_duration_ms"] == 11000


def test_heartbeat_or_non_business_events_cannot_keep_run_active(db, make_task):
    _, run, attempt = _case(db, make_task)
    start = _time()
    progress = _append(
        db,
        EventType.MODEL_CALL_STARTED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=start,
    )
    for seconds in (5, 10, 15):
        _append(
            db,
            EventType.EXECUTOR_EVENT,
            run_id=run.id,
            attempt_id=attempt.id,
            timestamp=start + timedelta(seconds=seconds),
            heartbeat=True,
        )

    inspection = inspect_run(
        db,
        run.id,
        now=start + timedelta(seconds=16),
        stall_threshold_seconds=10,
    )
    live = inspection["run"]["liveness"]

    assert live["execution_health"] == "STALLED"
    assert live["last_progress_event_cursor"] == progress.id
    assert live["last_event_cursor"] > progress.id


def test_long_running_with_continuous_progress_is_not_stalled(db, make_task):
    _, run, attempt = _case(db, make_task)
    start = _time()
    for seconds in range(0, 1001, 100):
        _append(
            db,
            EventType.MODEL_RESPONSE_RECEIVED,
            run_id=run.id,
            attempt_id=attempt.id,
            timestamp=start + timedelta(seconds=seconds),
        )

    inspection = inspect_run(
        db,
        run.id,
        now=start + timedelta(seconds=1000),
        stall_threshold_seconds=150,
    )

    assert inspection["run"]["liveness"]["execution_health"] == "ACTIVE"
    assert inspection["run"]["liveness"]["no_progress_duration_ms"] == 0


def test_event_projection_overrides_stale_snapshot_counters(db, make_task):
    _, run, attempt = _case(db, make_task)
    start = _time()
    _append(
        db,
        EventType.NATIVE_EXECUTION_SNAPSHOT,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=start,
        model_turn_count=99,
        tool_call_count=99,
    )
    for index in range(3):
        _append(
            db,
            EventType.NATIVE_MODEL_TURN_STARTED,
            run_id=run.id,
            attempt_id=attempt.id,
            timestamp=start + timedelta(seconds=index + 1),
        )
    for index in range(2):
        _append(
            db,
            EventType.NATIVE_TOOL_STARTED,
            run_id=run.id,
            attempt_id=attempt.id,
            timestamp=start + timedelta(seconds=index + 4),
            capability="workspace.read",
        )

    inspection = inspect_run(db, run.id, now=start + timedelta(seconds=5))

    assert inspection["attempts"][0]["turn_count"] == 3
    assert inspection["attempts"][0]["tool_calls"] == 2


def test_terminal_run_is_idle_not_stalled(db, make_task):
    task = make_task(
        constraints=[
            encode_cli_config(
                verify_argv=["pytest", "-q"],
                max_turns=20,
                provider="mimo",
                model="mimo-v2.5",
            )
        ]
    )
    run = RunRepository(db).create(Run(task_id=task.id, status="COMPLETED"))
    attempt = AttemptRepository(db).create(
        Attempt(run_id=run.id, attempt_number=1, status="COMPLETED")
    )
    _append(
        db,
        EventType.RUN_COMPLETED,
        run_id=run.id,
        attempt_id=attempt.id,
        timestamp=_time(),
    )

    live = inspect_run(db, run.id, now=_time() + timedelta(hours=1))["run"]["liveness"]

    assert live["execution_health"] == "IDLE"


def test_liveness_projection_is_heartbeat_independent():
    start = _time()
    events = [
        {
            "id": 10,
            "event_type": EventType.MODEL_CALL_STARTED,
            "timestamp": start,
        },
        {
            "id": 11,
            "event_type": EventType.EXECUTOR_EVENT,
            "timestamp": start + timedelta(seconds=100),
        },
    ]

    live = project_liveness(
        events,
        run_status=RunStatus.RUNNING,
        now=start + timedelta(seconds=101),
        stall_threshold_seconds=10,
    )

    assert live["execution_health"] == "STALLED"
    assert live["last_progress_event_cursor"] == 10
