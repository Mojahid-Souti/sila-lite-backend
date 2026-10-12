from fastapi import APIRouter

from sila.api.v1 import (
    agents,
    analytics,
    auth,
    bazaar,
    connectors,
    files,
    internal_runs,
    knowledge_base,
    n8n,
    notifications,
    public,
    sila,
    skills,
    tools,
    users,
    vetting,
    webhooks,
    workspaces,
)

router = APIRouter(prefix="/v1")
router.include_router(auth.router)
router.include_router(workspaces.router)
router.include_router(agents.router)
router.include_router(bazaar.router)
router.include_router(knowledge_base.router)
router.include_router(users.router)
router.include_router(tools.router)
router.include_router(vetting.router)
# Phase 7
router.include_router(public.router)
router.include_router(webhooks.router)
# Phase 8
router.include_router(analytics.router)
# Phase B multi-tenancy — n8n session bridge
router.include_router(n8n.router)
# SILA Voice phase — concierge WebSocket
router.include_router(sila.router)
# WS-9 — connector authorization (Nango)
router.include_router(connectors.router)
# Internal agent-run (capability-gated; called by built n8n workflows)
router.include_router(internal_runs.router)
# Generated-file downloads (capability URLs — chat artifacts like slide decks)
router.include_router(files.router)
# Phase 18 — open-source skills in the Bazaar + the notification bell
router.include_router(skills.router)
router.include_router(notifications.router)
