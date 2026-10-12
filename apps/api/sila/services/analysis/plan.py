"""Analysis Engine Phase 1 — the AnalysisPlan artifact and its hash gate.

One approval, then deterministic execution: every build tool refuses to run
in a template session unless the session holds a plan whose hash matches the
CURRENT objective + file identity + chart list. The plan is a plain dict
(JSON-persisted on the chat session's build_state, so it survives reconnects)
and the hash is computed here, in code — never by the model.

Auto mode ("just build me a dashboard") infers a one-sentence objective from
the profile so a non-analyst is never interviewed; guided mode stores what
the user actually said via the set_objective tool.

Complexity: O(columns + charts) per call — string work over an already-built
preview, no data access.
"""

from __future__ import annotations

import hashlib
from typing import Any

_MAX_OBJECTIVE_CHARS = 300
_AUDIENCES = ("exec", "analyst", "ops")


def normalize_objective(args: dict[str, Any]) -> dict[str, Any]:
    """A typed, length-capped objective from set_objective's args. Fixed enum
    for audience; everything else free text but capped (session state only)."""
    audience = str(args.get("audience", "")).strip().lower()
    metrics = args.get("metrics") or []
    return {
        "question": str(args.get("question", "")).strip()[:_MAX_OBJECTIVE_CHARS],
        "decision": str(args.get("decision", "")).strip()[:_MAX_OBJECTIVE_CHARS],
        "audience": audience if audience in _AUDIENCES else "exec",
        "time_grain": str(args.get("time_grain", "")).strip()[:40],
        "metrics": [str(m).strip()[:80] for m in metrics if str(m).strip()][:6],
        "mode": "guided",
    }


def infer_objective(preview: dict[str, Any], focus: str | None = None) -> dict[str, Any]:
    """Auto mode: a one-sentence objective from the profile — stated back for a
    single yes, never an interview."""
    columns = preview.get("columns") or []
    nums = [c["name"] for c in columns if c.get("kind") == "number"]
    cats = [c["name"] for c in columns if c.get("kind") == "category"]
    dates = [c["name"] for c in columns if c.get("kind") == "date"]
    metrics = ([focus] if focus and focus in nums else []) + [n for n in nums if n != focus]
    lens = f" by {cats[0]}" if cats else ""
    lens += f" over {dates[0]}" if dates else ""
    what = " and ".join(metrics[:2]) if metrics else "the data"
    return {
        "question": f"Get an overview of {what}{lens} in {preview.get('source', 'the file')}",
        "decision": "",
        "audience": "exec",
        "time_grain": "day" if dates else "",
        "metrics": metrics[:6],
        "mode": "auto",
    }


def plan_hash(objective: dict[str, Any], source: str, rows: int, titles: list[str]) -> str:
    """The approval binds to exactly this: the objective, the file identity
    (name + row count catches a re-upload), and the planned visuals."""
    basis = "|".join(
        [str(objective.get("question", "")), source, str(rows), *sorted(str(t) for t in titles)]
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def build_plan(
    objective: dict[str, Any],
    preview: dict[str, Any],
    deliverable: str,
) -> dict[str, Any]:
    """The single approval artifact (JSON-safe — it lives on build_state)."""
    titles = [str(c.get("title", "")) for c in preview.get("charts", [])]
    stats = [str(c.get("title")) for c in preview["charts"] if c.get("kind") == "stat"]
    others = [
        f"{c.get('title')} ({c.get('kind')})" for c in preview["charts"] if c.get("kind") != "stat"
    ]
    source = str(preview.get("source", "data.csv"))
    rows = int(preview.get("rows", 0))
    return {
        "objective": objective,
        "source": source,
        "rows": rows,
        "columns": len(preview.get("columns") or []),
        "analysis": ([f"stat cards: {', '.join(stats)}"] if stats else []) + others,
        "deliverable": deliverable,
        "hash": plan_hash(objective, source, rows, titles),
        "approved": False,
    }


def render_plan(plan: dict[str, Any]) -> str:
    """The ANALYSIS PLAN block the model relays (values are ground truth —
    computed here, quoted by the model)."""
    obj = plan.get("objective") or {}
    line = str(obj.get("question", ""))
    if obj.get("decision"):
        line += f" — informs: {obj['decision']}"
    analysis = "; ".join(plan.get("analysis") or []) or "—"
    cleaning = f"  Cleaning    : {plan['cleaning']}\n" if plan.get("cleaning") else ""
    return (
        "ANALYSIS PLAN\n"
        f"  Objective   : {line}\n"
        f"  Data        : {plan.get('source')} — {plan.get('rows'):,} rows × "
        f"{plan.get('columns')} cols\n"
        f"{cleaning}"
        f"  Analysis    : {analysis}\n"
        f"  Deliverable : {plan.get('deliverable')}\n"
        "  → Build it, or change something?"
    )
