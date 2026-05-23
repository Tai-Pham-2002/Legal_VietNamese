"""
Background ingestion task: parse -> chunk -> embed -> index.

Idempotent: nếu doc đã `indexed`, skip. Nếu retry sau lỗi, xoá vectors cũ
trước khi index lại.

Publish progress qua Redis Pub/Sub channel `doc:{doc_id}:events` để API có
thể stream tới client qua SSE.
"""

from __future__ import annotations

import uuid
from typing import Any

import orjson

from Legal_VietNamese.src.core.db import session_scope
from Legal_VietNamese.src.core.logging import get_logger
from Legal_VietNamese.src.core.minio import get_object_bytes, put_object
from Legal_VietNamese.src.core.redis import get_redis
from Legal_VietNamese.src.db.repositories import DocumentRepo
from Legal_VietNamese.src.ingestion.chunkers import chunk_document
from Legal_VietNamese.src.ingestion.indexer import delete_doc_vectors, index_chunks
from Legal_VietNamese.src.ingestion.parsers import parse_file

log = get_logger(__name__)


async def _publish(doc_id: uuid.UUID, event: str, data: dict[str, Any] | None = None) -> None:
    r = get_redis()
    payload = {"event": event, "data": data or {}}
    await r.publish(f"doc:{doc_id}:events", orjson.dumps(payload))


async def process_document(ctx: dict[str, Any], doc_id_str: str) -> dict[str, Any]:
    doc_id = uuid.UUID(doc_id_str)
    log.info("ingestion_start", doc_id=str(doc_id))

    async with session_scope() as session:
        repo = DocumentRepo(session)
        doc = await repo.get_internal(doc_id)
        if doc is None:
            log.warning("ingestion_doc_missing", doc_id=str(doc_id))
            return {"status": "missing"}
        if doc.status == "indexed":
            log.info("ingestion_already_indexed", doc_id=str(doc_id))
            return {"status": "indexed"}

        # snapshot fields trước khi commit
        storage_key = doc.storage_key
        mime_type = doc.mime_type
        title = doc.title
        user_id = doc.user_id
        tenant_id = doc.tenant_id

        await repo.set_status(doc_id, "parsing")
    await _publish(doc_id, "status", {"status": "parsing"})

    try:
        # ---- download ----
        data = await get_object_bytes(storage_key)

        # ---- parse ----
        parsed = await parse_file(data, mime_type=mime_type, filename=title)
        markdown_key = storage_key.rsplit("/", 1)[0] + "/parsed.md"
        await put_object(markdown_key, parsed.markdown.encode("utf-8"), "text/markdown")

        async with session_scope() as session:
            await DocumentRepo(session).set_status(
                doc_id, "chunking", markdown_key=markdown_key,
                meta={"parse": parsed.meta},
            )
        await _publish(doc_id, "status", {"status": "chunking"})

        # ---- chunk ----
        chunks = chunk_document(parsed)
        if not chunks:
            raise ValueError("no chunks produced (empty document?)")

        async with session_scope() as session:
            await DocumentRepo(session).set_status(doc_id, "embedding")
        await _publish(doc_id, "status", {"status": "embedding", "n_chunks": len(chunks)})

        # ---- index (clean re-run) ----
        await delete_doc_vectors(doc_id)
        orm_rows = await index_chunks(
            chunks,
            doc_id=doc_id,
            user_id=user_id,
            tenant_id=tenant_id,
            doc_title=title,
        )

        async with session_scope() as session:
            await DocumentRepo(session).bulk_insert_chunks(orm_rows)
            await DocumentRepo(session).set_status(
                doc_id, "indexed", n_chunks=len(orm_rows)
            )

        await _publish(doc_id, "status", {"status": "indexed", "n_chunks": len(orm_rows)})
        log.info("ingestion_done", doc_id=str(doc_id), n_chunks=len(orm_rows))
        return {"status": "indexed", "n_chunks": len(orm_rows)}

    except Exception as e:
        log.exception("ingestion_failed", doc_id=str(doc_id), error=str(e))
        async with session_scope() as session:
            await DocumentRepo(session).set_status(doc_id, "failed", error=str(e)[:2000])
        await _publish(doc_id, "status", {"status": "failed", "error": str(e)[:500]})
        # raise để ARQ mark job failed (visible trên monitoring)
        raise
