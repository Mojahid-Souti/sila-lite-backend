"""Package hygiene and regression checks for a generated .pptx.

Three rounds running, fixing one thing broke another: fragmentation was fixed
and an off-by-one appeared; alignment was fixed and the figures went missing;
the figures arrived and two slides lost their content while the package went
corrupt. Review caught each one. These checks catch them at build time instead,
so a fix stays fixed.

Every check runs on the EMITTED package — the bytes a reader opens — rather than
on the intermediate model, because that is where the defects actually showed up.

Complexity: O(total parts + total text) — one pass over the package.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

_MEDIA_PREFIX = "ppt/media/"
_TEXT_TAG = re.compile(r"<a:t[^>]*>([^<]*)</a:t>")
_TARGET = re.compile(r'Target="([^"]+)"')
_SIZE = re.compile(r'sz="(\d+)"')

# Below this, text is present but cannot be read from the back of a room. The
# stencils' own placeholder sizes sit under it, so anything we fill must not.
MIN_BODY_PT = 1200
MIN_TABLE_PT = 1400

# A title that promises a decision has to deliver one. The guard can prove a
# supplied string survived into the file; it cannot know a required piece of
# content was never authored, which is how three versions shipped a slide headed
# "…and the ask" carrying only a roadmap table.
_PROMISES_A_DECISION = re.compile(
    r"\b(the ask|decision|recommendation|approval|next steps)\b", re.I
)
# A bare keyword is not an ask. "The three agents in review reach approve or
# reject" satisfied the first version of this check from inside a roadmap table,
# which is exactly the false positive it existed to prevent. An ask has three
# parts and they are all required.
_ASK_PHRASE = re.compile(
    r"\b(we ask|we are asking|approval is requested|we request|we recommend|"
    r"asks? the \w+ to (approve|confirm|endorse|nominate)|"
    r"(approve|endorse|sign off on|confirm|nominate)\b[^.]{0,80}\bby\b)",
    re.I,
)
_APPROVER = re.compile(
    r"\b(committee|board|steering|sponsor|owner|exco|management|"
    r"you|each member|the \w+ team|department)\b",
    re.I,
)
_WHEN = re.compile(
    r"\b(by \d{1,2} \w+|by \w+ \d{1,2}|by (the )?end of \w+|by Q[1-4]|"
    r"this meeting|today|within \w+ (week|month)s?|before \w+)\b",
    re.I,
)
_TABLE_BLOCK = re.compile(r"<a:tbl>.*?</a:tbl>", re.S)
# White text inherited from a layout where it sat on a coloured field. Invisible
# is worse than wrong, because nothing on the slide looks broken.
_WHITE_ON_WHITE = re.compile(r'srgbClr val="FFFFFF"', re.I)
_SHAPE_BLOCK = re.compile(r"<p:sp>(.*?)</p:sp>", re.S)
# PowerPoint's identity fields, matched with any namespace prefix.
# Captures ANY value, not just a numeric one: a pattern of `val="(\d+)"` would
# skip a malformed id and report the sharing check clean — the same narrowness
# that let the rIdOkrChart ids through four rounds of review.
_P14_CREATION_ID = re.compile(r'<(?:[A-Za-z0-9_.-]+:)?creationId\b[^>]*?\bval="([^"]*)"')
_A16_CREATION_ID = re.compile(r'<(?:[A-Za-z0-9_.-]+:)?creationId\b[^>]*?\bid="(\{[^"]*\})"')
# The shape Office writes, and the only shape PowerPoint accepts without
# offering to repair the file.
_REL_ID = re.compile(r"rId\d+")
# Captures ANY value, so a malformed reference is CHECKED rather than skipped.
_REL_REF = re.compile(r'r:(?:id|embed|link|pict|dm|lo|qs|cs)="([^"]*)"')
# Relationship kinds whose target belongs to exactly one owning part.
_SINGLE_OWNER_RELS = frozenset(
    {"notesSlide", "chartStyle", "chartColorStyle", "package", "chartUserShapes"}
)
# Each Relationship element; Type and Target are then read from it
# INDEPENDENTLY, because their attribute order is not guaranteed and a pattern
# that assumes one silently matches nothing when a writer picks the other.
_REL_ELEMENT = re.compile(r"<Relationship\b[^>]*/>")
_REL_ATTR = re.compile(r'\b(Type|Target)="([^"]*)"')


def _has_white_content(xml: str, wanted: set[str]) -> bool:
    """Does a shape holding OUR text paint it white?

    Scoped to our own content: a full-bleed blue stencil sets white type on
    purpose, and only the slots we fill sit on a white ground.
    """
    for match in _SHAPE_BLOCK.finditer(xml):
        shape = match.group(1)
        runs = [_normalise(r) for r in _TEXT_TAG.findall(shape) if r.strip()]
        if any(run in wanted for run in runs) and _WHITE_ON_WHITE.search(shape):
            return True
    return False


class DeckValidationError(RuntimeError):
    """The package would reach the reader damaged or missing content."""


@dataclass(frozen=True)
class Expectation:
    """What the caller asked for, so the emitted file can be checked against it."""

    slide_count: int
    texts: tuple[str, ...]
    # Card text, checked for legibility. Kept apart from `texts` because titles
    # and the header bar are set at the designer's sizes, and a guard that
    # flags the template's own chrome is one nobody leaves switched on.
    body_texts: tuple[str, ...] = ()


def _normalise(value: str) -> str:
    return " ".join(html.unescape(value).split()).lower()


def _resolve(source_part: str, target: str) -> str:
    """A relationship target is relative to the part's own folder."""
    if target.startswith("/"):
        return target.lstrip("/")
    base = source_part.rsplit("/", 2)[0] if "/_rels/" in source_part else ""
    parts = [p for p in f"{base}/{target}".split("/") if p and p != "."]
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved:
                resolved.pop()
        else:
            resolved.append(part)
    return "/".join(resolved)


def referenced_parts(parts: dict[str, bytes]) -> set[str]:
    """Every part any .rels file points at, resolved to a package path."""
    found: set[str] = set()
    for name, blob in parts.items():
        if not name.endswith(".rels"):
            continue
        for target in _TARGET.findall(blob.decode("utf-8", "ignore")):
            if target.startswith(("http://", "https://")):
                continue
            found.add(_resolve(name, target))
    return found


def prune_orphans(parts: dict[str, bytes]) -> dict[str, bytes]:
    """Drop media no surviving relationship points at.

    PowerPoint treats an orphaned part as damaged content and shows "PowerPoint
    found a problem… Repaired and removed it" — which costs the reader's
    confidence before slide 1. Rendering only a handful of a template's slides
    strands the media belonging to the rest, so this is the normal case, not an
    edge case.
    """
    referenced = referenced_parts(parts)
    kept = {
        name: blob
        for name, blob in parts.items()
        if not name.startswith(_MEDIA_PREFIX) or name in referenced
    }
    dropped = len(parts) - len(kept)
    if dropped:
        log.info("deck_guard.pruned_orphans", count=dropped)
    return kept


def validate_package(parts: dict[str, bytes], expected: Expectation) -> None:
    """Fail the build rather than hand over a damaged or incomplete deck."""
    problems: list[str] = []
    slides = sorted(
        (n for n in parts if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.findall(r"\d+", n)[-1]),
    )

    # 1. Relationship integrity, both directions.
    referenced = referenced_parts(parts)
    problems += [
        f"unreferenced part: {name}"
        for name in sorted(parts)
        if name.startswith(_MEDIA_PREFIX) and name not in referenced
    ]
    problems += [
        f"broken reference: {target}"
        for target in sorted(referenced)
        if target not in parts and not target.startswith("ppt/slideLayouts/slideLayout")
    ]

    # 2. Nothing the caller supplied may be silently dropped.
    body = " ".join(
        _normalise(" ".join(_TEXT_TAG.findall(parts[name].decode("utf-8", "ignore"))))
        for name in list(slides) + [n for n in parts if "notesSlide" in n and n.endswith(".xml")]
    )
    problems += [
        f"content dropped: {text!r}"
        for text in expected.texts
        if text.strip() and _normalise(text) not in body
    ]

    # 3. Slide count follows the request.
    if expected.slide_count and len(slides) != expected.slide_count:
        problems.append(f"slide count {len(slides)}, expected {expected.slide_count}")

    # 4. No shape ships empty, and nothing WE FILLED ships too small to read.
    wanted = {_normalise(t) for t in expected.body_texts if t.strip()}
    for name in slides:
        xml = parts[name].decode("utf-8", "ignore")
        number = re.findall(r"\d+", name)[-1]
        if _has_empty_text_shape(xml):
            problems.append(f"slide {number}: a text shape ships with no content")
        smallest = _smallest_content_size(xml, wanted)
        floor = MIN_TABLE_PT if "<a:tbl>" in xml else MIN_BODY_PT
        if smallest is not None and smallest < floor:
            problems.append(f"slide {number}: content text at {smallest / 100:.0f}pt")

    # 5. A promised decision is actually made, in a block of its own.
    for name in slides:
        xml = parts[name].decode("utf-8", "ignore")
        runs = [_normalise(r) for r in _TEXT_TAG.findall(xml) if r.strip()]
        if not runs or not _PROMISES_A_DECISION.search(runs[0]):
            continue
        number = re.findall(r"\d+", name)[-1]
        # Table cells are data, not an ask — a keyword inside one must not count.
        prose = _normalise(" ".join(_TEXT_TAG.findall(_TABLE_BLOCK.sub(" ", xml))))
        missing = [
            part
            for part, pattern in (
                ("the request", _ASK_PHRASE),
                ("the approver", _APPROVER),
                ("the date", _WHEN),
            )
            if not pattern.search(prose)
        ]
        if missing:
            problems.append(
                f"slide {number}: the title promises a decision but the slide is "
                f"missing {', '.join(missing)}"
            )

    # 6. Nothing we filled is invisible.
    for name in slides:
        xml = parts[name].decode("utf-8", "ignore")
        if _WHITE_ON_WHITE.search(xml) and _has_white_content(xml, wanted):
            number = re.findall(r"\d+", name)[-1]
            problems.append(f"slide {number}: content set in white on a white slide")

    # 7. Every slide, and every shape, has an identity of its own.
    #
    # PowerPoint identifies a slide by `p14:creationId` and a shape by
    # `a16:creationId`, and a slide cloned verbatim brings its source's values
    # with it — six objective slides all claiming one identity are six slides
    # PowerPoint cannot tell apart, and it renders them blank.
    #
    # These live in Microsoft EXTENSION namespaces, so the XSD does not
    # validate them, LibreOffice ignores them, and the PDF renders perfectly
    # regardless. A clean schema check plus a correct PDF is not evidence that
    # a .pptx opens — the same lesson as the notes-slide cardinality bug.
    by_slide: dict[str, list[str]] = {}
    guids: dict[str, int] = {}
    for name in slides:
        xml = parts[name].decode("utf-8", "ignore")
        number = re.findall(r"\d+", name)[-1]
        for value in _P14_CREATION_ID.findall(xml):
            by_slide.setdefault(value, []).append(number)
    # Shape GUIDs are checked ACROSS SLIDES, not across the whole package. The
    # OKR team's own template — which PowerPoint opens cleanly — reuses GUIDs
    # between slide layouts, and inside its master where one shape is declared
    # in both branches of an mc:AlternateContent. The builders never clone
    # layouts or masters; they clone slides, and slides sharing identity is the
    # defect this check exists for (BUG-178).
    for name in slides:
        for guid in _A16_CREATION_ID.findall(parts[name].decode("utf-8", "ignore")):
            guids[guid] = guids.get(guid, 0) + 1
    problems += [
        f"p14:creationId {value} is shared by slides {', '.join(sorted(where, key=int))} "
        "— a cloned slide must be given its own"
        for value, where in sorted(by_slide.items())
        if len(where) > 1
    ]
    repeated = sorted(g for g, count in guids.items() if count > 1)
    if repeated:
        shown = ", ".join(repeated[:3])
        more = f" (+{len(repeated) - 3} more)" if len(repeated) > 3 else ""
        problems.append(
            f"a16:creationId shape id(s) reused across slides: {shown}{more} "
            "— a cloned shape must be given its own"
        )

    # 9. Relationship ids conform, and every reference resolves.
    #
    # Office always writes `rId` followed by an integer. PowerPoint offers to
    # repair a file whose ids are anything else, and it renders the slides
    # holding them blank — which is what `rIdOkrChart1` did.
    #
    # The reference check is deliberately written to capture ANY value and then
    # validate it. A pattern like `r:id="(rId\d+)"` matches only well-formed
    # ids, so a malformed one is skipped and the check reports clean: that
    # narrowness is exactly how this defect survived four rounds of review.
    for name, blob in sorted(parts.items()):
        if not name.endswith(".rels"):
            continue
        rels_xml = blob.decode("utf-8", "ignore")
        malformed = sorted(
            {i for i in re.findall(r'Id="([^"]+)"', rels_xml) if not _REL_ID.fullmatch(i)}
        )
        if malformed:
            problems.append(
                f"{name}: relationship id(s) {', '.join(malformed)} are not rId<n> — "
                "PowerPoint offers to repair such a file and renders the slide blank"
            )
    for name, blob in sorted(parts.items()):
        if not name.endswith(".xml") or "/_rels/" in name:
            continue
        folder, _, base = name.rpartition("/")
        rels_name = f"{folder}/_rels/{base}.rels"
        declared = set(
            re.findall(r'Id="([^"]+)"', parts.get(rels_name, b"").decode("utf-8", "ignore"))
        )
        referenced = set(_REL_REF.findall(blob.decode("utf-8", "ignore")))
        dangling = sorted(referenced - declared)
        if dangling:
            problems.append(
                f"{name}: reference(s) {', '.join(dangling)} are not declared in {rels_name}"
            )

    # 10. No part is declared twice in [Content_Types].xml.
    #
    # A part carries exactly one content-type declaration (ECMA-376
    # §10.1.2.2.2). A duplicate is a SEMANTIC violation, not a well-formedness
    # one, so an XML parse accepts it and LibreOffice just takes the first —
    # which is why nothing downstream saw five parts declared twice.
    #
    # Distinct from the ghost-Override check above: that one catches a
    # declaration with no part, this one a part with two declarations.
    content_types = parts.get("[Content_Types].xml", b"").decode("utf-8", "ignore")
    declared_twice = sorted(
        name
        for name, count in Counter(
            re.findall(r'<Override PartName="([^"]+)"', content_types)
        ).items()
        if count > 1
    )
    if declared_twice:
        problems.append(
            "[Content_Types].xml declares part(s) more than once: "
            f"{', '.join(declared_twice)} — a part may have exactly one "
            "content-type declaration"
        )

    # 11. A part that belongs to one owner has exactly one owner.
    #
    # Some OOXML parts are OWNED, not shared: a notes slide belongs to its
    # slide, and a chart owns its chartStyle, chartColorStyle and embedded
    # workbook. PowerPoint enforces that. Share one and it rejects every part
    # in the sharing set — twelve charts pointing at a single colors1.xml is
    # why Repair deleted all twelve, proven by bisection in real PowerPoint.
    #
    # This is deliberately ONE rule rather than a check per relationship type.
    # The same defect shape — one part, many owners — got past every check
    # twice: first as six slides sharing a notes part, then as twelve charts
    # sharing a style part. Both were schema-valid, every reference resolved,
    # every content type was declared, and LibreOffice rendered a clean PDF
    # from each. Only the cardinality was wrong.
    owners: dict[str, set[str]] = {}
    for name, blob in parts.items():
        if not name.endswith(".rels"):
            continue
        source = name.replace("/_rels/", "/").removesuffix(".rels")
        for element in _REL_ELEMENT.findall(blob.decode("utf-8", "ignore")):
            attrs = dict(_REL_ATTR.findall(element))
            rel_type, target = attrs.get("Type", ""), attrs.get("Target", "")
            if rel_type.rsplit("/", 1)[-1] not in _SINGLE_OWNER_RELS or not target:
                continue
            if target.startswith(("http://", "https://")):
                continue
            owners.setdefault(_resolve(name, target), set()).add(source)
    for target, sources in sorted(owners.items()):
        if len(sources) > 1:
            problems.append(
                f"{target} is owned by {len(sources)} parts "
                f"({', '.join(sorted(sources))}) — it may belong to exactly one"
            )

    if problems:
        raise DeckValidationError("; ".join(problems[:12]))


def _smallest_content_size(xml: str, wanted: set[str]) -> int | None:
    """The smallest point size on a shape holding text the caller supplied.

    Scoped to OUR content on purpose: the template's header bar and slide
    numbers sit at 9-11pt by design, and a guard that flags the designer's own
    chrome is a guard nobody can leave switched on.
    """
    sizes: list[int] = []
    for match in re.finditer(r"<(p:sp|a:tbl)>(.*?)</\1>", xml, re.S):
        kind, shape = match.group(1), match.group(2)
        runs = [_normalise(r) for r in _TEXT_TAG.findall(shape) if r.strip()]
        if not runs:
            continue
        # A shape counts only when a run is EXACTLY a string we supplied, so a
        # short title cannot match the header bar by coincidence. A table is
        # measured CELL BY CELL under the same rule: treating every cell as ours
        # was true for a data table but wrong for a template whose table is the
        # layout frame — the OKR deck's rotated rail labels are the designer's
        # 9pt chrome, and flagging those is flagging the template itself.
        if kind == "a:tbl":
            for cell in re.finditer(r"<a:tc[ >].*?</a:tc>", shape, re.S):
                text = cell.group(0)
                cell_runs = [_normalise(r) for r in _TEXT_TAG.findall(text) if r.strip()]
                if any(run in wanted for run in cell_runs):
                    sizes.extend(int(s) for s in _SIZE.findall(text))
            continue
        if not any(run in wanted for run in runs):
            continue
        sizes.extend(int(s) for s in _SIZE.findall(shape))
    return min(sizes) if sizes else None


def _has_empty_text_shape(xml: str) -> bool:
    """A shape holding a text body with nothing in it.

    An empty diamond reads as a bug — the sentence ran out before the shapes
    did — so an unused slot must be deleted, never left blank.
    """
    for match in re.finditer(r"<p:sp>(.*?)</p:sp>", xml, re.S):
        shape = match.group(1)
        if "<p:txBody>" not in shape:
            continue
        # A placeholder the stencil draws with no run at all is decoration; a
        # shape with runs that are all blank is a slot we failed to fill.
        runs = _TEXT_TAG.findall(shape)
        if runs and not any(run.strip() for run in runs):
            return True
    return False
