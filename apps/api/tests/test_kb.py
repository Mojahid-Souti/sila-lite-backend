"""Knowledge Base endpoint unit tests — auth guards + service-layer logic.

Integration tests (real DB + ClamAV + Ollama) are in the manual test checklist.
These unit tests verify:
  1. All KB endpoints reject unauthenticated requests (auth guards).
  2. Chunking helper produces correct sliding-window output.
  3. RRF fusion correctly scores and ranks candidates.
  4. File type detection correctly identifies magic bytes.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from sila.main import app
from sila.services.kb_service import _chunk_text, _detect_type, _rrf_fuse

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None]:
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Auth guard tests — all endpoints must reject unauthenticated callers
# ---------------------------------------------------------------------------

WS = uuid.uuid4()
KB = uuid.uuid4()
DOC = uuid.uuid4()


def test_list_kbs_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{WS}/kbs")
    assert r.status_code in (401, 403)


def test_create_kb_unauthenticated() -> None:
    r = client.post(f"/v1/workspaces/{WS}/kbs", json={"name": "Test KB"})
    assert r.status_code in (401, 403)


def test_get_kb_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{WS}/kbs/{KB}")
    assert r.status_code in (401, 403)


def test_delete_kb_unauthenticated() -> None:
    r = client.delete(f"/v1/workspaces/{WS}/kbs/{KB}")
    assert r.status_code in (401, 403)


def test_upload_document_unauthenticated() -> None:
    r = client.post(
        f"/v1/workspaces/{WS}/kbs/{KB}/documents",
        files={"file": ("test.txt", b"hello world", "text/plain")},
    )
    assert r.status_code in (401, 403)


def test_list_documents_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{WS}/kbs/{KB}/documents")
    assert r.status_code in (401, 403)


def test_get_document_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{WS}/kbs/{KB}/documents/{DOC}")
    assert r.status_code in (401, 403)


def test_delete_document_unauthenticated() -> None:
    r = client.delete(f"/v1/workspaces/{WS}/kbs/{KB}/documents/{DOC}")
    assert r.status_code in (401, 403)


def test_search_unauthenticated() -> None:
    r = client.post(
        f"/v1/workspaces/{WS}/kbs/{KB}/search",
        json={"query": "test query", "top_k": 5},
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# _chunk_text — sliding window chunker
# ---------------------------------------------------------------------------


def test_chunk_text_basic() -> None:
    """Single page, exact chunk boundary."""
    pages = ["word " * 10]  # 10 words
    chunks = _chunk_text(pages, chunk_tokens=5, overlap=0)
    assert len(chunks) == 2
    assert chunks[0]["content"] == "word word word word word"
    assert chunks[1]["content"] == "word word word word word"


def test_chunk_text_overlap() -> None:
    """Overlap means consecutive chunks share tokens; a trailing partial chunk is kept."""
    pages = ["a b c d e f g h i j"]  # 10 words
    chunks = _chunk_text(pages, chunk_tokens=4, overlap=2)
    # start=0 → [a,b,c,d]; 2 → [c,d,e,f]; 4 → [e,f,g,h]; 6 → [g,h,i,j]; 8 → [i,j]
    assert len(chunks) == 5
    assert chunks[0]["content"] == "a b c d"
    assert chunks[1]["content"] == "c d e f"
    assert chunks[-1]["content"] == "i j"  # partial tail chunk


def test_chunk_text_empty_page() -> None:
    """Empty input produces no chunks."""
    assert _chunk_text([], chunk_tokens=1024, overlap=128) == []
    assert _chunk_text([""], chunk_tokens=1024, overlap=128) == []


def test_chunk_text_page_metadata() -> None:
    """Each chunk carries page_num (1-indexed)."""
    pages = ["hello world", "foo bar baz"]
    chunks = _chunk_text(pages, chunk_tokens=2, overlap=0)
    page_nums = [c["metadata"]["page_num"] for c in chunks]
    # First two words are from page 1, second two from page 2
    assert page_nums[0] == 1
    assert page_nums[-1] == 2


# ---------------------------------------------------------------------------
# _detect_type — magic bytes file type detection
# ---------------------------------------------------------------------------


def test_detect_type_pdf() -> None:
    content = b"%PDF-1.4 rest of file..."
    assert _detect_type(content, "pdf") == "pdf"


def test_detect_type_docx() -> None:
    content = b"PK\x03\x04rest of zip..."
    assert _detect_type(content, "docx") == "docx"


def test_detect_type_txt_fallback() -> None:
    """TXT has no magic bytes — falls back to declared extension."""
    content = b"This is plain text without magic bytes."
    assert _detect_type(content, "txt") == "txt"


def test_detect_type_md_fallback() -> None:
    content = b"# Markdown heading"
    assert _detect_type(content, "md") == "md"


def test_detect_type_html_fallback() -> None:
    content = b"<html><body>test</body></html>"
    assert _detect_type(content, "html") == "html"


# ---------------------------------------------------------------------------
# _rrf_fuse — Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def _make_hit(chunk_id: str, score: float = 0.9) -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": str(uuid.uuid4()),
        "content": "test content",
        "chunk_metadata": {},
        "filename": "test.pdf",
        "score": score,
    }


def test_rrf_fuse_single_list() -> None:
    """Single result list: RRF rank = position in list."""
    hits = [_make_hit("a"), _make_hit("b"), _make_hit("c")]
    results = _rrf_fuse([hits], top_k=3)
    assert [r["chunk_id"] for r in results] == ["a", "b", "c"]


def test_rrf_fuse_deduplication() -> None:
    """Same chunk_id appearing in two lists gets a higher combined score."""
    list1 = [_make_hit("x"), _make_hit("y")]
    list2 = [_make_hit("x"), _make_hit("z")]
    results = _rrf_fuse([list1, list2], top_k=3)
    # x appears in both lists → highest RRF score
    assert results[0]["chunk_id"] == "x"


def test_rrf_fuse_top_k_limit() -> None:
    hits = [_make_hit(f"chunk_{i}") for i in range(20)]
    results = _rrf_fuse([hits], top_k=5)
    assert len(results) == 5


def test_rrf_fuse_empty() -> None:
    assert _rrf_fuse([], top_k=10) == []
    assert _rrf_fuse([[]], top_k=10) == []


# ---------------------------------------------------------------------------
# Storage key — the confirmed Arabic-filename root cause (regression guard)
# ---------------------------------------------------------------------------


def test_storage_key_is_synthetic_and_ascii() -> None:
    """Uploading `الاجازة.txt` failed because the raw filename was embedded in the
    object key. The key is now fully synthetic — it takes no filename at all, is
    pure ASCII, and ends with the VALIDATED type, whatever the user named the file."""
    import inspect
    import re as _re

    from sila.services.kb_service import _storage_key

    ws, kb = uuid.uuid4(), uuid.uuid4()
    key = _storage_key(ws, kb, "txt")
    assert key.isascii()
    assert _re.fullmatch(rf"ws/{ws}/kb/{kb}/[0-9a-f-]{{36}}\.txt", key)
    # Structural guarantee: the filename cannot leak into the key — the builder
    # doesn't even accept one.
    assert "filename" not in inspect.signature(_storage_key).parameters
