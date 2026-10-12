"""Analysis Engine Phase 5 — the Word report: the deliverable for the manager
who never opens the dashboard.

Sections follow the spec: Objective → Data & Quality → Method → Findings →
Recommendations → Dashboard figures → Appendix (cleaning recipe + semantic
layer). Every number is lifted verbatim from session state that was computed
in code (stat cards, finding phrasings, cleaning effects) — this module
formats, it never calculates, so the report reproduces the dashboard's
numbers exactly.

Complexity: O(findings + charts); pure function over build_state.
"""

from __future__ import annotations

from typing import Any

from sila.services.analysis import semantic

_MAX_FINDINGS = 10


def _objective_section(state: dict[str, Any]) -> str:
    objective = state.get("analysis_objective") or (state.get("analysis_plan") or {}).get(
        "objective"
    )
    if not isinstance(objective, dict) or not objective.get("question"):
        return "No explicit objective was set — this is a general overview of the data."
    lines = [f"Question: {objective['question']}"]
    if objective.get("decision"):
        lines.append(f"Decision it informs: {objective['decision']}")
    lines.append(f"Audience: {objective.get('audience', 'exec')}")
    if objective.get("metrics"):
        lines.append(f"Success metrics: {', '.join(objective['metrics'])}")
    return "\n".join(lines)


def _data_section(state: dict[str, Any]) -> str:
    plan = state.get("analysis_plan") or {}
    dash = state.get("last_dashboard") or {}
    source = plan.get("source") or dash.get("source") or "the attached file"
    rows = plan.get("rows") or dash.get("rows")
    cols = plan.get("columns")
    lines = [f"Source: {source}" + (f" — {rows:,} rows" if isinstance(rows, int) else "")]
    if isinstance(cols, int):
        lines[0] += f" × {cols} columns"
    quality = state.get("quality_summary")
    if isinstance(quality, dict) and quality.get("score") is not None:
        lines.append(f"Data quality score: {quality['score']}/100")
        for d in (quality.get("deductions") or [])[:3]:
            lines.append(f"  - {d.get('reason')} (−{d.get('points')})")
    applied = state.get("cleaning_recipe") or {}
    total_ops = sum(len(ops) for ops in applied.values() if isinstance(ops, list))
    if total_ops:
        lines.append(f"Cleaning applied: {total_ops} fix(es) — details in the appendix.")
    return "\n".join(lines)


def _method_section(state: dict[str, Any]) -> str:
    findings = [f for f in (state.get("findings") or []) if isinstance(f, dict)]
    if not findings:
        return "Descriptive profiling and deterministic chart selection over the full file."
    methods: list[str] = []
    for f in findings:
        slots = f.get("statement_slots") or {}
        test = slots.get("test")
        label = f"{f.get('kind')}" + (f" ({test})" if test else "")
        if label not in methods:
            methods.append(label)
    tests_run = max(
        (int((f.get("statement_slots") or {}).get("tests_run") or 1) for f in findings),
        default=1,
    )
    lines = ["Analyses run (all computed in code over the full file): " + "; ".join(methods) + "."]
    if tests_run > 1:
        lines.append(
            f"P-values were Holm-Bonferroni corrected across the {tests_run} tests run together."
        )
    lines.append(
        "Confidence policy: findings on fewer than 30 rows are capped at LOW confidence "
        "and are never described as statistically significant."
    )
    return "\n".join(lines)


def _findings_section(state: dict[str, Any]) -> str:
    findings = [f for f in (state.get("findings") or []) if isinstance(f, dict)]
    if not findings:
        return "No statistical findings were produced in this session."
    lines = []
    for f in findings[:_MAX_FINDINGS]:
        lines.append(f"[{f.get('confidence', 'low')} confidence] {f.get('phrasing', '')}")
        for caveat in f.get("caveats") or []:
            lines.append(f"  Caveat: {caveat}")
    return "\n".join(lines)


def _recommendations_section(state: dict[str, Any]) -> str:
    insights = [i for i in (state.get("insights") or []) if isinstance(i, dict)]
    if insights:
        return "\n".join(f"- {i.get('so_what', '')}" for i in insights[:5] if i.get("so_what"))
    summary = state.get("exec_summary") or {}
    bullets = summary.get("bullets") or []
    if bullets:
        return "\n".join(f"- {b}" for b in bullets)
    return "Run the analysis tools and rank the insights to generate recommendations."


def _figures_section(state: dict[str, Any]) -> str:
    stats = state.get("dashboard_stats") or []
    if not stats:
        return "No dashboard has been built in this session."
    lines = []
    for card in stats:
        line = f"{card.get('title')}: {card.get('value')}"
        if card.get("sub"):
            line += f" ({card['sub']})"
        lines.append(line)
    return "\n".join(lines)


def _appendix_section(state: dict[str, Any]) -> str:
    from sila.services.analysis import cleaning

    lines: list[str] = []
    applied = state.get("cleaning_recipe") or {}
    for filename, ops in applied.items():
        if isinstance(ops, list) and ops:
            lines.append(f"Cleaning recipe for {filename} (replayable, in order):")
            lines += [f"  {i}. {cleaning.describe_op(op)}" for i, op in enumerate(ops, start=1)]
    layer = state.get("semantic_layer") or []
    if layer:
        lines.append("Metric definitions (identical in chat, Grafana, and Power BI):")
        lines += [f"  - {line}" for line in semantic.describe_layer(layer)]
    return "\n".join(lines) or "No cleaning was applied and no metric registry was recorded."


def slide_title(kind: str, slots: dict[str, Any]) -> str:
    """A short HEADLINE per finding kind, composed from slot strings only —
    slide titles were the finding's full phrasing truncated mid-word ('acr…')
    which read broken (live feedback). No numbers → nothing to truncate."""
    metric = str(slots.get("metric") or slots.get("target") or "the metric")
    if kind == "trend":
        if "changepoint_date" in slots:
            return f"A shift in {metric}"
        if "forecast" in slots:
            return f"{metric}: the week ahead"
        slope = slots.get("slope_per_day")
        if isinstance(slope, int | float):
            return f"{metric} is {'falling' if slope < 0 else 'rising'}"
        return f"{metric} over time"
    if kind == "anomaly":
        return f"Unusual days in {metric}"
    if kind == "difference":
        group = slots.get("group_high")
        return f"{group} leads on {metric}" if group else f"{metric} differs across groups"
    if kind == "correlation":
        return f"{slots.get('x', 'One metric')} moves with {slots.get('y', 'another')}"
    if kind == "concentration":
        return f"{slots.get('top_group', 'A few groups')} dominates {metric}"
    if kind == "driver":
        return f"What moves {metric}"
    if kind == "segment":
        return f"Where {metric} varies most"
    return metric


def deck_slides(
    insights: list[dict[str, Any]],
    exec_summary: dict[str, Any] | None,
    style: str,
) -> list[dict[str, Any]]:
    """The deck outline per audience. 'executive': headline + what happened +
    so-what + one note, plain language. 'technical': full detail — confidence
    and every caveat. All text verbatim from the guarded insight cards."""
    slides: list[dict[str, Any]] = []
    bullets = (exec_summary or {}).get("bullets") or []
    if bullets:
        slides.append({"title": "Executive summary", "bullets": bullets[:5]})
    for card in insights[:8]:
        slots = card.get("statement_slots") or {}
        title = slide_title(str(card.get("kind", "")), slots)
        what = str(card.get("what_happened", ""))
        so_what = str(card.get("so_what", ""))
        caveats = [str(c) for c in card.get("caveats") or []]
        if style == "executive":
            body = [what, so_what]
            if caveats:
                body.append(f"Note: {caveats[0]}")
        else:
            body = [what, so_what, f"Confidence: {card.get('confidence', 'low')}"]
            body += [f"Caveat: {c}" for c in caveats[:3]]
        slides.append({"title": title, "bullets": [b for b in body if b]})
    return slides


def build_report_sections(state: dict[str, Any]) -> list[dict[str, str]]:
    """The full report as heading/body sections (the create_docx shape)."""
    return [
        {"heading": "Objective", "body": _objective_section(state)},
        {"heading": "Data & Quality", "body": _data_section(state)},
        {"heading": "Method", "body": _method_section(state)},
        {"heading": "Findings", "body": _findings_section(state)},
        {"heading": "Recommendations", "body": _recommendations_section(state)},
        {"heading": "Dashboard figures", "body": _figures_section(state)},
        {"heading": "Appendix", "body": _appendix_section(state)},
    ]
