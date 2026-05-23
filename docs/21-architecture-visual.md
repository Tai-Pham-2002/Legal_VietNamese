# 21 — Architecture Visual (Diagram trực quan)

> Đây là bản **diagram-first** của [01-architecture-overview.md](./01-architecture-overview.md).
> Cùng nội dung, nhưng mọi thứ được thể hiện bằng Mermaid thay vì text thuần.

---

## 1. Mục tiêu & NFR

```mermaid
mindmap
  root((Agentic RAG<br/>Production))
    Concurrency
      100+ user song song
      Horizontal scale API + Worker
    Memory
      Multi-turn chat
      Short-term Redis buffer
      Long-term Qdrant facts
    Ingestion
      Upload file bất đồng bộ
      Background worker
    Latency
      First token < 2s p95
      Cache aggressively
    Reliability
      1 component down ≠ sập hệ thống
      Circuit breaker
      Graceful shutdown
    Observability
      Trace toàn bộ flow
      Langfuse self-hosted
    Cost
      Cache LLM + Embedding
      LLM rerank thay cross-encoder
    Self-host
      Mọi infra tự host được
      Docker Compose ready
```

---

## 2. High-Level Architecture

```mermaid
flowchart TB
    subgraph Internet["🌐 Internet"]
        Client["💻 Clients\nweb / CLI / SDK"]
    end

    subgraph Gateway["🔒 Gateway Layer"]
        Nginx["NGINX\n• TLS termination\n• Round-robin LB\n• SSE: proxy_buffering off"]
    end

    subgraph APITier["⚡ API Tier  —  FastAPI + Uvicorn + Gunicorn"]
        direction LR
        API1["API replica 1"]
        API2["API replica 2"]
        APIN["API replica N"]
        AgentNote["LangGraph Agent\nchạy TRONG process API\n(cần real-time stream)"]
    end

    subgraph DataTier["🗄️ Data Tier"]
        direction TB
        PG[("PostgreSQL\n• Users / Tenants\n• Conversations / Messages\n• Documents / Chunks\n• user_facts (long-mem)\n• LangGraph checkpoints")]
        Redis[("Redis\n• Short-term buffer\n• LLM / Embed cache\n• ARQ job queue\n• Pub/Sub events\n• Rate limit ZSET")]
        Qdrant[("Qdrant\n• collection: docs\n  768d embeddings\n• collection: memory\n  user fact vectors")]
    end

    subgraph WorkerTier["⚙️ Worker Tier  —  ARQ (asyncio-native)"]
        W1["Worker 1"]
        W2["Worker 2"]
        WN["Worker N"]
        W_Jobs["Jobs:\n• process_document\n• extract_facts"]
    end

    subgraph Storage["📦 Object Storage"]
        MinIO["MinIO  (S3-compatible)\n• raw files (PDF, DOCX)\n• parsed markdown"]
    end

    subgraph Observe["📊 Observability"]
        Langfuse["Langfuse  (self-hosted)\n• Traces & Spans\n• Generations\n• Eval scores"]
    end

    Client -- "HTTPS / SSE" --> Nginx
    Nginx -- "Round-robin" --> API1
    Nginx -- "Round-robin" --> API2
    Nginx -- "Round-robin" --> APIN

    API1 & API2 & APIN <--> PG
    API1 & API2 & APIN <--> Redis
    API1 & API2 & APIN <--> Qdrant

    API1 & API2 & APIN -- "enqueue job" --> Redis

    W1 & W2 & WN -- "pop job" --> Redis
    W1 & W2 & WN <--> PG
    W1 & W2 & WN <--> Qdrant
    W1 & W2 & WN <--> MinIO
    W1 & W2 & WN -- "traces" --> Langfuse

    API1 & API2 & APIN -- "traces" --> Langfuse
    API1 & API2 & APIN -- "files" --> MinIO

    style Internet fill:#f0f4ff,stroke:#4a90d9
    style Gateway fill:#fff8e1,stroke:#fbc02d
    style APITier fill:#e8f5e9,stroke:#43a047
    style DataTier fill:#fce4ec,stroke:#e91e63
    style WorkerTier fill:#fff3e0,stroke:#fb8c00
    style Storage fill:#f3e5f5,stroke:#8e24aa
    style Observe fill:#e0f7fa,stroke:#00acc1
```

---

## 3. Phân tách trách nhiệm (Process Types)

```mermaid
flowchart LR
    subgraph API["⚡ API Process\n(stateless, nhiều replica)"]
        direction TB
        a1["✅ Nhận HTTP / SSE"]
        a2["✅ JWT Auth + Validate"]
        a3["✅ Rate limit"]
        a4["✅ LangGraph Agent stream"]
        a5["✅ Enqueue background jobs"]
        a6["❌ KHÔNG parse PDF"]
        a7["❌ KHÔNG embed batch lớn"]
    end

    subgraph Worker["⚙️ Worker Process\n(background, nhiều replica)"]
        direction TB
        w1["✅ Parse PDF → Markdown"]
        w2["✅ Chunk document"]
        w3["✅ Batch embed → Qdrant"]
        w4["✅ Extract long-term facts"]
        w5["✅ Publish progress events"]
        w6["❌ KHÔNG serve HTTP"]
    end

    subgraph Why["💡 Tại sao tách?"]
        direction TB
        r1["Parse PDF: ~30s\n→ block event loop nếu trong API"]
        r2["Worker fail ≠ API fail"]
        r3["Scale worker độc lập\nvới API"]
    end

    API -- "enqueue\n(Redis Stream)" --> Worker
    Worker -- "UPDATE status\n(Postgres)" --> API
    Worker -. "Pub/Sub event\n(Redis)" .-> API

    style API fill:#e8f5e9,stroke:#43a047
    style Worker fill:#fff3e0,stroke:#fb8c00
    style Why fill:#f5f5f5,stroke:#9e9e9e
```

---

## 4. Vai trò của Redis — 6 Use Cases

```mermaid
mindmap
  root(("Redis\nbackbone"))
    Short-term Buffer
      Key: conv:buf:uuid
      Type: List
      RPUSH append
      LTRIM giữ 40 entries
      TTL: 24h
    LLM Cache
      Key: sha256 of prompt+model
      Type: String JSON
      TTL: 1 giờ
      Bypass nếu stream=true
    Embedding Cache
      Key: sha256 of text+model
      Type: String JSON
      TTL: 7 ngày
      Ổn định vì deterministic
    ARQ Job Queue
      Type: Redis Stream
      Producers: API replicas
      Consumers: Workers
      Max 8 concurrent jobs/worker
    Pub/Sub Events
      Channel: doc:id:events
      Worker publish progress
      API subscribe → SSE client
    Rate Limit
      Key: rl:user_id:endpoint
      Type: Sorted Set
      Sliding window 60s
      60 req/phút/user chat
```

---

## 5. Flow Upload File (Ingestion Pipeline)

```mermaid
flowchart TB
    C["💻 Client"] -- "POST /v1/files\n(multipart)" --> API

    subgraph API["⚡ API"]
        A1["Validate: mime, size ≤ 50MB"]
        A2["Upload raw file → MinIO\ntenants/tid/docs/did/raw.pdf"]
        A3["INSERT documents\nstatus = 'pending'"]
        A4["ARQ.enqueue\n('process_document', doc_id)"]
        A5["Return 202\n{doc_id, status_url}"]
        A1 --> A2 --> A3 --> A4 --> A5
    end

    A4 -- "job" --> Redis[("Redis\nStream")]

    subgraph Worker["⚙️ Worker"]
        W0["Pop job from queue"]
        W1["Download raw file ← MinIO"]
        W2["Parse PDF/DOCX → Markdown\n(PyMuPDF + pypdf)"]
        W3["Upload parsed.md → MinIO"]
        W4["Chunk document\n(heading-aware + token-aware)\nmax 800 tokens, overlap 100"]
        W5["Batch embed → Gemini API\n768d vectors\n(với L2 cache lookup)"]
        W6["Upsert → Qdrant 'docs'\npayload: tenant_id, doc_id,\nchunk_index, heading_path,\npage_from, page_to, text"]
        W7["INSERT document_chunks\n→ Postgres"]
        W8["UPDATE documents\nstatus = 'indexed'\nn_chunks = K"]
        W9["PUBLISH doc:id:events\n{status: 'indexed'}"]

        W0 --> W1 --> W2 --> W3 --> W4 --> W5 --> W6 --> W7 --> W8 --> W9
    end

    Redis --> W0

    W9 -- "SSE event" --> C

    style API fill:#e8f5e9,stroke:#43a047
    style Worker fill:#fff3e0,stroke:#fb8c00
```

**Document status state machine:**

```mermaid
stateDiagram-v2
    direction LR
    [*] --> pending : API nhận file
    pending --> parsing : Worker bắt đầu
    parsing --> chunking : Parse xong → Markdown
    chunking --> embedding : Chunks ready
    embedding --> indexed : Upsert Qdrant xong
    parsing --> failed : Exception
    chunking --> failed : Exception
    embedding --> failed : Exception
    indexed --> [*]
    failed --> pending : Cron job re-enqueue
```

---

## 6. Flow Chat Multi-Turn (Streaming)

```mermaid
flowchart TB
    C["💻 Client\nEventSource SSE"] -- "POST /v1/chat/{conv_id}/messages\n{message, doc_ids?}" --> API

    subgraph API["⚡ API — chat_stream()"]
        P1["Auth JWT → user_id, tenant_id"]
        P2["Rate limit: 60 req/phút"]
        P3["Validate conv belongs to user\n← Postgres"]
        P4["INSERT message (role=user)\n→ Postgres + COMMIT"]
        P5["LRANGE Redis buffer\n(warmup từ PG nếu rỗng)"]
        P6["RPUSH message vào buffer"]
        P1 --> P2 --> P3 --> P4 --> P5 --> P6
    end

    subgraph Agent["🤖 LangGraph Agent — run_agent_stream()"]
        N1["Node: load_memory\n• Redis buffer → short_term_history\n• Qdrant 'memory' → long_term_facts"]
        N2["Node: retrieve_docs\n• Rewrite query (1→3 sub-queries)\n• Parallel vector search Qdrant\n• Dedup + sort\n• LLM Rerank → top-5"]
        N3["Stream LLM\n• Build prompt (system+history+facts+docs)\n• Gemini API stream\n• yield token chunks"]
        N1 --> N2 --> N3
    end

    P6 --> N1

    subgraph SSE["📡 SSE Events → Client"]
        E1["event: tool_call {name: load_memory}"]
        E2["event: tool_call {name: retrieve_docs}"]
        E3["event: citations [{doc_id, heading_path, score}]"]
        E4["event: token 'Theo...'"]
        E5["event: token 'Điều 12...'"]
        E6["event: done {usage, latency_ms}"]
    end

    N1 --> E1
    N2 --> E2
    N2 --> E3
    N3 --> E4
    N3 --> E5

    subgraph Post["Post-Stream"]
        Q1["INSERT message (role=assistant)\nmeta={citations}, tokens, latency_ms"]
        Q2["RPUSH assistant vào Redis buffer"]
        Q3["ARQ.enqueue('extract_facts', conv_id)\n→ async background"]
        Q1 --> Q2 --> Q3
    end

    N3 --> Q1
    Q3 --> E6

    style Agent fill:#e8f0fe,stroke:#4285f4
    style SSE fill:#e0f7fa,stroke:#00acc1
    style Post fill:#fff3e0,stroke:#fb8c00
```

---

## 7. Agent Graph — LangGraph Nodes

```mermaid
flowchart LR
    START(["▶ START"]) --> LM

    subgraph LM["load_memory\n@observe"]
        LM1["get_buffer(conv_id)\n→ LRANGE Redis"]
        LM2["retrieve_user_facts(user_id)\n→ Qdrant 'memory' vector search"]
        LM1 & LM2 --> LM3["Gắn vào AgentState:\nshort_term_history\nlong_term_facts"]
    end

    LM3 --> RD

    subgraph RD["retrieve_docs\n@observe"]
        RD1["rewrite_query()\nLLM → 1-3 sub-queries\n(cached 10 phút)"]
        RD2["asyncio.gather:\nvector_search × N queries\n← Qdrant 'docs'"]
        RD3["Dedup by point_id\ngiữ max score"]
        RD4["llm_rerank()\ntop-20 → top-5\n(cached 10 phút)"]
        RD1 --> RD2 --> RD3 --> RD4
        RD4 --> RD5["Gắn vào state:\nretrieved[]\ncitations[]"]
    end

    RD5 --> GEN

    subgraph GEN["generate\n@observe"]
        GEN1["build_answer_messages()\nsystem+summary+facts+docs+history"]
        GEN2["llm.complete_stream()\nGemini API → token chunks"]
        GEN1 --> GEN2
    end

    GEN --> END_NODE(["⏹ END"])

    style LM fill:#e8f0fe,stroke:#4285f4
    style RD fill:#f3e8ff,stroke:#7b68ee
    style GEN fill:#e8f5e9,stroke:#43a047
```

---

## 8. Cache Hierarchy — 3 Tầng

```mermaid
flowchart TB
    REQ["LLM complete() call\ntemperature = 0, no stream"] --> L1

    subgraph L1["L1 — In-Memory LRU\n(per-process, ~2000 entries)"]
        L1C{"Cache hit?"}
        L1C -->|"✅ Hit ~0.1ms"| RET1["Return cached response\n(cực nhanh)"]
        L1C -->|"❌ Miss"| L2
    end

    subgraph L2["L2 — Redis\n(shared across replicas)"]
        L2C{"Redis GET key?"}
        L2C -->|"✅ Hit ~1ms"| L2R["Parse JSON → ChatCompletion\nSet vào L1\nReturn"]
        L2C -->|"❌ Miss"| L3
    end

    subgraph L3["L3 — Gemini API\n(upstream, ~500ms-2s)"]
        L3R["POST /v1/chat/completions"]
        L3R --> L3S["Set L1 + L2\n(write-back)"]
        L3S --> RET3["Return response"]
    end

    subgraph Bypass["🚫 Cache Bypass khi:"]
        B1["temperature > 0"]
        B2["stream = True"]
        B3["tools != None"]
        B4["use_cache = False"]
    end

    subgraph TTLs["⏱️ TTL theo loại"]
        T1["LLM response: 1 giờ"]
        T2["Embedding: 7 ngày"]
        T3["Query rewrite: 10 phút"]
        T4["Rerank scores: 10 phút"]
    end

    style L1 fill:#e8f5e9,stroke:#43a047
    style L2 fill:#fff8e1,stroke:#fbc02d
    style L3 fill:#fce4ec,stroke:#e91e63
    style Bypass fill:#f5f5f5,stroke:#9e9e9e
    style TTLs fill:#f5f5f5,stroke:#9e9e9e
```

---

## 9. Long-Term Memory — Extract Facts Pipeline

```mermaid
flowchart TB
    TRIGGER["🔔 Trigger:\nchat_stream enqueue\nextract_facts(conv_id)"]

    subgraph Worker["⚙️ Worker Task"]
        W1["Fetch 20 recent messages\n← Postgres"]
        W2["Get conv → user_id, tenant_id"]
        W3["Build transcript:\n'[user] ...\n[assistant] ...'"]
        W4["LLM call (Gemini)\nExtraction prompt\n→ JSON {facts: [{key, value, confidence}]}"]

        W1 --> W2 --> W3 --> W4
    end

    TRIGGER --> W1

    W4 --> LOOP

    subgraph LOOP["🔁 Mỗi fact"]
        D1["Embed fact text\nkey: value"]
        D2["Qdrant search 'memory'\nfilter: user_id + key\ntop-1"]
        D3{"score ≥ 0.92\n(duplicate)?"}
        D3 -->|"✅ Dup"| SKIP["Skip\n(bump confidence)"]
        D3 -->|"❌ New"| SAVE

        subgraph SAVE["Dual-write"]
            S1["INSERT user_facts\n→ Postgres\n(key, value, confidence,\nqdrant_point_id)"]
            S2["Upsert point\n→ Qdrant 'memory'\n(vector + payload)"]
            S1 & S2
        end

        D1 --> D2 --> D3
    end

    subgraph NextChat["🗣️ Lần chat tiếp theo"]
        NX1["load_memory_node:\nretrieve_user_facts(user_id, query)"]
        NX2["Qdrant 'memory' search\ntop-5 facts liên quan"]
        NX3["Inject vào system prompt:\nNGỮ CẢNH NGƯỜI DÙNG:\n- user.role: luật sư\n- user.interest: M&A"]
        NX1 --> NX2 --> NX3
    end

    SAVE --> NextChat

    style Worker fill:#fff3e0,stroke:#fb8c00
    style LOOP fill:#f3e8ff,stroke:#7b68ee
    style NextChat fill:#e8f5e9,stroke:#43a047
```

---

## 10. Security & Multi-tenancy Model

```mermaid
flowchart TB
    subgraph Auth["🔑 Authentication — JWT HS256"]
        J1["POST /auth/login\n→ access token 15 phút\n+ refresh token 7 ngày"]
        J2["Mọi request:\nAuthorization: Bearer {token}"]
        J3["decode_token()\n→ TokenPayload\n{sub: user_id, tid: tenant_id, typ: access}"]
        J1 --> J2 --> J3
    end

    subgraph Isolation["🏢 Tenant Isolation — 3 lớp"]
        direction TB
        PGI["PostgreSQL:\nWHERE user_id = :uid\nhoặc tenant_id = :tid\ntrên MỌI query"]
        QDI["Qdrant:\nFilter must: tenant_id = :tid\nBẮT BUỘC — không có = lỗi"]
        MIO["MinIO:\nPath prefix:\ntenants/{tenant_id}/docs/{doc_id}/..."]
        PGI & QDI & MIO
    end

    subgraph RL["⚡ Rate Limiting"]
        RL1["Chat: 60 req / phút / user"]
        RL2["Upload: 10 file / phút / user"]
        RL3["Redis Sorted Set\nSliding window log"]
        RL1 & RL2 --> RL3
    end

    subgraph Validate["🛡️ Input Validation"]
        V1["Pydantic v2 strict mode"]
        V2["message: max 8000 chars"]
        V3["file: max 50MB"]
        V4["MIME type whitelist\nPDF, DOCX, TXT, MD"]
        V1 & V2 & V3 & V4
    end

    J3 --> Isolation
```

---

## 11. Scaling Model — Horizontal Scale

```mermaid
flowchart LR
    subgraph Bottlenecks["🔥 Bottleneck → Giải pháp"]
        direction TB
        B1["API CPU bound\nJSON serialize, Pydantic"]
        S1["↑ replicas\ndocker compose --scale api=4\nGunicorn: 2×CPU+1 workers"]

        B2["Embedding API rate limit"]
        S2["Batch embed + cache (7d)\nRetry exponential backoff"]

        B3["LLM rate limit"]
        S3["Per-user token bucket\n429 + Retry-After header"]

        B4["Worker bottleneck"]
        S4["↑ worker replicas\nmax_jobs=8 per worker\nPhân queue: ingestion/memory"]

        B5["Postgres connections"]
        S5["PgBouncer transaction pooling\npool_size=5 per API + max_overflow=10"]

        B6["Qdrant query latency"]
        S6["HNSW params tuning\nPayload index trên tenant_id\nQuantization (scalar)"]

        B1 --> S1
        B2 --> S2
        B3 --> S3
        B4 --> S4
        B5 --> S5
        B6 --> S6
    end

    subgraph Scale["📈 Scale command"]
        CMD["docker compose up\n--scale api=4\n--scale worker=3"]
    end

    style Bottlenecks fill:#fce4ec,stroke:#e91e63
    style Scale fill:#e8f5e9,stroke:#43a047
```

**Tại sao stateless = scale dễ:**

```mermaid
flowchart LR
    subgraph State["State luôn ở external store"]
        ST1["Session state → Redis"]
        ST2["Conversation history → Redis + PG"]
        ST3["Agent state → PG checkpoint"]
        ST4["Vector index → Qdrant"]
    end

    subgraph APIs["API Replicas\n(KHÔNG giữ state)"]
        A1["API-1"]
        A2["API-2"]
        A3["API-3"]
        A4["API-4"]
    end

    State <--> A1
    State <--> A2
    State <--> A3
    State <--> A4

    Note["Thêm replica = thêm capacity\nKhông cần session sticky"]

    style State fill:#e8f0fe,stroke:#4285f4
    style APIs fill:#e8f5e9,stroke:#43a047
```

---

## 12. Reliability Patterns

```mermaid
flowchart TB
    subgraph Idempotent["♻️ Idempotent Jobs"]
        I1["Mỗi job có job_id UUID"]
        I2["Worker check: doc.status == 'indexed'?"]
        I3["Nếu Yes → skip, return sớm"]
        I1 --> I2 --> I3
    end

    subgraph CircuitBreaker["⚡ Circuit Breaker (Gemini API)"]
        CB1["Bình thường: pass-through"]
        CB2["Lỗi > 5 lần / 30s\n→ Mở circuit 60s"]
        CB3["Trả lỗi nhanh\nkhông retry vô tận"]
        CB4["Sau 60s: half-open\nThử 1 request"]
        CB5{"Thành công?"}
        CB5 -->|Yes| CB1
        CB5 -->|No| CB2
        CB1 --> CB2
        CB2 --> CB3 --> CB4 --> CB5
    end

    subgraph Retry["🔄 Retry Policy (tenacity)"]
        R1["retry_if_exception_type:\nTimeoutError, ConnectionError"]
        R2["stop_after_attempt: 3"]
        R3["wait_exponential_jitter:\ninitial=1s, max=10s"]
        R1 & R2 & R3
    end

    subgraph Graceful["🛑 Graceful Shutdown"]
        G1["SIGTERM signal"]
        G2["Finish in-flight requests"]
        G3["lf_flush() Langfuse"]
        G4["Close Redis, Qdrant, DB pool"]
        G1 --> G2 --> G3 --> G4
    end

    subgraph Health["💊 Healthcheck"]
        H1["/health/live\nProcess còn sống?"]
        H2["/health/ready\nDeps OK?\n• PG SELECT 1\n• Redis PING\n• Qdrant GET /"]
        H1 & H2
    end
```

---

## 13. Tại sao Agentic, không phải Plain RAG?

```mermaid
flowchart TB
    subgraph PlainRAG["❌ Plain RAG — Thất bại với:"]
        P1["Câu hỏi multi-hop:\n'So sánh Điều 12 và Điều 15'"]
        P2["Câu hỏi cần clarify:\n'Quyền đó có áp dụng không?'"]
        P3["Multi-source:\n'Luật A + Nghị định B nói gì?'"]
        P4["1 query → 1 retrieve → 1 generate\nKhông đủ recall cho câu hỏi phức tạp"]
        P1 & P2 & P3 --> P4
    end

    subgraph AgenticRAG["✅ Agentic RAG — LangGraph giải:"]
        A1["Query Rewriting:\n1 câu → 3 sub-queries\n→ tăng recall"]
        A2["Parallel Retrieval:\n3 queries song song\ndedup + rerank"]
        A3["Long-term Memory:\nUser context inject\nvào system prompt"]
        A4["Self-reflection:\nĐủ info chưa?\nNếu không → retrieve thêm"]
        A5["Checkpointing:\nState persist Postgres\nResume nếu crash"]
        A1 --> A2 --> A3 --> A4 --> A5
    end

    subgraph Tradeoff["⚖️ Trade-off"]
        T1["Latency cao hơn plain RAG\n(~1.5-2.5s vs ~0.5s)"]
        T2["Token cost cao hơn\n(rewrite + rerank + generate)"]
        T3["Bù bằng:\nCache aggressively\nReranker dùng Flash (rẻ)"]
        T1 & T2 --> T3
    end

    PlainRAG --> AgenticRAG --> Tradeoff
```

---

## 14. Component Decision Map

```mermaid
quadrantChart
    title Component Choices (Self-host-ability vs Performance)
    x-axis "Khó self-host" --> "Dễ self-host"
    y-axis "Performance thấp" --> "Performance cao"
    quadrant-1 "Ideal: Dễ host + Fast"
    quadrant-2 "Chấp nhận: Khó nhưng nhanh"
    quadrant-3 "Tránh: Khó + chậm"
    quadrant-4 "Trade-off: Dễ nhưng chậm"

    FastAPI: [0.85, 0.9]
    Qdrant: [0.75, 0.88]
    Redis: [0.9, 0.85]
    PostgreSQL: [0.8, 0.75]
    MinIO: [0.8, 0.7]
    Langfuse: [0.65, 0.72]
    ARQ: [0.9, 0.65]
    LangGraph: [0.7, 0.8]
```

---

## 15. Tham chiếu tài liệu

| Doc | Nội dung |
|-----|---------|
| [01-architecture-overview.md](./01-architecture-overview.md) | Bản text gốc + giải thích chi tiết |
| [02-data-flow.md](./02-data-flow.md) | Sequence diagrams từng flow |
| [13-database-design.md](./13-database-design.md) | Schema DB chi tiết |
| [18-chat-request-workflow.md](./18-chat-request-workflow.md) | Chat workflow step-by-step |
| [19-database-operations-deep-dive.md](./19-database-operations-deep-dive.md) | Session, transaction, Redis ops |
| [20-langfuse-observability.md](./20-langfuse-observability.md) | Langfuse trace, spans, scoring |
