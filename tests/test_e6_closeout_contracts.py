"""Small E6 closeout contracts for event compatibility and checkpoint identity."""

from lhas.checkpoint import WorkingStateProjector
from lhas.domain.enums import EventType
from lhas.domain.models import Event


def test_recovery_action_projection_accepts_canonical_and_historical_fields():
    projector = WorkingStateProjector()
    canonical = Event(
        id=1,
        event_type=EventType.RECOVERY_DECIDED,
        payload={"action": "RETRY_WITH_FAILURE_CONTEXT"},
    )
    historical = Event(
        id=2,
        event_type=EventType.RECOVERY_DECIDED,
        payload={"action_type": "RETRY_WITH_EXPANDED_CONTEXT"},
    )
    state = projector.project(None, [canonical])
    assert state.last_recovery_action == "RETRY_WITH_FAILURE_CONTEXT"
    state = projector.project(state, [historical])
    assert state.last_recovery_action == "RETRY_WITH_EXPANDED_CONTEXT"
