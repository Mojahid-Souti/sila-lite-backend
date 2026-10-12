"""SILA supervisor graph — the LangGraph runtime for the concierge.

Three nodes so the LLM streams exactly once per round (interrupt() re-executes
its node on resume, so the streaming LLM must NOT live in the interrupting node):

    agent  — stream the LLM once; decide tool calls; emit token/tool frames.
    tools  — run non-pausing tools; feed results back.
    pause  — interrupt() for a pausing tool (ask_user / request_documents); on
             resume, append the user's response and loop back to agent.

State persists on the Postgres checkpointer (thread = user+session), so a pause
survives a disconnect. Frames are emitted via get_stream_writer() in the exact
shapes the WS contract already uses (token/tool/ui/ask_user).

Complexity: O(tokens) per round; rounds bounded by MAX_SILA_TOOL_ITERATIONS.
"""

from __future__ import annotations

import json
import operator
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, TypedDict

import structlog
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from sila.adapters.auth.base import UserResult
from sila.adapters.llm.base import LLMDone, LLMGateway, LLMGatewayError, LLMTextDelta, LLMToolCall
from sila.core.config import settings
from sila.core.constants import (
    MAX_SILA_TOOL_ITERATIONS,
    Action,
    Outcome,
    ResourceType,
    Role,
)
from sila.db.repositories.audit import AuditRepository
from sila.services.sila.concierge_service import (
    _PAUSING_TOOLS,
    _SYSTEM_PROMPT,
    _TOOL_NARRATIONS,
    language_note,
)
from sila.services.sila.mode_router import (
    INTENT_BUILD,
    resolve_auto,
    route_intent,
    split_attachments,
    wants_data_dashboard,
)
from sila.services.sila.orchestrator.budget import TurnBudget
from sila.services.sila.orchestrator.planner import (
    COMPLEX,
    SIMPLE,
    classify_complexity,
    decompose,
)
from sila.services.sila.think_stream import ThinkSplitter
from sila.services.sila.tool_registry import (
    WORKER_TOOL_NAMES,
    ToolContext,
    execute_tool,
    tool_schemas,
)

# The frontend's Send click resumes the turn with this marker. It is a protocol
# token, never prose for the model: if it ever reaches the LLM the model answers
# by inventing an email from its own schema example, which is what produced the
# recipient@example.com confirm loop.
_CONFIRM_EMAIL_SENTINEL = "__confirm_email__"


# Pausing UI commands a worker must NOT trigger (§C: workers are pause-free — they
# fail gracefully instead of interrupting inside a Send branch).
def tool_done_frame(name: str, result: Any) -> dict[str, Any]:
    """The `done` frame for one tool, including anything it produced.

    Built in one place because there are two tool paths — the normal round and
    the pausing one — and the pausing path used to carry only the summary. A
    tool that both pauses and produces a file dropped its attachment there,
    which is exactly the kind of gap a second literal invites.
    """
    frame: dict[str, Any] = {
        "type": "tool",
        "name": name,
        "status": "done",
        "result": result.summary,
    }
    meta = result.meta or {}
    # Citations for an answer, and whatever the turn handed back — both travel as
    # data so the UI renders them whether or not the model mentions them.
    if meta.get("sources"):
        frame["sources"] = meta["sources"]
    if meta.get("artifacts"):
        frame["artifacts"] = meta["artifacts"]
    # The turn's task list. Rides the same channel as sources and artifacts so
    # the checklist is DATA the UI renders, not prose the model has to remember
    # to repeat — which is the whole point of it existing.
    if meta.get("tasks"):
        frame["tasks"] = meta["tasks"]
    return frame


# Which checklist row a tool fulfils, by the words a user would have used for
# it. The plan is written in the user's words and the tick has to be earned by
# the thing actually happening — so the tick comes from the tool completing,
# not from the model remembering to say so. A recorded run sat at 0/6 with two
# steps visibly finished because the model never called track_tasks again.
_TOOL_FULFILS: dict[str, tuple[str, ...]] = {
    "read_outlook_emails": ("outlook", "inbox", "mail", "email"),
    "read_teams_messages": ("teams", "channel"),
    "web_search": ("search", "web", "look up", "lookup"),
    "create_data_dashboard": ("dashboard here", "render", "dashboard in", "chart"),
    "create_grafana_dashboard": ("grafana",),
    "create_slide_deck": ("deck", "slides", "pack", "presentation"),
    "create_docx": ("document", "word", "docx", "report"),
    "create_agent_workflow": ("agent",),
    "send_email": ("send", "email to"),
}

# Appended to every tool result while a plan is open. A tool that finished is a
# step the reader wants explained — what it did, what it found, what that means
# — BEFORE the next thing starts. Without this the turn was cards and one-line
# labels: "Building that dashboard on your Grafana." then silence.
_POST_FEEDBACK = (
    "\n\n[Step finished. Before ANY further tool call: write at least three full "
    "sentences for the reader on what this step did, what it found (name the "
    "actual figures, items or facts), and what that means for the request. Then "
    "continue with the next item on the plan, in order.]"
)


def _auto_tick(name: str, build_state: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Mark the checklist row a finished tool fulfils. Returns the updated
    list when something changed, else None. First unfinished match wins, in
    plan order, so two dashboard rows tick in sequence. O(rows)."""
    tasks = build_state.get("tasks")
    keys = _TOOL_FULFILS.get(name)
    if not isinstance(tasks, list) or not keys:
        return None
    for task in tasks:
        if not isinstance(task, dict) or task.get("done"):
            continue
        title = str(task.get("title", "")).lower()
        if any(k in title for k in keys):
            task["done"] = True
            return tasks
    return None


def _plan_frame(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    done = sum(1 for t in tasks if t.get("done"))
    return {
        "type": "tool",
        "name": "track_tasks",
        "status": "done",
        "result": f"{done}/{len(tasks)} done",
        "tasks": tasks,
    }


def _after_tool(name: str, result: Any, build_state: dict[str, Any], writer: Any) -> str:
    """What the model is handed after a tool: its result, plus the plan tick
    and the post-feedback instruction while a plan is open. track_tasks is
    exempt from both — it IS the plan, and nudging it would loop."""
    content = str(result.content)
    if name == "track_tasks":
        return content
    ticked = _auto_tick(name, build_state)
    if ticked is not None:
        writer(_plan_frame(ticked))
    tasks = build_state.get("tasks")
    if isinstance(tasks, list) and any(not t.get("done") for t in tasks if isinstance(t, dict)):
        remaining = [
            str(t.get("title")) for t in tasks if isinstance(t, dict) and not t.get("done")
        ]
        content += _POST_FEEDBACK + " Still to do: " + "; ".join(remaining) + "."
        # The instruction above is advisory; the gate in the agent round is not.
        build_state["owe_feedback"] = name
        build_state.pop("feedback_strikes", None)
    elif isinstance(tasks, list) and tasks:
        content += (
            "\n\n[Every item on the plan is done. Write the closing message now: at "
            "least four sentences covering what was produced, what it showed, and "
            "each thing published — name every card the reader can open.]"
        )
        # The closing is owed exactly like a step's explanation: a tool call
        # instead of it is refused by the same gate.
        build_state["owe_feedback"] = name
        build_state.pop("feedback_strikes", None)
    return content


# What each gatekeeper check says when it finds nothing — a full sentence per
# check, not a template. A templated "clear — no {what it looks at}" produced
# "clear — no the whole definition, read by the safety model", which is worse
# than the bare "clear" it replaced.
_SCAN_CLEAR: dict[str, str] = {
    "pii": "clear — no Omani national IDs, mobile numbers, IBANs or card numbers in the prompt",
    "prompt_injection": "clear — no instructions that try to override the agent's rules",
    "llm_review": "clear — the safety model read the whole definition and raised nothing",
    "workflow_rules": (
        "clear — every tool and data source it reaches is allowed for its classification"
    ),
}


def _scan_result_text(name: str, findings: list[Any]) -> str:
    """A result a reviewer can read: what the check covered, and what it found.
    Findings carry only the redacted preview — the raw match stays server-side."""
    if not findings:
        return _SCAN_CLEAR.get(name, "clear — nothing found in the agent definition")
    shown: list[str] = []
    for f in findings[:3]:
        kind = str(getattr(f, "finding_type", "") or "").rsplit(".", 1)[-1].replace("_", " ")
        sev = str(getattr(f, "severity", "") or "")
        preview = str(getattr(f, "matched_preview", "") or "").strip()
        piece = f"{kind} ({sev})" if sev else kind
        if preview and preview != kind:
            piece += f": {preview[:60]}"
        shown.append(piece)
    more = len(findings) - len(shown)
    tail = f" +{more} more" if more > 0 else ""
    return (
        f"{len(findings)} finding{'s' if len(findings) != 1 else ''} — " + "; ".join(shown) + tail
    )


_SENTENCE = re.compile(r"[^.!?]+[.!?]+")
_MIN_FEEDBACK_SENTENCES = 3
_MIN_FEEDBACK_CHARS = 120
# After this many refusals the call goes through: a model that cannot write
# three sentences must not wedge the turn, and the log says it happened.
_MAX_FEEDBACK_STRIKES = 2


def _needs_feedback_first(
    text: str, call_names: list[str], build_state: dict[str, Any]
) -> str | None:
    """The gate. Returns the message to send the model INSTEAD of running its
    tool calls, or None to proceed.

    The post-feedback instruction in the tool result was advisory, and the
    model walked past it: inbox read, straight to the search, straight to
    reading pages, not a word to the reader between. So the graph refuses. If a
    step's explanation is owed and the model answers with a tool call and fewer
    than three sentences, the calls are dropped and it is told to write first.
    Plan updates (track_tasks) pass; the explanation is still owed after them.
    Satisfied — or refused twice already — the flag clears and the calls run.
    """
    owed = build_state.get("owe_feedback")
    if not owed:
        return None
    real = [n for n in call_names if n != "track_tasks"]
    sentences = len(_SENTENCE.findall(text))
    enough = sentences >= _MIN_FEEDBACK_SENTENCES and len(text.strip()) >= _MIN_FEEDBACK_CHARS
    if enough or not real:
        if enough:
            build_state.pop("owe_feedback", None)
            build_state.pop("feedback_strikes", None)
        return None
    strikes = int(build_state.get("feedback_strikes", 0)) + 1
    build_state["feedback_strikes"] = strikes
    if strikes > _MAX_FEEDBACK_STRIKES:
        log.warning("sila.graph.feedback_gate_exhausted", owed=owed, strikes=strikes)
        build_state.pop("owe_feedback", None)
        build_state.pop("feedback_strikes", None)
        return None
    log.info("sila.graph.feedback_gate", owed=owed, strike=strikes, sentences=sentences)
    step = str(owed).replace("_", " ")
    return (
        f"Not yet. The step that just finished ({step}) has not been explained to the "
        "reader. Write at least three full sentences now — what it did, what it found "
        "(the actual figures, names, items), and what that means for the request. "
        "Do not call any tool in this message. Then, in your next message, continue "
        "with the next item on the plan."
    )


def _dedupe_calls(calls: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    """Identical calls collapse to one; `seen` carries the keys already run.

    A tool call is identified by its name plus its arguments, so asking the same
    question twice is one call while two different searches are still two.
    O(n) over the round's calls.
    """
    out: list[dict[str, Any]] = []
    for call in calls:
        try:
            key = f"{call['name']}:{json.dumps(call.get('arguments') or {}, sort_keys=True)}"
        except (TypeError, ValueError):
            out.append(call)  # unhashable args — run it rather than guess
            continue
        if key in seen:
            log.info("sila.tool.duplicate_skipped", tool=call["name"])
            continue
        seen.add(key)
        out.append(call)
    return out


_PAUSING_UI_COMMANDS = frozenset(
    {"open_modal", "ask_user", "request_credentials", "connect_provider", "confirm_email"}
)

# Big-action tools that must never run in the same round the model ASKED the user
# something — the question stands, the user's answer restarts the action.
_DEFER_ON_QUESTION = frozenset(
    {
        "create_agent_workflow",
        "create_workflow_draft",
        "create_agent_from_docs",
        "hire_agent",
        "create_grafana_dashboard",
        "create_slide_deck",
    }
)

_WORKER_SYSTEM = (
    "You are a focused worker. Complete ONLY the one sub-task you are given, using "
    "the build tools available. Do not chat, ask questions, or attempt anything "
    "outside the sub-task. If you cannot complete it with the tools and inputs you "
    "have, stop."
)

_SYNTH_SYSTEM = (
    "You are SILA. Several sub-tasks were just run for the user; you are given their "
    "results and the security-review outcome of anything built. Write a brief, "
    "friendly 2–4 sentence reply: what's ready, what (if anything) failed and what "
    "the user should do about it (e.g. connect an app and retry), and note that any "
    "built agents are Draft and need a human review before publishing. No headings."
)

log = structlog.get_logger("sila.orchestrator")

# Outcome → a human word for the "Security review" bracket step's result line
# (never the raw machine outcome). Per-scanner labels + icons live on the frontend
# trace, keyed by the stable step id we emit (the scanner.name).
_OUTCOME_WORD: dict[str, str] = {
    "auto_approved": "passed",
    "ready_for_review": "needs human review",
    "rejected": "flagged",
}


@dataclass
class GraphDeps:
    """Per-connection runtime deps bound into the nodes (never checkpointed)."""

    gateway: LLMGateway
    audit: AuditRepository
    user: UserResult
    role: Role
    workspace_id: uuid.UUID
    # provider → (n8n credential type, cred id) collected via the Incognito panel this
    # connection. Shared (mutable) with the WS handler + the build tool — never holds a
    # secret, only the n8n credential reference.
    pending_credentials: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Part B — the build requirements-interview state, shared with the WS handler so it
    # can record ask_user answers into it on resume (deterministic slot gate).
    build_state: dict[str, Any] = field(default_factory=dict)
    # Per-mode behaviour (Build/Plan/Auto): the system prompt + the allowed tool set
    # (Plan is restricted to help/discovery tools). Set from the WS hello's `mode`.
    # When `mode == "auto"`, the agent node re-routes EVERY message through the
    # deterministic intent router instead (system_prompt/tool_names are fallbacks).
    system_prompt: str = _SYSTEM_PROMPT
    tool_names: frozenset[str] | None = None
    mode: str = ""
    # A built-in template's persona (server-side constant, whitelisted by ID at
    # the WS hello). Appended to the resolved prompt EVERY round — AUTO mode
    # re-resolves the base prompt per message, so it can't ride system_prompt.
    persona: str = ""
    # Per-user memory note (durable facts). Separate from persona on purpose:
    # persona also disables the deterministic kicks — memory must never do that.
    memory: str = ""
    # Skills the user attached from the Bazaar (services/skills/context.py):
    # fenced reference instructions, appended like memory — never a persona, so
    # AUTO keeps routing each message.
    skills: str = ""
    # Fan-out runtime only (Slice 4+): per-turn denial-of-wallet budget + a run id that
    # keys idempotent worker side effects. Inert under the single-supervisor graph.
    budget: TurnBudget = field(
        default_factory=lambda: TurnBudget(
            token_budget=settings.sila_turn_token_budget,
            deadline_s=settings.sila_turn_deadline_s,
        )
    )
    run_id: uuid.UUID = field(default_factory=uuid.uuid4)


def _reduce_results(existing: list | None, new: list | None) -> list:  # type: ignore[type-arg]
    """Worker-results channel reducer. Parallel `Send` workers each append their one
    result; the planner writes None at the start of a fresh turn to RESET (so results
    don't accumulate across turns on the same checkpointed thread)."""
    if new is None:
        return []
    return (existing or []) + new


class GState(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], operator.add]  # user/assistant/tool turns
    pending: list[dict[str, Any]]  # non-pausing tool calls for the `tools` node
    pause: dict[str, Any] | None  # a pausing tool's UI payload for the `pause` node
    to_vet: str | None  # a freshly-built agent id awaiting the visible vetting node
    closing: bool  # post-vetting wrap-up round — TEXT ONLY, no tools offered
    retry: bool  # a pausing tool produced no UI → loop back to agent to recover
    blank_retried: bool  # one silent re-ask after an empty model round (cold start)
    reply: str  # final plain-text reply
    complexity: str  # fan-out planner's classification ("simple" | "complex")
    plan: list[dict[str, Any]]  # fan-out subtasks [{id, label}] (empty unless complex)
    subtask: dict[str, Any]  # a single worker's sub-task (set via Send, worker-local)
    worker_results: Annotated[list[dict[str, Any]], _reduce_results]  # joined fan-out results
    built_agents: list[dict[str, Any]]  # [{id, label}] artifacts to funnel through vetting
    vetting_summary: list[dict[str, Any]]  # [{id, label, outcome}] per-artifact gate results
    run_seed: str  # stable per-turn idempotency seed (checkpointed → survives reconnect)


def _llm_messages(
    state_messages: list[dict[str, Any]], system_prompt: str = _SYSTEM_PROMPT
) -> list[dict[str, Any]]:
    """System prompt (per-mode) + the (system-free) transcript, in Ollama chat shape."""
    return [{"role": "system", "content": system_prompt}, *state_messages]


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def _is_pausing_ui(ui: dict[str, Any] | None) -> bool:
    """A tool result that would pause for user input — forbidden inside a worker (§C)."""
    if not ui:
        return False
    return ui.get("command") in _PAUSING_UI_COMMANDS


async def _run_subtask(
    ctx: ToolContext, gateway: LLMGateway, budget: TurnBudget, st: dict[str, Any]
) -> dict[str, Any]:
    """Run one sub-task to completion with a scoped toolset. Returns a result dict
    {id, label, status: done|failed, summary?/reason?, artifact_id?}. Never streams
    tokens (workers are silent — the synthesizer narrates) and NEVER interrupts."""
    wid, label = str(st.get("id") or "task"), str(st.get("label") or "")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _WORKER_SYSTEM},
        {"role": "user", "content": f"Complete ONLY this sub-task: {label}"},
    ]

    def _fail(reason: str) -> dict[str, Any]:
        return {"id": wid, "label": label, "status": "failed", "reason": reason}

    def _done(summary: str, artifact_id: str | None = None) -> dict[str, Any]:
        return {
            "id": wid,
            "label": label,
            "status": "done",
            "summary": summary,
            "artifact_id": artifact_id,
        }

    retried = False
    for _round in range(MAX_SILA_TOOL_ITERATIONS):
        if budget.exhausted:
            return _fail("turn budget reached")
        try:
            text, calls = await _collect_worker_round(gateway, messages)
        except LLMGatewayError:
            if retried:
                return _fail("the model was unavailable")
            retried = True  # §A: exactly one transient retry
            continue
        if not calls:
            return _done(text.strip() or "completed")
        messages.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {"function": {"name": c.name, "arguments": c.arguments}} for c in calls
                ],
            }
        )
        for call in calls:
            result = await execute_tool(call.name, ctx, call.arguments)
            if _is_pausing_ui(result.ui):
                # §C: a worker NEVER interrupts. Fail gracefully — the input is
                # collected single-threaded (pre-collection / a retry), not here.
                return _fail(f"needs more input ({result.ui.get('command')})")  # type: ignore[union-attr]
            messages.append({"role": "tool", "content": result.content})
            artifact = result.meta.get("vetted_agent_id") if result.meta else None
            if artifact:
                return _done(result.summary or "built", artifact_id=str(artifact))
    return _done("completed")


def _synth_context(results: list[dict[str, Any]], vet: list[dict[str, Any]]) -> str:
    """A factual summary the synthesizer turns into a friendly reply."""
    lines: list[str] = []
    for r in results:
        if r.get("status") == "done":
            tag = f" (built agent {r['artifact_id']})" if r.get("artifact_id") else ""
            lines.append(f"DONE: {r['label']}{tag}")
        else:
            lines.append(f"FAILED: {r['label']} — {r.get('reason', 'unknown')}")
    for v in vet:
        lines.append(
            f"SECURITY REVIEW of '{v['label']}': {_OUTCOME_WORD.get(v['outcome'], 'flagged')}"
        )
    return "Results of the sub-tasks you orchestrated:\n" + "\n".join(lines)


def _synth_fallback(results: list[dict[str, Any]], vet: list[dict[str, Any]]) -> str:
    """Deterministic reply if the synthesizer LLM is unavailable."""
    done = sum(1 for r in results if r.get("status") == "done")
    failed = [r for r in results if r.get("status") != "done"]
    out = f"{done} of {len(results)} sub-tasks completed."
    if failed:
        names = ", ".join(f"'{r['label']}'" for r in failed)
        out += f" {names} didn't finish — you can connect what's needed and retry."
    if vet:
        out += " Anything built is Draft and needs a human review before publishing."
    return out


async def _collect_worker_round(
    gateway: LLMGateway, messages: list[dict[str, Any]]
) -> tuple[str, list[LLMToolCall]]:
    """One scoped LLM round for a worker — accumulate text + tool calls, no token
    streaming (workers run silent)."""
    text_parts: list[str] = []
    calls: list[LLMToolCall] = []
    async for chunk in gateway.stream_chat(
        messages=messages, tools=tool_schemas(WORKER_TOOL_NAMES)
    ):
        if isinstance(chunk, LLMTextDelta):
            text_parts.append(chunk.text)
        elif isinstance(chunk, LLMToolCall):
            calls.append(chunk)
        elif isinstance(chunk, LLMDone):
            break
    return "".join(text_parts), calls


def build_supervisor_graph(
    deps: GraphDeps, checkpointer: Any, *, with_planner: bool = False
) -> Any:  # noqa: ANN401
    """The proven single-supervisor graph. `with_planner=True` prepends the fan-out
    planner entry node (Slice 2: it classifies but always falls through to `agent`,
    so behavior is identical; the complex→fan-out branch is wired in Slice 4)."""
    ctx = ToolContext(
        db=deps.audit._db,  # the AuditRepository holds the request-scoped session
        user=deps.user,
        workspace_id=deps.workspace_id,
        role=deps.role,
        defer_vetting=True,  # the `vetting` node runs the review visibly, not the tool
        pending_credentials=deps.pending_credentials,  # shared dict, filled by the WS handler
        build_state=deps.build_state,  # shared dict, filled by the WS handler on slot answers
    )

    async def _audit(name: str, args: dict[str, Any]) -> None:
        try:
            await deps.audit.record(
                actor_user_id=deps.user.id,
                actor_workspace_id=deps.workspace_id,
                action=Action.SILA_TOOL_CALL,
                resource_type=ResourceType.WORKSPACE,
                resource_id=deps.workspace_id,
                outcome=Outcome.SUCCESS,
                metadata={"tool": name, "via": "sila-graph"},
            )
        except Exception:  # noqa: BLE001 — audit must never break the turn
            log.exception("sila.graph.audit_failed")

    async def agent(state: GState) -> dict[str, Any]:
        writer = get_stream_writer()
        # AUTO re-routes each message deterministically (build/plan/converse);
        # fixed modes keep the prompt + tool scope chosen at the WS hello.
        if deps.mode == "auto" and not deps.persona:
            system_prompt, tool_names = resolve_auto(
                _last_user_text(state.get("messages", [])), deps.build_state
            )
        else:
            # A pinned template rules the session: keep the base prompt + full
            # toolset instead of re-routing into the Build interview prompt —
            # its methodology leaked as visible "Step 1 / Step 2" narration in a
            # template session. The persona's staged flow does the routing.
            system_prompt, tool_names = deps.system_prompt, deps.tool_names
        if deps.persona:
            system_prompt = f"{system_prompt}\n\n{deps.persona}"
        if deps.memory:
            system_prompt = f"{system_prompt}\n\n{deps.memory}"
        if deps.skills:
            system_prompt = f"{system_prompt}\n\n{deps.skills}"
        # Pin the ORIGINAL build request the moment it arrives (first round, hard
        # build signal, no interview yet): the build tool uses it as the purpose,
        # so the model's paraphrase can never re-classify the agent.
        turn_msgs = state.get("messages", [])
        if (
            turn_msgs
            and turn_msgs[-1].get("role") == "user"
            and not deps.build_state.get("build_args")
            and (tool_names is None or "create_agent_workflow" in tool_names)
        ):
            # Route + pin on the user's OWN WORDS — attached-file content is data.
            first_words, _ = split_attachments(str(turn_msgs[-1].get("content", "")))
            if route_intent(first_words) == INTENT_BUILD:
                deps.build_state.setdefault("goal", first_words[:500])
        # Pin the reply language to the USER's script (qwen drifts into Chinese on
        # Arabic input otherwise) — recomputed per round from the live transcript.
        messages = _llm_messages(
            state.get("messages", []),
            system_prompt + language_note(_last_user_text(turn_msgs)),
        )
        reply_parts: list[str] = []
        calls: list[LLMToolCall] = []
        # Expose the workspace's registered MCP tools to the brain — but only in
        # action modes (tool_names None); Plan is help/discovery only, and MCP
        # tools have external side effects. Best-effort: discovery never blocks.
        mcp_extra: list[dict[str, Any]] = []
        if tool_names is None:
            from sila.services.sila.mcp_tools import workspace_mcp_schemas

            try:
                mcp_extra = await workspace_mcp_schemas(ctx.db, deps.workspace_id)
            except Exception as exc:  # noqa: BLE001 — a bad server never breaks the turn
                log.warning("sila.graph.mcp_discovery_failed", error=type(exc).__name__)
        # Split <think>…</think> reasoning from the answer: reasoning streams as
        # `thinking` frames (shown in the collapsible trace) and never lands in the
        # persisted reply; the answer streams as `token`.
        splitter = ThinkSplitter()

        def _emit_split(text: str) -> None:
            for mode, piece in splitter.feed(text):
                if mode == "answer":
                    reply_parts.append(piece)
                    writer({"type": "token", "content": piece})
                else:
                    writer({"type": "thinking", "content": piece})

        # The wrap-up round after the visible security review composes ONE closing
        # sentence — offering tools there let the model re-open an ask_user sheet
        # right after "your agent is ready" (live bug). Text only; no tools.
        closing = bool(state.get("closing"))
        try:
            async for chunk in deps.gateway.stream_chat(
                messages=messages,
                tools=None if closing else tool_schemas(tool_names, extra=mcp_extra),
            ):
                if isinstance(chunk, LLMTextDelta):
                    _emit_split(chunk.text)
                elif isinstance(chunk, LLMToolCall):
                    calls.append(chunk)
                elif isinstance(chunk, LLMDone):
                    break
            for mode, piece in splitter.flush():
                if mode == "answer":
                    reply_parts.append(piece)
                    writer({"type": "token", "content": piece})
                else:
                    writer({"type": "thinking", "content": piece})
        except LLMGatewayError as exc:
            log.warning("sila.graph.llm_failed", error=str(exc))
            writer({"type": "error", "message": "brain_unavailable"})
            return {"reply": "", "pending": [], "pause": None, "retry": False}

        text = "".join(reply_parts)

        if closing:
            # Closing round ends the turn unconditionally — never another tool,
            # question, or build after the review verdict has been delivered.
            final = text.strip() or "Done — your agent is drafted and reviewed."
            if not text.strip():
                writer({"type": "token", "content": final})
            return {
                "messages": [{"role": "assistant", "content": final}],
                "reply": final,
                "pending": [],
                "pause": None,
                "retry": False,
                "closing": False,
            }

        # Enforce the tool scope in CODE, not the prompt: a restricted behaviour
        # (Plan, or AUTO routed to plan) must never execute a build tool even if
        # the model emits one it wasn't offered. Blocked calls are dropped; if
        # nothing survives, feed the refusal back and loop so the model recovers
        # within scope (same shape as the pausing-tool-no-UI retry).
        if tool_names is not None and calls:
            blocked = [c for c in calls if c.name not in tool_names]
            calls = [c for c in calls if c.name in tool_names]
            if blocked and not calls:
                names = ", ".join(sorted({c.name for c in blocked}))
                log.info("sila.graph.tool_blocked", tools=names, mode=deps.mode or "fixed")
                return {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": text,
                            "tool_calls": [
                                {"function": {"name": c.name, "arguments": c.arguments}}
                                for c in blocked
                            ],
                        },
                        {
                            "role": "tool",
                            "content": (
                                f"The tool(s) {names} are not available in this advisory "
                                "scope — you cannot build here. Answer in plain text, or "
                                "use only the discovery tools you were offered."
                            ),
                        },
                    ],
                    "pending": [],
                    "pause": None,
                    "retry": True,
                }

        if not calls:
            # A model that ANSWERS a hard build request in prose (glm4-style essays,
            # or silence) instead of calling the build tool → kick the build
            # DETERMINISTICALLY. The gate runs docs → questions → confirm from here,
            # so nothing depends on the model acting; its narration stays above.
            msgs = state.get("messages", [])
            last_user, _ = split_attachments(_last_user_text(msgs))
            # DATA-DASHBOARD KICK: a data file is attached and the user asked for
            # charts/insights, but the model answered in prose (it lectured "how
            # to use Grafana" live). Deterministic: chart the file now.
            words, _ = split_attachments(last_user)
            data_attached = any(
                str(f.get("name", "")).lower().endswith((".csv", ".txt"))
                for f in deps.build_state.get("uploaded_files", [])
                if isinstance(f, dict)
            )
            allow_data = tool_names is None or "create_data_dashboard" in tool_names
            first_round_msg = bool(msgs) and msgs[-1].get("role") == "user"
            if (
                first_round_msg
                and allow_data
                and data_attached
                and wants_data_dashboard(words)
                and not text.rstrip().endswith("?")
                # A template session must go through its scan→propose→confirm
                # stages — the kick would jump straight to the dashboard.
                and not deps.persona
            ):
                log.info("sila.graph.data_dashboard_kick")
                if text.strip():
                    writer({"type": "token", "content": "\n\n"})
                writer({"type": "token", "content": "Let me chart that data for you."})
                return {
                    "messages": [{"role": "assistant", "content": text}],
                    "pending": [{"name": "create_data_dashboard", "arguments": {}}],
                    "pause": None,
                    "retry": False,
                }
            allow_build = tool_names is None or "create_agent_workflow" in tool_names
            # First round of the turn only (the newest message is the user's): a later
            # prose round is the model wrapping up after tools — kicking there loops.
            first_round = bool(msgs) and msgs[-1].get("role") == "user"
            if (
                first_round
                and allow_build
                and not deps.build_state.get("build_args")
                and route_intent(last_user) == INTENT_BUILD
                # Never kick past an unanswered question — if the model just asked
                # the user something, the turn ends there and waits.
                and not text.rstrip().endswith("?")
                # Template sessions confirm the own-copy offer first — the kick
                # was force-starting the build over the persona's confirm gate.
                and not deps.persona
            ):
                log.info("sila.graph.build_kick", reason="prose_no_tool_call")
                if text.strip():
                    writer({"type": "token", "content": "\n\n"})
                writer(
                    {
                        "type": "token",
                        "content": "Let me set this up properly — starting the build.",
                    }
                )
                return {
                    "messages": [{"role": "assistant", "content": text}],
                    "pending": [
                        {
                            "name": "create_agent_workflow",
                            "arguments": {"name": "", "purpose": last_user[:500]},
                        }
                    ],
                    "pause": None,
                    "retry": False,
                }
            # A weak model sometimes returns nothing at all (typically right after
            # an Ollama model reload) — retry ONCE silently before the graceful
            # "lost the thread" reply, so a cold-start blank doesn't reach the user.
            if not text.strip() and not state.get("blank_retried"):
                log.info("sila.graph.blank_round_retry")
                return {
                    "messages": [],
                    "pending": [],
                    "pause": None,
                    "retry": True,
                    "blank_retried": True,
                }
            final = text.strip() or "Sorry — I lost the thread there. Could you say that again?"
            if not text.strip():
                writer({"type": "token", "content": final})
            return {
                "messages": [{"role": "assistant", "content": final}],
                "reply": final,
                "pending": [],
                "pause": None,
                "retry": False,
                "blank_retried": False,
            }

        # Collect documents before behaviour questions, regardless of emit order.
        calls.sort(key=lambda c: 0 if c.name == "request_documents" else 1)
        spoke = bool(text.strip())
        # The model asked the user a question AND called a big-action tool in the
        # same breath ("Shall I proceed? …[create_agent_workflow]") — drop the
        # action and let the question stand; the user's answer restarts it.
        if spoke and text.rstrip().endswith("?"):
            calls = [c for c in calls if c.name not in _DEFER_ON_QUESTION]
            if not calls:
                return {
                    "messages": [{"role": "assistant", "content": text}],
                    "reply": text,
                    "pending": [],
                    "pause": None,
                    "retry": False,
                }
        # A tool call with the previous step unexplained does not run.
        nudge = _needs_feedback_first(text, [c.name for c in calls], deps.build_state)
        if nudge is not None:
            if spoke:
                writer({"type": "token", "content": "\n\n"})
            return {
                "messages": [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": nudge},
                ],
                "pending": [],
                "pause": None,
                "retry": True,
            }
        assistant_msg = {
            "role": "assistant",
            "content": text,
            "tool_calls": [{"function": {"name": c.name, "arguments": c.arguments}} for c in calls],
        }

        pausing = next((c for c in calls if c.name in _PAUSING_TOOLS), None)
        if pausing is not None:
            if not spoke and (line := _TOOL_NARRATIONS.get(pausing.name)):
                writer({"type": "token", "content": line})
            writer({"type": "tool", "name": pausing.name, "status": "running"})
            ctx.progress = lambda note, extra=None, _n=pausing.name: writer(
                {
                    "type": "tool",
                    "name": _n,
                    "status": "progress",
                    "note": note,
                    # A counted read ("3 of 23" + the item) rides as `progress`.
                    **({"progress": extra} if extra else {}),
                }
            )
            try:
                result = await execute_tool(pausing.name, ctx, pausing.arguments)
            finally:
                ctx.progress = None
            writer(tool_done_frame(pausing.name, result))
            await _audit(pausing.name, pausing.arguments)
            tool_msg = {
                "role": "tool",
                "content": _after_tool(pausing.name, result, deps.build_state, writer),
            }
            if result.ui:
                return {
                    "messages": [assistant_msg, tool_msg],
                    "pause": result.ui,
                    "pending": [],
                    "retry": False,
                }
            # No UI (e.g. a weak model emitted an invalid ask_user) — don't end the turn
            # silently. Feed the tool's guidance back and loop to the agent so it recovers
            # (asks in plain text) next round.
            return {
                "messages": [assistant_msg, tool_msg],
                "pause": None,
                "pending": [],
                "retry": True,
            }

        if not spoke and (line := _TOOL_NARRATIONS.get(calls[0].name)):
            # Template sessions PROPOSE before building — "Let me chart that data"
            # right before an interrupt-to-analyze read wrong (Mojahid). Narrate
            # the scan when the propose gate is about to fire.
            if (
                deps.persona
                and calls[0].name
                in ("create_data_dashboard", "create_grafana_dashboard", "create_powerbi_report")
                and not deps.build_state.get("analysis_plan")
            ):
                line = "Let me take a proper look at your data first…"
            writer({"type": "token", "content": line})
        return {
            "messages": [assistant_msg],
            "pending": [{"name": c.name, "arguments": c.arguments} for c in calls],
            "pause": None,
            "retry": False,
        }

    async def tools(state: GState) -> dict[str, Any]:
        writer = get_stream_writer()
        new_msgs: list[dict[str, Any]] = []
        to_vet: str | None = None
        pause: dict[str, Any] | None = None
        # Drop exact repeats within a round. A weak model emitting the same call
        # twice ran it twice — the user saw "Searching the Bazaar / No matches"
        # duplicated back to back. Identity is name + arguments, so a genuine
        # second search with different terms still runs.
        seen: set[str] = set()
        for call in _dedupe_calls(state.get("pending", []), seen):
            name, args = call["name"], call["arguments"]
            writer({"type": "tool", "name": name, "status": "running"})
            ctx.progress = lambda note, extra=None, _n=name: writer(
                {
                    "type": "tool",
                    "name": _n,
                    "status": "progress",
                    "note": note,
                    **({"progress": extra} if extra else {}),
                }
            )
            try:
                result = await execute_tool(name, ctx, args)
            finally:
                ctx.progress = None
            writer(tool_done_frame(name, result))
            await _audit(name, args)
            new_msgs.append(
                {"role": "tool", "content": _after_tool(name, result, deps.build_state, writer)}
            )
            # A non-pausing tool that needs the user mid-build → pause the turn:
            #   request_credentials → the Incognito panel (B2 typed secret)
            #   connect_provider   → the Nango Connect flow + resource picker (WS-9)
            #   ask_user / open_modal → the requirements gate's questions + docs panel
            # Don't keep building until the user finishes.
            pausing_ui = result.ui and result.ui.get("command") in (
                "request_credentials",
                "connect_provider",
                "ask_user",  # Part B — the build tool's deterministic requirements gate
                "open_modal",  # the gate's deterministic docs request (upload panel)
            )
            if pausing_ui:
                pause = result.ui
                break
            if result.ui:
                writer({"type": "ui", **result.ui})
            # A build tool hands back a freshly-registered agent → route to vetting.
            if result.meta and result.meta.get("vetted_agent_id"):
                to_vet = str(result.meta["vetted_agent_id"])
        return {"messages": new_msgs, "pending": [], "to_vet": to_vet, "pause": pause}

    async def vetting(state: GState) -> dict[str, Any]:
        # The Phase-6 gate, shown live: run each scanner in order and stream a step
        # per check, then hand a deterministic outcome message to the agent node so
        # it composes the closing line. The hard gate is never skipped or overridden.
        from sila.db.repositories.agent import AgentRepository
        from sila.services.vetting_service import VettingService

        writer = get_stream_writer()
        agent_id = state.get("to_vet")
        agent = (
            await AgentRepository(ctx.db).get(uuid.UUID(str(agent_id)), deps.workspace_id)
            if agent_id
            else None
        )
        if agent is None:
            return {
                "messages": [{"role": "tool", "content": "Could not find the agent to review."}],
                "to_vet": None,
                "closing": True,
            }

        writer({"type": "tool", "name": "security_review", "status": "running"})

        def on_step(name: str, findings: list[Any]) -> None:
            # `name` is the scanner's stable id (pii / prompt_injection / llm_review /
            # workflow_rules); the frontend trace maps it to a label + icon. The
            # result says WHAT was checked and what was found — "clear" alone told
            # a reviewer nothing about what the gate actually does.
            writer(
                {
                    "type": "tool",
                    "name": name,
                    "status": "done",
                    "result": _scan_result_text(name, findings),
                }
            )

        try:
            run = await VettingService(ctx.db).scan_streamed(
                agent=agent, actor_id=deps.user.id, on_step=on_step
            )
        except Exception as exc:  # noqa: BLE001 — a review failure must not crash the turn
            log.warning("sila.graph.vetting_failed", error=str(exc))
            writer({"type": "tool", "name": "security_review", "status": "done", "result": "error"})
            return {
                "messages": [
                    {
                        "role": "tool",
                        "content": (
                            "The security review couldn't complete; the agent stays Draft + "
                            "Unvetted and a human must review it before use."
                        ),
                    }
                ],
                "to_vet": None,
                "closing": True,
            }

        outcome = run.outcome
        writer(
            {
                "type": "tool",
                "name": "security_review",
                "status": "done",
                "result": _OUTCOME_WORD.get(str(outcome), "flagged"),
            }
        )
        if outcome == "auto_approved":
            content = (
                "Security review passed with no issues — the agent is ready in the workspace. "
                "Publishing it to the Bazaar still needs a human."
            )
        elif outcome == "ready_for_review":
            content = (
                "Security review complete — this agent needs a human reviewer before use "
                "(its classification or findings require sign-off)."
            )
        else:
            content = (
                "Security review flagged issues; the agent stays Draft until they're addressed."
            )
        return {"messages": [{"role": "tool", "content": content}], "to_vet": None, "closing": True}

    async def pause(state: GState) -> dict[str, Any]:
        # Only interrupt() here — cheap to re-run on resume. The WS adapter turns
        # `pause` into the ask_user / open_modal frame and resumes with the
        # readable user response string.
        answer = interrupt(state.get("pause") or {})
        out: dict[str, Any] = {
            "messages": [{"role": "user", "content": str(answer)}],
            "pause": None,
        }
        # A pause is where a multi-step turn loses its tail. The checklist lives
        # on build_state and survives the interrupt, but nothing put it back in
        # front of the model — so after answering a schedule question it built
        # the agent, called the turn done, and left the dashboard, the Grafana
        # push and the deck unticked and unbuilt. The reminder is data the model
        # cannot miss rather than a rule it has to remember.
        pending = [
            str(task.get("title", ""))
            for task in (deps.build_state.get("tasks") or [])
            if isinstance(task, dict) and not task.get("done")
        ]
        if pending:
            out["messages"].append(
                {
                    "role": "user",
                    "content": (
                        "Reminder — these items from the plan are NOT done yet, in "
                        "order: " + "; ".join(pending) + ". Do them now, calling "
                        "track_tasks after each one, before you write any closing "
                        "message."
                    ),
                }
            )
        # Email confirm gate: a stashed draft + a Send answer → deliver it
        # deterministically (confirm_send_email, not the model). Anything else
        # cancels the draft so nothing is sent.
        if deps.build_state.get("pending_email"):
            if _CONFIRM_EMAIL_SENTINEL in str(answer):
                deps.build_state["email_confirmed"] = True
                out["pending"] = [{"name": "confirm_send_email", "arguments": {}}]
            else:
                deps.build_state.pop("pending_email", None)
                deps.build_state.pop("email_confirmed", None)
                out["messages"] = [
                    {"role": "user", "content": "I cancelled that email — it was NOT sent."}
                ]
            return out
        # Deterministic build-interview advancement (Part B): if a build interview is
        # active, ALWAYS re-run the build tool with the stashed args instead of
        # relying on a weak model to re-call it. The gate reads whatever the WS
        # handler just recorded (a slot answer, the confirm, a change pick) and
        # advances — including the "Let me change something" menu, which the gate
        # itself now serves. build_state is cleared on a successful registration.
        bargs = deps.build_state.get("build_args")
        if bargs:
            # An upload just finished → the resume text carries the kb id; harvest it
            # into the stash so the deterministic re-runs build on the real KB.
            kb_match = re.search(r"kb_id:\s*([0-9a-fA-F-]{36})", str(answer))
            if kb_match and not bargs.get("kb_id"):
                bargs["kb_id"] = kb_match.group(1)
            out["pending"] = [{"name": "create_agent_workflow", "arguments": dict(bargs)}]
        return out

    def route(state: GState) -> str:
        if state.get("pause"):
            return "pause"
        if state.get("pending"):
            return "tools"
        if state.get("retry"):  # a pausing tool gave no UI → re-prompt the model
            return "agent"
        return END

    def route_after_tools(state: GState) -> str:
        if state.get("pause"):  # a tool asked for a secret → Incognito panel
            return "pause"
        # A build just happened → run the visible security review before replying.
        return "vetting" if state.get("to_vet") else "agent"

    def route_after_pause(state: GState) -> str:
        # Mid-build-interview → re-run the build tool deterministically (the pause node
        # set `pending`); otherwise hand the answer to the model.
        return "tools" if state.get("pending") else "agent"

    async def planner(state: GState) -> dict[str, Any]:
        # Fan-out entry. Cheap heuristic pre-filter (no LLM cost); only "complex"
        # pays for the LLM decomposition. Runs on a fresh turn only (resume re-enters
        # at `pause`). Slice 3: decompose + show the roster, but still fall through to
        # the single-threaded `agent` (the elicit + Send execution lands in Slice 4).
        writer = get_stream_writer()
        text = _last_user_text(state.get("messages", []))
        complexity = classify_complexity(text)
        plan: list[dict[str, Any]] = []
        if complexity == COMPLEX:
            plan = await decompose(deps.gateway, text, settings.sila_max_workers)
            if len(plan) >= 2:
                # Roster — expert-only (the Beginner serializer drops these). Each
                # subtask gets a "queued" lane that Slice 4's workers will advance.
                writer(
                    {
                        "type": "plan",
                        "run_id": str(deps.run_id),
                        "complexity": COMPLEX,
                        "subtasks": plan,
                        "audience": "expert",
                    }
                )
                for st in plan:
                    writer(
                        {
                            "type": "worker",
                            "run_id": str(deps.run_id),
                            "worker_id": st["id"],
                            "label": st["label"],
                            "status": "queued",
                            "audience": "expert",
                        }
                    )
            else:
                # Decomposition found no independent parallel work → run as the
                # proven single supervisor (conservative fallback).
                complexity = SIMPLE
                plan = []
        # worker_results=None resets the channel for this fresh turn (the reducer).
        # run_seed is the stable idempotency seed: set ONCE here, checkpointed, so it
        # survives a WS drop + reconnect (resume re-enters at pause/elicit, not the
        # planner). Workers key idempotent side effects on (run_seed, subtask id).
        return {
            "complexity": complexity,
            "plan": plan,
            "worker_results": None,
            "run_seed": str(uuid.uuid4()),
        }

    async def worker(state: GState) -> dict[str, Any]:
        # One autonomous sub-task, invoked via Send (parallel). Pause-free: it NEVER
        # interrupts (§C) — on a missing-input pausing UI it fails gracefully. Bounded
        # by the per-worker round cap + the per-turn TurnBudget; one transient retry.
        #
        # Idempotency: LangGraph persists per-task pending writes, so a COMPLETED worker
        # is NOT re-run on reconnect (its result is restored) — the common case. The
        # residual window (a crash AFTER the build commits to the DB but BEFORE the node
        # returns) would re-run + duplicate; closing it needs a DB-backed idempotency key
        # `f"{run_seed}:{subtask id}"` on the agent row (a migration) — deferred while
        # fan-out is flag-gated. run_seed (checkpointed) is the stable seed, ready for it.
        writer = get_stream_writer()
        st = state.get("subtask") or {}
        wid, label = str(st.get("id") or "task"), str(st.get("label") or "Sub-task")
        lane = {
            "type": "worker",
            "run_id": str(deps.run_id),
            "worker_id": wid,
            "label": label,
            "audience": "expert",
        }
        writer({**lane, "status": "running"})
        if deps.budget.exhausted:
            result = {
                "id": wid,
                "label": label,
                "status": "failed",
                "reason": "turn budget reached",
            }
        else:
            # Autonomous worker: non-interactive ctx (skips the pausing requirements
            # gate) with its own build_state so parallel workers don't collide.
            worker_ctx = replace(ctx, interactive=False, build_state={})
            result = await _run_subtask(worker_ctx, deps.gateway, deps.budget, st)
        writer(
            {
                **lane,
                "status": result["status"],
                "result": result.get("summary") or result.get("reason"),
            }
        )
        return {"worker_results": [result]}

    async def join(state: GState) -> dict[str, Any]:
        # Runs once after the whole worker superstep. Collects the artifacts that need
        # vetting (every successfully-built agent) → the vetting funnel. No reply here;
        # the synthesizer composes it after the gate.
        built = [
            {"id": r["artifact_id"], "label": r["label"]}
            for r in state.get("worker_results", [])
            if r.get("status") == "done" and r.get("artifact_id")
        ]
        return {"built_agents": built}

    async def vetting_funnel(state: GState) -> dict[str, Any]:
        # MANDATORY gate (invariant): EVERY built artifact runs the Phase-6 scanners,
        # visibly, before the reply. Non-skippable; SILA never publishes/approves —
        # scan_streamed only sets vetting_status (Draft + approved|ready_for_review).
        from sila.db.repositories.agent import AgentRepository
        from sila.services.vetting_service import VettingService

        writer = get_stream_writer()
        built = state.get("built_agents", [])
        summary: list[dict[str, Any]] = []
        for art in built:
            agent = await AgentRepository(ctx.db).get(uuid.UUID(str(art["id"])), deps.workspace_id)
            if agent is None:
                continue
            writer(
                {
                    "type": "tool",
                    "name": "security_review",
                    "status": "running",
                    "result": art["label"],
                }
            )

            def _on_step(name: str, findings: list[Any]) -> None:
                writer(
                    {
                        "type": "tool",
                        "name": name,
                        "status": "done",
                        "result": f"{len(findings)} finding(s)" if findings else "clear",
                        "audience": "expert",  # per-scanner detail is expert-only
                    }
                )

            try:
                run = await VettingService(ctx.db).scan_streamed(
                    agent=agent, actor_id=deps.user.id, on_step=_on_step
                )
                outcome = str(run.outcome)
            except Exception as exc:  # noqa: BLE001 — a review failure can't crash the turn
                log.warning("sila.fanout.vetting_failed", error=str(exc))
                outcome = "error"
            writer(
                {
                    "type": "tool",
                    "name": "security_review",
                    "status": "done",
                    "result": f"{art['label']}: {_OUTCOME_WORD.get(outcome, 'flagged')}",
                }
            )
            summary.append({"id": art["id"], "label": art["label"], "outcome": outcome})
        return {"vetting_summary": summary}

    async def synthesizer(state: GState) -> dict[str, Any]:
        # The ONLY reply/TTS-feeding node in the fan-out path (workers run silent).
        # Reconciles worker results + vetting outcomes into one friendly reply.
        writer = get_stream_writer()
        context = _synth_context(state.get("worker_results", []), state.get("vetting_summary", []))
        messages = [
            {"role": "system", "content": _SYNTH_SYSTEM},
            {"role": "user", "content": context},
        ]
        parts: list[str] = []
        try:
            async for chunk in deps.gateway.stream_chat(messages=messages, tools=[]):
                if isinstance(chunk, LLMTextDelta):
                    parts.append(chunk.text)
                    writer({"type": "token", "content": chunk.text})
                elif isinstance(chunk, LLMDone):
                    break
        except LLMGatewayError:
            log.warning("sila.fanout.synth_failed")
        reply = "".join(parts).strip() or _synth_fallback(
            state.get("worker_results", []), state.get("vetting_summary", [])
        )
        if not parts:  # fallback wasn't streamed yet
            writer({"type": "token", "content": reply})
        return {"reply": reply, "messages": [{"role": "assistant", "content": reply}]}

    async def elicit(state: GState) -> dict[str, Any]:
        # §C foundation: collect the OAuth connections the sub-tasks need SINGLE-
        # THREADED, before any Send fan-out, so the parallel workers are pause-free.
        # This node interrupt()s directly (no LLM here), so it safely RE-RUNS on
        # resume — the binding check is the idempotency: a just-connected provider is
        # skipped and we move to the next. (Document pre-collection is deferred — a
        # doc-needing worker still fails gracefully until that lands.)
        from sila.db.repositories.connectors import ConnectorBindingRepository
        from sila.services.connectors.manifest import spec_for as connector_spec_for

        wanted: list[str] = []
        for st in state.get("plan", []):
            for integ in st.get("integrations", []):
                spec = connector_spec_for(integ)
                if spec and spec.auth_type == "oauth" and integ not in wanted:
                    wanted.append(integ)
        bindings = ConnectorBindingRepository(ctx.db)
        for integ in wanted:
            spec = connector_spec_for(integ)
            try:
                binding = await bindings.get(
                    user_id=deps.user.id, workspace_id=deps.workspace_id, provider=integ
                )
            except Exception as exc:  # noqa: BLE001 — connectors not set up (no table/Nango)
                # Degrade gracefully instead of crashing the turn: skip the connect
                # pre-collection; the worker will note the missing connection.
                log.warning(
                    "sila.fanout.connector_unavailable",
                    provider=integ,
                    error=type(exc).__name__,
                )
                continue
            if binding is None:
                # Pause for the Nango Connect flow. On resume elicit re-runs and
                # re-checks — the just-connected provider is now skipped.
                interrupt(
                    {
                        "command": "connect_provider",
                        "request_id": str(uuid.uuid4()),
                        "provider": integ,
                        "label": spec.label if spec else integ,
                    }
                )
        return {}

    def route_after_planner(state: GState) -> str:
        # Complex with ≥2 sub-tasks → single-threaded elicitation, THEN fan out.
        # Otherwise fall through to the single supervisor (simple / downgrade).
        plan = state.get("plan") or []
        if state.get("complexity") == COMPLEX and len(plan) >= 2:
            return "elicit"
        return "agent"

    def route_after_elicit(state: GState) -> Any:  # noqa: ANN401
        # Inputs are collected — fan out one Send per sub-task (cap-bounded).
        plan = state.get("plan") or []
        return [Send("worker", {"subtask": st}) for st in plan[: settings.sila_max_workers]]

    g = StateGraph(GState)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("vetting", vetting)
    g.add_node("pause", pause)
    if with_planner:
        g.add_node("planner", planner)
        g.add_node("elicit", elicit)
        g.add_node("worker", worker)
        g.add_node("join", join)
        g.add_node("vetting_funnel", vetting_funnel)
        g.add_node("synthesizer", synthesizer)
        g.add_edge(START, "planner")
        g.add_conditional_edges("planner", route_after_planner, ["elicit", "agent"])
        g.add_conditional_edges("elicit", route_after_elicit, ["worker"])
        g.add_edge("worker", "join")
        g.add_edge("join", "vetting_funnel")  # mandatory gate — every artifact
        g.add_edge("vetting_funnel", "synthesizer")
        g.add_edge("synthesizer", END)
    else:
        g.add_edge(START, "agent")
    g.add_conditional_edges(
        "agent", route, {"pause": "pause", "tools": "tools", "agent": "agent", END: END}
    )
    g.add_conditional_edges(
        "tools", route_after_tools, {"vetting": "vetting", "agent": "agent", "pause": "pause"}
    )
    g.add_edge("vetting", "agent")
    g.add_conditional_edges("pause", route_after_pause, {"tools": "tools", "agent": "agent"})
    return g.compile(checkpointer=checkpointer)


def build_fanout_graph(deps: GraphDeps, checkpointer: Any) -> Any:  # noqa: ANN401
    """The fan-out runtime: the supervisor graph fronted by the planner entry node.
    Slice 2 = planner classifies + falls through; Slices 3–5 add elicitation, the
    Send workers, and the synthesizer."""
    return build_supervisor_graph(deps, checkpointer, with_planner=True)
