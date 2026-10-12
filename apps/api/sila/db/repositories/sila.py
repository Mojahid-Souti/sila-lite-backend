"""SILA conversation repository — sessions + transcript turns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import SilaDataset, SilaMessage, SilaSession, SilaUserMemory


class SessionSummary:
    """A Recents row: id, the AI title (None until written → UI shows "Untitled"),
    started_at."""

    __slots__ = ("id", "title", "started_at")

    def __init__(self, id: uuid.UUID, title: str | None, started_at: datetime) -> None:
        self.id = id
        self.title = title
        self.started_at = started_at


class SilaRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_session(self, *, workspace_id: uuid.UUID, user_id: uuid.UUID) -> SilaSession:
        session = SilaSession(workspace_id=workspace_id, user_id=user_id)
        self._db.add(session)
        await self._db.commit()
        await self._db.refresh(session)
        return session

    async def merge_session_metadata(self, session_id: uuid.UUID, patch: dict) -> None:  # type: ignore[type-arg]
        """Merge keys into the session's metadata JSONB — durable turn state
        (attachment stash, dashboard identity) that must survive a reconnect."""
        session = await self._db.get(SilaSession, session_id)
        if session is not None:
            session.metadata_ = {**(session.metadata_ or {}), **patch}
            await self._db.flush()

    async def get_session_metadata(self, session_id: uuid.UUID) -> dict:  # type: ignore[type-arg]
        session = await self._db.get(SilaSession, session_id)
        return dict(session.metadata_ or {}) if session is not None else {}

    async def save_uploaded_files(self, session_id: uuid.UUID, files: list[dict[str, str]]) -> None:
        """Persist the attachment stash on the session (metadata JSONB) so data
        tools survive a reconnect — the in-memory stash dies with the socket."""
        await self.merge_session_metadata(session_id, {"uploaded_files": files})

    async def load_uploaded_files(self, session_id: uuid.UUID) -> list[dict[str, str]]:
        files = (await self.get_session_metadata(session_id)).get("uploaded_files", [])
        return files if isinstance(files, list) else []

    async def end_session(self, session_id: uuid.UUID) -> None:
        # Idempotent Core UPDATE: if the row is gone (e.g. the user deleted the session
        # while the socket was open) this matches 0 rows and no-ops — never raises
        # StaleDataError from a stale ORM object, which used to crash the WS at close.
        await self._db.execute(
            update(SilaSession)
            .where(SilaSession.id == session_id, SilaSession.ended_at.is_(None))
            .values(ended_at=datetime.now(UTC).replace(tzinfo=None))
        )
        await self._db.commit()

    async def sessions_today(self, user_id: uuid.UUID) -> int:
        """Daily rate-limit counter. O(log n) via ix_sila_sessions_user_started."""
        since = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
        result = await self._db.execute(
            select(func.count())
            .select_from(SilaSession)
            .where(SilaSession.user_id == user_id, SilaSession.started_at >= since)
        )
        return int(result.scalar_one())

    async def add_message(
        self,
        *,
        session_id: uuid.UUID,
        workspace_id: uuid.UUID,
        role: str,
        content: str,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        self._db.add(
            SilaMessage(
                session_id=session_id,
                workspace_id=workspace_id,
                role=role,
                content=content,
                trace=trace,
            )
        )
        await self._db.commit()

    async def recent_messages(self, session_id: uuid.UUID, *, limit: int = 20) -> list[SilaMessage]:
        """Last `limit` turns, oldest first (context window). O(log n)."""
        result = await self._db.execute(
            select(SilaMessage)
            .where(SilaMessage.session_id == session_id)
            .order_by(SilaMessage.created_at.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    async def list_sessions(self, user_id: uuid.UUID, *, limit: int = 30) -> list[SessionSummary]:
        """The user's recent sessions for the sidebar, newest first. The title is
        the session's first *user* message; empty sessions are omitted. The title
        is a correlated scalar subquery, so this is one query (no N+1). O(n log n)."""
        title_sq = (
            select(SilaMessage.content)
            .where(SilaMessage.session_id == SilaSession.id, SilaMessage.role == "user")
            .order_by(SilaMessage.created_at.asc())
            .limit(1)
            .correlate(SilaSession)
            .scalar_subquery()
        )
        # Return the AI title (nullable → UI shows "Untitled"); the first-message
        # subquery is only a "has any messages" gate so empty sessions are omitted.
        result = await self._db.execute(
            select(
                SilaSession.id, SilaSession.started_at, SilaSession.title, title_sq.label("first")
            )
            .where(SilaSession.user_id == user_id)
            .order_by(SilaSession.started_at.desc())
            .limit(limit)
        )
        return [
            SessionSummary(row.id, row.title, row.started_at) for row in result.all() if row.first
        ]

    async def set_title(self, session_id: uuid.UUID, title: str) -> None:
        """Store an AI-written title (only if one isn't set yet). O(1). Idempotent Core
        UPDATE so a missing/deleted row no-ops instead of raising StaleDataError."""
        await self._db.execute(
            update(SilaSession)
            .where(SilaSession.id == session_id, SilaSession.title.is_(None))
            .values(title=title.strip()[:120])
        )
        await self._db.commit()

    async def needs_title(self, session_id: uuid.UUID, *, min_user_turns: int = 1) -> bool:
        """True when the session has no title yet and enough turns to name it.

        One turn is enough: even "hi" → "Greeting" should replace the pulsing
        "Untitled" as soon as the assistant has replied once.
        """
        session = await self._db.get(SilaSession, session_id)
        if session is None or session.title:
            return False
        result = await self._db.execute(
            select(func.count())
            .select_from(SilaMessage)
            .where(SilaMessage.session_id == session_id, SilaMessage.role == "user")
        )
        return int(result.scalar_one()) >= min_user_turns

    async def messages_for_user(
        self, session_id: uuid.UUID, user_id: uuid.UUID, *, limit: int = 200
    ) -> list[SilaMessage] | None:
        """A session's messages (oldest first) for display/resume — but only if the
        user owns it. Returns None when the session isn't theirs. O(log n)."""
        session = await self._db.get(SilaSession, session_id)
        if session is None or session.user_id != user_id:
            return None
        result = await self._db.execute(
            select(SilaMessage)
            .where(SilaMessage.session_id == session_id)
            .order_by(SilaMessage.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def owns_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Ownership check used by the WS to resume an existing session. O(log n)."""
        session = await self._db.get(SilaSession, session_id)
        return session is not None and session.user_id == user_id

    # ------------------------------------------------------------------
    # Per-user memory (cross-chat durable facts; capacity-capped)
    # ------------------------------------------------------------------

    async def list_memories(self, user_id: uuid.UUID) -> list[SilaUserMemory]:
        """The user's memories, oldest first. O(log n) via ix_sila_user_memories_user."""
        result = await self._db.execute(
            select(SilaUserMemory)
            .where(SilaUserMemory.user_id == user_id)
            .order_by(SilaUserMemory.created_at.asc())
        )
        return list(result.scalars())

    async def count_memories(self, user_id: uuid.UUID) -> int:
        result = await self._db.execute(
            select(func.count())
            .select_from(SilaUserMemory)
            .where(SilaUserMemory.user_id == user_id)
        )
        return int(result.scalar_one())

    async def add_memory(
        self, user_id: uuid.UUID, content: str, *, cap: int
    ) -> SilaUserMemory | None:
        """Save one fact; None when the cap is reached (caller tells the user to
        prune). Near-duplicates (same normalized text) update in place instead of
        burning a slot."""
        text = content.strip()[:400]
        if not text:
            return None
        existing = await self.list_memories(user_id)
        for m in existing:
            if m.content.strip().casefold() == text.casefold():
                return m  # already known — idempotent
        if len(existing) >= cap:
            return None
        row = SilaUserMemory(user_id=user_id, content=text)
        self._db.add(row)
        await self._db.commit()
        return row

    async def delete_memory(self, memory_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        row = await self._db.get(SilaUserMemory, memory_id)
        if row is None or row.user_id != user_id:
            return False
        await self._db.delete(row)
        # No commit here: get_db wraps the request in one transaction, so the
        # caller's audit row lands atomically with this change. Committing
        # inside closed that transaction and broke everything after it.
        return True

    async def clear_memories(self, user_id: uuid.UUID) -> int:
        result = await self._db.execute(
            delete(SilaUserMemory).where(SilaUserMemory.user_id == user_id)
        )
        # No commit here: get_db wraps the request in one transaction, so the
        # caller's audit row lands atomically with this change. Committing
        # inside closed that transaction and broke everything after it.
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # Analysis Engine datasets (full files on disk; per-user handles)
    # ------------------------------------------------------------------

    async def create_dataset(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        filename: str,
        path: str,
        size_bytes: int,
        rows: int | None,
    ) -> SilaDataset:
        row = SilaDataset(
            user_id=user_id,
            workspace_id=workspace_id,
            filename=filename[:200],
            path=path,
            size_bytes=size_bytes,
            rows=rows,
        )
        self._db.add(row)
        # flush, not commit — HTTP requests run inside get_db's begin() context
        # (a commit here closed the tx and broke the audit write that follows).
        await self._db.flush()
        return row

    async def get_dataset(self, dataset_id: uuid.UUID, user_id: uuid.UUID) -> SilaDataset | None:
        """Fetch a dataset the caller OWNS — fail-closed on anyone else's id."""
        row = await self._db.get(SilaDataset, dataset_id)
        if row is None or row.user_id != user_id:
            return None
        return row

    async def delete_sessions(self, session_ids: list[uuid.UUID], user_id: uuid.UUID) -> int:
        """Delete many of the user's own sessions and their messages; returns how
        many were actually removed.

        Ownership is filtered in SQL, not in Python, so a caller cannot delete
        another user's session by putting its id in the list — unowned ids are
        simply not matched. Silently skipping them (rather than 404-ing the whole
        batch) also makes the call idempotent: re-sending a batch after a partial
        failure is safe.

        Three statements regardless of batch size — resolve owned ids, delete
        their messages, delete the sessions — so this is not an N+1 over the
        selection. Time O(n + m) for n requested ids and m of their messages
        (index scans on the id and session_id keys); space O(n) for the id set.
        """
        if not session_ids:
            return 0
        owned = (
            (
                await self._db.execute(
                    select(SilaSession.id).where(
                        SilaSession.id.in_(session_ids), SilaSession.user_id == user_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if not owned:
            return 0
        await self._db.execute(delete(SilaMessage).where(SilaMessage.session_id.in_(owned)))
        await self._db.execute(delete(SilaSession).where(SilaSession.id.in_(owned)))
        # No commit here: get_db wraps the request in one transaction, so the
        # caller's audit row lands atomically with this change. Committing
        # inside closed that transaction and broke everything after it.
        return len(owned)

    async def delete_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Delete a session (and its messages) the user owns. Idempotent-ish:
        returns False if it isn't theirs / already gone. O(n) over its messages."""
        session = await self._db.get(SilaSession, session_id)
        if session is None or session.user_id != user_id:
            return False
        await self._db.execute(delete(SilaMessage).where(SilaMessage.session_id == session_id))
        await self._db.delete(session)
        # No commit here: get_db wraps the request in one transaction, so the
        # caller's audit row lands atomically with this change. Committing
        # inside closed that transaction and broke everything after it.
        return True
