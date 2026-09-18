"""FixtureRegistry mapping task_id -> fixture class."""
from typing import Type
from .base import BaseFixture
from .ci import CI_FIXTURES
from .esr import ESR_FIXTURES
from .cwr import CWR_FIXTURES
from .ptf import PTF_FIXTURES
from .rtp import RTP_FIXTURES
from .dl import DL_FIXTURES


class FixtureRegistry:
    """Maps task_id strings to concrete BaseFixture subclasses."""

    def __init__(self) -> None:
        self._registry: dict[str, Type[BaseFixture]] = {}
        for fixture_cls in (
            CI_FIXTURES + ESR_FIXTURES + CWR_FIXTURES
            + PTF_FIXTURES + RTP_FIXTURES + DL_FIXTURES
        ):
            self._registry[fixture_cls.task_id] = fixture_cls

    def get(self, task_id: str) -> BaseFixture:
        """Return a fresh instance of the fixture for *task_id*."""
        cls = self._registry.get(task_id)
        if cls is None:
            raise KeyError(f"No fixture registered for task_id={task_id!r}")
        return cls()

    def all_task_ids(self) -> list[str]:
        """Return sorted list of all registered task IDs."""
        return sorted(self._registry.keys())

    def __len__(self) -> int:
        return len(self._registry)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._registry
