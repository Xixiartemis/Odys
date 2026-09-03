from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from lhas.agent.models import AgentRole
from lhas.knowledge import LocalKnowledgeProvider, ProjectContextDiscovery
from lhas.memory import BuiltinMemoryProvider
from lhas.persistence.database import Database
from lhas.persistence.platform_repositories import SessionRepository
from lhas.platform_models import ConversationSession, SessionMessage
from lhas.skills import SkillRegistry


def _skill_tree(root: Path):
    skill=root/"coding"/"demo"; (skill/"references").mkdir(parents=True)
    (skill/"SKILL.md").write_text("---\nname: coding/demo\ndescription: Demo skill\nversion: 1\n---\n\nLevel one instructions\n",encoding="utf-8")
    (skill/"references"/"details.md").write_text("Level two details",encoding="utf-8")


def test_skills_level_zero_metadata_only(tmp_path):
    _skill_tree(tmp_path); items=SkillRegistry([tmp_path]).list()
    assert items[0].model_dump()=={"name":"coding/demo","description":"Demo skill","metadata":{"version":"1"}}


def test_skills_level_one_and_two_progressive_disclosure(tmp_path):
    _skill_tree(tmp_path); registry=SkillRegistry([tmp_path])
    assert "Level one" in registry.view("coding/demo").content
    assert "Level two" in registry.view("coding/demo","details.md").content


@pytest.mark.parametrize("reference",["../SKILL.md","/absolute.md","missing.md"])
def test_skill_reference_is_scoped(tmp_path,reference):
    _skill_tree(tmp_path)
    with pytest.raises(ValueError): SkillRegistry([tmp_path]).view("coding/demo",reference)


def test_builtin_skills_are_discoverable():
    root=Path(__file__).resolve().parents[1]/".odys"/"skills"; names={item.name for item in SkillRegistry([root]).list()}
    assert names=={"coding/bug-fix","coding/code-review"}


def test_memory_requires_root_approval(tmp_path):
    memory=BuiltinMemoryProvider(tmp_path)
    with pytest.raises(PermissionError,match="APPROVAL"):
        memory.add("remember this",scope="memory",role=AgentRole.ROOT)
    item=memory.add("remember this",scope="memory",role=AgentRole.ROOT,approved=True)
    assert memory.search("remember")[0].id==item.id


def test_subagent_memory_write_is_denied_even_with_approval(tmp_path):
    with pytest.raises(PermissionError,match="ROLE_DENIED"):
        BuiltinMemoryProvider(tmp_path).add("secret",scope="user",role=AgentRole.RESEARCHER,approved=True)


def test_memory_replace_remove_and_bounds(tmp_path):
    memory=BuiltinMemoryProvider(tmp_path,require_write_approval=False)
    item=memory.add("old value",scope="user",role=AgentRole.ROOT)
    assert memory.replace(item.id,"new value",role=AgentRole.ROOT).content=="new value"
    assert memory.remove(item.id,role=AgentRole.ROOT) is True
    assert memory.list()==[]
    bounded=memory.add("x"*100_000,scope="memory",role=AgentRole.ROOT)
    assert len(bounded.content)==4_000
    assert len((tmp_path/"MEMORY.md").read_text(encoding="utf-8"))<64_000


def _session_repo():
    db=Database(":memory:"); db.init_db(); return db,SessionRepository(db)


def test_session_persistence_and_lineage():
    db,repo=_session_repo(); parent=repo.create(ConversationSession(title="parent")); child=repo.create(ConversationSession(title="child",parent_session_id=parent.id))
    repo.append(SessionMessage(session_id=child.id,role="user",content="durable platform question"))
    sessions=repo.list()
    persisted_child=next(session for session in sessions if session.id==child.id)
    persisted_parent=next(session for session in sessions if session.id==parent.id)
    assert persisted_child.parent_session_id==parent.id
    assert persisted_parent.parent_session_id is None
    assert repo.read(child.id)[0].content=="durable platform question"
    db.close()


def test_session_lineage_is_independent_of_list_order():
    fixed=datetime(2026,1,1,tzinfo=timezone.utc)
    db,repo=_session_repo()
    parent=repo.create(ConversationSession(title="parent",created_at=fixed,updated_at=fixed))
    child=repo.create(ConversationSession(title="child",parent_session_id=parent.id,created_at=fixed,updated_at=fixed))
    repo.append(SessionMessage(session_id=child.id,role="user",content="durable platform question",created_at=fixed))
    sessions=repo.list()
    persisted_child=next(session for session in sessions if session.id==child.id)
    persisted_parent=next(session for session in sessions if session.id==parent.id)
    assert persisted_child.parent_session_id==parent.id
    assert persisted_parent.parent_session_id is None
    db.close()


def test_session_lineage_survives_process_reload(tmp_path):
    db_path=tmp_path/"sessions.db"
    db=Database(db_path); db.init_db(); repo=SessionRepository(db)
    parent=repo.create(ConversationSession(title="parent"))
    child=repo.create(ConversationSession(title="child",parent_session_id=parent.id))
    db.close()
    reload_code="""
import sys
from lhas.persistence.database import Database
from lhas.persistence.platform_repositories import SessionRepository

db=Database(sys.argv[1])
repo=SessionRepository(db)
sessions={session.id: session for session in repo.list()}
assert sessions[sys.argv[2]].parent_session_id == sys.argv[3]
assert sessions[sys.argv[3]].parent_session_id is None
db.close()
"""
    subprocess.run([sys.executable,"-c",reload_code,str(db_path),child.id,parent.id],cwd=Path(__file__).resolve().parents[1],check=True,capture_output=True,text=True)


def test_session_fts_search():
    db,repo=_session_repo(); session=repo.create(ConversationSession())
    repo.append(SessionMessage(session_id=session.id,role="user",content="alpha durable delegation evidence"))
    repo.append(SessionMessage(session_id=session.id,role="assistant",content="unrelated response"))
    hits=repo.search("durable delegation")
    assert len(hits)==1 and hits[0].role=="user"
    db.close()


def test_session_rejects_raw_provider_role():
    db,repo=_session_repo(); session=repo.create(ConversationSession())
    with pytest.raises(ValueError,match="role"):
        repo.append(SessionMessage(session_id=session.id,role="reasoning",content="hidden"))
    db.close()


def test_knowledge_lexical_search_and_open(tmp_path):
    (tmp_path/"docs").mkdir(); (tmp_path/"README.md").write_text("Odys durable runtime",encoding="utf-8"); (tmp_path/"docs"/"agent.md").write_text("Agent delegation architecture",encoding="utf-8")
    knowledge=LocalKnowledgeProvider(tmp_path); hits=knowledge.search("delegation architecture")
    assert hits[0].ref=="docs/agent.md"
    assert "delegation" in knowledge.open(hits[0].ref)


def test_knowledge_path_escape_is_rejected(tmp_path):
    with pytest.raises(ValueError,match="ESCAPE"):
        LocalKnowledgeProvider(tmp_path).open("../secret.txt")


def test_project_context_discovers_agents_then_odys(tmp_path):
    (tmp_path/"AGENTS.md").write_text("general",encoding="utf-8"); (tmp_path/".odys.md").write_text("odys override",encoding="utf-8")
    context=ProjectContextDiscovery().discover(tmp_path)
    assert list(context)==["AGENTS.md",".odys.md"]
