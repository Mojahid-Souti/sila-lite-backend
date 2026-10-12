"""WS-9 — connector authorization endpoints (Nango Auth + Proxy only).

Mounted at /v1/workspaces/{wid}/connectors. The browser never receives a raw
third-party token — only a short-lived Connect session token, connection
references, and proxied list results. Tokens live encrypted in Nango.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Form, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.api.deps import check_opa, require_workspace_role
from sila.core.constants import Action, Role
from sila.db.session import AsyncSessionLocal, get_db
from sila.services.connectors import capability
from sila.services.connectors.connector_service import ConnectorError, ConnectorService

log = structlog.get_logger(__name__)

router = APIRouter(tags=["connectors"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResourcePickerOut(BaseModel):
    id: str
    label: str


class ConnectorOut(BaseModel):
    provider: str
    label: str
    auth_type: str
    scopes: list[str]
    connected: bool
    selected_resources: dict[str, Any] = Field(default_factory=dict)
    resource_pickers: list[ResourcePickerOut] = Field(default_factory=list)


class ConnectSessionOut(BaseModel):
    provider: str
    integration_id: str
    # Short-lived (30m) token the Connect UI consumes. NOT a provider token.
    session_token: str


class PersistBindingRequest(BaseModel):
    # The connection id Nango returns to the Connect UI on success.
    connection_id: str = Field(min_length=1, max_length=255)


class ResourceOptionOut(BaseModel):
    value: str
    label: str


class SaveResourcesRequest(BaseModel):
    # e.g. {"channel": {"id": "C123", "label": "#general"}}
    resources: dict[str, Any]


class InternalProxyRequest(BaseModel):
    provider: str
    method: str = "POST"
    endpoint: str
    data: dict[str, Any] | None = None


def _svc(db: AsyncSession) -> ConnectorService:
    return ConnectorService(db)


def _bad(err: ConnectorError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/connectors", response_model=list[ConnectorOut])
async def list_connectors(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ConnectorOut]:
    user, role = caller
    if not await check_opa(Action.CONNECTOR_VIEW, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    entries = await _svc(db).catalog(user_id=user.id, workspace_id=workspace_id)
    return [
        ConnectorOut(
            provider=e.provider,
            label=e.label,
            auth_type=e.auth_type,
            scopes=list(e.scopes),
            connected=e.connected,
            selected_resources=e.selected_resources,
            resource_pickers=[ResourcePickerOut(id=p[0], label=p[1]) for p in e.resource_pickers],
        )
        for e in entries
    ]


# ---------------------------------------------------------------------------
# Connect (mint session → persist binding)
# ---------------------------------------------------------------------------


@router.post(
    "/workspaces/{workspace_id}/connectors/{provider}/session",
    response_model=ConnectSessionOut,
)
async def create_connect_session(
    workspace_id: uuid.UUID,
    provider: str,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConnectSessionOut:
    user, role = caller
    if not await check_opa(Action.CONNECTOR_CONNECT, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    try:
        init = await _svc(db).start_connect(
            user_id=user.id, user_email=user.email, workspace_id=workspace_id, provider=provider
        )
    except ConnectorError as err:
        raise _bad(err) from err
    return ConnectSessionOut(
        provider=init.provider,
        integration_id=init.integration_id,
        session_token=init.session_token,
    )


@router.post(
    "/workspaces/{workspace_id}/connectors/{provider}/binding",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def persist_binding(
    workspace_id: uuid.UUID,
    provider: str,
    body: PersistBindingRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    user, role = caller
    if not await check_opa(Action.CONNECTOR_CONNECT, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    try:
        await _svc(db).persist_binding(
            user_id=user.id,
            workspace_id=workspace_id,
            provider=provider,
            connection_id=body.connection_id,
        )
    except ConnectorError as err:
        raise _bad(err) from err


# ---------------------------------------------------------------------------
# Resource pickers
# ---------------------------------------------------------------------------


@router.get(
    "/workspaces/{workspace_id}/connectors/{provider}/resources/{picker}",
    response_model=list[ResourceOptionOut],
)
async def list_resources(
    workspace_id: uuid.UUID,
    provider: str,
    picker: str,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ResourceOptionOut]:
    user, role = caller
    if not await check_opa(Action.CONNECTOR_CONNECT, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    try:
        options = await _svc(db).list_resources(
            user_id=user.id, workspace_id=workspace_id, provider=provider, picker_id=picker
        )
    except ConnectorError as err:
        raise _bad(err) from err
    return [ResourceOptionOut(value=o.value, label=o.label) for o in options]


@router.put(
    "/workspaces/{workspace_id}/connectors/{provider}/resources",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def save_resources(
    workspace_id: uuid.UUID,
    provider: str,
    body: SaveResourcesRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    user, role = caller
    if not await check_opa(Action.CONNECTOR_CONNECT, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    try:
        await _svc(db).save_resource_choice(
            user_id=user.id,
            workspace_id=workspace_id,
            provider=provider,
            resources=body.resources,
        )
    except ConnectorError as err:
        raise _bad(err) from err


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Internal execution proxy (n8n → Sila → Nango)
#
# Authenticated by a per-connection CAPABILITY token (not the user JWT, not a
# reusable secret) baked into the built workflow. The token encodes exactly
# (workspace_id, user_id, provider); we resolve the connection server-side and
# call Nango's proxy. n8n never holds a third-party token. Fails closed if the
# binding was revoked.
# ---------------------------------------------------------------------------


@router.post("/internal/connectors/proxy")
async def internal_proxy(
    body: InternalProxyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_sila_connector_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    cap = capability.verify(x_sila_connector_token or "")
    if cap is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid capability")
    # The token is authoritative for BOTH the provider and the endpoint — the
    # body cannot widen the capability to another method/endpoint.
    if body.provider != cap.provider or body.endpoint != cap.endpoint:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Capability mismatch")
    try:
        return await _svc(db).execute_proxy(
            user_id=cap.user_id,
            workspace_id=cap.workspace_id,
            provider=cap.provider,
            method=body.method,
            endpoint=cap.endpoint,
            data=body.data,
            expected_connection_id=cap.connection_id,
        )
    except ConnectorError as err:
        # Revoked / rotated / not connected → fail closed (binding gone or changed).
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err)) from err


@router.delete(
    "/workspaces/{workspace_id}/connectors/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_connector(
    workspace_id: uuid.UUID,
    provider: str,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    user, role = caller
    if not await check_opa(Action.CONNECTOR_REVOKE, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    if provider == "grafana":
        # Route-order gotcha: this generic {provider} route is registered FIRST,
        # so it captures "grafana" before the dedicated endpoint below can (live
        # bug: Disconnect silently failed). Grafana is a workspace-level
        # connection (not a per-user Nango binding) — admin only, own store.
        if role != Role.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
        from sila.db.repositories.grafana import GrafanaConnectionRepository

        await GrafanaConnectionRepository(db).delete_for_workspace(workspace_id)
        return
    try:
        await _svc(db).revoke(user_id=user.id, workspace_id=workspace_id, provider=provider)
    except ConnectorError as err:
        raise _bad(err) from err


# ---------------------------------------------------------------------------
# Grafana — the first self-hosted-URL connector (base URL + API token, not OAuth)
# ---------------------------------------------------------------------------


class GrafanaConnectRequest(BaseModel):
    base_url: str = Field(min_length=4, max_length=500)
    token: str = Field(min_length=1, max_length=1000)


class GrafanaStatusOut(BaseModel):
    connected: bool
    base_url: str | None = None


@router.get("/workspaces/{workspace_id}/connectors/grafana/status", response_model=GrafanaStatusOut)
async def grafana_status(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GrafanaStatusOut:
    from sila.services.connectors.grafana_service import GrafanaService

    status_dict = await GrafanaService(db).get_status(workspace_id)
    if status_dict is None:
        return GrafanaStatusOut(connected=False)
    return GrafanaStatusOut(connected=True, base_url=str(status_dict["base_url"]))


@router.put(
    "/workspaces/{workspace_id}/connectors/grafana",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def connect_grafana(
    workspace_id: uuid.UUID,
    body: GrafanaConnectRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Register/replace the workspace's Grafana (admin only). Validates + health-
    checks the URL, then stores the token encrypted. The token is never returned."""
    from sila.services.connectors.grafana_service import GrafanaConfigError, GrafanaService

    user, _role = caller
    try:
        await GrafanaService(db).save_connection(
            workspace_id=workspace_id, base_url=body.base_url, token=body.token, actor_id=user.id
        )
    except GrafanaConfigError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err


@router.delete(
    "/workspaces/{workspace_id}/connectors/grafana",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect_grafana(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sila.db.repositories.grafana import GrafanaConnectionRepository

    await GrafanaConnectionRepository(db).delete_for_workspace(workspace_id)


@router.post(
    "/workspaces/{workspace_id}/connectors/grafana/builtin",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def connect_builtin_grafana(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """One-click connect to Sila's BUILT-IN Grafana (admin only). The backend
    mints the service-account token itself — no Grafana visit, no pasted token."""
    from sila.services.connectors.grafana_service import GrafanaConfigError, GrafanaService

    user, _role = caller
    try:
        await GrafanaService(db).connect_builtin(workspace_id=workspace_id, actor_id=user.id)
    except GrafanaConfigError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err


# ---------------------------------------------------------------------------
# Slide templates — corporate .pptx files SILA fills with generated content.
# ---------------------------------------------------------------------------

_PPTX_MAX_BYTES = 15 * 1024 * 1024


class PptxTemplateOut(BaseModel):
    # str, not UUID: built-ins are code-defined ("builtin-bold-blue"), not rows.
    id: str
    name: str
    file_size: int
    created_at: str
    thumbnails: list[str] = []
    builtin: bool = False


# Built-in previews render once per process (Gotenberg round-trip), then cache.
_builtin_thumbs_cache: dict[str, list[str]] = {}

# Missing/stale previews render in the BACKGROUND — the gallery must never
# block on a Gotenberg round-trip (a first open used to hang 10-30s doing
# renders inline). One attempt per template per process: a file that won't
# convert doesn't burn a render on every gallery open.
_SAMPLE_SLIDE_COUNT = 4  # pptx_thumbs' sample deck size; fewer stored = stale era
_thumb_backfill_attempted: set[uuid.UUID] = set()
_thumb_backfill_tasks: set[asyncio.Task[None]] = set()


async def _backfill_thumbnails(template_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Render + persist previews for one template, in its own session."""
    from sila.db.repositories.pptx_templates import PptxTemplateRepository
    from sila.services.connectors.pptx_thumbs import render_thumbnails

    try:
        async with AsyncSessionLocal.begin() as session:
            row = await PptxTemplateRepository(session).get(template_id, workspace_id)
            if row is None:
                return
            thumbs = await render_thumbnails(row.file_bytes)
            if thumbs:  # a failed render keeps whatever previews exist
                row.thumbnails = thumbs
    except Exception:
        log.warning("pptx_templates.thumb_backfill_failed", template_id=str(template_id))


def _spawn_thumb_backfill(template_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    task = asyncio.create_task(_backfill_thumbnails(template_id, workspace_id))
    _thumb_backfill_tasks.add(task)  # create_task holds only a weak ref
    task.add_done_callback(_thumb_backfill_tasks.discard)


async def _builtin_template_entries() -> list[PptxTemplateOut]:
    from sila.services.connectors.builtin_pptx import THEMES, builtin_template_bytes
    from sila.services.connectors.pptx_thumbs import render_thumbnails

    out: list[PptxTemplateOut] = []
    for theme in THEMES:
        blob = builtin_template_bytes(theme.slug)
        if not blob:
            continue
        if theme.slug not in _builtin_thumbs_cache:
            _builtin_thumbs_cache[theme.slug] = await render_thumbnails(blob)
        out.append(
            PptxTemplateOut(
                id=f"builtin-{theme.slug}",
                name=theme.name,
                file_size=len(blob),
                created_at="",
                thumbnails=_builtin_thumbs_cache[theme.slug],
                builtin=True,
            )
        )
    return out


@router.get("/workspaces/{workspace_id}/templates/pptx", response_model=list[PptxTemplateOut])
async def list_pptx_templates(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PptxTemplateOut]:
    from sila.db.repositories.pptx_templates import PptxTemplateRepository

    rows = await PptxTemplateRepository(db).list_for_workspace(workspace_id)
    for t in rows:
        # Uploaded before the renderer existed, or before the 4-slide sample:
        # refresh in the background; this open shows what's stored, the next
        # one shows the full set.
        if len(t.thumbnails or []) < _SAMPLE_SLIDE_COUNT and t.id not in _thumb_backfill_attempted:
            _thumb_backfill_attempted.add(t.id)
            _spawn_thumb_backfill(t.id, workspace_id)
    workspace_rows = [
        PptxTemplateOut(
            id=str(t.id),
            name=t.name,
            file_size=t.file_size,
            created_at=t.created_at.isoformat(),
            thumbnails=list(t.thumbnails or []),
        )
        for t in rows
    ]
    # Workspace uploads first (the corporate ones are the point), built-ins after.
    return workspace_rows + await _builtin_template_entries()


@router.post(
    "/workspaces/{workspace_id}/templates/pptx",
    response_model=PptxTemplateOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_pptx_template(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile,
    name: Annotated[str, Form(min_length=2, max_length=100)],
) -> PptxTemplateOut:
    """Upload a corporate .pptx template (admin). Validated as a real PowerPoint
    archive BEFORE storing — a bad file fails here, never at deck-build time."""
    from sila.db.repositories.pptx_templates import PptxTemplateRepository
    from sila.services.connectors.pptx_template import TemplateError, fill_template

    user, _role = caller
    blob = await file.read()
    if len(blob) > _PPTX_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Template exceeds 15 MB"
        )
    try:
        # Dry-run the fill: proves the archive parses AND has usable layouts.
        fill_template(blob, "probe", [])
    except TemplateError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err

    repo = PptxTemplateRepository(db)
    try:
        tpl = await repo.create(
            workspace_id=workspace_id, name=name.strip(), file_bytes=blob, uploaded_by=user.id
        )
    except IntegrityError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A template named '{name}' already exists.",
        ) from err
    # Real previews for the gallery — rendered once here (best-effort).
    from sila.services.connectors.pptx_thumbs import render_thumbnails

    tpl.thumbnails = await render_thumbnails(blob)
    return PptxTemplateOut(
        id=str(tpl.id),
        name=tpl.name,
        file_size=tpl.file_size,
        created_at=tpl.created_at.isoformat(),
        thumbnails=list(tpl.thumbnails or []),
    )


@router.delete(
    "/workspaces/{workspace_id}/templates/pptx/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_pptx_template(
    workspace_id: uuid.UUID,
    template_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sila.db.repositories.pptx_templates import PptxTemplateRepository

    await PptxTemplateRepository(db).delete(template_id, workspace_id)
