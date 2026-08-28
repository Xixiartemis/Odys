"""LHAS (Long-Horizon Agent System) — verifiable agent runtime / harness."""

__version__ = "0.1.0"

# Harness version: bump whenever Recovery / Context / Validation / Orchestration
# policy changes (docs/12_EXPERIMENT_PROTOCOL.md). Phase B added the
# Hardening modified Prompt Template / Validation / Recovery Context wiring;
# Phase E5 introduced durable checkpoints and bounded context reconstruction;
# E6-C adds deterministic crash-window-aware resume and generalized continuation;
# E6-D adds post-non-success arbitration for durable workspace FAILED/TIMED_OUT attempts -> HV-1.3;
# CLI Alpha adds tool-aware recovery, explicit command validation, and safe failure memory -> HV-1.4.
HARNESS_VERSION = "HV-1.4"

from .resume import CrashPoint, NoOpCrashInjector, ResumeAction, ResumeDecision, ResumeDecisionService, ResumeInspection

# Default dataset / context-policy labels used by experiment records.
DEFAULT_DATASET_VERSION = "RUNTIME-V0.1"
DEFAULT_CONTEXT_POLICY_VERSION = "CP-0"
DEFAULT_PROVIDER = "mock"
DEFAULT_MODEL = "mock-v0"
