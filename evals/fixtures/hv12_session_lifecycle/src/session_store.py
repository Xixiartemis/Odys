from .models import Session, SessionDeleted


class SessionStore:
    """Small in-memory store with durable-lifecycle semantics at its boundary."""

    def __init__(self) -> None:
        self._cache: dict[str, Session] = {}
        self._deleted: set[tuple[str, str]] = set()

    def get_or_create(self, tenant_id: str, session_id: str) -> Session:
        key = (tenant_id, session_id)
        if key in self._deleted:
            raise SessionDeleted(f"session {session_id!r} is deleted")
        # BUG: the cache ignores tenant_id, so equal session ids leak state.
        session = self._cache.get(session_id)
        if session is None:
            session = Session(tenant_id=tenant_id, session_id=session_id)
            self._cache[session_id] = session
        return session

    def delete(self, tenant_id: str, session_id: str) -> None:
        self._deleted.add((tenant_id, session_id))
        self._cache.pop(session_id, None)

    def has_session(self, tenant_id: str, session_id: str) -> bool:
        return (tenant_id, session_id) not in self._deleted and session_id in self._cache
