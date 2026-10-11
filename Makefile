SHELL := /bin/bash
COMPOSE := docker compose
# Export .env values and build the host-side DATABASE_URL (db port is published on localhost).
HOST_ENV := set -a; . ./.env; set +a; \
	export DATABASE_URL="postgresql+asyncpg://$$POSTGRES_USER:$$POSTGRES_PASSWORD@localhost:5432/$$POSTGRES_DB"

.PHONY: dev down test lint fmt migrate schema-sql seed contract-test

.env:
	cp .env.example .env
	@echo "Created .env from .env.example - edit POSTGRES_PASSWORD before real use."

dev: .env ## Start db + api (reload) in the background
	$(COMPOSE) up -d --build --wait

down: ## Stop everything (keeps the data volume)
	$(COMPOSE) down

test: .env ## Backend tests against the real Timescale DB
	$(COMPOSE) up -d --wait db
	$(HOST_ENV); cd backend && uv run pytest --cov

lint: ## ruff + mypy
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy

fmt: ## Auto-format and fix
	cd backend && uv run ruff check --fix . && uv run ruff format .

migrate: dev ## Apply Alembic migrations
	$(COMPOSE) exec api alembic upgrade head

schema-sql: ## Regenerate db/schema.sql (offline Alembic)
	$(HOST_ENV); cd backend && uv run alembic upgrade head --sql > ../db/schema.sql

seed: ## Populate sample data through the API (needs the generator, Phase 2)
	@echo "Generator not implemented yet (Phase 2)"; exit 1

contract-test: ## Official suite, E1 stage (ADMIN_PASSWORD required)
	node contract/zai-tests.mjs --app http://localhost:8000 --user admin --pass '$(ADMIN_PASSWORD)' --stage E1
