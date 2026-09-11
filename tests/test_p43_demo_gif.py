import hashlib
import json
from pathlib import Path

import pytest

from evals.reliability.demo_gif.generate_gifs import (
    TraceEventError,
    generate_gifs,
)


def _event(event_type, *, config=None, index=0, status="observed", metadata=None):
    details = dict(metadata or {})
    if config is not None:
        details["config"] = config
    return {
        "timestamp": f"2026-01-01T00:00:{index:02d}Z",
        "event_type": event_type,
        "task_id": "DEMO-01",
        "step_id": f"step-{index % 3}",
        "attempt_id": f"attempt-{index // 3}",
        "status": status,
        "metadata": details,
    }


def _trace(path: Path, *, include_unknown=True):
    events = [
        _event("TASK_CREATED", index=0),
        _event("STEP_DISPATCHED", config="minimal", index=1),
        _event("TOOL_CALL_STARTED", index=2),
        _event("FAULT_INJECTED", index=3, metadata={"failure_class": "TOOL_ERROR"}),
        _event("FAILURE_DETECTED", config="minimal", index=4, metadata={"failure_class": "TOOL_ERROR"}),
        _event("STEP_DISPATCHED", config="minimal", index=5),
        _event("VERIFICATION_STARTED", index=6),
        _event("VERIFICATION_FAILED", index=7, status="rejected"),
        _event("STEP_VERIFIED", index=8),
        _event("REPAIR_STARTED", config="odys_p3", index=9, metadata={"repair_scope": "LOCAL"}),
        _event("REPAIR_COMPLETED", config="odys_p3", index=10, metadata={"repair_scope": "LOCAL"}),
        _event("VERIFICATION_PASSED", config="odys_p3", index=11, status="passed"),
    ]
    if include_unknown:
        events.insert(4, _event("FUTURE_EVENT", index=12, metadata={"source": "forward-compatible"}))
    path.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")


def test_same_trace_produces_deterministic_gif_bytes(tmp_path):
    trace = tmp_path / "trace.jsonl"
    _trace(trace)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = generate_gifs(trace, first_dir)
    second = generate_gifs(trace, second_dir)
    for story in first:
        assert hashlib.sha256(first[story].read_bytes()).digest() == hashlib.sha256(second[story].read_bytes()).digest()


def test_missing_required_event_fails_clearly(tmp_path):
    trace = tmp_path / "missing.jsonl"
    _trace(trace)
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    records = [record for record in records if record["event_type"] != "REPAIR_COMPLETED"]
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(TraceEventError, match="MISSING_REQUIRED_EVENT:selective_repair:REPAIR_COMPLETED"):
        generate_gifs(trace, tmp_path / "out")


def test_unknown_event_does_not_crash_and_outputs_all_gifs(tmp_path):
    trace = tmp_path / "unknown.jsonl"
    _trace(trace, include_unknown=True)
    outputs = generate_gifs(trace, tmp_path / "out")
    assert len(outputs) == 3
    assert all(path.exists() and path.stat().st_size > 0 for path in outputs.values())
    assert all(path.read_bytes().startswith(b"GIF89a") for path in outputs.values())


def test_invalid_trace_schema_fails_before_render(tmp_path):
    trace = tmp_path / "invalid.jsonl"
    trace.write_text(json.dumps({"event_type": "TASK_CREATED"}) + "\n", encoding="utf-8")
    with pytest.raises(TraceEventError, match="TRACE_SCHEMA_MISSING"):
        generate_gifs(trace, tmp_path / "out")
