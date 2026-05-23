# 17 — Failure Modes & Resilience

Distributed system = nhiều thứ có thể fail cùng lúc. Doc này liệt kê **mọi
failure mode đã nghĩ ra**, hệ thống xử lý ra sao, và playbook recovery khi
fail thật.

---

## 1. Failure landscape

```mermaid
graph TB
    subgraph "External deps"
        Gemini[Gemini API down/slow/429]
    end

    subgraph "Internal infra"
        PG[Postgres down]
        Redis[Redis down/OOM]
        Qdrant[Qdrant down/slow]
        MinIO[MinIO down]
        Langfuse[Langfuse down]
    end

    subgraph "App processes"
        API[API process crash]
        Worker[Worker process crash]
        Nginx[Nginx down]
    end

    subgraph "Data"
        Inconsistent[State drift between stores]
        StuckJob[Job stuck in-flight]
        OrphanData[Orphan files in MinIO]
    end

    subgraph "Network"
        DNS[DNS resolution fail]
        Partition[Network partition]
    end

    style Gemini fill:#ffe1e1
    style PG fill:#ffe1e1
    style Inconsistent fill:#ffe1e1
```

---

## 2. Decision tree: phản ứng theo loại failure

```mermaid
flowchart TD
    Start[Detect failure]
    Start --> Class{Loại}

    Class -->|Transient| Retry[Retry với exponential backoff]
    Class -->|Permanent| Fail[Fail fast + mark]
    Class -->|Unknown| Probe[Healthcheck → classify]

    Retry --> Success{Success?}
    Success -->|Yes| Done([Done])
    Success -->|No, attempts < N| Retry
    Success -->|No, attempts = N| Degrade

    Fail --> Notify[Log + alert]
    Notify --> Done

    Probe --> Class

    Degrade{Có graceful path?}
    Degrade -->|Yes| Fallback[Trả lời partial / cached]
    Degrade -->|No| UserError[5xx + retry suggestion]

    Fallback --> Done
    UserError --> Done
```

---

## 3. Gemini API failures

### 3.1. Rate limit 429

```mermaid
sequenceDiagram
    participant Code
    participant Tenacity as tenacity retry
    participant Gemini

    Code->>Tenacity: call complete()
    Tenacity->>Gemini: POST /chat/completions
    Gemini-->>Tenacity: 429 Too Many Requests
    Note over Tenacity: wait 1s + jitter
    Tenacity->>Gemini: retry attempt 2
    Gemini-->>Tenacity: 429
    Note over Tenacity: wait 2s + jitter
    Tenacity->>Gemini: retry attempt 3
    Gemini-->>Tenacity: 200 OK
    Tenacity-->>Code: result
```

Code:
```python
# src/llm/client.py
@retry(
    retry=retry_if_exception_type((TimeoutError, ConnectionError)),
    stop=stop_after_attempt(self._s.llm_max_retries),
    wait=wait_exponential_jitter(initial=1, max=10),
    reraise=True,
)
```

**Vấn đề hiện tại**: tenacity chỉ retry trên `TimeoutError` và `ConnectionError`,
KHÔNG retry trên HTTP 429 từ OpenAI SDK (sẽ raise `RateLimitError`). Cần sửa:

```python
from openai import RateLimitError, APIError
retry_if_exception_type((TimeoutError, ConnectionError, RateLimitError, APIError))
```

(TODO sửa.)

### 3.2. Service unavailable / 5xx
Tenacity retry 3 lần. Nếu vẫn fail:
- Chat: API trả `event: error` → client thấy message lỗi friendly.
- Worker (embedding/rerank): job retry qua ARQ (up to max_tries). Cuối cùng
  doc → `failed`.

### 3.3. Latency cao bất thường
- HTTP timeout 60s (config `llm_timeout_s`).
- httpx ngắt → tenacity coi như TimeoutError → retry.
- Cumulative request time có thể vượt SSE timeout của Nginx (1h) — không
  vấn đề thường.

### 3.4. Circuit breaker (TODO)

Khi Gemini fail >50% trong 30s, mở circuit 60s — fail nhanh để không waste
retry latency:

```python
# pseudo, dùng aiocircuitbreaker
from aiocircuitbreaker import circuit

@circuit(failure_threshold=5, recovery_timeout=60)
async def _call(...):
    ...
```

Open state: trả error ngay, không gọi upstream. Half-open sau 60s: thử 1
request; OK → close; fail → open lại.

---

## 4. Postgres failure

```mermaid
stateDiagram-v2
    [*] --> Healthy
    Healthy --> Slow: Connection pool exhausted
    Healthy --> Down: Process crash / network
    Slow --> Healthy: Pool freed
    Slow --> Down: Sustained pressure
    Down --> Healthy: Postgres restarted
    Healthy --> Replica: failover (TODO)
    Replica --> Healthy: primary back
```

### Pool exhausted
- Symptom: `sqlalchemy.exc.TimeoutError: QueuePool limit overflow`.
- Cause: long transaction + nhiều concurrent request.
- Mitigation: tăng `db_pool_size`, hoặc đặt PgBouncer.

### Hard down
- Healthcheck `/health/ready` fail → Nginx route đi replica (chưa có HA).
- Mọi write fail → 500 cho user.
- Recovery: docker compose restart postgres → migrations idempotent rerun OK.

### Failover (HA, TODO)
- Postgres streaming replica (read-only).
- pgbouncer config primary/replica.
- Promote replica khi primary down → app reconnect.

### Data loss risk
Postgres `fsync=on` (default) → mọi commit durable trên disk. Power failure
mất ≤ 1 WAL segment chưa flush.

Backup chiến lược: xem [docs/04-deployment.md](04-deployment.md#7-backup--restore).

---

## 5. Redis failure

Redis nhiều role → tùy role mức độ ảnh hưởng khác nhau:

```mermaid
graph LR
    Redis_Down[Redis down] --> R1[Cache layer]
    Redis_Down --> R2[Queue ARQ]
    Redis_Down --> R3[Short-term buffer]
    Redis_Down --> R4[Pub/Sub]
    Redis_Down --> R5[Rate limit]

    R1 --> R1A[Bypass cache<br/>slow but functional]
    R2 --> R2A[New jobs lost<br/>API trả 503 upload]
    R3 --> R3A[Warm-up từ DB<br/>seamless cho user]
    R4 --> R4A[File events SSE broken<br/>user phải poll]
    R5 --> R5A[Rate limit fail-open<br/>tạm thời không giới hạn]

    style R1A fill:#e1f5e1
    style R3A fill:#e1f5e1
    style R5A fill:#fff4e1
    style R2A fill:#ffe1e1
    style R4A fill:#ffe1e1
```

### Graceful degradation cho cache
Cache function nên try/except, không raise:
```python
async def cache_get(key):
    try:
        raw = await r.get(key)
        return orjson.loads(raw) if raw else None
    except Exception:
        return None    # cache miss → upstream
```

Hiện code chưa wrap try → khi Redis down, exception lên → request fail. **TODO
fix**.

### Persistence
- `appendonly yes` + `save 60 1000` → AOF flush mỗi giây.
- Mất tối đa ~1 giây data khi crash → buffer hơi mất nhưng warm-up từ DB OK.

### Memory pressure
`maxmemory 512mb` + `allkeys-lru` → tự evict entry cũ. Không OOM hard.

---

## 6. Qdrant failure

```mermaid
flowchart TD
    Q[Qdrant down] --> Agent[Agent retrieve fail]
    Agent --> Choice{Fallback?}
    Choice -->|"Phương án 1"| Skip[Skip retrieve, LLM trả lời chỉ từ memory + general knowledge]
    Choice -->|"Phương án 2"| Error[Trả error → user retry]

    style Skip fill:#fff4e1
    style Error fill:#ffe1e1
```

Hiện code dùng phương án 2 (raise → SSE error). Phương án 1 tốt hơn UX nhưng
risk: LLM hallucinate vì không có context.

### Vectors loss
Nếu Qdrant data corrupt:
- **Source-of-truth ở Postgres** (`document_chunks.text`).
- Embedding cache Redis 7d → cache hits cao khi rebuild.
- Script rebuild:
  ```python
  # scripts/rebuild_qdrant.py (TODO)
  docs = await session.execute(select(Document).where(status='indexed'))
  for doc in docs:
      chunks = await get_chunks(doc.id)
      vectors = await embedder.embed([c.text for c in chunks])
      await qc.upsert(...)
  ```

---

## 7. MinIO failure

Storage tier — chứa file gốc.

### Mất file gốc
Nếu MinIO data corrupt + không có backup:
- Document Postgres còn metadata + chunks text → **vẫn search được**.
- Mất khả năng re-parse / re-chunk / download nguyên bản.

→ MinIO backup quan trọng. Mirror sang offsite định kỳ
(xem [04-deployment.md](04-deployment.md)).

### Upload fail
Symptom: `S3Error` khi `put_object`.
- API code: raise → 500 cho client → user retry.
- Worker code: `process_document` raise → ARQ retry → cuối cùng `failed`.

---

## 8. Langfuse failure

Quan trọng: **Langfuse fail KHÔNG được làm app fail**.

Code đã handle:
```python
# src/observability/langfuse.py
def trace_score(...):
    if _lf is None:
        return       # no-op nếu chưa init
    try:
        ...
    except Exception:
        pass         # swallow
```

→ Observability là nice-to-have. App vẫn serve, mất visibility.

Langfuse có **internal queue** flush batch → app chỉ write local buffer, không
synchronous với Langfuse server.

---

## 9. Worker process crash

```mermaid
sequenceDiagram
    participant ARQ
    participant Worker
    participant Redis
    participant Job

    Worker->>Redis: BZPOPMIN arq:queue
    Redis-->>Worker: job J1
    Worker->>Redis: SADD arq:in-progress J1
    Worker->>Job: execute(...)
    Note over Worker: OOM kill / SIGKILL
    Worker--xJob: (crashed)

    Note over Redis: J1 still in arq:in-progress<br/>but no worker

    Note over ARQ: Health check / restart mechanism
    ARQ->>Redis: detect stale in-progress<br/>(no heartbeat > timeout)
    ARQ->>Redis: re-queue J1
    Redis-->>Worker: J1 picked by another worker
    Worker->>Job: execute(...) (idempotent → safe)
```

### Idempotency là cứu cánh
Vì task được thiết kế idempotent (check `status==indexed`, delete-then-upsert
Qdrant, dedupe facts), re-deliver không gây side effect xấu.

### Stuck job (no heartbeat)
ARQ default: nếu worker không update heartbeat trong `job_timeout=600s`, job
được consider lost → re-queue. Hiện cấu hình OK.

### Doc stuck `parsing`
Cron sweep (TODO):
```python
# Mỗi 5 phút
async def sweep_stuck(ctx):
    cutoff = now - 10 phút
    stuck = SELECT documents WHERE status IN ('parsing','chunking','embedding')
                                AND updated_at < cutoff
    for d in stuck:
        log.warning("resurrect", doc_id=d.id)
        await arq.enqueue_job("process_document", str(d.id))
```

---

## 10. API process crash giữa stream

```mermaid
sequenceDiagram
    participant Client
    participant API
    participant Gemini

    Client->>API: POST /v1/chat/cid/messages
    API->>Gemini: chat completion stream
    Gemini-->>API: chunk 1
    API-->>Client: event: token data: "Xin"
    Gemini-->>API: chunk 2
    API-->>Client: event: token data: " chào"

    Note over API: SIGKILL / OOM
    API--xClient: (TCP FIN unexpected)

    Note over Client: EventSource onerror
    Client->>Client: auto-reconnect? NO - POST endpoint
    Note over Client: User sees half answer<br/>retry by re-sending message
```

### State sau crash
- User message **đã persist** (insert trước stream).
- Assistant message **KHÔNG persist** (insert sau stream done).
- Buffer Redis: chỉ có user message, không có assistant.

→ User retry → bot trả lời message lần 2, có history cũ (user msg). UX kém
nhưng không broken.

### Improvement: incremental persist (TODO)
Mỗi 500ms hoặc 100 token, append chunk vào DB. Nếu crash:
- Lưu partial message trong DB với flag `meta.incomplete=true`.
- Khi user reconnect, show partial + cho phép "continue".

Phức tạp; chưa làm.

---

## 11. Nginx down

Nginx là single point of failure ở compose single-host.

### Mitigation
- `restart: unless-stopped` → auto-restart container.
- Healthcheck → docker thấy unhealthy → restart.

### HA setup (K8s)
- 2 Nginx pods sau LoadBalancer (ELB).
- Nếu 1 pod chết, LB drain.

---

## 12. Data drift between stores

Tình huống tệ nhất: 3 store khác nhau (Postgres, Qdrant, MinIO) không đồng bộ.

### Drift patterns

| Drift | Triệu chứng | Fix |
|-------|------------|-----|
| Doc Postgres `indexed`, không có vectors Qdrant | Search miss | Re-enqueue `process_document` |
| Vectors Qdrant tồn tại, doc Postgres không có | Search trả "ghost" | Cron sweep Qdrant orphan |
| Chunks Postgres không có Qdrant point | Citation OK, search miss | Re-embed |
| File MinIO không có doc Postgres | Storage rác | Cron sweep MinIO orphan |
| Doc Postgres không có file MinIO | Cannot re-parse | Mark `failed`, request user re-upload |

### Detection script (TODO)
```python
# scripts/audit_consistency.py
async def audit():
    # 1. Mỗi doc indexed -> count vectors Qdrant matching doc_id
    docs = await pg.execute(select(Document).where(status='indexed'))
    for d in docs:
        pg_chunks = await pg.execute(select(func.count()).filter(...))
        qd_count = await qc.count(filter_by_doc_id=d.id)
        if pg_chunks != qd_count:
            log.error("drift", doc_id=d.id, pg=pg_chunks, qd=qd_count)

    # 2. Qdrant orphan: doc_id không có trong Postgres
    qd_doc_ids = await qc.scroll_unique_doc_ids()
    pg_doc_ids = await pg.execute(select(Document.id))
    orphan = qd_doc_ids - set(pg_doc_ids)
    log.warning("qdrant_orphans", count=len(orphan))
```

Chạy hàng tuần như cron.

---

## 13. Total system recovery

Scenario: data center power loss, restart cả stack.

```mermaid
flowchart TD
    Start[All services down]
    Start --> A[docker compose up -d]
    A --> B[Postgres start + WAL replay]
    B --> C[Redis start + AOF replay]
    C --> D[Qdrant start + WAL replay]
    D --> E[MinIO start + erasure check]
    E --> F[Langfuse migration]
    F --> G[Healthchecks pass]
    G --> H[API + Worker boot]
    H --> I{Migration OK?}
    I -->|Yes| J[Serve traffic]
    I -->|No| K[Exit + alert]

    style B fill:#e1f5e1
    style C fill:#e1f5e1
    style D fill:#e1f5e1
    style E fill:#e1f5e1
    style J fill:#e1f5e1
```

Mỗi component có recovery của riêng:
- Postgres WAL: rollback transaction chưa commit, replay committed.
- Redis AOF: replay commands.
- Qdrant WAL: replay upsert chưa flush.
- MinIO: scan disk integrity.

**RTO** (Recovery Time Objective): ~ 2-5 phút cho cold start.
**RPO** (Recovery Point Objective): ~ 1 giây mất data Redis cache; 0 cho
Postgres/MinIO (fsync default).

---

## 14. Runbook: troubleshooting cheat sheet

```mermaid
flowchart TD
    Symptom{Symptom}
    Symptom -->|All 5xx| RDY[Check /health/ready]
    Symptom -->|Slow chat| LF[Check Langfuse traces]
    Symptom -->|Doc stuck pending| WK[Check worker logs]
    Symptom -->|Search empty| QD[Check Qdrant count]
    Symptom -->|Auth fail| PG[Check Postgres + secret]

    RDY --> RDY1{Which dep down?}
    RDY1 -->|Postgres| Pg1[docker logs postgres]
    RDY1 -->|Redis| Re1[docker logs redis]
    RDY1 -->|Qdrant| Qd1[docker logs qdrant]
    RDY1 -->|MinIO| Mn1[docker logs minio]

    LF --> LF1{Node slow}
    LF1 -->|generate| Gem[Gemini latency / 429]
    LF1 -->|retrieve| QC[Qdrant CPU]
    LF1 -->|rerank| Gem

    WK --> WK1[arq:in-progress stale?]
    WK1 -->|Yes| Re-enqueue[Re-enqueue manually]
    WK1 -->|No| Logs[Inspect task error]

    QD --> QD1[count by tenant_id]
    QD1 -->|0| Reb[Rebuild from Postgres]
    QD1 -->|exists| Filter[Check filter logic]

    PG --> PG1[Verify SECRET_KEY matches]
```

### Commands tham khảo
```bash
# Health
curl -s http://localhost/health/ready | jq

# Stuck docs
make psql -c "SELECT id, title, status, updated_at FROM documents WHERE status NOT IN ('indexed','failed') ORDER BY updated_at;"

# ARQ queue depth
docker compose exec redis redis-cli ZCARD arq:queue

# Qdrant collection info
curl -s http://localhost:6333/collections/documents | jq

# Redis memory pressure
docker compose exec redis redis-cli INFO memory | grep used_memory_human

# Worker logs
make logs SERVICE=worker
```

---

## 15. Chaos testing (TODO)

Để verify resilience:

```mermaid
graph LR
    Test[chaos test] --> Scenarios

    Scenarios --> S1[Kill 1 API replica]
    Scenarios --> S2[Slow Qdrant: tc qdisc add netem delay 500ms]
    Scenarios --> S3[Drop Redis briefly]
    Scenarios --> S4[Postgres OOM]
    Scenarios --> S5[Disk full MinIO]

    S1 --> V1[Verify: load distributed, no error spike]
    S2 --> V2[Verify: timeout handled, retry kicks]
    S3 --> V3[Verify: graceful degrade cache + queue]
    S4 --> V4[Verify: 5xx returned with retry-after]
    S5 --> V5[Verify: upload fails clean, no data corrupt]
```

Tool: `pumba` (Docker chaos), `chaos-mesh` (K8s).

---

## 16. Lessons learned principles

1. **Failure không phải exception** — là norm. Code phải assume mọi I/O sẽ
   fail rồi handle.
2. **Idempotency là superpower** — retry an toàn = ngủ ngon.
3. **Fail loud, recover gracefully** — log mọi exception, nhưng user thấy
   message friendly.
4. **Defense in depth** — không trust 1 layer. Cache miss + DB filter + RLS.
5. **Source-of-truth single** — Postgres. Mọi store khác rebuild được.
6. **Test recovery thực sự** — không chỉ test happy path.
7. **Observability trước resilience** — không đo được thì không fix được.
