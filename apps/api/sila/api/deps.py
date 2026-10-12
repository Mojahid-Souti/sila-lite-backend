import uuid
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import AuthService, UserResult
from sila.adapters.auth.supabase import SupabaseAuthAdapter
from sila.adapters.n8n.base import N8nService
from sila.adapters.n8n.rest import N8nRestAdapter
from sila.core.config import settings
from sila.core.constants import Action, Role
from sila.db.repositories.membership import MembershipRepository
from sila.db.session import get_db

_bearer = HTTPBearer()


async def require_ai_enabled() -> None:
    """For endpoints that need the AI model (security scans, document embedding,
    skill review): while the model is off they refuse with the configured
    message instead of starting work that would fail closed half-way."""
    if not settings.sila_ai_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, settings.sila_ai_unavailable_message
        )


def get_auth_service() -> AuthService:
    return SupabaseAuthAdapter(
        base_url=str(settings.sila_supabase_url),
        service_key=settings.sila_supabase_service_key,
    )


def get_n8n_service() -> N8nService:
    return N8nRestAdapter(base_url=settings.n8n_internal_url)


def verify_supabase_jwt(token: str) -> UserResult | None:
    """Verify a Supabase JWT locally (no network round-trip). O(1).

    Returns the user, or None when invalid/expired. Shared by the HTTP
    dependency below and the SILA WebSocket handshake (which can't use
    HTTP Depends).
    """
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
            options={"verify_exp": True},
        )
    except JWTError:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None
    # Supabase access tokens carry user_metadata as a claim, so onboarding state
    # is readable without a network round-trip (Phase 17). O(1).
    meta = payload.get("user_metadata")
    if not isinstance(meta, dict):
        meta = {}
    return UserResult(
        id=uuid.UUID(user_id),
        email=payload.get("email", ""),
        email_confirmed=bool(payload.get("email_confirmed_at")),
        full_name=(meta.get("full_name") or None),
        onboarding_complete=bool(meta.get("onboarding_complete")),
        ux_mode=(meta.get("ux_mode") or None),
        job_role=(meta.get("job_role") or None),
        referral_source=(meta.get("referral_source") or None),
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> UserResult:
    """Verify Supabase JWT locally (no network round-trip). O(1)."""
    user = verify_supabase_jwt(credentials.credentials)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return user


async def get_access_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> str:
    """The raw bearer token — needed for user-scoped GoTrue writes (PUT /user).

    Pairs with get_current_user; FastAPI caches the shared `_bearer` per request,
    so the header is parsed once.
    """
    return credentials.credentials


async def check_opa(action: Action, role: Role) -> bool:
    """Call OPA sidecar for authorization decision. Result is per-request (not cached globally)."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            r = await client.post(
                f"{settings.opa_url}/v1/data/sila/authz/allow",
                json={"input": {"action": action, "role": role}},
            )
            return bool(r.json().get("result", False))
        except Exception:
            return False


def require_workspace_role(min_role: Role):  # type: ignore[no-untyped-def]
    """Dependency factory: resolves caller's membership and enforces min role via OPA."""

    async def _dep(
        workspace_id: uuid.UUID,
        current_user: Annotated[UserResult, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> tuple[UserResult, Role]:
        repo = MembershipRepository(db)
        membership = await repo.get(workspace_id, current_user.id)
        if not membership:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")

        caller_role = Role(membership.role)
        # OPA enforces the minimum role for WORKSPACE_VIEW as a baseline
        allowed = await check_opa(Action.WORKSPACE_VIEW, caller_role)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

        from sila.core.constants import ROLE_RANK

        if ROLE_RANK[caller_role] < ROLE_RANK[min_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

        return current_user, caller_role

    return _dep
