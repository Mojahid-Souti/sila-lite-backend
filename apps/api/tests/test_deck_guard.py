"""The build-time regression checks.

Three rounds running, fixing one thing broke another, and review caught each
one. These assert the checks that catch them at build time instead.
"""

import pytest

from sila.services.slides.deck_guard import (
    DeckValidationError,
    Expectation,
    prune_orphans,
    referenced_parts,
    validate_package,
)

_CT = b'<?xml version="1.0"?><Types/>'
_SLIDE = (
    b'<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><p:cSld><p:spTree>'
    b'<p:sp><p:txBody><a:p><a:r><a:rPr sz="1800"/><a:t>A complete sentence.</a:t>'
    b"</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)


def _package(**extra: bytes) -> dict[str, bytes]:
    return {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": _SLIDE, **extra}


def test_relationship_targets_resolve_relative_to_their_part() -> None:
    parts = {
        "ppt/slides/_rels/slide1.xml.rels": b'<R><Relationship Target="../media/a.png"/></R>',
        "ppt/_rels/presentation.xml.rels": b'<R><Relationship Target="slides/slide1.xml"/></R>',
    }
    assert referenced_parts(parts) == {"ppt/media/a.png", "ppt/slides/slide1.xml"}


def test_an_external_target_is_not_a_package_part() -> None:
    parts = {"a/_rels/b.rels": b'<R><Relationship Target="https://example.org/x.png"/></R>'}
    assert referenced_parts(parts) == set()


def test_orphaned_media_is_pruned() -> None:
    """Rendering a handful of a template's slides strands the media belonging to
    the rest. PowerPoint calls that damaged content and offers to repair."""
    parts = _package(
        **{
            "ppt/media/kept.png": b"k",
            "ppt/media/orphan.png": b"o",
            "ppt/slides/_rels/slide1.xml.rels": (
                b'<R><Relationship Target="../media/kept.png"/></R>'
            ),
        }
    )
    pruned = prune_orphans(parts)
    assert "ppt/media/kept.png" in pruned
    assert "ppt/media/orphan.png" not in pruned


def test_an_unreferenced_part_fails_the_build() -> None:
    parts = _package(**{"ppt/media/orphan.png": b"o"})
    with pytest.raises(DeckValidationError, match="unreferenced part"):
        validate_package(parts, Expectation(slide_count=1, texts=()))


def test_dropped_content_fails_the_build() -> None:
    """Two slides once became title-only dividers and their items vanished. Any
    supplied string that is not in the emitted XML is a failure, not a warning."""
    with pytest.raises(DeckValidationError, match="content dropped"):
        validate_package(
            _package(), Expectation(slide_count=1, texts=("An item that never made it.",))
        )


def test_supplied_content_that_is_present_passes() -> None:
    validate_package(_package(), Expectation(slide_count=1, texts=("A complete sentence.",)))


def test_a_wrong_slide_count_fails_the_build() -> None:
    with pytest.raises(DeckValidationError, match="slide count"):
        validate_package(_package(), Expectation(slide_count=10, texts=()))


def test_an_empty_shape_fails_the_build() -> None:
    """An empty diamond reads as a bug — the sentence ran out before the shapes
    did — so an unused slot must be deleted, never left blank."""
    blank = _SLIDE.replace(b"A complete sentence.", b"   ")
    with pytest.raises(DeckValidationError, match="no content"):
        validate_package(
            {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": blank},
            Expectation(slide_count=1, texts=()),
        )


def test_content_below_the_legibility_floor_fails() -> None:
    tiny = _SLIDE.replace(b'sz="1800"', b'sz="600"')
    with pytest.raises(DeckValidationError, match="content text at"):
        validate_package(
            {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": tiny},
            Expectation(slide_count=1, texts=(), body_texts=("A complete sentence.",)),
        )


def test_the_templates_own_chrome_is_not_flagged() -> None:
    """The header bar and slide numbers sit at 9-11pt by the designer's choice.
    A guard that flags them is one nobody leaves switched on."""
    chrome = _SLIDE.replace(b'sz="1800"', b'sz="900"')
    validate_package(
        {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": chrome},
        Expectation(slide_count=1, texts=(), body_texts=("Something else entirely",)),
    )


def test_a_title_promising_a_decision_must_state_one() -> None:
    """Three versions shipped a slide headed "…and the ask" carrying only a
    roadmap table. The guard proves a supplied string survived; it cannot know a
    required piece of content was never authored — so it checks for it."""
    promise = _SLIDE.replace(b"A complete sentence.", b"Q2 roadmap and the ask")
    with pytest.raises(DeckValidationError, match="promises a decision"):
        validate_package(
            {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": promise},
            Expectation(slide_count=1, texts=()),
        )


def test_a_stated_decision_satisfies_the_check() -> None:
    delivered = _SLIDE.replace(
        b"<a:t>A complete sentence.</a:t>",
        b"<a:t>Q2 roadmap and the ask</a:t></a:r><a:r><a:t>We ask the steering "
        b"committee to approve the three workstreams by 30 April.</a:t>",
    )
    validate_package(
        {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": delivered},
        Expectation(slide_count=1, texts=()),
    )


def test_a_keyword_inside_a_table_cannot_satisfy_the_ask_check() -> None:
    """The false positive this check exists to prevent: a roadmap row reading
    "The three agents in review reach approve or reject" satisfied a bare
    keyword search, so a deck shipped with no ask on it three versions running."""
    with_table = (
        b'<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><p:cSld><p:spTree>'
        b'<p:sp><p:txBody><a:p><a:r><a:rPr sz="3200"/>'
        b"<a:t>Q2 roadmap and the ask</a:t></a:r></a:p></p:txBody></p:sp>"
        b"<a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>The three agents in review "
        b"reach approve or reject by 30 April, per the committee.</a:t></a:r>"
        b"</a:p></a:txBody></a:tc></a:tr></a:tbl>"
        b"</p:spTree></p:cSld></p:sld>"
    )
    with pytest.raises(DeckValidationError, match="promises a decision"):
        validate_package(
            {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": with_table},
            Expectation(slide_count=1, texts=()),
        )


def test_an_ask_needs_all_three_parts() -> None:
    """One keyword is not an ask: the decision, the approver and the date."""
    for prose in (
        b"We ask the committee to approve the workstreams.",  # no date
        b"Approve the workstreams by 30 April.",  # no approver
        b"The committee meets by 30 April.",  # no request
    ):
        slide = _SLIDE.replace(
            b"<a:t>A complete sentence.</a:t>",
            b"<a:t>The ask</a:t></a:r><a:r><a:t>" + prose + b"</a:t>",
        )
        with pytest.raises(DeckValidationError, match="promises a decision"):
            validate_package(
                {"[Content_Types].xml": _CT, "ppt/slides/slide1.xml": slide},
                Expectation(slide_count=1, texts=()),
            )


def test_a_silently_dropped_slide_fails_the_build() -> None:
    """A slide vanishing between versions is the same class of failure as the
    v6 content loss. The count check is what stands between the two."""
    with pytest.raises(DeckValidationError, match="slide count 1, expected 2"):
        validate_package(_package(), Expectation(slide_count=2, texts=()))


def test_the_count_check_cannot_see_an_author_omitting_a_slide() -> None:
    """Worth stating plainly, because it is the limit of this check.

    `slide_count` is what the CALLER asked for. If a slide is never requested —
    an author omits it when composing the deck — the emitted file matches the
    request exactly and the guard is right to pass it. Catching that needs a
    check against the brief, not against the package.
    """
    validate_package(_package(), Expectation(slide_count=1, texts=()))
