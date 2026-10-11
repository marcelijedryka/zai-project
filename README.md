# ZAI 26Z – Warsaw Environment Monitor

IoT measurement-series web app (Warsaw air temperature and air quality).
FastAPI + PostgreSQL/TimescaleDB backend, React SPA, Python sensor emulator.

Status: work in progress. See [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the
architecture and [`docs/PROGRESS.md`](docs/PROGRESS.md) for the current state.

## Layout

| Path | Content |
|------|---------|
| `backend/` | FastAPI app, Alembic migrations, tests |
| `generator/` | `zai-sensor` data generator (sensor emulator) |
| `frontend/` | React SPA (E2) |
| `contract/` | Official OpenAPI contract and test script (unchanged copies) |
| `db/` | Generated `schema.sql` |
| `deploy/`, `infra/` | Deployment scripts and Terraform |

## Quick start

Filled in as Phase 0 completes.
