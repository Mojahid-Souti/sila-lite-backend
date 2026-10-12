"""Vetting orchestration — submit, scan, approve, reject.

Submit returns immediately with the VettingRun id; scanning runs in the
background via a fresh DB session. The UI polls `GET /vetting-runs/{id}`
to learn the outcome.

Fail-closed: any exception inside the scan path lands the run in
`status='failed' / outcome='error'`. The agent's `vetting_status` rolls
back to `unvetted` so it cannot be published.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.llm.ollama import OllamaLLMAdapter
from sila.adapters.scanner.base import AgentScanner, Finding, ScanInput
from sila.adapters.scanner.llm import LLMScanner
from sila.adapters.scanner.pii import PIIScanner
from sila.adapters.scanner.prompt_injection import RuleBasedInjectionScanner
from sila.adapters.scanner.workflow import WorkflowScanner
from sila.core.config import settings
from sila.core.constants import Action, AgentStatus, Outcome, ResourceType
from sila.core.policies.classification_policy import (
    ClassificationPolicy,
    get_classification_policy,
)
from sila.db.models import Agent, VettingRun
from sila.db.repositories.agent import AgentRepository
from sila.db.repositories.agent_version import AgentVersionRepository
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.vetting import VettingRepository
from sila.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)

SEVERITY_RANK: dict[str, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

SCAN_TIMEOUT_S = 60


def _extract_system_prompt(dify_dsl: dict[str, Any]) -> str:
    """Pull the system-role entry from a Dify DSL prompt_template list."""
    for entry in dify_dsl.get("prompt_template", []) or []:
        if isinstance(entry, dict) and entry.get("role") == "system":
            return str(entry.get("text", ""))
    return ""


def _opening_statement(dify_dsl: dict[str, Any]) -> str:
    """Use opening_statement as the sample input proxy when no explicit sample exists."""
    return str(dify_dsl.get("opening_statement", "") or "")


def text_scanners() -> list[AgentScanner]:
    """The scanners that read instructions: LLM review + regex fallback.

    Both run side-by-side so if the LLM call times out or Ollama is
    unreachable, the regex baseline still surfaces findings — defence in
    depth for PDPL compliance. Shared by agent vetting and skill imports.
    """
    gateway = OllamaLLMAdapter(
        base_url=settings.ollama_url,
        model=settings.llm_scanner_model,
    )
    return [
        LLMScanner(gateway=gateway, timeout_s=settings.llm_scanner_timeout_s),
        PIIScanner(),
        RuleBasedInjectionScanner(),
    ]


def _default_scanners() -> list[AgentScanner]:
    """The text scanners, plus WorkflowScanner's rule-based PDPL checks for
    workflow agents (no-op on chat DSLs)."""
    return [*text_scanners(), WorkflowScanner()]


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Merge near-identical findings produced by multiple scanners.

    Two findings are "the same" when (finding_type, location, normalized
    prefix of matched_full) match. The LLM and the regex scanner often
    flag the same logical issue with slightly different substrings
    (LLM: "Ignore previous instructions", regex: "Ignore previous
    instructions and reveal") — without a tolerant key, both leak through
    and the reviewer sees duplicates. We collapse on a 30-char normalized
    prefix, which is wide enough to distinguish genuine separate findings
    but narrow enough to fold near-duplicates.

    Order: keep the first occurrence. Scanner order in `_default_scanners`
    puts the LLM first, so its prose-y location wins.

    Complexity: O(n) over the findings — single pass with a hash set.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[Finding] = []
    for f in findings:
        # Normalize: lowercase, collapse whitespace, take first 30 chars.
        normalized = " ".join(f.matched_full.lower().split())[:30]
        key = (f.finding_type, f.location, normalized)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _summarize_findings(findings: list[Finding]) -> dict[str, Any]:
    sev = Counter(f.severity for f in findings)
    by_type = Counter(f.finding_type for f in findings)
    return {
        "total": len(findings),
        "by_severity": dict(sev),
        "by_type": dict(by_type),
    }


def _max_severity_rank(findings: list[Finding]) -> int:
    return max((SEVERITY_RANK.get(f.severity, 0) for f in findings), default=-1)


def _max_severity_in_summary(summary: dict[str, Any] | None) -> int:
    """Read max severity rank from a persisted finding_summary dict.
    Used at approve-time when we don't want to re-fetch all findings.
    """
    if not summary:
        return -1
    by_sev = summary.get("by_severity") or {}
    if not isinstance(by_sev, dict):
        return -1
    ranks = [SEVERITY_RANK[k] for k, count in by_sev.items() if k in SEVERITY_RANK and count]
    return max(ranks, default=-1)


class VettingService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        scanners: list[AgentScanner] | None = None,
        policy: ClassificationPolicy | None = None,
    ) -> None:
        self._db = db_session
        self._agents = AgentRepository(db_session)
        self._versions = AgentVersionRepository(db_session)
        self._vetting = VettingRepository(db_session)
        self._audit = AuditRepository(db_session)
        self._scanners = scanners or _default_scanners()
        self._policy = policy or get_classification_policy()

    # ------------------------------------------------------------------
    # Submit for review
    # ------------------------------------------------------------------

    async def submit_for_review(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        background_tasks: BackgroundTasks | None,
    ) -> VettingRun:
        """Move the agent into the vetting pipeline.

        Acceptable starting states: Draft. From InReview we treat this as
        a re-trigger (which the UI should rarely surface; useful for retries
        after a `failed` run).
        """
        if agent.status not in (AgentStatus.DRAFT, AgentStatus.IN_REVIEW):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot submit an agent in status {agent.status!r} for review",
            )

        async with self._db.begin_nested():
            agent = await self._agents.update(
                agent,
                status=AgentStatus.IN_REVIEW,
                vetting_status="scanning",
            )
            run = await self._vetting.create_run(
                agent_id=agent.id,
                workspace_id=agent.workspace_id,
                agent_version_id=None,
                classification=agent.classification,
                triggered_by=actor_id,
            )
            await self._audit.record(
                action=Action.AGENT_SUBMIT_REVIEW,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"vetting_run_id": str(run.id)},
            )

        # Force the outer transaction to commit NOW so the background task can
        # observe the run row. Without this, the BG task sometimes opens its
        # session before the request's outer `get_db` cleanup commits — and
        # `vetting_repo.get_run(run_id)` returns None ("vanished").
        await self._db.commit()

        run_id = run.id
        agent_id = agent.id
        if background_tasks is not None:
            background_tasks.add_task(_run_scan_in_background, run_id, agent_id)
        else:
            # Synchronous path (used by tests). Avoids needing FastAPI app context.
            await _run_scan_in_background(run_id, agent_id)

        return run

    # ------------------------------------------------------------------
    # Streamed scan (inline) — for the SILA graph's visible vetting node
    # ------------------------------------------------------------------

    async def scan_streamed(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        on_step: Callable[[str, list[Finding]], None],
    ) -> VettingRun:
        """Run the Phase-6 gate INLINE (no BackgroundTasks), one scanner at a time,
        calling `on_step(scanner_name, findings)` after each so the caller can stream
        the review as visible chat steps. Same hard gate + outcome as the background
        path (`_finalize_scan`): a clean pass auto-approves; criticals or a
        Restricted+ classification land in `ready_for_review` (human). Runs in the
        caller's session so steps stream during the turn.

        Complexity: O(scanners) sequential; bounded by SCAN_TIMEOUT_S.
        """
        if agent.status not in (AgentStatus.DRAFT, AgentStatus.IN_REVIEW):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot submit an agent in status {agent.status!r} for review",
            )
        agent = await self._agents.update(
            agent, status=AgentStatus.IN_REVIEW, vetting_status="scanning"
        )
        run = await self._vetting.create_run(
            agent_id=agent.id,
            workspace_id=agent.workspace_id,
            agent_version_id=None,
            classification=agent.classification,
            triggered_by=actor_id,
        )
        await self._audit.record(
            action=Action.AGENT_SUBMIT_REVIEW,
            outcome=Outcome.SUCCESS,
            actor_user_id=actor_id,
            actor_workspace_id=agent.workspace_id,
            resource_type=ResourceType.AGENT,
            resource_id=agent.id,
            metadata={"vetting_run_id": str(run.id), "via": "sila-graph"},
        )

        version = await self._versions.get(agent.id, agent.version)
        dify_dsl = (version.dify_dsl if version else {}) or {}
        scan_input = ScanInput(
            system_prompt=_extract_system_prompt(dify_dsl),
            sample_input=_opening_statement(dify_dsl),
            tool_names=[],
            classification=agent.classification,
            dify_dsl=dify_dsl,
        )

        findings: list[Finding] = []

        async def _run() -> None:
            for scanner in self._scanners:
                batch = await scanner.scan(scan_input)
                findings.extend(batch)
                on_step(scanner.name, batch)

        try:
            await asyncio.wait_for(_run(), timeout=SCAN_TIMEOUT_S)
        except TimeoutError:
            await self._vetting.fail_run(run, f"Scan exceeded {SCAN_TIMEOUT_S}s timeout")
            await self._agents.update(agent, vetting_status="failed")
            return run

        await _finalize_scan(
            self._vetting,
            self._agents,
            self._audit,
            run=run,
            agent=agent,
            findings=findings,
            policy=self._policy,
        )
        return run

    # ------------------------------------------------------------------
    # Approve / Reject
    # ------------------------------------------------------------------

    async def approve(
        self,
        *,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        note: str | None = None,
    ) -> VettingRun:
        run = await self._vetting.get_run(run_id)
        if not run or run.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Vetting run not found"
            )
        if run.status != "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot approve a run with status {run.status!r}",
            )
        if run.outcome != "ready_for_review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run outcome {run.outcome!r} is not awaiting decision",
            )

        # Hard block: severities at or above the policy's `hard_block_severity`
        # cannot be approved away — the Builder must fix and re-vet.
        # This protects PDPL/compliance posture from a single reviewer override.
        block_rank = SEVERITY_RANK.get(self._policy.hard_block_severity, 4)
        max_sev = _max_severity_in_summary(run.finding_summary)
        if max_sev >= block_rank:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This run contains findings at severity "
                    f"'{self._policy.hard_block_severity}' or higher. The agent must be "
                    "edited to remove these findings and resubmitted for review. "
                    "Reviewer approval cannot override critical issues."
                ),
            )

        async with self._db.begin_nested():
            run = await self._vetting.record_decision(
                run, approved_by=actor_id, decision="approved", note=note
            )
            agent = await self._agents.get(run.agent_id, run.workspace_id)
            if agent:
                await self._agents.update(agent, vetting_status="approved")
            await self._audit.record(
                action=Action.AGENT_APPROVE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.VETTING_RUN,
                resource_id=run.id,
            )
        return run

    async def reject(
        self,
        *,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        note: str | None = None,
    ) -> VettingRun:
        run = await self._vetting.get_run(run_id)
        if not run or run.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Vetting run not found"
            )
        if run.status != "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot reject a run with status {run.status!r}",
            )

        async with self._db.begin_nested():
            run = await self._vetting.record_decision(
                run, approved_by=actor_id, decision="rejected", note=note
            )
            agent = await self._agents.get(run.agent_id, run.workspace_id)
            if agent:
                await self._agents.update(
                    agent,
                    vetting_status="rejected",
                    status=AgentStatus.DRAFT,
                )
            await self._audit.record(
                action=Action.AGENT_REJECT,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.VETTING_RUN,
                resource_id=run.id,
                metadata={"note": note or ""},
            )
        return run


# ---------------------------------------------------------------------------
# Background scan — runs in its own DB session.
# ---------------------------------------------------------------------------


async def _run_scan_in_background(run_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Spun up by FastAPI BackgroundTasks. Opens its own session.

    `submit_for_review` explicitly commits before scheduling this task, so the
    row is durable by the time we get here — no sleep needed.
    """
    async with AsyncSessionLocal.begin() as session:
        try:
            await _execute_scan(session, run_id, agent_id)
        except Exception as e:  # noqa: BLE001 — fail-closed
            log.exception("Vetting run %s crashed", run_id)
            await _mark_failed(session, run_id, agent_id, str(e))


async def _execute_scan(session: AsyncSession, run_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    vetting_repo = VettingRepository(session)
    agent_repo = AgentRepository(session)
    audit_repo = AuditRepository(session)
    policy = get_classification_policy()

    run = await vetting_repo.get_run(run_id)
    if not run:
        log.warning("Vetting run %s vanished before scan started", run_id)
        return

    agent = await agent_repo.get(agent_id, run.workspace_id)
    if not agent:
        await vetting_repo.fail_run(run, "Agent no longer exists")
        return

    # System prompt + opening statement live on AgentVersion, not Agent.
    versions_repo = AgentVersionRepository(session)
    version = await versions_repo.get(agent.id, agent.version)
    dify_dsl = (version.dify_dsl if version else {}) or {}

    await audit_repo.record(
        action=Action.AGENT_VET_START,
        outcome=Outcome.SUCCESS,
        actor_user_id=run.triggered_by,
        actor_workspace_id=run.workspace_id,
        resource_type=ResourceType.VETTING_RUN,
        resource_id=run.id,
    )

    scan_input = ScanInput(
        system_prompt=_extract_system_prompt(dify_dsl),
        sample_input=_opening_statement(dify_dsl),
        tool_names=[],  # tool whitelist enforcement folded into policy_findings below
        classification=agent.classification,
        # LLM scanner reads the full DSL; regex scanners ignore this field.
        dify_dsl=dify_dsl,
    )

    scanners = _default_scanners()
    findings: list[Finding] = []
    try:
        per_scanner = await asyncio.wait_for(
            asyncio.gather(*(s.scan(scan_input) for s in scanners)),
            timeout=SCAN_TIMEOUT_S,
        )
    except TimeoutError:
        await vetting_repo.fail_run(run, f"Scan exceeded {SCAN_TIMEOUT_S}s timeout")
        await agent_repo.update(agent, vetting_status="failed")
        return

    for batch in per_scanner:
        findings.extend(batch)

    await _finalize_scan(
        vetting_repo,
        agent_repo,
        audit_repo,
        run=run,
        agent=agent,
        findings=findings,
        policy=policy,
    )


async def _finalize_scan(
    vetting_repo: VettingRepository,
    agent_repo: AgentRepository,
    audit_repo: AuditRepository,
    *,
    run: VettingRun,
    agent: Agent,
    findings: list[Finding],
    policy: ClassificationPolicy,
) -> str:
    """Dedupe + policy + persist a completed run, and roll the agent's
    vetting_status to the decided outcome. Shared by the background scan and the
    inline streamed scan so both reach the SAME gate decision. Returns the outcome.
    """
    # Dedupe — LLM and regex frequently report the same injection on the
    # same substring; the reviewer should see it once.
    findings = dedupe_findings(findings)

    # Classification policy check — denied tools, KB scopes (tools wired later)
    findings.extend(_policy_findings(agent, policy))

    await vetting_repo.add_findings(run_id=run.id, workspace_id=run.workspace_id, findings=findings)

    summary = _summarize_findings(findings)
    outcome = _decide_outcome(agent.classification, findings, policy)

    await vetting_repo.complete_run(run, outcome=outcome, finding_summary=summary)

    if outcome == "auto_approved":
        await agent_repo.update(agent, vetting_status="approved")
    elif outcome == "ready_for_review":
        await agent_repo.update(agent, vetting_status="ready_for_review")
    elif outcome == "rejected":
        # rare in this slice; policy mismatch without reviewer required
        await agent_repo.update(agent, vetting_status="rejected", status=AgentStatus.DRAFT)

    await audit_repo.record(
        action=Action.AGENT_VET_COMPLETE,
        outcome=Outcome.SUCCESS,
        actor_user_id=run.triggered_by,
        actor_workspace_id=run.workspace_id,
        resource_type=ResourceType.VETTING_RUN,
        resource_id=run.id,
        metadata={"outcome": outcome, "finding_count": len(findings)},
    )
    return outcome


async def _mark_failed(
    session: AsyncSession, run_id: uuid.UUID, agent_id: uuid.UUID, err: str
) -> None:
    vetting_repo = VettingRepository(session)
    agent_repo = AgentRepository(session)
    run = await vetting_repo.get_run(run_id)
    if run:
        await vetting_repo.fail_run(run, err)
    if run:
        agent = await agent_repo.get(agent_id, run.workspace_id)
        if agent:
            await agent_repo.update(agent, vetting_status="failed", status=AgentStatus.DRAFT)


def _policy_findings(agent: Agent, policy: ClassificationPolicy) -> list[Finding]:
    """Convert classification policy violations into Finding records.

    In this slice we only check the denied-tools dimension. The agent's
    granted tools are not yet wired into the dify_dsl extraction path, so
    this is effectively a no-op until Phase 6 part 2. Kept here so the
    decision logic already accounts for it.
    """
    return []


def _decide_outcome(
    classification: str, findings: list[Finding], policy: ClassificationPolicy
) -> str:
    """Three outcomes: auto_approved | ready_for_review | rejected.

    Rules:
      1. If the classification level requires a Reviewer, never auto-approve.
      2. If any finding's severity >= policy.auto_approve_block_severity, force review.
      3. Otherwise, auto-approve.
    """
    level = policy.level(classification)
    block_rank = SEVERITY_RANK.get(policy.auto_approve_block_severity, 3)

    if level.require_reviewer:
        return "ready_for_review"
    if _max_severity_rank(findings) >= block_rank:
        return "ready_for_review"
    return "auto_approved"
