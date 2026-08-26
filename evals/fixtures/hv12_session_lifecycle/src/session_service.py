from .models import Session
from .session_store import SessionStore


class SessionService:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def receive(self, tenant_id: str, session_id: str, message: str) -> Session:
        session = self.store.get_or_create(tenant_id, session_id)
        session.messages.append(message)
        return session

    def delete_session(self, tenant_id: str, session_id: str) -> None:
        self.store.delete(tenant_id, session_id)

    def create_session(self, tenant_id: str, session_id: str, message: str) -> Session:
        """Explicit creation path used only for genuinely new sessions."""
        return self.receive(tenant_id, session_id, message)
