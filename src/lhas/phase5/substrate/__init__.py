"""Phase5 Research Substrate — minimum state/evidence/evaluation types.

NOT a general agent framework.  Provides only the minimum substrate
needed to test Odys Recovery Control Policy rigorously.

Source-of-truth hierarchy:
  1. Environment observation
  2. Append-only execution evidence
  3. Validator feedback
  4. Verified state commit
  5. Materialized verified task state
"""

from .state import (
    ControlState,
    StateCommit,
    VerifiedFact,
    VerifiedTaskState,
)
from .evidence import (
    EvidenceEvent,
    EvidenceEventType,
    EvidenceLedger,
    EFFECT_EVENT_MAP,
)
from .artifacts import (
    ArtifactRef,
    ArtifactStore,
    InMemoryArtifactStore,
    EffectReceipt,
    EffectStatus,
)
from .validation import (
    ValidatorFeedback,
    ValidatorDecision,
    ValidatorExecutionStatus,
)
from .reducer import TaskStateReducer, CommitRejected
from .store import (
    BenchmarkOutcome,
    RuntimeValidatorProtocol,
    TestValidatorHelper,
    RuntimeValidator,
    OfflineGrader,
)

__all__ = [
    "VerifiedTaskState",
    "VerifiedFact",
    "ControlState",
    "StateCommit",
    "EvidenceEvent",
    "EvidenceEventType",
    "EvidenceLedger",
    "EFFECT_EVENT_MAP",
    "ArtifactRef",
    "ArtifactStore",
    "InMemoryArtifactStore",
    "EffectReceipt",
    "EffectStatus",
    "ValidatorFeedback",
    "ValidatorDecision",
    "ValidatorExecutionStatus",
    "TaskStateReducer",
    "CommitRejected",
    "BenchmarkOutcome",
    "RuntimeValidatorProtocol",
    "TestValidatorHelper",
    "RuntimeValidator",
    "OfflineGrader",
]
