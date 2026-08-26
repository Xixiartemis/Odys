from src.session_service import SessionService
from src.session_store import SessionStore


def test_same_session_id_is_isolated_per_tenant():
    service = SessionService(SessionStore())

    tenant_a = service.receive("tenant-a", "shared", "a-message")
    tenant_b = service.receive("tenant-b", "shared", "b-message")

    assert tenant_a is not tenant_b
    assert tenant_a.messages == ["a-message"]
    assert tenant_b.messages == ["b-message"]
