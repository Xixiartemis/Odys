from .models import Session, SessionDeleted
from .session_service import SessionService


def route_delayed_message(
    service: SessionService,
    tenant_id: str,
    session_id: str,
    message: str,
) -> Session | None:
    try:
        return service.receive(tenant_id, session_id, message)
    except SessionDeleted:
        # BUG: delayed delivery silently recreates a tombstoned session.
        return service.create_session(tenant_id, session_id, message)
