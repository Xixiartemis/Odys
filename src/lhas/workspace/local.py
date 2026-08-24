import fnmatch
from pathlib import Path, PureWindowsPath
from .errors import WorkspacePathEscape, BinaryFileError
from .models import WorkspaceLimits

class LocalReadOnlyWorkspace:
    def __init__(self, root: str | Path, limits: WorkspaceLimits | None = None):
        self._root = Path(root).resolve(); self.limits = limits or WorkspaceLimits()
    @property
    def root(self): return self._root
    def resolve_path(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute() or PureWindowsPath(relative_path).is_absolute() or PureWindowsPath(relative_path).drive or relative_path.startswith(("\\\\", "/")):
            raise WorkspacePathEscape("WORKSPACE_PATH_ESCAPE")
        candidate = (self._root / relative_path).resolve(strict=False)
        if candidate != self._root and self._root not in candidate.parents:
            raise WorkspacePathEscape("WORKSPACE_PATH_ESCAPE")
        return candidate
    def _safe_discovered(self, item: Path) -> Path | None:
        candidate = item.resolve(strict=False)
        if candidate != self._root and self._root not in candidate.parents:
            return None
        return candidate
    async def list_files(self, path=".", recursive=False, max_entries=200):
        directory = self.resolve_path(path)
        if not directory.is_dir(): raise FileNotFoundError(path)
        entries=[]; iterator = directory.rglob("*") if recursive else directory.iterdir()
        for item in sorted(iterator, key=lambda p: str(p).lower()):
            if any(part in self.limits.excluded_dirs for part in item.relative_to(self._root).parts): continue
            safe_item = self._safe_discovered(item)
            if safe_item is None: continue
            if len(entries) >= max(0, min(int(max_entries), 10000)): break
            rel=item.relative_to(self._root).as_posix(); entries.append({"path": rel, "type": "directory" if safe_item.is_dir() else "file", "size": safe_item.stat().st_size if safe_item.is_file() else 0, "relative_path": rel})
        return {"path": directory.relative_to(self._root).as_posix() or ".", "entries": entries, "truncated": len(entries) >= max(0, min(int(max_entries), 10000))}
    async def read_file(self, path, start_line=None, end_line=None):
        file = self.resolve_path(path)
        if not file.exists(): raise FileNotFoundError(path)
        if not file.is_file(): raise IsADirectoryError(path)
        limit=min(self.limits.max_read_bytes, self.limits.hard_max_read_bytes)
        if file.stat().st_size > limit: return {"path": Path(path).as_posix(), "content": "", "truncated": True, "total_lines": 0}
        data=file.read_bytes()
        if b"\x00" in data: raise BinaryFileError("BINARY_FILE")
        text=data.decode("utf-8")
        lines=text.splitlines(); begin=max(1, start_line or 1); finish=min(len(lines), end_line or len(lines)); content="\n".join(lines[begin-1:finish])
        return {"path": Path(path).as_posix(), "content": content, "start_line": begin, "end_line": finish, "total_lines": len(lines), "truncated": finish < len(lines)}
    async def search_text(self, query, path=".", glob=None, max_matches=100, context_lines=1):
        if not isinstance(query, str) or not query: raise ValueError("INVALID_ARGUMENTS")
        base=self.resolve_path(path); matches=[]; scanned=0; truncated=False
        requested_matches=max(0, int(max_matches)); effective_matches=min(requested_matches, self.limits.max_search_matches)
        requested_context=max(0, int(context_lines)); effective_context=min(requested_context, self.limits.max_context_lines)
        if requested_matches > effective_matches or requested_context > effective_context: truncated=True
        for file in sorted((p for p in base.rglob("*") if p.is_file()), key=lambda p: str(p).lower()):
            if any(part in self.limits.excluded_dirs for part in file.relative_to(self._root).parts): continue
            safe_file=self._safe_discovered(file)
            if safe_file is None: continue
            if glob and not fnmatch.fnmatch(file.name, glob): continue
            if scanned >= self.limits.max_search_files: truncated=True; break
            scanned += 1
            if safe_file.stat().st_size > self.limits.max_file_bytes: truncated=True; continue
            data=safe_file.read_bytes()
            if b"\x00" in data: continue
            try: lines=data.decode("utf-8").splitlines()
            except UnicodeDecodeError: continue
            for index,line in enumerate(lines):
                if query in line:
                    before=lines[max(0,index-effective_context):index]; after=lines[index+1:index+1+effective_context]
                    matches.append({"path": file.relative_to(self._root).as_posix(), "line": index+1, "text": line, "before": before, "after": after})
                    if len(matches) >= effective_matches: truncated=True; break
            if len(matches) >= effective_matches: break
        return {"matches": matches, "truncated": truncated}
