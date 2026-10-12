"""Sila→n8n session bridge endpoint.

POST /v1/n8n/session — caller's Supabase JWT in the Authorization header.
Returns the cookie name + value the frontend must set on the response so the
embedded iframe loads with the user's private n8n session.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.adapters.n8n.base import N8nService
from sila.api.deps import get_current_user, get_n8n_service
from sila.db.session import get_db
from sila.services import n8n_provisioning

router = APIRouter(prefix="/n8n", tags=["n8n"])


class N8nSessionResponse(BaseModel):
    cookie_name: str
    cookie_value: str
    max_age_s: int
    cookie_path: str = "/n8n"


@router.post("/session", response_model=N8nSessionResponse)
async def create_n8n_session(
    current_user: Annotated[UserResult, Depends(get_current_user)],
    n8n: Annotated[N8nService, Depends(get_n8n_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> N8nSessionResponse:
    """Mint a fresh n8n session cookie for the current Sila user.

    The caller is expected to be the Next.js build-page server, which sets
    the returned cookie on its response to the browser before serving the
    iframe shell. The cookie is HttpOnly + scoped to /n8n so it's only sent
    to the n8n proxy path.
    """
    session = await n8n_provisioning.ensure_session(
        db=db,
        n8n=n8n,
        supabase_user_id=current_user.id,
        sila_full_name=None,  # TODO: thread Supabase user_metadata.full_name in
    )
    return N8nSessionResponse(
        cookie_name=session.cookie_name,
        cookie_value=session.cookie_value,
        max_age_s=session.max_age_s,
    )
