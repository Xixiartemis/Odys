import asyncio, json
from pathlib import Path
from lhas.live_tools import ResumeReaderTool, WebFetchTool, WebSearchTool, SearchProvider, TavilySearchProvider
import lhas.live_tools as live_tools
import urllib.error
import socket
import pytest
from lhas.tools.protocol import ToolRequest, ToolResult, ToolResultStatus
from lhas.job.live_pipeline import deduplicate_jobs, expiration_status, shortlist_record
from lhas import HARNESS_VERSION
from lhas.planning.models import Goal
from lhas.planning.planner import DeterministicPlanner
from lhas.planning.service import PlanExecutionService
from lhas.tools.registry import ToolRegistry
from lhas.tools.fakes import FakeTool
from lhas.tools.protocol import ToolResult
from lhas.domain.models import Project
from lhas.persistence.repositories import ProjectRepository

def req(cap,args): return ToolRequest(tool_call_id="c",task_id="t",run_id="r",attempt_id="a",capability=cap,arguments=args)

def test_resume_reader_txt(tmp_path):
    p=tmp_path/"resume.md"; p.write_text("Alice\nAI Engineer",encoding="utf-8")
    result=asyncio.run(ResumeReaderTool().execute(req("document.resume.read",{"path":str(p)})))
    assert result.status == ToolResultStatus.SUCCESS and result.output["text"].startswith("Alice")

class Provider(SearchProvider):
    async def search(self,q,n): return [{"title":"x","url":"https://example.com/x","snippet":"s","source":"test"}]

def test_search_adapter_parsing():
    result=asyncio.run(WebSearchTool(Provider()).execute(req("web.search",{"query":"ai","max_results":5})))
    assert result.output["results"][0]["url"].startswith("https://")

def test_search_requires_provider_config(monkeypatch):
    monkeypatch.delenv("LHAS_SEARCH_ENDPOINT",raising=False); monkeypatch.delenv("LHAS_SEARCH_API_KEY",raising=False)
    result=asyncio.run(WebSearchTool().execute(req("web.search",{"query":"x"})))
    assert result.status == ToolResultStatus.FAILURE and "CONFIG" in result.error_type

def test_tavily_post_headers_body(monkeypatch):
    captured={}
    class Resp:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self): return b'{"results":[{"title":"T","url":"https://e","content":"evidence","score":0.9}]}'
    def fake(req,timeout):
        captured.update(method=req.method,headers=dict(req.header_items()),body=json.loads(req.data.decode()))
        return Resp()
    monkeypatch.setenv("LHAS_SEARCH_API_KEY","secret-value"); monkeypatch.setattr(live_tools.urllib.request,"urlopen",fake)
    result=asyncio.run(WebSearchTool(TavilySearchProvider()).execute(req("web.search",{"query":"q","max_results":3})))
    assert captured["method"] == "POST" and captured["body"] == {"query":"q","max_results":3}
    assert captured["headers"]["Authorization"] == "Bearer secret-value" and captured["headers"]["Content-type"] == "application/json"
    assert result.output["results"][0]["snippet"] == "evidence" and result.output["results"][0]["score"] == 0.9

def test_tavily_error_classification(monkeypatch):
    class E:
        def __init__(self,code): self.code=code
    for code,expected in ((401,"AUTH_ERROR"),(429,"RATE_LIMIT"),(500,"UPSTREAM_5XX")):
        def fake(req,timeout,code=code): raise urllib.error.HTTPError("https://e",code,"x",{},None)
        monkeypatch.setenv("LHAS_SEARCH_API_KEY","x"); monkeypatch.setattr(live_tools.urllib.request,"urlopen",fake)
        result=asyncio.run(WebSearchTool(TavilySearchProvider()).execute(req("web.search",{"query":"q"})))
        assert result.error_type == expected

def test_tavily_invalid_json(monkeypatch):
    class Resp:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self): return b"not-json"
    monkeypatch.setenv("LHAS_SEARCH_API_KEY","x"); monkeypatch.setattr(live_tools.urllib.request,"urlopen",lambda *a,**k: Resp())
    result=asyncio.run(WebSearchTool(TavilySearchProvider()).execute(req("web.search",{"query":"q"})))
    assert result.error_type == "INVALID_RESPONSE"

def test_tavily_timeout(monkeypatch):
    monkeypatch.setenv("LHAS_SEARCH_API_KEY","x")
    monkeypatch.setattr(live_tools.urllib.request,"urlopen",lambda *a,**k: (_ for _ in ()).throw(socket.timeout()))
    result=asyncio.run(WebSearchTool(TavilySearchProvider()).execute(req("web.search",{"query":"q"})))
    assert result.status == ToolResultStatus.FAILURE and result.error_type == "TIMEOUT"

def test_fetch_ssrf_guard():
    result=asyncio.run(WebFetchTool().execute(req("web.fetch",{"url":"http://127.0.0.1/"})))
    assert result.status == ToolResultStatus.FAILURE and result.error_type == "SSRF_BLOCKED"

def test_dedup_expiration_and_evidence():
    jobs=[{"company":"A","title":"X","source_url":"https://e.com/a/"},{"company":"A","title":"X","source_url":"https://e.com/a"}]
    rows,count=deduplicate_jobs(jobs); assert len(rows)==1 and count==1
    assert expiration_status({"status":"closed"}) == "expired"
    assert shortlist_record(rows[0])["evidence"][0]["source_url"]

def test_d2_harness_version():
    assert HARNESS_VERSION == "HV-1.5"

def test_semantic_pipeline_e2e(db,tmp_path,monkeypatch):
    resume=tmp_path/"resume.txt"; resume.write_text("React TypeScript Python Agent",encoding="utf-8")
    class P(SearchProvider):
        async def search(self,q,n): return [{"title":"React Agent","url":"https://jobs.example/a","snippet":"React Python","source":"tavily","score":.9},{"title":"TypeScript Agent","url":"https://jobs.example/b","snippet":"TypeScript Agent","source":"tavily","score":.8}]
    class Resp:
        def __init__(self,url): self.url=url; self.status=200; self.headers={"Content-Type":"text/html"}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,n=-1): return ("<html><title>"+self.url+" Role</title><body>React TypeScript Python Agent engineer</body></html>").encode()
    class Opener:
        def open(self,req,timeout=15): return Resp(req.full_url)
    monkeypatch.setattr(live_tools.urllib.request,"build_opener",lambda *a: Opener())
    from lhas.live_tools import ResumeReaderTool, WebSearchTool, WebFetchTool, JobParseTool, JobMatchTool, JobRankTool, ShortlistArtifactTool
    registry=ToolRegistry()
    for t in (ResumeReaderTool(),WebSearchTool(P()),WebFetchTool(),JobParseTool(),JobMatchTool(),JobRankTool(),ShortlistArtifactTool()): registry.register(t)
    project=Project(name="e2e-d2"); ProjectRepository(db).create(project)
    names=["document.resume.read","web.search","web.fetch","job.parse","job.match","job.rank","artifact.write"]
    goal=Goal(project_id=project.id,objective="find roles",allowed_capabilities=names,metadata={"plan_steps":names,"resume_path":str(resume),"query":"AI agent"})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),registry).execute_goal(goal,context={"live":True}))
    assert plan.status.value == "COMPLETED"
    fetch=next(s for s in plan.steps if s.capability=="web.fetch"); assert len(fetch.output["results"])>=2
    parsed=next(s for s in plan.steps if s.capability=="job.parse"); assert len(parsed.output["jobs"])==2
    matched=next(s for s in plan.steps if s.capability=="job.match"); assert any(x["match_score"]>0 for x in matched.output["jobs"]) and all(x["fit_reasons"] and x["evidence"] for x in matched.output["jobs"])
    ranked=next(s for s in plan.steps if s.capability=="job.rank"); assert ranked.output["shortlist"][0]["match_score"]>=ranked.output["shortlist"][1]["match_score"]
    artifact=next(s for s in plan.steps if s.capability=="artifact.write").output["artifact_path"]
    data=json.loads((Path(artifact)/"shortlist.json").read_text(encoding="utf-8")); assert "steps" not in data and data["shortlist"][0]["source_url"]

def test_fetch_false_green_guards():
    empty=req("web.fetch",{}); empty.context={"steps":{"x":{"capability":"web.search","output":{"results":[]}}}}
    assert asyncio.run(WebFetchTool().execute(empty)).error_type == "NO_SEARCH_RESULTS"

def test_expiration_live_path_and_rank_filter(db):
    from lhas.live_tools import JobParseTool, JobRankTool
    context={"steps":{"f":{"capability":"web.fetch","output":{"results":[{"url":"https://a","title":"A","text":"closed role","search_snippet":"closed"},{"url":"https://b","title":"B","text":"open position apply now","search_snippet":"open"}]}}}}
    parsed=asyncio.run(JobParseTool().execute(req("job.parse",{}))); parsed
    request=req("job.parse",{}); request.context=context
    parsed=asyncio.run(JobParseTool().execute(request)); assert parsed.output["jobs"][0]["status"]=="expired"
    rank=req("job.rank",{}); rank.context={"steps":{"m":{"capability":"job.match","output":{"jobs":[{**j,"match_score":1} for j in parsed.output["jobs"]]}}}}
    result=asyncio.run(JobRankTool().execute(rank)); assert len(result.output["shortlist"])==1

def test_all_fetch_failed_plan_recovery(db):
    from lhas.domain.models import Project
    from lhas.persistence.repositories import ProjectRepository
    from lhas.planning.models import Goal, CapabilitySpec
    from lhas.planning.planner import DeterministicPlanner
    from lhas.planning.service import PlanExecutionService
    from lhas.tools.protocol import ToolResultStatus
    project=Project(name="all-fetch-fail"); ProjectRepository(db).create(project)
    search=FakeTool(CapabilitySpec(name="web.search",description="search"),lambda r:{"results":[{"url":"https://x"}]})
    fetch=FakeTool(CapabilitySpec(name="web.fetch",description="fetch"),lambda r:ToolResult(status=ToolResultStatus.FAILURE,error_type="NETWORK_ERROR",error_message="offline"))
    reg=ToolRegistry(); reg.register(search); reg.register(fetch)
    goal=Goal(project_id=project.id,objective="x",allowed_capabilities=["web.search","web.fetch"],metadata={"plan_steps":["web.search","web.fetch"]})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).execute_goal(goal)); assert plan.status.value=="FAILED" and plan.steps[1].status.value=="FAILED"

def test_real_webfetch_all_failed_recovery(db,monkeypatch):
    from lhas.domain.models import Project
    from lhas.persistence.repositories import ProjectRepository, RunRepository, AttemptRepository
    from lhas.persistence.phaseb_repos import FailureReportRepository, RecoveryActionRepository
    from lhas.planning.models import Goal, CapabilitySpec
    from lhas.planning.planner import DeterministicPlanner
    from lhas.planning.service import PlanExecutionService
    project=Project(name="real-fetch-fail"); ProjectRepository(db).create(project)
    search=FakeTool(CapabilitySpec(name="web.search",description="search"),lambda r:{"results":[{"url":"https://example.com/a"},{"url":"https://example.com/b"}]})
    class BadOpener:
        def open(self,req,timeout=15): raise urllib.error.URLError("offline")
    monkeypatch.setattr(live_tools.urllib.request,"build_opener",lambda *a: BadOpener())
    reg=ToolRegistry(); reg.register(search); reg.register(WebFetchTool())
    goal=Goal(project_id=project.id,objective="x",allowed_capabilities=["web.search","web.fetch"],metadata={"plan_steps":["web.search","web.fetch"]})
    plan=asyncio.run(PlanExecutionService(db,DeterministicPlanner(),reg).execute_goal(goal)); assert plan.status.value=="FAILED"
    run=RunRepository(db).list_for_task(plan.steps[1].task_id)[0]; attempts=AttemptRepository(db).list_for_run(run.id)
    assert len(attempts)==2 and FailureReportRepository(db).list_for_attempt(attempts[0].id) and RecoveryActionRepository(db).list_for_attempt(attempts[0].id)

def test_redirect_private_block_before_target(monkeypatch):
    class Resp:
        status=302; headers={"Content-Type":"text/html","Location":"http://127.0.0.1/secret"}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,*a): return b""
        def geturl(self): return "http://127.0.0.1/secret"
    class Opener:
        def open(self,req,timeout=15): return Resp()
    monkeypatch.setattr(live_tools.urllib.request,"build_opener",lambda *a: Opener())
    result=asyncio.run(WebFetchTool().execute(req("web.fetch",{"url":"https://public.example"})))
    assert result.error_type == "SSRF_BLOCKED"

def test_pdf_native_extraction(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
    p=tmp_path/"resume.pdf"; writer=PdfWriter(); writer.add_blank_page(width=72,height=72)
    page=writer.pages[0]
    font=DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
    font_ref=writer._add_object(font)
    page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):font_ref})})
    stream=DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 10 50 Td (React TypeScript Python Agent) Tj ET")
    page[NameObject("/Contents")]=writer._add_object(stream)
    with p.open("wb") as fh: writer.write(fh)
    result=asyncio.run(ResumeReaderTool().execute(req("document.resume.read",{"path":str(p)})))
    assert result.status == ToolResultStatus.SUCCESS and "React TypeScript Python Agent" in result.output["text"]

def test_pdf_dependency_missing(monkeypatch,tmp_path):
    p=tmp_path/"resume.pdf"; p.write_bytes(b"%PDF-1.4")
    real=__import__("builtins").__import__
    def fake(name,*a,**k):
        if name == "pypdf": raise ImportError("missing")
        return real(name,*a,**k)
    monkeypatch.setattr("builtins.__import__",fake)
    result=asyncio.run(ResumeReaderTool().execute(req("document.resume.read",{"path":str(p)})))
    assert result.error_type == "PDF_DEPENDENCY_MISSING"
