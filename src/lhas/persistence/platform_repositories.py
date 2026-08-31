"""Repositories for conversation sessions and durable delegation."""

from __future__ import annotations

import re

from sqlalchemy import select, text

from lhas.domain.models import json_dumps, json_loads, utcnow
from lhas.persistence.orm import ConversationSessionRow, DelegationRow, SessionMessageRow
from lhas.platform_models import ConversationSession, Delegation, DelegationStatus, SessionMessage


class SessionRepository:
    def __init__(self, db):
        self.db = db
        self._ensure_fts()

    def _ensure_fts(self) -> None:
        with self.db.session() as session:
            session.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS session_message_fts USING fts5(message_id UNINDEXED, session_id UNINDEXED, content)"))

    def create(self, item: ConversationSession) -> ConversationSession:
        with self.db.session() as session:
            session.add(ConversationSessionRow(id=item.id,title=item.title,parent_session_id=item.parent_session_id,metadata_json=json_dumps(item.metadata),created_at=item.created_at,updated_at=item.updated_at))
        return item

    def append(self, message: SessionMessage) -> SessionMessage:
        if message.role not in {"user", "assistant", "tool"}:
            raise ValueError("unsupported session message role")
        with self.db.session() as session:
            if session.get(ConversationSessionRow, message.session_id) is None:
                raise KeyError(f"conversation session not found: {message.session_id}")
            session.add(SessionMessageRow(id=message.id,session_id=message.session_id,role=message.role,content=message.content,safe_tool_summary=message.safe_tool_summary,metadata_json=json_dumps(message.metadata),created_at=message.created_at))
            session.execute(text("INSERT INTO session_message_fts(message_id, session_id, content) VALUES (:message_id, :session_id, :content)"),{"message_id":message.id,"session_id":message.session_id,"content":message.content})
            row=session.get(ConversationSessionRow,message.session_id); row.updated_at=message.created_at
        return message

    def list(self, limit: int = 50) -> list[ConversationSession]:
        with self.db.session() as session:
            rows=session.execute(select(ConversationSessionRow).order_by(ConversationSessionRow.updated_at.desc()).limit(min(max(limit,1),200))).scalars().all()
            return [self._session(row) for row in rows]

    def read(self, session_id: str, limit: int = 100) -> list[SessionMessage]:
        with self.db.session() as session:
            rows=session.execute(select(SessionMessageRow).where(SessionMessageRow.session_id==session_id).order_by(SessionMessageRow.created_at).limit(min(max(limit,1),500))).scalars().all()
            return [self._message(row) for row in rows]

    def scroll(self, session_id: str, limit: int = 100) -> list[SessionMessage]:
        return self.read(session_id, limit)

    def search(self, query: str, limit: int = 20) -> list[SessionMessage]:
        terms=re.findall(r"[\w-]+",query,flags=re.UNICODE)[:12]
        if not terms:
            return []
        expression=" AND ".join(f'"{term}"' for term in terms)
        with self.db.session() as session:
            ids=session.execute(text("SELECT message_id FROM session_message_fts WHERE session_message_fts MATCH :query ORDER BY bm25(session_message_fts) LIMIT :limit"),{"query":expression,"limit":min(max(limit,1),100)}).scalars().all()
            if not ids: return []
            rows=session.execute(select(SessionMessageRow).where(SessionMessageRow.id.in_(ids))).scalars().all()
            by_id={row.id:row for row in rows}
            return [self._message(by_id[item_id]) for item_id in ids if item_id in by_id]

    @staticmethod
    def _session(row):
        return ConversationSession(id=row.id,title=row.title,parent_session_id=row.parent_session_id,metadata=json_loads(row.metadata_json) or {},created_at=row.created_at,updated_at=row.updated_at)

    @staticmethod
    def _message(row):
        return SessionMessage(id=row.id,session_id=row.session_id,role=row.role,content=row.content,safe_tool_summary=row.safe_tool_summary,metadata=json_loads(row.metadata_json) or {},created_at=row.created_at)


class DelegationRepository:
    def __init__(self, db): self.db=db

    def create(self, item: Delegation) -> Delegation:
        with self.db.session() as session:
            session.add(DelegationRow(id=item.id,parent_agent_id=item.parent_agent_id,parent_task_id=item.parent_task_id,parent_run_id=item.parent_run_id,child_agent_id=item.child_agent_id,child_task_id=item.child_task_id,child_run_id=item.child_run_id,spawn_depth=item.spawn_depth,status=item.status.value,context_json=json_dumps(item.context),result_json=json_dumps(item.result),created_at=item.created_at,updated_at=item.updated_at))
        return item

    def update(self, item: Delegation) -> Delegation:
        item.updated_at=utcnow()
        with self.db.session() as session:
            row=session.get(DelegationRow,item.id)
            if row is None: raise KeyError(f"delegation not found: {item.id}")
            row.child_run_id=item.child_run_id; row.status=item.status.value; row.result_json=json_dumps(item.result); row.updated_at=item.updated_at
        return item

    def get(self, item_id: str) -> Delegation | None:
        with self.db.session() as session:
            row=session.get(DelegationRow,item_id)
            return self._from_row(row) if row else None

    def list_for_parent_run(self, run_id: str) -> list[Delegation]:
        with self.db.session() as session:
            rows=session.execute(select(DelegationRow).where(DelegationRow.parent_run_id==run_id).order_by(DelegationRow.created_at)).scalars().all()
            return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row):
        return Delegation(id=row.id,parent_agent_id=row.parent_agent_id,parent_task_id=row.parent_task_id,parent_run_id=row.parent_run_id,child_agent_id=row.child_agent_id,child_task_id=row.child_task_id,child_run_id=row.child_run_id,spawn_depth=row.spawn_depth,status=DelegationStatus(row.status),context=json_loads(row.context_json) or {},result=json_loads(row.result_json) or {},created_at=row.created_at,updated_at=row.updated_at)
