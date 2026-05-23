# 18 — Chat Request Workflow (Chi tiết từng bước)

Tài liệu này mô tả **chi tiết từng bước** xảy ra khi một user gửi message chat,
từ lúc HTTP request chạm API cho đến khi SSE trả token cuối cùng về client.

Mỗi bước được giải thích: **làm gì**, **ở đâu trong code**, **dữ liệu đi qua đâu**, và **tại sao**.

---

## 1. Tổng quan — Lifecycle Diagram

```mermaid
flowchart TB
    subgraph Client
        A["Client gửi POST /v1/chat/{conv_id}/messages"]
    end

    subgraph API["FastAPI API Layer"]
        B["RequestContextMiddleware<br/>gắn request_id + structlog"]
        C["Auth: decode JWT → user_id, tenant_id"]
        D["Rate Limit: Redis ZSET sliding window<br/>60 req/phút/user"]
        E["Validate conversation thuộc user<br/>SELECT conversations WHERE id = conv_id AND user_id = uid"]
        F["Persist user message → Postgres<br/>INSERT INTO messages"]
        G["Redis buffer warmup nếu cần<br/>LRANGE conv:buf:{conv_id}"]
        H["Append message vào Redis buffer<br/>RPUSH + LTRIM"]
    end

    subgraph Agent["LangGraph Agent Pipeline"]
        I["Node 1: load_memory<br/>Redis buffer + Qdrant memory"]
        J["Node 2: retrieve_docs<br/>Rewrite → Search → Rerank"]
        K["Stream LLM tokens<br/>Gemini API → SSE events"]
    end

    subgraph PostStream["Post-Stream Processing"]
        L["Persist assistant message → Postgres<br/>INSERT INTO messages + meta"]
        M["Append assistant vào Redis buffer"]
        N["Enqueue extract_facts → ARQ/Redis"]
    end

    subgraph Observability
        O["Langfuse trace toàn bộ pipeline"]
    end

    A --> B --> C --> D --> E --> F --> G --> H
    H --> I --> J --> K
    K --> L --> M --> N
    I -.->|"@observe"| O
    J -.->|"@observe"| O
    K -.->|"manual_generation"| O
```

---

## 2. Bước 1 — HTTP Request đến API

### 2.1. Request Format

```http
POST /v1/chat/550e8400-e29b-41d4-a716-446655440000/messages
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9...
Content-Type: application/json

{
  "message": "Điều 12 Luật Doanh nghiệp 2020 quy định gì?",
  "doc_ids": null
}
```

**Schema** (file `src/api/schemas/chat.py`):
```python
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    doc_ids: list[uuid.UUID] | None = None
```

### 2.2. Middleware Pipeline

```mermaid
sequenceDiagram
    participant C as Client
    participant MW as RequestContextMiddleware
    participant CORS as CORSMiddleware
    participant Prom as PrometheusInstrumentator
    participant Route as chat_stream()

    C->>MW: POST /v1/chat/{conv_id}/messages
    MW->>MW: Tạo request_id (uuid hoặc từ X-Request-ID header)
    MW->>MW: bind_contextvars(request_id, path, method)
    MW->>CORS: Forward request
    CORS->>CORS: Kiểm tra Origin, thêm CORS headers
    CORS->>Prom: Forward request
    Prom->>Prom: Start timer (cho /metrics endpoint)
    Prom->>Route: Forward request
    Route-->>Prom: EventSourceResponse
    Prom-->>CORS: Response + metrics
    CORS-->>MW: Response + CORS headers
    MW->>MW: Log request_done (status, elapsed_ms)
    MW->>MW: clear_contextvars()
    MW-->>C: SSE Stream
```

**File**: `src/api/middleware/request_context.py`

Mỗi request được gắn `request_id` → tất cả log trong request đó đều có field `request_id`, giúp trace qua nhiều component.

---

## 3. Bước 2 — Authentication & Authorization

```mermaid
flowchart LR
    A["Authorization header<br/>Bearer eyJhbG..."] --> B["_bearer(): tách token string"]
    B --> C["decode_token(token)<br/>jwt.decode() với secret_key + HS256"]
    C --> D["TokenPayload<br/>sub=user_id, tid=tenant_id, typ=access"]
    D --> E{"typ == 'access'?"}
    E -->|No| F["HTTP 401: wrong token type"]
    E -->|Yes| G["UserRepo.by_id(user_id)<br/>SELECT * FROM users WHERE id = user_id"]
    G --> H{"user exists<br/>AND is_active?"}
    H -->|No| I["HTTP 401: user inactive"]
    H -->|Yes| J["Return (user_id, tenant_id)"]
```

**Files**:
- `src/api/deps.py` — `current_user()` dependency
- `src/core/security.py` — `decode_token()`, JWT encode/decode

**Chi tiết JWT payload**:
```json
{
  "sub": "user-uuid",
  "tid": "tenant-uuid",
  "typ": "access",
  "jti": "unique-token-id",
  "iat": 1716480000,
  "exp": 1716480900
}
```

---

## 4. Bước 3 — Rate Limiting

```mermaid
flowchart LR
    A["user_id + bucket='chat'"] --> B["Redis key: rl:{user_id}:chat"]
    B --> C["Pipeline:<br/>1. ZREMRANGEBYSCORE purge cũ<br/>2. ZCARD đếm<br/>3. ZADD thêm request<br/>4. EXPIRE set TTL"]
    C --> D{"count < 60?"}
    D -->|Yes| E["Tiếp tục"]
    D -->|No| F["HTTP 429: rate limited<br/>Retry-After header"]
```

**File**: `src/cache/rate_limit.py`

Rate limit dùng **Sorted Set** trong Redis — mỗi request là 1 member có score = timestamp. Trước mỗi check, xóa entries cũ hơn window (60s), đếm còn bao nhiêu.

---

## 5. Bước 4 — Validate Conversation & Persist User Message

### 5.1. Sequence chi tiết

```mermaid
sequenceDiagram
    participant API as chat_stream()
    participant Repo as ConversationRepo
    participant PG as PostgreSQL
    participant Redis as Redis

    API->>Repo: repo.get(conv_id, user_id=user_id)
    Repo->>PG: SELECT * FROM conversations<br/>WHERE id = conv_id AND user_id = user_id
    PG-->>Repo: Conversation | None
    
    alt conv is None
        Repo-->>API: None
        API-->>API: HTTP 404 "conversation not found"
    end

    Note over API: Lấy conv.summary (tóm tắt hội thoại trước)

    API->>Repo: repo.add_message(conv_id, role="user", content=message)
    Repo->>PG: INSERT INTO messages (id, conversation_id, role, content, meta)
    Repo->>PG: UPDATE conversations SET message_count += 1, last_message_at = now()
    Repo->>PG: FLUSH (chưa commit)
    API->>PG: session.commit()

    Note over API: User message đã persisted — nếu crash sau đây, message không mất

    API->>Redis: get_buffer(conv_id) → LRANGE conv:buf:{conv_id}
    
    alt Buffer rỗng (Redis mới restart)
        API->>Repo: repo.recent_messages(conv_id, n=20)
        Repo->>PG: SELECT * FROM messages WHERE conv_id = ? ORDER BY created_at DESC LIMIT 20
        PG-->>Repo: List[Message]
        API->>Redis: warmup_from_db(conv_id, messages)<br/>DELETE → RPUSH × N → EXPIRE
    end

    API->>Redis: append_message(conv_id, "user", message)<br/>RPUSH + LTRIM(-buffer_size, -1) + EXPIRE
```

### 5.2. Tại sao Persist trước khi chạy Agent?

| Lý do | Giải thích |
|-------|-----------|
| **Durability** | Nếu agent crash giữa chừng, message user không mất. User có thể retry. |
| **Consistency** | `message_count` và `last_message_at` cập nhật ngay, conversation list hiển thị đúng. |
| **Buffer integrity** | Redis buffer cần có message user TRƯỚC khi agent đọc history. |

### 5.3. Redis Buffer — Cấu trúc dữ liệu

```
Key: conv:buf:{conv_id}
Type: List
Content: [
  '{"role":"user","content":"Xin chào"}',
  '{"role":"assistant","content":"Chào bạn! Tôi có thể giúp gì?"}',
  '{"role":"user","content":"Điều 12 Luật Doanh nghiệp 2020 quy định gì?"}',
]
TTL: 86400s (24h sau message cuối)
Max size: LTRIM giữ N entries cuối (default: 40 = 20 turn)
```

**File**: `src/memory/short_term.py`

---

## 6. Bước 5 — Agent Pipeline (LangGraph)

### 6.1. Graph Topology

```mermaid
graph LR
    START(["START"]) --> LM["load_memory"]
    LM --> RD["retrieve_docs"]
    RD --> GEN["generate"]
    GEN --> END_NODE(["END"])

    style LM fill:#4a90d9,color:#fff
    style RD fill:#7b68ee,color:#fff
    style GEN fill:#2ecc71,color:#fff
```

**File**: `src/agent/graph.py` — `build_graph()`

> **Quan trọng**: Trong streaming mode (`run_agent_stream`), graph KHÔNG chạy qua LangGraph runtime. Thay vào đó, code gọi từng node thủ công rồi stream LLM trực tiếp. Lý do: LangGraph stream callback + SSE integration phức tạp, tách ra rõ ràng hơn.

### 6.2. Streaming Runner — Từng bước

```mermaid
sequenceDiagram
    participant API as chat_stream()
    participant Runner as run_agent_stream()
    participant MemNode as load_memory_node
    participant RetNode as retrieve_docs_node
    participant LLM as Gemini API
    participant Client as SSE Client

    API->>Runner: run_agent_stream(message, conv_id, user_id, tenant_id, summary)
    
    Runner->>Client: yield {"type":"tool_call","name":"load_memory"}
    Runner->>MemNode: load_memory_node(state)
    Note over MemNode: (Chi tiết ở Section 7)
    MemNode-->>Runner: state + short_term_history + long_term_facts

    Runner->>Client: yield {"type":"tool_call","name":"retrieve_docs"}
    Runner->>RetNode: retrieve_docs_node(state)
    Note over RetNode: (Chi tiết ở Section 8)
    RetNode-->>Runner: state + retrieved[] + citations[]

    Runner->>Client: yield {"type":"citations","data":[...]}

    Note over Runner: Build prompt messages từ:<br/>system prompt + summary + facts + retrieved + history

    Runner->>LLM: complete_stream(messages, temperature=0.2)
    
    loop Mỗi chunk từ LLM
        LLM-->>Runner: ChatCompletionChunk (delta.content)
        Runner->>Client: yield {"type":"token","data":"..."}
    end

    LLM-->>Runner: Final chunk (usage info)
    Runner->>Client: yield {"type":"done","data":{"answer":"...","usage":{...}}}
```

### 6.3. AgentState — Dữ liệu chảy qua pipeline

```mermaid
classDiagram
    class AgentState {
        +str user_message
        +UUID conversation_id
        +UUID user_id
        +UUID tenant_id
        +list~dict~ short_term_history
        +str|None summary
        +list~dict~ long_term_facts
        +list~str~ rewritten_queries
        +list~RetrievedDoc~ retrieved
        +list~Citation~ citations
        +str answer
        +int iteration
        +bool needs_more_info
        +str|None error
    }

    class RetrievedDoc {
        +str doc_id
        +str chunk_id
        +str doc_title
        +str|None heading_path
        +int|None page_from
        +int|None page_to
        +str text
        +float score
    }

    class Citation {
        +str doc_id
        +str chunk_id
        +str doc_title
        +str|None heading_path
        +int|None page_from
        +int|None page_to
        +float score
    }

    AgentState --> RetrievedDoc : retrieved[]
    AgentState --> Citation : citations[]
```

**File**: `src/agent/state.py`

---

## 7. Bước 6 — Node: Load Memory (Chi tiết)

```mermaid
flowchart TB
    subgraph ShortTerm["Short-Term Memory (Redis)"]
        A["get_buffer(conv_id)<br/>LRANGE conv:buf:{conv_id} 0 -1"] --> B["Parse JSON → ShortTermBuffer"]
        B --> C["to_chat_format()<br/>→ list of {role, content}"]
    end

    subgraph LongTerm["Long-Term Memory (Qdrant)"]
        D["retrieve_user_facts(user_id, query, top_k=5)"]
        D --> E["Embed query → Gemini Embedding API"]
        E --> F["Qdrant search collection='memory'<br/>filter: user_id = {user_id}"]
        F --> G["Top-5 facts: {key, value, confidence, score}"]
    end

    C --> H["Gắn vào state.short_term_history"]
    G --> I["Gắn vào state.long_term_facts"]

    style ShortTerm fill:#e8f4f8,stroke:#4a90d9
    style LongTerm fill:#f3e8ff,stroke:#7b68ee
```

**Files**:
- `src/agent/nodes/memory.py` — `load_memory_node()`
- `src/memory/short_term.py` — `get_buffer()`
- `src/memory/long_term.py` — `retrieve_user_facts()`

**Ví dụ long_term_facts**:
```json
[
  {"key": "user.role", "value": "luật sư", "confidence": 0.9, "score": 0.87},
  {"key": "user.interest", "value": "luật doanh nghiệp", "confidence": 0.85, "score": 0.82}
]
```

Các facts này được inject vào system prompt dưới dạng:
```
NGỮ CẢNH NGƯỜI DÙNG:
- user.role: luật sư
- user.interest: luật doanh nghiệp
```

---

## 8. Bước 7 — Node: Retrieve Documents (Chi tiết)

### 8.1. Full Retrieval Pipeline

```mermaid
flowchart TB
    A["user_message:<br/>'Điều 12 Luật DN 2020<br/>quy định gì?'"] --> B{"rewrite = true?"}
    
    B -->|Yes| C["rewrite_query()<br/>LLM call → JSON {queries: [...]}"]
    B -->|No| D["queries = [user_message]"]
    
    C --> E["Cache check (Redis)<br/>key = sha256(model + query)"]
    E -->|Hit| F["Dùng cached queries"]
    E -->|Miss| G["Gemini Flash call<br/>temperature=0, max_tokens=200"]
    G --> H["Parse JSON → list[str]<br/>Cắt tối đa 3 queries"]
    H --> I["Cache set (TTL 10 phút)"]
    F --> J["Parallel vector_search()"]
    I --> J
    D --> J

    subgraph ParallelSearch["Parallel Vector Search (Qdrant)"]
        J --> K1["Search query 1"]
        J --> K2["Search query 2"]
        J --> K3["Search query 3"]
        K1 --> L["asyncio.gather()"]
        K2 --> L
        K3 --> L
    end

    L --> M["Dedup by point_id<br/>Giữ max(score)"]
    M --> N["Sort theo score → lấy top_k_search × 2"]
    N --> O["llm_rerank(query, pool, top_k=5)"]

    subgraph Rerank["LLM Rerank (Gemini Flash)"]
        O --> P["Build candidate text<br/>[0] heading + text[:500]<br/>[1] heading + text[:500]<br/>..."]
        P --> Q["Gemini Flash call<br/>response_format=json_object<br/>temperature=0"]
        Q --> R["Parse ranked JSON<br/>{ranked: [{id, score}, ...]}"]
        R --> S["Materialize: map id → SearchHit<br/>Fallback: unranked hits × 0.5"]
    end

    S --> T["Top-5 RetrievedChunk<br/>với doc_id, text, score, heading_path, page"]

    style ParallelSearch fill:#e8f8e8,stroke:#2ecc71
    style Rerank fill:#fff3e0,stroke:#ff9800
```

### 8.2. Vector Search — Chi tiết Qdrant Query

```mermaid
sequenceDiagram
    participant VS as vector_search()
    participant Emb as EmbeddingClient
    participant QD as Qdrant

    VS->>Emb: embed([query])
    Note over Emb: Cache check: L1 (LRU) → L2 (Redis) → L3 (Gemini API)
    Emb-->>VS: vector (768 dims)

    VS->>QD: search(<br/>collection = "docs",<br/>query_vector = vector,<br/>limit = 20,<br/>filter = {<br/>  must: [<br/>    tenant_id = "...",<br/>    user_id = "..." (optional),<br/>    doc_id IN [...] (optional)<br/>  ]<br/>},<br/>with_payload = true)

    QD-->>VS: List[ScoredPoint]
    
    Note over VS: Map payload → SearchHit:<br/>point_id, score, doc_id, doc_title,<br/>chunk_index, heading_path,<br/>page_from, page_to, text
```

**Files**:
- `src/agent/nodes/retrieval.py` — `retrieve_docs_node()`
- `src/retrieval/pipeline.py` — `retrieve_and_rerank()`, `rewrite_query()`
- `src/retrieval/search.py` — `vector_search()`
- `src/retrieval/rerank.py` — `llm_rerank()`

### 8.3. Rewrite Query — Ví dụ Input/Output

**Input**: `"So sánh quyền cổ đông theo Luật DN 2020 và 2014"`

**LLM Output**:
```json
{
  "queries": [
    "quyền cổ đông Luật Doanh nghiệp 2020",
    "quyền cổ đông Luật Doanh nghiệp 2014",
    "so sánh quy định cổ đông doanh nghiệp"
  ]
}
```

→ 3 queries song song vào Qdrant, mỗi query trả 20 hits → dedup → 40 unique → rerank → top 5.

---

## 9. Bước 8 — Build Prompt & Stream LLM

### 9.1. Prompt Assembly

```mermaid
flowchart TB
    subgraph Inputs
        A["SYSTEM_ANSWER<br/>(prompt cố định)"]
        B["conv.summary<br/>(tóm tắt hội thoại cũ, nếu có)"]
        C["long_term_facts<br/>(user context)"]
        D["retrieved chunks<br/>(5 đoạn tài liệu)"]
        E["short_term_history<br/>(20 messages gần nhất)"]
        F["user_message<br/>(câu hỏi hiện tại)"]
    end

    A --> G["build_answer_messages()"]
    B --> G
    C --> G
    D --> G
    E --> G
    F --> G

    G --> H["Final messages list"]

    subgraph FinalMessages["Messages gửi tới LLM"]
        H1["{ role: system, content:<br/>SYSTEM_ANSWER<br/>+ TÓM TẮT HỘI THOẠI TRƯỚC ĐÓ: ...<br/>+ NGỮ CẢNH NGƯỜI DÙNG:<br/>- user.role: luật sư<br/>+ TÀI LIỆU:<br/>[#1] Điều 12 Luật DN 2020...<br/>[#2] Điều 13...<br/>... }"]
        H2["{ role: user, content: 'msg cũ 1' }"]
        H3["{ role: assistant, content: 'trả lời cũ 1' }"]
        H4["..."]
        H5["{ role: user, content: 'Điều 12 quy định gì?' }"]
    end

    H --> H1
    H --> H2
    H --> H3
    H --> H4
    H --> H5
```

**File**: `src/agent/prompts.py` — `build_answer_messages()`, `format_context()`, `format_facts()`

### 9.2. LLM Streaming

```mermaid
sequenceDiagram
    participant Runner as run_agent_stream
    participant LLM as LLMClient.complete_stream()
    participant Gemini as Gemini API
    participant SSE as SSE Generator
    participant Client as Browser

    Runner->>LLM: complete_stream(messages, temperature=0.2)
    LLM->>Gemini: POST /chat/completions (stream=true)
    
    loop Mỗi chunk
        Gemini-->>LLM: ChatCompletionChunk
        LLM-->>Runner: chunk
        
        alt chunk.choices[0].delta.content exists
            Runner->>SSE: yield {"type":"token","data":"Điều"}
            SSE->>Client: event: token\ndata: Điều\n\n
        end
        
        alt chunk.usage exists (chunk cuối)
            Runner->>Runner: Lưu usage = {prompt_tokens, completion_tokens, total_tokens}
        end
    end

    Runner->>SSE: yield {"type":"done","data":{"answer":"...","citations":[...],"usage":{...}}}
    SSE->>Client: event: done\ndata: {"user_message_id":"...","citations":[...],...}\n\n
```

**Lưu ý**: Streaming **KHÔNG** qua cache. LLM client có 2 method:
- `complete()` → non-streaming, có cache (L1 LRU + L2 Redis)
- `complete_stream()` → streaming, bypass cache hoàn toàn

---

## 10. Bước 9 — Post-Stream: Persist & Enqueue

### 10.1. Sequence sau khi LLM stream xong

```mermaid
sequenceDiagram
    participant Gen as SSE Generator
    participant DB as PostgreSQL
    participant Redis as Redis
    participant ARQ as ARQ Queue

    Note over Gen: full_answer = "".join(all tokens)<br/>elapsed_ms = (perf_counter - t0) * 1000

    Gen->>DB: session_scope() — mở session MỚI
    Gen->>DB: ConversationRepo.add_message(<br/>  conv_id, role="assistant",<br/>  content=full_answer,<br/>  meta={"citations": [...]},<br/>  tokens_in=prompt_tokens,<br/>  tokens_out=completion_tokens,<br/>  latency_ms=elapsed_ms)
    DB->>DB: INSERT INTO messages
    DB->>DB: UPDATE conversations SET message_count += 1
    Gen->>DB: COMMIT

    Gen->>Redis: append_message(conv_id, "assistant", full_answer)
    Redis->>Redis: RPUSH conv:buf:{conv_id}
    Redis->>Redis: LTRIM -buffer_size -1
    Redis->>Redis: EXPIRE 86400s

    Gen->>ARQ: arq.enqueue_job("extract_facts", str(conv_id))
    Note over ARQ: Job chạy async bởi worker,<br/>KHÔNG block response

    Gen->>Gen: yield SSE event "done"
```

### 10.2. Tại sao dùng `session_scope()` mới?

```python
# ---- persist assistant message + buffer ----
async with session_scope() as s2:       # ← session MỚI, không phải session từ request
    await ConversationRepo(s2).add_message(...)
```

Session gốc từ FastAPI dependency (`SessionDep`) đã commit sau persist user message.
Trong SSE generator (async iterator), session gốc có thể đã expired hoặc bị GC.
Dùng `session_scope()` mới đảm bảo connection fresh + auto-commit/rollback.

### 10.3. Dữ liệu lưu vào bảng `messages`

```sql
INSERT INTO messages (
    id,                 -- uuid auto-gen
    conversation_id,    -- conv_id
    role,               -- "assistant"
    content,            -- full_answer (toàn bộ text)
    meta,               -- JSONB: {"citations": [{doc_id, chunk_id, doc_title, heading_path, page_from, page_to, score}]}
    tokens_in,          -- prompt_tokens (từ LLM usage)
    tokens_out,         -- completion_tokens
    latency_ms,         -- elapsed_ms
    created_at,         -- auto timestamptz
    updated_at          -- auto timestamptz
);
```

---

## 11. SSE Event Format — Client nhận được gì?

### 11.1. Sequence các SSE events

```mermaid
sequenceDiagram
    participant C as Client (EventSource)
    participant S as API (SSE)

    Note over S: Agent pipeline bắt đầu

    S->>C: event: tool_call<br/>data: {"name":"load_memory"}
    Note over C: UI hiển thị "Đang tải ngữ cảnh..."

    S->>C: event: tool_call<br/>data: {"name":"retrieve_docs"}
    Note over C: UI hiển thị "Đang tìm trong tài liệu..."

    S->>C: event: citations<br/>data: [{"doc_id":"...","doc_title":"Luật DN 2020",<br/>"heading_path":"Chương II > Điều 12","score":0.95}]
    Note over C: UI hiển thị citations panel

    loop Streaming tokens
        S->>C: event: token<br/>data: Theo
        S->>C: event: token<br/>data: Điều
        S->>C: event: token<br/>data: 12
        S->>C: event: token<br/>data: ...
    end

    S->>C: event: done<br/>data: {"user_message_id":"uuid",<br/>"citations":[...],"usage":{"prompt_tokens":1200,<br/>"completion_tokens":350},"latency_ms":2150.32}
    Note over C: UI hoàn thành, hiển thị usage info

    Note over S: Heartbeat mỗi 15s<br/>: ping
```

### 11.2. Event Types

| Event | Data | Mục đích |
|-------|------|----------|
| `tool_call` | `{"name":"load_memory"}` | UX feedback: agent đang làm gì |
| `citations` | `[{doc_id, doc_title, heading_path, score}]` | Hiển thị nguồn trích dẫn trước khi có answer |
| `token` | `"text"` | Streaming token-by-token cho typewriter effect |
| `done` | `{user_message_id, citations, usage, latency_ms}` | Kết thúc, metadata cho analytics |
| `error` | `"error message"` | Lỗi — client hiển thị thông báo |
| `: ping` | (comment line) | Heartbeat mỗi 15s giữ connection |

---

## 12. Diagram tổng hợp — Dữ liệu đọc/ghi ở đâu

```mermaid
flowchart LR
    subgraph ReadOps["ĐỌC (Read)"]
        R1["PostgreSQL<br/>• conversations (validate ownership)<br/>• messages (warmup buffer, extract_facts)<br/>• users (auth check)"]
        R2["Redis<br/>• conv:buf:{id} (short-term history)<br/>• Cache: rewrite, rerank, embedding"]
        R3["Qdrant<br/>• collection 'docs' (vector search)<br/>• collection 'memory' (user facts)"]
    end

    subgraph WriteOps["GHI (Write)"]
        W1["PostgreSQL<br/>• INSERT messages (user + assistant)<br/>• UPDATE conversations (count, timestamp)<br/>• INSERT user_facts (via worker)"]
        W2["Redis<br/>• RPUSH conv:buf:{id} (buffer)<br/>• SET cache keys (rewrite, rerank, embed)<br/>• ZADD rate limit<br/>• XADD ARQ job queue"]
        W3["Qdrant<br/>• UPSERT memory points (via worker)"]
        W4["Langfuse<br/>• Trace + spans + generation log"]
    end

    style ReadOps fill:#e8f4f8,stroke:#4a90d9
    style WriteOps fill:#ffeef0,stroke:#e74c3c
```

---

## 13. Error Handling & Recovery

### 13.1. Error Flow trong SSE Generator

```mermaid
flowchart TB
    A["try: async for evt in run_agent_stream()"] --> B{"Exception?"}
    B -->|No| C["Normal flow → yield events"]
    B -->|Yes within agent| D["Agent catches → yield error event<br/>+ return (stop generator)"]
    B -->|Yes in generator| E["Outer catch: log exception<br/>→ yield error event"]
    
    D --> F["Client nhận:<br/>event: error<br/>data: 'error message'"]
    E --> F

    subgraph Recovery["Auto-Recovery"]
        G["User message đã persisted<br/>→ không mất"]
        H["Redis buffer đã có user message<br/>→ consistent"]
        I["Client có thể retry<br/>→ gửi lại cùng message"]
    end

    F --> G
    F --> H
    F --> I
```

### 13.2. Các điểm failure và xử lý

| Failure Point | Hậu quả | Recovery |
|--------------|---------|----------|
| Gemini API timeout | `run_agent_stream` raise exception | Retry 3 lần (tenacity), sau đó yield error event |
| Qdrant down | `vector_search` raise | yield error event, user retry |
| Redis down (buffer) | `get_buffer` raise | Warmup từ Postgres fallback |
| Postgres down (persist) | `add_message` fail | Session rollback, yield error |
| LLM stream interrupted | Partial `full_answer` | Yield error, assistant message KHÔNG persist (chỉ persist khi stream xong) |

---

## 14. Performance — Latency Breakdown

```mermaid
gantt
    title Typical Chat Request Latency (~2.5s total)
    dateFormat X
    axisFormat %Lms

    section Pre-Agent
    Auth + Rate Limit           :0, 15
    Validate Conv + Persist Msg :15, 35
    Buffer Read/Warmup          :35, 40

    section Agent Pipeline
    Load Memory (Redis + Qdrant)    :40, 120
    Rewrite Query (LLM, cached)     :120, 150
    Vector Search (3 parallel)      :150, 300
    LLM Rerank                      :300, 600
    
    section LLM Streaming  
    First Token (TTFT)              :600, 800
    Token Streaming                 :800, 2300
    
    section Post-Stream
    Persist + Buffer + Enqueue      :2300, 2400
```

> **First Token**: Client nhận token đầu tiên sau ~800ms. Tổng latency ~2.5s cho response đầy đủ.
> Các bước có cache hit (rewrite, embedding) giảm ~200ms mỗi bước.

---

## 15. Tham chiếu Code

| Bước | File | Function |
|------|------|----------|
| HTTP Entry | `src/api/routes/chat.py` | `chat_stream()` |
| Middleware | `src/api/middleware/request_context.py` | `RequestContextMiddleware` |
| Auth | `src/api/deps.py` | `current_user()` |
| Rate Limit | `src/cache/rate_limit.py` | `allow_request()` |
| Conversation Repo | `src/db/repositories/conversation.py` | `ConversationRepo` |
| Short-term Buffer | `src/memory/short_term.py` | `get_buffer()`, `append_message()` |
| Agent Runner | `src/agent/graph.py` | `run_agent_stream()` |
| Memory Node | `src/agent/nodes/memory.py` | `load_memory_node()` |
| Retrieval Node | `src/agent/nodes/retrieval.py` | `retrieve_docs_node()` |
| Retrieval Pipeline | `src/retrieval/pipeline.py` | `retrieve_and_rerank()` |
| Vector Search | `src/retrieval/search.py` | `vector_search()` |
| LLM Rerank | `src/retrieval/rerank.py` | `llm_rerank()` |
| Prompt Builder | `src/agent/prompts.py` | `build_answer_messages()` |
| LLM Client | `src/llm/client.py` | `LLMClient.complete_stream()` |
| Langfuse | `src/observability/langfuse.py` | `observe()`, `manual_generation()` |
