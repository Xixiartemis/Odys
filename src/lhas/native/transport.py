"""Secret-free identity for the HTTP transport a provider actually uses."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import posixpath
from urllib.parse import urlparse


OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class TransportEndpointIdentity:
    endpoint_identity: str
    endpoint_host: str
    endpoint_fingerprint: str


def canonical_transport_identity(
    base_url: object | None,
    *,
    default_url: str = OPENAI_DEFAULT_BASE_URL,
) -> TransportEndpointIdentity:
    """Derive a non-secret identity from an effective HTTP client base URL."""

    raw = str(base_url or default_url).strip()
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError("TRANSPORT_ENDPOINT_INVALID")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("TRANSPORT_ENDPOINT_INVALID") from exc
    path = parsed.path or "/"
    path = posixpath.normpath("/" + path.lstrip("/"))
    if path != "/":
        path = path.rstrip("/")
    display_host = f"[{host}]" if ":" in host else host
    identity = f"{scheme}://{display_host}:{port}{path}"
    return TransportEndpointIdentity(
        endpoint_identity=identity,
        endpoint_host=host,
        endpoint_fingerprint=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    )


def opaque_transport_identity(identity: str) -> TransportEndpointIdentity:
    """Provide the same evidence shape for deterministic non-HTTP adapters."""

    safe = str(identity).split("@", 1)[-1][:256]
    return TransportEndpointIdentity(
        endpoint_identity=safe,
        endpoint_host=safe,
        endpoint_fingerprint=hashlib.sha256(safe.encode("utf-8")).hexdigest(),
    )
