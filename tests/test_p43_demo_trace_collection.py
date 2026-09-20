"""Integration checks for the demo-only real trace collector."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evals.reliability.demo_trace_collection import REQUIRED_TRACE_FIELDS, collect


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_collects_real_false_repair_and_recovery_traces(tmp_path):
    trace_dir = tmp_path / "demo_traces"
    gif_dir = tmp_path / "gifs"
    result = asyncio.run(collect(trace_dir, gif_dir))

    assert result["trace_count"] == 3
    false_rows = _rows(trace_dir / "false_completion_prevention.jsonl")
    selective_rows = _rows(trace_dir / "selective_repair.jsonl")
    baseline_rows = _rows(trace_dir / "baseline.jsonl")

    for rows in (false_rows, selective_rows, baseline_rows):
        assert rows
        assert all(set(REQUIRED_TRACE_FIELDS).issubset(row) for row in rows)
        # Source projections point at a durable EventStore event; the two
        # verifier boundary records explicitly identify the real verifier call.
        assert all(
            row["metadata"].get("source_event_id") is not None
            or row["metadata"].get("source") == "WorkflowVerifier.verify"
            for row in rows
        )

    false_types = [row["event_type"] for row in false_rows]
    assert false_types.index("AGENT_CLAIM") < false_types.index("WAITING_FOR_VERIFICATION")
    assert "VERIFICATION_FAILED" in false_types

    selective_types = [row["event_type"] for row in selective_rows]
    for required in (
        "FAILURE_DETECTED",
        "StepFailureProvenance",
        "REPAIR_STARTED",
        "REPAIR_COMPLETED",
        "VERIFICATION_PASSED",
        "STEP_VERIFIED",
    ):
        assert required in selective_types
    assert selective_rows[selective_types.index("FAILURE_DETECTED")]["step_id"] == "step-b"
    assert all(row["step_id"] == "step-b" for row in selective_rows if row["event_type"].startswith("REPAIR_"))

    assert len(baseline_rows) >= 2
    assert all(Path(path).is_file() for path in result["gif_files"])
