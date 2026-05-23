# 07 — Evaluation Strategy

Hệ thống Agentic RAG có nhiều bước (rewrite → retrieve → rerank → generate),
mỗi bước có thể là điểm gãy. Doc này mô tả cách đo và cải thiện.

---

## 1. Mục tiêu đo

| Mức | Metric | Công cụ |
|-----|--------|---------|
| **Retrieval** | Recall@K (top-K có chứa chunk gold?) | Eval script + Qdrant |
| **Retrieval** | MRR / nDCG | Eval script |
| **Rerank** | Precision@5 | Eval script + LLM-judge |
| **Generation** | Faithfulness (answer có grounded trong context không) | RAGAS + Langfuse |
| **Generation** | Answer relevance | LLM-judge |
| **Generation** | Citation accuracy | Regex match `[#N]` với source |
| **System** | Latency p50/p95 | Langfuse + Prometheus |
| **System** | Token usage / cost | Langfuse |

---

## 2. Tạo eval dataset

### Cách 1: tay
50-100 cặp (question, expected_answer, expected_doc_ids).
Lưu Langfuse Dataset (UI hoặc SDK).

### Cách 2: bootstrap từ tài liệu
Worker script:
1. Lấy random chunk.
2. LLM (Gemini Pro, temp=0.3) sinh 2-3 câu hỏi mà chunk này trả lời được.
3. Người review nhanh → dataset.

```python
# scripts/bootstrap_eval.py (suggested)
QUESTIONS_PROMPT = """Đọc đoạn TÀI LIỆU dưới đây và sinh 2 câu hỏi mà đoạn này
trực tiếp trả lời được. Câu hỏi tự nhiên, đa dạng (mệnh đề what/how/khi nào)."""
```

---

## 3. Chạy eval với Langfuse

Mọi LLM/retrieve call qua agent đã được trace. Trên Langfuse:
- Tạo `Dataset` chứa eval items.
- Định nghĩa `Experiment` mapping mỗi item -> agent run.
- Định nghĩa scorer:
  - `faithfulness` (LLM-judge với prompt RAGAS).
  - `answer_relevance` (LLM-judge).
  - `context_precision` (boolean: gold doc có trong retrieved?).

---

## 4. RAGAS integration (suggested)

```python
# scripts/run_eval.py
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision

# 1. Load dataset từ Langfuse
# 2. Run agent cho mỗi item, thu thập (question, answer, contexts, ground_truth)
# 3. evaluate(...)
# 4. Push scores ngược về Langfuse
```

---

## 5. Regression test

Sau mỗi PR đổi prompt / chunker / retrieval:
- CI chạy eval script trên subset (10-20 items).
- So với baseline; nếu metric giảm >5% → block merge.

---

## 6. Drift detection (production)

Hàng tuần, sample 100 trace ngẫu nhiên từ Langfuse:
- LLM-judge score faithfulness.
- Báo cáo trend: nếu score giảm dần → có thể data mới (luật mới ban hành) đã
  được hỏi nhưng chưa được index → bổ sung ingestion.

---

## 7. Citation accuracy check

Script đơn giản:
1. Parse `[#N]` trong answer.
2. Với mỗi N, lấy text chunk gốc.
3. LLM check: "Câu trả lời có khẳng định điều mà chunk này hỗ trợ không?" → 0/1.

---

## 8. Cost dashboard

Trong Langfuse, mỗi generation log `usage`. Tổng theo:
- Per user / tenant (xác định power user).
- Per model (gemini-flash vs pro).
- Per node (rewrite/rerank/generate).

Trigger optimize khi 1 node chiếm >40% cost.
