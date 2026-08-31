import difflib, hashlib, os, shutil, tempfile
from pathlib import Path
from .local import LocalReadOnlyWorkspace
from .models import WorkspaceLimits
from .errors import BinaryFileError, WorkspaceEditError, WorkspacePathEscape

class StagingLimitExceeded(Exception): pass
class StagingRootConflict(Exception): pass

class StagedWorkspace(LocalReadOnlyWorkspace):
    """Mutable workspace backed by a private copy; never writes source_root."""
    def __init__(self, source_root, staging_root, limits=None, baseline_root=None):
        self.source_root=Path(source_root).resolve(); self._staging_owner=False
        super().__init__(staging_root, limits)
        self._baseline={}
        baseline_base=Path(baseline_root).resolve() if baseline_root is not None else self.root
        if baseline_root is None or baseline_base == self.root:
            for p in self._iter_files(): self._baseline[p.relative_to(self.root).as_posix()] = p.read_bytes()
        else:
            for p in self._iter_files_under(baseline_base):
                self._baseline[p.relative_to(baseline_base).as_posix()] = p.read_bytes()
    @classmethod
    def create(cls, source_root, staging_root=None, limits=None):
        source=Path(source_root).resolve(); limits=limits or WorkspaceLimits()
        auto_target = staging_root is None
        target=Path(staging_root).resolve() if staging_root else Path(tempfile.mkdtemp(prefix="odys-stage-"))
        if target == source or target in source.parents or source in target.parents:
            raise StagingRootConflict("STAGING_ROOT_CONFLICT")
        if not auto_target and target.exists(): raise StagingRootConflict("STAGING_ROOT_CONFLICT")
        target_parent=target.parent; target_parent.mkdir(parents=True, exist_ok=True)
        build=target if auto_target else Path(tempfile.mkdtemp(prefix=".odys-stage-build-", dir=str(target_parent)))
        count=0; total=0
        excluded=limits.excluded_dirs
        try:
            for item in source.rglob("*"):
                rel=item.relative_to(source)
                if any(part in excluded for part in rel.parts): continue
                if item.is_symlink(): continue
                if item.is_dir(): (build / rel).mkdir(parents=True, exist_ok=True); continue
                if not item.is_file(): continue
                size=item.stat().st_size
                count += 1; total += size
                if count > getattr(limits, "max_files", 10000) or total > getattr(limits, "max_total_bytes", 256*1024*1024) or size > getattr(limits, "max_file_bytes_copy", 4*1024*1024):
                    raise StagingLimitExceeded("STAGING_LIMIT_EXCEEDED")
                (build / rel).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(item, build / rel)
            if not auto_target: os.replace(build, target)
        except Exception:
            shutil.rmtree(build, ignore_errors=True)
            if auto_target: shutil.rmtree(target, ignore_errors=True)
            raise
        return cls(source, target, limits)
    def _iter_files(self):
        for p in self.root.rglob("*"):
            if p.is_file() and self._safe_discovered(p) is not None: yield p
    @staticmethod
    def _iter_files_under(root):
        root=Path(root).resolve()
        for p in root.rglob("*"):
            candidate=p.resolve(strict=False)
            if candidate != root and root not in candidate.parents: continue
            if p.is_file() and not p.is_symlink(): yield p
    @staticmethod
    def _sha(data): return hashlib.sha256(data).hexdigest()
    @staticmethod
    def _line_spans(text):
        """Return line bodies and offsets without normalizing the source text."""
        spans=[]; offset=0
        for raw in text.splitlines(keepends=True):
            body=raw[:-2] if raw.endswith("\r\n") else (raw[:-1] if raw.endswith(("\n","\r")) else raw)
            spans.append((body,offset,offset+len(body),offset+len(raw)))
            offset += len(raw)
        if text and not spans:
            spans.append((text,0,len(text),len(text)))
        return spans
    @classmethod
    def _normalized_candidates(cls, text, target):
        """Resolve only whole-line targets with newline/trailing-space normalization.

        This is intentionally not fuzzy matching: leading and internal whitespace,
        line order, and all non-whitespace characters must remain identical.
        """
        normalized=target.replace("\r\n","\n").replace("\r","\n")
        includes_final_newline=normalized.endswith("\n")
        parts=normalized.split("\n")
        if includes_final_newline: parts.pop()
        if not parts: return []
        expected=[line.rstrip(" \t") for line in parts]
        spans=cls._line_spans(text); width=len(expected); candidates=[]
        for index in range(0,len(spans)-width+1):
            actual=[spans[index+offset][0].rstrip(" \t") for offset in range(width)]
            if actual != expected: continue
            start=spans[index][1]
            last=spans[index+width-1]
            if includes_final_newline and last[3] == last[2]:
                continue
            end=last[3] if includes_final_newline else last[2]
            candidates.append((start,end,index+1,index+width))
        return candidates
    @staticmethod
    def _replacement_for_source(new_text, source_text):
        newline="\r\n" if "\r\n" in source_text else ("\r" if "\r" in source_text and "\n" not in source_text else "\n")
        return new_text.replace("\r\n","\n").replace("\r","\n").replace("\n",newline)
    @staticmethod
    def _atomic_write(file, data):
        tmp=None
        try:
            fd,tmp=tempfile.mkstemp(prefix=f".{file.name}.", dir=str(file.parent))
            with os.fdopen(fd,"wb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp,file); tmp=None
        finally:
            if tmp: Path(tmp).unlink(missing_ok=True)
    async def edit_file(self, path, old_text, new_text, expected_sha256=None, *, allow_safe_normalization=True):
        file=self.resolve_path(path)
        if not file.exists(): raise FileNotFoundError(path)
        if not file.is_file(): raise IsADirectoryError(path)
        if file.is_symlink(): raise WorkspacePathEscape("WORKSPACE_PATH_ESCAPE")
        data=file.read_bytes()
        if b"\x00" in data: raise BinaryFileError("BINARY_FILE")
        before=self._sha(data)
        if expected_sha256 is not None and expected_sha256 != before: raise ValueError("STALE_FILE_VERSION")
        text=data.decode("utf-8")
        if not old_text: raise WorkspaceEditError("INVALID_ARGUMENTS")
        occurrences=text.count(old_text)
        match_mode="EXACT"; candidate_count=occurrences; start_line=end_line=None
        if occurrences > 1:
            raise WorkspaceEditError("EDIT_TARGET_AMBIGUOUS", candidate_count=min(occurrences,100), candidates_truncated=occurrences>100, match_mode="EXACT")
        if occurrences == 1:
            start=text.index(old_text); end=start+len(old_text)
            start_line=text.count("\n",0,start)+1
            end_line=start_line+max(0,len(old_text.splitlines())-1)
            replacement=new_text
        else:
            candidates=self._normalized_candidates(text,old_text) if allow_safe_normalization else []
            candidate_count=len(candidates); match_mode="NORMALIZED_UNIQUE"
            if candidate_count == 0:
                raise WorkspaceEditError("EDIT_TARGET_NOT_FOUND", candidate_count=0, normalization_attempted=allow_safe_normalization)
            if candidate_count > 1:
                raise WorkspaceEditError("EDIT_TARGET_AMBIGUOUS", candidate_count=min(candidate_count,100), candidates_truncated=candidate_count>100, match_mode="NORMALIZED")
            start,end,start_line,end_line=candidates[0]
            replacement=self._replacement_for_source(new_text,text)
        updated_text=text[:start]+replacement+text[end:]
        if updated_text == text:
            raise WorkspaceEditError("NO_CHANGE", candidate_count=1, match_mode=match_mode)
        updated=updated_text.encode("utf-8")
        self._atomic_write(file, updated)
        after=self._sha(updated)
        return {"path":Path(path).as_posix(),"replacements":1,"before_sha256":before,"after_sha256":after,"bytes_before":len(data),"bytes_after":len(updated),"match_mode":match_mode,"candidate_count":candidate_count,"matched_start_line":start_line,"matched_end_line":end_line}
    async def edit_lines(self, path, start_line, end_line, new_lines, expected_sha256):
        file=self.resolve_path(path)
        if not file.exists(): raise FileNotFoundError(path)
        if not file.is_file(): raise IsADirectoryError(path)
        if file.is_symlink(): raise WorkspacePathEscape("WORKSPACE_PATH_ESCAPE")
        data=file.read_bytes()
        if b"\x00" in data: raise BinaryFileError("BINARY_FILE")
        before=self._sha(data)
        if not isinstance(expected_sha256,str) or not expected_sha256: raise ValueError("INVALID_ARGUMENTS")
        if expected_sha256 != before: raise ValueError("STALE_FILE_VERSION")
        if not isinstance(start_line,int) or isinstance(start_line,bool) or not isinstance(end_line,int) or isinstance(end_line,bool): raise WorkspaceEditError("INVALID_EDIT_RANGE")
        if not isinstance(new_lines,list) or not all(isinstance(line,str) and "\n" not in line and "\r" not in line for line in new_lines): raise ValueError("INVALID_ARGUMENTS")
        text=data.decode("utf-8"); lines=text.splitlines(keepends=True)
        if start_line < 1 or end_line < start_line or end_line > len(lines): raise WorkspaceEditError("INVALID_EDIT_RANGE", start_line=start_line, end_line=end_line, total_lines=len(lines))
        newline="\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
        selected_reaches_eof=end_line == len(lines); had_final_newline=text.endswith(("\n","\r")); replacement=[]
        for index,line in enumerate(new_lines):
            needs_newline=(not selected_reaches_eof) or index < len(new_lines)-1 or had_final_newline
            replacement.append(line + (newline if needs_newline else ""))
        updated=("".join(lines[:start_line-1]) + "".join(replacement) + "".join(lines[end_line:])).encode("utf-8")
        if updated == data: raise WorkspaceEditError("NO_CHANGE", start_line=start_line, end_line=end_line)
        self._atomic_write(file, updated)
        after=self._sha(updated)
        return {"path":Path(path).as_posix(),"start_line":start_line,"end_line":end_line,"lines_written":len(new_lines),"before_sha256":before,"after_sha256":after,"bytes_before":len(data),"bytes_after":len(updated)}
    async def restore_file(self, path):
        rel=self.resolve_path(path).relative_to(self.root).as_posix()
        if rel not in self._baseline: raise FileNotFoundError(path)
        file=self.resolve_path(path); data=self._baseline[rel]
        self._atomic_write(file,data)
        return {"path":Path(path).as_posix(),"restored":True,"sha256":self._sha(data)}
    async def diff(self, path=None, max_diff_bytes=None):
        requested=self.limits.max_diff_bytes if max_diff_bytes is None else int(max_diff_bytes)
        if requested <= 0: raise ValueError("INVALID_ARGUMENTS")
        max_diff_bytes=min(requested, self.limits.max_diff_bytes)
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
