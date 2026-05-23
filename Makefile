# Production Agentic RAG — common ops
# `make help` để xem list lệnh.

SHELL := /bin/bash
COMPOSE := docker compose
ENV_FILE := .env

.DEFAULT_GOAL := help

.PHONY: help env build up down restart logs ps shell migrate fresh test fmt lint typecheck \
        api-shell worker-shell psql redis-cli

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Copy .env.example -> .env (nếu chưa có)
	@test -f $(ENV_FILE) || (cp .env.example $(ENV_FILE) && echo "Created .env. Đừng quên điền secrets!")

build: env ## Build images
	$(COMPOSE) build

up: env ## Start full stack (detached)
	$(COMPOSE) up -d
	@echo "Stack starting... Theo dõi: make logs"
	@echo "- API:        http://localhost"
	@echo "- API docs:   http://localhost/docs"
	@echo "- Langfuse:   http://localhost:3000"
	@echo "- MinIO UI:   http://localhost:9001"
	@echo "- Qdrant:     http://localhost:6333/dashboard"

down: ## Stop stack (giữ volumes)
	$(COMPOSE) down

down-clean: ## Stop stack + xoá volumes (DỮ LIỆU SẼ MẤT)
	$(COMPOSE) down -v

restart: ## Restart cụ thể (make restart SERVICE=api)
	$(COMPOSE) restart $(SERVICE)

logs: ## Tail logs (make logs SERVICE=api)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

ps: ## List containers
	$(COMPOSE) ps

shell: ## Open shell trong service (make shell SERVICE=api)
	$(COMPOSE) exec $(SERVICE) bash

api-shell: ## Shortcut api shell
	$(COMPOSE) exec api bash

worker-shell: ## Shortcut worker shell
	$(COMPOSE) exec worker bash

psql: ## psql vào DB chính
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-rag} -d $${POSTGRES_DB:-rag}

redis-cli: ## redis-cli
	$(COMPOSE) exec redis redis-cli

migrate: ## Chạy DB migration thủ công (entrypoint đã tự chạy)
	$(COMPOSE) run --rm api migrate

fresh: down-clean build up migrate ## Wipe + rebuild + migrate

# ---- dev quality ----------------------------------------------------------
test: ## Run pytest local (cần uv + deps)
	uv run pytest -q

fmt: ## Ruff format
	uv run ruff format src tests

lint: ## Ruff check
	uv run ruff check src tests

typecheck: ## mypy
	uv run mypy src

ci: lint typecheck test ## Pre-commit pipeline
