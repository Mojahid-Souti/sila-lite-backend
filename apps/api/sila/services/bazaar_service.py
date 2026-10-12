"""BazaarService — catalog, hire, and review business logic.

Responsibilities:
  - Search / list published agents (Meilisearch primary, SQL fallback)
  - Hire / unhire agents into a workspace (idempotent)
  - Submit / update reviews with profanity filtering
  - Index agents into Meilisearch on publish/archive events (called by AgentService)
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from better_profanity import profanity
from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.search.base import SearchAdapter
from sila.core.constants import (
    MAX_REPORTS_PER_AGENT_PER_DAY,
    Action,
    Outcome,
    ReactionType,
    ResourceType,
)
from sila.db.models import (
    Agent,
    AgentReaction,
    AgentReport,
    AgentTool,
    Category,
    Hire,
    Review,
    Tool,
)
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.bazaar import BazaarRepository
from sila.db.repositories.category import CategoryRepository
from sila.db.repositories.hire import HireRepository
from sila.db.repositories.organization import OrganizationRepository
from sila.db.repositories.review import ReviewRepository
from sila.services.notification_service import NotificationKind, notify

profanity.load_censor_words()

_TAG_RE = re.compile(r"<[^>]*>")


def _sanitize_reason(raw: str) -> str:
    """Strip HTML tags + control chars and cap at 2000 chars (matches the DB check)."""
    no_tags = _TAG_RE.sub("", raw)
    cleaned = "".join(ch for ch in no_tags if ch == "\n" or ch >= " ")
    return cleaned.strip()[:2000]


class BazaarService:
    def __init__(self, db: AsyncSession, search: SearchAdapter) -> None:
        self._db = db
        self._search = search
        self._hires = HireRepository(db)
        self._reviews = ReviewRepository(db)
        self._categories = CategoryRepository(db)
        self._bazaar = BazaarRepository(db)
        self._orgs = OrganizationRepository(db)
        self._audit = AuditRepository(db)

    async def _decorate_provenance(
        self,
        items: list[dict[str, object]],
        agents: list[Agent],
        *,
        user_id: uuid.UUID,
        my_org_ids: set[uuid.UUID],
        hired_ids: set[uuid.UUID],
    ) -> None:
        """Tag each item with provenance (mine/shared/org/third_party), the creator
        name, and the owning org name — the Bazaar's four-section grouping. Batched:
        one org-by-workspace query, one org-names query, one creator lookup. O(n)."""
        if not agents:
            return
        ws_to_org = await self._orgs.org_id_by_workspace({a.workspace_id for a in agents})
        org_names = await self._orgs.names_by_ids(set(ws_to_org.values()))
        creators = await self._orgs.display_names({a.owner_id for a in agents})
        for item, a in zip(items, agents, strict=False):
            org_id = ws_to_org.get(a.workspace_id)
            item["organization_id"] = str(org_id) if org_id else None
            item["org_name"] = org_names.get(org_id) if org_id else None
            item["creator"] = creators.get(a.owner_id, "Unknown")
            if a.owner_id == user_id:
                item["provenance"] = "mine"
            elif a.id in hired_ids:
                item["provenance"] = "shared"
            elif org_id is not None and org_id in my_org_ids:
                item["provenance"] = "org"
            else:
                item["provenance"] = "third_party"

    # ------------------------------------------------------------------
    # Catalog — search and list
    # ------------------------------------------------------------------

    async def search_catalog(
        self,
        query: str,
        *,
        category_ids: list[uuid.UUID],
        page: int,
        size: int,
        calling_workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict[str, object]:
        """Search published agents. Meilisearch primary, SQL fallback.

        Enriches each result with hired=bool for the calling workspace and its
        provenance (mine/shared/org/third_party) + creator + org name.
        Returns paginated catalog response. Hot path: O(log n).
        """
        cat_strs = [str(c) for c in category_ids]

        if query:
            hits, total = await self._search.search(
                query, category_ids=cat_strs, page=page, size=size
            )
            # hits from Meilisearch are dicts; convert to consistent format
            agent_ids = [uuid.UUID(str(h["id"])) for h in hits]
            # Fetch full rows to get authoritative data (search hits may be stale by ms)
            agents = []
            for aid in agent_ids:
                agent = await self._bazaar.get_published(aid)
                if agent:
                    agents.append(agent)
        else:
            cat_filter = category_ids[0] if len(category_ids) == 1 else None
            agents, total = await self._bazaar.list_published(
                category_id=cat_filter, page=page, size=size
            )

        hired_ids = await self._hires.hired_agent_ids(calling_workspace_id)
        items = [self._agent_to_dict(a, hired_ids) for a in agents]
        my_org_ids = await self._orgs.org_ids_for_user(user_id)
        await self._decorate_provenance(
            items, agents, user_id=user_id, my_org_ids=my_org_ids, hired_ids=hired_ids
        )
        return {"items": items, "total": total, "page": page, "size": size}

    async def get_agent_detail(
        self,
        agent_id: uuid.UUID,
        *,
        calling_workspace_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> dict[str, object]:
        """Fetch published agent detail with rating, hired flag, and the caller's reaction. O(1)."""
        agent = await self._bazaar.get_published(agent_id)
        if not agent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        hired_ids = await self._hires.hired_agent_ids(calling_workspace_id)
        d = self._agent_to_dict(agent, hired_ids)
        rating = await self._reviews.avg_rating(agent_id)
        d["rating"] = rating
        category_ids = await self._categories.get_agent_category_ids(agent_id)
        d["category_ids"] = [str(c) for c in category_ids]
        d["my_reaction"] = None
        if user_id is not None:
            mine = (
                await self._db.execute(
                    select(AgentReaction.reaction).where(
                        AgentReaction.agent_id == agent_id, AgentReaction.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            d["my_reaction"] = mine
            my_org_ids = await self._orgs.org_ids_for_user(user_id)
            await self._decorate_provenance(
                [d], [agent], user_id=user_id, my_org_ids=my_org_ids, hired_ids=hired_ids
            )
        # Manager's agent-card fields: version (already in d), permissions, token usage.
        d["version"] = agent.version
        perms = (
            (
                await self._db.execute(
                    select(Tool.name)
                    .join(AgentTool, AgentTool.tool_id == Tool.id)
                    .where(AgentTool.agent_id == agent_id)
                )
            )
            .scalars()
            .all()
        )
        d["permissions"] = sorted(set(perms))
        # All-time tokens the agent consumed, from its test-run audit metadata (the
        # same source the Command Center's compute-cost uses). Shown for every agent.
        tokens = (
            await self._db.execute(
                text(
                    "SELECT COALESCE(SUM((metadata->>'tokens')::bigint), 0) FROM audit_log "
                    "WHERE action = 'agent.test' AND resource_id = :aid AND (metadata ? 'tokens')"
                ),
                {"aid": agent_id},
            )
        ).scalar()
        d["token_usage"] = int(tokens or 0)
        return d

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def list_categories(self) -> list[Category]:
        """Return all categories. O(n) where n < 100."""
        return await self._categories.list_all()

    # ------------------------------------------------------------------
    # Hire / Unhire
    # ------------------------------------------------------------------

    async def hire(
        self,
        *,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        hired_by: uuid.UUID,
        background_tasks: BackgroundTasks,
    ) -> Hire:
        """Hire an agent into a workspace. Idempotent: re-hiring same agent is a no-op.

        Only published agents can be hired.
        """
        agent = await self._bazaar.get_published(agent_id)
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found or not published",
            )
        hire = await self._hires.hire(
            workspace_id=workspace_id, agent_id=agent_id, hired_by=hired_by
        )
        await notify(
            self._db,
            user_id=hired_by,
            workspace_id=workspace_id,
            kind=NotificationKind.AGENT_HIRED,
            title=f"Agent hired: {agent.name}",
            body="It's ready in My agents.",
            link="/bazaar/hired",
        )
        await self._db.commit()
        background_tasks.add_task(
            self._audit.record,
            actor_user_id=hired_by,
            actor_workspace_id=workspace_id,
            action=Action.HIRE_CREATE,
            resource_type=ResourceType.HIRE,
            resource_id=hire.id,
            outcome=Outcome.SUCCESS,
        )
        return hire

    async def unhire(
        self,
        *,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        unhired_by: uuid.UUID,
        background_tasks: BackgroundTasks,
    ) -> None:
        """Remove a hire. 404 if not hired."""
        deleted = await self._hires.unhire(workspace_id=workspace_id, agent_id=agent_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not hired")
        await self._db.commit()
        background_tasks.add_task(
            self._audit.record,
            actor_user_id=unhired_by,
            actor_workspace_id=workspace_id,
            action=Action.HIRE_VIEW,  # reuse for unhire audit
            resource_type=ResourceType.HIRE,
            resource_id=agent_id,
            outcome=Outcome.SUCCESS,
        )

    async def list_hires(
        self,
        workspace_id: uuid.UUID,
        *,
        page: int,
        size: int,
    ) -> dict[str, object]:
        """List agents hired by a workspace, with agent detail. O(log n)."""
        hires, total = await self._hires.list(workspace_id, page=page, size=size)
        items = []
        for h in hires:
            agent = await self._bazaar.get_published(h.agent_id)
            if agent:
                d = self._agent_to_dict(agent, {h.agent_id})
                d["hired_at"] = h.hired_at.isoformat()
                items.append(d)
        return {"items": items, "total": total, "page": page, "size": size}

    # ------------------------------------------------------------------
    # Reviews
    # ------------------------------------------------------------------

    async def submit_review(
        self,
        *,
        agent_id: uuid.UUID,
        workspace_id: uuid.UUID,
        author_id: uuid.UUID,
        rating: int,
        body: str,
        background_tasks: BackgroundTasks,
    ) -> Review:
        """Create or update a review. Profanity-filtered. One review per user per agent."""
        agent = await self._bazaar.get_published(agent_id)
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found or not published"
            )
        clean_body = profanity.censor(body)
        review = await self._reviews.upsert(
            agent_id=agent_id,
            workspace_id=workspace_id,
            author_id=author_id,
            rating=rating,
            body=clean_body,
        )
        await self._db.commit()
        background_tasks.add_task(
            self._audit.record,
            actor_user_id=author_id,
            actor_workspace_id=workspace_id,
            action=Action.REVIEW_CREATE,
            resource_type=ResourceType.REVIEW,
            resource_id=review.id,
            outcome=Outcome.SUCCESS,
        )
        return review

    async def list_reviews(self, agent_id: uuid.UUID, *, page: int, size: int) -> dict[str, object]:
        """Paginated reviews for an agent. O(log n)."""
        reviews, total = await self._reviews.list(agent_id, page=page, size=size)
        items = [
            {
                "id": str(r.id),
                "author_id": str(r.author_id),
                "rating": r.rating,
                "body": r.body,
                "created_at": r.created_at.isoformat(),
            }
            for r in reviews
        ]
        return {"items": items, "total": total, "page": page, "size": size}

    # ------------------------------------------------------------------
    # Feedback — reactions, reports, trending
    # ------------------------------------------------------------------

    async def react(
        self,
        *,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        reaction: ReactionType | None,
    ) -> dict[str, object]:
        """Set/toggle/clear the caller's like|dislike on a published agent.

        Idempotent — one reaction per user (UNIQUE). Counters on the agent are
        adjusted in the same transaction so the trending sort never drifts. O(1).
        Pass reaction=None (or the same value again) to clear it.
        """
        agent = await self._bazaar.get_published(agent_id)
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found or not published"
            )
        existing = (
            await self._db.execute(
                select(AgentReaction).where(
                    AgentReaction.agent_id == agent_id, AgentReaction.user_id == user_id
                )
            )
        ).scalar_one_or_none()

        prev = ReactionType(existing.reaction) if existing else None
        # Clicking the active reaction again clears it (toggle off).
        new = None if reaction is not None and reaction == prev else reaction

        if prev == ReactionType.LIKE:
            agent.liked_count = max(0, agent.liked_count - 1)
        elif prev == ReactionType.DISLIKE:
            agent.disliked_count = max(0, agent.disliked_count - 1)
        if new == ReactionType.LIKE:
            agent.liked_count += 1
        elif new == ReactionType.DISLIKE:
            agent.disliked_count += 1

        if new is None:
            if existing:
                await self._db.delete(existing)
        elif existing:
            existing.reaction = new.value
        else:
            self._db.add(
                AgentReaction(
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    reaction=new.value,
                )
            )
        await self._db.commit()
        return {
            "liked_count": agent.liked_count,
            "disliked_count": agent.disliked_count,
            "my_reaction": new.value if new else None,
        }

    async def report(
        self,
        *,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        reporter_id: uuid.UUID,
        reason: str,
        background_tasks: BackgroundTasks,
    ) -> AgentReport:
        """File a report on a published agent. Rate-limited per user per agent per day."""
        agent = await self._bazaar.get_published(agent_id)
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found or not published"
            )
        clean = _sanitize_reason(reason)
        if not clean:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Report reason is required"
            )
        # Naive UTC to match the column's SQLAlchemy type (avoids an asyncpg
        # naive/aware cast mismatch on the rate-limit comparison).
        since = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
        recent = (
            await self._db.execute(
                select(func.count())
                .select_from(AgentReport)
                .where(
                    AgentReport.agent_id == agent_id,
                    AgentReport.reporter_id == reporter_id,
                    AgentReport.created_at >= since,
                )
            )
        ).scalar_one()
        if recent >= MAX_REPORTS_PER_AGENT_PER_DAY:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="You've reached the daily report limit for this agent.",
            )

        report = AgentReport(
            agent_id=agent_id,
            workspace_id=workspace_id,
            reporter_id=reporter_id,
            reason=clean,
        )
        self._db.add(report)
        agent.report_count += 1
        await self._db.commit()
        await self._db.refresh(report)
        background_tasks.add_task(
            self._audit.record,
            actor_user_id=reporter_id,
            actor_workspace_id=workspace_id,
            action=Action.AGENT_REPORT,
            resource_type=ResourceType.AGENT,
            resource_id=agent_id,
            outcome=Outcome.SUCCESS,
        )
        return report

    async def get_trending(self, *, calling_workspace_id: uuid.UUID) -> dict[str, object] | None:
        """The single most-liked Published agent, or None when the Bazaar is empty.

        O(log n) via ix_agents_trending (status, liked_count DESC).
        """
        agent = (
            await self._db.execute(
                select(Agent)
                .where(Agent.status == "published")
                .order_by(Agent.liked_count.desc(), Agent.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not agent:
            return None
        hired_ids = await self._hires.hired_agent_ids(calling_workspace_id)
        return self._agent_to_dict(agent, hired_ids)

    # ------------------------------------------------------------------
    # Indexing (called by AgentService on publish/archive)
    # ------------------------------------------------------------------

    async def index_agent(self, agent: Agent) -> None:
        """Upsert agent into Meilisearch. Called as background task on publish. O(s)."""
        category_ids = await self._categories.get_agent_category_ids(agent.id)
        doc: dict[str, object] = {
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "tags": agent.tags,
            "status": agent.status,
            "language": agent.language,
            "classification": agent.classification,
            "category_ids": [str(c) for c in category_ids],
            "updated_at": agent.updated_at.isoformat(),
        }
        await self._search.index_agent(doc)

    async def deindex_agent(self, agent_id: uuid.UUID) -> None:
        """Remove agent from Meilisearch. Called as background task on archive. O(1)."""
        await self._search.delete_agent(str(agent_id))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _agent_to_dict(self, agent: Agent, hired_ids: set[uuid.UUID]) -> dict[str, object]:
        return {
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "tags": agent.tags,
            "status": agent.status,
            "language": agent.language,
            "classification": agent.classification,
            "kind": agent.kind,
            "liked_count": agent.liked_count,
            "disliked_count": agent.disliked_count,
            "owner_id": str(agent.owner_id),
            "workspace_id": str(agent.workspace_id),
            "version": agent.version,
            "created_at": agent.created_at.isoformat(),
            "updated_at": agent.updated_at.isoformat(),
            "hired": agent.id in hired_ids,
            # Sanitized "what you'll need" — provider TYPES only, never the
            # publisher's targets/docs (see _requirements_from_config).
            "requirements": _requirements_from_config(
                agent.config if agent.kind == "workflow" else None
            ),
        }


def _requirements_from_config(config: dict | None) -> dict[str, object] | None:  # type: ignore[type-arg]
    """Sanitized, target-free requirements derived from publisher_config.

    WHITELIST extraction — never copies destination dicts (they carry the
    publisher's `to`/`channel_id`/`spreadsheet_id`/`space`…), `kb_id`, or doc
    filenames. This is the ONLY window the Bazaar opens onto a workflow
    agent's config. O(destinations)."""
    if not config:
        return None
    from sila.services.connectors.manifest import spec_for as connector_spec_for

    destinations = [d for d in (config.get("destinations") or []) if isinstance(d, dict)]
    requires: list[str] = []
    needs_email = False
    for dest in destinations:
        dest_type = str(dest.get("type") or "")
        spec = connector_spec_for(dest_type)
        if spec is not None and spec.auth_type == "oauth":
            if dest_type not in requires:
                requires.append(dest_type)
        elif dest_type == "email":
            needs_email = True  # platform SMTP — just needs a recipient
    schedule = config.get("schedule") if isinstance(config.get("schedule"), dict) else None
    return {
        "data_mode": "included" if config.get("share_docs") else "own_docs",
        "requires": sorted(requires),
        "needs_email_recipient": needs_email,
        "schedule": (
            {"cron": schedule.get("cron"), "timezone": schedule.get("timezone")}
            if schedule
            else None
        ),
        "content": config.get("content"),
        "docs_count": len(config.get("docs") or []),
        "language": config.get("language"),
    }
