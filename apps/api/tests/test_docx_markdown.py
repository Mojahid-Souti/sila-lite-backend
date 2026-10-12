"""Markdown → WordprocessingML, and the branded document package.

The bug these cover: `build_docx_sections` emitted one raw paragraph per source
line, so a GFM table reached Word as literal pipe characters and a ```chart
fence as ASCII bars. No network, no DB — the builder is pure.
"""

import io
import zipfile

from sila.services.connectors.docx_markdown import (
    brand_header,
    brand_media,
    markdown_to_ooxml,
)
from sila.services.connectors.office_files import build_docx_sections

TABLE = """| State | What it means |
|---|---|
| Draft | Real workflow, inactive |
| Vetted | Cleared for publication |
"""


def _document_xml(doc: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(doc)).read("word/document.xml").decode()


def test_pipe_table_becomes_a_real_table() -> None:
    xml = "".join(markdown_to_ooxml(TABLE))
    assert xml.count("<w:tbl>") == 1
    assert xml.count("<w:tr>") == 3  # header + two body rows
    assert "| State |" not in xml, "pipes survived into the document"
    assert "Real workflow, inactive" in xml


def test_table_without_a_separator_row_stays_prose() -> None:
    """A lone pipe line is prose, not a one-column table."""
    xml = "".join(markdown_to_ooxml("| not actually | a table |"))
    assert "<w:tbl>" not in xml


def test_ragged_rows_are_padded_to_the_widest() -> None:
    ragged = "| a | b | c |\n|---|---|---|\n| only-one |\n"
    xml = "".join(markdown_to_ooxml(ragged))
    assert xml.count("<w:tc>") == 6  # 2 rows × 3 columns


def test_inline_spans_become_formatting() -> None:
    xml = "".join(markdown_to_ooxml("A **bold** and *italic* and `code` span."))
    assert "<w:b/>" in xml and "<w:i/>" in xml and "Consolas" in xml
    assert "**" not in xml and "`" not in xml


def test_link_keeps_its_label_and_url() -> None:
    xml = "".join(markdown_to_ooxml("See [the Bazaar](https://silai.om/bazaar)."))
    assert "the Bazaar" in xml and "https://silai.om/bazaar" in xml
    assert "](" not in xml


def test_chart_fence_becomes_a_table_not_ascii_bars() -> None:
    fence = '```chart\n{"type":"bar","title":"By state","data":[{"label":"Draft","value":8}]}\n```'
    xml = "".join(markdown_to_ooxml(fence))
    assert "<w:tbl>" in xml
    assert "By state" in xml and "Draft" in xml and "8" in xml


def test_malformed_chart_fence_does_not_crash() -> None:
    """Model output is untrusted; a broken fence degrades to a paragraph."""
    xml = "".join(markdown_to_ooxml("```chart\n{not json at all\n```"))
    assert "<w:p>" in xml


def test_headings_and_lists_are_distinguishable_from_body() -> None:
    xml = "".join(markdown_to_ooxml("## A heading\n\n- a bullet\n\n> a quote\n"))
    assert "A heading" in xml and "<w:sz " in xml
    assert "•" in xml
    assert "<w:i/>" in xml  # the blockquote


def test_text_is_escaped() -> None:
    xml = "".join(markdown_to_ooxml("5 < 6 & <script>alert(1)</script>"))
    assert "<script>" not in xml
    assert "&lt;script&gt;" in xml


def test_document_carries_the_brand_mark_as_a_real_image() -> None:
    doc = build_docx_sections("Platform Brief", [("Lifecycle", TABLE)])
    names = zipfile.ZipFile(io.BytesIO(doc)).namelist()
    assert any(n.startswith("word/media/") and n.endswith(".png") for n in names)
    assert "word/_rels/document.xml.rels" in names
    xml = _document_xml(doc)
    assert "<w:drawing>" in xml
    # Every relationship the body references must exist in the rels part.
    rels = zipfile.ZipFile(io.BytesIO(doc)).read("word/_rels/document.xml.rels").decode()
    for rel_id in brand_media():
        assert rel_id in xml and rel_id in rels


def test_brand_header_falls_back_to_a_wordmark_without_the_asset(monkeypatch) -> None:
    """A missing asset must cost the logo, never the document."""
    monkeypatch.setattr(
        "sila.services.connectors.docx_markdown.brand_media", lambda: {}, raising=True
    )
    xml = "".join(brand_header("Platform Brief"))
    assert "<w:drawing>" not in xml
    assert "SILA" in xml and "Platform Brief" in xml


def test_full_document_has_no_literal_pipes() -> None:
    """The end-to-end regression: what the user actually saw in Word."""
    doc = build_docx_sections("Platform Brief", [("Lifecycle", TABLE)])
    xml = _document_xml(doc)
    assert "| State |" not in xml
    assert xml.count("<w:tbl>") == 1
