"""PDD Template — Phase 4: the gap register and the clarifying-question gate.

The coverage diff between what the pipeline grounded/derived and what the
spine requires becomes RANKED questions with suggested answers. Answers
write back as `user_answered` content; anything the user waives renders in
Section 8 as an open item. The gate is code: `assert_exportable` RAISES
while a required gap is neither answered nor waived — an export cannot
happen around it.

An open item is a feature; a fabricated KPI is a liability.

Complexity: O(sections + gaps) per pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sila.services.pdd.registry import Section, flat_sections

# Sections whose absence blocks export (the manager-signs-this core).
BLOCKING_SECTIONS = ("1.1", "1.3", "2.1", "2.3", "3.1", "3.3")

_SUGGESTIONS: dict[str, str] = {
    "1.1": "One paragraph: what the process is, who runs it, and why it exists.",
    "1.2": "The concrete pains of today's manual process, one per line.",
    "1.3": "One objective per line as: objective | how success is measured.",
    "1.4": "One paragraph summarizing what the automation covers and what stays manual.",
    "2.1": "A prose walkthrough of how the work is done manually today.",
    "2.3": "One step per line as: step name | actor | system | inputs | outputs.",
    "2.5": "Typical volumes, e.g. 'invoices processed | 1,150 | invoices | monthly'.",
    "3.1": "One paragraph: what the automation will do end to end.",
    "3.3": "One automated step per line as: step | action | system | input | output.",
    "3.6": "Trigger, frequency, run window, and any calendar constraints.",
    "5.4": "Data classes touched, retention period, and PDPL considerations.",
    "6.3": "Where working data lives, how long it is kept, and how it is disposed.",
}


@dataclass
class Gap:
    id: str
    section_id: str
    question: str
    suggestion: str = ""
    blocking: bool = False
    status: str = "open"  # open | answered | waived
    answer: str = ""


@dataclass
class GapRegister:
    gaps: list[Gap] = field(default_factory=list)

    def by_id(self, gap_id: str) -> Gap:
        return self.resolve(gap_id)

    def resolve(self, ref: str) -> Gap:
        """A gap by its id ('gap-03') OR by section reference ('1.3' /
        '§1.3' / 'section 1.3') — models drop the ids when relaying, so the
        section number must work too (live bug). Raises KeyError listing the
        OPEN gaps so the caller can self-correct."""
        wanted = ref.strip().lower().removeprefix("section").strip().lstrip("§#").strip()
        gap = next((g for g in self.gaps if g.id.lower() == wanted), None)
        if gap is None:
            gap = next(
                (g for g in self.gaps if g.status == "open" and g.section_id == wanted), None
            )
        if gap is None:
            listing = ", ".join(f"{g.id}=§{g.section_id}" for g in self.gaps if g.status == "open")
            raise KeyError(f"no gap '{ref}' — open gaps: {listing or 'none'}")
        return gap

    def waive_all_open(self) -> int:
        count = 0
        for gap in self.gaps:
            if gap.status == "open":
                gap.status = "waived"
                count += 1
        return count

    @property
    def blockers(self) -> list[Gap]:
        return [g for g in self.gaps if g.blocking and g.status == "open"]

    @property
    def open_count(self) -> int:
        return sum(1 for g in self.gaps if g.status == "open")


def _payload_present(section: Section, payload: Any) -> bool:
    from sila.services.pdd.render import _payload_present as present

    return present(section, payload)


def build_register(
    content: dict[str, Any],
    *,
    doc_type: str,
    undefined_acronyms: list[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> GapRegister:
    """The coverage diff, ranked: blocking sections first (spine order), then
    the rest of the required spine, then metadata and glossary questions."""
    register = GapRegister()
    n = 0

    def _add(section_id: str, question: str, suggestion: str, blocking: bool) -> None:
        nonlocal n
        n += 1
        register.gaps.append(
            Gap(
                id=f"gap-{n:02d}",
                section_id=section_id,
                question=question,
                suggestion=suggestion,
                blocking=blocking,
            )
        )

    missing = [
        s
        for s in flat_sections(doc_type)
        if s.kind in ("prose", "list", "table")
        and s.empty_policy == "open_item"
        and s.id != "8"
        and not _payload_present(s, content.get(s.id))
    ]
    for section in sorted(missing, key=lambda s: (s.id not in BLOCKING_SECTIONS, s.id)):
        _add(
            section.id,
            f"Section {section.id} ({section.title}) has no grounded content — what should it say?",
            _SUGGESTIONS.get(section.id, f"Provide the {section.title.lower()}."),
            section.id in BLOCKING_SECTIONS,
        )
    for key, label in (("process_name", "process name"), ("department", "owning department")):
        if not str((meta or {}).get(key, "")).strip():
            _add("front.cover", f"What is the {label}?", "", True)
    for acronym in undefined_acronyms or []:
        _add("6.4", f"The transcript uses '{acronym}' — what does it stand for?", "", False)
    return register


def _rows_from_answer(section: Section, answer: str) -> list[list[str]]:
    """Free-text table answers: one row per line, cells split on '|', padded
    to the registry's column count. Deterministic, never guessed."""
    rows = []
    # The '#' column is auto-numbered — user cells start at the first REAL
    # column (live bug: the objective landed in the '#' cell).
    skip_hash = section.columns[:1] == ("#",)
    for line in answer.splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split("|")]
        if skip_hash:
            cells = ["", *cells]
        rows.append(([*cells, *[""] * len(section.columns)])[: len(section.columns)])
    return rows


def apply_answer(
    register: GapRegister, content: dict[str, Any], *, gap_id: str, answer: str, doc_type: str
) -> None:
    """Write the user's answer back as content (`user_answered` provenance) and
    close the gap. Empty answers don't close anything — loud, not silent."""
    text = answer.strip()
    if not text:
        raise ValueError("an empty answer cannot close a gap — waive it instead")
    gap = register.by_id(gap_id)
    section = next((s for s in flat_sections(doc_type) if s.id == gap.section_id), None)
    if section is not None:
        if section.kind == "table":
            rows = _rows_from_answer(section, text)
            if not rows:
                raise ValueError("a table answer needs at least one 'cell | cell' line")
            content.setdefault(section.id, {"rows": []})["rows"].extend(rows)
        elif section.kind == "list":
            items = [line.strip() for line in text.splitlines() if line.strip()]
            content.setdefault(section.id, []).extend(items)
        else:
            content[section.id] = text
    gap.status = "answered"
    gap.answer = text


def waive(register: GapRegister, gap_id: str) -> None:
    register.by_id(gap_id).status = "waived"


def open_item_rows(register: GapRegister, start_at: int = 1) -> list[list[str]]:
    """Section-8 rows for everything NOT answered — waived and still-open
    items render as the honesty section, never as invented content."""
    rows = []
    for gap in register.gaps:
        if gap.status == "answered":
            continue
        status = "Waived" if gap.status == "waived" else "Open"
        rows.append(
            [str(start_at + len(rows)), gap.section_id, gap.question, status, "Document owner"]
        )
    return rows


def coverage(content: dict[str, Any], doc_type: str) -> float:
    """% of required spine sections populated (extracted + answered + derived)."""
    required = [
        s
        for s in flat_sections(doc_type)
        if s.kind in ("prose", "list", "table") and s.empty_policy == "open_item" and s.id != "8"
    ]
    if not required:
        return 1.0
    populated = sum(1 for s in required if _payload_present(s, content.get(s.id)))
    return populated / len(required)


def assert_exportable(register: GapRegister) -> None:
    """THE gate: export is impossible while a blocking gap is neither answered
    nor waived. Raises with the exact questions still owed."""
    blockers = register.blockers
    if blockers:
        listed = "; ".join(f"{g.id} ({g.section_id}): {g.question}" for g in blockers)
        raise ValueError(
            f"export blocked — {len(blockers)} required gap(s) are unanswered and "
            f"unwaived: {listed}"
        )
