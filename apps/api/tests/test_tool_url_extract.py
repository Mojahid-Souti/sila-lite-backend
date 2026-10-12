"""Unit tests for image-URL extraction from MCP tool output.

Gradio-backed MCP tools (e.g. Z-Image) return a file URL in the text result
rather than a base64 image block; the playground renders these as <img>.
"""

from __future__ import annotations

from sila.services.tool_service import _extract_image_urls


def test_extracts_gradio_file_url():
    out = (
        "[{'path': '/tmp/gradio/abc/image.webp', "
        "'url': 'https://mcp-tools-z-image-turbo.hf.space/--replicas/hvrlk/"
        "gradio_api/file=/tmp/gradio/abc/image.webp'}]"
    )
    urls = _extract_image_urls(out)
    assert urls == [
        "https://mcp-tools-z-image-turbo.hf.space/--replicas/hvrlk/gradio_api/file=/tmp/gradio/abc/image.webp"
    ]


def test_extracts_common_extensions_and_dedupes():
    text = "see https://x.com/a.png and https://x.com/a.png and http://y.io/b.JPEG"
    assert _extract_image_urls(text) == ["https://x.com/a.png", "http://y.io/b.JPEG"]


def test_ignores_non_image_urls_and_empty():
    assert _extract_image_urls("visit https://example.com/page and ftp://x/y.png") == []
    assert _extract_image_urls("") == []
