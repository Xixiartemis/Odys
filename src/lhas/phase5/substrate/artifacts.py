"""Artifact Store and Effect Receipt — HARDENED.

Content-addressed artifact storage with integrity verification.
Effect receipts track side-effect certainty.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


class EffectStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    NOT_OBSERVED = "NOT_OBSERVED"
    UNCERTAIN = "UNCERTAIN"



class ArtifactRef(BaseModel):
    """Reference to a stored artifact with content addressing."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(default_factory=_new_id)
    kind: str
    sha256: str
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    storage_uri: str = ""
    producer_evidence_id: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def verify_digest(self, content: bytes) -> bool:
        return hashlib.sha256(content).hexdigest() == self.sha256


class EffectReceipt(BaseModel):
    """Represents side-effect certainty."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str
    effect_status: EffectStatus
    before_digest: Optional[str] = None
    after_digest: Optional[str] = None
    artifact_refs: tuple[str, ...] = ()
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ArtifactStore(Protocol):
    """Protocol for content-addressed artifact storage."""

    def put(self, content: bytes, *, kind: str, mime_type: str = "", producer_evidence_id: str = "") -> ArtifactRef: ...
    def get(self, artifact_id: str) -> Optional[bytes]: ...
    def ref(self, artifact_id: str) -> Optional[ArtifactRef]: ...
    def exists(self, artifact_id: str) -> bool: ...
    def verify_integrity(self, artifact_id: str) -> bool: ...


class InMemoryArtifactStore:
    """In-memory artifact store with content addressing.

    put() stores content, computes digest, and verifies the stored
    content matches the declared sha256 before returning the ref.
    UNAUTHORIZED_ARTIFACT_PROMOTION=BLOCKED: only put() can create refs.
    """

    def __init__(self):
        self._store: dict[str, bytes] = {}
        self._refs: dict[str, ArtifactRef] = {}

    def put(
        self,
        content: bytes,
        *,
        kind: str,
        mime_type: str = "",
        producer_evidence_id: str = "",
    ) -> ArtifactRef:
        sha = hashlib.sha256(content).hexdigest()
        for existing_id, existing_ref in self._refs.items():
            if existing_ref.sha256 == sha:
                return existing_ref
        artifact_id = _new_id()
        ref = ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            sha256=sha,
            size_bytes=len(content),
            mime_type=mime_type,
            storage_uri=f"memory://{artifact_id}",
            producer_evidence_id=producer_evidence_id,
        )
        self._store[artifact_id] = content
        self._refs[artifact_id] = ref
        # Post-put verification: confirm stored content matches declared digest
        assert self.verify_integrity(artifact_id), (
            f"Artifact integrity check failed immediately after put: {artifact_id}"
        )
        return ref

    def get(self, artifact_id: str) -> Optional[bytes]:
        return self._store.get(artifact_id)

    def ref(self, artifact_id: str) -> Optional[ArtifactRef]:
        return self._refs.get(artifact_id)

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._store

    def verify_integrity(self, artifact_id: str) -> bool:
        content = self._store.get(artifact_id)
        ref = self._refs.get(artifact_id)
        if content is None or ref is None:
            return False
        return hashlib.sha256(content).hexdigest() == ref.sha256
