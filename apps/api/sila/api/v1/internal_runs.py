"""Internal agent-run endpoint.

A built n8n workflow calls this with a **run capability token** (not a JWT) to have
Sila perform the agent's RAG: KB retrieval (pgvector) + LLM summary (local Ollama).
n8n holds no secret and does no LLM — Sila is the brain (sovereignty / PDPL). The
capability is authoritative for the workspace + KB; the body only carries the query
and the instruction, never widening scope.

Security: capability-gated (HMAC, fail-closed on tamper/rotate); the service role
runs the search scoped to the token's (workspace, kb); no user-supplied URL.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.constants import Action, Outcome, ResourceType
from sila.core.email import send_email
from sila.db.repositories.audit import AuditRepository
from sila.db.session import get_db
from sila.services.sila import email_capability, run_capability
from sila.services.sila.preview import generate_sample

router = APIRouter(tags=["internal"])


class RunRequest(BaseModel):
    query: str = Field(default="overview key points policies", max_length=500)
    instruction: str = Field(
        default="Summarize the content into 3-5 concise, professional bullet points.",
        max_length=1000,
    )


class RunResponse(BaseModel):
    summary: str


@router.post("/internal/agents/run", response_model=RunResponse)
async def internal_agent_run(
    body: RunRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_sila_run_token: Annotated[str | None, Header()] = None,
) -> RunResponse:
    cap = run_capability.verify(x_sila_run_token or "")
    if cap is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid capability")

    # Retrieve + generate (shared with the SILA build preview) — scoped by the
    # capability (no JWT, service role); this endpoint then posts, the preview doesn't.
    try:
        summary = await generate_sample(
            db=db,
            workspace_id=cap.workspace_id,
            kb_id=cap.kb_id,
            instruction=body.instruction,
            model=cap.model,
            query=body.query,
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Generation failed: {exc}") from exc
    if not summary:
        return RunResponse(summary="Nothing in the knowledge base to summarize yet.")
    return RunResponse(summary=summary)


class SendEmailRequest(BaseModel):
    subject: str = Field(default="Your summary", max_length=300)
    body: str = Field(default="", max_length=20000)  # the generated summary ($json.summary)


class SendEmailResponse(BaseModel):
    sent: bool


@router.post("/internal/agents/send-email", response_model=SendEmailResponse)
async def internal_send_email(
    body: SendEmailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_sila_email_token: Annotated[str | None, Header()] = None,
) -> SendEmailResponse:
    """Send the summary as an email via the PLATFORM SMTP server (sovereign — no
    per-user OAuth, no cloud). Capability-gated: the recipient is fixed by the token,
    so the workflow body cannot widen it to another address. n8n holds no SMTP creds."""
    cap = email_capability.verify(x_sila_email_token or "")
    if cap is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid capability")
    try:
        await send_email(to=cap.to, subject=body.subject, body=body.body)
    except OSError as exc:  # SMTP transport failure
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Email send failed: {exc}") from exc
    # Audit — metadata only (NEVER the body or recipient PII beyond the domain).
    domain = cap.to.rsplit("@", 1)[-1] if "@" in cap.to else "?"
    await AuditRepository(db).record(
        Action.EMAIL_SEND,
        Outcome.SUCCESS,
        actor_user_id=cap.user_id,
        actor_workspace_id=cap.workspace_id,
        resource_type=ResourceType.WORKSPACE,
        resource_id=cap.workspace_id,
        metadata={"to_domain": domain},
    )
    return SendEmailResponse(sent=True)
