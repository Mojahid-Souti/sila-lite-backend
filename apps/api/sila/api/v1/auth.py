import contextlib
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import AuthService, SessionResult, UserResult
from sila.api.deps import get_access_token, get_auth_service, get_current_user
from sila.core.config import settings
from sila.core.constants import (
    Action,
    AgentExperience,
    JobRole,
    Outcome,
    ReferralSource,
    ResourceType,
    UxMode,
    ux_mode_for_experience,
)
from sila.db.repositories.audit import AuditRepository
from sila.db.session import get_db

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class SignUpRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 12:
            raise ValueError("Password must be at least 12 characters")
        return v

    @field_validator("full_name")
    @classmethod
    def full_name_length(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip()
        if not stripped:
            return None
        if not 2 <= len(stripped) <= 60:
            raise ValueError("Full name must be 2–60 characters")
        return stripped


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ResetPasswordRequest(BaseModel):
    email: EmailStr


class OnboardingRequest(BaseModel):
    """Phase 17 onboarding answers. The three selections are fixed enums (Pydantic
    rejects anything off-list). The two `*_other` fields are the only free text:
    each is required iff its enum is OTHER, stripped of non-printable characters,
    and capped at 60 chars — so the surface is a short, sanitized label."""

    referral_source: ReferralSource
    referral_source_other: str | None = None
    job_role: JobRole
    job_role_other: str | None = None
    agent_experience: AgentExperience

    @field_validator("referral_source_other", "job_role_other")
    @classmethod
    def _clean_other(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = "".join(ch for ch in v if ch.isprintable()).strip()
        if not stripped:
            return None
        if len(stripped) > 60:
            raise ValueError("Must be 60 characters or fewer")
        return stripped

    @model_validator(mode="after")
    def _require_other_text(self) -> "OnboardingRequest":
        if self.referral_source is ReferralSource.OTHER and not self.referral_source_other:
            raise ValueError("Tell us where you heard about us")
        if self.job_role is JobRole.OTHER and not self.job_role_other:
            raise ValueError("Tell us your role")
        return self


class UxModeRequest(BaseModel):
    """Phase 17 mode toggle. `ux_mode` is a fixed enum (simple|advanced) — Pydantic
    rejects anything off-list. Mode is a UX preference, never a privilege."""

    ux_mode: UxMode


class ProfileRequest(BaseModel):
    """A user editing their OWN display name.

    Same shape and limits as the signup `full_name`, so a name set here and a
    name set at signup cannot diverge: 2-60 characters, no control characters.
    """

    full_name: str = Field(min_length=2, max_length=60)

    @field_validator("full_name")
    @classmethod
    def _printable(cls, value: str) -> str:
        cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
        if len(cleaned) < 2:
            raise ValueError("Name must be at least 2 characters.")
        return cleaned


class UserOut(BaseModel):
    id: str
    email: str
    email_confirmed: bool
    # The display name lives in GoTrue user_metadata and reaches us as a JWT
    # claim. Omitting it here made a saved name invisible: the write persisted,
    # every read dropped it, and the UI fell back to the email handle.
    full_name: str | None = None
    onboarding_complete: bool = False
    ux_mode: str | None = None


class SessionOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    user: UserOut


def _user_out(u: UserResult) -> UserOut:
    return UserOut(
        id=str(u.id),
        email=u.email,
        email_confirmed=u.email_confirmed,
        full_name=u.full_name,
        onboarding_complete=u.onboarding_complete,
        ux_mode=u.ux_mode,
    )


def _session_out(s: SessionResult) -> SessionOut:
    return SessionOut(
        access_token=s.access_token,
        refresh_token=s.refresh_token,
        token_type=s.token_type,
        expires_in=s.expires_in,
        user=_user_out(s.user),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def signup_domain_allowed(email: str) -> bool:
    """Whether `email` may sign up under SILA_SIGNUP_ALLOWED_DOMAINS (empty = any).
    Exact domain match, case-insensitive: a sub-domain is not the domain."""
    allowed = settings.signup_allowed_domains
    if not allowed:
        return True
    return email.rsplit("@", 1)[-1].strip().lower() in allowed


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignUpRequest,
    background: BackgroundTasks,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> UserOut:
    if not signup_domain_allowed(str(body.email)):
        # Naming the rule is not enumeration: it says nothing about who exists.
        log.info("auth.signup_refused_domain", domain=str(body.email).rsplit("@", 1)[-1])
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sign-up is limited to your organisation's email addresses.",
        )
    try:
        user = await auth.sign_up(body.email, body.password, full_name=body.full_name)
    except Exception as exc:
        # Return identical error shape regardless of reason (no user enumeration)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Signup failed. Check your email and password.",
        ) from exc
    return _user_out(user)


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> SessionOut:
    try:
        session = await auth.sign_in(body.email, body.password)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        ) from exc
    return _session_out(session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    current_user: Annotated[UserResult, Depends(get_current_user)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    # We don't have the raw token here, but GoTrue also expires on JWT exp
    # Full revocation happens via GoTrue's admin endpoint if needed
    pass


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    body: RefreshRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> SessionOut:
    try:
        session = await auth.refresh_session(body.refresh_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from exc
    return _session_out(session)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: ResetPasswordRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    # Always succeed — don't reveal whether the email exists
    with contextlib.suppress(Exception):
        await auth.reset_password(body.email)


@router.get("/me", response_model=UserOut)
async def me(
    current_user: Annotated[UserResult, Depends(get_current_user)],
) -> UserOut:
    return _user_out(current_user)


@router.patch("/onboarding", response_model=UserOut)
async def complete_onboarding(
    body: OnboardingRequest,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    token: Annotated[str, Depends(get_access_token)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    """Persist the onboarding answers to the caller's own user_metadata and set
    the initial UX mode. The mode is computed server-side from the experience
    answer (never client-supplied). The client must refresh its session after
    this to pick up the new claims. O(1)."""
    ux_mode = ux_mode_for_experience(body.agent_experience)
    metadata: dict[str, object] = {
        "onboarding_complete": True,
        "ux_mode": ux_mode.value,
        "agent_experience": body.agent_experience.value,
        "job_role": body.job_role.value,
        "referral_source": body.referral_source.value,
    }
    if body.referral_source_other:
        metadata["referral_source_other"] = body.referral_source_other
    if body.job_role_other:
        metadata["job_role_other"] = body.job_role_other
    try:
        user = await auth.update_user_metadata(token, metadata)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not save onboarding. Please try again.",
        ) from exc

    await AuditRepository(db).record(
        action=Action.ONBOARDING_COMPLETE,
        outcome=Outcome.SUCCESS,
        actor_user_id=current_user.id,
        resource_type=ResourceType.USER,
        resource_id=current_user.id,
        metadata={
            "ux_mode": ux_mode.value,
            "job_role": body.job_role.value,
            "referral_source": body.referral_source.value,
        },
    )
    return _user_out(user)


@router.patch("/profile", response_model=UserOut)
async def update_profile(
    body: ProfileRequest,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    token: Annotated[str, Depends(get_access_token)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    """Change the caller's own display name.

    Writes to the caller's OWN user_metadata through their own token, so a user
    can only ever rename themselves — the same path as onboarding and the mode
    toggle, and the same reason it needs no extra authorization check. O(1).
    """
    try:
        user = await auth.update_user_metadata(token, {"full_name": body.full_name})
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not save your name. Please try again.",
        ) from exc

    await AuditRepository(db).record(
        action=Action.PROFILE_UPDATE,
        outcome=Outcome.SUCCESS,
        actor_user_id=current_user.id,
        resource_type=ResourceType.USER,
        resource_id=current_user.id,
        metadata={"field": "full_name"},
    )
    return _user_out(user)


@router.patch("/ux-mode", response_model=UserOut)
async def change_ux_mode(
    body: UxModeRequest,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    token: Annotated[str, Depends(get_access_token)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    """Flip the caller's UX shell preference (simple|advanced) on their own
    user_metadata. Mode is a UX preference, never a security boundary — RLS/OPA
    still gate every action regardless of mode. The client must refresh its
    session afterwards to pick up the new claim. Idempotent. O(1)."""
    try:
        user = await auth.update_user_metadata(token, {"ux_mode": body.ux_mode.value})
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not change mode. Please try again.",
        ) from exc

    await AuditRepository(db).record(
        action=Action.UX_MODE_CHANGE,
        outcome=Outcome.SUCCESS,
        actor_user_id=current_user.id,
        resource_type=ResourceType.USER,
        resource_id=current_user.id,
        metadata={"ux_mode": body.ux_mode.value},
    )
    return _user_out(user)
