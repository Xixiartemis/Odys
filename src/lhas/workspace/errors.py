class WorkspaceError(Exception): pass
class WorkspacePathEscape(WorkspaceError): pass
class BinaryFileError(WorkspaceError): pass


class WorkspaceEditError(ValueError):
    """Structured, safe edit failure surfaced through workspace tools."""

    def __init__(self, code: str, **diagnostics):
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics


class WorkspaceSessionError(WorkspaceError):
    """Base error for durable workspace session validation failures."""


class WorkspaceSessionCorrupt(WorkspaceSessionError):
    pass


class WorkspaceBaselineCorrupt(WorkspaceSessionError):
    pass


class WorkspaceSourceDrift(WorkspaceSessionError):
    pass


class WorkspaceSessionBindingMismatch(WorkspaceSessionError):
    pass
