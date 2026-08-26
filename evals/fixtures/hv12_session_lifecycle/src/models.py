from dataclasses import dataclass, field


class SessionDeleted(Exception):
    """Raised when a message targets a tombstoned session."""


@dataclass
class Session:
    tenant_id: str
    session_id: str
    messages: list[str] = field(default_factory=list)
