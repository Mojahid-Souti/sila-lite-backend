"""Open-source skills: import, vet, approve, publish, list (Phase 18).

The lifecycle, all audited:

  import   (Admin)     upstream SKILL.md -> parsed -> scanned -> needs_review
                       or rejected. Never published by the import itself. A
                       skill the catalog snapshot does not list is imported by
                       its GitHub reference (owner/repo + skill folder).
  approve  (Reviewer+) needs_review -> approved + published.
  rescan   (Reviewer+) scan the stored text again -> needs_review or rejected,
                       unpublished until approved again.
  reject   (Reviewer+) -> rejected (and unpublished).
  publish / unpublish (Reviewer+) — only an approved skill can be published.

A skill's text is untrusted: parsed with yaml.safe_load, scanned by the same
text scanners as an agent (local LLM review, PII, rule-based injection), and a
scan that fails is a finding, not a pass (fail-closed). A re-import with new
content re-vets and unpublishes until approved again; the same content is a
no-op (idempotent on the instructions' hash).

Complexity: an import is O(SKILL.md size) plus one scan; listings are indexed
(see db/repositories/skills.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.scanner.base import AgentScanner, Finding, ScanInput
from sila.adapters.skills.base import (
    SKILL_KEY_PATTERN,
    SOURCE_PATTERN,
    CatalogEntry,
    SkillSource,
    SkillSourceError,
)
from sila.core.config import settings
from sila.core.constants import Action, Outcome, ResourceType
from sila.core.utils.timestamps import utc_iso
from sila.db.models import Skill
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.skills import SkillFilters, SkillRepository
from sila.services.notification_service import NotificationKind, notify
from sila.services.skills.categories import CATEGORY_KEYS, categorize
from sila.services.skills.parse import ParsedSkill, SkillParseError, parse_skill_md

log = structlog.get_logger(__name__)

NEEDS_REVIEW, APPROVED, REJECTED = "needs_review", "approved", "rejected"
# A finding at or above this severity rejects the import outright.
_REJECT_RANK = 3
_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_SCAN_INCOMPLETE = "scan.incomplete"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SkillService:
    def __init__(
        self,
        db: AsyncSession,
        source: SkillSource,
        scanners: list[AgentScanner] | None = None,
    ) -> None:
        self._db = db
        self._source = source
        self._scanners = scanners
        self._repo = SkillRepository(db)
        self._audit = AuditRepository(db)

    # --- the upstream catalog (Admin's import screen) ------------------------

    async def search_catalog(self, query: str, *, page: int, size: int) -> dict[str, Any]:
        try:
            entries, total = await self._source.search(query, page=page, size=size)
        except SkillSourceError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Skill catalog: {exc}") from exc
        imported = await self._repo.imported_keys([(e.source, e.skill_key) for e in entries])
        return {
            "items": [catalog_payload(e, imported.get((e.source, e.skill_key))) for e in entries],
            "total": total,
            "page": page,
            "size": size,
        }

    # --- import ----------------------------------------------------------------

    async def import_skill(
        self, *, workspace_id: uuid.UUID, user_id: uuid.UUID, source: str, skill_key: str
    ) -> Skill:
        """Fetch, parse and vet one upstream skill into this workspace."""
        if not SOURCE_PATTERN.fullmatch(source) or not SKILL_KEY_PATTERN.fullmatch(skill_key):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Not a valid skill reference")
        await self._check_rate(user_id)
        existing = await self._repo.get_by_upstream(source, skill_key)
        if existing is not None and existing.workspace_id != workspace_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This skill is already in the Bazaar from another workspace",
            )
        entry, parsed = await self._fetch(source, skill_key)
        digest = hashlib.sha256(parsed.instructions.encode()).hexdigest()
        if existing is not None and existing.body_sha256 == digest and not _incomplete(existing):
            return existing  # same content, already vetted: nothing to do

        findings = await self._scan(parsed)
        verdict = REJECTED if _max_rank(findings) >= _REJECT_RANK else NEEDS_REVIEW
        skill = existing or Skill(workspace_id=workspace_id, source=source, skill_key=skill_key)
        _fill(skill, entry, parsed, digest)
        skill.vetting_status, skill.findings, skill.published = verdict, findings, False
        skill.imported_by, skill.imported_at = user_id, _now()
        skill.reviewed_by, skill.reviewed_at = None, None
        if existing is None:
            self._repo.add(skill)
        await self._db.flush()
        await self._record(Action.SKILL_IMPORT, skill, user_id, workspace_id, verdict=verdict)
        await notify(
            self._db,
            user_id=user_id,
            workspace_id=workspace_id,
            kind=NotificationKind.SKILL_IMPORTED,
            title=f"Skill imported: {skill.name}",
            body=(
                "Rejected by the security scan — it can't be published."
                if verdict == REJECTED
                else "Scanned and waiting for a Reviewer's approval."
            ),
            link="/bazaar?tab=skills",
        )
        return skill

    # --- decisions -------------------------------------------------------------

    async def approve(
        self, workspace_id: uuid.UUID, skill_id: uuid.UUID, reviewer: uuid.UUID
    ) -> Skill:
        skill = await self._own(workspace_id, skill_id)
        if skill.vetting_status != NEEDS_REVIEW:
            raise HTTPException(status.HTTP_409_CONFLICT, f"The skill is {skill.vetting_status}")
        skill.vetting_status, skill.published = APPROVED, True
        skill.reviewed_by, skill.reviewed_at = reviewer, _now()
        await self._record(Action.SKILL_APPROVE, skill, reviewer, workspace_id)
        await notify(
            self._db,
            user_id=skill.imported_by,
            workspace_id=workspace_id,
            kind=NotificationKind.SKILL_PUBLISHED,
            title=f"Skill published: {skill.name}",
            body="Approved and now in the Bazaar.",
            link=f"/bazaar?tab=skills&skill={skill.id}",
        )
        return skill

    async def reject(
        self, workspace_id: uuid.UUID, skill_id: uuid.UUID, reviewer: uuid.UUID
    ) -> Skill:
        skill = await self._own(workspace_id, skill_id)
        skill.vetting_status, skill.published = REJECTED, False
        skill.reviewed_by, skill.reviewed_at = reviewer, _now()
        await self._record(Action.SKILL_REJECT, skill, reviewer, workspace_id)
        return skill

    async def rescan(
        self, workspace_id: uuid.UUID, skill_id: uuid.UUID, reviewer: uuid.UUID
    ) -> Skill:
        """Scan the stored instructions again (after a scanner change, or a scan
        that could not finish). The skill leaves the Bazaar until approved again."""
        skill = await self._own(workspace_id, skill_id)
        parsed = ParsedSkill(
            name=skill.name, description=skill.description, instructions=skill.body
        )
        findings = await self._scan(parsed)
        skill.findings = findings
        skill.vetting_status = REJECTED if _max_rank(findings) >= _REJECT_RANK else NEEDS_REVIEW
        skill.published = False
        skill.reviewed_by, skill.reviewed_at = reviewer, _now()
        await self._record(
            Action.SKILL_RESCAN, skill, reviewer, workspace_id, verdict=skill.vetting_status
        )
        return skill

    async def remove(self, workspace_id: uuid.UUID, skill_id: uuid.UUID, actor: uuid.UUID) -> None:
        """Take a skill out of Sila: gone from the Bazaar, the review list and any
        chat that had it (a chat loads published rows only). Its import history
        stays in the audit log; importing it again re-fetches and re-vets it."""
        skill = await self._own(workspace_id, skill_id)
        await self._record(Action.SKILL_REMOVE, skill, actor, workspace_id, name=skill.name)
        await self._repo.delete(skill)

    async def set_published(
        self, workspace_id: uuid.UUID, skill_id: uuid.UUID, actor: uuid.UUID, *, published: bool
    ) -> Skill:
        skill = await self._own(workspace_id, skill_id)
        if published and skill.vetting_status != APPROVED:
            raise HTTPException(status.HTTP_409_CONFLICT, "Only an approved skill can be published")
        skill.published = published
        action = Action.SKILL_PUBLISH if published else Action.SKILL_UNPUBLISH
        await self._record(action, skill, actor, workspace_id)
        return skill

    # --- reads -----------------------------------------------------------------

    async def list_published(
        self, filters: SkillFilters, *, page: int, size: int
    ) -> dict[str, Any]:
        if filters.category is not None and filters.category not in CATEGORY_KEYS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown category")
        skills, total = await self._repo.list_published(filters, page=page, size=size)
        return {
            "items": [skill_payload(s) for s in skills],
            "total": total,
            "page": page,
            "size": size,
            "categories": await self._repo.category_counts(),
        }

    async def get_visible(self, skill_id: uuid.UUID, member_of: set[uuid.UUID]) -> Skill:
        """A published skill, or one of the caller's own workspace's imports."""
        skill = await self._repo.get(skill_id)
        if skill is None or not (skill.published or skill.workspace_id in member_of):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
        return skill

    async def list_workspace(self, workspace_id: uuid.UUID, *, page: int, size: int) -> list[Skill]:
        return await self._repo.list_workspace(workspace_id, page=page, size=size)

    # --- internals -------------------------------------------------------------

    async def _check_rate(self, user_id: uuid.UUID) -> None:
        since = _now() - timedelta(hours=1)
        if await self._repo.imports_since(user_id, since) >= settings.skills_imports_per_hour:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"You can import {settings.skills_imports_per_hour} skills an hour; "
                "try again later.",
            )

    async def _fetch(self, source: str, skill_key: str) -> tuple[CatalogEntry, ParsedSkill]:
        try:
            # The public catalog is a snapshot (~6k of the 100k+ skills indexed
            # upstream). A skill it does not list is imported by its reference —
            # the same allow-listed fetch, parse and scan; only the catalog's
            # install count and dependency list are unknown.
            entry = await self._source.entry(source, skill_key) or CatalogEntry(
                source=source,
                skill_key=skill_key,
                name=skill_key,
                description="",
                repo_url=f"https://github.com/{source}",
            )
            text = await self._source.fetch_skill_md(entry)
        except SkillSourceError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Could not fetch the skill: {exc}"
            ) from exc
        try:
            return entry, parse_skill_md(text)
        except SkillParseError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"SKILL.md: {exc}") from exc

    async def _scan(self, parsed: ParsedSkill) -> list[dict[str, Any]]:
        """Every scanner over the skill's text; a scanner that fails is a finding."""
        from sila.services.vetting_service import dedupe_findings

        scanners = self._scanners if self._scanners is not None else skill_scanners()
        scan_input = ScanInput(
            system_prompt=f"{parsed.name}\n{parsed.description}\n\n{parsed.instructions}"
        )
        results = await asyncio.gather(
            *(scanner.scan(scan_input) for scanner in scanners), return_exceptions=True
        )
        findings: list[Finding] = []
        for scanner, result in zip(scanners, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("skills.scanner_failed", scanner=scanner.name, error=str(result))
                findings.append(
                    Finding(
                        finding_type=_SCAN_INCOMPLETE,
                        severity="high",
                        location="skill",
                        matched_full="",
                        matched_preview=f"The {scanner.name} scan could not run; import it again.",
                    )
                )
            else:
                findings.extend(result)
        return [
            {"type": f.finding_type, "severity": f.severity, "preview": f.matched_preview[:300]}
            for f in dedupe_findings(findings)
        ]

    async def _own(self, workspace_id: uuid.UUID, skill_id: uuid.UUID) -> Skill:
        skill = await self._repo.get(skill_id)
        if skill is None or skill.workspace_id != workspace_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
        return skill

    async def _record(
        self, action: Action, skill: Skill, actor: uuid.UUID, workspace_id: uuid.UUID, **extra: str
    ) -> None:
        await self._audit.record(
            action=action,
            outcome=Outcome.SUCCESS,
            actor_user_id=actor,
            actor_workspace_id=workspace_id,
            resource_type=ResourceType.SKILL,
            resource_id=skill.id,
            metadata={"source": skill.source, "skill": skill.skill_key, **extra},
        )


def skill_scanners() -> list[AgentScanner]:
    """What reads a skill: the local model's skill review (fail-closed, whole
    text, quotes verified) beside the agents' PII and rule-based injection
    scanners, which run over the full text regardless of the model."""
    from sila.adapters.llm.ollama import OllamaLLMAdapter
    from sila.adapters.scanner.pii import PIIScanner
    from sila.adapters.scanner.prompt_injection import RuleBasedInjectionScanner
    from sila.adapters.scanner.skill_review import SkillLLMScanner

    gateway = OllamaLLMAdapter(
        base_url=settings.ollama_url,
        model=settings.llm_scanner_model,
        num_ctx=settings.sila_llm_num_ctx,
    )
    return [
        SkillLLMScanner(gateway, timeout_s=settings.skills_scan_timeout_s),
        PIIScanner(),
        RuleBasedInjectionScanner(),
    ]


def _fill(skill: Skill, entry: CatalogEntry, parsed: ParsedSkill, digest: str) -> None:
    skill.name = parsed.name
    skill.description = parsed.description
    skill.category = categorize(parsed.name, parsed.description)
    skill.repo_url = entry.repo_url
    skill.license = parsed.license or entry.license
    skill.installs = entry.installs
    skill.has_scripts = entry.has_scripts
    skill.dependencies = entry.dependencies
    skill.body = parsed.instructions
    skill.body_sha256 = digest


def _max_rank(findings: list[dict[str, Any]]) -> int:
    return max((_SEVERITY_RANK.get(str(f["severity"]), 0) for f in findings), default=-1)


def _incomplete(skill: Skill) -> bool:
    return any(f.get("type") == _SCAN_INCOMPLETE for f in skill.findings or [])


def skill_payload(skill: Skill, *, with_body: bool = False) -> dict[str, Any]:
    """A skill as the web app shows it. The instructions only on request."""
    payload: dict[str, Any] = {
        "id": str(skill.id),
        "workspace_id": str(skill.workspace_id),
        "source": skill.source,
        "skill_key": skill.skill_key,
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "repo_url": skill.repo_url,
        "license": skill.license,
        "installs": skill.installs,
        "has_scripts": skill.has_scripts,
        "dependencies": skill.dependencies,
        "vetting_status": skill.vetting_status,
        "findings": skill.findings,
        "published": skill.published,
        "imported_at": utc_iso(skill.imported_at) if skill.imported_at else None,
    }
    if with_body:
        payload["instructions"] = skill.body
    return payload


def catalog_payload(entry: CatalogEntry, imported: Skill | None) -> dict[str, Any]:
    return {
        "source": entry.source,
        "skill_key": entry.skill_key,
        "name": entry.name,
        "description": entry.description,
        "repo_url": entry.repo_url,
        "installs": entry.installs,
        "license": entry.license,
        "has_scripts": entry.has_scripts,
        "imported": imported is not None,
        "imported_status": imported.vetting_status if imported else None,
        "imported_id": str(imported.id) if imported else None,
    }
