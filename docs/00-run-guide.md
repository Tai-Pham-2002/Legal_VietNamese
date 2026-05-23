# 00 — Hướng dẫn chạy chi tiết (Run Guide)

Tài liệu này giải thích **mọi thứ xảy ra khi bạn `make up`**: từ điền `.env`, build
image, các container khởi tạo theo thứ tự nào, mỗi container làm gì, đến khi
nào hệ thống thực sự sẵn sàng nhận request.

Đọc tài liệu này nếu:
- Bạn lần đầu chạy project và muốn hiểu hệ thống đang dựng cái gì.
- Bạn gặp lỗi lúc startup và cần biết container nào phụ thuộc container nào.
- Bạn cần biết phải sửa biến môi trường nào, vì sao.

Tài liệu liên quan:
- [04-deployment.md](04-deployment.md) — quickstart ngắn gọn + backup/scale.
- [01-architecture-overview.md](01-architecture-overview.md) — tổng quan kiến trúc.
- [17-failure-modes.md](17-failure-modes.md) — runbook khi component lỗi.

---

## 1. Yêu cầu trước khi chạy

### 1.1. Host

| Resource | Min dev | Recommended prod |
|----------|---------|------------------|
| CPU      | 4 cores | 8+ cores         |
| RAM      | 8 GB    | 16+ GB           |
| Disk     | 20 GB SSD | 200+ GB SSD    |
| OS       | Linux / macOS | Ubuntu 22.04 LTS |
| Docker   | 24+ (Compose v2) | 24+        |

Không cần GPU vì LLM/Embedding gọi qua Gemini API.

### 1.2. Tools cần có trên máy host

| Tool | Dùng để | Bắt buộc? |
|------|---------|-----------|
| Docker Engine + Compose v2 | Chạy stack | ✅ |
| `make` | Shortcut command | ⛔ (có thể gõ `docker compose` trực tiếp) |
| `python3` | Sinh `SECRET_KEY` | ✅ (một lần) |
| `openssl` | Sinh secrets Langfuse | ✅ (một lần) |
| `jq` | Format JSON response | ⛔ (chỉ tiện hơn) |
| `curl` | Smoke test endpoint | ⛔ |
| `uv` ≥ 0.5.8 | Chạy test/lint dev (local) | ⛔ (chỉ khi dev) |

### 1.3. Tài khoản / API key cần xin trước

- **Gemini API key**: lấy ở [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey).
  Free tier đủ dùng cho dev.
- **Langfuse keys**: KHÔNG cần xin trước — sẽ tự tạo sau khi stack lên (xem
  [§5](#5-bootstrap-langfuse-sau-khi-stack-lên)).

---

## 2. Chuẩn bị file `.env`

`.env` là **nguồn duy nhất** của mọi biến môi trường truyền vào docker-compose.
Compose tự load file `.env` ở cùng cấp với `docker-compose.yml`. Mỗi biến trong
`.env` được tham chiếu trong [`docker-compose.yml`](../docker-compose.yml) dạng
`${VAR_NAME}` rồi inject vào container tương ứng.

### 2.1. Tạo file `.env`

```bash
make env
# tương đương: cp .env.example .env (nếu .env chưa tồn tại)
```

### 2.2. Sinh secret

Có 4 secret tối thiểu phải sinh mới, **không được dùng giá trị mặc định** trong
`.env.example`:

```bash
# SECRET_KEY — JWT signing key (app)
python3 -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(64))"

# 3 secret cho Langfuse (mỗi cái độc lập, KHÔNG reuse)
openssl rand -hex 32   # -> dán vào LANGFUSE_SALT
openssl rand -hex 32   # -> dán vào LANGFUSE_ENCRYPTION_KEY
openssl rand -hex 32   # -> dán vào LANGFUSE_NEXTAUTH_SECRET
```

Sửa luôn các password mặc định trong cùng file:
- `POSTGRES_PASSWORD`
- `MINIO_ROOT_PASSWORD`
- `CLICKHOUSE_PASSWORD`

> ⚠️ **Password phải URL-safe.** Compose ráp `POSTGRES_PASSWORD` thẳng vào DSN
> `postgresql://user:password@host/db`, nên **không được chứa**: `@ : / ? # [ ]`.
> Ví dụ `tantai@admin` sẽ vỡ parser → app báo `failed to resolve host 'admin@postgres'`.
> Dùng `_` hoặc `-` thay cho `@` (ví dụ `tantai_admin`).

> ⚠️ **`QDRANT_API_KEY` không được để rỗng.** Compose pass biến này thẳng vào
> Qdrant qua `QDRANT__SERVICE__API_KEY: ${QDRANT_API_KEY:-}`. Qdrant hiểu chuỗi
> rỗng là "bật auth với key rỗng" → client của app sẽ bị 401 ngay lúc khởi tạo
> collection. Set một giá trị bất kỳ (ví dụ `qdrant-dev-key-change-me`).

### 2.3. Bảng giải thích từng biến trong `.env`

Nhóm **App** — runtime API/worker.

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `ENV` | ⛔ | `dev` | `dev` / `staging` / `prod`. `prod` → log JSON, tắt debug. |
| `LOG_LEVEL` | ⛔ | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `SECRET_KEY` | ✅ | — | JWT HS256 signing key. **Phải ≥ 32 byte ngẫu nhiên.** Đổi key này = invalidate tất cả token đang sống. |

Nhóm **Postgres** — DB chính (app + LangGraph checkpointer + Langfuse meta).

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `POSTGRES_DB` | ⛔ | `rag` | DB cho app. |
| `POSTGRES_USER` | ⛔ | `rag` | Superuser của instance. |
| `POSTGRES_PASSWORD` | ✅ | — | **Đổi giá trị mặc định.** |
| `POSTGRES_PORT` | ⛔ | `5432` | Map ra host. Đổi nếu port đã bị chiếm. |
| `LANGFUSE_DB` | ⛔ | `langfuse` | DB phụ cho Langfuse, tạo tự động lúc init Postgres. |

Nhóm **Redis** — cache + queue + pub/sub + buffer.

| Biến              | Bắt buộc | Mặc định | Ý nghĩa                                             |
| -------------------| ----------| ----------| -----------------------------------------------------|
| `REDIS_PORT`      | ⛔        | `6379`   | Map ra host.                                        |
| `REDIS_MAXMEMORY` | ⛔        | `512mb`  | Cap RAM. Policy `allkeys-lru` đã set sẵn ở compose. |

Nhóm **Qdrant** — vector store.

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `QDRANT_PORT` | ⛔ | `6333` | HTTP API + dashboard. |
| `QDRANT_GRPC_PORT` | ⛔ | `6334` | gRPC (client Python dùng). |
| `QDRANT_API_KEY` | ⛔ | _empty_ | Để trống = không auth, **chỉ OK khi không expose ra ngoài network Docker**. Prod nên bật. |

Nhóm **MinIO** — object storage cho file raw + parsed markdown.

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `MINIO_ROOT_USER` | ⛔ | `minioadmin` | Username admin. |
| `MINIO_ROOT_PASSWORD` | ✅ | — | **Đổi giá trị mặc định.** |
| `MINIO_BUCKET` | ⛔ | `rag-files` | Bucket app dùng, tạo tự động (service `minio-init`). |
| `MINIO_PORT` | ⛔ | `9000` | S3 API. |
| `MINIO_CONSOLE_PORT` | ⛔ | `9001` | Web UI. |

Nhóm **LLM** (Gemini qua OpenAI-compat endpoint).

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `LLM_PROVIDER` | ⛔ | `gemini` | Label thông tin, không ảnh hưởng routing. |
| `LLM_BASE_URL` | ⛔ | Gemini OpenAI endpoint | Đổi nếu dùng provider khác (OpenAI/Together/local LLM). |
| `LLM_API_KEY` | ✅ | — | API key của provider. |
| `LLM_MODEL_DEFAULT` | ⛔ | `gemini-2.5-flash` | Model dùng cho chat + rerank. |
| `LLM_MODEL_HEAVY` | ⛔ | `gemini-2.5-pro` | Dùng cho task khó (memory extraction, summarization dài). |

Nhóm **Embedding**.

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `EMBEDDING_MODEL` | ⛔ | `text-embedding-004` | Model embedding. |
| `EMBEDDING_DIM` | ⛔ | `768` | **Phải khớp với model.** Đổi model → đổi giá trị này → reindex toàn bộ Qdrant. |

Nhóm **Langfuse**.

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `LANGFUSE_SALT` | ✅ | — | 32-byte hex. |
| `LANGFUSE_ENCRYPTION_KEY` | ✅ | — | 32-byte hex. |
| `LANGFUSE_NEXTAUTH_SECRET` | ✅ | — | 32-byte hex. |
| `LANGFUSE_NEXTAUTH_URL` | ⛔ | `http://localhost:3000` | URL public Langfuse UI. Đổi khi deploy domain thật. |
| `LANGFUSE_WEB_PORT` | ⛔ | `3000` | Map ra host. |
| `LANGFUSE_PUBLIC_KEY` | ⛔ ban đầu | _empty_ | Điền sau khi tạo project trên Langfuse UI ([§5](#5-bootstrap-langfuse-sau-khi-stack-lên)). |
| `LANGFUSE_SECRET_KEY` | ⛔ ban đầu | _empty_ | Như trên. |

Nhóm **ClickHouse** (backing store của Langfuse v3).

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
|------|----------|----------|---------|
| `CLICKHOUSE_USER` | ⛔ | `clickhouse` | Username. |
| `CLICKHOUSE_PASSWORD` | ✅ | — | **Đổi giá trị mặc định.** |

Nhóm **Scaling**.

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `API_REPLICAS` | `2` | Số container `api` đứng sau Nginx. |
| `WORKER_REPLICAS` | `2` | Số container `worker` chạy ARQ. |
| `API_WORKERS` | `2` | Số gunicorn worker process **trong mỗi** container `api`. Tổng concurrency ≈ `API_REPLICAS × API_WORKERS × async_loop_concurrency`. |

Nhóm **Public**.

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `HTTP_PORT` | `80` | Port Nginx expose. Đổi nếu port 80 đã bận. |

### 2.4. Checklist trước khi chạy `make up` lần đầu

- [ ] `SECRET_KEY` đã sinh mới (≥ 32 byte).
- [ ] `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`, `LANGFUSE_NEXTAUTH_SECRET` đã sinh mới — **mỗi cái độc lập**.
- [ ] `LLM_API_KEY` đã điền Gemini key thật.
- [ ] `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`, `CLICKHOUSE_PASSWORD` đã đổi.
- [ ] Các port `80`, `3000`, `5432`, `6333`, `6334`, `6379`, `9000`, `9001` chưa bị chiếm bởi service khác trên host (kiểm tra `lsof -i :PORT`).

---

## 3. Lệnh chạy

```bash
make up
```

Tương đương `docker compose up -d`. Cờ `-d` chạy detached; logs xem qua
`make logs` hoặc `make logs SERVICE=api`.

Build image lần đầu mất ~3–5 phút (chủ yếu `uv sync` cài deps Python). Lần sau
Docker cache nên gần như tức thì.

Các shortcut khác (xem [Makefile](../Makefile)):

| Lệnh | Tác dụng |
|------|----------|
| `make build` | Build image (không start). |
| `make up` | Build + start detached. |
| `make down` | Stop, **giữ volume** (data còn nguyên). |
| `make down-clean` | Stop + xoá volume (**MẤT DATA**). |
| `make restart SERVICE=api` | Restart 1 service. |
| `make logs SERVICE=worker` | Tail logs. |
| `make ps` | List container + status. |
| `make psql` | Mở psql vào DB app. |
| `make redis-cli` | Mở redis-cli. |
| `make migrate` | Chạy Alembic upgrade thủ công (entrypoint đã tự chạy). |
| `make fresh` | `down-clean` + build + up + migrate (wipe sạch). |

---

## 4. Khi `make up`, hệ thống khởi tạo cái gì?

Bên dưới mô tả **thứ tự thực tế** các container start, ai chờ ai, và mỗi
container làm gì trong vài giây đầu đời.

### 4.1. Sơ đồ phụ thuộc (dependency graph)

```
postgres ──┬──► langfuse-web ──┐
           ├──► langfuse-worker│
           ├──► api ───────────┼──► nginx
           └──► worker         │
                               │
redis ─────┬──► langfuse-web ──┤
           ├──► langfuse-worker│
           ├──► api ───────────┤
           └──► worker         │
                               │
qdrant ────┬──► api ───────────┤
           └──► worker         │
                               │
minio ──┬──► minio-init        │
        ├──► langfuse-web ─────┤
        ├──► api ──────────────┤
        └──► worker            │
                               │
clickhouse ─┬──► langfuse-web ─┤
            └──► langfuse-worker
```

Compose tôn trọng `depends_on: { condition: service_healthy }` — service "sau"
chờ service "trước" pass healthcheck.

### 4.2. Diễn giải từng container

**1. `postgres`** (image `postgres:16-alpine`)
- Mount volume `postgres-data` → persist `/var/lib/postgresql/data`.
- Lần đầu: chạy mọi file trong [`docker/postgres/init/`](../docker/postgres/init/).
  Hiện tại có [`01-init.sql`](../docker/postgres/init/01-init.sql) — tạo DB
  `langfuse` (nếu chưa có), bật extension `pgcrypto`, `pg_trgm`, `btree_gin`
  trên DB `rag`.
- Healthcheck: `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` mỗi 5s.
- Khi healthy → mọi service phụ thuộc bắt đầu start.

**2. `redis`** (image `redis:7-alpine`)
- Mount volume `redis-data` → persist append-only log.
- Cấu hình: `--appendonly yes --maxmemory <REDIS_MAXMEMORY> --maxmemory-policy
  allkeys-lru --save 60 1000`.
- Healthcheck: `redis-cli ping`.

**3. `qdrant`** (image `qdrant/qdrant:v1.12.4`)
- Mount volume `qdrant-data` → `/qdrant/storage`.
- Auth: nếu `QDRANT_API_KEY` rỗng → mở. Có key → mọi request phải kèm header.
- Healthcheck: TCP probe vào port `6333`.
- **Chưa tạo collection lúc này** — collection `documents` và `memory` được tạo
  bởi `api` lúc startup (lazy create, xem [12-vector-search-internals.md](12-vector-search-internals.md)).

**4. `minio`** (image `minio/minio:RELEASE.2024-12-18T13-15-44Z`)
- Mount volume `minio-data` → `/data`.
- Chạy `minio server /data --console-address ":9001"`.
- Healthcheck: HTTP `/minio/health/ready`.

**5. `minio-init`** (image `minio/mc:RELEASE.2024-11-21T17-21-54Z`)
- One-shot job (`restart: no`). Chờ `minio` healthy → chạy:
  - `mc alias set local ...` — đăng ký endpoint.
  - `mc mb --ignore-existing local/<MINIO_BUCKET>` — tạo bucket `rag-files`.
  - `mc anonymous set none ...` — đảm bảo bucket **không** public.
- Xong → exit 0. Sau đó container ở trạng thái `Exited (0)` là **bình thường**,
  không phải lỗi.

**6. `clickhouse`** (image `clickhouse/clickhouse-server:24.10`)
- Backing store của Langfuse v3 (lưu traces). Riêng biệt với Postgres để chịu
  load ghi cao + truy vấn analytical.
- Mount 2 volume: `clickhouse-data` + `clickhouse-logs`.
- `ulimits.nofile: 262144` — ClickHouse cần nhiều file descriptor.
- Healthcheck: HTTP `/ping` port `8123`.

**7. `langfuse-web`** (image `langfuse/langfuse:3`)
- Chờ `postgres`, `redis`, `clickhouse`, `minio` đều healthy.
- Startup: chạy migration Langfuse trên cả Postgres (DB `langfuse`) và
  ClickHouse → khởi động Next.js server.
- Expose UI port `3000` ra host.
- `TELEMETRY_ENABLED=false` đã set sẵn.

**8. `langfuse-worker`** (image `langfuse/langfuse-worker:3`)
- Worker async của Langfuse (xử lý batch trace ingest từ Redis queue → ClickHouse).
- Không expose port.

**9. `api`** (build từ [`docker/Dockerfile`](../docker/Dockerfile), target `runtime`)
- Lần đầu: Docker build 2-stage. Stage builder cài deps qua `uv sync`. Stage
  runtime copy venv `/opt/venv` + source → tạo user `appuser` (uid 1001).
- Entrypoint: [`docker/entrypoint.sh`](../docker/entrypoint.sh) với arg `api`.
- Vào container, thứ tự:
  1. Chạy `python -m src.scripts.migrate` → Alembic `upgrade head`. Postgres
     advisory lock đảm bảo chỉ 1 replica thực sự chạy migration, các replica
     khác chờ.
  2. Gunicorn boot với `WEB_CONCURRENCY=$API_WORKERS` worker
     `UvicornWorker` → FastAPI app `src.api.main:app` bind `0.0.0.0:8000`.
  3. Lúc app khởi động (FastAPI lifespan), code khởi tạo:
     - DB pool (SQLAlchemy async, pool size `db_pool_size=10`).
     - Redis pool.
     - Qdrant client → đảm bảo 2 collection `documents` & `memory` tồn tại
       (tạo nếu chưa có, vector dim = `EMBEDDING_DIM`).
     - MinIO client.
     - Langfuse client (no-op nếu `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` rỗng).
- Healthcheck: HTTP `/health/ready` port `8000`. Khi pass → ready nhận traffic.
- **Không expose port ra host trực tiếp** — chỉ `expose: 8000` cho mạng internal.
  Truy cập qua Nginx.

**10. `worker`** (cùng image với `api`, command `worker`)
- Entrypoint chạy `arq src.worker.main.WorkerSettings` → ARQ poll Redis cho job.
- Cùng pool kết nối: Postgres, Redis, Qdrant, MinIO, LLM client.
- Xử lý các task: parse PDF/DOCX, chunk, embed batch, upsert Qdrant, extract
  long-term memory facts. Chi tiết: [14-queue-and-workers.md](14-queue-and-workers.md).

**11. `nginx`** (image `nginx:1.27-alpine`)
- Mount config từ [`docker/nginx/`](../docker/nginx/) read-only.
- Reverse proxy `http://api:8000` (Docker DNS resolve round-robin qua tất cả replica `api`).
- Tuning sẵn cho SSE: tắt `proxy_buffering`, `proxy_read_timeout` cao. Xem
  [15-streaming-sse.md](15-streaming-sse.md).
- Expose `HTTP_PORT` (mặc định `80`) ra host.

### 4.3. Timeline thực tế

| Mốc | Sự kiện |
|-----|---------|
| `t=0s` | `docker compose up -d` — image pull / build. |
| `t≈5–10s` | `postgres`, `redis`, `qdrant`, `minio`, `clickhouse` start song song. |
| `t≈10–20s` | Healthcheck pass lần lượt. `minio-init` chạy xong (`Exited 0`). |
| `t≈20–40s` | `langfuse-web` migrate xong → ready ở `:3000`. `langfuse-worker` lên. |
| `t≈20–40s` | `api` chạy Alembic migration → gunicorn boot → ready ở `:8000`. `worker` poll queue. |
| `t≈40s` | `nginx` proxy ready, `/health/ready` qua `localhost:80` trả 200. |

Nếu sau **~60s** vẫn chưa ready → `make logs SERVICE=<tên>` để xem container nào kẹt.

### 4.4. Volume tạo ra (data persistence)

`docker compose down` **không** xoá các volume sau. Chỉ `make down-clean` (cờ `-v`) mới xoá:

- `postgres-data` — app DB + langfuse DB + LangGraph checkpoint.
- `redis-data` — AOF persistence (cache có thể warm lại, nhưng buffer/session sẽ mất nếu xoá).
- `qdrant-data` — toàn bộ vector + payload.
- `minio-data` — file raw + parsed markdown.
- `clickhouse-data`, `clickhouse-logs` — traces Langfuse.

### 4.5. Network

- `backend` (bridge) — mọi service nội bộ.
- `edge` (bridge) — chỉ `nginx` thuộc cả 2 mạng. Tạo ranh giới rõ ràng giữa
  internet-facing và internal.

---

## 5. Bootstrap Langfuse sau khi stack lên

Lần đầu Langfuse chưa có project nào → app log sẽ ghi "langfuse disabled" và
traces không gửi đi đâu. Khắc phục:

1. Mở `http://localhost:3000`.
2. Đăng ký account đầu tiên (auto thành admin của instance).
3. Tạo Organization → Project.
4. Vào project → **Settings → API Keys** → tạo cặp key.
5. Copy:
   - `pk-lf-...` → `LANGFUSE_PUBLIC_KEY` trong `.env`.
   - `sk-lf-...` → `LANGFUSE_SECRET_KEY` trong `.env`.
6. Restart để app nạp env mới:
   ```bash
   make restart SERVICE=api
   make restart SERVICE=worker
   ```

Sau bước này, mọi request chat/upload sẽ tạo trace trên Langfuse UI.

---

## 6. Smoke test sau khi stack ready

```bash
# 1. Health
curl http://localhost/health/ready | jq
# -> {"status":"ready","checks":{"db":"ok","redis":"ok","qdrant":"ok","minio":"ok"}}

# 2. Đăng ký user
curl -X POST http://localhost/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","password":"Secret123!","tenant_slug":"demo"}'

# 3. Login → lấy token
TOKEN=$(curl -s -X POST http://localhost/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","password":"Secret123!","tenant_slug":"demo"}' \
  | jq -r .access_token)

# 4. Upload
curl -X POST http://localhost/v1/files \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@./data/sample.pdf"

# 5. Tạo conversation + chat stream
CID=$(curl -s -X POST http://localhost/v1/conversations \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | jq -r .id)

curl -N http://localhost/v1/chat/$CID/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Tóm tắt nội dung file vừa upload"}'
```

Endpoint chi tiết: [05-api-reference.md](05-api-reference.md).

---

## 7. URL truy cập sau khi up

| Service | URL | Mục đích |
|---------|-----|----------|
| API (qua Nginx) | http://localhost | Tất cả request app. |
| API docs (Swagger) | http://localhost/docs | OpenAPI UI. |
| Langfuse UI | http://localhost:3000 | Trace LLM + dashboard. |
| MinIO Console | http://localhost:9001 | Browse object store (login bằng `MINIO_ROOT_USER`/`PASSWORD`). |
| Qdrant Dashboard | http://localhost:6333/dashboard | Browse collection + payload. |
| Postgres | `localhost:5432` | Direct connect (psql/DBeaver). |
| Redis | `localhost:6379` | redis-cli. |

---

## 8. Lỗi thường gặp khi chạy lần đầu

### 8.1. "address already in use" / "bind: port is already allocated"

Port trên host đã bị service khác chiếm. Sửa bằng cách đổi biến `*_PORT` tương
ứng trong `.env` (ví dụ `HTTP_PORT=8080` thay vì `80`).

### 8.2. `api` container restart liên tục, log "migration failed"

- DB chưa healthy trước migration (race) → hiếm, vì compose đã chờ healthcheck.
  Nếu vẫn xảy ra: `make logs SERVICE=postgres` xem có lỗi disk/init không.
- Lock advisory bị giữ bởi process zombie → restart `postgres` rồi `api`.

### 8.3. Langfuse UI báo "Internal Server Error" lần đầu mở

- ClickHouse chưa migrate xong. Chờ thêm 30–60s rồi refresh.
- Hoặc `LANGFUSE_*` secret rỗng / không đủ 32 byte hex → check `.env`.

### 8.4. App log "langfuse disabled — keys missing"

`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` chưa điền. Làm theo [§5](#5-bootstrap-langfuse-sau-khi-stack-lên).

### 8.5. Upload file bị 401 / 403

- Token hết hạn (`jwt_access_ttl_min=15`) → login lại.
- Token thuộc tenant khác file đang chỉ tới → đúng RLS, không phải bug.

### 8.6. Embedding lỗi `dimension mismatch`

Đã đổi `EMBEDDING_MODEL` mà không xoá collection cũ. Hai lựa chọn:
- Đổi lại model cũ.
- `make down-clean` (mất data Qdrant) rồi `make up` để tạo lại collection với
  `EMBEDDING_DIM` mới.

### 8.7. Worker không xử lý job

- `make logs SERVICE=worker` → kiểm tra có poll Redis không.
- `make redis-cli` → `KEYS arq:*` xem có job trong queue.
- Worker dùng cùng `LLM_API_KEY` — key sai/hết quota cũng làm job stuck retry.

---

## 9. Stop & cleanup

```bash
make down            # stop, giữ data
make down-clean      # stop, XOÁ MỌI VOLUME (dev only, không undo được)
make fresh           # = down-clean + build + up + migrate (rebuild sạch)
```

Trước khi `make down-clean` ở môi trường có data thật, tham khảo backup
([04-deployment.md §7](04-deployment.md)).

---

## 10. Next steps

- Hiểu data flow upload → searchable: [02-data-flow.md](02-data-flow.md), [08-data-lifecycle.md](08-data-lifecycle.md).
- Hiểu concurrency: [09-concurrency-model.md](09-concurrency-model.md).
- Tuning trước go-prod: [04-deployment.md §9](04-deployment.md).
- Khi component lỗi: [17-failure-modes.md](17-failure-modes.md).
