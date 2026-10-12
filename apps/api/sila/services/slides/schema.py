"""Slides Engine v2 — P0: the deck schema and the validation gate.
P2 adds a geometry-derived character-capacity backstop (reflow.py) and
auto-pagination for slides that still overflow after reflow.

The model is a structured content architect: it selects from CLOSED enums
(five layout components, three palettes, three container types) and obeys
density limits (8-word titles, 5-word headings, 25-word bodies, ≤4
containers per slide — the 8-second rule). This module is the gate between
the model and the renderer:

  - unknown layouts/palettes/container types are REJECTED loudly (the
    caller retries the model — the renderer never sees them);
  - overlong text is REFLOWED deterministically where capacity allows
    (title spill → subtitle, heading spill → body, body spill → an extra
    container). The word-count limits are the editorial rule; a second,
    geometry-derived char-capacity check (reflow.py) then catches the
    rare case where content still overflows its box physically (e.g. a
    title with a few unusually long words) even though it satisfied the
    word count — whichever limit is stricter wins;
  - a slide whose reflowed containers still exceed the per-slide cap is
    PAGINATED across continuation slides rather than rejected: grid
    layouts continue as themselves, single-message layouts (title-hero,
    quote-focus) downgrade continuation pages to 3-column-cards;
  - slide numbers are renumbered in code; empty containers are dropped
    (no orphaned elements); every normalization is recorded in a changelog.

Complexity: O(words) over the deck; pure functions, no I/O.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sila.services.slides import reflow
from sila.services.slides.enums import (
    FIGURE_CONTAINERS,
    ChartKind,
    ContainerType,
    IconName,
    LayoutType,
    Palette,
)
from sila.services.slides.geometry import MAX_CONTAINERS

MAX_TITLE_WORDS = 8
MAX_HEADING_WORDS = 5
MAX_BODY_WORDS = 25
MAX_SLIDES = 30
_MAX_CHARS = 400  # hard cap per text field — nothing unbounded reaches OOXML

# Figure density — a table/chart is a glance, not a spreadsheet dump. Content
# beyond these caps is TRUNCATED (with a changelog note), never silently, and
# never rejected: the point is a readable slide, not the whole dataset.
MAX_TABLE_COLS = 6
MAX_TABLE_DATA_ROWS = 8  # excluding the header row
MAX_CHART_CATEGORIES = 12
MAX_CHART_SERIES = 3
MAX_CAPTION_CARDS = 3  # a data-showcase figure may carry this many caption cards

__all__ = [
    "MAX_TITLE_WORDS",
    "MAX_HEADING_WORDS",
    "MAX_BODY_WORDS",
    "MAX_CONTAINERS",
    "MAX_SLIDES",
    "LayoutType",
    "Palette",
    "ContainerType",
    "IconName",
    "ChartKind",
    "ChartSeries",
    "ChartSpec",
    "Container",
    "Slide",
    "Deck",
    "DeckValidationError",
    "validate_deck",
]


class ChartSeries(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = ""
    values: list[float] = Field(default_factory=list)


class ChartSpec(BaseModel):
    """A self-contained chart the model fills from its own analysis — code
    renders it as a native, editable PowerPoint chart. No dependency on
    analysis build-state, so the engine stays pure and fixture-testable."""

    model_config = ConfigDict(populate_by_name=True)

    kind: ChartKind = ChartKind.BAR
    categories: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)


class Container(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: ContainerType = ContainerType.CARD
    heading: str = ""
    body: str = ""
    accent_value: str = Field("", alias="accentValue")
    icon: IconName | None = None  # optional leading icon (card/metric layouts)
    rows: list[list[str]] = Field(default_factory=list)  # TABLE: first row = header
    chart: ChartSpec | None = None  # CHART: the chart spec
    image_prompt: str = Field("", alias="imagePrompt")  # IMAGE: what to generate
    # Runtime-only: the generated PNG bytes, injected by the tool AFTER
    # validation (never model-supplied). None → the renderer draws a placeholder.
    image_bytes: bytes | None = Field(default=None, exclude=True)

    @property
    def is_empty(self) -> bool:
        if self.type is ContainerType.TABLE:
            return not any(any(str(c).strip() for c in row) for row in self.rows)
        if self.type is ContainerType.CHART:
            return self.chart is None or not self.chart.categories
        if self.type is ContainerType.IMAGE:
            return not (self.image_prompt.strip() or self.heading.strip())
        return not (self.heading.strip() or self.body.strip() or self.accent_value.strip())

    @property
    def is_figure(self) -> bool:
        return self.type in FIGURE_CONTAINERS

    @property
    def is_image(self) -> bool:
        return self.type is ContainerType.IMAGE


class Slide(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slide_number: int = Field(0, alias="slideNumber")
    layout_type: LayoutType = Field(alias="layoutType")
    slide_title: str = Field("", alias="slideTitle")
    subtitle: str = ""
    containers: list[Container] = Field(default_factory=list)


class Deck(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str
    palette: Palette
    slides: list[Slide]


class DeckValidationError(ValueError):
    """The deck cannot be rendered as-is. `violations` names every problem so
    the caller can retry the model with concrete feedback."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


def _words(text: str) -> list[str]:
    return text.split()


def _cap_chars(text: str) -> str:
    return text.strip()[:_MAX_CHARS]


def _split_at_word_limit(text: str, limit: int) -> tuple[str, str]:
    """(head, tail) split at the limit, preferring a sentence/comma boundary
    inside the head so the cut reads naturally. Deterministic."""
    words = _words(text)
    if len(words) <= limit:
        return text.strip(), ""
    head_words = words[:limit]
    joined = " ".join(head_words)
    for boundary in (". ", "; ", ", "):
        cut = joined.rfind(boundary)
        if cut > len(joined) // 3:
            head = joined[: cut + 1].strip()
            tail = text.strip()[len(head) :].strip()
            return head, tail
    return joined, " ".join(words[limit:])


def _chunk_body(text: str, limit: int = MAX_BODY_WORDS) -> list[str]:
    """Body text as ≤`limit`-word chunks, boundary-aware. O(words)."""
    chunks: list[str] = []
    rest = text.strip()
    while rest:
        head, rest = _split_at_word_limit(rest, limit)
        chunks.append(head)
    return chunks or [""]


def _split_at_char_limit(text: str, max_chars: int) -> tuple[str, str]:
    """(head, tail) split at a character budget — the geometry backstop.
    Prefers a sentence/clause boundary, then a word boundary, inside the
    budget; only cuts mid-word if a single word alone exceeds it."""
    text = text.strip()
    if len(text) <= max_chars:
        return text, ""
    window = text[:max_chars]
    for boundary in (". ", "; ", ", "):
        cut = window.rfind(boundary)
        if cut > max_chars // 3:
            return text[: cut + 1].strip(), text[cut + 1 :].strip()
    cut = window.rfind(" ")
    if cut > 0:
        return text[:cut].strip(), text[cut:].strip()
    return text[:max_chars], text[max_chars:]  # one very long word — rare, hard cut


def _merge_small_chunks(chunks: list[str], max_chars: int) -> list[str]:
    """A word-boundary cut can strand a tiny trailing piece (e.g. one
    long word bumped past the budget by itself) — fold it into whichever
    neighbor still fits, so no container ends up holding a single word."""
    min_chars = max(1, max_chars // 5)
    merged = list(chunks)
    i = 0
    while i < len(merged):
        if len(merged[i]) >= min_chars or len(merged) == 1:
            i += 1
            continue
        if i + 1 < len(merged) and len(merged[i]) + 1 + len(merged[i + 1]) <= max_chars:
            merged[i + 1] = f"{merged[i]} {merged[i + 1]}"
            del merged[i]
        elif i > 0 and len(merged[i - 1]) + 1 + len(merged[i]) <= max_chars:
            merged[i - 1] = f"{merged[i - 1]} {merged[i]}"
            del merged[i]
            i -= 1
        else:
            i += 1  # nowhere for it to fit — leave it standing alone
    return merged


def _chunk_by_chars(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    rest = text.strip()
    while rest:
        head, rest = _split_at_char_limit(rest, max_chars)
        chunks.append(head)
    return _merge_small_chunks(chunks, max_chars) if chunks else [""]


def _apply_char_backstop(chunks: list[str], max_chars: int) -> list[str]:
    """The word-based split already ran (the editorial rule); re-split
    any chunk that still overflows its box physically — the rare
    long-word case word counts alone can't see. The small-fragment merge
    runs once over the FULL flattened result, not per original chunk, so
    a stray word from one overflowing chunk can still fold into its
    neighbor from the next chunk over."""
    raw: list[str] = []
    for chunk in chunks:
        if len(chunk) > max_chars:
            rest = chunk
            while rest:
                head, rest = _split_at_char_limit(rest, max_chars)
                raw.append(head)
        else:
            raw.append(chunk)
    return _merge_small_chunks(raw, max_chars)


def _normalize_figure(container: Container, n: int, changelog: list[str]) -> Container:
    """Cap a table/chart to its density budget — truncate loudly, never
    reject. Table rows are made rectangular; chart series are aligned to
    the category count so the renderer never indexes past an axis."""
    heading = _cap_chars(container.heading)
    if container.type is ContainerType.TABLE:
        rows = [
            [str(cell).strip() for cell in row]
            for row in container.rows
            if any(str(cell).strip() for cell in row)
        ]
        width = min(max((len(r) for r in rows), default=0), MAX_TABLE_COLS)
        if any(len(r) > MAX_TABLE_COLS for r in rows):
            changelog.append(f"slide {n}: table trimmed to {MAX_TABLE_COLS} columns")
        rows = [[(r + [""] * width)[:width][i][:_MAX_CHARS] for i in range(width)] for r in rows]
        if len(rows) > MAX_TABLE_DATA_ROWS + 1:  # +1 for the header row
            changelog.append(f"slide {n}: table trimmed to {MAX_TABLE_DATA_ROWS} data rows")
            rows = rows[: MAX_TABLE_DATA_ROWS + 1]
        return Container(type=ContainerType.TABLE, heading=heading, rows=rows)

    spec = container.chart or ChartSpec()
    cats = [str(c).strip() for c in spec.categories]
    if len(cats) > MAX_CHART_CATEGORIES:
        changelog.append(f"slide {n}: chart trimmed to {MAX_CHART_CATEGORIES} categories")
        cats = cats[:MAX_CHART_CATEGORIES]
    kept = spec.series[:MAX_CHART_SERIES]
    if len(spec.series) > MAX_CHART_SERIES:
        changelog.append(f"slide {n}: chart trimmed to {MAX_CHART_SERIES} series")
    aligned = [
        ChartSeries(
            name=_cap_chars(s.name),
            values=(list(s.values)[: len(cats)] + [0.0] * len(cats))[: len(cats)],
        )
        for s in kept
    ]
    return Container(
        type=ContainerType.CHART,
        heading=heading,
        chart=ChartSpec(kind=spec.kind, categories=cats, series=aligned),
    )


# A layout's structure determines how many items it can hold. These are the
# fixed-shape layouts (checked below; exempt from container pagination).
_COUNT_RULES: dict[LayoutType, tuple[int, int]] = {
    LayoutType.PROCESS_FLOW: (2, 5),
    LayoutType.TIMELINE: (2, 6),
    LayoutType.COMPARISON: (2, 2),
}


def _check_placement(layout: LayoutType, containers: list[Container], n: int) -> list[str]:
    """Tables/charts are figures: valid ONLY on the data-showcase layout,
    which must carry exactly one. Everywhere else they are rejected with a
    named violation so the model retries with a correct layout. The
    infographic layouts (process-flow/comparison/timeline) additionally
    enforce their item count."""
    figures = [c for c in containers if c.is_figure]
    images = [c for c in containers if c.is_image]

    if layout is LayoutType.IMAGE_FEATURE:
        if len(images) != 1:
            return [
                f"slide {n}: the 'image-feature' layout needs exactly one image "
                f"container (found {len(images)})"
            ]
        if figures:
            return [f"slide {n}: 'image-feature' can't hold a table or chart"]
        return []
    if images:
        return [
            f"slide {n}: an image is only valid on the 'image-feature' layout, not '{layout.value}'"
        ]

    if layout is not LayoutType.DATA_SHOWCASE:
        if figures:
            return [
                f"slide {n}: a {figures[0].type.value} is only valid on the "
                f"'data-showcase' layout, not '{layout.value}'"
            ]
        if layout in _COUNT_RULES:
            lo, hi = _COUNT_RULES[layout]
            if not lo <= len(containers) <= hi:
                span = f"exactly {lo}" if lo == hi else f"{lo}–{hi}"
                return [
                    f"slide {n}: the '{layout.value}' layout needs {span} items "
                    f"(found {len(containers)})"
                ]
        return []
    if len(figures) != 1:
        return [
            f"slide {n}: the 'data-showcase' layout needs exactly one table or chart "
            f"(found {len(figures)})"
        ]
    if any(f.type is ContainerType.CHART and not (f.chart and f.chart.series) for f in figures):
        return [f"slide {n}: the chart has no data series"]
    captions = [c for c in containers if not c.is_figure]
    if len(captions) > MAX_CAPTION_CARDS:
        return [
            f"slide {n}: a data-showcase figure may carry at most {MAX_CAPTION_CARDS} "
            f"caption cards (found {len(captions)})"
        ]
    return []


def _normalize_slide(slide: Slide, changelog: list[str], violations: list[str]) -> Slide:
    n = slide.slide_number
    layout = slide.layout_type
    title = _cap_chars(slide.slide_title)
    subtitle = _cap_chars(slide.subtitle)

    # Title spill → subtitle (never silently truncated). Word count is
    # the editorial rule; the char-capacity backstop below catches the
    # rare case where a few long words still overflow the title box.
    if len(_words(title)) > MAX_TITLE_WORDS:
        head, tail = _split_at_word_limit(title, MAX_TITLE_WORDS)
        subtitle = f"{tail} — {subtitle}".strip(" —") if subtitle else tail
        title = head
        changelog.append(f"slide {n}: title trimmed to {MAX_TITLE_WORDS} words; rest → subtitle")
    title_cap = reflow.title_capacity(layout)
    if len(title) > title_cap:
        head, tail = _split_at_char_limit(title, title_cap)
        subtitle = f"{tail} — {subtitle}".strip(" —") if subtitle else tail
        title = head
        changelog.append(f"slide {n}: title overflowed its box at this font size; rest → subtitle")

    containers: list[Container] = []
    heading_cap, body_cap = reflow.container_capacity(_REFLOW_AS.get(layout, layout))
    for container in slide.containers:
        if container.is_empty:
            changelog.append(f"slide {n}: dropped an empty container (no orphaned elements)")
            continue
        if container.is_figure:
            containers.append(_normalize_figure(container, n, changelog))
            continue
        if container.is_image:
            containers.append(
                Container(
                    type=ContainerType.IMAGE,
                    heading=_cap_chars(container.heading),
                    body=_cap_chars(container.body),
                    image_prompt=_cap_chars(container.image_prompt),
                )
            )
            continue
        heading = _cap_chars(container.heading)
        body = _cap_chars(container.body)
        if len(_words(heading)) > MAX_HEADING_WORDS:
            head, tail = _split_at_word_limit(heading, MAX_HEADING_WORDS)
            body = f"{tail}. {body}".strip(". ").strip() if body else tail
            heading = head
            changelog.append(
                f"slide {n}: heading trimmed to {MAX_HEADING_WORDS} words; rest → body"
            )
        if len(heading) > heading_cap:
            head, tail = _split_at_char_limit(heading, heading_cap)
            body = f"{tail}. {body}".strip(". ").strip() if body else tail
            heading = head
            changelog.append(
                f"slide {n}: heading overflowed its box at this font size; rest → body"
            )

        # Fixed-shape layouts (comparison, process-flow, timeline,
        # data-showcase captions) own their item count — a body must never
        # split into extra siblings there, or it would break that count.
        if layout in _FIXED_LAYOUTS:
            chunks = [body]
        else:
            chunks = _chunk_body(body) if body else [""]
            if body:
                chunks = _apply_char_backstop(chunks, body_cap)
        containers.append(
            Container(
                type=container.type,
                heading=heading,
                body=chunks[0],
                accent_value=_cap_chars(container.accent_value),
                icon=container.icon,  # the icon rides with the first (only) chunk
            )
        )
        # Body spill → sibling containers of the same type (reflow in place).
        for extra in chunks[1:]:
            containers.append(Container(type=container.type, heading="", body=extra))
        if len(chunks) > 1:
            changelog.append(
                f"slide {n}: body split into {len(chunks)} containers "
                f"(≤ {MAX_BODY_WORDS} words / {body_cap} chars each)"
            )

    if not title.strip() and not containers:
        violations.append(f"slide {n}: has no title and no content")
    violations.extend(_check_placement(layout, containers, n))

    return Slide(
        slide_number=n,
        layout_type=layout,
        slide_title=title,
        subtitle=subtitle,
        containers=containers,
    )


_GRID_LAYOUTS = (LayoutType.SPLIT_GRID, LayoutType.THREE_COLUMN_CARDS, LayoutType.METRIC_SHOWCASE)

# The reflow capacity of these layouts is borrowed from a structurally
# similar grid layout (they don't appear in reflow's own box tables).
_REFLOW_AS: dict[LayoutType, LayoutType] = {
    LayoutType.DATA_SHOWCASE: LayoutType.THREE_COLUMN_CARDS,
    LayoutType.PROCESS_FLOW: LayoutType.THREE_COLUMN_CARDS,
    LayoutType.COMPARISON: LayoutType.SPLIT_GRID,
    LayoutType.TIMELINE: LayoutType.THREE_COLUMN_CARDS,
    LayoutType.IMAGE_FEATURE: LayoutType.SPLIT_GRID,
}

# Fixed-shape layouts own their item count (validated in _check_placement),
# so they are never split by container pagination.
_FIXED_LAYOUTS = frozenset(
    {
        LayoutType.DATA_SHOWCASE,
        LayoutType.PROCESS_FLOW,
        LayoutType.COMPARISON,
        LayoutType.TIMELINE,
        LayoutType.IMAGE_FEATURE,
    }
)


def _check_cover(first: Slide) -> list[str]:
    """The cover is never paginated: a title-hero first slide with more blocks
    than a slide holds is a structuring mistake, named for the retry.

    Paginating it turned one cover into the cover plus five "(cont.)" card
    pages, each titled with the deck's name — nothing on them belonged on a
    cover. The retry moves those blocks onto content slides; nothing is lost.
    """
    if first.layout_type is not LayoutType.TITLE_HERO or len(first.containers) <= MAX_CONTAINERS:
        return []
    return [
        f"slide 1 is the cover: it holds at most {MAX_CONTAINERS} blocks (found "
        f"{len(first.containers)}) — keep the title and subtitle there and move the rest "
        "to content slides after it"
    ]


def _paginate(slide: Slide, changelog: list[str]) -> list[Slide]:
    """A slide whose reflowed containers exceed MAX_CONTAINERS is split
    across continuation slides instead of rejected (P0 deferred this to
    P2). Grid layouts (split-grid, 3-column-cards, metric-showcase)
    continue as themselves; single-message layouts (title-hero,
    quote-focus) downgrade continuation pages to 3-column-cards —
    repeating a hero title or a second quote glyph for overflow content
    doesn't read well, but a card grid holds arbitrary content cleanly."""
    if slide.layout_type in _FIXED_LAYOUTS or len(slide.containers) <= MAX_CONTAINERS:
        return [slide]

    continuation_layout = (
        slide.layout_type if slide.layout_type in _GRID_LAYOUTS else LayoutType.THREE_COLUMN_CARDS
    )
    chunks = [
        slide.containers[i : i + MAX_CONTAINERS]
        for i in range(0, len(slide.containers), MAX_CONTAINERS)
    ]
    pages = [
        Slide(
            slide_number=slide.slide_number,
            layout_type=slide.layout_type if i == 0 else continuation_layout,
            slide_title=slide.slide_title if i == 0 else f"{slide.slide_title} (cont.)",
            subtitle=slide.subtitle if i == 0 else "",
            containers=list(chunk),
        )
        for i, chunk in enumerate(chunks)
    ]
    downgrade_note = (
        f" (continuation pages use {continuation_layout.value})"
        if continuation_layout != slide.layout_type
        else ""
    )
    changelog.append(
        f"slide {slide.slide_number}: {len(slide.containers)} containers split across "
        f"{len(pages)} slides{downgrade_note}"
    )
    return pages


def validate_deck(raw: dict) -> tuple[Deck, list[str]]:
    """The gate. Returns (normalized deck, changelog) or raises
    DeckValidationError naming every violation — the renderer can NEVER
    receive an invalid deck, and nothing is silently lost."""
    violations: list[str] = []
    changelog: list[str] = []
    try:
        deck = Deck.model_validate(raw)
    except Exception as exc:  # pydantic error → named violations for the retry
        details = []
        for err in getattr(exc, "errors", lambda: [])():
            location = ".".join(str(x) for x in err.get("loc", ()))
            details.append(f"{location}: {err.get('msg', 'invalid')}")
        raise DeckValidationError(details or [str(exc)]) from exc

    if not deck.slides:
        raise DeckValidationError(["the deck has no slides"])
    if len(deck.slides) > MAX_SLIDES:
        raise DeckValidationError([f"{len(deck.slides)} slides exceed the {MAX_SLIDES}-slide cap"])

    normalized = [_normalize_slide(s, changelog, violations) for s in deck.slides]
    violations.extend(_check_cover(normalized[0]))
    if violations:
        raise DeckValidationError(violations)

    paged = [page for slide in normalized for page in _paginate(slide, changelog)]
    if len(paged) > MAX_SLIDES:
        raise DeckValidationError(
            [
                f"reflow split the content into {len(paged)} slides, exceeding the "
                f"{MAX_SLIDES}-slide cap — this deck's content is too large for one export"
            ]
        )

    # Numbering is code's job — the model's claims are ignored.
    for i, slide in enumerate(paged, start=1):
        slide.slide_number = i

    return Deck(
        title=_cap_chars(deck.title) or "Presentation", palette=deck.palette, slides=paged
    ), changelog
