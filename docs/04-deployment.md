# 04 — Deployment & Operations

Hướng dẫn triển khai từ zero đến production-ready single host, kèm scale-up.

---

## 1. Yêu cầu host

| Resource | Min dev | Recommended prod |
|----------|---------|------------------|
| CPU | 4 cores | 8+ cores |
| RAM | 8 GB | 16+ GB |
| Disk | 20 GB SSD | 200+ GB SSD |
| OS | Linux/macOS | Ubuntu 22.04 LTS |
| Docker | 24+ | 24+ với compose v2 |

GPU: **không bắt buộc** vì dùng Gemini API + LLM rerank, không host model.

---

## 2. Quickstart (dev)

```bash
# 1. Clone & cd
cd production_rag

# 2. Tạo .env từ template
cp .env.example .env

# 3. Sinh secrets (chạy 3 lệnh, paste vào .env)
python -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(64))"
openssl rand -hex 32   # LANGFUSE_SALT
openssl rand -hex 32   # LANGFUSE_ENCRYPTION_KEY
openssl rand -hex 32   # LANGFUSE_NEXTAUTH_SECRET

# 4. Điền LLM_API_KEY (Gemini key)

# 5. Build + start
make up

# 6. Theo dõi logs
make logs SERVICE=api

# 7. Kiểm tra health
curl http://localhost/health/ready | jq
```

Khi tất cả services ready (~30-60s), tạo project Langfuse:
```
http://localhost:3000   (đăng ký account đầu tiên = admin)
```
Tạo project, copy `LANGFUSE_PUBLIC_KEY` và `LANGFUSE_SECRET_KEY` vào `.env`,
`make restart SERVICE=api worker`.

---

## 3. Tạo tài khoản test

```bash
curl -X POST http://localhost/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","password":"Secret123!","tenant_slug":"demo"}'

curl -X POST http://localhost/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","password":"Secret123!","tenant_slug":"demo"}'
# -> {"access_token":"...", "refresh_token":"...", ...}
```

Lưu `access_token` thành `TOKEN`.

---

## 4. Upload tài liệu

```bash
curl -X POST http://localhost/v1/files \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@./data/luat_doanh_nghiep.pdf" \
  -F "files=@./data/luat_dau_tu.pdf"
# -> 202 với doc_id list + job_ids
```

Theo dõi status:
```bash
curl http://localhost/v1/files/<doc_id>/events \
  -H "Authorization: Bearer $TOKEN" \
  -N    # disable buffer
```

---

## 5. Chat (streaming)

```bash
# Tạo conversation
CID=$(curl -s -X POST http://localhost/v1/conversations \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{}' | jq -r .id)

# Stream chat
curl -N http://localhost/v1/chat/$CID/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Điều 5 luật doanh nghiệp quy định gì?"}'
```

---

## 6. Scale services

Compose hỗ trợ `--scale` trên cùng image:
```bash
docker compose up -d --scale api=4 --scale worker=3
```

Cấu hình giá trị mặc định qua `.env`:
```
API_REPLICAS=4
WORKER_REPLICAS=3
```

Lưu ý: Nginx upstream resolve qua Docker DNS, tự nhận thêm replica mới.

---

## 7. Backup & restore

### 7.1. Postgres
```bash
# backup
docker compose exec -T postgres pg_dump -U $POSTGRES_USER $POSTGRES_DB | gzip > backup_$(date +%F).sql.gz

# restore
gunzip -c backup_2026-05-21.sql.gz | docker compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB
```

### 7.2. Qdrant
```bash
# snapshot (HTTP API)
curl -X POST http://localhost:6333/collections/documents/snapshots
# Restore: copy file vào volume + import qua API
```

### 7.3. MinIO
```bash
# mirror sang offsite
docker run --rm -v $(pwd)/backup:/data --network production_rag_backend minio/mc \
  mirror --overwrite local/rag-files /data/rag-files-$(date +%F)
```

### 7.4. Redis
- Cache có thể mất; nhưng `appendonly yes` + `save 60 1000` đã bật.
- Buffer ngắn hạn rebuild được từ Postgres (xem `warmup_from_db`).

---

## 8. Monitoring & alerting (gợi ý)

Hệ thống đã expose:
- `/health/live`, `/health/ready` — uptime probe.
- `/metrics` (Prometheus) — request rate, latency, errors.
- Langfuse — RAG-level traces.

Setup Prometheus + Grafana song song (compose riêng) cho prod:
- Scrape `api:8000/metrics`.
- Alert: 5xx > 1% / 5 phút, p95 latency > 5s, /health/ready failed > 2 phút.

---

## 9. Tuning checklist trước khi go-prod

- [ ] Đổi mọi password default trong `.env`.
- [ ] `SECRET_KEY`, `LANGFUSE_*` đều random, không reuse.
- [ ] `CORS_ORIGINS` whitelist domain frontend (không `*`).
- [ ] Bật TLS ở Nginx (mount cert từ `certbot`).
- [ ] Đặt `ENV=prod` -> log JSON, debug tắt.
- [ ] Tăng `API_REPLICAS` / `WORKER_REPLICAS` theo throughput thực tế.
- [ ] Bật Qdrant API key (`QDRANT_API_KEY`).
- [ ] Migrate Langfuse DB sang ClickHouse riêng nếu trace > 1M/tháng.
- [ ] Snapshot Qdrant định kỳ.
- [ ] Mở giới hạn upload qua `MAX_UPLOAD_SIZE_MB` theo nhu cầu.

---

## 10. Migrating to Kubernetes

Khi đã quá tải single host:
1. Build image push registry (`make build push`).
2. Mỗi service trong compose -> 1 Deployment + Service trong K8s.
3. Postgres / Redis / Qdrant / MinIO -> dùng Helm chart official hoặc managed.
4. Nginx → Ingress (nginx-ingress hoặc traefik).
5. Secrets → External Secrets Operator hoặc Sealed Secrets.
6. HPA cho `api` và `worker` theo CPU/QPS.

Code không cần đổi vì đã stateless + 12-factor.
