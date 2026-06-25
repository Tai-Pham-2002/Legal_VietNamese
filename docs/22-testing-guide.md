# 22 — Hướng dẫn Test

Tài liệu này mô tả cách chạy và viết test cho hệ thống. Bộ test gồm **214 unit test
hermetic** (mock toàn bộ IO, không cần hạ tầng/mạng) + **live test** gọi API thật
(Cohere/Gemini) + chỗ đặt **integration test** cần hạ tầng.

---

## 1. Triết lý test

| Loại | Marker | Cần gì | Mặc định chạy? |
|------|--------|--------|----------------|
| **Unit** | (không) | Chỉ Python + venv. Mock hết redis/qdrant/minio/postgres/openai/cohere | ✅ Có |
| **Live** | `@pytest.mark.live` | Key Cohere + Gemini thật + mạng | ❌ Bỏ qua |
| **Integration** | `@pytest.mark.integration` | Postgres/Redis/Qdrant/MinIO thật (docker) | ❌ Bỏ qua |

Cấu hình ở `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"                 # async test không cần decorator
pythonpath = ["."]                    # để import `src.` được
testpaths = ["tests"]
addopts = ["-q", "--strict-markers", "-m", "not live and not integration"]
markers = [
    "live: gọi API thật (Cohere/Gemini) — cần key + mạng",
    "integration: cần hạ tầng thật (postgres/redis/qdrant/minio)",
]
```

> Vì `addopts` đã lọc `not live and not integration`, lệnh `pytest` trần chỉ chạy
> unit test → nhanh, hermetic, an toàn cho CI.

---

## 2. Chuẩn bị môi trường

Yêu cầu: **Python 3.12**, [`uv`](https://github.com/astral-sh/uv).

```bash
cd Legal_VietNamese

# Tạo venv + cài deps của project
uv venv --python 3.12
uv pip install -e .

# Cài deps dev (pytest...) — hoặc: uv pip install --group dev
uv pip install pytest pytest-asyncio respx
```

> **Quan trọng — thư mục chạy:** mọi lệnh test chạy từ thư mục `Legal_VietNamese/`
> (nơi có `pyproject.toml`). Module gốc là `src` (vd `from src.core.security import …`),
> đúng với cách Docker chạy (`gunicorn src.api.main:app`).

### `.env` cho test

- **Unit test KHÔNG cần `.env`.** Nếu thiếu, `tests/conftest.py` tự cấp giá trị giả
  (`SECRET_KEY`, `LLM_API_KEY`, `COHERE_API_KEY`) để `get_settings()` load được.
- **Live test CẦN `.env` với key thật.** `conftest.py` chỉ cấp giá trị giả cho key
  **không có** trong `.env` (tránh đè key thật — pydantic ưu tiên biến môi trường hơn
  `.env`). Copy mẫu rồi điền:

```bash
cp .env.example .env
# Điền: LLM_API_KEY (Gemini), COHERE_API_KEY, SECRET_KEY (>=32 ký tự)
```

---

## 3. Chạy test

```bash
# Toàn bộ unit test (mặc định — hermetic, ~5–7s)
.venv/bin/python -m pytest

# Một file / một test
.venv/bin/python -m pytest tests/test_rerank.py
.venv/bin/python -m pytest tests/test_rerank.py::test_cohere_rerank_maps_index_and_orders

# Verbose + lọc theo tên
.venv/bin/python -m pytest -v -k "rerank or chunker"

# Live test (gọi Cohere + Gemini THẬT — cần .env có key, tốn quota)
.venv/bin/python -m pytest -m live

# Integration test (cần hạ tầng — xem mục 5)
.venv/bin/python -m pytest -m integration

# Chạy TẤT CẢ (unit + live + integration)
.venv/bin/python -m pytest -m ""
```

Qua `Makefile` (chạy bằng `uv run`):

```bash
make test        # uv run pytest -q   (unit)
make lint        # ruff check
make typecheck   # mypy src
make ci          # lint + typecheck + test
```

---

## 4. Bản đồ test (test nào cho module nào)

| File test | Module nguồn | Trọng tâm |
|-----------|--------------|-----------|
| `test_security.py` | `core/security.py` | hash/verify mật khẩu (argon2), JWT issue/decode, token sai/giả/sai-secret |
| `test_settings.py` | `core/settings.py` | `sync_dsn`, `cohere_enabled`, `is_configured`, cache `get_settings` |
| `test_redis_keys.py` | `core/redis.py` | `make_key` deterministic, sort-keys, đa kiểu input |
| `test_llm_client.py` | `llm/client.py` | LRU cache, `complete` cache L1/L2, `embed` thứ tự + `dimensions` + batching |
| `test_rate_limit.py` | `cache/rate_limit.py` | sliding-window cho đúng `limit` request, rollback khi vượt |
| `test_rerank.py` | `retrieval/rerank.py` | map index Cohere, ordering, dispatcher + **fallback LLM**, fallback dense |
| `test_search.py` | `retrieval/search.py` | dựng `SearchHit` từ payload, filter tenant/user/doc |
| `test_pipeline.py` | `retrieval/pipeline.py` | `rewrite_query`, dedup theo point_id giữ max score, map `RetrievedChunk` |
| `test_chunker.py` | `ingestion/chunkers/base.py` | tách theo Điều, **page range per-chunk**, strip marker |
| `test_parsers.py` | `ingestion/parsers/*` | parse text/md/pdf/docx → markdown + `## [Page N]` |
| `test_indexer.py` | `ingestion/indexer.py` | embed + upsert Qdrant, batching, delete-trước-reindex |
| `test_memory.py` | `memory/short_term.py`, `long_term.py` | buffer Redis (order/trim/TTL), save/retrieve fact + dedupe |
| `test_prompts.py` | `agent/prompts.py` | `format_context`/`format_facts`/`build_answer_messages` |
| `test_langfuse.py` | `observability/langfuse.py` | `observe` **quyết định lazy tại call-time**, no-op an toàn |
| `test_agent_nodes.py` | `agent/nodes/*`, `graph.py` | node retrieve/memory/generate, `build_graph()` compile |
| `test_schemas.py` | `api/schemas/*` | validation pydantic (email, ràng buộc độ dài, default) |
| `test_middleware.py` | `api/middleware/request_context.py` | set `X-Request-ID`, **không UnboundLocalError khi route raise** |
| `test_health_route.py` | `api/routes/health.py` | liveness, readiness 200 vs **503 khi degraded** |
| `test_repositories.py` | `db/repositories/*` | lowercase email, order message, truncate, mapping (mock `AsyncSession`) |
| `test_worker.py` | `worker/tasks/*`, `main.py` | `process_document` chuyển trạng thái, `extract_facts`, `WorkerSettings` |
| `test_live_integrations.py` | (live) | Cohere rerank-v3.5 + Gemini embedding 768d + Gemini chat **THẬT** |

---

## 5. Integration test (cần hạ tầng)

Một số phần (repository với DB thật, route end-to-end, ingestion full) chỉ kiểm chứng
trọn vẹn khi có hạ tầng. Khởi động bằng docker-compose rồi chạy marker `integration`:

```bash
make up                         # postgres + redis + qdrant + minio + langfuse
make migrate                    # chạy Alembic
.venv/bin/python -m pytest -m integration
make down                       # dừng (giữ volume)
```

Hiện có 1 chỗ đặt sẵn (`test_repositories.py`, đánh dấu `integration`, skip mặc định)
làm khung để viết test DB round-trip thật. Khi thêm integration test:
- Dùng DB/Redis/Qdrant test riêng (đừng đụng dữ liệu thật).
- Cân nhắc `testcontainers` (đã có trong dev deps) để tự spin-up Postgres/Redis trong test.

---

## 6. Viết test mới

Quy ước để test luôn **hermetic** và **deterministic**:

1. **Async test:** chỉ cần `async def test_...` (đã bật `asyncio_mode=auto`).
2. **Mock ở ranh giới IO**, dùng `monkeypatch` + `unittest.mock.AsyncMock`:
   - Redis: patch `get_redis` / `cache_get` / `cache_set`.
   - Qdrant: patch `get_qdrant`. Embedder/LLM: patch `get_embedder`/`get_llm` hoặc
     truyền client giả vào constructor (`LLMClient(client=fake)`).
   - Cohere: patch `src.retrieval.rerank._get_cohere`.
   - DB: patch `session_scope` hoặc tạo `AsyncMock()` cho `AsyncSession`.
3. **Fixtures dùng chung** (`tests/conftest.py`):
   - `_reset_singletons` (autouse): tự xoá cache `get_settings` + reset mọi singleton
     client trước MỖI test → không rò state.
   - `settings`: trả `Settings` tươi.
4. **Factory** (`tests/factories.py`): `search_hit(...)` tạo `SearchHit` mẫu nhanh.
5. Test cần API thật → `pytestmark = pytest.mark.live` + tự `pytest.skip` nếu thiếu key.
   Test cần hạ tầng → `@pytest.mark.integration`.

Ví dụ tối giản (mock Cohere):

```python
from types import SimpleNamespace
from src.retrieval import rerank as R
from tests.factories import search_hit

async def test_my_rerank(monkeypatch):
    fake = SimpleNamespace()
    async def _rerank(*, model, query, documents, top_n):
        return SimpleNamespace(results=[SimpleNamespace(index=0, relevance_score=0.9)])
    fake.rerank = _rerank
    monkeypatch.setattr(R, "_get_cohere", lambda: fake)

    out = await R.cohere_rerank("q", [search_hit("a")], top_k=1, use_cache=False)
    assert out[0].hit.point_id == "a"
```

---

## 7. Khắc phục sự cố

| Triệu chứng | Nguyên nhân / cách xử lý |
|-------------|--------------------------|
| `ModuleNotFoundError: src` | Chạy không đúng thư mục — phải ở `Legal_VietNamese/`. |
| `No module named pytest` | Chưa cài dev deps: `uv pip install pytest pytest-asyncio respx`. |
| Live test bị **skip** | `.env` chưa có key thật (đang dùng giá trị giả). Điền `COHERE_API_KEY`/`LLM_API_KEY`. |
| Live test **fail 404 embedding** | Dùng `gemini-embedding-001` (model `text-embedding-004` đã bị Gemini gỡ). |
| `'live' not found in markers` | Marker chưa đăng ký — đã khai trong `pyproject.toml [tool.pytest.ini_options].markers`. |
| Unit test gọi mạng thật | Thiếu mock một ranh giới IO. Kiểm tra patch `get_*` / `cache_*` đúng đường dẫn module **nơi nó được dùng**. |

---

## 8. Tóm tắt lệnh hay dùng

```bash
.venv/bin/python -m pytest              # unit (mặc định)
.venv/bin/python -m pytest -v           # unit, chi tiết
.venv/bin/python -m pytest -m live      # gọi Cohere/Gemini thật
.venv/bin/python -m pytest -m integration  # cần docker-compose up
.venv/bin/python -m pytest -m ""        # chạy tất cả
.venv/bin/python -m pytest -k <pattern> # lọc theo tên
```
