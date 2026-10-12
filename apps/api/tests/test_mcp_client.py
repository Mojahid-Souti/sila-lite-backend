"""Unit tests for the Streamable-HTTP MCP client's parsing logic.

The end-to-end transport (initialize handshake / session / fallback) is
verified against real servers in the manual test; here we lock down the
non-trivial pure logic: SSE decoding and JSON-RPC message selection, which
must handle both plain-JSON and `text/event-stream` responses.
"""

from __future__ import annotations

import httpx
import pytest

from sila.adapters.mcp.http_client import (
    MCPError,
    _parse_content_blocks,
    _parse_message,
    _parse_sse,
)


def _resp(body: str, content_type: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": content_type}, text=body)


# ---- SSE decoding --------------------------------------------------------


def test_parse_sse_single_event():
    text = 'event: message\ndata: {"jsonrpc":"2.0","id":"1","result":{"ok":true}}\n\n'
    msgs = _parse_sse(text)
    assert msgs == [{"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}]


def test_parse_sse_multiple_events():
    text = (
        'data: {"jsonrpc":"2.0","method":"x/notify"}\n\n'
        'data: {"jsonrpc":"2.0","id":"42","result":{"tools":[]}}\n\n'
    )
    msgs = _parse_sse(text)
    assert len(msgs) == 2
    assert msgs[1]["id"] == "42"


def test_parse_sse_ignores_non_data_and_bad_json():
    text = 'event: ping\n\ndata: not-json\n\ndata: {"id":"1","result":{}}\n\n'
    msgs = _parse_sse(text)
    assert msgs == [{"id": "1", "result": {}}]


def test_parse_sse_crlf_multi_event():
    # Real-server shape (DeepWiki): CRLF line endings + a progress event before
    # the result. Must split into two messages, not one corrupt blob.
    text = (
        'event: message\r\ndata: {"jsonrpc":"2.0","method":"x/progress"}\r\n\r\n'
        'event: message\r\ndata: {"jsonrpc":"2.0","id":"CALLID","result":{"ok":1}}\r\n\r\n'
    )
    msgs = _parse_sse(text)
    assert len(msgs) == 2
    assert msgs[1] == {"jsonrpc": "2.0", "id": "CALLID", "result": {"ok": 1}}


# ---- message selection ---------------------------------------------------


def test_parse_message_plain_json():
    resp = _resp('{"jsonrpc":"2.0","id":"abc","result":{"value":1}}', "application/json")
    msg = _parse_message(resp, "abc")
    assert msg["result"] == {"value": 1}


def test_parse_message_sse_matches_id():
    body = (
        'data: {"jsonrpc":"2.0","id":"other","result":{"nope":1}}\n\n'
        'data: {"jsonrpc":"2.0","id":"target","result":{"yes":1}}\n\n'
    )
    msg = _parse_message(_resp(body, "text/event-stream"), "target")
    assert msg["result"] == {"yes": 1}


def test_parse_message_falls_back_to_first_response_shape():
    # Server echoed a different id than we sent — still pick the response message.
    resp = _resp('{"jsonrpc":"2.0","id":"server-gen","result":{"ok":1}}', "application/json")
    msg = _parse_message(resp, "client-id")
    assert msg["result"] == {"ok": 1}


def test_parse_message_non_json_raises():
    resp = _resp("<html>oops</html>", "text/html")
    with pytest.raises(MCPError):
        _parse_message(resp, "1")


def test_parse_message_no_response_message_raises():
    # Only a notification (no id, no result/error) → nothing to answer with.
    resp = _resp('data: {"jsonrpc":"2.0","method":"x/notify"}\n\n', "text/event-stream")
    with pytest.raises(MCPError):
        _parse_message(resp, "1")


# ---- content-block parsing (text + images) -------------------------------


def test_parse_content_blocks_text_only():
    text, images = _parse_content_blocks([{"type": "text", "text": "hello"}])
    assert text == "hello"
    assert images == []


def test_parse_content_blocks_image_only():
    text, images = _parse_content_blocks(
        [{"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]
    )
    assert text == ""
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert images[0].data == "iVBORw0KGgo="


def test_parse_content_blocks_mixed_and_ignores_unknown():
    text, images = _parse_content_blocks(
        [
            {"type": "text", "text": "caption"},
            {"type": "image", "data": "abc", "mimeType": "image/jpeg"},
            {"type": "audio", "data": "xyz", "mimeType": "audio/wav"},
        ]
    )
    assert text == "caption"
    assert len(images) == 1
    assert images[0].mime_type == "image/jpeg"


def test_parse_content_blocks_skips_malformed_image():
    # Missing mimeType → not a usable image, skip it.
    text, images = _parse_content_blocks([{"type": "image", "data": "abc"}])
    assert images == []
