# 02 — Data Flow chi tiết

Tài liệu này mô tả **từng bước** của các flow chính, kèm component liên quan và format dữ liệu.

---

## A. Flow: User upload files

### A.1. Trình tự (Sequence)
```
Client            API                 Postgres        Redis(queue)    MinIO         Worker        Qdrant
  │                │                       │              │             │             │             │
  │POST /v1/files  │                       │              │             │             │             │
  │ (multipart, N) │                       │              │             │             │             │
  ├───────────────►│                       │              │             │             │             │
  │                │ validate (mime,size)  │              │             │             │             │
  │                │ for each file:        │              │             │             │             │
  │                ├──put_object──────────────────────────►             │             │             │
  │                ├──INSERT doc (pending)─►              │              │             │             │
  │                ├──enqueue parse_pdf───────────────────►              │             │             │
  │                │                       │              │             │             │             │
  │◄─202 {jobs}────┤                       │              │             │             │             │
  │                │                       │              │             │             │             │
  │                                                                      │             │             │
  │                                          poll job ◄──────────────────┤             │             │
  │                                                       get_object────►│             │             │
  │                                                                      │ bytes ─────►│             │
  │                                                                      │             │ parse PDF   │
  │                                                                      │             │ → markdown  │
  │                                                                      │             │             │
  │                                                       put markdown──►│             │             │
  │                                                                      │             │ chunk       │
  │                                                                      │             │ embed (Gemini│
  │                                                                      │             │  + L2 cache)│
  │                                                                      │             │             │
  │                                                                      │             │ upsert ────►│
  │                                                                      │             │             │
  │                                          UPDATE doc(indexed)◄────────┴─────────────┤             │
  │                                                                                    │             │
  │                                          PUBLISH doc.indexed (Redis Pub/Sub)◄──────┤             │
  │                                                                                                  │
  │GET /v1/files/{id}/status (SSE) ─────────────────────────────────────────────────────►            │
  │◄── event: status, data: indexed ─────────────────────────────────────────────────────            │
```

### A.2. Schema dữ liệu
**Bảng `documents`**:
```sql
id              uuid PK
tenant_id       uuid (FK users.tenant_id) -- multi-tenancy
user_id         uuid (FK users.id)
title           text
mime_type       text
size_bytes      bigint
status          text  -- pending|parsing|chunking|embedding|indexed|failed
n_chunks        int   -- null until indexed
storage_key     text  -- "tenants/{tenant}/docs/{doc_id}/raw.pdf"
markdown_key    text  -- "tenants/{tenant}/docs/{doc_id}/parsed.md"
checksum_sha256 text  -- để dedupe
error           text  -- nếu failed
created_at      timestamptz
indexed_at      timestamptz
```

**Qdrant point payload**:
```json
{
  "tenant_id": "uuid",
  "user_id": "uuid",
  "doc_id": "uuid",
  "chunk_id": "string",
  "doc_title": "string",
  "page": 12,
  "heading_path": "Chương II > Điều 5",
  "text": "...",
  "n_tokens": 412,
  "created_at": "2026-..."
}
```

**Tại sao có heading_path**: legal documents có cấu trúc Chương/Điều/Khoản. Chunker giữ path này → reranker và citation chính xác hơn.

### A.3. Chunking strategy (legal docs)
1. **Detect cấu trúc**: regex bắt `Điều \d+`, `Chương [IVX]+`, `Khoản \d+`.
2. **Split theo `Điều`** trước (semantic boundary tự nhiên của luật VN).
3. **Nếu 1 Điều > max_tokens (mặc định 800)**: split tiếp theo `Khoản`, rồi paragraph.
4. **Overlap 100 tokens** giữa các chunk liền kề để giữ context.
5. **Heading_path** đi kèm chunk: e.g. `"Luật X 2023 > Chương II > Điều 5 > Khoản 2"`.

---

## B. Flow: Chat multi-turn với streaming

### B.1. Trình tự
```
Client         API           Redis(buf)     Postgres      LangGraph      Qdrant      Gemini     Langfuse
  │             │                │             │              │             │            │            │
  │POST /chat   │                │             │              │             │            │            │
  │ {conv_id,   │                │             │              │             │            │            │
  │  message}   │                │             │              │             │            │            │
  ├────────────►│                │             │              │             │            │            │
  │             │ auth + load conv             │              │             │            │            │
  │             ├──GET conv──────────────────►│              │             │            │            │
  │             ├──LRANGE buf────►             │              │             │            │            │
  │             │ start Langfuse trace ────────────────────────────────────────────────────────────►│
  │             │                │             │              │             │            │            │
  │             ├── invoke graph ─────────────────────────────►              │            │            │
  │             │                │             │              │ Node: rewrite                          │
  │             │                │             │              ├─────────────────────────►│            │
  │             │                │             │              │◄────rewritten queries────┤            │
  │             │                │             │              │ Node: retrieve                         │
  │             │                │             │              ├──hybrid_search──────────►│            │
  │             │                │             │              │◄────top-50 chunks────────┤            │
  │             │                │             │              │ Node: retrieve_memory                  │
  │             │                │             │              ├──vector + facts query────►            │
  │             │                │             │              ├──SELECT facts────────────►            │
  │             │                │             │              │ Node: rerank (Gemini Flash)            │
  │             │                │             │              ├─────────────────────────────────────►│
  │             │                │             │              │◄──── top-5 chunks ranked ────────────┤
  │             │                │             │              │ Node: generate (stream)                │
  │             │                │             │              ├─────────────────────────────────────►│
  │             │ tokens ◄───────────────────────────────────│◄──token1                              │
  │             │ tokens ◄───────────────────────────────────│◄──token2                              │
  │◄SSE token1──┤                │             │              │   ...                                  │
  │◄SSE token2──┤                │             │              │                                        │
  │   ...       │                │             │              │                                        │
  │             ├─INSERT message (user)──────►│              │                                        │
  │             ├─INSERT message (assistant)─►│              │                                        │
  │             ├─RPUSH buf────►│             │              │                                        │
  │             ├─enqueue extract_facts ──── async ───────────────────────────────────────────────►(worker)
  │             │ flush Langfuse trace ─────────────────────────────────────────────────────────────►│
  │◄SSE done────┤                │             │              │                                        │
```

### B.2. State của LangGraph
```python
class AgentState(TypedDict):
    # Input
    user_message: str
    conversation_id: UUID
    user_id: UUID
    tenant_id: UUID

    # Conversation context
    short_term_history: list[Message]   # từ Redis buffer
    long_term_facts: list[Fact]          # facts được retrieve về user

    # Retrieval
    rewritten_queries: list[str]
    retrieved_chunks: list[Chunk]        # raw từ Qdrant
    reranked_chunks: list[Chunk]         # sau LLM rerank

    # Generation
    answer: str
    citations: list[Citation]

    # Control
    iteration: int                       # tránh loop vô hạn
    needs_more_info: bool
```

State được **checkpoint sau mỗi node** vào Postgres qua `langgraph.checkpoint.postgres`. Nếu crash giữa chừng → resume từ checkpoint cuối.

### B.3. Streaming details
- Client mở SSE connection → API giữ open, yield event:
  - `event: token` — tokens của câu trả lời (partial).
  - `event: citation` — citation kèm doc_id, chunk_id, page.
  - `event: tool_call` — báo agent đang gọi tool nào (UX hiển thị "Đang tìm trong tài liệu...").
  - `event: done` — kết thúc, kèm message_id và usage.
  - `event: error` — lỗi.
- Heartbeat 15s 1 lần (`event: ping`) để giữ kết nối + detect client disconnect.

### B.4. Cache tại đây
- **Query rewrite**: cache key = `sha256(model + user_message + short_term_summary_hash)`, TTL 10 phút.
- **Retrieve**: cache key = `sha256(query + tenant_id + filters)`, TTL 5 phút (vì doc có thể được add mới).
- **Rerank**: cache key = `sha256(query + chunk_ids_concat)`, TTL 10 phút.
- **Generate**: KHÔNG cache full response (vì có short-term history thay đổi liên tục), nhưng cache token-by-token cho cùng prompt deterministic temperature=0 chỉ áp dụng cho non-streaming.

---

## C. Flow: Long-term memory extraction

Sau mỗi cuộc hội thoại (trigger: idle 30s hoặc N=5 message), worker chạy:

```
Worker
  │
  ├─► Fetch last N messages từ Postgres
  ├─► LLM call: "Extract facts/preferences về user, format JSON"
  │      e.g. {"facts": [
  │              {"key":"user.role", "value":"luật sư", "confidence":0.9},
  │              {"key":"user.interest", "value":"luật doanh nghiệp"}
  │           ]}
  ├─► Deduplicate với facts hiện tại (vector similarity > 0.92 → skip)
  ├─► INSERT facts → Postgres (table `user_facts`)
  ├─► Embed mỗi fact mới → upsert Qdrant collection `memory`
  └─► Trace toàn bộ → Langfuse
```

**Bảng `user_facts`**:
```sql
id              uuid PK
user_id         uuid FK
key             text
value           text
confidence      float
source_msg_ids  uuid[] -- truy ngược message gốc
embedding_id    uuid   -- ref Qdrant point id
created_at      timestamptz
updated_at      timestamptz
```

Khi chat, agent có tool `retrieve_user_memory(query)` → vector search trên `memory` collection filter `user_id` → trả top 5 facts đính kèm system prompt.

---

## D. Flow: Cache lookup chi tiết

Mỗi LLM/embedding call đi qua `CachedLLMClient`:

```python
async def complete(self, prompt, **kw):
    key = hash_key("llm", self.model, prompt, kw)

    # L1: in-memory LRU
    if v := self.lru.get(key): return v

    # L2: Redis
    if raw := await redis.get(key):
        v = orjson.loads(raw)
        self.lru[key] = v
        return v

    # L3: upstream
    v = await upstream.complete(prompt, **kw)

    # write-back
    await redis.set(key, orjson.dumps(v), ex=TTL)
    self.lru[key] = v
    return v
```

**Khi nào KHÔNG cache?**
- `temperature > 0` (non-deterministic) → bypass cache.
- Streaming responses cho user (UX cần stream từ token đầu).
- Yêu cầu của user có `nocache=true` flag (debug).

---

## E. Flow: Rate limiting

Sliding window log trong Redis sorted-set, key = `rl:{user_id}:{endpoint}`:

```python
async def allow(user_id, endpoint, limit, window_sec):
    key = f"rl:{user_id}:{endpoint}"
    now = time.time()
    async with redis.pipeline() as p:
        p.zremrangebyscore(key, 0, now - window_sec)  # purge cũ
        p.zcard(key)
        p.zadd(key, {str(uuid4()): now})
        p.expire(key, window_sec)
        _, count, _, _ = await p.execute()
    return count < limit
```

- Chat: 60 req/phút/user.
- Upload: 10 file/phút/user, size tổng 200MB/giờ.
- Public API key (nếu có): 1000 req/phút/key.

---

## F. Failure modes & recovery

| Failure | Triệu chứng | Recovery |
|---------|------------|----------|
| Worker crash giữa parse | doc stuck `parsing` | Cron job sweep: `WHERE status='parsing' AND updated_at < now()-10m` → re-enqueue |
| Qdrant down | retrieve lỗi 503 | Circuit breaker mở 60s; agent fallback dùng memory + LLM raw |
| Gemini API down | generate lỗi | Retry với exponential backoff (max 3); nếu vẫn lỗi → trả message lỗi friendly |
| Redis down | cache miss + queue down | API trả 503 (worker không nhận job được); cache bypass tự động |
| Postgres down | toàn bộ hệ thống lỗi | Healthcheck báo not-ready → Nginx route sang replica khác (nếu có) |
