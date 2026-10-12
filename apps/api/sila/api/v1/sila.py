"""SILA concierge WebSocket — `/v1/sila/ws`.

Handshake: the FIRST frame must be `{"type":"hello","token":...,"workspace_id":...}`.
The token is the user's normal Supabase JWT, verified locally; membership is
resolved exactly like HTTP routes. Invalid auth → close 4401; daily session
rate-limit → close 4429 (security review).

Frames:
  client → server: hello{token, workspace_id} · text{content} ·
                   <binary mic chunks> · voice_end · bye
  server → client: ready{session_id} · state{value} · token{content} ·
                   transcript{content} · tool{name, status} · ui{command, ...} ·
                   say{value:start|end, content?} · <binary WAV reply> ·
                   error{message}

Voice notes: mic audio is buffered in memory only for the current utterance
(≤ 10 MB), transcribed, then discarded — never persisted (security review).
If the voice container is down, voice turns fail soft and text keeps working.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.api.deps import get_current_user, verify_supabase_jwt
from sila.core.config import settings
from sila.core.constants import MAX_SILA_BULK_DELETE, Action, Outcome, Role
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.membership import MembershipRepository
from sila.db.repositories.sila import SilaRepository
from sila.db.session import AsyncSessionLocal, get_db
from sila.services.bazaar_templates import OKR_TEMPLATE_ID
from sila.services.sila.concierge_service import ConciergeService
from sila.services.sila.okr_session import admit_turn, stash_carries_okr_sheet
from sila.services.sila.residency_gateway import ResidencyGateway
from sila.services.sila.session_start import (
    announce_template,
    attach_skills,
    skills_frame,
    skills_prompt,
)
from sila.services.sila.tool_registry import CONFIDENTIAL_KEY

if TYPE_CHECKING:
    from sila.adapters.llm.base import LLMGateway
    from sila.adapters.voice.base import SpeechToText, TextToSpeech

logger = logging.getLogger(__name__)

router = APIRouter()


def _record_slot_answer(build_state: dict, answers: dict) -> None:  # type: ignore[type-arg]
    """Record build-gate ask_user answers into build_state (Part B). The wizard answers
    are keyed by question id (= the slot name), so we write EVERY answer — the gate now
    asks all missing slots in one sheet, so a single response carries several. __confirm__
    flips the confirmed flag (only on an affirmative answer)."""
    filled = build_state.setdefault("filled", {})
    slot_changed = False
    for key, raw in answers.items():
        value = ", ".join(raw) if isinstance(raw, list) else str(raw or "")
        if key == "__confirm__":
            confirmed = value.strip().lower().startswith("yes")
            build_state["confirmed"] = confirmed
            if not confirmed:
                # "Let me change something" — the gate serves a deterministic
                # what-should-change question on its next run (never the model).
                build_state["needs_change"] = True
        elif key == "__change__":
            # The change question's answer (a slot label or a typed correction) —
            # the gate maps it and re-asks / applies it deterministically.
            build_state["change_slot"] = value
        elif not key.startswith("__"):
            # Control answers (e.g. the plan card's __plan__ decision) are routed by
            # the conversation, never recorded as requirement slots.
            if filled.get(key) != value:
                slot_changed = True
            filled[key] = value
    if slot_changed and "__confirm__" not in answers:
        # The spec changed (the preview card's inline edit / a re-answered slot) →
        # the pending confirm + its block are stale: drop them so the gate
        # re-confirms with a fresh id. The costly dry-run regenerates only when
        # the CONTENT changed (the only edit that alters the sample).
        build_state["confirmed"] = False
        build_state.pop("confirm_rid", None)
        build_state.pop("preview_shown", None)
        if "content" in answers:
            build_state.pop("preview_tried", None)
    build_state["asked"] = None


# WebSocket close codes (4000-range = application-defined).
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403
WS_RATE_LIMITED = 4429


MAX_UTTERANCE_BYTES = 10 * 1024 * 1024


def _gateway(model: str | None = None) -> LLMGateway:
    """The concierge brain. Local by default; hosted only on a deliberate switch.

    `SILA_LLM_PROVIDER=qwen-cloud` routes the CONCIERGE ONLY to a hosted model,
    for benchmarking answer quality against the local 14B. The gatekeeper's
    scanner is untouched and stays local: vetting is the one decision that must
    never depend on a third party. See docs/ops/CLOUD_MODEL_EVALUATION.md.
    """
    # Late import so tests can monkeypatch; same construction as the vetting LLM.
    if settings.sila_llm_provider == "qwen-cloud":
        from sila.adapters.llm.openai_compat import OpenAICompatLLMAdapter

        return OpenAICompatLLMAdapter(
            base_url=settings.qwen_base_url,
            # An explicit picker choice is an Ollama tag and means nothing to the
            # hosted API, so the configured cloud model wins while it is active.
            model=settings.qwen_model,
            api_key=settings.qwen_api_key,
        )
    from sila.adapters.llm.ollama import OllamaLLMAdapter

    return OllamaLLMAdapter(
        base_url=settings.ollama_url,
        model=model or settings.sila_llm_model,
        num_ctx=settings.sila_llm_num_ctx,
    )


def _session_gateway(model: str | None = None) -> ResidencyGateway:
    """The session's brain: the configured one, kept local for confidential chats.

    A conversation that carries an OKR sheet, or is pinned to the OKR Reporting
    Pack, sends every model call to the local Ollama whatever
    SILA_LLM_PROVIDER says — or refuses (services/sila/residency_gateway.py).
    """
    from sila.adapters.llm.ollama import OllamaLLMAdapter

    hosted = _gateway(model)
    local: LLMGateway = (
        hosted
        if getattr(hosted, "is_local", False) is True
        else OllamaLLMAdapter(
            base_url=settings.ollama_url,
            model=settings.sila_llm_model,
            num_ctx=settings.sila_llm_num_ctx,
        )
    )
    return ResidencyGateway(hosted=hosted, local=local)


def _is_embedding(name: str) -> bool:
    """Embedding models (bge, nomic, *-embed) can't drive a chat — exclude them
    from the brain picker so a pick never breaks the session."""
    low = name.lower()
    return "embed" in low or low.startswith(("bge", "nomic"))


# SILA is a TOOL-CALLING agent (it opens modals, builds workflows) — a model that can't
# reliably emit Ollama native tool calls just narrates and never opens anything. Only
# these families are known-good at it; everything else is offered but flagged "may not
# build reliably" so users don't pick a big-but-toolless model (gemma, qwen3.5, phi…).
_TOOL_CAPABLE_HINTS = (
    "qwen3",
    "qwen2.5",
    "qwen2:",
    "llama3.1",
    "llama3.3",
    "mistral",
    "command-r",
    "firefunction",
    "hermes",
    "glm4",
)


def _supports_tools(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in _TOOL_CAPABLE_HINTS)


@router.get("/sila/models")
async def list_models(
    _user: Annotated[UserResult, Depends(get_current_user)],
) -> dict[str, object]:
    """Locally-available chat models for the SILA model picker. Tool-capable models are
    floated to the top and also returned in `recommended`, so the picker steers users to
    a model that can actually build. Embedding models excluded; falls back to the default
    if Ollama is unreachable so the picker always renders."""
    # While the hosted provider is active the picker must not offer Ollama tags:
    # _gateway ignores them, so every pick would silently do nothing. Show the one
    # model actually serving the session instead.
    if settings.sila_llm_provider == "qwen-cloud":
        # The display name IS the id here. _gateway ignores whatever the picker
        # sends while the hosted provider is active, so nothing routes on this
        # value — which means the UI can show the product name instead of the
        # vendor's. Listing it as `recommended` marks it tool-capable, so the
        # picker stops labelling a function-calling model "Chat only".
        name = settings.cloud_model_display_name
        return {"models": [name], "default": name, "recommended": [name]}
    default = settings.sila_llm_model
    models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        chat = [m for m in data.get("models", []) if m.get("name") and not _is_embedding(m["name"])]
        # Tool-capable first, then largest first within each group.
        chat.sort(key=lambda m: (not _supports_tools(m["name"]), -m.get("size", 0)))
        models = [m["name"] for m in chat]
    except (httpx.HTTPError, KeyError, ValueError):
        models = []
    if not models:
        models = [default]
    recommended = [m for m in models if _supports_tools(m)]
    # The picker's default must exist locally AND, if possible, be tool-capable.
    fallback = recommended[0] if recommended else models[0]
    chosen_default = default if default in models else fallback
    return {"models": models, "default": chosen_default, "recommended": recommended}


@router.get("/sila/sessions")
async def list_sila_sessions(
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """The signed-in user's recent SILA sessions for the Recents sidebar."""
    rows = await SilaRepository(db).list_sessions(user.id)
    return {
        "sessions": [
            {"id": str(s.id), "title": (s.title or "")[:80], "started_at": s.started_at.isoformat()}
            for s in rows
        ]
    }


@router.get("/sila/sessions/{session_id}")
async def get_sila_session(
    session_id: uuid.UUID,
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """A session's transcript (oldest first) so the chat can resume it."""
    messages = await SilaRepository(db).messages_for_user(session_id, user.id)
    if messages is None:
        raise HTTPException(status_code=404, detail="Session not found")
    # created_at rides along so a reopened session can show when each reply
    # landed. It was on the row all along and simply never left the API.
    return {
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "trace": m.trace,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ]
    }


@router.delete("/sila/sessions/{session_id}", status_code=204)
async def delete_sila_session(
    session_id: uuid.UUID,
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete one of the user's own sessions (and its messages)."""
    if not await SilaRepository(db).delete_session(session_id, user.id):
        raise HTTPException(status_code=404, detail="Session not found")
    # Deleting a conversation is a state change, so it takes an audit row like
    # every other one (cross-cutting concerns, CLAUDE.md section 7). Ids only —
    # never the titles or message text, which carry user content.
    await AuditRepository(db).record(
        action=Action.SILA_SESSION_DELETE,
        outcome=Outcome.SUCCESS,
        actor_user_id=user.id,
        metadata={"session_ids": [str(session_id)], "removed": 1},
    )


class BulkDeleteSessionsIn(BaseModel):
    """Ids to delete. Capped so one call cannot queue unbounded work; ids the
    user does not own are ignored rather than failing the batch."""

    session_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=MAX_SILA_BULK_DELETE)


@router.post("/sila/sessions/bulk-delete")
async def bulk_delete_sila_sessions(
    payload: BulkDeleteSessionsIn,
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    """Delete several of the user's own sessions in one call.

    One request rather than one DELETE per row: the UI can select a whole page
    of chats, and firing N requests would be an N+1 against the API and the DB
    (Rule 6). Returns how many were actually removed, which may be fewer than
    requested if some ids were already gone or never belonged to this user.
    """
    removed = await SilaRepository(db).delete_sessions(payload.session_ids, user.id)
    await AuditRepository(db).record(
        action=Action.SILA_SESSION_DELETE,
        outcome=Outcome.SUCCESS,
        actor_user_id=user.id,
        metadata={
            "session_ids": [str(i) for i in payload.session_ids],
            "requested": len(payload.session_ids),
            "removed": removed,
        },
    )
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Per-user memory (Mojahid's design): durable facts SILA learns about the user,
# capacity-capped; when full the composer blocks sending until entries go.
# ---------------------------------------------------------------------------


@router.get("/sila/memory")
async def list_sila_memory(
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    rows = await SilaRepository(db).list_memories(user.id)
    limit = settings.sila_max_memories
    return {
        "items": [
            {"id": str(m.id), "content": m.content, "created_at": m.created_at.isoformat()}
            for m in rows
        ],
        "limit": limit,
        "full": len(rows) >= limit,
    }


@router.delete("/sila/memory/{memory_id}", status_code=204)
async def delete_sila_memory(
    memory_id: uuid.UUID,
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    if not await SilaRepository(db).delete_memory(memory_id, user.id):
        raise HTTPException(status_code=404, detail="Memory not found")
    await AuditRepository(db).record(
        action=Action.SILA_MEMORY_DELETE,
        outcome=Outcome.SUCCESS,
        actor_user_id=user.id,
        metadata={"memory_id": str(memory_id)},
    )


@router.delete("/sila/memory", status_code=204)
async def clear_sila_memory(
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Empty the user's memory. The UI confirms with a warning first."""
    removed = await SilaRepository(db).clear_memories(user.id)
    await AuditRepository(db).record(
        action=Action.SILA_MEMORY_CLEAR,
        outcome=Outcome.SUCCESS,
        actor_user_id=user.id,
        metadata={"removed": removed},
    )


# ---------------------------------------------------------------------------
# Analysis Engine datasets — big data files upload HERE (bytes on disk, a
# per-user handle row); chat attachments stay for small files. DuckDB reads
# the FULL file, so analysis is never head-truncated again.
# ---------------------------------------------------------------------------

_DATASET_EXTENSIONS = (".csv", ".tsv", ".xlsx")


@router.post("/sila/datasets", status_code=201)
async def upload_sila_dataset(
    file: UploadFile,
    workspace_id: uuid.UUID,
    user: Annotated[UserResult, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """Store a FULL data file for DuckDB-backed analysis. Size-capped, csv/tsv/xlsx
    only, bytes streamed to disk (never through the model), audited by name+size.

    A workbook is kept as uploaded ({id}.xlsx — the OKR pack reads merged cells
    and cell line breaks from it) beside a CSV of its first sheet with content
    ({id}.csv), which is what the analysis tools read."""
    import os

    name = os.path.basename(file.filename or "data.csv")
    if not name.lower().endswith(_DATASET_EXTENSIONS):
        raise HTTPException(
            status_code=415, detail="Only .csv, .tsv and .xlsx datasets are supported."
        )
    if await MembershipRepository(db).get(workspace_id, user.id) is None:
        raise HTTPException(status_code=403, detail="Not a member of this workspace.")

    limit = settings.sila_dataset_max_mb * 1024 * 1024
    os.makedirs(settings.sila_datasets_dir, exist_ok=True)
    dataset_id = uuid.uuid4()
    path = os.path.join(settings.sila_datasets_dir, f"{dataset_id}.csv")
    workbook = name.lower().endswith(".xlsx")
    received = path[: -len(".csv")] + ".xlsx" if workbook else path
    size = 0
    try:
        with open(received, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Dataset exceeds the {settings.sila_dataset_max_mb} MB limit.",
                    )
                out.write(chunk)
    except HTTPException:
        with contextlib.suppress(OSError):
            os.remove(received)
        raise
    if workbook:
        _workbook_to_dataset_csv(received, path)

    # Row count via DuckDB (fast, streaming) — best-effort metadata.
    rows: int | None = None
    try:
        from sila.services.analysis.engine import profile_dataset

        rows = int(profile_dataset(path)["rows"])
    except Exception:  # noqa: BLE001 — an unreadable file still uploads; tools surface it
        rows = None

    record = await SilaRepository(db).create_dataset(
        user_id=user.id,
        workspace_id=workspace_id,
        filename=name,
        path=path,
        size_bytes=size,
        rows=rows,
    )
    await AuditRepository(db).record(
        action=Action.SILA_DATASET_UPLOAD,
        outcome=Outcome.SUCCESS,
        actor_user_id=user.id,
        actor_workspace_id=workspace_id,
        metadata={"filename": name, "size_bytes": size, "rows": rows},
    )
    return {"id": str(record.id), "filename": name, "size_bytes": size, "rows": rows}


def _workbook_to_dataset_csv(workbook_path: str, csv_path: str) -> None:
    """The CSV the analysis tools read, from an uploaded workbook; 422 (and no
    files left behind) when it is not a readable .xlsx."""
    import os

    from sila.core.utils.xlsx import XlsxError, workbook_to_csv

    try:
        with open(workbook_path, "rb") as source:
            _, text = workbook_to_csv(source.read())
    except XlsxError as exc:
        with contextlib.suppress(OSError):
            os.remove(workbook_path)
        raise HTTPException(status_code=422, detail=f"Not a readable workbook: {exc}") from exc
    with open(csv_path, "w", encoding="utf-8", newline="") as out:
        out.write(text)


def _voice() -> tuple[SpeechToText, TextToSpeech]:
    from sila.adapters.voice.http_voice import HttpSpeechToText, HttpTextToSpeech

    return HttpSpeechToText(settings.voice_url), HttpTextToSpeech(settings.voice_url)


@router.websocket("/sila/ws")
async def sila_ws(ws: WebSocket) -> None:
    await ws.accept()

    # --- Handshake -------------------------------------------------------
    try:
        hello = await ws.receive_json()
    except (WebSocketDisconnect, Exception):
        await ws.close(code=WS_UNAUTHORIZED)
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await ws.close(code=WS_UNAUTHORIZED)
        return

    token = str(hello.get("token", ""))
    user = verify_supabase_jwt(token)
    if user is None:
        await ws.close(code=WS_UNAUTHORIZED)
        return
    try:
        workspace_id = uuid.UUID(str(hello.get("workspace_id", "")))
    except ValueError:
        await ws.close(code=WS_FORBIDDEN)
        return

    # Optional per-session model override (the picker); falls back to the default.
    requested = hello.get("model")
    model = str(requested) if isinstance(requested, str) and requested.strip() else None

    # Per-session mode (Build / Ask / Auto) — selects the system prompt + tool scope.
    from sila.services.sila.concierge_service import prompt_for_mode, tools_for_mode

    mode = str(hello.get("mode") or "auto").strip().lower()
    mode_prompt = prompt_for_mode(mode)
    mode_tools = tools_for_mode(mode)

    # Built-in template persona (Data Analyst, …): the hello carries only a
    # template ID; the persona text is a server-side constant whitelisted by the
    # registry — client-supplied prompt text can never reach the system prompt.
    # Passed separately (not concatenated here) because AUTO mode re-resolves
    # the base prompt per message; the persona must survive that.
    from sila.services.bazaar_templates import persona_for, tools_for

    template_raw = hello.get("template")
    template_persona = persona_for(str(template_raw).strip()) if template_raw else ""
    template_tools = tools_for(str(template_raw).strip()) if template_raw else ()

    # Optional resume of an existing owned session (the Recents sidebar).
    resume_raw = hello.get("session_id")
    resume_id: uuid.UUID | None = None
    if isinstance(resume_raw, str) and resume_raw.strip():
        with contextlib.suppress(ValueError):
            resume_id = uuid.UUID(resume_raw)

    async with AsyncSessionLocal() as db:
        membership = await MembershipRepository(db).get(workspace_id, user.id)
        if membership is None:
            await ws.close(code=WS_FORBIDDEN)
            return
        role = Role(membership.role)

        repo = SilaRepository(db)
        if resume_id is not None and await repo.owns_session(resume_id, user.id):
            session_id = resume_id  # resuming — doesn't count against the daily limit
        else:
            if await repo.sessions_today(user.id) >= settings.sila_max_sessions_per_day:
                await ws.close(code=WS_RATE_LIMITED)
                return
            session = await repo.create_session(workspace_id=workspace_id, user_id=user.id)
            session_id = session.id
            await AuditRepository(db).record(
                action=Action.SILA_SESSION_START,
                outcome=Outcome.SUCCESS,
                actor_user_id=user.id,
                actor_workspace_id=workspace_id,
                resource_id=session_id,
            )
        await ws.send_json({"type": "ready", "session_id": str(session_id)})
        await ws.send_json({"type": "state", "value": "listening"})

        # One gateway for the session's life: its confidential flag must cover
        # every model call the conversation makes (reasoning, planner, title).
        gateway = _session_gateway(model)
        if template_raw and str(template_raw).strip() == OKR_TEMPLATE_ID:
            gateway.mark_confidential("okr template pinned")
        elif resume_id == session_id and await stash_carries_okr_sheet(
            (await repo.get_session_metadata(session_id)).get("uploaded_files"), db, user.id
        ):
            gateway.mark_confidential("resumed session holds an okr sheet")
        concierge = ConciergeService(db, gateway)

        async def emit(event: dict) -> None:  # type: ignore[type-arg]
            await ws.send_json(event)

        # LangGraph orchestrator behind the flag; default stays the loop. "graph" =
        # single supervisor; "fanout" = planner/worker fan-out around it (Slice 0:
        # fanout compiles the same supervisor graph — flag plumbing only).
        # Durable per-user memory rides every session's prompt (separate from the
        # template persona — the persona also gates behavior, memory must not).
        # A confidential (OKR) session reads no memories into its prompt.
        memories = [] if gateway.confidential else await repo.list_memories(user.id)
        memory_note = ""
        if memories:
            facts = "\n".join(f"- {m.content}" for m in memories[: settings.sila_max_memories])
            memory_note = (
                "MEMORY — durable facts about this user from earlier chats (use them; "
                f"save NEW lasting preferences with the remember tool):\n{facts}"
            )

        resumed = resume_id is not None and session_id == resume_id
        await announce_template(
            db,
            template_id=template_raw,
            resumed=resumed,
            user_id=user.id,
            workspace_id=workspace_id,
        )
        attached = await attach_skills(
            db,
            requested=hello.get("skills"),
            session_id=session_id,
            resumed=resumed,
            confidential=gateway.confidential,
            user_id=user.id,
            workspace_id=workspace_id,
        )
        skills_note = skills_prompt(attached)
        # The chat shows the session's skills as chips (a resumed chat learns
        # them here); a confidential session answers with none.
        await ws.send_json(skills_frame(attached))

        graph_runner = None
        if settings.sila_orchestrator in ("graph", "fanout"):
            from sila.services.sila.orchestrator.runner import GraphRunner, tier_for

            graph_runner = GraphRunner(
                db=db,
                gateway=gateway,
                user=user,
                role=role,
                workspace_id=workspace_id,
                session_id=session_id,
                emit=emit,
                fanout=settings.sila_orchestrator == "fanout",
                tier=tier_for(role),
                system_prompt=mode_prompt,
                # A template's whitelist beats the mode toolset — staged flows
                # must never reach the generic plan/build machinery.
                tool_names=frozenset(template_tools) if template_tools else mode_tools,
                mode=mode,  # "auto" re-routes per message (deterministic intent router)
                persona=template_persona,
                memory=memory_note,
                skills=skills_note,
            )
            # Plan→Build handoff: an accepted plan pre-seeds the build interview so
            # the gate skips what the plan already settled. Sanitized (whitelisted
            # slots, capped values) — and the gate re-validates + still requires the
            # user's confirm, so a seed can never bypass the guard. Not applicable in
            # Plan mode (its scope has no build tool).
            if mode != "plan":
                from sila.services.sila.build_slots import sanitize_seed

                seed = sanitize_seed(hello.get("build_seed"))
                if seed:
                    graph_runner.build_state.update(seed)
            # Template sessions run a deterministic propose-first flow in the data
            # tools (qwen skipped the persona's confirm stage) — mark the session.
            if template_persona:
                graph_runner.build_state["active_template"] = str(template_raw).strip()

        stt, tts = _voice()

        okr_state: dict[str, Any] = graph_runner.build_state if graph_runner else {}

        def _record_confidential() -> None:
            okr_state[CONFIDENTIAL_KEY] = True  # read by `remember` (writes nothing)

        gateway.on_confidential = _record_confidential
        if gateway.confidential:
            _record_confidential()

        async def _okr_turn(content: str) -> str | None:
            """An OKR turn by code (services/sila/okr_dispatch.py); None = converse."""
            from sila.services.bazaar_templates import persona_for, tools_for

            try:
                from sila.services.sila.okr_dispatch import OkrTurn
            except ImportError:  # an edition shipped without the OKR pack
                return None

            if graph_runner is not None and okr_state.get("active_template") != OKR_TEMPLATE_ID:
                graph_runner.use_template(
                    OKR_TEMPLATE_ID,
                    persona_for(OKR_TEMPLATE_ID),
                    frozenset(tools_for(OKR_TEMPLATE_ID)),
                )
            return await OkrTurn(
                db=db,
                user=user,
                role=role,
                workspace_id=workspace_id,
                session_id=session_id,
                build_state=okr_state,
                emit=emit,
            ).run(content)

        async def run_turn(content: str, *, speak: bool, stop: asyncio.Event) -> None:
            """One user turn: think → converse/graph → (voice turns) speak the reply.

            `stop` lets the user interrupt: converse breaks cooperatively and we
            skip TTS, returning to listening with whatever was produced so far.
            """
            await ws.send_json({"type": "state", "value": "thinking"})
            if not settings.sila_ai_enabled:
                # No model to call yet: say so as SILA's reply, not as an error —
                # framed like a streamed reply (token, then end), which is what
                # the chat renders.
                message = settings.sila_ai_unavailable_message
                await ws.send_json({"type": "token", "content": message})
                await ws.send_json({"type": "say", "value": "end", "content": message})
                await ws.send_json({"type": "state", "value": "listening"})
                return
            refusal = await admit_turn(gateway, content, db, user.id)
            if refusal is not None:
                # Refused BEFORE any model call: nothing leaves, nothing falls back.
                await ws.send_json({"type": "say", "value": "end", "content": refusal})
                await ws.send_json({"type": "state", "value": "listening"})
                return
            if gateway.confidential:
                # Confidential here means OKR (the only trigger): the pack runs by
                # code; the model only answers what the pack does not do.
                reply_or_none = await _okr_turn(content)
                if reply_or_none is not None:
                    await ws.send_json({"type": "say", "value": "end", "content": reply_or_none})
                    await ws.send_json({"type": "state", "value": "listening"})
                    return
            if graph_runner is not None:
                reply = await graph_runner.run(content)
            else:
                reply = await concierge.converse(
                    session_id=session_id,
                    workspace_id=workspace_id,
                    user=user,
                    role=role,
                    text=content,
                    emit=emit,
                    stop=stop,
                )
            if stop.is_set():
                await ws.send_json({"type": "say", "value": "end", "content": reply})
                await ws.send_json({"type": "state", "value": "listening"})
                return
            if speak and reply:
                from sila.adapters.voice.base import VoiceError

                try:
                    audio = await tts.synthesize(reply[:2000])
                    await ws.send_json({"type": "state", "value": "speaking"})
                    await ws.send_json({"type": "say", "value": "start"})
                    await ws.send_bytes(audio)
                except VoiceError as exc:
                    logger.warning("TTS degraded to text-only: %s", exc)
            await ws.send_json({"type": "say", "value": "end", "content": reply})
            await ws.send_json({"type": "state", "value": "listening"})

        # --- Conversation loop -------------------------------------------
        # The active turn runs as a task so the loop keeps receiving — that's how
        # a "stop" frame can interrupt mid-generation. `stop_event` is per-turn.
        utterance = bytearray()
        current_turn: asyncio.Task[None] | None = None
        stop_event = asyncio.Event()

        def busy() -> bool:
            return current_turn is not None and not current_turn.done()

        def _log_turn_error(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and (exc := task.exception()) is not None:
                logger.error("SILA turn task failed", exc_info=exc)

                # A crashed turn must be VISIBLE — silence reads as "SILA is dead"
                # (live: a transient DNS flake killed a turn mid-model-load and the
                # user saw nothing). Best-effort: the socket may itself be gone.
                async def _tell_user() -> None:
                    try:
                        await ws.send_json({"type": "error", "message": "turn_failed"})
                        await ws.send_json({"type": "state", "value": "listening"})
                    except Exception:  # noqa: BLE001 — socket already closed
                        pass

                asyncio.get_running_loop().create_task(_tell_user())

        def start_turn(content: str, *, speak: bool) -> None:
            nonlocal current_turn, stop_event
            stop_event = asyncio.Event()
            current_turn = asyncio.create_task(run_turn(content, speak=speak, stop=stop_event))
            current_turn.add_done_callback(_log_turn_error)

        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                # Binary frames = mic chunks for the in-flight utterance.
                if (chunk := message.get("bytes")) is not None:
                    if len(utterance) + len(chunk) <= MAX_UTTERANCE_BYTES:
                        utterance.extend(chunk)
                    continue

                raw = message.get("text")
                if not raw:
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = frame.get("type")
                if kind == "bye":
                    break
                if kind == "stop":
                    if graph_runner is not None:
                        # Graph turns aren't cooperative — cancel the task, then
                        # return to listening (state lives at the last checkpoint).
                        if current_turn is not None and not current_turn.done():
                            current_turn.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await current_turn
                        await ws.send_json({"type": "state", "value": "listening"})
                    else:
                        stop_event.set()  # loop path interrupts cooperatively
                    continue

                # Re-verify the JWT each turn — close when it expires mid-session.
                if verify_supabase_jwt(token) is None:
                    await ws.close(code=WS_UNAUTHORIZED)
                    return

                if kind == "idle_check":
                    # Conference-call etiquette: the client noticed silence;
                    # SILA gently checks in (canned, no LLM round-trip).
                    idle_line = "Are you still there? I'm here whenever you need me."
                    from sila.adapters.voice.base import VoiceError

                    try:
                        audio = await tts.synthesize(idle_line)
                        await ws.send_json({"type": "state", "value": "speaking"})
                        await ws.send_json({"type": "say", "value": "start"})
                        await ws.send_bytes(audio)
                    except VoiceError:
                        pass
                    await ws.send_json({"type": "say", "value": "end", "content": idle_line})
                    await ws.send_json({"type": "state", "value": "listening"})
                elif kind == "text":
                    content = str(frame.get("content", "")).strip()
                    if content and not busy():
                        start_turn(content, speak=False)
                elif kind == "ask_user_response":
                    # The bottom-sheet wizard's selections resume the turn. (This
                    # is where a LangGraph interrupt() resume will plug in.)
                    answers = frame.get("answers")
                    if isinstance(answers, dict) and answers and not busy():
                        # Natural resume text — the user's VALUES only, never the internal
                        # slot ids (so it reads like a person, not "__confirm__ = Yes").
                        readable = "; ".join(
                            ", ".join(v) if isinstance(v, list) else str(v)
                            for v in answers.values()
                        )
                        # Part B — if the build GATE asked a requirement slot, record the
                        # answer into build_state deterministically (don't trust the model
                        # to remember it). __confirm__ flips the confirmed flag.
                        if graph_runner is not None:
                            _record_slot_answer(graph_runner.build_state, answers)
                        start_turn(f"{readable}. Please continue.", speak=False)
                elif kind == "credentials_response":
                    # Incognito panel: the values create the credential in its ENCRYPTED
                    # store and are then dropped — NEVER logged, persisted, or sent to the
                    # model. The graph resumes with a non-secret handle only.
                    provider = str(frame.get("provider", "")).strip().lower()
                    values = frame.get("values")
                    if isinstance(values, dict) and not values and provider and not busy():
                        # The user dismissed the panel — continue without the integration.
                        start_turn(f"Skipped {provider} — building without it.", speak=False)
                    elif isinstance(values, dict) and provider and not busy():
                        from sila.services.sila.credential_service import connect

                        result = None
                        try:
                            result = await connect(
                                provider=provider,
                                values={str(k): str(v) for k, v in values.items()},
                                user=user,
                                workspace_id=workspace_id,
                                db=db,
                            )
                        except Exception as exc:  # noqa: BLE001 — never leak the secret
                            logger.warning(
                                "SILA credential connect failed (%s): %s",
                                provider,
                                type(exc).__name__,
                            )
                        del values  # drop the secret promptly
                        if result is not None:
                            if graph_runner is not None and result.wiring is not None:
                                graph_runner.pending_credentials[provider] = result.wiring
                            start_turn(
                                f"{result.label} is now connected. Finish building the "
                                "agent now (use the same details as before).",
                                speak=False,
                            )
                        else:
                            start_turn(
                                f"Couldn't connect {provider} — build without it for now.",
                                speak=False,
                            )
                elif kind == "email_confirm_response":
                    # The Outlook confirm panel: Send delivers the stashed draft
                    # (the pause node dispatches confirm_send_email); anything else
                    # cancels. No email content rides this frame — just the decision.
                    if not busy():
                        if bool(frame.get("confirmed")):
                            start_turn("__confirm_email__ Send the email now.", speak=False)
                        else:
                            start_turn("Don't send that email — I cancelled it.", speak=False)
                elif kind == "connect_response":
                    # WS-9 — the user finished (or cancelled) the Nango Connect flow.
                    # The binding + resource choice were already persisted via the REST
                    # endpoints; here we only resume the paused build. No secret is ever
                    # carried over this frame — just the provider + an ok flag.
                    provider = str(frame.get("provider", "")).strip().lower()
                    ok = bool(frame.get("ok", True))
                    if provider and not busy():
                        if ok:
                            start_turn(
                                f"{provider} is now connected. Finish building the agent "
                                "now (use the same details as before).",
                                speak=False,
                            )
                        else:
                            start_turn(f"Skipped {provider} — building without it.", speak=False)
                elif kind == "voice_end":
                    audio, utterance = bytes(utterance), bytearray()
                    if not audio or busy():
                        continue
                    from sila.adapters.voice.base import VoiceError

                    await ws.send_json({"type": "state", "value": "thinking"})
                    try:
                        transcript = (await stt.transcribe(audio)).strip()
                    except VoiceError as exc:
                        logger.warning("STT failed, staying in text mode: %s", exc)
                        await ws.send_json({"type": "error", "message": "voice_unavailable"})
                        await ws.send_json({"type": "state", "value": "listening"})
                        continue
                    if not transcript:
                        await ws.send_json({"type": "state", "value": "listening"})
                        continue
                    await ws.send_json({"type": "transcript", "content": transcript})
                    start_turn(transcript, speak=True)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 — log, close cleanly, never leak details
            logger.exception("SILA WS session crashed")
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "message": "internal"})
        finally:
            if current_turn is not None and not current_turn.done():
                current_turn.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await current_turn
            await repo.end_session(session_id)
