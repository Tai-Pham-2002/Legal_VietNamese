# 06 — Prompt Templates

Tất cả prompt được tách thành module để dễ A/B test và version. Lý do từng prompt:

---

## 1. Query rewrite (`src/retrieval/pipeline.py::REWRITE_SYSTEM`)

**Mục đích**: chuyển 1 câu hỏi tự nhiên thành 1-3 truy vấn cô đọng cho vector search.

**Tại sao**: user thường hỏi dài dòng + đại từ ("vấn đề đó", "luật này"). Vector
search không hiểu đại từ và nhạy với từ thừa → recall giảm. Rewrite ép câu về
"keyword + concept" giúp embedding gần với chunks hơn.

**Cấu hình**: `temperature=0`, JSON object output, max 200 tokens, cache TTL 10
phút (cùng câu trong cùng cuộc hội thoại thường được dùng lại).

**Failure mode**: nếu LLM trả JSON không parse được → fallback dùng query gốc.
Tránh cascade lỗi.

---

## 2. LLM reranker (`src/retrieval/rerank.py::RERANK_SYSTEM`)

**Mục đích**: re-score N (≤20) candidates từ vector search dựa trên relevance
sâu, không chỉ similarity embedding.

**Tại sao**:
- Dense embedding tốt cho "topical similarity" nhưng yếu cho "answerability".
  Chunk có thể đề cập topic nhưng không trả lời được câu hỏi.
- Cross-encoder (BGE-reranker) chính xác hơn LLM nhưng cần host model.
- Gemini Flash $/token thấp, latency <1s, đủ tốt.

**Cấu hình**: temperature=0, JSON output, **trả id+score chứ không re-emit text**
→ tiết kiệm output token (giảm 10x).

**Cache key** = hash(query + sorted point_ids). Cache TTL 10 phút.

---

## 3. Answer generation (`src/agent/prompts.py::SYSTEM_ANSWER`)

**Mục đích**: trả lời từ TÀI LIỆU + ngữ cảnh hội thoại + user facts.

**Quy tắc cứng (hardcoded trong prompt)**:
1. **Anti-hallucination**: nếu không có info trong TÀI LIỆU, phải nói "không có
   thông tin" — không bịa.
2. **Citation cú pháp `[#N]`**: số tương ứng index trong TÀI LIỆU. UI parse và
   highlight được.
3. **Vietnamese-first** + ưu tiên trích dẫn điều/khoản cho legal docs.
4. **Co-reference resolution** từ short-term history (đại từ).

**Cấu hình streaming**: `temperature=0.2` (nhỏ để bám tài liệu, nhưng đủ tạo
câu mạch lạc), `max_tokens=1500`. KHÔNG cache (mỗi turn khác).

---

## 4. Long-term memory extraction (`src/worker/tasks/memory.py::EXTRACTION_SYSTEM`)

**Mục đích**: chạy background sau mỗi conversation/N messages, trích `user_facts`.

**Tại sao tách worker**: không block chat. Cũng dùng được retry nếu rate-limit.

**Output format**: `{facts:[{key,value,confidence}]}`, key snake_case English
để consistent (vd `user.role`, `user.preferred_language`).

**Dedupe**: trước khi insert, vector-search facts của cùng user, cùng `key`,
similarity > 0.92 → coi như dup → skip (giảm noise).

---

## 5. Tips khi tinh chỉnh prompts

### Bias dễ gặp
- LLM hay "diễn giải" câu trả lời thay vì trích nguyên văn → thêm câu "Giữ
  nguyên cụm từ pháp lý quan trọng".
- LLM bịa số điều/khoản → ép thêm "Chỉ trích điều/khoản có trong TÀI LIỆU".

### Đo lường
- Mỗi version prompt nên log version tag vào Langfuse metadata
  (`prompt_version: "v2"`).
- Eval set 50-100 câu hỏi-đáp benchmark trên Langfuse Datasets.

### Quy trình thay prompt
1. Branch git mới, sửa prompt + bump version tag.
2. Chạy eval script trên dataset.
3. So sánh metrics (faithfulness, answer relevance, citation accuracy).
4. Promote nếu tốt hơn.
