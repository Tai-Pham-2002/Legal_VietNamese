# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Production-grade **agentic RAG** system for Vietnamese legal documents: multi-turn streaming chat, async file ingestion (PDF/DOCX/MD/TXT), hybrid-ready vector retrieval with rerank, short/long-term memory, and full observability. Self-hosted except the LLM (Gemini via OpenAI-compatible API). The extensive `docs/` set explains the *why* behind decisions and is treated as first-class — read the relevant doc before changing a subsystem.

> Most code comments and docs are in Vietnamese. Match that style when editing existing files.

## Working directory

All commands run from `Legal_VietNamese/` (where `pyproject.toml` lives). The package root is `src` — imports use `from src.core... import ...`, matching how Docker runs (`gunicorn src.api.main:app`). Running from the parent dir gives `ModuleNotFoundError: src`.

## Commands

Tooling is `uv` (Python 3.12 only). Tests can run via `uv run` or the project venv directly.

```bash
# Quality (also: `make ci` = lint + typecheck + test)
make lint            # uv run ruff check src tests
make fmt             # uv run ruff format src tests
make typecheck       # uv run mypy src   (strict mode)
make test            # uv run pytest -q

# Tests — default run is UNIT ONLY (hermetic, all IO mocked, ~5-7s)
.venv/bin/python -m pytest                       # unit
.venv/bin/python -m pytest tests/test_rerank.py::test_name   # single test
.venv/bin/python -m pytest -k "rerank or chunker"            # filter by name
.venv/bin/python -m pytest -m live          # real Cohere/Gemini calls (needs keys + network)
.venv/bin/python -m pytest -m integration   # needs real infra (docker compose up first)
.venv/bin/python -m pytest -m ""            # everything

# Docker stack
make up              # build + start full stack; API at http://localhost
make migrate         # run Alembic migrations (entrypoint also auto-runs on api start)
make down            # stop, keep volumes
make down-clean      # stop + DELETE volumes (data loss)
make logs SERVICE=api / make shell SERVICE=worker / make psql / make redis-cli
```

`pytest`'s `addopts` filters `not live and not integration` by default, so `live`/`integration` markers must be requested explicitly. Unit tests need no `.env` — `tests/conftest.py` injects fake secrets; live tests need a real `.env`.

## Architecture

Three-tier, single Docker image whose role (`api` | `worker` | `migrate`) is chosen by `docker/entrypoint.sh`. Both API and worker share the same connection pools and bootstrap logic (ensure Qdrant collections + MinIO bucket on startup).

**Request → answer flow** (`src/agent/graph.py`): a LangGraph `load_memory → retrieve_docs → generate` pipeline with a **Postgres checkpointer** (resumable state). Two run modes exist and are deliberately split:
- `run_agent` (non-stream) — full graph, for batch/eval.
- `run_agent_stream` — runs the first two nodes through node functions directly, emits `tool_call`/`citations` SSE events, then **streams LLM tokens directly** (bypassing LangGraph) for UX. Edit this when changing streaming behavior, not the graph edges.

**Retrieval** (`src/retrieval/pipeline.py` → `retrieve_and_rerank`): query rewrite (1→≤3 sub-queries via LLM, cached) → parallel `vector_search` per sub-query → dedup by Qdrant point_id keeping max score → rerank top pool to top-K. Rerank (`rerank.py`) uses Cohere by default and **auto-falls back to LLM-as-reranker** on Cohere error. Only dense vectors today; `search.py`/Qdrant are structured to add sparse later.

**Memory** (`src/memory/`): short-term = Redis ring buffer (recent turns); long-term = extracted facts in Postgres + Qdrant. Facts are extracted asynchronously by the `extract_facts` worker job, not inline.

**Ingestion** is fully async via **ARQ** (`src/worker/`, Redis-backed, no separate broker). Upload enqueues `process_document`; worker parses (`ingestion/parsers/`) → chunks (`ingestion/chunkers/`, splits Vietnamese legal docs by Điều with per-chunk page ranges) → embeds → upserts to Qdrant (`ingestion/indexer.py`). Progress streams to the client over SSE. Files dedupe by SHA-256.

### Cross-cutting invariants

- **Multi-tenant isolation is enforced at every layer.** `vector_search` *requires* a `tenant_id` filter (plus optional `user_id`/`doc_ids`); repositories filter by tenant; never write a query path that omits these. See `docs/16-security-deep.md`.
- **Settings are nested + singleton** (`src/core/settings.py`, `get_settings()` is `lru_cache`d). Each group (`db`, `redis`, `qdrant`, `minio`, `llm`, `rerank`, `langfuse`, `security`) reads env via pydantic-settings. To change config, edit the relevant group; tests reset the cache per-test via the autouse `_reset_singletons` fixture.
- **LLM is provider-agnostic** through the OpenAI SDK pointed at Gemini's OpenAI-compat endpoint (`src/llm/client.py`). Embeddings use `gemini-embedding-001` truncated to 768 dims to match the Qdrant collection (`text-embedding-004` is retired). Two cache tiers: in-memory LRU + Redis.
- **`sync_dsn`** (computed in `DBSettings`) converts the async DSN to psycopg-sync — used only by Alembic and the LangGraph Postgres checkpointer; app code uses the async `postgres_dsn`.
- Migrations run on `api` container start (Alembic advisory lock means only one replica migrates; others wait).

## Testing conventions

Unit tests must be **hermetic and deterministic** — mock at the IO boundary by patching the accessor *where it is used* (`get_redis`/`cache_get`/`cache_set`, `get_qdrant`, `get_embedder`/`get_llm` or `LLMClient(client=fake)`, `src.retrieval.rerank._get_cohere`, `session_scope`). `tests/factories.py` provides `search_hit(...)`. `asyncio_mode=auto` so `async def test_...` needs no decorator. Mark real-API tests `@pytest.mark.live` (self-skip if keys absent) and infra tests `@pytest.mark.integration`. See `docs/22-testing-guide.md` for the full test→module map.
