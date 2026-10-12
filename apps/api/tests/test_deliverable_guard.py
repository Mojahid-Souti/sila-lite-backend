"""The security gateway on deliverables.

Pins the contract the UI and the model both rely on: a high-severity hit
blocks the card, the finding names WHERE (a slide number, a section, a slot),
only the redacted preview ever leaves, and a clean artifact says nothing.
"""

import pytest

from sila.adapters.scanner.pii import PIIScanner
from sila.services.sila.deliverable_guard import (
    Chunk,
    agent_chunks,
    deck_chunks,
    describe_for_model,
    guard_chunks,
    guard_text,
    section_chunks,
)

_ID = "12345678"
_MOBILE = "96891234567"


async def test_national_id_blocks_and_names_the_slide() -> None:
    sec = await guard_chunks(
        [
            Chunk("Slide 1", "Quarterly review"),
            Chunk("Slide 3", f"Escalate the customer with national ID {_ID} to retention."),
        ]
    )
    assert sec["status"] == "blocked"
    (f,) = [x for x in sec["findings"] if x["type"] == "pii.oman_national_id"]
    assert f["location"] == "Slide 3"
    assert f["severity"] == "critical"
    assert f["label"] == "Omani national ID"


async def test_only_the_redacted_preview_leaves() -> None:
    """The raw match is the user's data. It must not be in anything the UI or
    the model receives — not the finding, not the sentence for the model."""
    sec = await guard_text(f"Call them on {_MOBILE}", location="Section 'Contact'")
    text = repr(sec) + describe_for_model(sec, artifact="Brief")
    assert _MOBILE not in text
    assert any("*" in f["preview"] for f in sec["findings"])


async def test_clean_text_is_clear_and_silent() -> None:
    sec = await guard_text("Revenue grew 6.9% to RO 514.5 million.", location="Slide 2")
    assert sec == {"status": "clear", "findings": []}
    assert describe_for_model(sec, artifact="Deck") == ""


async def test_model_text_groups_by_location_and_forbids_repeating() -> None:
    sec = await guard_chunks([Chunk("Slide 4", f"ID {_ID}"), Chunk("Slide 4", f"mobile {_MOBILE}")])
    note = describe_for_model(sec, artifact="Ops pack")
    assert note.startswith("SECURITY GATEWAY BLOCKED 'Ops pack'")
    assert note.count("Slide 4:") == 1  # said once, both findings under it
    assert "Do NOT reproduce the raw values" in note
    assert "regenerate" in note


async def test_worst_finding_leads() -> None:
    """The tooltip reads top-down; the thing that blocked the card goes first."""
    sec = await guard_chunks([Chunk("A", f"mobile {_MOBILE}"), Chunk("B", f"ID {_ID}")])
    assert sec["findings"][0]["severity"] == "critical"


def test_deck_chunks_number_slides_as_the_user_sees_them() -> None:
    from sila.services.slides import schema as slide_schema

    deck, _ = slide_schema.validate_deck(
        {
            "title": "T",
            "palette": "modern-slate",
            "slides": [
                {"layoutType": "title-hero", "slideTitle": "Cover"},
                {
                    "layoutType": "3-column-cards",
                    "slideTitle": "Cases",
                    "containers": [{"type": "card", "heading": "One", "body": "first body"}],
                },
            ],
        }
    )
    chunks = deck_chunks(deck)
    assert [c.location for c in chunks] == ["Slide 1", "Slide 2"]
    assert "first body" in chunks[1].text and "One" in chunks[1].text


def test_section_chunks_use_the_heading_when_there_is_one() -> None:
    chunks = section_chunks("Title", [("Background", "b"), ("", "no heading")])
    assert [c.location for c in chunks] == ["Title", "Section 'Background'", "Section 2"]


def test_agent_chunks_cover_every_interview_answer() -> None:
    chunks = agent_chunks("answer HR questions", {"content": "x", "email_to": "y", "n": 3})
    assert [c.location for c in chunks] == ["Purpose", "Answer 'content'", "Answer 'email_to'"]


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
async def test_blank_chunks_are_skipped(blank: str) -> None:
    sec = await guard_chunks([Chunk("Slide 1", blank)])
    assert sec["status"] == "clear"


@pytest.mark.asyncio
async def test_a_scan_that_fails_blocks_rather_than_passes(monkeypatch) -> None:
    """A scan that cannot complete is not a pass.

    Before this, the exception escaped the tool call and took the finished
    artifact with it: the user saw a generic failure and no card at all, for a
    file that was already built and stored. Now the deliverable still reaches
    them — blocked, and saying why.
    """

    async def boom(self, agent_input):  # noqa: ANN001, ANN201
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(PIIScanner, "scan", boom)
    result = await guard_chunks([Chunk(location="Slide 1", text="anything at all")])
    assert result["status"] == "blocked"
    assert [f["type"] for f in result["findings"]] == ["scan.failed"]
    assert result["findings"][0]["severity"] == "critical"


@pytest.mark.asyncio
async def test_a_scan_failure_is_not_reported_as_personal_data(monkeypatch) -> None:
    """The wording matters: the file is held as a precaution, not accused."""

    async def boom(self, agent_input):  # noqa: ANN001, ANN201
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(PIIScanner, "scan", boom)
    result = await guard_chunks([Chunk(location="Slide 1", text="anything at all")])
    note = describe_for_model(result, artifact="Deck")
    assert "COULD NOT SCAN" in note
    assert "contains personal data" not in note
