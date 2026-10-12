import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UserResult:
    id: uuid.UUID
    email: str
    email_confirmed: bool
    full_name: str | None = None
    # Phase 17 — onboarding state, stored in GoTrue user_metadata.
    onboarding_complete: bool = False
    ux_mode: str | None = None
    job_role: str | None = None
    referral_source: str | None = None


@dataclass(frozen=True)
class SessionResult:
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    user: UserResult


class AuthService(Protocol):
    """Interface for authentication operations. Swap adapters without touching business logic."""

    async def sign_up(
        self, email: str, password: str, full_name: str | None = None
    ) -> UserResult: ...

    async def sign_in(self, email: str, password: str) -> SessionResult: ...

    async def sign_out(self, access_token: str) -> None: ...

    async def refresh_session(self, refresh_token: str) -> SessionResult: ...

    async def reset_password(self, email: str) -> None: ...

    async def get_user(self, access_token: str) -> UserResult: ...

    async def update_user_metadata(
        self, access_token: str, data: dict[str, object]
    ) -> UserResult: ...

    async def get_users_by_ids(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, UserResult]: ...

    async def admin_delete_user(self, user_id: uuid.UUID) -> None: ...

    async def revoke_all_sessions(self, user_id: uuid.UUID) -> None: ...
