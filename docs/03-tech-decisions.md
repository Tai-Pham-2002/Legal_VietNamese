# 03 — Technology Decisions

Bảng so sánh alternatives + lý do chốt. Mỗi mục trả lời: **vấn đề là gì?**, **các lựa chọn?**, **chọn cái nào và tại sao?**.

---

## D1. Agent framework → LangGraph

| Tiêu chí | LangGraph | LlamaIndex Workflows | Pydantic AI | Custom |
|---------|-----------|---------------------|-------------|--------|
| Graph stateful native | ✅ | ✅ | ⚠️ | ❌ |
| Checkpointer Postgres | ✅ built-in | ⚠️ manual | ❌ | ❌ |
| Streaming token | ✅ | ✅ | ✅ | tự code |
| Conditional edges | ✅ | event-driven (tương đương) | ⚠️ | tự code |
| Ecosystem (tracing, eval) | ✅ Langfuse, LangSmith | ✅ | ⚠️ | tự code |
| Maturity | High | High | Medium | n/a |

**Chốt**: LangGraph — checkpointer + multi-agent ready + Langfuse integration tốt nhất hiện tại.

---

## D2. Vector DB → Qdrant

| Tiêu chí | Qdrant | Milvus | Weaviate | pgvector |
|---------|--------|--------|----------|----------|
| Self-host dễ | ✅ 1 container | ❌ etcd+pulsar+minio | ⚠️ medium | ✅ |
| Hybrid (dense+sparse) native | ✅ | ✅ | ✅ | ⚠️ thêm trigger |
| Multi-tenancy filter | ✅ payload index | ✅ partition | ✅ | ⚠️ index thường |
| Memory/CPU footprint | Low (Rust) | High | Medium | Low |
| Quantization | ✅ scalar/binary/PQ | ✅ | ⚠️ | ⚠️ |
| Snapshot/Backup | ✅ | ✅ | ✅ | DB backup |

**Chốt**: Qdrant — vừa nhẹ vừa đủ tính năng. pgvector chỉ phù hợp <100k chunks; Milvus overkill ở scale này.

---

## D3. LLM access → OpenAI-compatible client + Gemini endpoint

**Vấn đề**: muốn linh hoạt đổi provider, không lock-in Gemini SDK.

**Giải pháp**: Dùng `openai` Python SDK trỏ vào Gemini's OpenAI-compatible endpoint:
```
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
api_key  = GEMINI_API_KEY
model    = "gemini-2.5-pro" / "gemini-2.5-flash"
```

**Lợi ích**:
- Đổi sang provider khác (Together, Groq, OpenRouter, local vLLM) chỉ cần đổi `base_url`.
- Tool calling, structured output, streaming đều hoạt động qua OpenAI schema.
- Langfuse `@observe` decorator hỗ trợ OpenAI client native.

**Trade-off**: một vài tính năng Gemini-specific (file API, search grounding) không xài qua OpenAI compatible — phải gọi Gemini SDK trực tiếp khi cần (hiện chưa cần).

---

## D4. Task queue → ARQ (over Celery / Dramatiq / RQ)

| Tiêu chí | ARQ | Celery | Dramatiq | RQ |
|---------|-----|--------|----------|-----|
| Async native | ✅ | ⚠️ qua eventlet | ⚠️ | ❌ |
| Broker | Redis | Redis/RabbitMQ | Redis/RabbitMQ | Redis |
| Setup phức tạp | Thấp | Cao | Trung bình | Thấp |
| Scheduling | ✅ cron | ✅ beat | ❌ (thêm apscheduler) | ❌ |
| Monitoring UI | basic | Flower | basic | RQ-dashboard |
| Maturity | Medium | Rất cao | High | High |

**Chốt**: ARQ — codebase async toàn diện (FastAPI + httpx + langgraph), Celery sync-first sẽ tạo friction. Hệ thống nhỏ, không cần Celery tooling phức tạp.

---

## D5. ORM → SQLAlchemy 2 async + Alembic

- **SQLAlchemy 2**: API mới (`select()`, async session) gọn, type-hint tốt với Pydantic.
- **Alembic**: migration chuẩn de-facto cho SA.
- **Tại sao không Tortoise/SQLModel**:
  - Tortoise: ecosystem nhỏ hơn, ít tài liệu khi gặp edge case.
  - SQLModel: dùng SA bên dưới nhưng abstraction leak; async support trễ hơn SA.

---

## D6. Schema validation → Pydantic v2

- Speed (Rust core) gấp 5-10x v1.
- FastAPI 0.100+ tích hợp native.
- `model_config = ConfigDict(strict=True)` cho input từ ngoài.

---

## D7. Configuration → pydantic-settings + .env hierarchy

- Class `Settings(BaseSettings)` load từ env, validate.
- Tách `settings.api`, `settings.db`, `settings.redis`, `settings.qdrant`, `settings.llm` thành nested để rõ ràng.
- `.env.local` > `.env` (override dev local mà không commit).

---

## D8. Object storage → MinIO

- S3-compatible API → đổi sang AWS S3 production chỉ cần đổi endpoint.
- Self-host 1 container.
- Versioning + lifecycle policies built-in.

**Alternative**: lưu file thẳng trên volume → đơn giản nhưng không scale multi-node. Skip.

---

## D9. Observability → Langfuse + structlog

- **Langfuse**: trace LLM/RAG-specific (generation, retrieval, scores). Self-host được, có UI eval.
- **structlog**: structured JSON log → forward Loki/ELK sau dễ.
- **Prometheus metrics**: expose `/metrics` cho `prometheus_fastapi_instrumentator` — dù bây giờ chưa scrape, có sẵn endpoint.

**Tại sao không OpenTelemetry?** Có thể thêm sau. Langfuse phủ phần "AI-specific" mà OTel chưa có chuẩn. Langfuse mới đây cũng support OTel ingestion → tương lai unified.

---

## D10. Reverse proxy → Nginx (over Traefik)

- Nginx config file static, predictable. SSE buffer config dễ:
  ```nginx
  proxy_buffering off;
  proxy_cache off;
  proxy_read_timeout 1h;
  ```
- Traefik tốt khi auto-discovery (K8s) nhưng compose-only thì Nginx đơn giản hơn.

---

## D11. Auth → JWT (HS256) + refresh token

- Stateless access token 15 phút.
- Refresh token lưu hash trong Postgres → có thể revoke.
- Rotation (refresh trả token mới + invalidate cũ) tránh replay.
- **Tại sao không session cookie**: API có thể được mobile/CLI call → header `Authorization: Bearer` cleaner.

---

## D12. Embedding model → Gemini Embedding API

- User chốt dùng Gemini (cùng provider LLM).
- Model: `gemini-embedding-001` (3072 dim) hoặc `text-embedding-004` (768 dim).
- **Lưu ý**: Qdrant collection phải khớp dim. Chọn 768 (text-embedding-004) để index/query nhanh hơn, đủ cho legal docs.
- Cache embedding TTL 7 ngày (text+model gần như deterministic).

---

## D13. Reranker → LLM-as-reranker (Gemini Flash)

- User chốt.
- Prompt: đưa N candidate chunks + query → LLM trả về ranked list + score.
- **Trade-off**: chậm hơn cross-encoder ~3-5x, đắt hơn ~$0.001/query nhưng latency vẫn <1s với Flash.
- **Mitigation**: cache aggressive (TTL 10 phút), parallel với generate step nếu confidence trên top-1 dense > threshold thì skip rerank.

---

## D14. Chunking → Custom legal-aware

PDF luật VN có cấu trúc rõ. Generic recursive splitter làm mất ngữ nghĩa.
- **Strategy**: detect `Điều X`, `Khoản Y`, `Chương Z` → split theo boundary.
- **Fallback**: nếu detect không thành công (non-luật doc), dùng `langchain.text_splitter.RecursiveCharacterTextSplitter` với separator `["\n\n", "\n", ". ", " "]`.
- **Token-aware** dùng `tiktoken` (cl100k_base) ước lượng token (~OK với Gemini).

---

## D15. Memory → Hai tầng

| Tầng | Storage | Scope | TTL |
|------|---------|-------|-----|
| Short-term | Redis list `conv:{id}:buf` | 1 conversation | 24h sau message cuối |
| Long-term — facts | Postgres `user_facts` | User-wide | persistent |
| Long-term — vector | Qdrant `memory` collection | User-wide | persistent |

- Short-term cap 20 messages mới nhất, tóm tắt cũ hơn vào `summary` field của conversation.
- Long-term trích xuất qua LLM (worker), embed, dedupe.

---

## D16. Dependency management → uv (over pip-tools, poetry)

- `uv` cực nhanh (10-100x pip), lockfile reproducible, Python-version manager built-in.
- `pyproject.toml` chuẩn PEP 621.
- Mature đủ production-ready (2025+).

---

## D17. Testing → pytest + pytest-asyncio + testcontainers

- **pytest-asyncio**: async test cho FastAPI/agent.
- **testcontainers**: spin Postgres/Redis/Qdrant thật trong test → integration test sát production.
- **Mock LLM**: dùng `responses` hoặc `respx` để intercept HTTP, không gọi Gemini thật trong CI.

---

## D18. CI/CD → GitHub Actions (suggested, not committed)

- Pipeline: lint (ruff) → typecheck (mypy/pyright) → test (pytest) → build images → push registry.
- Production deploy tách stage (manual approval).
