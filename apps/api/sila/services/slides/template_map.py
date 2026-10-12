"""Translate a generated deck into slides a corporate template can carry.

Kept apart from `template_fill` deliberately: that module knows how to write
text into one particular .pptx, this one knows what a generated deck MEANS and
which stencil says it best. Swapping templates changes the stencil book, not
this mapping; adding a layout to the generator changes this mapping, not the
filler.

The rules here are the ones that make a machine-made deck stop looking
machine-made: a card is always a label AND a sentence, a sentence is shortened
rather than spilled into the next box, and the number of cards comes from the
stencil rather than from however many the model happened to produce.

Complexity: O(total containers across the deck).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sila.services.slides.template_fill import OMANTEL, Slide, Slot, Stencil

# Which stencil best carries each generated layout. The model chooses layouts by
# MEANING (a process, a comparison, a table); this says what that means in one
# particular template's vocabulary.
_LAYOUT_KIND: dict[str, str] = {
    "title-hero": "title",
    "quote-focus": "statement",
    "image-feature": "statement",
    "data-showcase": "table",
    "comparison": "table",
    "process-flow": "points",
    "timeline": "points",
    "3-column-cards": "points",
    "split-grid": "points",
    "metric-showcase": "points4",
}

# A card is a fixed box, but CUTTING text to fit lost the end of every sentence.
# The filler scales a card's type down instead; these are the outer bounds past
# which the copy is genuinely too long for a slide and the tail moves to the
# speaker notes, always at a sentence boundary so nothing ends mid-thought.
_BODY_CHARS = 320
_LABEL_CHARS = 40
_TITLE_CHARS = 64
_CELL_CHARS = 110

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CONT_TITLE = re.compile(r"^(?P<stem>.+?)\s*\((?:cont\.?|continued)\)\s*$", re.I)

_ACRONYM = re.compile(r"^[A-Z0-9&/.-]{2,5}$")

# Proper nouns keep their capital in EVERY position. Sentence case governs
# style, not names — the rule that only spared inner-capital words flattened
# "Sila" and "Bazaar" the moment they appeared after the first word, and left
# one slide with "the bazaar" in its title and "the Bazaar" in its own table.
PROPER_NOUNS = frozenset(
    {
        "sila",
        "bazaar",
        "omantel",
        "oman",
        "omani",
        "powerpoint",
        "outlook",
        "teams",
        "excel",
        "word",
        "dify",
        "grafana",
    }
)


def sentence_case(title: str) -> str:
    """Headlines are sentence case; Title Case is off-brand.

    Only later words are re-cased, and a word that is an acronym ("Q2", "KPI")
    or carries an inner capital ("Bazaar", "PowerPoint", "Omantel") is left
    exactly as written — the rule is about casing style, not about flattening
    proper nouns.
    """
    words = title.split()
    if not words:
        return title
    out = [words[0]]
    for word in words[1:]:
        core = word.strip(".,;:()")
        if (
            not core
            or core.lower() in PROPER_NOUNS
            or _ACRONYM.match(core)
            or any(ch.isupper() for ch in core[1:])
        ):
            out.append(word)
        elif core[:1].isupper():
            out.append(word[:1].lower() + word[1:])
        else:
            out.append(word)
    return " ".join(out)


def _reauthor(slide: object) -> tuple[str, list[tuple[str, str]]]:
    """Undo the generated engine's chunking: recover the AUTHORED items.

    `schema` splits a long body into sibling containers, giving the heading to
    the first and `heading=""` to the rest, then paginates past four of them
    onto "(cont.)" slides. That is right for the generated renderer, whose card
    grid holds arbitrary chunks. It is catastrophic here, where every slot is a
    designed diamond: one sentence arrived as eight fragments across two slides,
    each fragment reading as a heading.

    A container with no heading is therefore a CONTINUATION of the one before
    it, not an item of its own, and is joined back onto it.
    """
    items: list[tuple[str, str]] = []
    for container in getattr(slide, "containers", []):
        heading = _tidy(getattr(container, "heading", ""))
        body = _tidy(getattr(container, "body", ""))
        if not heading and items:
            label, previous = items[-1]
            items[-1] = (label, f"{previous} {body}".strip())
        elif heading or body:
            items.append((heading, body))
    return _tidy(getattr(slide, "slide_title", "")), items


@dataclass
class _Section:
    """A slide's title, its authored items, and the source slide they came from.

    One object, because keeping them in separate lists is what silently shifted
    every title one slide away from its content: merging a "(cont.)" page
    shortens the section list but not the raw slide list, so an index into one
    stops meaning the same thing in the other.
    """

    title: str
    items: list[tuple[str, str]]
    source: object


def _merge_continuations(slides: list[object]) -> list[_Section]:
    """Fold every "(cont.)" page back into the slide it continues.

    Deck length must follow the content, not the shape count — four consecutive
    slides titled "Q2 roadmap and the ask" were the paginator running out of
    slots, never a deliberate continuation.
    """
    merged: list[_Section] = []
    for slide in slides:
        title, items = _reauthor(slide)
        cont = _CONT_TITLE.match(title)
        stem = _tidy(cont.group("stem")) if cont else title
        if merged and stem and merged[-1].title == stem:
            # The page break can fall MID-ITEM: a continuation whose first
            # container has no heading is the tail of the previous slide's last
            # item, not a new one.
            previous = merged[-1].items
            if items and not items[0][0] and previous:
                label, body = previous[-1]
                previous[-1] = (label, f"{body} {items[0][1]}".strip())
                items = items[1:]
            previous.extend(items)
            continue
        merged.append(_Section(stem, items, slide))
    return merged


_NUMBER = re.compile(r"^\s*([\d,]+(?:\.\d+)?%?)\s*$")


def _compact(figure: str) -> str:
    """A KPI figure at glance size: 125,194.71 → 125.2K, 1,472.88 → 1.5K,
    47.31 → 47.3, 914.79 → 915. A percent keeps one decimal. The full value is
    in the data the slide was built from; the slide is for the magnitude."""
    text = figure.strip()
    pct = text.endswith("%")
    raw = text.rstrip("%").replace(",", "")
    try:
        n = float(raw)
    except ValueError:
        return text
    if pct:
        return f"{n:.1f}".rstrip("0").rstrip(".") + "%"
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            short = f"{n / cut:.1f}".rstrip("0").rstrip(".")
            return f"{short}{suffix}"
    if abs(n) >= 100:
        return f"{n:.0f}"
    return f"{n:.1f}".rstrip("0").rstrip(".")


def _numeric_items(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(label, figure) pairs when EVERY item carries a bare figure.

    All-or-nothing, so prose is never dressed up as a metric.
    """
    pairs: list[tuple[str, str]] = []
    for label, body in items:
        if _NUMBER.match(body) and label:
            pairs.append((_tidy(label), _tidy(body)))
        elif _NUMBER.match(label) and body:
            pairs.append((_tidy(body), _tidy(label)))
        else:
            return []
    return pairs if len(pairs) >= 2 else []


def _as_chart(title: str, items: list[tuple[str, str]]) -> object | None:
    """A slide whose items are all counts is a chart, not a chain of diamonds.

    Built only when EVERY item carries a bare number, so prose is never coerced
    into a graph.
    """
    from sila.services.slides.schema import ChartSpec

    if len(items) < 2:
        return None
    pairs: list[tuple[str, float]] = []
    for label, body in items:
        match = _NUMBER.match(body) or _NUMBER.match(label)
        if match is None:
            return None
        name = label if _NUMBER.match(body) else body
        if not name.strip():
            return None
        pairs.append((_tidy(name), float(match.group(1).replace(",", ""))))
    return ChartSpec.model_validate(
        {
            "kind": "bar",
            "categories": [name for name, _ in pairs],
            "series": [{"name": title or "Total", "values": [value for _, value in pairs]}],
        }
    )


def _tidy(text: object) -> str:
    return " ".join(str(text).split())


def _whole_sentences(text: object, limit: int) -> tuple[str, str]:
    """Split at a SENTENCE boundary: what fits, and what moves to the notes.

    Never mid-word and never with an ellipsis — a reader who cannot finish a
    sentence has lost the information, which is worse than not showing it.
    """
    clean = _tidy(text)
    if len(clean) <= limit:
        return clean, ""
    kept: list[str] = []
    rest: list[str] = []
    used = 0
    for sentence in _SENTENCE_END.split(clean):
        if not rest and used + len(sentence) + 1 <= limit:
            kept.append(sentence)
            used += len(sentence) + 1
        else:
            rest.append(sentence)
    if not kept:  # one sentence longer than the whole budget
        return clean, ""
    return " ".join(kept), " ".join(rest)


def _clip(text: object, limit: int) -> str:
    """Labels and table cells only — short strings where a sentence split makes
    no sense. Body copy goes through `_whole_sentences`."""
    clean = _tidy(text)
    return clean if len(clean) <= limit else clean[:limit].rsplit(" ", 1)[0] or clean[:limit]


def deck_to_slides(deck: object, *, stencils: dict[str, Stencil] | None = None) -> list[Slide]:
    """Translate a generated `schema.Deck` into stencil-filled slides.

    Content first, layout second: the authored items are recovered, counted, and
    only then matched to a stencil that holds exactly that many. Nothing is
    sliced to fit a shape, and no slide is created because a shape ran out.
    """
    book = stencils or OMANTEL
    out: list[Slide] = []
    raw = list(getattr(deck, "slides", []))
    seen_titles: set[str] = set()
    _used: dict[str, int] = {}

    for index, section in enumerate(_merge_continuations(raw)):
        title, items, source = section.title, section.items, section.source
        # The title and the body must come from the SAME section object.
        assert source is None or _tidy(getattr(source, "slide_title", "")).startswith(
            title[:20]
        ), f"title/body drift: {title!r} paired with {getattr(source, 'slide_title', None)!r}"
        layout = str(getattr(source, "layout_type", ""))
        kind = _LAYOUT_KIND.get(layout, "points")
        heading = sentence_case(_clip(title, _TITLE_CHARS))

        # A figure the model built itself always wins.
        chart = next(
            (c.chart for c in getattr(source, "containers", []) if getattr(c, "chart", None)),
            None,
        )
        table = next(
            (c for c in getattr(source, "containers", []) if getattr(c, "rows", None)), None
        )
        numeric = _numeric_items(items)
        if numeric and kind != "table":
            # A metrics slide states its figures; a data slide graphs them. Both
            # beat four diamonds of prose with the numbers left out.
            out.append(
                Slide(
                    "kpi",
                    title=heading,
                    subtitle=_subtitle(source),
                    slots=tuple(Slot(_compact(value), label) for label, value in numeric[:4]),
                )
            )
            _used[kind] = _used.get(kind, 0) + 1
            continue
        if chart is None and kind != "table":
            chart = _as_chart(heading, items)
        if chart is not None:
            # No derived total. A breakdown may or may not already include its
            # own total ("25 registered" beside 12 / 3 / 8 sums to 48, which
            # describes nothing), and the two cases are indistinguishable from
            # the numbers alone. A wrong figure on a board slide is worse than
            # no figure, so a total is the model's to author.
            out.append(Slide("chart", title=heading, subtitle=_subtitle(source), chart=chart))
            continue
        if table is not None:
            rows = tuple(
                tuple(_cell(cell) for cell in row) for row in table.rows if any(str(c) for c in row)
            )
            if rows:
                out.append(Slide("table", title=heading, subtitle=_subtitle(source), rows=rows))
                continue

        # The cover keeps its stencil even when the model hung content off it —
        # converting it to a content layout left the deck with no title slide.
        if kind == "title" and index == 0:
            out.append(Slide("title", title=heading, notes=_notes(items)))
            continue

        if not items:
            out.append(Slide("section" if kind != "statement" else "statement", title=heading))
            continue

        # Count the items, THEN pick the stencil that holds exactly that many.
        stencil, kind = _stencil_for(
            len(items), book, _used, sequential=_is_sequential(heading, items)
        )
        _used[kind] = _used.get(kind, 0) + 1
        capacity = stencil.max_slots or len(items)
        slots: list[Slot] = []
        notes: list[str] = []
        # Budget the body against what THIS stencil's card actually holds, not a
        # fixed 320 characters. The card holds 110-150; at 320 the renderer
        # scaled the type past the legibility floor and the build-time guard
        # rejected the deck — four times in a row, the model retrying blind with
        # shorter text each round. The surplus goes to the speaker notes, which
        # is where it went already for anything past the slot count.
        budget = stencil.slot_chars or _BODY_CHARS
        for label, body in items[:capacity]:
            # A node label that merely repeats the slide's heading is the title
            # pasted into a content slot.
            slot, overflow = _slot("" if _tidy(label) == heading else label, body, budget=budget)
            slots.append(slot)
            if overflow:
                notes.append(f"{slot.label}: {overflow}")
        # Items past the stencil's slot count go to the notes rather than onto a
        # second slide wearing the same title.
        notes.extend(f"{lbl}: {txt}".strip(": ") for lbl, txt in items[capacity:])

        if heading in seen_titles:
            # A title may appear once. A repeat here means the model wrote two
            # slides with one heading, so the second carries its own first item.
            heading = _clip(slots[0].label if slots else heading, _TITLE_CHARS)
        seen_titles.add(heading)
        out.append(
            Slide(
                kind,
                title=heading,
                subtitle=_subtitle(source),
                slots=tuple(slots),
                notes=" ".join(notes),
            )
        )
    return out


# Words that mean the items really are a sequence. The vertical diamond chain
# implies an order, so parallel content ("four problems") must not get it.
_SEQUENCE_WORDS = (
    "step",
    "stage",
    "phase",
    "process",
    "how it works",
    "how an agent",
    "lifecycle",
    "journey",
    "workflow",
    "roadmap",
    "timeline",
    "first",
    "then",
    "finally",
)

# No layout may carry more than this many slides, so a deck does not become two
# stencils alternating.
_MAX_PER_STENCIL = 2


def _is_sequential(title: str, items: list[tuple[str, str]]) -> bool:
    haystack = " ".join([title, *(label for label, _ in items)]).lower()
    return any(word in haystack for word in _SEQUENCE_WORDS)


def _stencil_for(
    count: int,
    book: dict[str, Stencil],
    used: dict[str, int],
    *,
    sequential: bool,
) -> tuple[Stencil, str]:
    """The stencil that fits the item count, the content's shape, and variety.

    Preference order, then anything not yet over its quota — a deck of four
    identical diamond timelines is what this exists to prevent.
    """
    order = ["points" if sequential else "points4", "points4" if sequential else "points"]
    for kind in order:
        if kind in book and used.get(kind, 0) < _MAX_PER_STENCIL:
            return book[kind], kind
    for kind in order:
        if kind in book:
            return book[kind], kind
    return book["points"], "points"


def _label_from(text: str) -> tuple[str, str]:
    """A heading and what remains, broken at a natural boundary.

    Never a mid-word or mid-phrase cut: a sentence if one fits, else the leading
    clause, else the whole sentence left for the filler to scale. "the current
    state of the agent" as a bold heading is the defect this avoids.
    """
    sentence, rest = _first_sentence(text)
    if len(sentence) <= _LABEL_CHARS:
        return sentence, rest
    clause, _, tail = sentence.partition(", ")
    if clause and len(clause) <= _LABEL_CHARS:
        return clause, f"{tail} {rest}".strip()
    return sentence, rest


def _runs_on(label: str, body: str) -> bool:
    """Does the body continue the label's sentence?

    "No visibility of what is" / "live. Without a register…" is one sentence the
    model split across the two fields; joined and re-broken it reads properly.
    """
    return bool(label) and bool(body) and label[-1] not in ".!?:" and body[:1].islower()


def _slot(label: str, body: str, *, budget: int = _BODY_CHARS) -> tuple[Slot, str]:
    """One card: a complete label and a complete sentence, never the same text
    twice. Where a chunk arrived with no heading of its own, the label is the
    body's first sentence and that sentence is REMOVED from the body — printing
    it in both places is what made every fragment appear twice."""
    text = _tidy(body)
    name = _tidy(label)
    if _runs_on(name, text):
        name, text = _label_from(f"{name} {text}")
    elif not name:
        name, text = _label_from(text)
    # The label shares the card with the body, so it spends the same budget.
    kept, overflow = _whole_sentences(text, max(40, budget - len(name)))
    # A label carries no terminal stop — it is a heading, not a sentence.
    return Slot(name.rstrip("."), kept), overflow


def _first_sentence(text: str) -> tuple[str, str]:
    parts = _SENTENCE_END.split(text, maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (text, "")


def _subtitle(source: object) -> str:
    """The model's one-line framing for a slide, if it wrote one.

    Never invented here: a subtitle is supposed to say what the reader should
    TAKE from the slide, and a generated restatement of the title would be
    worse than none.
    """
    return _clip(_tidy(getattr(source, "subtitle", "")), 120)


def _notes(items: list[tuple[str, str]]) -> str:
    return " ".join(f"{label}: {body}".strip(": ") for label, body in items)


def _cell(value: object) -> str:
    """A table cell keeps whole sentences — a cell cut mid-phrase ("dedup and
    rate") is unreadable, and the column can carry a shorter sentence."""
    kept, _ = _whole_sentences(str(value), _CELL_CHARS)
    return kept
