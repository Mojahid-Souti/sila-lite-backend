"""PDD Template — the document renderer: registry + content → styled .docx.

The professional-output rules from the spec live HERE, not in any prompt:
  - no empty headings, ever — a missing required section renders an explicit
    "Not discussed — see Open Item #n" line and lands in Section 8; sections
    whose policy is `suppress` disappear heading and all;
  - every table renders with at least one meaningful row or not at all;
  - numbering comes verbatim from the registry (identical across doc types);
  - the classification footer and ToC are part of the base style.

Content is a plain dict keyed by section id:
  prose  → str
  list   → list[str]
  table  → {"rows": [[cell, …], …]}           (columns come from the registry)
  image  → {"note": str} (Phase 5 replaces the note with the rendered figure)

Complexity: O(sections + cells) single pass.
"""

from __future__ import annotations

from typing import Any

from sila.services.pdd import docx
from sila.services.pdd.registry import (
    DOC_TYPE_LABEL,
    Section,
    appendices,
    front_matter,
    sections_for,
)

_OPEN_ITEM_OWNER = "Document owner"


def _heading_style(section: Section) -> str:
    depth = section.id.count(".")
    return ("Heading1", "Heading2", "Heading3")[min(depth, 2)]


def _has_rows(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("rows"))


def _payload_present(section: Section, payload: Any) -> bool:
    if section.kind == "table":
        return _has_rows(payload)
    if section.kind == "list":
        return isinstance(payload, list) and any(str(x).strip() for x in payload)
    if section.kind == "image":
        return isinstance(payload, dict) and bool(payload.get("note") or payload.get("rows"))
    return isinstance(payload, str) and bool(payload.strip())


def _render_payload(section: Section, payload: Any, out: list[str]) -> None:
    if section.note:
        out.append(docx.paragraph(section.note, italic=True, color="55607A", size=18))
    if section.kind == "table":
        rows = [[str(c) for c in row] for row in payload["rows"]]
        out.append(docx.table(list(section.columns), rows))
    elif section.kind == "list":
        out.extend(docx.bullet(str(item)) for item in payload if str(item).strip())
    elif section.kind == "image":
        if payload.get("note"):
            out.append(docx.paragraph(str(payload["note"]), italic=True, color="55607A"))
        if payload.get("steps"):
            # P5: the flowchart is GENERATED from the structured steps — never
            # a placeholder box, never a second model call.
            out.append(docx.flow_diagram([str(x) for x in payload["steps"]]))
        if payload.get("rows"):
            columns = [str(c) for c in payload.get("columns") or section.columns]
            rows = [[str(c) for c in r] for r in payload["rows"]]
            if section.id == "7":  # the color-coded RACI grid
                out.append(docx.raci_matrix(columns, rows))
            else:
                out.append(docx.table(columns, rows))
    else:  # prose
        out.extend(docx.paragraph(p) for p in str(payload).split("\n\n") if p.strip())


def _open_item_number(
    section_id: str,
    question: str,
    open_items: list[tuple[str, str]],
    known: dict[str, int],
    base: int,
) -> int:
    """The Section-8 row number a body line should reference. A gap-register
    row for this section already exists → point at IT (the export used to
    print a waived question AND a twin 'not discussed' row — live bug);
    otherwise append a new auto item after the provided rows."""
    if section_id in known:
        return known[section_id]
    open_items.append((section_id, question))
    return base + len(open_items)


def _render_section(
    section: Section,
    content: dict[str, Any],
    open_items: list[tuple[str, str]],
    out: list[str],
    known: dict[str, int],
    base: int,
) -> bool:
    """Render one section (and children). Returns False when suppressed —
    the caller drops the heading too, so a bare heading can never ship."""
    body: list[str] = []
    payload = content.get(section.id)
    if section.kind == "image" and section.source and not _payload_present(section, payload):
        payload = _figure_from_source(section, content)
        if payload is not None:
            content[section.id] = payload
    if _payload_present(section, payload):
        _render_payload(section, payload, body)
    elif section.kind != "container":
        if section.empty_policy == "suppress":
            return False
        number = _open_item_number(
            section.id, f"{section.title}: content was not discussed", open_items, known, base
        )
        body.append(
            docx.paragraph(
                f"Not discussed — see Open Item #{number}.",
                italic=True,
                color="8A93A8",
            )
        )
    rendered_children: list[str] = []
    for child in section.children:
        _render_section(child, content, open_items, rendered_children, known, base)
    if not body and not rendered_children:
        return False  # nothing under this heading at all → suppress it
    out.append(docx.paragraph(section.numbered_title, style=_heading_style(section)))
    out.extend(body)
    out.extend(rendered_children)
    return True


_STEP_NAME_COLUMNS = ("Step name", "Step")


def _figure_from_source(section: Any, content: dict[str, Any]) -> dict[str, Any] | None:
    """An image section with a `source` (2.2 ← 2.3, 3.2 ← 3.3) materializes
    its flowchart FROM the structured rows — present rows, present figure."""
    src = content.get(section.source)
    if not _has_rows(src):
        return None
    source_section = next(
        (s for s in sections_for("macro") for s in [s, *_walk(s)] if s.id == section.source), None
    )
    if source_section is None:
        return None
    name_idx = next(
        (
            source_section.columns.index(c)
            for c in _STEP_NAME_COLUMNS
            if c in source_section.columns
        ),
        1,
    )
    steps = [
        str(row[name_idx]).strip()
        for row in src["rows"]
        if len(row) > name_idx and str(row[name_idx]).strip()
    ]
    if not steps:
        return None
    label = "AS-IS" if section.id.startswith("2") else "TO-BE"
    return {
        "note": f"Figure: {label} process flow — generated from §{section.source}.",
        "steps": steps,
    }


def _walk(section: Any) -> list[Any]:
    out = []
    for child in section.children:
        out.append(child)
        out.extend(_walk(child))
    return out


def _cover(meta: dict[str, Any], out: list[str]) -> None:
    doc_type = str(meta.get("doc_type", "rpa"))
    out.append(docx.paragraph(str(meta.get("process_name", "Process")), style="Title"))
    out.append(docx.paragraph(DOC_TYPE_LABEL[doc_type], style="Subtitle"))
    out.append(
        docx.table(
            ["Field", "Value"],
            [
                ["Version", str(meta.get("version", "0.1"))],
                ["Author", str(meta.get("author", ""))],
                ["Department", str(meta.get("department", ""))],
                ["Date", str(meta.get("date", ""))],
                ["Classification", str(meta.get("classification", "CONFIDENTIAL"))],
            ],
        )
    )
    out.append(docx.page_break())


def _document_control(meta: dict[str, Any], out: list[str]) -> None:
    out.append(docx.paragraph("Document Control", style="Heading1"))
    if meta.get("abstract"):
        out.append(docx.paragraph("Abstract", style="Heading3"))
        out.append(docx.paragraph(str(meta["abstract"])))
    out.append(
        docx.table(
            ["Field", "Value"],
            [
                ["Document Type", DOC_TYPE_LABEL[str(meta.get("doc_type", "rpa"))]],
                ["Document Tags", ", ".join(meta.get("tags") or [])],
                ["Current Status", str(meta.get("status", "Draft"))],
            ],
        )
    )
    history = meta.get("history") or [
        [
            str(meta.get("version", "0.1")),
            str(meta.get("author", "")),
            "Initial draft",
            str(meta.get("date", "")),
        ]
    ]
    out.append(docx.paragraph("Document History", style="Heading3"))
    out.append(docx.table(["Version", "Author", "Comments", "Date"], history))


def render_document(doc_type: str, meta: dict[str, Any], content: dict[str, Any]) -> bytes:
    """The whole document: front matter → sections 1-8 → appendices → .docx."""
    meta = {**meta, "doc_type": doc_type}
    out: list[str] = []
    open_items: list[tuple[str, str]] = []
    provided_rows = (
        (content.get("8") or {}).get("rows") if isinstance(content.get("8"), dict) else []
    ) or []
    base = len(provided_rows)
    known: dict[str, int] = {}
    for i, row in enumerate(provided_rows, start=1):
        sid = str(row[1]).strip() if len(row) > 1 else ""
        known.setdefault(sid, i)

    for fm in front_matter():
        if fm.kind == "cover":
            _cover(meta, out)
        elif fm.kind == "control":
            _document_control(meta, out)
        elif fm.kind == "toc":
            out.append(docx.paragraph("Table of Contents", style="Heading1"))
            out.append(docx.toc_field())
            out.append(docx.page_break())
        elif fm.kind == "table":
            payload = content.get(fm.id)
            if _has_rows(payload):
                out.append(docx.paragraph(fm.title, style="Heading1"))
                if fm.note:
                    out.append(docx.paragraph(fm.note, italic=True, color="55607A", size=18))
                out.append(
                    docx.table(list(fm.columns), [[str(c) for c in r] for r in payload["rows"]])
                )
            else:
                number = _open_item_number(
                    fm.id, f"{fm.title}: not provided", open_items, known, base
                )
                out.append(docx.paragraph(fm.title, style="Heading1"))
                out.append(
                    docx.paragraph(
                        f"Not discussed — see Open Item #{number}.",
                        italic=True,
                        color="8A93A8",
                    )
                )

    for section in sections_for(doc_type):
        if section.id == "8":
            continue  # rendered last, with the auto-collected items merged in
        _render_section(section, content, open_items, out, known, base)

    # Section 8 — the honesty section: provided rows + everything collected
    # (auto items only for sections NO register row already covers).
    eight = next(s for s in sections_for(doc_type) if s.id == "8")
    auto = [
        [str(base + i + 1), sid, question, "Open", _OPEN_ITEM_OWNER]
        for i, (sid, question) in enumerate(open_items)
    ]
    rows = [*provided_rows, *auto]
    out.append(docx.paragraph(eight.numbered_title, style="Heading1"))
    if rows:
        out.append(docx.table(list(eight.columns), [[str(c) for c in r] for r in rows]))
    else:
        out.append(docx.paragraph("No open items — every section is grounded or answered."))

    for appendix in appendices():
        payload = content.get(appendix.id)
        if not _payload_present(appendix, payload):
            if appendix.empty_policy == "suppress":
                continue
            payload = "Pending." if appendix.kind == "prose" else {"rows": []}
            if appendix.kind != "prose" and not payload["rows"]:
                continue
        out.append(docx.paragraph(f"Appendix {appendix.id} — {appendix.title}", style="Heading1"))
        _render_payload(appendix, payload, out)

    label = f"{str(meta.get('classification', 'CONFIDENTIAL'))} · {meta.get('process_name', '')}"
    return docx.assemble("".join(out), footer_label=label)
