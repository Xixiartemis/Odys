class WorkspaceError(Exception): pass
class WorkspacePathEscape(WorkspaceError): pass
class BinaryFileError(WorkspaceError): pass
