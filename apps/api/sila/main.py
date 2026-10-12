import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from sila.api.v1.router import router
from sila.core.config import settings
from sila.core.policies.classification_policy import REPO_POLICY_PATH
from sila.db.session import AsyncSessionLocal
from sila.services import n8n_provisioning
from sila.services.webhook_service import worker as webhook_worker

log = structlog.get_logger()

# Phase 8 — refresh the Command Center materialized view this often (seconds).
_MV_REFRESH_INTERVAL_S = 60


async def _mv_refresh_loop(stop_event: asyncio.Event) -> None:
    """Refresh mv_workspace_daily every 60s.

    CONCURRENTLY keeps read traffic unblocked, but Postgres rejects it until
    the MV has been populated once. First pass is non-CONCURRENT; subsequent
    passes use CONCURRENTLY.
    """
    from sqlalchemy import text as _text

    first = True
    while not stop_event.is_set():
        sql = (
            "REFRESH MATERIALIZED VIEW mv_workspace_daily"
            if first
            else "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_workspace_daily"
        )
        try:
            async with AsyncSessionLocal.begin() as session:
                await session.execute(_text(sql))
            first = False
        except Exception:  # noqa: BLE001 — loop must not die
            log.exception("mv_refresh_failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=_MV_REFRESH_INTERVAL_S)


# Files the API needs at runtime from the /policies mount. Vetting reads
# classification.yaml on every submission; pdpl.yaml drives the sovereignty
# denylist. Missing them does not crash anything — it just makes the security
# review fail with an errno-2 nobody reads, which is worse.
_REQUIRED_POLICIES = ("classification.yaml", "pdpl.yaml")


def _check_policy_mount() -> None:
    """Warn loudly when the policy mount came up empty.

    The WSL bind mount for ./infra/policies has come up empty after a Docker
    restart — the same failure that repeatedly emptied OPA's (BUG-020/057/080).
    There it caused a visible 403 storm; here it silently broke the security
    review, which is a worse way to fail. A plain `restart` does NOT re-resolve
    the mount; the container has to be recreated.
    """
    # Same resolution the loaders use: the container env var, else the repo path.
    env_path = os.environ.get("SILA_CLASSIFICATION_POLICY_PATH")
    base = Path(env_path).parent if env_path else REPO_POLICY_PATH.parent
    missing = [name for name in _REQUIRED_POLICIES if not (base / name).is_file()]
    if missing:
        log.error(
            "sila_api.policy_mount_empty",
            missing=missing,
            dir=str(base),
            impact="the security review will fail on every submission",
            recovery="docker compose up -d --force-recreate --no-deps sila-api",
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    log.info("sila_api.startup", env=settings.sila_env)
    _check_policy_mount()
    webhook_worker.start()
    mv_stop = asyncio.Event()
    mv_task = asyncio.create_task(_mv_refresh_loop(mv_stop), name="mv-refresh-worker")
    # Phase B multi-tenancy — make sure n8n has an owner the first time we boot.
    # Background-fired so a slow n8n start doesn't block the API healthcheck.
    n8n_bootstrap_task = asyncio.create_task(
        n8n_provisioning.bootstrap_owner_with_retry(n8n_provisioning.make_n8n_service()),
        name="n8n-owner-bootstrap",
    )
    try:
        yield
    finally:
        mv_stop.set()
        mv_task.cancel()
        n8n_bootstrap_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await mv_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await n8n_bootstrap_task
        await webhook_worker.stop()
        with contextlib.suppress(Exception):
            from sila.services.sila.orchestrator.checkpointer import close_checkpointer

            await close_checkpointer()
        log.info("sila_api.shutdown")


app = FastAPI(
    title="Sila API",
    version="0.1.0",
    docs_url="/docs" if settings.sila_env == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Every call carries an Authorization header, which makes it a non-simple
    # request, so the browser preflights each endpoint. Starlette defaults to a
    # 600 s cache, meaning the OPTIONS round-trip reappears every 10 minutes on
    # a long-lived session. 7200 s is Chrome's ceiling (Firefox allows 24 h and
    # clamps the rest), so this is the most preflight traffic we can remove
    # without the browser ignoring the value.
    max_age=7200,
)

app.include_router(router)


@app.get("/healthz")
async def health() -> dict:  # type: ignore[type-arg]
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Public OpenAPI (Phase 7)
# Strips internal-only endpoints — only `tags=["public"]` and `tags=["webhooks"]`
# (admin self-service for managing event subscriptions) are exposed.
# Used by the SDK generator and by the public docs portal.
# ---------------------------------------------------------------------------

_PUBLIC_TAGS: frozenset[str] = frozenset({"public", "webhooks"})


def _public_openapi() -> dict[str, Any]:
    """Return a filtered OpenAPI 3 document with only public-tagged operations."""
    full = get_openapi(
        title="Sila Public API",
        version="0.1.0",
        description="External-caller API. Authenticate with `Authorization: Bearer wk_...`.",
        routes=app.routes,
    )
    filtered_paths: dict[str, dict[str, Any]] = {}
    for path, operations in full.get("paths", {}).items():
        kept_ops: dict[str, Any] = {}
        for method, op in operations.items():
            tags = op.get("tags") or []
            if any(tag in _PUBLIC_TAGS for tag in tags):
                kept_ops[method] = op
        if kept_ops:
            filtered_paths[path] = kept_ops
    full["paths"] = filtered_paths
    return full


@app.get("/v1/openapi.json", include_in_schema=False)
async def public_openapi() -> JSONResponse:
    """Filtered OpenAPI spec exposing only public-tagged endpoints."""
    return JSONResponse(_public_openapi())
