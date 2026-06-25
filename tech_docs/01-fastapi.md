# FastAPI — Application Layer (API + Worker)

> The brain of the system. Every request enters here, every external service is
> orchestrated from here. In this stack FastAPI runs as the `api` container; the
> same image also runs as the `worker` container (ARQ), so this doc covers both.

---

## 1. What is its task?

FastAPI is the **application / orchestration layer** of the Vietnamese Legal
agentic-RAG system. Concretely it is responsible for:

- **HTTP API surface** — chat, file upload, health, auth. Served by
  `gunicorn src.api.main:app` with `uvicorn` workers, listening on port `8000`.
- **Agentic RAG orchestration** — a LangGraph pipeline
  `load_memory → retrieve_docs → generate` (`src/agent/graph.py`).
- **Streaming (SSE)** — `run_agent_stream` emits `tool_call` / `citations`
  events then streams LLM tokens token-by-token to the browser.
- **Multi-tenant enforcement** — every data path attaches a `tenant_id` filter
  before touching Postgres / Qdrant.
- **Async ingestion entrypoint** — upload endpoints enqueue jobs onto Redis
  (ARQ) that the `worker` role consumes.

FastAPI itself is **stateless** — all state lives in Postgres / Redis / Qdrant /
MinIO. That is what allows it to run as `API_REPLICAS=2` behind Nginx.

---

## 2. How does it work?

### Process model
```
gunicorn (master)
 └─ uvicorn worker × WEB_CONCURRENCY (=2)     ← async event loop each
      └─ asyncio tasks: request handlers, SSE generators
```
Async all the way down: `asyncpg` (Postgres), `redis.asyncio`, async Qdrant &
MinIO clients, and the OpenAI SDK (async) pointed at Gemini.

### Request → answer flow (`src/agent/graph.py`)
1. **load_memory** — pull short-term turns from Redis + long-term facts from
   Postgres/Qdrant.
2. **retrieve_docs** — `retrieve_and_rerank`: LLM query-rewrite (1→≤3
   sub-queries, cached) → parallel vector search in Qdrant per sub-query → dedup
   by point_id → rerank (Cohere, falls back to LLM-as-reranker).
3. **generate** — stream tokens from the LLM (Gemini via OpenAI-compatible API).

A **Postgres checkpointer** persists graph state so multi-turn conversations are
resumable.

### Configuration (singleton settings)
`src/core/settings.py` exposes a `lru_cache`d `get_settings()` with nested groups
(`db`, `redis`, `qdrant`, `minio`, `llm`, `rerank`, `langfuse`, `security`), each
reading env vars. Connection pools are created once at startup (lifespan) and
shared across requests; both `api` and `worker` run the same bootstrap (ensure
Qdrant collections + MinIO bucket exist).

### The `worker` role
Same Docker image, `command: ["worker"]`. It is an **ARQ** consumer (Redis-backed,
no separate broker) running jobs like `process_document` and `extract_facts`.
See [05-redis.md](05-redis.md) and the ingestion docs.

---

## 3. How does it communicate with the other services?

| Target | Protocol / Driver | DSN / Endpoint (compose) | Purpose |
|--------|-------------------|--------------------------|---------|
| **Nginx** | HTTP/1.1 (inbound) | `expose 8000` | Receives proxied client traffic; never exposed directly |
| **PostgreSQL** | TCP, `asyncpg` (app) / `psycopg` sync (checkpointer) | `postgresql+asyncpg://rag:***@postgres:5432/rag` | Tenants, conversations, messages, long-term facts, graph checkpoints |
| **Redis** | RESP, `redis.asyncio` | `redis://redis:6379/0` | Cache, short-term memory ring buffer, ARQ queue |
| **Qdrant** | HTTP REST `6333` (gRPC `6334`) | `http://qdrant:6333` + API key | Dense vector search & upsert (768-dim) |
| **MinIO** | S3 API | `minio:9000` | Store / fetch raw uploaded files (`rag-files` bucket) |
| **Langfuse** | HTTPS/SDK | `http://langfuse-web:3000` | Send traces / spans / generations |
| **LLM (Gemini)** | HTTPS, OpenAI SDK | `LLM_BASE_URL` (external) | Generation, query-rewrite, embeddings, LLM-rerank fallback |
| **Cohere** | HTTPS (external) | API key | Reranking (primary) |

### Diagram

```mermaid
flowchart TD
    NGINX["Nginx (edge)"] -->|HTTP :8000| API["FastAPI api<br/>(2 replicas)"]

    API -->|asyncpg| PG[("PostgreSQL")]
    API -->|RESP| REDIS[("Redis")]
    API -->|REST/gRPC| QDRANT[("Qdrant")]
    API -->|S3| MINIO[("MinIO")]
    API -->|SDK| LF["Langfuse"]
    API -->|OpenAI SDK| GEMINI(["Gemini LLM<br/>(external)"])
    API -->|HTTPS| COHERE(["Cohere rerank<br/>(external)"])

    API -.enqueue job.-> REDIS
    REDIS -.dequeue.-> WORKER["FastAPI worker<br/>(ARQ, 2 replicas)"]
    WORKER -->|parse/chunk/embed| QDRANT
    WORKER --> PG
    WORKER --> MINIO
    WORKER --> GEMINI
```

---

## 4. Operational notes & failure modes

- **Startup ordering:** `depends_on … condition: service_healthy` for postgres,
  redis, qdrant, minio — the API waits for data services to be healthy.
- **Migrations:** Alembic runs on `api` start; an advisory lock means only one
  replica migrates while the others wait.
- **Health:** `GET /health/ready` (used by Docker healthcheck) — verifies
  downstream connectivity. Nginx upstream marks the replica down after
  `max_fails=3`.
- **Rerank fallback:** Cohere error → automatic LLM-as-reranker, so a Cohere
  outage degrades quality but does not break chat.
- **Streaming:** tokens are flushed immediately; Nginx must have `proxy_buffering
  off` on chat/SSE routes (see [07-nginx.md](07-nginx.md)).
