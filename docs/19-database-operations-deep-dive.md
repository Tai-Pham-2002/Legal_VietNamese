# 19 — Database Operations Deep Dive

Tài liệu này giải thích **chi tiết** cách hệ thống đọc/ghi dữ liệu với PostgreSQL, Redis, và Qdrant —
bao gồm connection management, transaction patterns, và data lifecycle.

---

## 1. Tổng quan — Vai trò mỗi Database

```mermaid
flowchart TB
    subgraph PostgreSQL["PostgreSQL — Source of Truth"]
        PG1["users / tenants"]
        PG2["conversations"]
        PG3["messages"]
        PG4["documents / document_chunks"]
        PG5["user_facts (long-term memory)"]
        PG6["LangGraph checkpoints"]
    end

    subgraph Redis["Redis — Hot Cache & Queue"]
        R1["Short-term buffer (conv:buf:*)"]
        R2["LLM response cache"]
        R3["Embedding cache"]
        R4["Rate limit counters (rl:*)"]
        R5["ARQ job queue"]
        R6["Pub/Sub channels"]
    end

    subgraph Qdrant["Qdrant — Vector Store"]
        Q1["Collection 'docs'<br/>Document chunk embeddings"]
        Q2["Collection 'memory'<br/>User fact embeddings"]
    end

    PG3 -.->|warmup nếu Redis miss| R1
    PG5 -.->|embedding dual-write| Q2
    PG4 -.->|embedding dual-write| Q1

    style PostgreSQL fill:#e8f0fe,stroke:#4285f4
    style Redis fill:#fef7e0,stroke:#fbbc04
    style Qdrant fill:#e8f5e9,stroke:#34a853
```

---

## 2. PostgreSQL — Connection & Session Management

### 2.1. Engine Singleton

```mermaid
flowchart LR
    A["get_engine()"] --> B{"_engine is None?"}
    B -->|Yes| C["create_async_engine(<br/>postgres_dsn,<br/>pool_size=5,<br/>max_overflow=10,<br/>pool_pre_ping=True)"]
    C --> D["_engine = engine"]
    B -->|No| D
    D --> E["Return _engine"]
```

**File**: `src/core/db.py`

**Cấu hình quan trọng**:

| Parameter | Giá trị | Tại sao |
|-----------|---------|---------|
| `pool_size` | 5 | Số connection thường trực. Nhỏ vì có PgBouncer phía trước |
| `max_overflow` | 10 | Connection thêm khi pool đầy. Tổng max = 15/process |
| `pool_timeout` | 30s | Thời gian chờ lấy connection từ pool |
| `pool_pre_ping` | True | Kiểm tra connection sống trước khi dùng (detect stale conn) |
| `expire_on_commit` | False | Sau commit, object ORM vẫn dùng được (không cần reload) |
| `autoflush` | False | Tránh auto-flush gây side effect; flush tường minh |

### 2.2. Session Patterns — 2 kiểu

#### Pattern 1: FastAPI Dependency (`get_session`)

```mermaid
sequenceDiagram
    participant FastAPI as FastAPI Request
    participant Dep as get_session() dependency
    participant Pool as Connection Pool
    participant PG as PostgreSQL

    FastAPI->>Dep: Inject session
    Dep->>Pool: Checkout connection
    Pool->>PG: SELECT 1 (pre_ping)
    PG-->>Pool: OK
    Pool-->>Dep: Connection
    Dep->>Dep: Create AsyncSession

    Note over Dep,FastAPI: yield session<br/>(request handler sử dụng session)

    alt No exception
        Note over Dep: Session auto-close<br/>(KHÔNG auto-commit, handler tự commit)
    else Exception raised
        Dep->>Dep: session.rollback()
    end

    Dep->>Pool: Return connection
```

**Dùng khi**: Request handler cần kiểm soát commit timing (ví dụ: persist user message rồi mới chạy agent).

```python
# src/api/routes/chat.py
async def chat_stream(session: SessionDep):
    repo = ConversationRepo(session)
    user_msg = await repo.add_message(...)
    await session.commit()    # ← Handler tự commit
```

#### Pattern 2: Context Manager (`session_scope`)

```mermaid
sequenceDiagram
    participant Code as Business Logic
    participant Scope as session_scope()
    participant Pool as Connection Pool
    participant PG as PostgreSQL

    Code->>Scope: async with session_scope() as session:
    Scope->>Pool: Checkout connection
    Pool-->>Scope: Connection
    Scope->>Scope: Create AsyncSession

    Note over Code,Scope: yield session<br/>(code sử dụng session)

    alt No exception
        Scope->>PG: COMMIT
    else Exception raised
        Scope->>PG: ROLLBACK
        Scope->>Scope: Re-raise exception
    end

    Scope->>Pool: Return connection
```

**Dùng khi**: Code ngoài request context (worker, background tasks, SSE generator).

```python
# src/api/routes/chat.py — trong SSE generator
async with session_scope() as s2:
    await ConversationRepo(s2).add_message(
        conversation_id=conv_id,
        role="assistant",
        content=full_answer,
    )
# Auto-commit ở đây
```

### 2.3. Tại sao 2 pattern?

| Scenario | Pattern | Lý do |
|----------|---------|-------|
| API route handler | `get_session` (dependency) | FastAPI inject, handler control commit timing |
| SSE generator (async iterator) | `session_scope()` | Generator chạy sau response trả, session gốc có thể expired |
| Worker task | `session_scope()` | Worker không có FastAPI dependency injection |
| Healthcheck | `session_scope()` | Standalone, cần auto-commit/rollback |

---

## 3. PostgreSQL — CRUD Operations chi tiết

### 3.1. Conversation Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Created : POST /v1/conversations
    Created --> Active : Có message đầu tiên
    Active --> Active : Thêm message
    Active --> Summarized : Buffer > N messages → rolling summary
    Summarized --> Active : Message mới tiếp tục
    Active --> Archived : User archive
    Archived --> [*]
```

#### Tạo Conversation

```sql
INSERT INTO conversations (id, tenant_id, user_id, title, message_count, archived)
VALUES (gen_random_uuid(), :tenant_id, :user_id, 'New conversation', 0, false);
```

#### Thêm Message

```python
async def add_message(self, *, conversation_id, role, content, meta=None, 
                      tokens_in=None, tokens_out=None, latency_ms=None) -> Message:
    # 1) INSERT message
    m = Message(conversation_id=conversation_id, role=role, content=content,
                meta=meta or {}, tokens_in=tokens_in, tokens_out=tokens_out,
                latency_ms=latency_ms)
    self.s.add(m)
    
    # 2) UPDATE conversation counters (trong cùng transaction)
    conv = await self.s.get(Conversation, conversation_id)
    if conv:
        conv.message_count = (conv.message_count or 0) + 1
        conv.last_message_at = datetime.now(UTC)
    
    # 3) FLUSH (ghi xuống DB nhưng chưa commit)
    await self.s.flush()
    return m
```

**Diagram ghi Message**:

```mermaid
sequenceDiagram
    participant Code as Route Handler
    participant Repo as ConversationRepo
    participant Session as AsyncSession
    participant PG as PostgreSQL

    Code->>Repo: add_message(role="user", content="...")
    Repo->>Session: session.add(Message(...))
    Repo->>Session: session.get(Conversation, conv_id)
    Session->>PG: SELECT * FROM conversations WHERE id = ?
    PG-->>Session: Conversation ORM object
    Repo->>Repo: conv.message_count += 1<br/>conv.last_message_at = now()
    Repo->>Session: session.flush()
    Session->>PG: BEGIN
    Session->>PG: INSERT INTO messages (...)
    Session->>PG: UPDATE conversations SET message_count = ?, last_message_at = ?
    Note over Session,PG: Chưa COMMIT — caller sẽ commit

    Code->>Session: session.commit()
    Session->>PG: COMMIT
```

### 3.2. Message Schema — Lưu những gì

```mermaid
erDiagram
    CONVERSATIONS {
        uuid id PK
        uuid tenant_id FK
        uuid user_id FK
        varchar title
        text summary "Rolling summary khi buffer dài"
        int message_count
        bool archived
        timestamptz last_message_at
        timestamptz created_at
        timestamptz updated_at
    }

    MESSAGES {
        uuid id PK
        uuid conversation_id FK
        varchar role "user | assistant | system | tool"
        text content "Full text"
        jsonb meta "citations, tool_calls, model info"
        int tokens_in "Prompt tokens (assistant only)"
        int tokens_out "Completion tokens (assistant only)"
        float latency_ms "LLM latency (assistant only)"
        timestamptz created_at
        timestamptz updated_at
    }

    USER_FACTS {
        uuid id PK
        uuid user_id FK
        uuid tenant_id FK
        varchar key "e.g. user.role"
        text value "e.g. luật sư"
        float confidence "0.0 - 1.0"
        uuid_array source_message_ids "Truy ngược message gốc"
        uuid qdrant_point_id "Link sang Qdrant"
        timestamptz created_at
        timestamptz updated_at
    }

    CONVERSATIONS ||--o{ MESSAGES : has
    CONVERSATIONS }o--|| USERS : belongs_to
    USER_FACTS }o--|| USERS : belongs_to
```

### 3.3. Indexes — Tại sao

```sql
-- Liệt kê conversation của user, sort by updated
CREATE INDEX ix_conv_user_updated ON conversations (user_id, updated_at);

-- Filter theo tenant (admin queries)
CREATE INDEX ix_conv_tenant ON conversations (tenant_id);

-- Lấy messages theo conversation + thời gian (cho pagination, recent_messages)
CREATE INDEX ix_msg_conv_created ON messages (conversation_id, created_at);

-- Lấy facts của user (memory retrieval fallback)
CREATE INDEX ix_fact_user ON user_facts (user_id);

-- Dedupe facts (cùng user + cùng key)
CREATE INDEX ix_fact_user_key ON user_facts (user_id, key);
```

---

## 4. Redis — Operations chi tiết

### 4.1. Short-Term Buffer Operations

```mermaid
flowchart TB
    subgraph Write["Ghi Message vào Buffer"]
        W1["RPUSH conv:buf:{id} '{role, content}'"]
        W2["LTRIM conv:buf:{id} -40 -1<br/>(giữ 40 entries cuối = 20 turn)"]
        W3["EXPIRE conv:buf:{id} 86400<br/>(24h TTL)"]
        W1 --> W2 --> W3
        Note1["3 commands trong 1 pipeline<br/>→ 1 RTT tới Redis"]
    end

    subgraph Read["Đọc Buffer"]
        R1["LRANGE conv:buf:{id} 0 -1"]
        R2["Parse mỗi entry: orjson.loads()"]
        R3["Return ShortTermBuffer(messages=[...])"]
        R1 --> R2 --> R3
    end

    subgraph Warmup["Warmup từ DB"]
        WU1["Buffer rỗng?<br/>(Redis mới restart)"]
        WU2["SELECT messages<br/>ORDER BY created_at DESC<br/>LIMIT 20"]
        WU3["Pipeline:<br/>DELETE key<br/>RPUSH × N<br/>EXPIRE"]
        WU1 -->|Yes| WU2 --> WU3
    end
```

### 4.2. Cache Operations (LLM & Embedding)

```mermaid
flowchart TB
    A["LLM complete() được gọi"] --> B["Tạo cache_key:<br/>sha256(model + messages + params)"]
    
    B --> C{"L1: LRU in-memory<br/>(per-process, 2000 entries)"}
    C -->|Hit| D["Return cached response"]
    C -->|Miss| E{"L2: Redis GET key"}
    E -->|Hit| F["Parse JSON → ChatCompletion<br/>Set vào L1<br/>Return"]
    E -->|Miss| G["L3: Gọi Gemini API"]
    G --> H["Response"]
    H --> I["L1: lru.set(key, response)"]
    H --> J["L2: Redis SET key value EX ttl"]
    I --> K["Return response"]
    J --> K

    subgraph CachePolicy["Cache Policy"]
        P1["LLM response: TTL = 3600s (1h)"]
        P2["Embedding: TTL = 604800s (7 ngày)"]
        P3["Query rewrite: TTL = 600s (10 phút)"]
        P4["Rerank: TTL = 600s (10 phút)"]
    end

    style CachePolicy fill:#f9f9f9,stroke:#999
```

**Khi nào KHÔNG cache?**

| Condition | Cache? | Lý do |
|-----------|--------|-------|
| `temperature > 0` | ❌ | Non-deterministic, kết quả khác nhau mỗi lần |
| `stream = True` | ❌ | UX cần token ngay, không chờ cache |
| `tools != None` | ❌ | Tool calling responses phụ thuộc context |
| `use_cache = False` | ❌ | Caller yêu cầu bypass (eval, debug) |

### 4.3. Rate Limit — Sliding Window

```mermaid
sequenceDiagram
    participant API as Rate Limit Check
    participant Redis as Redis Sorted Set

    Note over API: Key: rl:{user_id}:chat<br/>Limit: 60 req / 60s window

    API->>Redis: Pipeline bắt đầu
    API->>Redis: ZREMRANGEBYSCORE key 0 (now - 60s)<br/>→ Xóa entries cũ hơn window
    API->>Redis: ZCARD key<br/>→ Đếm entries còn lại
    API->>Redis: ZADD key {uuid: now}<br/>→ Thêm request hiện tại
    API->>Redis: EXPIRE key 60<br/>→ TTL = window size
    Redis-->>API: [_, count, _, _]

    alt count < 60
        API->>API: ✅ Cho phép request
    else count >= 60
        API->>API: ❌ HTTP 429 Too Many Requests<br/>Retry-After: ceil(oldest_entry_age)
    end
```

### 4.4. ARQ Job Queue

```mermaid
flowchart LR
    subgraph Producer["API (Producer)"]
        A["arq.enqueue_job('extract_facts', conv_id_str)"]
    end

    subgraph Redis["Redis"]
        B["Stream: arq:queue:default<br/>Job data: function name + args + job_id"]
    end

    subgraph Consumer["Worker (Consumer)"]
        C["ARQ Worker poll job từ stream"]
        D["Gọi extract_facts(ctx, conv_id_str)"]
        E["Job result → Redis (TTL 3600s)"]
    end

    A --> B --> C --> D --> E
```

---

## 5. Qdrant — Vector Operations chi tiết

### 5.1. Collection Schema

```mermaid
flowchart TB
    subgraph DocsCollection["Collection: 'docs'"]
        DC["Vector: 768 dims (Gemini text-embedding-004)"]
        DP["Payload:<br/>• tenant_id (indexed)<br/>• user_id (indexed)<br/>• doc_id<br/>• doc_title<br/>• chunk_index<br/>• heading_path<br/>• page_from / page_to<br/>• text<br/>• n_tokens"]
    end

    subgraph MemoryCollection["Collection: 'memory'"]
        MC["Vector: 768 dims (Gemini text-embedding-004)"]
        MP["Payload:<br/>• user_id (indexed)<br/>• tenant_id<br/>• key<br/>• value<br/>• confidence"]
    end

    style DocsCollection fill:#e8f0fe,stroke:#4285f4
    style MemoryCollection fill:#f3e8ff,stroke:#7b68ee
```

### 5.2. Search Operation — Cách filter multi-tenant

```mermaid
flowchart TB
    A["vector_search(query, tenant_id, user_id?, doc_ids?)"]
    
    A --> B["Embed query → vector 768d"]
    B --> C["Build filter"]
    
    C --> D["must:<br/>tenant_id = {tid} (BẮT BUỘC)"]
    
    C --> E{"user_id?"}
    E -->|Yes| F["must: + user_id = {uid}"]
    E -->|No| G["Bỏ qua"]
    
    C --> H{"doc_ids?"}
    H -->|Yes| I["must: + doc_id IN [...]"]
    H -->|No| J["Bỏ qua"]

    D --> K["Qdrant search(<br/>collection='docs',<br/>query_vector=vector,<br/>limit=20,<br/>filter=Filter(must=[...]),<br/>with_payload=True)"]
    F --> K
    G --> K
    I --> K
    J --> K

    K --> L["Return list[SearchHit]"]
```

**Tại sao filter `tenant_id` BẮT BUỘC?**
- Multi-tenancy isolation: user thuộc tenant A **không bao giờ** thấy docs của tenant B.
- Qdrant index trên `tenant_id` payload → filter trước khi HNSW search → nhanh.

### 5.3. Dual Write Pattern (Postgres + Qdrant)

Khi lưu user facts (long-term memory), hệ thống ghi cả 2 nơi:

```mermaid
sequenceDiagram
    participant Code as save_fact()
    participant Emb as EmbeddingClient
    participant QD as Qdrant
    participant PG as PostgreSQL

    Code->>Emb: embed(["user.role: luật sư"])
    Emb-->>Code: vector (768d)

    Note over Code: Dedupe check

    Code->>QD: search(collection='memory',<br/>query_vector=vector,<br/>filter={user_id, key},<br/>limit=1)
    QD-->>Code: hits

    alt hits[0].score >= 0.92
        Code->>Code: Skip (duplicate fact)
    else New fact
        Code->>Code: point_id = uuid4()
        
        Code->>PG: session_scope() → INSERT INTO user_facts<br/>(user_id, tenant_id, key, value,<br/>confidence, qdrant_point_id=point_id)
        PG-->>Code: COMMIT OK
        
        Code->>QD: upsert(collection='memory',<br/>points=[PointStruct(id=point_id,<br/>vector=vector, payload={...})],<br/>wait=True)
        QD-->>Code: OK
    end
```

**Consistency trade-off**:
- Postgres commit trước Qdrant upsert → nếu Qdrant fail, fact ở Postgres nhưng không searchable.
- Chấp nhận được vì: (1) Qdrant ít khi fail, (2) fact vẫn ở Postgres, (3) retry worker có thể sync lại.

---

## 6. Data Lifecycle — Message từ lúc sinh đến lúc dùng

```mermaid
flowchart TB
    subgraph T0["T=0: User gửi message"]
        A1["Client → API"]
    end

    subgraph T1["T=0.02s: Persist"]
        B1["INSERT INTO messages<br/>(role='user', content='...')"]
        B2["UPDATE conversations<br/>(message_count++, last_message_at)"]
        B3["RPUSH conv:buf:{id}"]
    end

    subgraph T2["T=0.04s: Agent đọc"]
        C1["LRANGE conv:buf:{id}<br/>→ short_term_history"]
        C2["Qdrant search 'memory'<br/>→ long_term_facts"]
    end

    subgraph T3["T=0.8-2.5s: Generate"]
        D1["Messages list = system + history + user_msg<br/>→ Gemini API stream"]
    end

    subgraph T4["T=2.5s: Persist response"]
        E1["INSERT INTO messages<br/>(role='assistant', content='...',<br/>meta={citations}, tokens_in, tokens_out, latency_ms)"]
        E2["RPUSH conv:buf:{id}<br/>('assistant', full_answer)"]
    end

    subgraph T5["T=2.5s+: Background"]
        F1["ARQ job: extract_facts"]
        F2["Fetch 20 recent messages từ Postgres"]
        F3["LLM trích xuất facts"]
        F4["Dedupe + INSERT user_facts"]
        F5["Upsert Qdrant 'memory'"]
    end

    subgraph T_Next["Lần chat tiếp theo"]
        G1["load_memory_node đọc buffer Redis"]
        G2["retrieve_user_facts từ Qdrant 'memory'"]
        G3["Facts inject vào system prompt"]
    end

    T0 --> T1 --> T2 --> T3 --> T4 --> T5
    T5 -.->|async| T_Next

    style T5 fill:#fff3e0,stroke:#ff9800
```

---

## 7. Transaction Boundaries — Ai commit ở đâu

```mermaid
flowchart TB
    subgraph TX1["Transaction 1: Persist user message"]
        A["ConversationRepo(session).add_message(role='user')"]
        B["session.commit()"]
        A --> B
        Note1["Session từ FastAPI dependency<br/>Commit tường minh bởi route handler"]
    end

    subgraph TX2["Transaction 2: Persist assistant message"]
        C["async with session_scope() as s2:"]
        D["ConversationRepo(s2).add_message(role='assistant')"]
        E["Auto-commit khi exit context manager"]
        C --> D --> E
        Note2["Session MỚI (session_scope)<br/>Auto-commit/rollback"]
    end

    subgraph TX3["Transaction 3: Extract facts (worker)"]
        F["async with session_scope() as session:"]
        G["ConversationRepo.recent_messages()"]
        H["session.get(Conversation, conv_id)"]
        I["Auto-commit"]
        F --> G --> H --> I
        Note3["Worker context<br/>Chỉ đọc"]
    end

    subgraph TX4["Transaction 4: Save fact (worker)"]
        J["async with session_scope() as session:"]
        K["UserFactRepo.add(...)"]
        L["Auto-commit"]
        J --> K --> L
        Note4["Mỗi fact 1 transaction<br/>Nếu 1 fact fail, các fact khác không bị ảnh hưởng"]
    end

    TX1 --> TX2
    TX2 -.->|async enqueue| TX3
    TX3 --> TX4

    style TX1 fill:#e8f4f8,stroke:#4a90d9
    style TX2 fill:#e8f4f8,stroke:#4a90d9
    style TX3 fill:#fff3e0,stroke:#ff9800
    style TX4 fill:#fff3e0,stroke:#ff9800
```

---

## 8. Tham chiếu Code

| Component | File | Class/Function |
|-----------|------|----------------|
| Engine + Session | `src/core/db.py` | `get_engine()`, `session_scope()`, `get_session()` |
| Conversation CRUD | `src/db/repositories/conversation.py` | `ConversationRepo` |
| Memory CRUD | `src/db/repositories/memory.py` | `UserFactRepo` |
| Conversation Model | `src/db/models/conversation.py` | `Conversation`, `Message` |
| Memory Model | `src/db/models/memory.py` | `UserFact` |
| Redis Buffer | `src/memory/short_term.py` | `append_message()`, `get_buffer()`, `warmup_from_db()` |
| Redis Cache | `src/core/redis.py` | `cache_get()`, `cache_set()`, `make_key()` |
| LLM Cache | `src/llm/client.py` | `LLMClient.complete()` (cache logic inline) |
| Qdrant Search | `src/retrieval/search.py` | `vector_search()` |
| Qdrant Memory | `src/memory/long_term.py` | `save_fact()`, `retrieve_user_facts()` |
| Rate Limit | `src/cache/rate_limit.py` | `allow_request()` |
