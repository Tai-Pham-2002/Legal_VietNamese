"""
Centralized settings — load từ env, validate qua pydantic-settings.

Lý do tách nested (db/redis/qdrant/...): mỗi nhóm có thể inject riêng vào module
liên quan -> dễ test (override 1 nhóm), dễ đọc, và tránh "god object" Settings.

Singleton qua lru_cache: pydantic-settings parse env mỗi instance, cache để
tránh re-parse mỗi import.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Base(BaseSettings):
    """Common config — đọc env không phân biệt hoa-thường, prefix riêng cho mỗi sub."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class AppSettings(_Base):
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    service_name: str = "api"
    secret_key: SecretStr = Field(..., min_length=32)
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    request_timeout_s: int = 60

    # CORS — restrict ở prod
    cors_origins: list[str] = ["*"]

    # File upload
    max_upload_size_mb: int = 50
    max_files_per_request: int = 20
    allowed_mime_types: list[str] = [
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    ]


class DBSettings(_Base):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"), env_prefix="", extra="ignore", case_sensitive=False
    )
    postgres_dsn: str = Field(
        default="postgresql+asyncpg://rag:rag@localhost:5432/rag",
        description="Async DSN dùng cho SQLAlchemy.",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout_s: int = 30
    db_echo: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_dsn(self) -> str:
        """DSN sync (psycopg) — dùng cho Alembic migration và LangGraph checkpointer."""
        return self.postgres_dsn.replace("+asyncpg", "").replace(
            "postgresql://", "postgresql+psycopg://"
        )


class RedisSettings(_Base):
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50
    cache_ttl_llm_s: int = 3600          # 1h
    cache_ttl_embedding_s: int = 604800  # 7 ngày
    cache_ttl_retrieval_s: int = 300     # 5 phút
    short_term_buffer_size: int = 20
    short_term_buffer_ttl_s: int = 86400  # 24h


class QdrantSettings(_Base):
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_timeout_s: int = 30
    qdrant_collection_docs: str = "documents"
    qdrant_collection_memory: str = "memory"


class MinioSettings(_Base):
    minio_endpoint: str = "localhost:9000"
    minio_access_key: SecretStr = Field(default=SecretStr("minioadmin"))
    minio_secret_key: SecretStr = Field(default=SecretStr("minioadmin"))
    minio_bucket: str = "rag-files"
    minio_secure: bool = False


class LLMSettings(_Base):
    llm_provider: str = "gemini"
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: SecretStr
    llm_model_default: str = "gemini-2.5-flash"
    llm_model_heavy: str = "gemini-2.5-pro"
    llm_timeout_s: int = 60
    llm_max_retries: int = 3

    # text-embedding-004 đã bị Gemini gỡ; gemini-embedding-001 là model hiện hành.
    # Mặc định 3072 chiều nhưng hỗ trợ rút gọn qua param `dimensions` -> giữ 768
    # để khớp Qdrant collection (đỡ RAM, đủ tốt).
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    embedding_batch_size: int = 64
    embedding_timeout_s: int = 30


class RerankSettings(_Base):
    """Cấu hình rerank.

    `rerank_provider`:
      - "cohere": dùng Cohere Rerank API (mặc định, chất lượng cao, hỗ trợ tiếng Việt).
      - "llm":    dùng LLM-as-reranker (Gemini Flash) — fallback khi không có Cohere.
    Khi provider="cohere" mà gọi lỗi (timeout/quota/key sai) -> tự fallback sang "llm".
    """

    rerank_provider: Literal["cohere", "llm"] = "cohere"
    cohere_api_key: SecretStr | None = None
    cohere_rerank_model: str = "rerank-v3.5"
    rerank_timeout_s: int = 20
    rerank_max_candidates: int = 100  # giới hạn số document gửi cho Cohere/LLM

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cohere_enabled(self) -> bool:
        return self.rerank_provider == "cohere" and self.cohere_api_key is not None


class LangfuseSettings(_Base):
    langfuse_host: str = "http://langfuse-web:3000"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_enabled: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_configured(self) -> bool:
        return bool(
            self.langfuse_enabled
            and self.langfuse_public_key
            and self.langfuse_secret_key
        )


class SecuritySettings(_Base):
    jwt_alg: str = "HS256"
    jwt_access_ttl_min: int = 15
    jwt_refresh_ttl_days: int = 7
    password_min_length: int = 8
    rate_limit_chat_per_min: int = 60
    rate_limit_upload_per_min: int = 10


class Settings(_Base):
    """Aggregate — tất cả các nhóm settings."""

    app: AppSettings = Field(default_factory=AppSettings)
    db: DBSettings = Field(default_factory=DBSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor."""
    return Settings()
