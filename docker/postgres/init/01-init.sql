-- Tạo database phụ cho Langfuse, dùng chung Postgres server để gọn stack.
-- Khi scale lớn nên tách ra cluster riêng.
SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec

-- Extensions hữu ích cho app DB
\c rag
CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- text similarity index
CREATE EXTENSION IF NOT EXISTS "btree_gin";  -- mixed GIN index
