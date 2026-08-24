import asyncio, os, re, time
from .errors import WorkspacePathEscape
_META = re.compile(r"^(?:&&|\|\||[;|><`])$")
_SECRET = re.compile(r"(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE)", re.I)
class SafeCli:
    def __init__(self, workspace, policy, default_timeout=30, max_timeout=120, max_output_bytes=64*1024): self.workspace=workspace; self.policy=policy; self.default_timeout=default_timeout; self.max_timeout=max_timeout; self.max_output_bytes=max_output_bytes
    def _env(self): return {k:v for k,v in os.environ.items() if not _SECRET.search(k) and k in {"PATH","SYSTEMROOT","WINDIR","TEMP","TMP","HOME","USERPROFILE","LANG","LC_ALL","VIRTUAL_ENV"}}
    async def execute(self, argv, cwd=".", timeout_seconds=None):
        if not isinstance(argv, list) or not argv or not all(isinstance(x,str) for x in argv) or any(_META.match(x) for x in argv): return None, "INVALID_ARGUMENTS"
        if not self.policy.allows(argv): return None, "COMMAND_NOT_ALLOWED"
        timeout=float(timeout_seconds if timeout_seconds is not None else self.default_timeout)
        if timeout <= 0 or timeout > self.max_timeout: return None, "INVALID_TIMEOUT"
        directory=self.workspace.resolve_path(cwd); start=time.monotonic()
        try:
            proc=await asyncio.create_subprocess_exec(*argv, cwd=str(directory), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env())
            try: out,err=await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill(); await proc.communicate(); return None, "COMMAND_TIMEOUT"
        except WorkspacePathEscape: raise
        except OSError as exc: return None, f"SPAWN_ERROR: {exc}"
        limit=self.max_output_bytes; out_tr=len(out)>limit; err_tr=len(err)>limit
        return {"exit_code":proc.returncode,"stdout":out[:limit].decode("utf-8", "replace"),"stderr":err[:limit].decode("utf-8", "replace"),"timed_out":False,"duration_ms":int((time.monotonic()-start)*1000),"stdout_truncated":out_tr,"stderr_truncated":err_tr}, None
