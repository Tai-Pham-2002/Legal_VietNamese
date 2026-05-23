# Production Agentic RAG

Hệ thống RAG agentic chuẩn production — multi-turn chat, file ingestion, hybrid
retrieval, memory ngắn/dài hạn, observability đầy đủ. **Tự host 100%** trừ LLM
(dùng Gemini hoặc bất kỳ endpoint OpenAI-compatible).

> **Đọc trước**: bộ docs trong [`docs/`](docs/) giải thích **vì sao** mỗi quyết
> định lại như vậy. Tài liệu là first-class citizen của repo này.

---

## Tính năng

- **Multi-user / multi-tenant** với JWT + RLS-style filter ở mọi layer.
- **Multi-turn** chat streaming SSE, có **short-term buffer** (Redis) + **long-term
  facts** (Postgres + Qdrant).
- **Upload nhiều file cùng lúc** (PDF/DOCX/MD/TXT). Worker bất đồng bộ parse → chunk
  → embed → index, progress stream realtime qua SSE.
- **Agentic retrieval**: LangGraph (load_memory → retrieve → rerank → generate)
  với checkpointer Postgres.
- **Hybrid friendly**: hiện dense (Gemini Embedding), kiến trúc Qdrant đã sẵn sàng
  thêm sparse khi cần.
- **LLM reranker** dùng Gemini Flash, cache aggressive 2 tầng (in-memory LRU + Redis).
- **Observability**: Langfuse traces (LLM-aware), Prometheus `/metrics`, structlog JSON.
- **Production basics**: rate limit per-user, idempotent jobs, healthcheck,
  graceful shutdown, dedupe file theo SHA-256.

---

## Stack

| Layer | Tech | Vì sao |
|-------|------|--------|
| API | FastAPI + Uvicorn + Gunicorn | Async, OpenAPI native, SSE tốt |
| Worker | ARQ (Redis-backed) | Async-native, không cần broker thứ 2 |
| Agent | LangGraph + Postgres checkpointer | Stateful, resumable |
| LLM | OpenAI SDK → Gemini OpenAI-compat | Đổi provider 1 dòng |
| Embedding | `text-embedding-004` qua Gemini | Cùng provider, cache 7d |
| Vector DB | Qdrant | Self-host nhẹ, multi-tenancy bằng payload filter |
| Relational | Postgres 16 | App + LangGraph checkpoint + Langfuse meta |
| KV / queue | Redis 7 | Cache + pub/sub + ARQ + rate limit |
| Object store | MinIO | S3-compat, dễ migrate |
| Observability | Langfuse v3 + ClickHouse | Trace LLM end-to-end |
| Edge | Nginx | LB + SSE buffer-off |

Chi tiết so sánh alternatives: [`docs/03-tech-decisions.md`](docs/03-tech-decisions.md).

---

## Cấu trúc thư mục

```
production_rag/
├── docs/                          # 7 tài liệu kiến trúc (đọc trước khi sửa code)
├── docker/                        # Dockerfile, entrypoint, nginx/postgres init
├── docker-compose.yml             # Stack 1 file
├── pyproject.toml                 # uv-managed deps + ruff + mypy + pytest
├── alembic.ini
├── Makefile                       # make help
├── .env.example                   # Template — KHÔNG commit .env
└── src/
    ├── core/                      # settings, logging, db, redis, qdrant, minio, security
    ├── llm/                       # OpenAI-compat client + embedding + cache
    ├── cache/                     # Rate limiter
    ├── observability/             # Langfuse wrapper
    ├── db/
    │   ├── models/                # SQLAlchemy
    │   ├── repositories/          # Repository pattern
    │   └── migrations/            # Alembic
    ├── ingestion/                 # parsers + chunkers + indexer
    ├── retrieval/                 # search + LLM rerank + pipeline
    ├── memory/                    # short_term (Redis) + long_term (Postgres+Qdrant)
    ├── agent/                     # LangGraph: state, prompts, nodes, graph
    ├── api/                       # FastAPI app + routes + deps + middleware
    ├── worker/                    # ARQ entry + tasks
    └── scripts/                   # migrate, future eval/bootstrap
```

---

## Quick start (60 giây sau khi clone)

```bash
make env           # tạo .env từ template
# -> mở .env, điền LLM_API_KEY (Gemini) + đổi mọi password mặc định
make up            # build + start docker stack
curl http://localhost/health/ready | jq
```

Mở Langfuse `http://localhost:3000` → tạo project → copy keys vào `.env` →
`make restart SERVICE=api worker`.

Demo flow đầy đủ (đăng ký → login → upload → chat) trong
[`docs/04-deployment.md`](docs/04-deployment.md).

---

## Roadmap kiến trúc (đã build)

```
                 ┌────── Nginx (LB, SSE-friendly) ──────┐
                 │                                       │
                 ▼                                       ▼
            FastAPI replica 1                       FastAPI replica N
                 │                                       │
                 ├── Postgres (app+checkpointer+LF)      │
                 ├── Redis (cache+queue+pubsub+buffer)   │
                 ├── Qdrant (docs + memory collections)  │
                 ├── MinIO (raw files + parsed md)       │
                 ├── Langfuse (traces)                   │
                 │                                       │
                 └─►  ARQ workers (parse, embed, index, memory-extract)
```

Phase tương lai (chưa build, ưu tiên):
- [ ] Hybrid sparse + dense (BM25 / SPLADE).
- [ ] Multi-doc cross-encoder rerank.
- [ ] Streaming citations highlight (cùng token).
- [ ] User-uploaded "private collections".
- [ ] Auto re-ingest khi file gốc đổi.

---

## Đọc thêm

**Foundations** — đọc trước khi sửa code:
0. [`docs/00-run-guide.md`](docs/00-run-guide.md) — hướng dẫn chạy chi tiết: điền `.env`, khi `make up` hệ thống khởi tạo gì, troubleshoot
1. [`docs/01-architecture-overview.md`](docs/01-architecture-overview.md) — tổng quan + vì sao tách 3 tier
2. [`docs/02-data-flow.md`](docs/02-data-flow.md) — sequence diagram upload + chat + memory
3. [`docs/03-tech-decisions.md`](docs/03-tech-decisions.md) — bảng compare alternatives
4. [`docs/04-deployment.md`](docs/04-deployment.md) — quickstart + scale + backup
5. [`docs/05-api-reference.md`](docs/05-api-reference.md) — endpoint + schema
6. [`docs/06-prompts.md`](docs/06-prompts.md) — lý do từng prompt template
7. [`docs/07-evaluation.md`](docs/07-evaluation.md) — chiến lược eval với RAGAS + Langfuse

**Deep dives** — chi tiết kỹ thuật + diagrams:
8. [`docs/08-data-lifecycle.md`](docs/08-data-lifecycle.md) — byte upload → vector searchable, mỗi store làm gì
9. [`docs/09-concurrency-model.md`](docs/09-concurrency-model.md) — 100 user cùng lúc xử lý thế nào, bottleneck ở đâu
10. [`docs/10-caching-deep-dive.md`](docs/10-caching-deep-dive.md) — 5 tầng cache, hit rate, stampede, invalidation
11. [`docs/11-multi-turn-memory.md`](docs/11-multi-turn-memory.md) — short-term/long-term/summary phối hợp
12. [`docs/12-vector-search-internals.md`](docs/12-vector-search-internals.md) — Qdrant HNSW, hybrid, multi-tenancy
13. [`docs/13-database-design.md`](docs/13-database-design.md) — ER, transactions, RLS, isolation
14. [`docs/14-queue-and-workers.md`](docs/14-queue-and-workers.md) — ARQ lifecycle, idempotency, retry, DLQ
15. [`docs/15-streaming-sse.md`](docs/15-streaming-sse.md) — SSE vs WS, Nginx tuning, backpressure, reconnect
16. [`docs/16-security-deep.md`](docs/16-security-deep.md) — JWT, refresh rotation, 5-layer tenant enforcement
17. [`docs/17-failure-modes.md`](docs/17-failure-modes.md) — mỗi component down xử lý ra sao + runbook

---

## License

MIT.
