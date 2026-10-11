SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: dev down test lint fmt migrate schema-sql seed contract-test create-admin

.env:
	cp .env.example .env
	@echo "Created .env from .env.example - set a real POSTGRES_PASSWORD before use."

dev: .env ## Start db + api (reload) in the background
	$(COMPOSE) up -d --build --wait

down: ## Stop everything (keeps the data volume)
	$(COMPOSE) down

test: .env ## Backend tests against the real Timescale DB (settings read from .env)
	$(COMPOSE) up -d --wait db
	cd backend && uv run pytest --cov

lint: ## ruff + mypy
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy

fmt: ## Auto-format and fix
	cd backend && uv run ruff check --fix . && uv run ruff format .

migrate: dev ## Apply Alembic migrations
	@test -f backend/alembic.ini || { echo "Alembic is not set up yet (Phase 1)"; exit 1; }
	$(COMPOSE) exec api alembic upgrade head

create-admin: dev ## Create the admin (hidden prompt): make create-admin ADMIN_USERNAME=admin
	@# Scripted use: ZAI_ADMIN_PASSWORD=... plus `docker compose exec -T` (no TTY); the password
	@# is then visible in the process environment, so prefer the prompt on shared machines.
	$(COMPOSE) exec api python -m app.cli create-admin --username "$${ADMIN_USERNAME:-admin}"

schema-sql: ## Regenerate db/schema.sql (offline Alembic); the old file is kept on failure
	@test -f backend/alembic.ini || { echo "Alembic is not set up yet (Phase 1)"; exit 1; }
	cd backend && uv run alembic upgrade head --sql > ../db/schema.sql.tmp
	mv db/schema.sql.tmp db/schema.sql

seed: ## TEMPORARY sample data via the API (ZAI_ADMIN_PASSWORD=... make seed); Phase 2: zai-sensor
	cd backend && uv run python -m scripts.seed_temp --api-url "$${ZAI_API_URL:-http://localhost:8000}"

contract-test: ## Official suite, E1 stage: ADMIN_PASSWORD=... make contract-test
	@test -n "$$ADMIN_PASSWORD" || { echo "Set ADMIN_PASSWORD in the environment"; exit 1; }
	node contract/zai-tests.mjs --app http://localhost:8000 --user "$${ADMIN_USER:-admin}" --pass "$$ADMIN_PASSWORD" --stage E1
