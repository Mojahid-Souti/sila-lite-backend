"""Analysis Engine Phase 5 — "explain this chart", bilingual (EN/AR).

Per-kind plain-language explanations rendered in code from the chart's own
recipe and data — then validated by the Phase-4 number guard against the
chart itself, so an explanation can never quote a number the chart doesn't
contain. Arabic strings are Modern Standard Arabic; full AR UI localization
remains the Phase-10 pass, but the accessibility feature ships now.

Complexity: O(data points) per chart (top/first/last lookups).
"""

from __future__ import annotations

from typing import Any

from sila.services.analysis.insights import check_numbers_from_slots


def _top(data: list[dict[str, Any]]) -> tuple[str, Any]:
    best = max(
        (d for d in data if isinstance(d.get("value"), int | float)),
        key=lambda d: d["value"],
        default=None,
    )
    return (str(best.get("name", "")), best.get("value")) if best else ("", None)


def explain_chart(chart: dict[str, Any]) -> dict[str, str]:
    """{'en': …, 'ar': …} for one chart spec. Raises ValueError if a rendered
    explanation somehow contains a number absent from the chart (the guard)."""
    kind = str(chart.get("kind", ""))
    title = str(chart.get("title", ""))
    metric = str(chart.get("metric") or "")
    by = str(chart.get("by") or "")
    data = [d for d in (chart.get("data") or []) if isinstance(d, dict)]

    if kind == "stat":
        value = chart.get("value")
        en = f"'{title}' is the {chart.get('agg', 'avg')} of {metric} over the whole file: {value}."
        ar = f"'{title}' هو {chart.get('agg', 'avg')} لعمود {metric} على كامل الملف: {value}."
    elif kind == "bar":
        top_name, top_value = _top(data)
        en = (
            f"'{title}' compares {metric} across {by} — taller bars mean more. "
            f"The leader is {top_name} at {top_value}."
        )
        ar = (
            f"'{title}' يقارن {metric} حسب {by} — العمود الأطول يعني قيمة أكبر. "
            f"المتصدر هو {top_name} بقيمة {top_value}."
        )
    elif kind == "pie":
        top_name, top_value = _top(data)
        en = (
            f"'{title}' shows how the whole splits by {by}. "
            f"The biggest slice is {top_name} ({top_value})."
        )
        ar = f"'{title}' يوضح توزيع الإجمالي حسب {by}. أكبر شريحة هي {top_name} ({top_value})."
    elif kind == "line":
        series = [str(s) for s in chart.get("series") or [metric] if s]
        first = str(data[0].get("x", "")) if data else ""
        last = str(data[-1].get("x", "")) if data else ""
        en = (
            f"'{title}' tracks {', '.join(series)} over {by or 'time'}, "
            f"from {first} to {last} — follow the line to see the direction."
        )
        ar = (
            f"'{title}' يتتبع {', '.join(series)} عبر {by or 'الزمن'} "
            f"من {first} إلى {last} — اتجاه الخط يوضح المسار."
        )
    elif kind == "scatter":
        en = (
            f"'{title}': every dot is one row — {by} runs left-to-right, {metric} "
            "bottom-to-top. Dots forming a diagonal mean the two move together."
        )
        ar = (
            f"'{title}': كل نقطة تمثل صفاً — {by} أفقياً و{metric} عمودياً. "
            "اصطفاف النقاط قطرياً يعني أن القيمتين تتحركان معاً."
        )
    elif kind == "histogram":
        top_name, top_value = _top(data)
        en = (
            f"'{title}' shows how often {metric} lands in each range. "
            f"The fullest range is {top_name} with {top_value} rows."
        )
        ar = (
            f"'{title}' يوضح تكرار قيم {metric} في كل نطاق. "
            f"النطاق الأكثر امتلاءً هو {top_name} بعدد {top_value} صفاً."
        )
    elif kind == "radar":
        groups = [str(s) for s in chart.get("series") or []]
        en = (
            f"'{title}' profiles {', '.join(groups)} across several metrics at once — "
            "each axis is scaled so 100 marks the strongest group on that metric."
        )
        ar = (
            f"'{title}' يقارن {', '.join(groups)} عبر عدة مقاييس معاً — "
            "كل محور مُدرّج بحيث تمثل 100 المجموعة الأقوى في ذلك المقياس."
        )
    elif kind == "table":
        en = f"'{title}' lists the underlying rows — sort or search it to inspect the raw records."
        ar = f"'{title}' يعرض الصفوف الأصلية — يمكن البحث فيها لفحص السجلات كما هي."
    else:
        en = f"'{title}' is a {kind} view of the data."
        ar = f"'{title}' عرض من نوع {kind} للبيانات."

    guard_basis = {**chart, "_scale": 100}  # radar's fixed 0–100 scale note
    check_numbers_from_slots(en, guard_basis)
    check_numbers_from_slots(ar, guard_basis)
    return {"en": en, "ar": ar}
