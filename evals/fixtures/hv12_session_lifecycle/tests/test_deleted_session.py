from src.message_router import route_delayed_message
from src.session_service import SessionService
from src.session_store import SessionStore


def test_delayed_message_does_not_recreate_deleted_session():
    store = SessionStore()
    service = SessionService(store)
    service.receive("tenant-a", "session-1", "before-delete")
    service.delete_session("tenant-a", "session-1")

    delivered = route_delayed_message(service, "tenant-a", "session-1", "late-message")

    assert delivered is None
    assert not store.has_session("tenant-a", "session-1")
