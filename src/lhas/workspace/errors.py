class WorkspaceError(Exception): pass
class WorkspacePathEscape(WorkspaceError): pass
class BinaryFileError(WorkspaceError): pass


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
