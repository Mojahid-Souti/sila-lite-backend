"""Drive the supervisor graph for one WebSocket connection.

Maps `graph.astream(stream_mode="custom")` events → the SILA WS frames (the same
token/tool/ui/ask_user shapes the loop emits), and decides per input whether to
start a NEW turn or RESUME a paused interrupt (from the checkpointer state). The
interrupt payload becomes the ask_user / open_modal frame; the next user message
becomes the `Command(resume=…)` value.

Complexity: O(events) per turn; state lives on the checkpointer (thread=user+session).
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import uuid
from typing import Any

import structlog
from langgraph.types import Command

from sila.adapters.auth.base import UserResult
from sila.adapters.llm.base import LLMDone, LLMGateway
from sila.core.config import settings
from sila.core.constants import Action, Outcome, ResourceType, Role
from sila.core.cost_telemetry import cost_metadata
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.sila import SilaRepository
from sila.services.sila.orchestrator.checkpointer import get_checkpointer
from sila.services.sila.orchestrator.graph import (
    _CONFIRM_EMAIL_SENTINEL,
    GraphDeps,
    build_fanout_graph,
    build_supervisor_graph,
)

log = structlog.get_logger("sila.orchestrator")

_RECURSION_LIMIT = 24  # ~8 agent↔tools rounds; bounds runaway loops

# Pace the visible trace steps so each one is FELT — the concierge loop paces, but the
# graph runner forwarded frames with no delay, so steps flashed by. Tunable here.
_STEP_PACE_S = 0.5

Emit = Any  # Callable[[dict], Awaitable[None]]

# Tier filter (pre-read §3). The single serializer every frame flows through. A
# Beginner socket must never *receive* expert-only content — enforced here,
# server-side, not in the frontend. Two conventions, both honored by fan-out:
#   - frame["audience"] == "expert"  → the whole frame is dropped for a Beginner.
#   - frame["expert"] = {…}          → that nested detail is stripped for a Beginner
#                                       (the friendly fields stay).


def serialize_frame(ev: dict[str, Any], tier: str) -> dict[str, Any] | None:
    """Return the frame to send to a `tier` ("beginner"|"expert") socket, a
    Beginner-redacted copy, or None to drop it entirely. Experts see everything."""
    if tier == "expert":
        return ev
    if ev.get("audience") == "expert":
        return None
    if "expert" in ev or "audience" in ev:
        return {k: v for k, v in ev.items() if k not in ("expert", "audience")}
    return ev


def tier_for(role: Role) -> str:
    """UX-exposure tier (NOT a privilege — RLS/OPA gate regardless). Builders and
    operators get the expandable expert trace; end-users get the friendly one."""
    return "expert" if role in (Role.BUILDER, Role.REVIEWER, Role.ADMIN) else "beginner"


ATTACH_RE = re.compile(
    r"\[Attached file: (?P<name>[^\]]+)\]\n(?P<body>.*?)\n\[End of (?P=name)\]",
    re.DOTALL,
)
# Analysis Engine P0: big files upload as DATASETS (bytes on disk, DuckDB reads
# the full file) — the message carries only this one-line handle.
DATASET_RE = re.compile(
    r"\[Attached dataset: (?P<name>.+?) dataset_id=(?P<id>[0-9a-fA-F-]{36}) rows=(?P<rows>\d+)\]"
)
_MAX_STASHED_FILES = 5
# The composer sends at most this many characters of a tabular file inline
# (ATTACH_CSV_MAX in sila-chat.tsx); bigger files upload as datasets. A body
# longer than this was cut somewhere and is marked `truncated`.
_MAX_STASH_CHARS = 150_000
# build_state keys persisted on the session row across reconnects: the attachment
# stash, the Grafana dashboard identity (same dashboard keeps updating), and the
# ACCUMULATED chart edits (each "add a chart" appends; nothing is lost between turns).
_DURABLE_STATE_KEYS = (
    "uploaded_files",
    "grafana_dash_uid",
    "grafana_dash_title",
    "dashboard_extra_charts",
    # Power BI push-dataset identity — same-session re-runs refresh in place.
    "powerbi_dataset_id",
    "powerbi_dataset_title",
    # Analysis Engine P1 — the approved AnalysisPlan + objective survive a
    # reconnect (otherwise every drop would re-gate an already-approved build).
    "analysis_plan",
    "analysis_objective",
    # Analysis Engine P2 — the pending proposal, the APPLIED recipe (replayable),
    # and the dataset-id → cleaned-copy paths.
    "proposed_cleaning",
    "cleaning_recipe",
    "cleaned_datasets",
    # The email awaiting the user's Send click. Without this the draft lives only
    # in connection memory, so ANY reconnect between "here's your draft" and the
    # Send click loses it — the confirm gate in graph.pause then sees no
    # pending_email, the __confirm_email__ sentinel falls through to the model,
    # and it answers by inventing a draft from its own schema example
    # (recipient@example.com / "Your Subject Here"). That is the confirm loop.
    #
    # `email_confirmed` is deliberately NOT durable: it is set by the pause gate
    # and read by confirm_send_email inside the SAME run, so it never needs to
    # survive a reconnect — and a stale True landing on a later draft would send
    # mail the user never clicked Send on.
    "pending_email",
    # Analysis Engine P3 — accumulated typed findings (Phase 4 ranks them).
    "findings",
    # Analysis Engine P4 — ranked insight cards + the executive summary block.
    "insights",
    "exec_summary",
    # Analysis Engine P5 — the metric registry, the last dashboard's exact
    # figures (the report quotes them), and the trimmed chart copy explain uses.
    "semantic_layer",
    "dashboard_stats",
    "last_dashboard",
    "quality_summary",
    # Analysis Engine P6 — the monitoring contract: baseline, alert rules, and
    # the exported pipeline the user-owned agent copy inherits.
    "monitor_baseline",
    "alert_rules",
    "monitor_config",
    # PDD template — the whole document session (source, roster, content,
    # gaps) survives reconnects like everything else.
    "pdd",
)


_MODEL_SAMPLE_LINES = 20  # attachment rows the MODEL sees (tools get the full file)


_MODEL_PROSE_CHARS = 1_500  # transcript preview the MODEL sees (tools get it all)


def _shrink_tabular_attachments(content: str) -> str:
    """Replace big attachment bodies with a head sample in the text the MODEL
    reads. The full bytes stay in the stash for the tools — csv/tsv keep a
    row sample; transcripts (.txt/.md/.vtt) keep a short preview (a full 17k
    transcript in the prompt would blow the 12k context — live bug once the
    composer stopped truncating them)."""

    def _shrink(m: re.Match[str]) -> str:
        name, body = m.group("name"), m.group("body")
        lowered = name.strip().lower()
        if lowered.endswith((".csv", ".tsv")):
            # Records, not lines: a quoted cell with line breaks is one row.
            # Counting lines once showed 10 of 18 key results as "20 rows".
            records = _records(body, "\t" if lowered.endswith(".tsv") else ",")
            if len(records) <= _MODEL_SAMPLE_LINES:
                return m.group(0)
            head = _as_csv(records[:_MODEL_SAMPLE_LINES], "\t" if lowered.endswith(".tsv") else ",")
            note = (
                f"... ({len(records) - _MODEL_SAMPLE_LINES} more rows not shown — the FULL "
                "file is available to the dashboard/data tools)"
            )
            return f"[Attached file: {name}]\n{head}\n{note}\n[End of {name}]"
        if lowered.endswith((".txt", ".md", ".vtt")) and len(body) > _MODEL_PROSE_CHARS:
            head = body[:_MODEL_PROSE_CHARS].rsplit("\n", 1)[0]
            note = (
                f"... ({len(body) - len(head):,} more characters not shown — the FULL "
                "text is available to the document tools)"
            )
            return f"[Attached file: {name}]\n{head}\n{note}\n[End of {name}]"
        return m.group(0)

    return ATTACH_RE.sub(_shrink, content)


def _records(body: str, delimiter: str) -> list[list[str]]:
    """The file's non-empty CSV records (quoted line breaks stay inside a cell)."""
    reader = csv.reader(io.StringIO(body.removeprefix("\ufeff")), delimiter=delimiter)
    try:
        return [row for row in reader if any(cell.strip() for cell in row)]
    except csv.Error:
        return [line.split(delimiter) for line in body.splitlines() if line.strip()]


def _as_csv(records: list[list[str]], delimiter: str) -> str:
    out = io.StringIO()
    csv.writer(out, delimiter=delimiter, lineterminator="\n").writerows(records)
    return out.getvalue().rstrip("\n")


def stash_attachments(content: str, build_state: dict[str, Any]) -> bool:
    """Keep the raw text of files the user attached ({name, content}) on the shared
    build_state, so data tools (e.g. the CSV dashboard) work on the REAL bytes —
    never on the model's paraphrase. New uploads replace the stash; a message with
    no attachments leaves it alone (follow-ups can rebuild from the same file).
    Returns True when new files arrived (the caller persists them)."""
    found: list[dict[str, Any]] = [
        {
            "name": m.group("name").strip(),
            "content": m.group("body")[:_MAX_STASH_CHARS],
            # A cut file must never be read as the whole (the OKR pack refuses it).
            **({"truncated": True} if len(m.group("body")) > _MAX_STASH_CHARS else {}),
        }
        for m in ATTACH_RE.finditer(content)
    ]
    found += [
        {
            "name": m.group("name").strip(),
            "content": "",
            "dataset_id": m.group("id"),
            "rows": int(m.group("rows")),
        }
        for m in DATASET_RE.finditer(content)
    ]
    if found:
        build_state["uploaded_files"] = found[:_MAX_STASHED_FILES]
        return True
    return False


def _push_thinking(timeline: list[dict[str, Any]], text: str) -> None:
    """Merge consecutive reasoning into one 'thinking' segment (like _push_text)."""
    if timeline and timeline[-1].get("type") == "thinking":
        timeline[-1]["text"] += text
    else:
        timeline.append({"type": "thinking", "text": text})


def _push_text(timeline: list[dict[str, Any]], text: str) -> None:
    """Mirror the frontend's pushText: merge consecutive text into one segment."""
    if timeline and timeline[-1].get("type") == "text":
        timeline[-1]["text"] += text
    else:
        timeline.append({"type": "text", "text": text})


def _upsert_tool(
    timeline: list[dict[str, Any]],
    name: str,
    status: str,
    result: Any,
    note: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    """Mirror the frontend's upsertTool: a 'running' opens a step; a 'progress' updates
    the open step's note (live stage text); a 'done' closes the matching open one (or
    appends a finished step). Builds the same collapsed timeline the live view shows."""
    if status == "running":
        timeline.append({"type": "tool", "name": name, "status": "running"})
        return
    if status == "progress":
        for seg in reversed(timeline):
            if (
                seg.get("type") == "tool"
                and seg.get("name") == name
                and seg.get("status") == "running"
            ):
                if note:
                    seg["note"] = note
                return
        return
    for seg in reversed(timeline):
        if seg.get("type") == "tool" and seg.get("name") == name and seg.get("status") == "running":
            seg["status"] = "done"
            if result is not None:
                seg["result"] = result
            # Citations are persisted with the step, so reopening the session
            # still shows what the answer was based on.
            if sources:
                seg["sources"] = sources
            if artifacts:
                seg["artifacts"] = artifacts
            return
    seg: dict[str, Any] = {"type": "tool", "name": name, "status": "done"}
    if result is not None:
        seg["result"] = result
    if sources:
        seg["sources"] = sources
    if artifacts:
        seg["artifacts"] = artifacts
    timeline.append(seg)


class _CostMeter:
    """Accumulates one TURN's cost across every LLM call and tool call it makes.

    COSTING UNIT = THE TURN (one user message -> one complete assistant response),
    not the conversation and not the individual LLM call. Rationale: a turn is the
    only unit a customer can predict — they send one message and get one answer.
    A conversation is unbounded (its cost depends on how long someone keeps
    talking), and an individual LLM call is invisible to the user because a single
    turn silently fans out into extra calls whenever a tool fires. `llm_turns`
    records that internal fan-out so the hidden cost stays attributable.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.prefill_s = 0.0
        self.decode_s = 0.0
        self.load_s = 0.0
        self.total_s = 0.0
        self.model = ""
        self.first_turn_input = 0
        self.tool_calls: list[tuple[str, float]] = []
        self._tool_started: dict[str, float] = {}

    def add_usage(self, u: dict[str, Any]) -> None:
        if not u:
            return
        self.llm_calls += 1
        inp = int(u.get("input_tokens") or 0)
        if self.first_turn_input == 0:
            self.first_turn_input = inp
        self.input_tokens += inp
        self.output_tokens += int(u.get("output_tokens") or 0)
        self.reasoning_tokens += int(u.get("reasoning_tokens") or 0)
        self.prefill_s += float(u.get("prefill_seconds") or 0)
        self.decode_s += float(u.get("decode_seconds") or 0)
        self.load_s += float(u.get("load_seconds") or 0)
        self.total_s += float(u.get("total_seconds") or 0)
        self.model = str(u.get("model") or self.model)

    def tool_started(self, name: str) -> None:
        self._tool_started[name] = time.perf_counter()

    def tool_done(self, name: str) -> None:
        t0 = self._tool_started.pop(name, None)
        if t0 is not None:
            self.tool_calls.append((name, time.perf_counter() - t0))

    def usage_block(self) -> dict[str, Any]:
        """The same canonical shape agent.test / public.invoke record, so the
        cost aggregation needs no special-casing for the concierge."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "prefill_seconds": round(self.prefill_s, 6),
            "decode_seconds": round(self.decode_s, 6),
            "active_seconds": round(self.prefill_s + self.decode_s, 6),
            "load_seconds": round(self.load_s, 6),
            "total_seconds": round(self.total_s, 6),
            "engine": "ollama",
            "model": self.model,
        }


class _MeteredGateway:
    """Transparent LLMGateway proxy that feeds a _CostMeter. Wrapping here keeps
    the graph untouched — every LLM call in the turn goes through this seam."""

    def __init__(self, inner: LLMGateway, meter: _CostMeter) -> None:
        self._inner = inner
        self._meter = meter

    async def stream_chat(self, **kwargs: Any) -> Any:
        async for chunk in self._inner.stream_chat(**kwargs):
            if isinstance(chunk, LLMDone):
                self._meter.add_usage(chunk.usage or {})
            yield chunk

    async def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.complete_json(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:  # delegate anything else
        return getattr(self._inner, item)


class GraphRunner:
    def __init__(
        self,
        *,
        db: Any,  # AsyncSession
        gateway: LLMGateway,
        user: UserResult,
        role: Role,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        emit: Emit,
        fanout: bool = False,
        tier: str = "expert",
        system_prompt: str | None = None,
        tool_names: frozenset[str] | None = None,
        mode: str = "",
        persona: str = "",
        memory: str = "",
        skills: str = "",
    ) -> None:
        deps_extra: dict[str, Any] = {}
        if system_prompt is not None:
            deps_extra["system_prompt"] = system_prompt
        deps_extra["tool_names"] = tool_names
        deps_extra["mode"] = mode
        deps_extra["persona"] = persona
        deps_extra["memory"] = memory
        deps_extra["skills"] = skills
        # Meter every LLM call in the turn (cost telemetry — the concierge was the
        # one runtime recording nothing, so its workload was invisible to the model).
        self._meter = _CostMeter()
        self._audit = AuditRepository(db)
        self._deps = GraphDeps(
            gateway=_MeteredGateway(gateway, self._meter),
            audit=AuditRepository(db),
            user=user,
            role=role,
            workspace_id=workspace_id,
            **deps_extra,
        )
        self._repo = SilaRepository(db)
        self._workspace_id = workspace_id
        self._session_id = session_id
        self._raw_emit = emit
        self._tier = tier
        self._fanout = fanout
        self._graph: Any = None
        # The fan-out graph's deepest branch is bounded by its own caps; give it a
        # larger backstop recursion_limit than the single supervisor.
        self._config = {
            "configurable": {"thread_id": f"{user.id}:{session_id}"},
            "recursion_limit": (
                settings.sila_fanout_recursion_limit if fanout else _RECURSION_LIMIT
            ),
        }

    @property
    def pending_credentials(self) -> dict[str, tuple[str, str]]:
        """Shared dict the WS handler fills on credentials_response; the build tool
        reads it (via ToolContext) to wire credentials into the workflow nodes."""
        return self._deps.pending_credentials

    @property
    def build_state(self) -> dict[str, Any]:
        """Shared build-interview state; the WS handler records ask_user answers into
        it (deterministic slot gate). The build tool reads it via ToolContext."""
        return self._deps.build_state

    def use_template(self, template_id: str, persona: str, tool_names: frozenset[str]) -> None:
        """Switch the session to a template mid-conversation (the graph reads its
        persona and tool whitelist every round). An attached OKR sheet does this:
        from then on the model has the OKR tools only, and no memories."""
        self._deps.persona = persona
        self._deps.tool_names = tool_names
        # Memories from other chats — and skills — do not steer a template
        # session that turned confidential mid-way (the OKR case).
        self._deps.memory = ""
        self._deps.skills = ""
        self._deps.build_state["active_template"] = template_id

    async def _send(self, ev: dict[str, Any]) -> None:
        """Every outbound frame goes through the tier filter (pre-read §3)."""
        out = serialize_frame(ev, self._tier)
        if out is not None:
            # Structural frames are logged (tokens are noise) so a UI report like
            # "the sheet rendered twice / never proceeded" is diagnosable from the
            # exact frames sent — count the ask_user rows for the turn.
            if out.get("type") != "token":
                log.info(
                    "sila.frame",
                    frame=out.get("type"),
                    command=out.get("command"),
                    name=out.get("name"),
                    status=out.get("status"),
                    request_id=out.get("request_id"),
                    session=str(self._session_id)[:8],
                )
            await self._raw_emit(out)

    async def _ensure_graph(self) -> Any:  # noqa: ANN401
        if self._graph is None:
            checkpointer = await get_checkpointer()
            builder = build_fanout_graph if self._fanout else build_supervisor_graph
            self._graph = builder(self._deps, checkpointer)
        return self._graph

    async def run(self, content: str) -> str:
        """Run a turn (or resume a paused one); return the streamed display text."""
        self._meter.reset()  # cost is accumulated PER TURN (see _CostMeter)
        _turn_t0 = time.perf_counter()
        # Durable turn state (attachment stash + dashboard identity/charts) lives in
        # connection memory but must survive a reconnect — restore from the session
        # row on the first turn, persist changes after every turn.
        if stash_attachments(content, self._deps.build_state):
            await self._repo.save_uploaded_files(
                self._session_id, self._deps.build_state["uploaded_files"]
            )
        # One tick per USER input — the Analysis-Engine plan gate uses it to
        # require that an approval round-tripped through the user's turn (the
        # model cannot approve its own plan by calling the tool twice).
        self._deps.build_state["turn_seq"] = int(self._deps.build_state.get("turn_seq", 0)) + 1
        meta = await self._repo.get_session_metadata(self._session_id)
        for key in _DURABLE_STATE_KEYS:
            if key not in self._deps.build_state and key in meta:
                self._deps.build_state[key] = meta[key]
        content = _shrink_tabular_attachments(content)
        graph = await self._ensure_graph()
        paused = bool((await graph.aget_state(self._config)).next)
        # A Send click whose draft is gone (expired session, a reconnect older
        # than the stash) must NOT reach the model: with no draft to confirm it
        # answers by inventing one from its schema example, then that fake draft
        # asks to be confirmed too, and the pair loops. Answer deterministically
        # instead — no model call, no cost, no invented recipient.
        if _CONFIRM_EMAIL_SENTINEL in content and not (
            paused and self._deps.build_state.get("pending_email")
        ):
            reply = (
                "That draft is no longer open, so I didn't send anything. "
                "Tell me the recipient and what to say and I'll draft it again."
            )
            await self._repo.add_message(
                session_id=self._session_id,
                workspace_id=self._workspace_id,
                role="user",
                content=content,
            )
            await self._send({"type": "token", "content": reply})
            await self._repo.add_message(
                session_id=self._session_id,
                workspace_id=self._workspace_id,
                role="sila",
                content=reply,
            )
            return reply
        # Persist EVERY user input — a new turn AND a resume (the answers a question
        # sheet produced, or the upload confirmation) — so reopening the session shows
        # the whole conversation, not just the first question and the last reply.
        await self._repo.add_message(
            session_id=self._session_id,
            workspace_id=self._workspace_id,
            role="user",
            content=content,
        )
        stream_input: Any = (
            Command(resume=content)
            if paused
            else {"messages": [{"role": "user", "content": content}]}
        )

        display: list[str] = []
        # NOTE: the persisted timeline is built from the RAW frame (pre-tier-filter), so
        # it stores full detail. Once fan-out attaches expert-only fields, the rehydrate
        # path (GET session) must re-apply serialize_frame(step, tier) — addressed when
        # expert detail first lands on a persisted frame (Slice 5/6), not before.
        timeline: list[dict[str, Any]] = []  # collapsed trace (text + tool steps)
        async for ev in graph.astream(stream_input, self._config, stream_mode="custom"):
            if isinstance(ev, dict):
                kind = ev.get("type")
                if kind == "token":
                    chunk = str(ev.get("content", ""))
                    display.append(chunk)
                    _push_text(timeline, chunk)
                elif kind == "thinking":
                    # Reasoning (<think>…</think>) — persisted in the trace (the
                    # collapsible "thinking" section), never in the reply text.
                    _push_thinking(timeline, str(ev.get("content", "")))
                elif kind == "tool":
                    # Time each tool from its own running/done frames — non-GPU
                    # wall clock, attributable per connector (measured 6.5-26 s
                    # for one external MCP endpoint).
                    _tname, _tstatus = str(ev.get("name", "")), ev.get("status", "")
                    if _tstatus == "running":
                        self._meter.tool_started(_tname)
                    elif _tstatus == "done":
                        self._meter.tool_done(_tname)
                    _upsert_tool(
                        timeline,
                        ev.get("name", ""),
                        ev.get("status", ""),
                        ev.get("result"),
                        note=ev.get("note"),
                        sources=ev.get("sources"),
                        artifacts=ev.get("artifacts"),
                    )
                elif kind == "ui" and ev.get("command") == "open_canvas":
                    # The built workflow renders inline in the trace; persist it so the
                    # canvas block is still there when the session is reopened.
                    timeline.append(
                        {
                            "type": "canvas",
                            "workflowId": ev.get("workflow_id"),
                            "definition": ev.get("definition"),
                        }
                    )
                elif kind == "ui" and ev.get("command") == "open_dashboard":
                    # The in-Sila data dashboard — persist so reopening replays it.
                    timeline.append({"type": "dashboard", "dashboard": ev.get("dashboard")})
                elif kind == "ui" and ev.get("command") == "open_plan":
                    # The Plan-mode plan card — persist it so a reopened session shows
                    # the proposal read-only (the request_id is stale, so no buttons).
                    timeline.append(
                        {
                            "type": "plan",
                            "plan": ev.get("plan"),
                            "requestId": ev.get("request_id"),
                        }
                    )
                elif kind == "ui" and ev.get("command") == "open_preview":
                    # The dry-run sample ("show me before build") — persist it too. On a
                    # reopened session the request_id is stale (the pause is gone), so the
                    # block renders read-only; live, it carries the Accept/Adjust buttons.
                    timeline.append(
                        {
                            "type": "preview",
                            "kind": ev.get("kind"),  # "confirm" = spec-only block
                            "output": ev.get("output"),
                            "destination": ev.get("destination"),
                            "spec": ev.get("spec"),
                            "requestId": ev.get("request_id"),
                        }
                    )
                await self._send(ev)
                # A beat when each step STARTS, so the trace reveals one step at a time
                # (cancellable — a `stop` cancels the whole turn task).
                if kind == "tool" and ev.get("status") == "running":
                    await asyncio.sleep(_STEP_PACE_S)

        state = await graph.aget_state(self._config)
        paused_now = bool(state.next)
        if paused_now:  # paused at an interrupt → surface it as a frame
            payload = state.tasks[0].interrupts[0].value
            await self._emit_interrupt(payload)
            # A FREEFORM ask renders as plain chat (no sheet) — persist its question
            # as this turn's text so the rehydrated transcript keeps the exchange.
            if payload.get("command") == "ask_user" and payload.get("freeform"):
                questions = payload.get("questions") or []
                question = str((questions[0] if questions else {}).get("question") or "")
                if question:
                    display.append(("\n\n" if display else "") + question)
            # The Outlook draft card — persist it in the trace so a reopened session
            # still shows what was drafted (read-only; the request_id is stale).
            elif payload.get("command") == "confirm_email" and payload.get("draft"):
                timeline.append(
                    {
                        "type": "email",
                        "draft": payload.get("draft"),
                        "requestId": payload.get("request_id"),
                    }
                )

        # Persist THIS turn's assistant text — the narration on a paused turn or the
        # final reply on a finished one — so the rehydrated transcript is complete.
        # Also persist the collapsed trace IF the turn had tool steps, so reopening
        # the session replays the vetting/tool timeline (not just the text).
        # Persist the durable dashboard state a tool may have changed this turn
        # (identity + accumulated chart edits) — survives reconnects.
        patch = {
            k: self._deps.build_state[k]
            for k in _DURABLE_STATE_KEYS
            if k != "uploaded_files" and k in self._deps.build_state
        }
        if patch:
            await self._repo.merge_session_metadata(self._session_id, patch)

        # COST TELEMETRY for the concierge turn. Same canonical schema as
        # agent.test / public.invoke, so the cost aggregation needs no special
        # case. Recorded as `agent.test` with surface="concierge" — the action
        # stays one of the two the cost query already counts, and `surface`
        # distinguishes it. Fire-and-forget: costing must never break a turn.
        try:
            if self._meter.llm_calls:
                await self._audit.record(
                    action=Action.AGENT_TEST,
                    outcome=Outcome.SUCCESS,
                    actor_user_id=self._deps.user.id,
                    actor_workspace_id=self._workspace_id,
                    resource_type=ResourceType.WORKSPACE,
                    resource_id=self._workspace_id,
                    metadata=cost_metadata(
                        self._meter.usage_block(),
                        # NO agent_id: a concierge turn is not an agent invocation.
                        # Attributing it to the session id made every turn look like
                        # its own one-run "agent" in cost_by_agent. The concierge is
                        # identified by surface="concierge" instead, and its cost
                        # lands in the workspace total where it belongs.
                        agent_id=None,
                        latency_ms=(time.perf_counter() - _turn_t0) * 1000,
                        # Where the turn's calls actually went: a hosted model
                        # was once recorded as "ollama" here.
                        engine=str(getattr(self._deps.gateway, "engine", "ollama")),
                        surface="concierge",
                        llm_turns=self._meter.llm_calls,
                        tool_calls=self._meter.tool_calls,
                        first_turn_input_tokens=self._meter.first_turn_input,
                    ),
                )
        except Exception:  # noqa: BLE001 — telemetry never breaks the conversation
            log.warning("sila.cost_telemetry_failed")

        text = "".join(display).strip()
        # A turn is worth persisting if it produced ANY visible artifact — text OR a
        # trace step/card. The old `if text` gate silently dropped tool-only turns
        # (e.g. "Emailed X", a dashboard, a finished build with no closing sentence),
        # so reopening the session lost them. Persist whenever either exists.
        has_steps = any(
            s.get("type") in ("tool", "canvas", "preview", "plan", "thinking", "dashboard", "email")
            for s in timeline
        )
        if text or has_steps:
            await self._repo.add_message(
                session_id=self._session_id,
                workspace_id=self._workspace_id,
                role="sila",
                content=text,
                trace=timeline if has_steps else None,
            )
        # Title ONLY when the turn isn't waiting on the user. The title LLM call is
        # slow; running it on a paused turn keeps the turn "busy" past the interrupt,
        # so the WS busy-guard drops the user's upload/answer resume and the build
        # hangs. Defer it to a finished (non-paused) turn.
        if not paused_now and await self._repo.needs_title(self._session_id):
            await self._maybe_title()
        return text

    async def _maybe_title(self) -> None:
        """Best-effort: name the session once it has a couple of turns (Recents)."""
        try:
            msgs = await self._repo.recent_messages(self._session_id, limit=6)
            convo = "\n".join(
                f"{m.role}: {m.content[:200]}" for m in msgs if m.role in ("user", "sila")
            )
            out = await self._deps.gateway.complete_json(
                system_prompt=(
                    "Write a concise 3–5 word title for this chat. Respond as JSON "
                    '{"title": "..."} — Title Case, no punctuation.'
                ),
                user_prompt=convo,
                timeout_s=20.0,
            )
            title = str(out.get("title") or "").strip()
            if title:
                await self._repo.set_title(self._session_id, title)
        except Exception:  # noqa: BLE001 — titling never breaks the turn
            log.warning("sila.graph.title_failed")

    async def _emit_interrupt(self, payload: dict[str, Any]) -> None:
        cmd = payload.get("command")
        if cmd == "ask_user":
            await self._send(
                {
                    "type": "ask_user",
                    "request_id": payload["request_id"],
                    "questions": payload["questions"],
                    # Freeform asks render as plain chat (no docked sheet); the
                    # composer's next message is the answer.
                    "freeform": bool(payload.get("freeform")),
                }
            )
        elif cmd == "request_credentials":
            await self._send(
                {
                    "type": "request_credentials",
                    "request_id": payload["request_id"],
                    "provider": payload.get("provider"),
                    "label": payload.get("label"),
                    "fields": payload.get("fields", []),
                }
            )
        elif cmd == "connect_provider":
            # WS-9 — the build needs an OAuth app the user hasn't connected. The
            # client runs the Nango Connect flow + resource picker, then resumes.
            await self._send(
                {
                    "type": "request_connect",
                    "request_id": payload["request_id"],
                    "provider": payload.get("provider"),
                    "label": payload.get("label"),
                }
            )
        elif cmd == "open_modal":
            await self._send(
                {
                    "type": "ui",
                    "command": "open_modal",
                    "modal": payload.get("modal"),
                    "prompt": payload.get("prompt"),
                }
            )
        elif cmd:
            await self._send({"type": "ui", **payload})
