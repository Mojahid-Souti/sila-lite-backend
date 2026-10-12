"""Preview routing for generated files.

The classifier decides what the side pane renders, and `/view` is the one route
in the app that sets `Content-Disposition: inline` — so both get pinned here.
No network, no DB: `_preview_mode` is pure.
"""

import pytest

from sila.api.v1.files import _CONVERTIBLE, _PDF, _preview_mode

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("text/markdown", "brief.md", "markdown"),
        ("text/plain", "notes.markdown", "markdown"),
        ("text/csv", "rows.csv", "sheet"),
        ("text/tab-separated-values", "rows.tsv", "sheet"),
        ("text/plain", "run.log", "text"),
        ("application/json", "spec.json", "text"),
        ("text/plain", "anything.txt", "text"),
        (_PDF, "brief.pdf", "pdf"),
        (DOCX, "brief.docx", "pdf"),
        (PPTX, "deck.pptx", "pdf"),
        ("application/zip", "bundle.zip", "none"),
        ("image/png", "shot.png", "none"),
        ("application/octet-stream", "blob.bin", "none"),
    ],
)
def test_preview_mode(content_type: str, filename: str, expected: str) -> None:
    assert _preview_mode(content_type, filename) == expected


def test_sheet_wins_over_a_generic_text_type() -> None:
    """A CSV served as text/plain must still get the grid, not a wall of commas."""
    assert _preview_mode("text/plain", "export.csv") == "sheet"


def test_every_convertible_type_routes_to_the_pdf_viewer() -> None:
    # If a format is added to _CONVERTIBLE, the pane has to know to embed it —
    # otherwise /view would happily convert a file the UI never asks to see.
    for content_type in _CONVERTIBLE:
        assert _preview_mode(content_type, "file.bin") == "pdf"


def test_html_and_svg_never_get_an_inline_preview() -> None:
    """The reason /view is PDF-only.

    Anything a browser renders inline runs on the API's origin, so a generated
    .html or .svg must fall through to 'none' (download it, do not render it).
    """
    assert _preview_mode("text/html", "page.html") != "pdf"
    assert _preview_mode("image/svg+xml", "mark.svg") == "none"
