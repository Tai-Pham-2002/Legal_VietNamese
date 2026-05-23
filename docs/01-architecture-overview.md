# 01 — Architecture Overview

Tài liệu này mô tả kiến trúc tổng thể của hệ thống Agentic RAG production và **giải thích lý do của từng lựa chọn**. Đọc theo thứ tự: vấn đề → giải pháp → trade-off.

---

## 1. Mục tiêu & Yêu cầu phi chức năng (NFR)

| # | Yêu cầu | Mục tiêu |
|---|---------|----------|
| 1 | Concurrent users | 100+ user dùng song song không nghẽn |
| 2 | Multi-turn chat | Giữ ngữ cảnh hội thoại + memory dài hạn |
| 3 | File upload | 1 hoặc nhiều file/lần; xử lý bất đồng bộ |
| 4 | Latency | First token < 2s ở p95 |
| 5 | Reliability | Hỏng 1 component không sập cả hệ |
| 6 | Observability | Trace được toàn bộ flow của 1 request |
| 7 | Cost | Cache aggressive, tránh gọi LLM/embedding thừa |
| 8 | Self-host | Mọi storage/observability tự host được |

---

## 2. High-Level Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              CLIENTS (web/CLI)              │
                    └──────────────────┬──────────────────────────┘
                                       │ HTTPS / SSE
                                       ▼
                    ┌─────────────────────────────────────────────┐
                    │     NGINX (Reverse Proxy + Load Balancer)   │
                    │     • TLS termination                       │
                    │     • Round-robin → API replicas            │
                    │     • Rate-limit cơ bản                     │
                    └──────────────────┬──────────────────────────┘
                                       │
                       ┌───────────────┼───────────────┐
                       ▼               ▼               ▼
                  ┌─────────┐     ┌─────────┐     ┌─────────┐
                  │  API-1  │     │  API-2  │ ... │  API-N  │   ← FastAPI (stateless)
                  │ (Uvicorn│     │         │     │         │     • Auth (JWT)
                  │  +Gunic)│     │         │     │         │     • Routing
                  └────┬────┘     └────┬────┘     └────┬────┘     • Streaming SSE
                       │               │               │            • Enqueue jobs
                       └───────────────┼───────────────┘
                                       │
       ┌───────────────────────────────┼──────────────────────────────┐
       │                               │                              │
       ▼                               ▼                              ▼
┌─────────────┐                ┌──────────────┐              ┌────────────────┐
│   REDIS     │                │   POSTGRES   │              │     QDRANT     │
│ • Cache LLM │                │ • Users      │              │ • Doc vectors  │
│ • Sessions  │                │ • Conv/Msgs  │              │ • Memory vec   │
│ • Short-mem │                │ • Documents  │              │ • Hybrid (dense│
│ • Queue     │◀──ARQ jobs─────│ • Long-mem   │              │   + sparse)    │
│ • Pub/Sub   │                │ • Langfuse   │              │                │
│ • Rate-lim  │                │   metadata   │              │                │
└─────────────┘                └──────────────┘              └────────────────┘
       ▲                                                              ▲
       │ pop job                                                      │ index
       │                                                              │
┌──────┴────────────────────────────────────────────────────────────┐│
│                       WORKERS (ARQ pool, N replicas)              ││
│  • parse_pdf (PyMuPDF + pypdf)                                    ││
│  • chunk_document (semantic + token-aware)                        ││
│  • embed_chunks → Gemini Embedding API                            ││
│  • index_to_qdrant ──────────────────────────────────────────────►│
│  • extract_long_term_memory (post-conversation)                   │
│  • langfuse_flush                                                 │
└───────────────────────────────────────────────────────────────────┘
       │                              ▲
       │ download/upload file         │
       ▼                              │
┌─────────────┐                ┌──────┴───────┐
│   MINIO     │                │   LANGFUSE   │
│ • Raw files │                │ • Traces     │
│ • Processed │                │ • Generations│
│   markdown  │                │ • Sessions   │
└─────────────┘                │ • Eval scores│
                               └──────────────┘
```

---

## 3. Phân tách trách nhiệm (Separation of Concerns)

Hệ thống được tách thành 3 process types để mỗi loại scale độc lập:

### 3.1. API tier (synchronous, light)
- **Trách nhiệm**: Nhận HTTP/SSE, authenticate, validate, **không** làm việc nặng.
- **Việc nặng** (parse PDF, embed batch, agent loop dài) → enqueue vào Redis queue, worker xử lý.
- **Tại sao tách**: Một request parse PDF lớn có thể chiếm 30s. Nếu chạy trong API thread → blocks event loop → các request khác chờ. Tách worker = API luôn responsive.

### 3.2. Agent tier (LangGraph)
- Chạy **trong cùng process API** vì agent loop cần streaming token real-time về client qua SSE. Đẩy sang worker khác sẽ phá streaming UX.
- LangGraph có **checkpointer** (Postgres) → state agent persist được, có thể resume nếu crash.

### 3.3. Worker tier (async background jobs)
- **ARQ** (Asynchronous Redis Queue) thay vì Celery:
  - **Tại sao ARQ?** Async-native (asyncio), nhẹ, dùng Redis có sẵn, không cần thêm broker (RabbitMQ).
  - Celery dùng được nhưng nặng hơn, design sync-first, async support qua eventlet/gevent vẫn awkward.
- Worker poll job từ Redis stream, xử lý, ghi kết quả về Postgres + publish event qua Redis Pub/Sub để API stream progress về client.

---

## 4. Tại sao chọn từng component

### 4.1. FastAPI + Uvicorn (workers) + Gunicorn (process manager)
- **FastAPI**: async-native, OpenAPI tự sinh, Pydantic v2 validation, hỗ trợ SSE/WebSocket tốt.
- **Uvicorn**: ASGI server, hỗ trợ HTTP/2 và streaming.
- **Gunicorn**: spawn `N` Uvicorn workers (process), tận dụng nhiều CPU. Trong container thì Uvicorn-only cũng OK; tách process giúp isolation lỗi.
- **Trade-off**: Có thể dùng `granian` (Rust-based, nhanh hơn) nhưng Uvicorn maturity cao hơn.

### 4.2. PostgreSQL (self-host)
- Một DB duy nhất cho:
  - Relational data (users, conversations, messages, documents, jobs).
  - LangGraph **checkpointer** (state agent).
  - **Long-term memory** (facts đã trích xuất).
  - Langfuse metadata.
- **Tại sao 1 DB**: scale nhỏ (<100 user), 1 Postgres đủ. Sau scale lớn tách Langfuse ra DB riêng (Langfuse cần ClickHouse cho event log).
- **Tại sao Postgres, không MySQL**: JSONB, partial index, RLS (row-level security cho multi-tenant), pgvector extension nếu cần migrate khỏi Qdrant.

### 4.3. Redis (self-host)
Vai trò đa dụng — đây là backbone:
| Use case | Cấu trúc | TTL |
|----------|----------|-----|
| LLM response cache | String (key = hash(prompt+model)) | 1h |
| Embedding cache | String (key = hash(text+model)) | 7d |
| Conversation buffer (short-term memory) | List | session-based |
| Job queue (ARQ) | Stream | until consumed |
| Pub/Sub progress events | Channel | n/a |
| Rate limiting | Sorted set (sliding window) | window |
| Session/auth blacklist | Set | token expiry |

- **Tại sao Redis duy nhất**: tránh thêm RabbitMQ/Memcached. Redis 7+ đủ tin cậy cho mức scale này. Có persistence (AOF + RDB) để không mất hết cache khi restart.

### 4.4. Qdrant (Vector DB)
- **Hybrid search**: dense (Gemini embedding) + sparse (BM25-like via Qdrant's sparse vectors) → recall cao hơn dense thuần.
- **Multi-tenancy**: dùng **payload filter** trên field `tenant_id` / `user_id` thay vì tạo collection riêng → tiết kiệm memory, scale hơn nhiều.
- **Tại sao không pgvector**: ở scale <100k chunks pgvector OK, nhưng khi >1M chunks performance giảm rõ rệt; Qdrant giữ recall + latency tốt hơn nhờ HNSW + quantization.

### 4.5. MinIO (Object storage, S3-compatible)
- File gốc + markdown đã parse lưu ở MinIO, **không** lưu trong Postgres (BLOB phình DB).
- API trả về pre-signed URL nếu client cần download.
- **Tại sao**: tách storage khỏi DB, scale độc lập, restore DB nhẹ hơn.

### 4.6. Langfuse (Observability)
- Trace mỗi request: LLM call, retrieval, tool call, latency từng node của LangGraph.
- Tự host (cần Postgres + ClickHouse, ở compose chia sẻ Postgres để gọn).
- **Tại sao quan trọng**: agentic RAG có nhiều bước (rewrite query → retrieve → rerank → generate). Không có trace, debug rất khổ.

### 4.7. Nginx (Reverse proxy + LB)
- TLS termination (cert tự sinh hoặc Let's Encrypt qua certbot).
- Round-robin load-balance giữa các container API.
- Buffer-off cho SSE endpoint (`proxy_buffering off`) để stream không bị giữ.

---

## 5. Data Flow chính

### 5.1. Flow upload file
```
Client ──POST /v1/files (multipart)──► API
                                        │
                                        ├─► Upload raw → MinIO
                                        ├─► INSERT documents (status=pending) → Postgres
                                        ├─► ARQ.enqueue(parse_pdf, doc_id) → Redis
                                        └─► Return 202 {doc_id, status_url}
Worker  ◄──pop job─── Redis
   │
   ├─► Download file ◄── MinIO
   ├─► Parse PDF → markdown
   ├─► Upload markdown → MinIO
   ├─► Chunk (token-aware + heading-aware)
   ├─► Batch-embed via Gemini Embedding API (with cache lookup)
   ├─► Upsert vectors → Qdrant (payload: doc_id, chunk_id, tenant_id, text, page)
   ├─► UPDATE documents SET status=indexed, n_chunks=K → Postgres
   └─► Publish event "doc.indexed" → Redis Pub/Sub
Client (polling or SSE) ─► GET /v1/files/{doc_id} → "indexed"
```

### 5.2. Flow chat multi-turn (streaming)
```
Client ──POST /v1/chat (SSE)──► API
                                 │
                                 ├─► Auth + load conv → Postgres
                                 ├─► Load short-term buffer → Redis
                                 ├─► Init LangGraph agent với checkpoint
                                 │
                                 ▼
                       ┌────── LangGraph Agent Loop ──────┐
                       │  Node 1: query_rewrite (LLM)      │
                       │  Node 2: retrieve_docs (tool)     │
                       │       └► Qdrant hybrid search     │
                       │  Node 3: retrieve_memory (tool)   │
                       │       └► Qdrant memory + Postgres │
                       │  Node 4: rerank (LLM Gemini Flash)│
                       │  Node 5: generate (stream tokens) │
                       │       └► yield tokens → SSE       │
                       └───────────────────────────────────┘
                                 │
                                 ├─► Append message → Postgres
                                 ├─► Update short-term buffer → Redis
                                 ├─► ARQ.enqueue(extract_facts, conv_id) → background
                                 └─► Langfuse trace flushed
```

### 5.3. Cache hierarchy
Mỗi LLM call qua `LLMClient` đi qua 3 layer:
1. **L1 — In-memory LRU** (per-process): cho lookup cực nhanh, size nhỏ (~1k entries).
2. **L2 — Redis cache** (shared): key = `sha256(model + prompt + params)`, TTL 1h.
3. **L3 — Upstream** (Gemini API).

Embedding cache TTL dài hơn (7 ngày) vì text + model gần như deterministic.

---

## 6. Concurrency & Scaling Model

| Bottleneck tiềm năng | Cách giải |
|----------------------|-----------|
| API CPU-bound (JSON serialize, Pydantic) | Tăng số replica API + Gunicorn workers (~ 2*CPU+1) |
| Embedding API rate limit | Batch + cache + retry với exponential backoff |
| LLM rate limit | Per-user token bucket + queue overflow → 429 |
| Worker bottleneck | Tăng số ARQ worker; phân queue theo loại job (`ingestion`, `memory`, `eval`) |
| Postgres connection pool | PgBouncer (transaction pooling) trước Postgres |
| Qdrant query latency | HNSW params + payload index trên `tenant_id` |

Vì stateless (state ở Redis/Postgres/Qdrant), API và Worker scale **horizontal** chỉ bằng `docker compose up --scale api=4 --scale worker=3`.

---

## 7. Security & Multi-tenancy

- **Auth**: JWT (HS256) — access token 15 phút + refresh token 7 ngày, lưu hash refresh ở Postgres.
- **Tenant isolation**:
  - Mọi query Postgres filter `WHERE user_id = :uid` (hoặc `tenant_id` nếu có org).
  - Qdrant: payload filter `tenant_id = :tid` bắt buộc, có index để fast filter.
  - MinIO: prefix path `{tenant_id}/{doc_id}/...`.
- **Rate limit**: sliding window per `user_id` (chat 60 req/phút, upload 10 file/phút).
- **Input validation**: Pydantic v2 strict mode; size limit file (50MB default).
- **Secrets**: `.env` (dev), Docker secrets (prod).

---

## 8. Reliability patterns

- **Idempotent jobs**: mỗi job có `job_id` UUID + Postgres `unique constraint`. Worker check-and-skip nếu re-deliver.
- **Outbox pattern (lite)**: khi cần emit event sau commit DB → ghi vào table `outbox`, worker reads + publishes. Tránh "ghi DB OK nhưng event mất".
- **Circuit breaker** (Gemini API): nếu Gemini lỗi >5 lần/30s → mở circuit 60s, trả lỗi nhanh thay vì retry vô tận.
- **Graceful shutdown**: API + worker bắt SIGTERM, finish job in-flight rồi mới exit.
- **Healthcheck**: mỗi service có endpoint `/health/live` (process sống) và `/health/ready` (deps OK).

---

## 9. Tại sao agentic, không phải plain RAG?

Plain RAG (1 query → 1 retrieve → 1 generate) thất bại với:
- Câu hỏi phức tạp cần **multi-hop** (so sánh điều khoản 12 và 15 của Luật X).
- Câu hỏi cần **clarify** trước khi retrieve.
- Câu hỏi cần **multiple sources** (Luật A + Nghị định B).

Agent (LangGraph) giải bằng:
- **Query rewriting**: chuyển câu user thành 1-3 sub-queries optimize cho retrieval.
- **Tool loop**: agent quyết định gọi `retrieve_docs`, `retrieve_memory`, hoặc trả lời trực tiếp.
- **Self-reflection**: sau khi có context, đánh giá đủ thông tin chưa, nếu không thì retrieve thêm.
- **Stop condition**: max 5 iterations để tránh loop vô hạn.

Trade-off: latency cao hơn, token cost cao hơn → bù bằng cache + reranker rẻ.

---

## 10. Tham chiếu file

- `docs/02-data-flow.md` — chi tiết từng flow + sequence diagram.
- `docs/03-tech-decisions.md` — bảng so sánh chi tiết alternatives.
- `docs/04-deployment.md` — runbook deploy + scale.
- `docs/05-api-reference.md` — OpenAPI summary.
- `docs/06-prompts.md` — toàn bộ prompt templates.
- `docs/07-evaluation.md` — chiến lược eval (RAGAS + Langfuse).
