SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: dev down test test-generator lint fmt migrate schema-sql seed contract-test create-admin e2e refresh-aggregates

.env:
	cp .env.example .env
	@echo "Created .env from .env.example - set a real POSTGRES_PASSWORD before use."

dev: .env ## Start db + api (reload) in the background
	@# ./logs is mounted at /var/log/zai (the F4 error log); the container user must be able to write.
	@# TEMPORARY dev shortcut (Linux hosts): must go before the project leaves dev - prod uses a chown'd
	@# directory, or run the dev service as the host user (user: DEV_UID:DEV_GID). Tracked in PROGRESS.md.
	mkdir -p logs && chmod 777 logs
	$(COMPOSE) up -d --build --wait

down: ## Stop everything (keeps the data volume)
	$(COMPOSE) down

test: .env ## Backend tests against the real Timescale DB (settings read from .env)
	$(COMPOSE) up -d --wait db
	cd backend && uv run pytest --cov

lint: ## ruff + mypy (backend and generator)
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd generator && uv run ruff check . && uv run ruff format --check . && uv run mypy

fmt: ## Auto-format and fix
	cd backend && uv run ruff check --fix . && uv run ruff format .
	cd generator && uv run ruff check --fix . && uv run ruff format .

test-generator: ## Generator unit tests (mocked API); the real-app test runs in `make test`
	cd generator && uv run pytest --cov

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

seed: ## Sample data (F11) via the API, real Open-Meteo data (internet); offline: SEED_ARGS="--source synthetic"
	uv run --frozen --project generator zai-sensor --api-url "$${ZAI_API_URL:-http://localhost:8000}" \
		seed --config generator/seed.example.toml --admin-user "$${ADMIN_USERNAME:-admin}" $(SEED_ARGS)

contract-test: ## Official suite, E1 stage: ADMIN_PASSWORD=... make contract-test
	@test -n "$$ADMIN_PASSWORD" || { echo "Set ADMIN_PASSWORD in the environment"; exit 1; }
	node contract/zai-tests.mjs --app http://localhost:8000 --user "$${ADMIN_USER:-admin}" --pass "$$ADMIN_PASSWORD" --stage E1

e2e: ## Official suite (E1 + E2) against a fresh runtime-image stack, like CI (stop `make dev` first)
	scripts/e2e.sh

refresh-aggregates: ## Materialise the hourly aggregate now (it refreshes itself every minute)
	$(COMPOSE) exec -T db sh -c "psql -U \"\$$POSTGRES_USER\" -d \"\$$POSTGRES_DB\" \
		-c \"CALL refresh_continuous_aggregate('measurements_hourly', NULL, NULL)\""
