"""Webhook subscriptions + signed delivery worker.

Lifecycle:
  1. Admin creates subscription -> we generate a 32-byte HMAC secret, store
     Argon2id-hashed (mirrors ApiKey). The plaintext secret is returned ONCE.
  2. Event fires (e.g. agent.invoked) -> `fan_out()` writes a `webhook_deliveries`
     row per active subscription that includes the event in its `events[]` list.
  3. Long-running worker (started at app lifespan) scans for `status='pending'
     AND next_attempt_at <= now()` and POSTs the payload with the
     `X-Sila-Signature` HMAC header.
  4. Retry with exponential backoff: 1s, 5s, 25s, 125s, 625s.
     After `webhook_max_attempts`, mark `status='dead'`.

Receivers verify with the same HMAC secret and a constant-time compare.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.config import settings
from sila.core.constants import Action, Outcome, ResourceType
from sila.core.security.ssrf_guard import validate_external_url
from sila.db.models import WebhookDelivery, WebhookSubscription
from sila.db.repositories.audit import AuditRepository
from sila.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)

ALLOWED_EVENTS: frozenset[str] = frozenset({"agent.invoked", "agent.failed", "agent.completed"})

_SECRET_BYTES = 24  # 48 hex chars
_SECRET_PREFIX = "whsec_"
_PREFIX_VISIBLE_LEN = len(_SECRET_PREFIX) + 8  # whsec_ + 8 hex chars


@dataclass(frozen=True)
class CreatedSubscription:
    """Returned once on creation. `secret` is the plaintext HMAC secret —
    never retrievable again."""

    subscription: WebhookSubscription
    secret: str


class WebhookService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._audit = AuditRepository(db)

    # ----------------- Subscription CRUD -----------------

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        url: str,
        events: list[str],
        actor_id: uuid.UUID,
    ) -> CreatedSubscription:
        """Validate URL via SSRF guard; generate + hash secret; return plaintext once."""
        bad_events = [e for e in events if e not in ALLOWED_EVENTS]
        if bad_events:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Unknown events: {bad_events}. Allowed: {sorted(ALLOWED_EVENTS)}."),
            )

        try:
            validated_url = await validate_external_url(url)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid webhook URL: {e}",
            ) from e

        plaintext = _SECRET_PREFIX + secrets.token_hex(_SECRET_BYTES)

        async with self._db.begin_nested():
            sub = WebhookSubscription(
                workspace_id=workspace_id,
                name=name,
                url=validated_url,
                events=events,
                signing_secret=plaintext,
                secret_prefix=plaintext[:_PREFIX_VISIBLE_LEN],
                created_by=actor_id,
            )
            self._db.add(sub)
            await self._db.flush()
            await self._audit.record(
                action=Action.WEBHOOK_CREATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.WEBHOOK_SUBSCRIPTION,
                resource_id=sub.id,
                metadata={"events": events},
            )
        return CreatedSubscription(subscription=sub, secret=plaintext)

    async def delete(
        self, *, subscription_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        sub = await self._db.get(WebhookSubscription, subscription_id)
        if not sub or sub.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found"
            )
        async with self._db.begin_nested():
            await self._db.delete(sub)
            await self._audit.record(
                action=Action.WEBHOOK_DELETE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.WEBHOOK_SUBSCRIPTION,
                resource_id=subscription_id,
            )

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[WebhookSubscription]:
        result = await self._db.execute(
            select(WebhookSubscription)
            .where(WebhookSubscription.workspace_id == workspace_id)
            .order_by(WebhookSubscription.created_at.desc())
        )
        return list(result.scalars().all())

    # ----------------- Fan-out (called by services on event) -----------------

    async def fan_out(
        self,
        *,
        workspace_id: uuid.UUID,
        event: str,
        data: dict[str, Any],
    ) -> int:
        """Enqueue a delivery row for each active subscription matching the event.

        Returns the count of rows enqueued. O(s) where s = subscriptions per workspace
        (typically < 5).
        """
        if event not in ALLOWED_EVENTS:
            log.warning("fan_out called with unknown event %r", event)
            return 0

        result = await self._db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.workspace_id == workspace_id,
                WebhookSubscription.status == "active",
                WebhookSubscription.events.contains([event]),  # PG ARRAY contains
            )
        )
        subs = list(result.scalars().all())
        if not subs:
            return 0

        now = datetime.utcnow()
        delivery_id_pool = [uuid.uuid4() for _ in subs]
        async with self._db.begin_nested():
            for sub, delivery_id in zip(subs, delivery_id_pool, strict=True):
                payload = {
                    "delivery_id": str(delivery_id),
                    "event": event,
                    "occurred_at": now.isoformat(),
                    "data": data,
                }
                row = WebhookDelivery(
                    subscription_id=sub.id,
                    workspace_id=workspace_id,
                    event=event,
                    payload=payload,
                    delivery_id=delivery_id,
                    status="pending",
                    next_attempt_at=now,  # try immediately
                )
                self._db.add(row)
            await self._db.flush()
        return len(subs)


# =============================================================================
# Long-running delivery worker
# =============================================================================


def sign_payload(secret: str, body: bytes) -> str:
    """Return `sha256=<hex>` HMAC signature. Used by tests and by senders."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time comparison for receivers that want to verify deliveries."""
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature_header or "")


def _next_backoff_seconds(attempt_count: int, initial: int) -> int:
    """Geometric: initial * 5^attempt. attempt_count 0..4 -> 1, 5, 25, 125, 625."""
    return int(initial * (5**attempt_count))


def _resolve_secret_for_subscription(subscription: WebhookSubscription) -> str:
    """Return the plaintext HMAC signing secret for this subscription.

    Standard webhook design — receivers got these same bytes at create time
    and use them to verify signatures. Encrypt-at-rest with an app key is a
    follow-on (see PHASE_LOG outstanding items).
    """
    return subscription.signing_secret


class WebhookDeliveryWorker:
    """Polls `webhook_deliveries` for due rows and attempts HMAC-signed POST.

    Started once at FastAPI lifespan start. Stop signal via `cancel()`.
    """

    def __init__(self, *, interval_s: int, http_timeout_s: float, max_attempts: int) -> None:
        self._interval_s = interval_s
        self._timeout_s = http_timeout_s
        self._max_attempts = max_attempts
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="webhook-delivery-worker")
            log.info("Webhook delivery worker started.")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        log.info("Webhook delivery worker stopped.")

    async def _loop(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001 — never let a tick error kill the loop
                    log.exception("Webhook worker tick failed")
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_s)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        now = datetime.utcnow()
        async with AsyncSessionLocal.begin() as session:
            due = await session.execute(
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.next_attempt_at.asc())
                .limit(20)
            )
            rows = list(due.scalars().all())
            if not rows:
                return
            # Build a sub_id -> secret_hash map in one query.
            sub_ids = list({r.subscription_id for r in rows})
            sub_lookup = await session.execute(
                select(WebhookSubscription).where(WebhookSubscription.id.in_(sub_ids))
            )
            subs_by_id = {s.id: s for s in sub_lookup.scalars().all()}

            for row in rows:
                sub = subs_by_id.get(row.subscription_id)
                if sub is None or sub.status != "active":
                    row.status = "failed"
                    row.last_error = "Subscription missing or not active"
                    continue
                await self._attempt(session, row, sub)

    async def _attempt(
        self,
        session: AsyncSession,
        row: WebhookDelivery,
        sub: WebhookSubscription,
    ) -> None:
        row.attempt_count = (row.attempt_count or 0) + 1
        row.last_attempt_at = datetime.utcnow()
        secret = _resolve_secret_for_subscription(sub)
        body = json.dumps(row.payload, default=str).encode("utf-8")
        signature = sign_payload(secret, body)
        headers = {
            "Content-Type": "application/json",
            "X-Sila-Event": row.event,
            "X-Sila-Delivery": str(row.delivery_id),
            "X-Sila-Signature": signature,
        }

        # Re-validate URL on every attempt (DNS-rebind mitigation).
        try:
            await validate_external_url(sub.url)
        except ValueError as e:
            row.status = "failed"
            row.last_error = f"URL no longer safe: {e}"
            return

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(sub.url, content=body, headers=headers)
            row.last_status_code = resp.status_code
            if 200 <= resp.status_code < 300:
                row.status = "success"
                row.last_error = None
                row.next_attempt_at = None
                return
            row.last_error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            row.last_status_code = None
            row.last_error = str(exc)[:300]

        # Not a success — schedule retry or mark dead.
        if row.attempt_count >= self._max_attempts:
            row.status = "dead"
            row.next_attempt_at = None
            return
        backoff = _next_backoff_seconds(row.attempt_count, settings.webhook_initial_backoff_s)
        row.next_attempt_at = datetime.utcnow() + timedelta(seconds=backoff)


# Singleton — started by app lifespan.
worker = WebhookDeliveryWorker(
    interval_s=settings.webhook_worker_interval_s,
    http_timeout_s=settings.webhook_delivery_timeout_s,
    max_attempts=settings.webhook_max_attempts,
)


# Re-exports for tests / receivers
__all__ = [
    "ALLOWED_EVENTS",
    "CreatedSubscription",
    "WebhookDeliveryWorker",
    "WebhookService",
    "sign_payload",
    "verify_signature",
    "worker",
]
