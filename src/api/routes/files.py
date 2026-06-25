"""
File upload + status tracking.

POST /v1/files (multipart, nhiều file):
- Per file: validate -> dedupe theo checksum -> upload MinIO -> insert Doc ->
  enqueue ingestion job.
- Trả 202 với danh sách Doc + job_id.

GET /v1/files/{id} -> trạng thái + n_chunks.
GET /v1/files/{id}/events -> SSE stream progress (Pub/Sub Redis).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import orjson
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sse_starlette.sse import EventSourceResponse

from src.core.logging import get_logger
from src.core.minio import put_object
from src.core.redis import get_redis
from src.core.settings import get_settings
from src.db.repositories import DocumentRepo

from ..deps import (
    CurrentUserDep,
    SessionDep,
    get_arq_pool,
    rate_limit,
)
from ..schemas import DocumentOut, UploadResponse

router = APIRouter()
log = get_logger(__name__)


def _doc_to_out(d) -> DocumentOut:  # type: ignore[no-untyped-def]
    return DocumentOut(
        id=d.id,
        title=d.title,
        mime_type=d.mime_type,
        size_bytes=d.size_bytes,
        status=d.status,
        n_chunks=d.n_chunks,
        error=d.error,
        created_at=d.created_at,
        indexed_at=d.indexed_at,
    )


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("upload", limit=10, window_s=60))],
)
async def upload_files(
    current: CurrentUserDep,
    session: SessionDep,
    files: Annotated[list[UploadFile], File(description="Multiple files")],
) -> UploadResponse:
    user_id, tenant_id = current
    s = get_settings()
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files")
    if len(files) > s.app.max_files_per_request:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"too many files (max {s.app.max_files_per_request})",
        )

    arq = await get_arq_pool()
    repo = DocumentRepo(session)
    out_docs = []
    job_ids: list[str] = []
    to_enqueue: list[str] = []  # doc_id mới — enqueue SAU khi commit
    max_bytes = s.app.max_upload_size_mb * 1024 * 1024

    for up in files:
        data = await up.read()
        if len(data) == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"empty file: {up.filename}")
        if len(data) > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"{up.filename} > {s.app.max_upload_size_mb}MB",
            )
        mime = up.content_type or "application/octet-stream"
        if mime not in s.app.allowed_mime_types:
            # fallback theo extension
            name = (up.filename or "").lower()
            if not name.endswith((".pdf", ".txt", ".md", ".docx")):
                raise HTTPException(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    f"unsupported type: {mime}",
                )

        checksum = hashlib.sha256(data).hexdigest()

        # dedupe per tenant -> trả về doc cũ
        existing = await repo.by_checksum(tenant_id=tenant_id, checksum=checksum)
        if existing is not None:
            out_docs.append(_doc_to_out(existing))
            continue

        doc_id = uuid.uuid4()
        storage_key = f"tenants/{tenant_id}/docs/{doc_id}/raw"
        await put_object(storage_key, data, content_type=mime)

        d = await repo.create(
            tenant_id=tenant_id,
            user_id=user_id,
            title=up.filename or "untitled",
            mime_type=mime,
            size_bytes=len(data),
            checksum=checksum,
            storage_key=storage_key,
        )
        # gán ID đã sinh trong session — repo.create đã trả đối tượng có id.
        await session.flush()

        to_enqueue.append(str(d.id))
        out_docs.append(_doc_to_out(d))

    # Commit TRƯỚC khi enqueue: worker chạy trên connection khác, nếu enqueue
    # trước commit thì worker có thể đọc DB và không thấy row (job fail/race).
    await session.commit()

    for doc_id_str in to_enqueue:
        job = await arq.enqueue_job("process_document", doc_id_str)
        if job is not None:
            job_ids.append(job.job_id)

    return UploadResponse(documents=out_docs, job_ids=job_ids)


@router.get("", response_model=list[DocumentOut])
async def list_files(
    current: CurrentUserDep,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[DocumentOut]:
    user_id, _ = current
    items = await DocumentRepo(session).list_for_user(user_id=user_id, limit=limit, offset=offset)
    return [_doc_to_out(d) for d in items]


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_file(doc_id: uuid.UUID, current: CurrentUserDep, session: SessionDep) -> DocumentOut:
    user_id, _ = current
    d = await DocumentRepo(session).get(doc_id, user_id=user_id)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return _doc_to_out(d)


@router.get("/{doc_id}/events")
async def file_events(
    doc_id: uuid.UUID, current: CurrentUserDep, session: SessionDep
) -> EventSourceResponse:
    """SSE: subscribe Redis Pub/Sub channel của doc, stream cho client."""
    user_id, _ = current
    d = await DocumentRepo(session).get(doc_id, user_id=user_id)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    async def gen() -> AsyncIterator[dict]:
        r = get_redis()
        pubsub = r.pubsub()
        await pubsub.subscribe(f"doc:{doc_id}:events")
        # gửi state hiện tại trước
        yield {"event": "status", "data": orjson.dumps({"status": d.status}).decode()}
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    payload = orjson.loads(msg["data"])
                except Exception as e:
                    log.debug("sse_bad_payload_skipped", error=str(e))
                    continue
                yield {
                    "event": payload.get("event", "message"),
                    "data": orjson.dumps(payload.get("data", {})).decode(),
                }
                if payload.get("data", {}).get("status") in ("indexed", "failed"):
                    break
        finally:
            await pubsub.unsubscribe(f"doc:{doc_id}:events")
            await pubsub.aclose()

    return EventSourceResponse(gen(), ping=15)
