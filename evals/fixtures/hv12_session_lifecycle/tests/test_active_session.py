from src.message_router import route_delayed_message
from src.session_service import SessionService
from src.session_store import SessionStore


def test_new_and_active_session_behavior_remains_available():
    service = SessionService(SessionStore())

    first = route_delayed_message(service, "tenant-a", "new", "first")
    second = route_delayed_message(service, "tenant-a", "new", "second")

    assert first is second
    assert second.messages == ["first", "second"]
