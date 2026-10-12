"""KnowledgeBaseService — orchestrates KB creation, document upload, and RAG search.

Processing pipeline (per upload):
  1. Validate file type (magic bytes) + size
  2. ClamAV scan — fail-closed: reject if scan fails
  3. Dedup by SHA-256 hash within the same KB
  4. Store in Supabase Storage
  5. INSERT kb_documents (status=pending) + enqueue kb_jobs — return 202 immediately
  6. Dedicated worker drains kb_jobs: parse → chunk → embed (batched 32) → ready
     (out-of-process, so a burst of uploads never blocks the API event loop)

Search:
  vector_search (HNSW cosine, O(log n)) fused with BM25 via RRF (k=60).
  Results scoped to workspace_id at query time + enforced by RLS.
"""

import asyncio
import hashlib
import logging
import uuid
from typing import Any

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.document_processor.base import DocumentProcessorProtocol
from sila.adapters.document_processor.pypdf_adapter import PypdfAdapter
from sila.adapters.embedding.base import EmbeddingAdapter
from sila.adapters.embedding.ollama import OllamaEmbeddingAdapter
from sila.adapters.storage.base import ObjectStoreProtocol
from sila.adapters.storage.supabase import SupabaseStorageAdapter
from sila.adapters.virus_scanner.base import VirusScannerProtocol
from sila.adapters.virus_scanner.clamav import ClamAVAdapter
from sila.core.config import settings
from sila.core.constants import Action, Outcome, ResourceType
from sila.db.models import KBChunk
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.knowledge_base import (
    ChunkRepository,
    DocumentRepository,
    KBJobRepository,
    KBRepository,
)

logger = logging.getLogger(__name__)

_ALLOWED_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
}
_MAGIC_SIGNATURES: dict[bytes, str] = {
    b"%PDF": "pdf",
    b"PK\x03\x04": "docx",  # ZIP-based (DOCX, XLSX, etc.)
}


def _storage_key(workspace_id: uuid.UUID, kb_id: uuid.UUID, file_type: str) -> str:
    """The object-store key for an upload — FULLY synthetic (uuid + the validated
    file type). The user's filename must NEVER enter the key: non-ASCII names
    (e.g. Arabic) break Storage keys/URLs, and user input doesn't belong in a
    server-constructed path. The original filename lives in the DB for display."""
    return f"ws/{workspace_id}/kb/{kb_id}/{uuid.uuid4()}.{file_type}"


def _detect_type(content: bytes, declared_ext: str) -> str:
    """Validate file type by magic bytes; fall back to declared extension for text formats."""
    for sig, ftype in _MAGIC_SIGNATURES.items():
        if content.startswith(sig):
            return ftype
    # Text formats have no reliable magic bytes — trust declared extension
    if declared_ext in ("txt", "md", "html"):
        return declared_ext
    return declared_ext  # caller validates against allow-list


def _chunk_text(pages: list[str], chunk_tokens: int, overlap: int) -> list[dict[str, Any]]:
    """Sliding-window chunker over page strings.

    Splits on whitespace, respects chunk_tokens and overlap.
    O(n) over total tokens. Returns list of {content, metadata}.
    """
    chunks: list[dict[str, Any]] = []
    words: list[tuple[str, int]] = []  # (word, page_num)
    for page_num, page_text in enumerate(pages):
        for word in page_text.split():
            words.append((word, page_num))

    start = 0
    while start < len(words):
        end = min(start + chunk_tokens, len(words))
        chunk_words = [w for w, _ in words[start:end]]
        page_num = words[start][1]
        chunks.append(
            {
                "content": " ".join(chunk_words),
                "metadata": {"page_num": page_num + 1, "chunk_start_word": start},
            }
        )
        start += chunk_tokens - overlap

    return chunks


class KnowledgeBaseService:
    def __init__(
        self,
        db: AsyncSession,
        processor: DocumentProcessorProtocol,
        embedder: EmbeddingAdapter,
        scanner: VirusScannerProtocol,
        store: ObjectStoreProtocol,
    ) -> None:
        self._db = db
        self._processor = processor
        self._embedder = embedder
        self._scanner = scanner
        self._store = store
        self._kbs = KBRepository(db)
        self._docs = DocumentRepository(db)
        self._chunks = ChunkRepository(db)
        self._jobs = KBJobRepository(db)
        self._audit = AuditRepository(db)

    # ------------------------------------------------------------------
    # Knowledge Base CRUD
    # ------------------------------------------------------------------

    async def create_kb(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        description: str = "",
        scope: str = "workspace",
        agent_id: uuid.UUID | None = None,
        actor_id: uuid.UUID,
    ) -> dict[str, Any]:
        async with self._db.begin_nested():
            kb = await self._kbs.create(
                workspace_id=workspace_id,
                name=name,
                description=description,
                scope=scope,
                agent_id=agent_id,
                created_by=actor_id,
            )
            await self._audit.record(
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                action=Action.KB_CREATE,
                resource_type=ResourceType.KB,
                resource_id=kb.id,
                outcome=Outcome.SUCCESS,
            )
        return _kb_out(kb)

    async def list_kbs(self, workspace_id: uuid.UUID, *, page: int, size: int) -> dict[str, Any]:
        items, total = await self._kbs.list_by_workspace(workspace_id, page=page, size=size)
        counts = await self._kbs.document_counts([k.id for k in items])
        return {
            "items": [_kb_out(k, counts.get(k.id, 0)) for k in items],
            "total": total,
            "page": page,
            "size": size,
        }

    async def get_kb(self, kb_id: uuid.UUID, workspace_id: uuid.UUID) -> dict[str, Any]:
        kb = await self._kbs.get(kb_id, workspace_id)
        if not kb:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge base not found")
        return _kb_out(kb)

    async def delete_kb(
        self, kb_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        kb = await self._kbs.get(kb_id, workspace_id)
        if not kb:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge base not found")
        async with self._db.begin_nested():
            await self._kbs.archive(kb_id, workspace_id)
            await self._audit.record(
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                action=Action.KB_DELETE,
                resource_type=ResourceType.KB,
                resource_id=kb_id,
                outcome=Outcome.SUCCESS,
            )

    # ------------------------------------------------------------------
    # Document Upload (202 Accepted + enqueue kb_jobs)
    # ------------------------------------------------------------------

    async def upload_document(
        self,
        *,
        kb_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        file: UploadFile,
    ) -> dict[str, Any]:
        # Validate KB exists
        kb = await self._kbs.get(kb_id, workspace_id)
        if not kb:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge base not found")

        content = await file.read()
        file_size = len(content)

        # Size check
        max_bytes = settings.document_max_mb * 1024 * 1024
        if file_size > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"File exceeds {settings.document_max_mb} MB limit",
            )

        # File type validation (magic bytes + extension)
        ext = (file.filename or "").rsplit(".", 1)[-1].lower()
        file_type = _detect_type(content, ext)
        if file_type not in _ALLOWED_TYPES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"File type '{file_type}' not allowed. Use: {', '.join(_ALLOWED_TYPES)}",
            )

        # ClamAV scan — fail-closed
        try:
            is_clean = await self._scanner.scan(content)
        except (RuntimeError, TimeoutError) as exc:
            # TimeoutError happens on a cold scanner (signature reload after a stack
            # restart) — it used to escape as a raw 500 with no message.
            logger.error("ClamAV scan error: %s", exc)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Virus scanner unavailable or timed out — try the upload again in a moment",
            ) from exc
        if not is_clean:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "File failed virus scan")

        content_hash = hashlib.sha256(content).hexdigest()

        # Dedup: if same file already uploaded to this KB, return existing
        existing = await self._docs.find_duplicate(kb_id, content_hash)
        if existing:
            return {"document_id": str(existing.id), "status": existing.status, "duplicate": True}

        # Store in Supabase Storage — the key is FULLY synthetic; the original
        # filename (Arabic, spaces, anything) lives only in the DB `filename`
        # column for display.
        storage_path = _storage_key(workspace_id, kb_id, file_type)
        await self._store.put(storage_path, content, _ALLOWED_TYPES[file_type])

        async with self._db.begin_nested():
            doc = await self._docs.create(
                kb_id=kb_id,
                workspace_id=workspace_id,
                uploaded_by=actor_id,
                filename=file.filename or "document",
                file_type=file_type,
                file_size=file_size,
                storage_path=storage_path,
                content_hash=content_hash,
            )
            await self._audit.record(
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                action=Action.DOCUMENT_UPLOAD,
                resource_type=ResourceType.DOCUMENT,
                resource_id=doc.id,
                outcome=Outcome.SUCCESS,
            )

        # Enqueue the processing job in the SAME transaction as the document, then
        # commit both. The NOTIFY fires on commit so the worker only wakes once the
        # document row is visible. Processing runs out-of-process in the worker —
        # nothing heavy touches the API event loop.
        await self._jobs.enqueue(document_id=doc.id, kb_id=kb_id, workspace_id=workspace_id)
        await self._db.commit()

        return {"document_id": str(doc.id), "status": "pending", "duplicate": False}

    async def copy_documents(
        self,
        *,
        source_kb_id: uuid.UUID,
        source_workspace_id: uuid.UUID,
        dest_kb_id: uuid.UUID,
        dest_workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> list[str]:
        """Copy every document of a source KB into a destination KB.

        Bytes are re-uploaded through upload_document, so virus scanning, type
        checks, and the async ingestion pipeline all re-run in the DESTINATION
        workspace — the copy owns its rows end to end. Powers the Bazaar's
        "runs on included data" hire: the source is read cross-workspace
        server-side, gated upstream by the publisher's explicit share_docs
        opt-in at publish time. NOTE: upload_document commits per document.

        Complexity: O(docs) storage round-trips; parsing/embedding stays async.
        """
        from io import BytesIO

        from starlette.datastructures import UploadFile as StarletteUploadFile

        docs, _total = await self._docs.list_by_kb(
            source_kb_id, source_workspace_id, page=1, size=200
        )
        copied: list[str] = []
        for doc in docs:
            content = await self._store.get(doc.storage_path)
            upload = StarletteUploadFile(filename=doc.filename, file=BytesIO(content))
            await self.upload_document(
                kb_id=dest_kb_id,
                workspace_id=dest_workspace_id,
                actor_id=actor_id,
                file=upload,  # type: ignore[arg-type]  # duck-compatible with fastapi's
            )
            copied.append(doc.filename)
        return copied

    async def process_document(
        self,
        *,
        doc_id: uuid.UUID,
        kb_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> None:
        """Worker pipeline: fetch → parse → PII flag → chunk → embed → store.

        Runs on the caller's (worker's) session. Re-reads the file from object
        storage by ``doc_id`` so the heavy bytes never ride the queue. Sets the
        document 'processing' then 'ready'; **raises** on any failure so the
        worker marks the job failed/retry and the document 'failed' in one place.
        """
        doc = await self._docs.get(doc_id, workspace_id)
        if not doc:
            raise RuntimeError(f"document {doc_id} not found in workspace {workspace_id}")
        content = await self._store.get(doc.storage_path)
        file_type = doc.file_type

        await self._docs.set_status(doc_id, "processing")
        # Idempotent re-run: clear chunks from any previous (failed/reclaimed)
        # attempt so a retry never collides on the (document_id, chunk_index)
        # unique constraint. Makes the whole pipeline safe to re-execute.
        await self._chunks.delete_by_document(doc_id)
        await self._db.commit()

        # 1. Parse (offloaded to a thread inside the adapter)
        pages = await self._processor.extract_pages(content, file_type)
        page_count = len(pages)

        # 2. PII flag (Phase 6 enforces; here we log). Presidio spaCy NER is
        #    CPU-bound — offload off the loop.
        await asyncio.to_thread(_flag_pii, pages, doc_id)

        # 3. Chunk — pure-Python string work; offload to stay responsive.
        raw_chunks = await asyncio.to_thread(
            _chunk_text,
            pages,
            settings.document_chunk_tokens,
            settings.document_chunk_overlap,
        )
        total_tokens = sum(len(c["content"].split()) for c in raw_chunks)

        # 4. Embed in batches of settings.embedding_batch_size
        all_embeddings: list[list[float]] = []
        batch_size = settings.embedding_batch_size
        texts = [c["content"] for c in raw_chunks]
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            embeddings = await self._embedder.embed_batch(batch)
            all_embeddings.extend(embeddings)

        # 5. Build KBChunk objects and bulk insert
        chunk_objs = [
            KBChunk(
                document_id=doc_id,
                kb_id=kb_id,
                workspace_id=workspace_id,
                chunk_index=idx,
                content=raw_chunks[idx]["content"],
                token_count=max(1, len(raw_chunks[idx]["content"].split())),
                chunk_metadata=raw_chunks[idx]["metadata"],
            )
            for idx in range(len(raw_chunks))
        ]
        await self._chunks.bulk_insert(chunk_objs)

        # 6. Set embeddings via raw SQL (pgvector type)
        for idx, emb in enumerate(all_embeddings):
            vec = "[" + ",".join(str(v) for v in emb) + "]"
            await self._db.execute(
                sa_text(
                    "UPDATE kb_chunks SET embedding = :emb ::vector"
                    " WHERE document_id = :doc_id AND chunk_index = :idx"
                ),
                {"emb": vec, "doc_id": str(doc_id), "idx": idx},
            )

        await self._docs.set_status(
            doc_id, "ready", page_count=page_count, token_count=total_tokens
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Document management
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        kb_id: uuid.UUID,
        workspace_id: uuid.UUID,
        *,
        page: int,
        size: int,
    ) -> dict[str, Any]:
        items, total = await self._docs.list_by_kb(kb_id, workspace_id, page=page, size=size)
        return {
            "items": [_doc_out(d) for d in items],
            "total": total,
            "page": page,
            "size": size,
        }

    async def get_document(self, doc_id: uuid.UUID, workspace_id: uuid.UUID) -> dict[str, Any]:
        doc = await self._docs.get(doc_id, workspace_id)
        if not doc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
        return _doc_out(doc)

    async def delete_document(
        self, doc_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        doc = await self._docs.get(doc_id, workspace_id)
        if not doc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

        async with self._db.begin_nested():
            await self._chunks.delete_by_document(doc_id)
            path = await self._docs.delete(doc_id, workspace_id)
            await self._audit.record(
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                action=Action.DOCUMENT_DELETE,
                resource_type=ResourceType.DOCUMENT,
                resource_id=doc_id,
                outcome=Outcome.SUCCESS,
            )

        if path:
            try:
                await self._store.delete(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Storage delete failed for %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Hybrid Search (vector + BM25, RRF fusion)
    # ------------------------------------------------------------------

    async def search(
        self,
        kb_id: uuid.UUID,
        workspace_id: uuid.UUID,
        query: str,
        top_k: int = 10,
    ) -> dict[str, Any]:
        kb = await self._kbs.get(kb_id, workspace_id)
        if not kb:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge base not found")

        # Embed query (single text, not batched)
        query_embedding = (await self._embedder.embed_batch([query]))[0]

        # Vector search (HNSW) — O(log n)
        vector_hits = await self._chunks.vector_search(
            query_embedding, kb_id, workspace_id, top_k=top_k * 2
        )

        # RRF fusion: score = Σ 1/(k + rank) where k=60 (standard constant)
        # With only vector results in Phase 4, this is an identity pass.
        # Phase 4+ can add BM25 hits from Meilisearch here for true hybrid.
        results = _rrf_fuse([vector_hits], top_k=top_k)

        return {"results": results, "total": len(results)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_pii_engine: Any = None
_pii_engine_loaded = False


def _get_pii_engine() -> Any:
    """Lazily build the Presidio engine ONCE. Loading spaCy models is heavy —
    rebuilding it per document was a big chunk of the 'processing takes long'.
    """
    global _pii_engine, _pii_engine_loaded
    if not _pii_engine_loaded:
        _pii_engine_loaded = True
        try:
            import spacy.util

            # Construct the engine ONLY if the model is already installed. If it
            # isn't, Presidio/spaCy would try to DOWNLOAD it — which hangs forever
            # behind a TLS-intercepting proxy (no timeout, not an exception). PII
            # flagging is best-effort in Phase 4 (Phase 6 enforces), so degrade to
            # a no-op rather than block the worker.
            if not spacy.util.is_package("en_core_web_lg"):
                logger.warning(
                    "spaCy model en_core_web_lg not installed — skipping PII flag "
                    "(provision the model to enable PII detection)"
                )
                _pii_engine = None
                return _pii_engine

            from presidio_analyzer import AnalyzerEngine

            _pii_engine = AnalyzerEngine()
        except Exception as exc:  # noqa: BLE001 — PII flag is best-effort in Phase 4
            logger.warning("PII engine unavailable — skipping PII flag: %s", exc)
            _pii_engine = None
    return _pii_engine


def _flag_pii(pages: list[str], doc_id: uuid.UUID) -> None:
    """Log PII detections. Phase 6 will block; Phase 4 only flags.

    Runs off the event loop (called via asyncio.to_thread) — spaCy NER is
    CPU-bound and would otherwise freeze the whole API mid-upload.
    """
    engine = _get_pii_engine()
    if engine is None:
        return
    for page in pages:
        results = engine.analyze(text=page, language="en")
        if results:
            logger.warning(
                "PII detected in document %s: %s",
                doc_id,
                [r.entity_type for r in results],
            )


def _rrf_fuse(
    result_lists: list[list[dict[str, Any]]], *, top_k: int, k: int = 60
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion. O(k log k) where k = total candidates."""
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}
    for result_list in result_lists:
        for rank, item in enumerate(result_list):
            cid = str(item["chunk_id"])
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            items[cid] = item
    return [
        {**items[cid], "rrf_score": round(score, 6)}
        for cid, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    ]


def _kb_out(kb: object, document_count: int = 0) -> dict[str, Any]:
    return {
        "id": str(kb.id),  # type: ignore[attr-defined]
        "workspace_id": str(kb.workspace_id),  # type: ignore[attr-defined]
        "name": kb.name,  # type: ignore[attr-defined]
        "description": kb.description,  # type: ignore[attr-defined]
        "scope": kb.scope,  # type: ignore[attr-defined]
        "agent_id": str(kb.agent_id) if kb.agent_id else None,  # type: ignore[attr-defined]
        "status": kb.status,  # type: ignore[attr-defined]
        "created_at": kb.created_at.isoformat(),  # type: ignore[attr-defined]
        "document_count": document_count,
    }


def _doc_out(doc: object) -> dict[str, Any]:
    return {
        "id": str(doc.id),  # type: ignore[attr-defined]
        "kb_id": str(doc.kb_id),  # type: ignore[attr-defined]
        "filename": doc.filename,  # type: ignore[attr-defined]
        "file_type": doc.file_type,  # type: ignore[attr-defined]
        "file_size": doc.file_size,  # type: ignore[attr-defined]
        "status": doc.status,  # type: ignore[attr-defined]
        "error_detail": doc.error_detail,  # type: ignore[attr-defined]
        "page_count": doc.page_count,  # type: ignore[attr-defined]
        "token_count": doc.token_count,  # type: ignore[attr-defined]
        "created_at": doc.created_at.isoformat(),  # type: ignore[attr-defined]
    }


def build_kb_service(db: AsyncSession) -> KnowledgeBaseService:
    """Construct the service with all production adapters from settings.

    Shared by the API request dependency and the document worker so the adapter
    wiring lives in exactly one place (Rule 7 DRY).
    """
    return KnowledgeBaseService(
        db=db,
        processor=PypdfAdapter(),
        embedder=OllamaEmbeddingAdapter(
            base_url=settings.ollama_url,
            model=settings.embedding_model,
        ),
        scanner=ClamAVAdapter(
            host=settings.clamav_host,
            port=settings.clamav_port,
            timeout=settings.clamav_timeout_s,
        ),
        store=SupabaseStorageAdapter(
            storage_url=settings.supabase_storage_url,
            service_key=settings.sila_supabase_service_key,
        ),
    )
