import difflib, hashlib, os, shutil, tempfile
from pathlib import Path
from .local import LocalReadOnlyWorkspace
from .models import WorkspaceLimits
from .errors import BinaryFileError, WorkspacePathEscape

class StagingLimitExceeded(Exception): pass

class StagedWorkspace(LocalReadOnlyWorkspace):
    """Mutable workspace backed by a private copy; never writes source_root."""
    def __init__(self, source_root, staging_root, limits=None):
        self.source_root=Path(source_root).resolve(); self._staging_owner=False
        super().__init__(staging_root, limits)
        self._baseline={}
        for p in self._iter_files(): self._baseline[p.relative_to(self.root).as_posix()] = p.read_bytes()
    @classmethod
    def create(cls, source_root, staging_root=None, limits=None):
        source=Path(source_root).resolve(); limits=limits or WorkspaceLimits()
        target=Path(staging_root).resolve() if staging_root else Path(tempfile.mkdtemp(prefix="odys-stage-"))
        target.mkdir(parents=True, exist_ok=True)
        count=0; total=0
        excluded=limits.excluded_dirs
        for item in source.rglob("*"):
            rel=item.relative_to(source)
            if any(part in excluded for part in rel.parts): continue
            if item.is_symlink(): continue
            if item.is_dir(): (target / rel).mkdir(parents=True, exist_ok=True); continue
            if not item.is_file(): continue
            size=item.stat().st_size
            count += 1; total += size
            if count > getattr(limits, "max_files", 10000) or total > getattr(limits, "max_total_bytes", 256*1024*1024) or size > getattr(limits, "max_file_bytes_copy", 4*1024*1024):
                raise StagingLimitExceeded("STAGING_LIMIT_EXCEEDED")
            (target / rel).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(item, target / rel)
        return cls(source, target, limits)
    def _iter_files(self):
        for p in self.root.rglob("*"):
            if p.is_file() and self._safe_discovered(p) is not None: yield p
    @staticmethod
    def _sha(data): return hashlib.sha256(data).hexdigest()
    async def edit_file(self, path, old_text, new_text, expected_sha256=None):
        file=self.resolve_path(path)
        if not file.exists(): raise FileNotFoundError(path)
        if not file.is_file(): raise IsADirectoryError(path)
        if file.is_symlink(): raise WorkspacePathEscape("WORKSPACE_PATH_ESCAPE")
        data=file.read_bytes()
        if b"\x00" in data: raise BinaryFileError("BINARY_FILE")
        before=self._sha(data)
        if expected_sha256 is not None and expected_sha256 != before: raise ValueError("STALE_FILE_VERSION")
        text=data.decode("utf-8")
        if not old_text: raise ValueError("INVALID_ARGUMENTS")
        occurrences=text.count(old_text)
        if occurrences == 0: raise ValueError("EDIT_TARGET_NOT_FOUND")
        if occurrences > 1: raise ValueError("EDIT_TARGET_AMBIGUOUS")
        updated=text.replace(old_text, new_text, 1).encode("utf-8"); tmp=None
        try:
            fd,tmp=tempfile.mkstemp(prefix=f".{file.name}.", dir=str(file.parent))
            with os.fdopen(fd, "wb") as handle: handle.write(updated); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, file); tmp=None
        finally:
            if tmp: Path(tmp).unlink(missing_ok=True)
        after=self._sha(updated)
        return {"path":Path(path).as_posix(),"replacements":1,"before_sha256":before,"after_sha256":after,"bytes_before":len(data),"bytes_after":len(updated)}
    async def restore_file(self, path):
        rel=self.resolve_path(path).relative_to(self.root).as_posix()
        if rel not in self._baseline: raise FileNotFoundError(path)
        file=self.resolve_path(path); data=self._baseline[rel]; tmp=None
        try:
            fd,tmp=tempfile.mkstemp(prefix=f".{file.name}.", dir=str(file.parent))
            with os.fdopen(fd,"wb") as handle: handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp,file); tmp=None
        finally:
            if tmp: Path(tmp).unlink(missing_ok=True)
        return {"path":Path(path).as_posix(),"restored":True,"sha256":self._sha(data)}
    async def diff(self, path=None, max_diff_bytes=128*1024):
        paths=[path] if path else sorted(self._baseline)
        chunks=[]; changed=[]; added=removed=0; truncated=False
        for rel in paths:
            if rel not in self._baseline: continue
            current=self.resolve_path(rel).read_bytes()
            baseline=self._baseline[rel]
            if current == baseline: continue
            changed.append(rel)
            old=baseline.decode("utf-8", "replace").splitlines(True); new=current.decode("utf-8", "replace").splitlines(True)
            piece="".join(difflib.unified_diff(old,new,fromfile=f"a/{rel}",tofile=f"b/{rel}"))
            added += sum(1 for x in difflib.ndiff(old,new) if x.startswith("+ ")); removed += sum(1 for x in difflib.ndiff(old,new) if x.startswith("- "))
            chunks.append(piece)
        text="".join(chunks); raw=text.encode("utf-8")
        if len(raw)>max_diff_bytes: text=raw[:max_diff_bytes].decode("utf-8","replace"); truncated=True
        return {"changed_files":changed,"diff":text,"files_changed":len(changed),"lines_added":added,"lines_removed":removed,"truncated":truncated}
