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
make e2e                          # what CI runs: fresh runtime-image stack + official suite (E1 + E2)
```

Interactive API docs (dev only): <http://localhost:8000/api/docs>.

The official test script also runs directly:

```bash
node contract/zai-tests.mjs --app http://localhost:8000 --user admin --pass '<password>' --stage E1
node contract/zai-tests.mjs --app http://localhost:8000 --user admin --pass '<password>' --stage E2
```

The API keeps its login and password-change throttles in memory, so restarting it
(`docker compose restart api`) clears them. A successful login also clears the password-change
throttle, which is why repeated runs of the official script do not lock it.

Past measurement timestamps are accepted back to `EARLIEST_MEASUREMENT_TIMESTAMP`
(default 2000-01-01, configurable); older ones get 422. This is a deliberate limit on the
contract's "past timestamps are allowed", see the deviations below.

`make e2e` builds the production (`runtime`) image in its own compose project (`zai-e2e`, throw-away
volume), migrates, creates an admin, seeds, runs the official script for E1 and E2 and fails on any
`✘` (the script itself always exits 0, so `backend/scripts/check_official_output.py` parses its
output). Port 8000 must be free: run `make down` first. The stack runs with `ENVIRONMENT=prod`.
`--allow-deployment-only` is for these local runs only: the pre-submission run against the
deployed stack uses the plain checker and expects zero failures.

Locally, three E2 checks cannot pass: HTTPS, the http-to-https redirect and the SPA's
Content-Security-Policy need the public deployment.

## Deliberate deviations from the contract text

- **Earliest measurement timestamp:** the contract allows past timestamps. Ours accepts them back
  to `EARLIEST_MEASUREMENT_TIMESTAMP` (default `2000-01-01T00:00:00Z`) and answers 422
  (`urn:zai:problem:timestamp-too-old`) for older ones. Reason: every 7-day Timescale chunk is
  locked by queries over all data; about 2,900 scattered chunks (one per week since 1970) made
  `count(*)` fail with "out of shared memory", and about 1,370 (since 2000) still work. The limit
  stops one faulty or leaked sensor key from breaking the public listing.

## Where to look

- `backend/app/api/` – HTTP routes; `backend/app/services/` – business rules;
  `backend/app/security/` – passwords, tokens, API keys, throttling, auth dependencies.
- `backend/tests/contract/test_official_contract_suite.py` – every official test ported to pytest.
- `db/schema.sql` – generated with `make schema-sql`.
