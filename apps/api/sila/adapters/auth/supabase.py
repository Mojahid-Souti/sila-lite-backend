import asyncio
import uuid
from typing import Any

import httpx

from sila.adapters.auth.base import AuthService, SessionResult, UserResult


def _parse_user(data: dict[str, Any]) -> UserResult:
    # GoTrue returns the profile metadata under "user_metadata" (REST) or
    # "raw_user_meta_data" (admin); full_name is set at signup (see sign_up),
    # the onboarding fields by update_user_metadata (Phase 17).
    meta = data.get("user_metadata") or data.get("raw_user_meta_data") or {}
    if not isinstance(meta, dict):
        meta = {}
    return UserResult(
        id=uuid.UUID(data["id"]),
        email=data["email"],
        email_confirmed=bool(data.get("email_confirmed_at")),
        full_name=(meta.get("full_name") or None),
        onboarding_complete=bool(meta.get("onboarding_complete")),
        ux_mode=(meta.get("ux_mode") or None),
        job_role=(meta.get("job_role") or None),
        referral_source=(meta.get("referral_source") or None),
    )


def _parse_session(data: dict) -> SessionResult:  # type: ignore[type-arg]
    return SessionResult(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_type=data.get("token_type", "bearer"),
        expires_in=data["expires_in"],
        user=_parse_user(data["user"]),
    )


class SupabaseAuthAdapter:
    """Calls GoTrue REST API. Swap for OmantelSSOAdapter or KeycloakAdapter in production."""

    def __init__(self, base_url: str, service_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/auth/v1",
            headers={"apikey": service_key, "Content-Type": "application/json"},
            timeout=10.0,
        )

    async def sign_up(self, email: str, password: str, full_name: str | None = None) -> UserResult:
        payload: dict[str, Any] = {"email": email, "password": password}
        if full_name:
            payload["data"] = {"full_name": full_name}
        r = await self._client.post("/signup", json=payload)
        r.raise_for_status()
        data = r.json()
        return _parse_user(data.get("user") or data)

    async def sign_in(self, email: str, password: str) -> SessionResult:
        r = await self._client.post(
            "/token?grant_type=password",
            json={"email": email, "password": password},
        )
        r.raise_for_status()
        return _parse_session(r.json())

    async def sign_out(self, access_token: str) -> None:
        await self._client.post(
            "/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def refresh_session(self, refresh_token: str) -> SessionResult:
        r = await self._client.post(
            "/token?grant_type=refresh_token",
            json={"refresh_token": refresh_token},
        )
        r.raise_for_status()
        return _parse_session(r.json())

    async def reset_password(self, email: str) -> None:
        await self._client.post("/recover", json={"email": email})

    async def get_user(self, access_token: str) -> UserResult:
        r = await self._client.get(
            "/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        return _parse_user(r.json())

    async def update_user_metadata(self, access_token: str, data: dict[str, object]) -> UserResult:
        """Merge ``data`` into the caller's own GoTrue user_metadata (PUT /user).

        Scoped by the user's own access token — never the admin key — so a user
        can only ever write their own metadata. GoTrue merges (does not replace)
        the keys under user_metadata.
        """
        r = await self._client.put(
            "/user",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"data": data},
        )
        r.raise_for_status()
        return _parse_user(r.json())

    async def get_users_by_ids(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, UserResult]:
        """Resolve identities (email + full_name) for a set of users via the
        GoTrue admin API.

        One admin call per id, issued concurrently. n is the number of members
        in a workspace — small and bounded — so this is O(n) parallel network
        calls, not an N+1 serialized loop. Missing/failed lookups are omitted
        from the map; callers treat an absent id as "identity unknown" rather
        than failing the whole request.
        """
        unique_ids = list(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}

        async def _fetch(uid: uuid.UUID) -> UserResult | None:
            try:
                r = await self._client.get(f"/admin/users/{uid}")
                r.raise_for_status()
            except httpx.HTTPError:
                return None
            return _parse_user(r.json())

        results = await asyncio.gather(*(_fetch(uid) for uid in unique_ids))
        return {u.id: u for u in results if u is not None}

    async def admin_delete_user(self, user_id: uuid.UUID) -> None:
        r = await self._client.delete(f"/admin/users/{user_id}")
        r.raise_for_status()

    async def revoke_all_sessions(self, user_id: uuid.UUID) -> None:
        r = await self._client.post(f"/admin/users/{user_id}/logout", json={"scope": "global"})
        r.raise_for_status()


# Ensure the adapter satisfies the protocol at import time
_: AuthService = SupabaseAuthAdapter.__new__(SupabaseAuthAdapter)
