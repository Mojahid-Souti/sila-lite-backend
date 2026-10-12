"""Planner — the fan-out entry node's brain.

`classify_complexity` is the **cheap pre-filter**: a heuristic that decides whether
a request is plausibly several independent deliverables ("complex") or one
("simple"). It runs on every fanout turn with no LLM cost. Only when it returns
"complex" does the planner pay for an LLM **decomposition** (Slice 4) and fan out;
"simple" falls straight through to the single supervisor — identical to today.

Deliberately **conservative — defaults to "simple"**: a missed-complex just runs as
the proven supervisor (safe), whereas a false-complex would spend an LLM call and
maybe spawn one redundant worker. Bias toward under-fanning-out.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger("sila.planner")

_BUILD_VERBS = ("build", "create", "make", "set up", "setup", "automate", "generate")
_CONJUNCTIONS = (" and ", " then ", " also ", " as well as ", " plus ", " & ")
_INTEGRATIONS = (
    "slack",
    "email",
    "gmail",
    "outlook",
    "sheet",
    "notion",
    "calendar",
    "drive",
    "workflow",
    "n8n",
    "webhook",
)

SIMPLE = "simple"
COMPLEX = "complex"


def classify_complexity(text: str) -> str:
    """Heuristic simple/complex pre-filter. Conservative — defaults to simple."""
    t = (text or "").lower()
    # Count OCCURRENCES, not just presence, so "build X and build Y" (two builds,
    # no integrations) is caught as well as "build … slack … email".
    build_hits = sum(t.count(v) for v in _BUILD_VERBS)
    integ_hits = sum(1 for i in _INTEGRATIONS if i in t)
    conjoined = any(c in t for c in _CONJUNCTIONS)
    # "a separate / another / second" agent strongly signals a second deliverable.
    multi_signal = any(w in t for w in (" separate ", " another ", " second ", " also build"))

    # Complex when there's clear evidence of >1 deliverable:
    #   - two+ build verbs joined by a conjunction ("build X and build Y"), or
    #   - a build + a conjunction + two integrations ("build … slack … email"), or
    #   - a build + an explicit "separate/another" second-agent signal.
    if build_hits >= 2 and conjoined:
        return COMPLEX
    if build_hits >= 1 and conjoined and integ_hits >= 2:
        return COMPLEX
    if build_hits >= 1 and multi_signal:
        return COMPLEX
    return SIMPLE


_DECOMPOSE_SYSTEM = (
    "You split a user's request into INDEPENDENT sub-tasks that could run in "
    "parallel — no sub-task may depend on another's output. If it is really one "
    "task, return a single item. For each sub-task, list the third-party apps it "
    "needs (e.g. slack, google) in `integrations`, or [] if none. Respond ONLY as "
    'JSON: {"subtasks": [{"id": "kebab-case-id", "label": "short imperative label", '
    '"integrations": ["slack"]}]}. Return at most {max} items.'
)


async def decompose(gateway: Any, text: str, max_workers: int) -> list[dict[str, Any]]:
    """LLM decomposition (only run when the heuristic says COMPLEX). Returns a
    capped list of {id, label} sub-tasks, or [] on failure / a single task — the
    caller treats <2 as "run as the single supervisor" (conservative fallback)."""
    try:
        out = await gateway.complete_json(
            system_prompt=_DECOMPOSE_SYSTEM.replace("{max}", str(max_workers)),
            user_prompt=text,
            timeout_s=30.0,
        )
    except Exception:  # noqa: BLE001 — a planner hiccup must fall back to the supervisor
        log.warning("sila.planner.decompose_failed")
        return []
    raw = out.get("subtasks") if isinstance(out, dict) else None
    if not isinstance(raw, list):
        return []
    subtasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw[:max_workers]):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        sid = str(item.get("id") or f"task-{i + 1}").strip() or f"task-{i + 1}"
        if sid in seen:
            sid = f"{sid}-{i + 1}"
        seen.add(sid)
        raw_integ = item.get("integrations")
        integrations = (
            [str(x).lower().strip() for x in raw_integ if str(x).strip()]
            if isinstance(raw_integ, list)
            else []
        )
        subtasks.append({"id": sid, "label": label[:80], "integrations": integrations})
    return subtasks
