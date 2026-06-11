"""Test config — đảm bảo env tối thiểu để get_settings() không lỗi khi thiếu .env."""

from __future__ import annotations

import os

# Giá trị mặc định an toàn cho unit test (không gọi mạng). Nếu .env đã có key thật
# (cho live test) thì setdefault không ghi đè.
os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ.setdefault("LLM_API_KEY", "test-llm-key")
os.environ.setdefault("RERANK_PROVIDER", "cohere")
os.environ.setdefault("COHERE_API_KEY", "test-cohere-key")
