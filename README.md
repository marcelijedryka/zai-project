# ZAI 26Z – Warsaw Environment Monitor

IoT measurement-series web app (Warsaw air temperature and air quality).
FastAPI + PostgreSQL/TimescaleDB backend, React SPA (later), Python sensor emulator (later).

[![CI](https://github.com/marcelijedryka/zai-project/actions/workflows/ci.yml/badge.svg)](https://github.com/marcelijedryka/zai-project/actions/workflows/ci.yml)

Status: Phase 1 (core API) is complete and passes the official contract and validation tests
locally. The generator, realtime updates, frontend and deployment follow.

## Layout

| Path | Content |
|------|---------|
| `backend/` | FastAPI app, Alembic migrations, tests |
| `generator/` | `zai-sensor` data generator (sensor emulator), Phase 2 |
| `frontend/` | React SPA, E2 |
| `contract/` | Official OpenAPI contract and test script (unchanged copies) |
| `db/` | Generated `schema.sql` |
| `deploy/`, `infra/` | Deployment scripts and Terraform, Phase 3 |

## Local development

Requirements: Docker with Compose, [uv](https://docs.astral.sh/uv/) (Python 3.13 is pinned),
Node 22 (only for the official test script).

```bash
make dev                          # creates .env, starts TimescaleDB + the API (http://localhost:8000)
make migrate                      # apply the Alembic migrations
make create-admin                 # create the admin account (hidden password prompt)
ZAI_ADMIN_PASSWORD=... make seed  # TEMPORARY sample data, sent through the API only
make test                         # pytest against a real TimescaleDB, coverage gate 85 %
make lint                         # ruff + mypy (strict)
ADMIN_PASSWORD=... make contract-test   # the official suite, stage E1
```

Interactive API docs (dev only): <http://localhost:8000/api/docs>.

The official test script also runs directly:

```bash
node contract/zai-tests.mjs --app http://localhost:8000 --user admin --pass '<password>' --stage E1
node contract/zai-tests.mjs --app http://localhost:8000 --user admin --pass '<password>' --stage E2
```

Locally, three E2 checks cannot pass: HTTPS, the http-to-https redirect and the SPA's
Content-Security-Policy need the public deployment.

## Where to look

- `backend/app/api/` – HTTP routes; `backend/app/services/` – business rules;
  `backend/app/security/` – passwords, tokens, API keys, throttling, auth dependencies.
- `backend/tests/contract/test_official_contract_suite.py` – every official test ported to pytest.
- `db/schema.sql` – generated with `make schema-sql`.
