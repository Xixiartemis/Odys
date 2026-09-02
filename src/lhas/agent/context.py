"""Composable, budgeted context assembly above the CP-0..CP-3 boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class ContextPriority(IntEnum):
    REQUIRED = 100
    HIGH = 80
    NORMAL = 50
    LOW = 20


@dataclass(frozen=True)
class ContextSource:
    name: str
    value: Any
    priority: ContextPriority = ContextPriority.NORMAL
    max_chars: int = 8_000


@dataclass(frozen=True)
class AssembledContext:
    sections: dict[str, Any]
    chars_used: int
    budget_chars: int
    truncated_sections: tuple[str, ...]


class ContextAssembler:
    """Selects only caller-approved sources and applies a deterministic budget."""

    def assemble(self, sources: list[ContextSource], *, budget_chars: int) -> AssembledContext:
        if budget_chars < 1:
            raise ValueError("context budget must be positive")
        sections: dict[str, Any] = {}
        truncated: list[str] = []
        used = 0
        for source in sorted(sources, key=lambda item: (-int(item.priority), item.name)):
            if source.name in sections:
                raise ValueError(f"duplicate context section: {source.name}")
            encoded = json.dumps(source.value, ensure_ascii=False, sort_keys=True, default=str)
            allowance = min(source.max_chars, budget_chars - used)
            if allowance <= 0:
                truncated.append(source.name)
                continue
            if len(encoded) <= allowance:
                sections[source.name] = source.value
                used += len(encoded)
            else:
                sections[source.name] = {"summary": encoded[: max(0, allowance - 40)], "truncated": True}
                used += allowance
                truncated.append(source.name)
        return AssembledContext(sections, used, budget_chars, tuple(truncated))
