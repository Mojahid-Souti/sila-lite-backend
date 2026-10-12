"""API key bearer authentication for the public /v1 endpoints.

Phase 7. Keys are `wk_<40 hex>`; the first 11 chars (`wk_<8 hex>`) are
stored in `api_keys.key_prefix` for O(log n) lookup, and the full key is
Argon2id-hashed at rest. Verification:

  1. Strip "Bearer " from the Authorization header.
  2. Pull the first 11 chars as the prefix and look up active rows by it.
  3. For each candidate (almost always one), Argon2id-verify the full key.
  4. Return the resolved (api_key, workspace_id) tuple.

Failure modes are collapsed into a single generic 401 to avoid key enumeration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import ApiKey
from sila.db.session import get_db

_KEY_PREFIX_LEN = 11  # "wk_" + 8 hex
_ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)

# Custom Bearer that doesn't auto-raise — we want a unified 401 from us.
_bearer = HTTPBearer(auto_error=False, scheme_name="ApiKey")


@dataclass(frozen=True)
class ApiCaller:
    """Authenticated external caller. Resolved from an API key Bearer token."""

    api_key_id: uuid.UUID
    workspace_id: uuid.UUID
    key_name: str


async def get_api_caller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiCaller:
    """Dependency: validates `Authorization: Bearer wk_<...>` and returns ApiCaller.

    Generic 401 on any failure to avoid key enumeration.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": 'Bearer realm="sila-api"'},
        )

    raw = credentials.credentials.strip()
    if not raw.startswith("wk_") or len(raw) < _KEY_PREFIX_LEN + 8:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    prefix = raw[:_KEY_PREFIX_LEN]

    # Look up by prefix. Almost always exactly one row; the index is on key_prefix.
    result = await db.execute(
        select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.revoked_at.is_(None))
    )
    candidates = list(result.scalars().all())
    if not candidates:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    matched: ApiKey | None = None
    for candidate in candidates:
        try:
            _ph.verify(candidate.key_hash, raw)
            matched = candidate
            break
        except VerifyMismatchError:
            continue
    if matched is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    # Stash on request.state so downstream middleware/endpoints don't need to re-resolve.
    request.state.api_key_id = matched.id
    request.state.workspace_id = matched.workspace_id

    return ApiCaller(
        api_key_id=matched.id,
        workspace_id=matched.workspace_id,
        key_name=matched.name,
    )
