"""Plan-mode plan artifact — normalize the model's proposal into a structured,
auditable plan card + the deterministic Build seed it hands off.

The model PROPOSES (propose_plan args); this module DISPOSES: every field is
validated, capped, and mapped in code. The card the user sees and the seed the
Build interview receives are built here, never trusted from the model as-is.
On Accept the frontend echoes the seed back in the Build hello, where
`build_slots.sanitize_seed` re-validates it — two independent gates.

Complexity: O(fields) per plan; fields < 12, options < 6 each.
"""

from __future__ import annotations

from typing import Any

from sila.services.sila import build_slots as bs

_TEXT_MAX = 300
_GOAL_MAX = 200
_MAX_DECISIONS = 4
_MAX_OPTIONS = 6
_MAX_INTEGRATIONS = 5
_MAX_STEPS = 10
_MAX_RISKS = 6
_MAX_CHECKPOINTS = 5

# Plain-language labels for the card (the slot ids stay expert detail).
AGENT_TYPE_LABELS: dict[str, str] = {
    bs.KNOWLEDGE: "Knowledge / Q&A",
    bs.SCHEDULED_PUBLISHER: "Scheduled publisher",
    bs.MONITOR: "Monitor / trigger",
    bs.WORKFLOW: "Workflow automation",
}


def _clean(value: Any, cap: int = _TEXT_MAX) -> str:
    return str(value or "").strip()[:cap]


def _clean_integrations(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:_MAX_INTEGRATIONS]:
        name = _clean(item, 40).lower()
        if name and name not in out:
            out.append(name)
    return out


def _clean_decisions(raw: Any) -> list[dict[str, Any]]:
    """Open decisions the user still has to make — rendered as choices on the card."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:_MAX_DECISIONS]:
        if not isinstance(item, dict):
            continue
        question = _clean(item.get("question"), 200)
        options = (
            [_clean(o, 80) for o in item["options"][:_MAX_OPTIONS] if _clean(o, 80)]
            if isinstance(item.get("options"), list)
            else []
        )
        if question:
            out.append({"question": question, "options": options})
    return out


def _clean_steps(raw: Any) -> list[dict[str, Any]]:
    """The execution decomposition — functional, context-driven step titles (the
    prompt forbids 'Step 1' style), a detail line, the data handoff (input →
    output), and dependency/parallel notes. All display text, all capped."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:_MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), 120)
        if not title:
            continue
        step: dict[str, Any] = {"title": title, "detail": _clean(item.get("detail"))}
        handoff_in = _clean(item.get("input"), 160)
        handoff_out = _clean(item.get("output"), 160)
        if handoff_in:
            step["input"] = handoff_in
        if handoff_out:
            step["output"] = handoff_out
        depends = _clean(item.get("depends_on"), 120)
        if depends:
            step["dependsOn"] = depends
        if item.get("parallel"):
            step["parallel"] = True
        out.append(step)
    return out


def _clean_risks(raw: Any) -> list[dict[str, str]]:
    """Failure analysis rows: what can go wrong → the concrete fallback."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:_MAX_RISKS]:
        if not isinstance(item, dict):
            continue
        risk = _clean(item.get("risk"), 200)
        fallback = _clean(item.get("fallback") or item.get("mitigation"), 250)
        if risk and fallback:
            out.append({"risk": risk, "fallback": fallback})
    return out


def _clean_checkpoints(raw: Any) -> list[str]:
    """Human-in-the-loop safety checkpoints — points where a person approves."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:_MAX_CHECKPOINTS]:
        text = _clean(item, 200)
        if text:
            out.append(text)
    return out


def _clean_bazaar_match(raw: Any) -> dict[str, str] | None:
    """A vetted ready-made agent that already covers the goal ("you may not need
    to build this"). Only id + name + a short note — the card links the hire."""
    if not isinstance(raw, dict):
        return None
    agent_id = _clean(raw.get("agent_id") or raw.get("id"), 40)
    name = _clean(raw.get("name"), 80)
    if not agent_id or not name:
        return None
    return {"id": agent_id, "name": name, "note": _clean(raw.get("note"), 200)}


def _seed_filled(agent_type: str, args: dict[str, Any]) -> dict[str, str]:
    """Map plan fields → the Build interview's slot names for this agent type.
    Only slots the plan actually settled; the gate asks the rest."""
    filled: dict[str, str] = {}
    content = _clean(args.get("content"))
    schedule = _clean(args.get("schedule"), 120)
    destination = _clean(args.get("destination"), 120)
    fallback = _clean(args.get("fallback"))
    escalation = _clean(args.get("escalation"))
    integrations = _clean_integrations(args.get("integrations"))

    if agent_type == bs.SCHEDULED_PUBLISHER:
        if content:
            filled["content"] = content
        if schedule:
            filled["schedule"] = schedule
        if destination:
            # An email destination fills email_to; a channel fills the typed
            # destination slot (skipped anyway when Slack is OAuth-wired).
            key = "email_to" if ("@" in destination or "email" in integrations) else "destination"
            filled[key] = destination
    elif agent_type == bs.KNOWLEDGE:
        if fallback:
            filled["out_of_scope"] = fallback
        if escalation:
            filled["escalation"] = escalation
    elif agent_type == bs.MONITOR and destination:
        filled["destination"] = destination
    elif agent_type == bs.WORKFLOW:
        # The plan's execution steps ARE the workflow's steps slot — joined titles.
        titles = [
            _clean(s.get("title"), 120)
            for s in args.get("steps", [])
            if isinstance(s, dict) and _clean(s.get("title"), 120)
        ]
        if titles:
            filled["steps"] = "; ".join(titles)[:300]
    return filled


def _derive_name(args: dict[str, Any], goal: str) -> str:
    """The agent's name: the model's proposal, else the goal's leading words."""
    name = _clean(args.get("name"), 60)
    if name:
        return name
    words = goal.split()
    return " ".join(words[:6]).rstrip(".,;:")[:60] or "Planned Agent"


def build_plan(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize propose_plan args → (plan card payload, build seed).

    The card payload is camelCase (it is persisted verbatim as the frontend
    trace segment); the seed is re-run through sanitize_seed so only real,
    per-type slots survive. An empty goal yields ({}, {}) — nothing to show."""
    goal = _clean(args.get("goal"), _GOAL_MAX)
    if not goal:
        return {}, {}
    integrations = _clean_integrations(args.get("integrations"))
    raw_type = _clean(args.get("agent_type"), 40).lower()
    agent_type = (
        raw_type if raw_type in bs.REQUIRED_SLOTS else bs.classify_agent_type(goal, integrations)
    )
    # build_args make the Accept handoff DETERMINISTIC: the pause node re-runs the
    # build tool with exactly these args, so the model can never reinvent the
    # name/purpose after the user accepted (the hallucinated-handoff bug).
    content = _clean(args.get("content"))
    purpose = f"{goal} — each output: {content}" if content else goal
    seed = bs.sanitize_seed(
        {
            "agent_type": agent_type,
            "filled": _seed_filled(agent_type, args),
            "build_args": {
                "name": _derive_name(args, goal),
                "purpose": purpose[:500],
                "integrations": integrations,
            },
        }
    )
    plan: dict[str, Any] = {
        "goal": goal,
        "agentType": AGENT_TYPE_LABELS[agent_type],
        "content": _clean(args.get("content")),
        "sources": _clean(args.get("sources")),
        "destination": _clean(args.get("destination"), 120),
        "integrations": integrations,
        "schedule": _clean(args.get("schedule"), 120),
        "fallback": _clean(args.get("fallback")),
        "steps": _clean_steps(args.get("steps")),
        "risks": _clean_risks(args.get("risks")),
        "checkpoints": _clean_checkpoints(args.get("checkpoints")),
        "openDecisions": _clean_decisions(args.get("open_decisions")),
        "bazaarMatch": _clean_bazaar_match(args.get("bazaar_match")),
        "seed": seed,
    }
    return plan, seed
