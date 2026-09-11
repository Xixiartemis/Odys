"""Determinism tests for the frozen benchmark execution environment (P43)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

RELIABILITY = Path(__file__).resolve().parent.parent / "evals" / "reliability"
ENVIRONMENT_PATH = RELIABILITY / "environment.json"

REQUIRED_FIELDS = [
    "runner_git_sha",
    "protocol_hash",
    "benchmark_version",
    "python_version",
    "os",
    "dependency_lock_hash",
    "model_identity",
    "provider_identity",
    "budget_identity",
    "created_at",
]


@pytest.fixture()
def env() -> dict:
    """Load the frozen environment record."""
    return json.loads(ENVIRONMENT_PATH.read_text(encoding="utf-8"))


def test_environment_file_exists():
    assert ENVIRONMENT_PATH.is_file(), f"Missing {ENVIRONMENT_PATH}"


def test_all_required_fields_present(env: dict):
    missing = [f for f in REQUIRED_FIELDS if f not in env]
    assert not missing, f"Missing required fields: {missing}"


def test_no_required_field_is_empty(env: dict):
    empty = [f for f in REQUIRED_FIELDS if not env.get(f)]
    assert not empty, f"Empty required fields: {empty}"


def test_protocol_hash_determinism(env: dict):
    """Recompute protocol_hash and verify it matches the stored value."""
    from evals.reliability.phase4_v1.validate import protocol_hash

    recomputed = protocol_hash()
    assert recomputed == env["protocol_hash"], (
        f"protocol_hash drift: stored={env['protocol_hash']}, computed={recomputed}"
    )


def test_budget_identity_determinism(env: dict):
    """Recompute budget_identity from protocol.json and verify it matches."""
    protocol = json.loads(
        (RELIABILITY / "phase4_v1" / "protocol.json").read_text(encoding="utf-8")
    )
    canonical = json.dumps(
        protocol["budgets"],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    recomputed = hashlib.sha256(canonical).hexdigest()
    assert recomputed == env["budget_identity"], (
        f"budget_identity drift: stored={env['budget_identity']}, computed={recomputed}"
    )


def test_benchmark_version(env: dict):
    assert env["benchmark_version"] == "phase4-v1"


def test_runner_git_sha_format(env: dict):
    sha = env["runner_git_sha"]
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), (
        f"Invalid git SHA format: {sha}"
    )
