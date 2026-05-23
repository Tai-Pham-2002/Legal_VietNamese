# 05 — API Reference

Base URL: `http://localhost` (qua Nginx) hoặc `http://api:8000` (trong network).
OpenAPI schema: `GET /docs` (Swagger UI).

Mọi endpoint trừ `/v1/auth/*` và `/health/*` đều cần header:
```
Authorization: Bearer <access_token>
```

---

## Auth

### POST /v1/auth/register
Body:
```json
{
  "email": "user@example.com",
  "password": "min8chars",
  "display_name": "Nguyễn Văn A",   // optional
  "tenant_slug": "default"           // optional, default = "default"
}
```
201 → `UserOut`.

### POST /v1/auth/login
Body: `{email, password, tenant_slug}` → 200 `{access_token, refresh_token, expires_in}`.

### POST /v1/auth/refresh
Body: `{refresh_token}` → 200 (rotate, refresh cũ bị revoke).

### POST /v1/auth/logout
Body: `{refresh_token}` → 204.

---

## Conversations

### POST /v1/conversations
Body: `{title?: string}` → 201 `ConversationOut`.

### GET /v1/conversations?limit=50&offset=0
→ 200 `ConversationOut[]`.

### GET /v1/conversations/{conv_id}/messages
→ 200 `MessageOut[]` (sort theo created_at asc).

### DELETE /v1/conversations/{conv_id}
→ 204. Archive (soft delete).

---

## Chat (SSE)

### POST /v1/chat/{conv_id}/messages
Body:
```json
{
  "message": "Điều 5 luật doanh nghiệp nói gì?",
  "doc_ids": ["uuid1", "uuid2"]    // optional: chỉ retrieve trong các doc này
}
```
Response: `text/event-stream`. Events:

| event | data | mô tả |
|-------|------|-------|
| `tool_call` | `{"name": "load_memory" \| "retrieve_docs"}` | Báo agent đang chạy node |
| `citations` | `Citation[]` | List citations sau retrieve |
| `token` | `"<partial text>"` | 1 mảnh token answer |
| `done` | `{user_message_id, citations, usage, latency_ms}` | Hoàn tất |
| `error` | `"<message>"` | Lỗi |
| `ping` | `""` | Heartbeat (15s/lần) |

Rate limit: 60 req/phút/user (HTTP 429 với `Retry-After`).

---

## Files

### POST /v1/files (multipart)
Body: 1 hoặc nhiều file qua field `files`. Max:
- 50 MB/file (tuỳ `MAX_UPLOAD_SIZE_MB`)
- 20 file/request (tuỳ `MAX_FILES_PER_REQUEST`)

Allowed mime: `application/pdf`, `text/plain`, `text/markdown`,
`application/vnd.openxmlformats-officedocument.wordprocessingml.document`.

Trả 202:
```json
{
  "documents": [DocumentOut, ...],
  "job_ids": ["arq_job_1", ...]
}
```

Dedupe: file giống checksum trong cùng tenant → trả document cũ, không re-ingest.

Rate limit: 10 file/phút/user.

### GET /v1/files?limit=50&offset=0
→ 200 `DocumentOut[]`.

### GET /v1/files/{doc_id}
→ 200 `DocumentOut`. Trường `status` ∈ `pending|parsing|chunking|embedding|indexed|failed`.

### GET /v1/files/{doc_id}/events  (SSE)
Stream progress events:
```
event: status
data: {"status": "parsing"}

event: status
data: {"status": "indexed", "n_chunks": 42}
```
Tự đóng connection khi `indexed` hoặc `failed`.

---

## Health

### GET /health/live
→ `{status: "ok"}`. Luôn 200 nếu process còn chạy.

### GET /health/ready
Check tất cả deps (Postgres/Redis/Qdrant/MinIO). 200 chỉ khi all OK.

---

## Schemas (Pydantic)

```jsonc
ConversationOut {
  "id": "uuid",
  "title": "string",
  "message_count": 0,
  "last_message_at": "datetime|null",
  "created_at": "datetime",
  "updated_at": "datetime"
}

MessageOut {
  "id": "uuid",
  "role": "user|assistant|tool|system",
  "content": "string",
  "meta": {"citations": [...] /* assistant only */},
  "tokens_in": 0,
  "tokens_out": 0,
  "created_at": "datetime"
}

DocumentOut {
  "id": "uuid",
  "title": "string",
  "mime_type": "string",
  "size_bytes": 0,
  "status": "indexed",
  "n_chunks": 0,
  "error": null,
  "created_at": "datetime",
  "indexed_at": "datetime|null"
}

Citation {
  "doc_id": "uuid",
  "chunk_id": "uuid",
  "doc_title": "string",
  "heading_path": "Chương II > Điều 5",
  "page_from": 12,
  "page_to": 12,
  "score": 0.92
}
```

---

## Errors

Format chuẩn FastAPI:
```json
{"detail": "human-readable message"}
```

Mã lỗi quan trọng:
- `401` token thiếu/sai/expired.
- `403` user inactive hoặc không có quyền.
- `404` không tìm thấy.
- `413` file quá lớn.
- `415` mime không hỗ trợ.
- `429` rate limit (có `Retry-After`).
- `503` deps down (qua /health/ready).
