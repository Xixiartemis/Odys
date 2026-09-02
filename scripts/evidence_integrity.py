"""Integrity helpers for frozen evidence created on the historical Windows host."""

from __future__ import annotations

import hashlib
from pathlib import Path


def historical_canonical_sha256(path: Path) -> str:
    """Hash evidence using the CRLF byte convention used when its digest was frozen."""
    data = path.read_bytes()
    normalized_lf = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized_lf.replace(b"\n", b"\r\n")).hexdigest()
